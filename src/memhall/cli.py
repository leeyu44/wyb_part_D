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
                verdicts: list[Verdict], cases: dict[str, MemoryCase],
                judge_mode: str = "scripted") -> dict:
    from memhall.scoring.judge import JUDGE_PROMPT_VERSION
    manifest["judge"] = {  # 依赖锁定：判卷口径可追溯（design.md §10）
        "mode": judge_mode,
        "model_a": os.environ.get("JUDGE_A_MODEL", ""),
        "model_b": os.environ.get("JUDGE_B_MODEL", ""),
        "prompt_version": JUDGE_PROMPT_VERSION,
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
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
    if not case_dir.exists():  # deb 装机：用例在 /usr/share/memhall/cases
        deb_root = Path("/usr/share/memhall") / args.cases
        if deb_root.exists():
            case_dir = deb_root
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
    elif args.adapter == "hermes-local":
        from memhall.adapters.hermes_local import LocalHermesAdapter
        adapters["hermes-local"] = LocalHermesAdapter
    elif args.adapter == "claude-local":
        from memhall.adapters.claude_local import LocalClaudeAdapter
        adapters["claude-local"] = LocalClaudeAdapter
    elif args.adapter == "qwen-local":
        from memhall.adapters.qwen_local import LocalQwenAdapter
        adapters["qwen-local"] = LocalQwenAdapter
    elif args.adapter == "opencode":
        from memhall.adapters.opencode import OpenCodeAdapter
        adapters["opencode"] = OpenCodeAdapter
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
    metrics = _finish_run(run_dir, run_id, manifest, verdicts,
                          {c.case_id: c for c in cases}, judge_mode=args.judge)

    print(f"run_id: {run_id}")
    print(f"总体正确率: {metrics['overall_score']:.1%}"
          f"（有效 {metrics['n_valid']}/{metrics['n_probes_total']}，"
          f"规则判卷率 {metrics['rule_scoring_rate']:.0%}）")
    for cap, score in metrics["capability_scores"].items():
        print(f"  {cap:<14} {score:.0%}")
    print(f"产物: {run_dir}")
    from memhall.notify import notify_run_done
    notify_run_done(args.adapter, metrics["overall_score"],
                    metrics["n_valid"], metrics["n_probes_total"],
                    str(run_dir), radar=str(run_dir / "radar.png"))
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


def _resolve_run(p: str) -> Path:
    d = Path(p)
    if d.exists():
        return d
    alt = Path("runs") / p
    if alt.exists():
        return alt
    raise SystemExit(f"找不到运行目录: {p}（可用: runs/<run_id>，或完整路径）")


def cmd_compare(args: argparse.Namespace) -> int:
    from memhall.report import compare_runs
    out = compare_runs(_resolve_run(args.run_a), _resolve_run(args.run_b),
                       Path(args.out))
    print(f"{out['label_a']} vs {out['label_b']}")
    print(f"总体: {out['overall_a']:.1%} → {out['overall_b']:.1%}"
          f"　共同探测点 {out['n_common']}　判定翻转 {out['n_flips']}")
    print(f"产物: {out['radar']}  {out['report']}")
    return 0


def cmd_aggregate(args: argparse.Namespace) -> int:
    from memhall.report.aggregate import aggregate_runs, format_table
    result = aggregate_runs([_resolve_run(p) for p in args.runs], Path(args.out))
    print(format_table(result))
    print(f"产物: {Path(args.out) / 'aggregate.json'}")
    return 0


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


def _load_dotenv() -> None:
    """把 .env 装进进程环境（setdefault，手工 export 优先）。
    候选：源码=仓库根 / 打包=exe 同级 / deb 装机=~/memhall.env。
    所有 CLI 入口统一走这里——run/doctor 直跑也依赖 AGENT_LLM_* 等键。"""
    candidates = ([Path(sys.executable).resolve().parent / ".env"]
                  if getattr(sys, "frozen", False)
                  else [Path(__file__).resolve().parents[2] / ".env"])
    candidates.append(Path.home() / "memhall.env")  # deb 装机配置页的落点
    for env_file in candidates:
        if not env_file.exists():
            continue
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.split(" #")[0].strip())


def cmd_doctor(args: argparse.Namespace) -> int:
    from memhall.discovery import render_doctor, run_doctor
    rep = run_doctor(scan_remote=not args.no_vm)
    print(render_doctor(rep))
    return 0 if rep.usable_adapters() else 1


def cmd_systest(args: argparse.Namespace) -> int:
    try:
        from memhall.systests import run_systest
    except ImportError:
        print("系统级测试需要 paramiko（uv run / pip 安装），exe 单文件版不含", file=sys.stderr)
        return 2
    print("系统级测试将重启虚拟机并短暂断网（自动恢复），开始…")
    rep = run_systest(args.adapter, Path(args.out))
    for x in rep["results"]:
        print(f"  {'✅' if x.passed else '❌'} {x.zh}: {x.detail}")
    print(f"产物: {rep['run_dir']}")
    from memhall.notify import notify_run_done
    notify_run_done(f"systest-{args.adapter}",
                    rep["n_pass"] / rep["n_total"], rep["n_pass"], rep["n_total"],
                    rep["run_dir"], radar=f"{rep['run_dir']}/systest.png")
    return 0 if rep["n_pass"] == rep["n_total"] else 1


def cmd_ui(args: argparse.Namespace) -> int:
    import socket
    url = f"http://127.0.0.1:{args.port}/"
    # 单实例：菜单重复点击时第二份进程绑不上端口会无声退出，浏览器却连回旧实例，
    # 造成"重启了但没生效"的错觉——这里识别到已有实例就直接开浏览器走人。
    probe = socket.socket()
    probe.settimeout(0.5)
    try:
        probe.connect(("127.0.0.1", args.port))
        alive = True
    except OSError:
        alive = False
    finally:
        probe.close()
    if alive:
        import json
        import urllib.request
        try:
            with urllib.request.urlopen(f"{url}api/meta", timeout=2) as r:
                ours = "version" in json.load(r)
        except Exception:
            ours = False
        if ours:
            if not args.no_open:
                import webbrowser
                webbrowser.open(url)
            print(f"已有麟阁实例在 {url}，直接打开（不再重复启动）")
            return 0
        print(f"端口 {args.port} 被其他程序占用", file=sys.stderr)
        return 1
    from memhall.ui.app import create_app
    app = create_app()
    if args.window:
        return _run_window(app)
    import threading
    import webbrowser
    threading.Timer(1.2, lambda: webbrowser.open(url)).start() if not args.no_open else None
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
    _load_dotenv()
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

    p_cmp = sub.add_parser("compare", help="对比两次运行：对比雷达 + 判定翻转明细")
    p_cmp.add_argument("run_a", help="运行 A（runs/<run_id> 或完整路径）")
    p_cmp.add_argument("run_b", help="运行 B")
    p_cmp.add_argument("-o", "--out", default="runs/_compare", help="输出目录")
    p_cmp.set_defaults(func=cmd_compare)

    p_agg = sub.add_parser("aggregate", help="N 轮重跑聚合成 mean±std（方差口径）")
    p_agg.add_argument("runs", nargs="+", help="N 个运行（runs/<run_id> 或完整路径）")
    p_agg.add_argument("-o", "--out", default="runs/_aggregate", help="输出目录")
    p_agg.set_defaults(func=cmd_aggregate)

    p_sys = sub.add_parser("systest", help="系统级测试：重启/拨钟/多用户/断网（真机真做）")
    p_sys.add_argument("-a", "--adapter", default="hermes", help="VM 内适配器")
    p_sys.add_argument("-o", "--out", default="runs", help="输出根目录")
    p_sys.set_defaults(func=cmd_systest)

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
