"""CLI 入口：memhall run / report。

run:    载入用例 → Runner 编排 → 评分引擎 → 指标/雷达图/报告，一次出齐
report: 对已有 run 目录重渲染报告（不重跑智能体）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

from memhall.adapters.mock import MockAdapter
from memhall.runner.orchestrator import run_suite
from memhall.schema.models_case import MemoryCase
from memhall.scoring.engine import evaluate_case
from memhall.scoring.judge import OpenAICompatJudge
from memhall.report import compute_metrics, render_radar, render_report
from memhall.schema.evidence import Verdict


def load_cases(case_dir: Path) -> list[MemoryCase]:
    cases = []
    for path in sorted(case_dir.rglob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        cases.append(MemoryCase.model_validate(raw))
    return cases


def _finish_run(run_dir: Path, run_id: str, manifest: dict,
                verdicts: list[Verdict], cases: dict[str, MemoryCase]) -> dict:
    metrics = compute_metrics(verdicts, cases)
    (run_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    with (run_dir / "verdicts.jsonl").open("w", encoding="utf-8") as f:
        for v in verdicts:
            f.write(v.model_dump_json() + "\n")
    render_radar({manifest.get("adapter", "agent"): metrics["capability_scores"]},
                 str(run_dir / "radar.png"))
    report = render_report(run_dir, run_id, manifest, verdicts, cases, metrics)
    (run_dir / "report.md").write_text(report, encoding="utf-8")
    return metrics


def cmd_run(args: argparse.Namespace) -> int:
    case_dir = Path(args.cases)
    cases = load_cases(case_dir)
    if not cases:
        print(f"未找到用例: {case_dir}", file=sys.stderr)
        return 1

    adapters: dict = {"mock": MockAdapter}
    if args.adapter == "hermes":
        from memhall.adapters.hermes import HermesAdapter
        adapters["hermes"] = HermesAdapter
    elif args.adapter == "kylinbot":
        from memhall.adapters.kylinbot import KylinBotAdapter
        adapters["kylinbot"] = KylinBotAdapter
    if args.adapter not in adapters:
        print(f"未知适配器: {args.adapter}（可选: {', '.join(adapters)}）", file=sys.stderr)
        return 1
    adapter = adapters[args.adapter]()

    judges = OpenAICompatJudge.pair_from_env() if args.judge == "dual" else None
    if args.judge == "dual" and judges is None:
        print("缺少 JUDGE_A_ 环境变量，回退脚本判卷", file=sys.stderr)

    run_id, stores = run_suite(adapter, cases, Path(args.out), args.adapter)
    run_dir = Path(args.out) / run_id
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))

    verdicts = []
    for case, store in zip(cases, stores):
        verdicts.extend(evaluate_case(case, store, run_id, judges))
    metrics = _finish_run(run_dir, run_id, manifest, verdicts, {c.case_id: c for c in cases})

    print(f"run_id: {run_id}")
    print(f"总体正确率: {metrics['overall_score']:.1%}"
          f"（有效 {metrics['n_valid']}/{metrics['n_probes_total']}，"
          f"规则判卷率 {metrics['rule_scoring_rate']:.0%}）")
    for cap, score in metrics["capability_scores"].items():
        print(f"  {cap:<14} {score:.0%}")
    print(f"产物: {run_dir}")
    return 0


def _load_verdicts(run_dir: Path, manifest: dict,
                   cases: dict[str, MemoryCase], judges) -> list[Verdict]:
    """优先读已落盘 verdicts；缺则从证据 JSONL 重放评分（评测贵、评分便宜）。"""
    vpath = run_dir / "verdicts.jsonl"
    if vpath.exists():
        return [Verdict.model_validate(json.loads(line))
                for line in vpath.read_text(encoding="utf-8").splitlines()]
    from memhall.schema.evidence import Evidence
    from memhall.scoring.engine import evaluate_case
    from memhall.scoring.rules import EvidenceStore
    verdicts: list[Verdict] = []
    for cid in manifest["cases"]:
        case = cases[cid]
        ev_path = run_dir / "cases" / cid / "evidence.jsonl"
        store = EvidenceStore([Evidence.model_validate(json.loads(line))
                               for line in ev_path.read_text(encoding="utf-8").splitlines()])
        verdicts.extend(evaluate_case(case, store, manifest["run_id"], judges))
    return verdicts


def cmd_report(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    repo_root = Path(__file__).resolve().parents[2]
    cases = {c.case_id: c for c in load_cases(repo_root / "cases")}
    judges = OpenAICompatJudge.pair_from_env() if args.judge == "dual" else None
    verdicts = _load_verdicts(run_dir, manifest, cases, judges)
    metrics = _finish_run(run_dir, manifest["run_id"], manifest, verdicts, cases)
    print(f"报告已出: {run_dir / 'report.md'}（总体 {metrics['overall_score']:.1%}）")
    return 0


def _ensure_streams() -> None:
    """窗口模式 exe（console=False）双击启动时无控制台，sys.stdout/stderr
    为 None——print/logging 一碰就崩。重定向到 exe 同级 memhall.log，
    写不进（只读目录等）则退临时目录。"""
    if not (getattr(sys, "frozen", False)
            and (sys.stdout is None or sys.stderr is None)):
        return
    import tempfile
    from pathlib import Path
    for base in (Path(sys.executable).resolve().parent,
                 Path(tempfile.gettempdir())):
        try:
            log = (base / "memhall.log").open("a", encoding="utf-8")
        except OSError:
            continue
        if sys.stdout is None:
            sys.stdout = log
        if sys.stderr is None:
            sys.stderr = log
        break


def _utf8_console() -> None:
    """Windows 控制台默认 GBK 代码页，中文输出乱码——统一改 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        if stream and stream.encoding and stream.encoding.lower() not in ("utf-8", "utf8"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except AttributeError:
                pass  # 非 TextIOWrapper（重定向到文件等）时不动


def cmd_doctor(args: argparse.Namespace) -> int:
    from memhall.discovery import render_doctor, run_doctor
    rep = run_doctor(scan_remote=not args.no_vm)
    print(render_doctor(rep))
    return 0 if rep.usable_adapters() else 1


def cmd_ui(args: argparse.Namespace) -> int:
    from pathlib import Path as _P
    _env_file = (_P(sys.executable).resolve().parent / ".env"
                 if getattr(sys, "frozen", False)
                 else _P(__file__).resolve().parents[2] / ".env")
    if _env_file.exists():  # UI 进程不吃手工 source，自动装 .env
        for line in _env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.split(" #")[0].strip())
    from memhall.ui.app import create_app
    app = create_app()
    if args.window:
        return _run_window(app)
    import threading
    import webbrowser
    url = f"http://127.0.0.1:{args.port}/"
    if not args.no_open:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"麟阁 Web UI: {url}（Ctrl+C 退出）")
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def _run_window(app) -> int:
    """原生窗口壳：优先 pywebview（真原生窗口+任务栏图标）；打包环境缺
    pythonnet/WebView2 时退 Edge 应用模式窗口（无地址栏，观感接近原生）。"""
    import shutil
    import socket
    import subprocess
    import threading
    import time
    import urllib.request

    import uvicorn

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{port}/"
    for _ in range(50):  # 等服务就绪再开窗，避免白屏
        try:
            urllib.request.urlopen(f"{url}api/meta", timeout=1).read()
            break
        except OSError:
            time.sleep(0.2)
    try:
        import webview
        webview.create_window("麟阁 MemHall · 智能体记忆评测", url,
                              width=1280, height=880, min_size=(980, 640))
        webview.start()
        return 0
    except Exception:
        pass
    _open_app_window(url)
    t.join()
    return 0


def _open_app_window(url: str) -> None:
    """Edge/Chrome 的 --app 窗口（无地址栏）；都没有则普通浏览器。"""
    import os
    import webbrowser

    cands = [shutil.which("msedge"), shutil.which("chrome"),
             os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
             os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe")]
    for path in cands:
        if path and os.path.isfile(path):
            import subprocess
            subprocess.Popen([path, f"--app={url}"])
            return
    webbrowser.open(url)


def main() -> None:
    _ensure_streams()
    _utf8_console()
    if len(sys.argv) == 1 and getattr(sys, "frozen", False):
        sys.argv = ["memhall", "ui", "--window"]  # 双击 exe = 直接开窗口
    parser = argparse.ArgumentParser(prog="memhall",
                                     description="麟阁：智能体记忆能力评测基准")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="跑一轮评测并出报告")
    p_run.add_argument("-a", "--adapter", default="mock", help="适配器名（默认 mock）")
    p_run.add_argument("-c", "--cases", default="cases/full", help="用例目录")
    p_run.add_argument("-o", "--out", default="runs", help="输出根目录")
    p_run.add_argument("--judge", choices=["scripted", "dual"], default="scripted",
                       help="判卷方式（dual=LLM 判卷[单判或双判，按 JUDGE_B 是否配置]）")
    p_run.set_defaults(func=cmd_run)

    p_rep = sub.add_parser("report", help="出报告（缺 verdicts 时从证据重放评分）")
    p_rep.add_argument("run_dir", help="runs/ 下的 run 目录")
    p_rep.add_argument("--judge", choices=["scripted", "dual"], default="scripted",
                       help="重放评分时的判卷方式")
    p_rep.set_defaults(func=cmd_report)

    p_doc = sub.add_parser("doctor", help="一键发现本机/评测机智能体，体检评测环境")
    p_doc.add_argument("--no-vm", action="store_true", help="跳过评测机 SSH 扫描")
    p_doc.set_defaults(func=cmd_doctor)

    p_ui = sub.add_parser("ui", help="启动 Web UI（本地服务 + 自动开浏览器）")
    p_ui.add_argument("--port", type=int, default=8300, help="端口（默认 8300）")
    p_ui.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    p_ui.add_argument("--window", action="store_true",
                      help="原生窗口模式（pywebview，exe 双击默认）")
    p_ui.set_defaults(func=cmd_ui)

    args = parser.parse_args()
    raise SystemExit(args.func(args))
