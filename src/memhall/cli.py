"""CLI 入口：memhall run / report。

run:    载入用例 → Runner 编排 → 评分引擎 → 指标/雷达图/报告，一次出齐
report: 对已有 run 目录重渲染报告（不重跑智能体）
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from memhall.adapters.mock import MockAdapter
from memhall.runner.orchestrator import _atomic_json, _file_sha256, run_suite
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


def load_run_cases(run_dir: Path) -> list[MemoryCase]:
    """Load the immutable case snapshot captured with a run."""
    path = run_dir / "cases.json"
    if not path.is_file():
        repo_root = Path(__file__).resolve().parents[2]
        return load_cases(repo_root / "cases")
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [MemoryCase.model_validate(item) for item in raw.get("cases", [])]


def _adapter_factory(name: str):
    if name == "mock":
        return MockAdapter
    if name == "hermes":
        from memhall.adapters.hermes import HermesAdapter
        return HermesAdapter
    if name == "kylinbot":
        from memhall.adapters.kylinbot import KylinBotAdapter
        return KylinBotAdapter
    if name == "hermes-local":
        from memhall.adapters.hermes_local import LocalHermesAdapter
        return LocalHermesAdapter
    if name == "claude-local":
        from memhall.adapters.claude_local import LocalClaudeAdapter
        return LocalClaudeAdapter
    if name == "qwen-local":
        from memhall.adapters.qwen_local import LocalQwenAdapter
        return LocalQwenAdapter
    if name == "opencode":
        from memhall.adapters.opencode import OpenCodeAdapter
        return OpenCodeAdapter
    return None


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("必须是正整数")
    return number


def _judge_token_cost(judges, mode: str) -> dict:
    endpoints = [judge.usage_summary() for judge in (judges or ())
                 if hasattr(judge, "usage_summary")]
    completions = sum(int(item.get("completions", 0)) for item in endpoints)
    reported = sum(int(item.get("reported_completions", 0)) for item in endpoints)
    return {
        "mode": mode,
        "endpoints": endpoints,
        "http_requests": sum(int(item.get("http_requests", 0)) for item in endpoints),
        "completions": completions,
        "reported_completions": reported,
        "unreported_completions": completions - reported,
        "coverage": round(reported / completions, 4) if completions else None,
        "prompt": sum(int(item.get("prompt", 0)) for item in endpoints),
        "completion": sum(int(item.get("completion", 0)) for item in endpoints),
        "total": sum(int(item.get("total", 0)) for item in endpoints),
    }


def _atomic_text(path: Path, content: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def _finish_run(run_dir: Path, run_id: str, manifest: dict,
                verdicts: list[Verdict], cases: dict[str, MemoryCase],
                judge_mode: str | None = "scripted", judges=None,
                requested_judge_mode: str | None = None) -> dict:
    from memhall.scoring.judge import JUDGE_PROMPT_VERSION
    if judge_mode is not None or "judge" not in manifest:
        effective_mode = judge_mode or "scripted"
        models = [judge.name for judge in (judges or ())]
        manifest["judge"] = {  # 判卷口径和实际模型可追溯
            "requested_mode": requested_judge_mode or effective_mode,
            "mode": effective_mode,
            "models": models,
            "model_a": models[0] if models else "",
            "model_b": models[1] if len(models) > 1 else "",
            "prompt_version": JUDGE_PROMPT_VERSION,
        }
        manifest.setdefault("cost", {})["judge_tokens"] = _judge_token_cost(
            judges, effective_mode)
    metrics = compute_metrics(verdicts, cases)
    _atomic_json(run_dir / "metrics.json", metrics)
    _atomic_text(
        run_dir / "verdicts.jsonl",
        "".join(verdict.model_dump_json() + "\n" for verdict in verdicts),
    )
    radar_path = run_dir / "radar.png"
    radar_tmp = radar_path.with_name(f".{radar_path.stem}.{os.getpid()}.tmp.png")
    try:
        render_radar(
            {manifest.get("adapter", "agent"): metrics["capability_scores"]},
            str(radar_tmp),
        )
        os.replace(radar_tmp, radar_path)
    finally:
        if radar_tmp.exists():
            radar_tmp.unlink()
    report = render_report(run_dir, run_id, manifest, verdicts, cases, metrics)
    _atomic_text(run_dir / "report.md", report)
    manifest["output_hashes"] = {
        name: _file_sha256(run_dir / name)
        for name in ("verdicts.jsonl", "metrics.json", "report.md", "radar.png")
    }
    _atomic_json(run_dir / "manifest.json", manifest)
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

    factory = _adapter_factory(args.adapter)
    if factory is None:
        supported = ("mock", "hermes", "kylinbot", "hermes-local",
                     "claude-local", "qwen-local", "opencode")
        print(f"未知适配器: {args.adapter}（可选: {', '.join(supported)}）",
              file=sys.stderr)
        return 1

    judges = OpenAICompatJudge.pair_from_env() if args.judge == "dual" else None
    if args.judge == "dual" and judges is None:
        print("缺少 JUDGE_A_ 环境变量，回退脚本判卷", file=sys.stderr)

    repeat_count = max(1, int(getattr(args, "repeat", 1)))
    seed = int(getattr(args, "seed", 42))
    repeat_group = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    run_dirs: list[Path] = []
    first_run_id: str | None = None
    integrity_failed = False
    vm_manager = None
    if getattr(args, "prepare_vm", False):
        try:
            from memhall.vm import VmwareManager
            vm_manager = VmwareManager.from_env()
        except Exception as error:
            print(f"VM 初始化失败: {error}", file=sys.stderr)
            return 2
    for repeat_index in range(1, repeat_count + 1):
        if vm_manager is not None:
            try:
                health = vm_manager.prepare()
            except Exception as error:
                print(f"第 {repeat_index} 轮 VM 基线准备失败: {error}", file=sys.stderr)
                return 2
            target = health.get("environment", {}).get("os", "openKylin")
            print(f"第 {repeat_index}/{repeat_count} 轮 VM 已回滚并就绪: {target}")
        adapter = factory()
        run_id, stores = run_suite(
            adapter, cases, Path(args.out), args.adapter,
            case_sample_seed=seed,
            repeat_of=first_run_id if repeat_index > 1 else None,
            repeat_group=repeat_group,
            repeat_index=repeat_index,
            repeat_count=repeat_count,
        )
        first_run_id = first_run_id or run_id
        run_dir = Path(args.out) / run_id
        run_dirs.append(run_dir)
        manifest = json.loads(
            (run_dir / "manifest.json").read_text(encoding="utf-8"))

        verdicts = []
        for case, store in zip(cases, stores):
            verdicts.extend(evaluate_case(case, store, run_id, judges))
        actual_judge_mode = ("dual" if judges and len(judges) > 1
                             else "single" if judges else "scripted")
        metrics = _finish_run(
            run_dir, run_id, manifest, verdicts,
            {case.case_id: case for case in cases},
            judge_mode=actual_judge_mode, judges=judges,
            requested_judge_mode=args.judge)

        from memhall.runner.verify import verify_run
        verification = verify_run(run_dir)
        integrity_failed = integrity_failed or not verification.ok
        manifest = json.loads(
            (run_dir / "manifest.json").read_text(encoding="utf-8"))
        manifest["verification"] = {
            "ok": verification.ok,
            "n_evidence": verification.n_evidence,
            "errors": verification.errors,
        }
        _atomic_json(run_dir / "manifest.json", manifest)

        label = (f"第 {repeat_index}/{repeat_count} 轮 "
                 if repeat_count > 1 else "")
        print(f"{label}run_id: {run_id}")
        print(f"总体正确率: {metrics['overall_score']:.1%}"
              f"（有效 {metrics['n_valid']}/{metrics['n_probes_total']}，"
              f"规则判卷率 {metrics['rule_scoring_rate']:.0%}）")
        for cap, score in metrics["capability_scores"].items():
            print(f"  {cap:<14} {score:.0%}")
        print(f"证据校验: {'通过' if verification.ok else '失败'}"
              f"（{verification.n_evidence} 条）")
        if verification.errors:
            for error in verification.errors:
                print(f"  - {error}", file=sys.stderr)
        print(f"产物: {run_dir}")
        from memhall.notify import notify_run_done
        notify_run_done(args.adapter, metrics["overall_score"],
                        metrics["n_valid"], metrics["n_probes_total"],
                        str(run_dir), radar=str(run_dir / "radar.png"))

    if repeat_count > 1:
        from memhall.report import analyze_stability
        stability_dir = (Path(args.out)
                         / f"{repeat_group}-stability-{args.adapter}")
        stability = analyze_stability(run_dirs, stability_dir)
        print(f"稳定性: 判定一致率 {stability['verdict_agreement_rate']:.1%}，"
              f"pass^{repeat_count} {stability['pass_all_rate']:.1%}，"
              f"标准差 {stability['overall_stddev']:.1%}")
        print(f"稳定性报告: {stability_dir / 'stability.md'}")
    return 2 if integrity_failed else 0


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


def cmd_report(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    cases = {case.case_id: case for case in load_run_cases(run_dir)}
    judges = OpenAICompatJudge.pair_from_env() if args.judge == "dual" else None
    had_verdicts = (run_dir / "verdicts.jsonl").is_file()
    verdicts = _load_verdicts(run_dir, manifest, cases, judges)
    judge_mode = None if had_verdicts else (
        "dual" if judges and len(judges) > 1 else "single" if judges else "scripted")
    metrics = _finish_run(
        run_dir, manifest["run_id"], manifest, verdicts, cases,
        judge_mode=judge_mode, judges=judges,
        requested_judge_mode=args.judge)
    print(f"报告已出: {run_dir / 'report.md'}（总体 {metrics['overall_score']:.1%}）")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    from memhall.runner.verify import verify_run
    run_dir = _resolve_run(args.run_dir)
    result = verify_run(run_dir)
    print(f"run_id: {result.run_id or run_dir.name}")
    print(f"证据完整性: {'通过' if result.ok else '失败'}"
          f"（{result.n_cases} cases / {result.n_evidence} evidence）")
    for warning in result.warnings:
        print(f"  提示: {warning}")
    for error in result.errors:
        print(f"  错误: {error}", file=sys.stderr)
    return 0 if result.ok else 1


def cmd_stability(args: argparse.Namespace) -> int:
    from memhall.report import analyze_stability
    run_dirs = [_resolve_run(item) for item in args.runs]
    result = analyze_stability(run_dirs, Path(args.out))
    print(f"重复轮数: {result['n_repeats']}　共同探测点: "
          f"{result['n_common_probes']}")
    print(f"判定一致率: {result['verdict_agreement_rate']:.1%}　"
          f"pass^{result['n_repeats']}: {result['pass_all_rate']:.1%}　"
          f"总体分标准差: {result['overall_stddev']:.1%}")
    print(f"产物: {Path(args.out) / 'stability.md'}")
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


def cmd_vm(args: argparse.Namespace) -> int:
    from dataclasses import asdict
    from memhall.vm import VmError, VmwareManager
    try:
        manager = VmwareManager.from_env()
        action = args.vm_action
        if action == "status":
            print(json.dumps(asdict(manager.status()), ensure_ascii=False, indent=2))
        elif action == "start":
            manager.start(timeout_s=args.timeout)
            print("openKylin VM 已启动，SSH 就绪")
        elif action == "stop":
            manager.stop(args.mode)
            print(f"openKylin VM 已关闭（{args.mode}）")
        elif action == "snapshot":
            manager.create_snapshot(args.name)
            print(f"快照已创建: {args.name}")
        elif action == "delete-snapshot":
            manager.delete_snapshot(args.name)
            print(f"快照已删除: {args.name}")
        elif action == "revert":
            target = args.name or manager.snapshot_name
            manager.revert(target, timeout_s=args.timeout)
            print(f"已回滚并启动: {target}")
        elif action == "prepare":
            health = manager.prepare(timeout_s=args.timeout)
            print(json.dumps(health, ensure_ascii=False, indent=2))
        elif action == "health":
            print(json.dumps(manager.health(), ensure_ascii=False, indent=2))
        else:
            raise VmError(f"未知 VM 操作: {action}")
    except (VmError, OSError, subprocess.SubprocessError) as error:
        print(f"VM 操作失败: {error}", file=sys.stderr)
        return 2
    return 0


def cmd_systest(args: argparse.Namespace) -> int:
    try:
        from memhall.systests import run_systest
    except ImportError:
        print("系统级测试需要 paramiko（uv run / pip 安装），exe 单文件版不含", file=sys.stderr)
        return 2
    print("系统级测试将重启虚拟机并短暂断网（自动恢复），开始…")
    rep = run_systest(args.adapter, Path(args.out))
    for x in rep["results"]:
        icon = "⏭" if x.skipped else ("✅" if x.passed else "❌")
        print(f"  {icon} {x.zh}: {x.detail}")
    print(f"产物: {rep['run_dir']}")
    from memhall.notify import notify_run_done
    notify_run_done(f"systest-{args.adapter}",
                    rep["n_pass"] / max(1, rep["n_total"]),
                    rep["n_pass"], rep["n_total"],
                    rep["run_dir"], radar=f"{rep['run_dir']}/systest.png")
    return 0 if rep["n_pass"] == rep["n_total"] else 1


def cmd_ui(args: argparse.Namespace) -> int:
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
    _load_dotenv()
    if len(sys.argv) == 1 and getattr(sys, "frozen", False):
        sys.argv = ["memhall", "ui", "--window"]  # 双击 exe = 直接开窗口
    parser = argparse.ArgumentParser(prog="memhall",
                                     description="麟阁：智能体记忆能力评测基准")
    from memhall import __version__
    parser.add_argument("--version", action="version",
                        version=f"memhall {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="跑一轮评测并出报告")
    p_run.add_argument("-a", "--adapter", default="mock", help="适配器名（默认 mock）")
    p_run.add_argument("-c", "--cases", default="cases/full", help="用例目录")
    p_run.add_argument("-o", "--out", default="runs", help="输出根目录")
    p_run.add_argument("--judge", choices=["scripted", "dual"], default="scripted",
                       help="判卷方式（dual=LLM 判卷[单判或双判，按 JUDGE_B 是否配置]）")
    p_run.add_argument("--seed", type=int, default=42,
                       help="用例抽样/生成种子，写入 manifest（默认 42）")
    p_run.add_argument("--repeat", type=_positive_int, default=1,
                       help="完整重复运行次数；大于 1 时自动生成稳定性报告")
    p_run.add_argument(
        "--prepare-vm", action="store_true",
        help="每轮开始前回滚 VM_SNAPSHOT、启动并等待 SSH（远端适配器推荐）")
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

    p_stable = sub.add_parser("stability", help="汇总两次以上运行的稳定性与 pass^k")
    p_stable.add_argument("runs", nargs="+", help="运行目录或 runs/<run_id>")
    p_stable.add_argument("-o", "--out", default="runs/_stability",
                          help="稳定性报告输出目录")
    p_stable.set_defaults(func=cmd_stability)

    p_verify = sub.add_parser("verify", help="校验证据哈希、ID 和阶段采集完整性")
    p_verify.add_argument("run_dir", help="运行目录或 run_id")
    p_verify.set_defaults(func=cmd_verify)

    p_vm = sub.add_parser("vm", help="管理 openKylin VMware 评测机")
    vm_sub = p_vm.add_subparsers(dest="vm_action", required=True)
    vm_status = vm_sub.add_parser("status", help="查看电源、快照和 SSH 状态")
    vm_status.set_defaults(func=cmd_vm)
    vm_start = vm_sub.add_parser("start", help="无界面启动并等待 SSH")
    vm_start.add_argument("--timeout", type=_positive_int, default=300)
    vm_start.set_defaults(func=cmd_vm)
    vm_stop = vm_sub.add_parser("stop", help="关闭虚拟机")
    vm_stop.add_argument("--mode", choices=["soft", "hard"], default="soft")
    vm_stop.set_defaults(func=cmd_vm)
    vm_snap = vm_sub.add_parser("snapshot", help="创建快照")
    vm_snap.add_argument("name")
    vm_snap.set_defaults(func=cmd_vm)
    vm_delete = vm_sub.add_parser("delete-snapshot", help="删除快照")
    vm_delete.add_argument("name")
    vm_delete.set_defaults(func=cmd_vm)
    vm_revert = vm_sub.add_parser("revert", help="回滚快照、启动并等待 SSH")
    vm_revert.add_argument("name", nargs="?", help="默认使用 VM_SNAPSHOT")
    vm_revert.add_argument("--timeout", type=_positive_int, default=300)
    vm_revert.set_defaults(func=cmd_vm)
    vm_prepare = vm_sub.add_parser(
        "prepare", help="回滚基线、启动、等待 SSH 并执行健康检查")
    vm_prepare.add_argument("--timeout", type=_positive_int, default=300)
    vm_prepare.set_defaults(func=cmd_vm)
    vm_health = vm_sub.add_parser("health", help="采集 openKylin 环境指纹")
    vm_health.set_defaults(func=cmd_vm)

    p_sys = sub.add_parser("systest", help="系统级测试：重启/拨钟/多用户/断网（真机真做）")
    p_sys.add_argument("-a", "--adapter", choices=["hermes", "kylinbot"],
                       default="hermes", help="VM 内适配器")
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
