"""麟阁 MemHall Web UI 后端（FastAPI，本地 127.0.0.1，无鉴权——本机工具）。

架构仿 DeepSeek Harness Desktop 的思路：核心是本地 Web 服务 + 单页界面；
后续可套 Electron/pywebview 薄壳做独立窗口，UI 层零改动。

路由一览：
- GET  /                     单页界面（static/index.html）
- GET  /api/doctor           三路体检（结构化 JSON）
- GET  /api/case-dirs        可选用例目录
- GET  /api/agents           智能体注册表名单（体检扫描动画素材）
- GET  /api/meta             服务端版本信息（页面据此自检新旧）
- POST /api/start            开始一轮评测（后台线程跑，SSE 推进度）
- GET  /api/events           SSE 进度流（case/done/stopped/error）
- POST /api/stop             请求停止（当前用例跑完后生效）
- GET  /api/runs             历史运行列表
- GET  /api/runs/{id}/data   单次运行指标+判定
- GET  /api/runs/{id}/radar  雷达图 PNG
- GET  /api/compare          双运行对比雷达 PNG
- GET/POST /api/config       .env 图形化（密钥脱敏回显，留空=不变）
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

# 打包成 exe 时（onedir）源码树不存在：cases/runs/.env 都落在 exe 同级目录
REPO_ROOT = (Path(sys.executable).resolve().parent
             if getattr(sys, "frozen", False)
             else Path(__file__).resolve().parents[3])
ENV_PATH = REPO_ROOT / ".env"
if not os.access(REPO_ROOT, os.W_OK):  # deb 装机：系统目录不可写，配置落家目录
    ENV_PATH = Path.home() / "memhall.env"


def _runs_root() -> Path:
    """runs 落点：仓库/exe 同级；deb 装机系统目录不可写时退 ~/memhall-runs。"""
    r = REPO_ROOT / "runs"
    try:
        r.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        r = Path.home() / "memhall-runs"
        r.mkdir(parents=True, exist_ok=True)
    return r


def _case_roots() -> list[Path]:
    """用例目录候选根：源码=仓库；onedir=exe 同级；onefile=解包目录（用例打进
    exe 内）；deb 装机=/usr/share/memhall。runs/.env 始终落 exe 同级。"""
    roots = [REPO_ROOT]
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        roots.append(Path(meipass))
    deb_share = Path("/usr/share/memhall")
    if (deb_share / "cases").is_dir():
        roots.append(deb_share)
    return roots
STATIC_DIR = Path(__file__).resolve().parent / "static"

SECRET_KEYS = {"VM_PASS", "AGENT_LLM_KEY", "JUDGE_A_KEY", "JUDGE_B_KEY"}
KNOWN_KEYS = [
    "VM_HOST", "VM_USER", "VM_PASS",
    "AGENT_LLM_BASE_URL", "AGENT_LLM_KEY", "AGENT_LLM_MODEL",
    "JUDGE_A_BASE_URL", "JUDGE_A_MODEL", "JUDGE_A_KEY",
    "JUDGE_B_BASE_URL", "JUDGE_B_MODEL", "JUDGE_B_KEY",
    "JUDGE_MIN_INTERVAL", "KYLINBOT_SEND_INTERVAL",
]


class _RunAborted(Exception):
    pass


class RunSession:
    """单例运行会话：后台线程 + asyncio.Queue 供 SSE 消费。"""

    def __init__(self) -> None:
        self.q: asyncio.Queue = asyncio.Queue()
        self.active = False
        self.stop = False
        self.lock = threading.Lock()

    def reset(self) -> None:
        while not self.q.empty():
            self.q.get_nowait()
        self.stop = False


session = RunSession()


def _safe_run_id(run_id: str) -> Path:
    if not re.fullmatch(r"[\w.-]+", run_id):
        raise HTTPException(400, "非法 run_id")
    return _runs_root() / run_id


def create_app() -> FastAPI:
    app = FastAPI(title="麟阁 MemHall", docs_url=None, redoc_url=None)

    # ---------- 页面 ----------
    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/static/{name:path}")
    def static_file(name: str) -> FileResponse:
        p = (STATIC_DIR / name).resolve()
        if not str(p).startswith(str(STATIC_DIR.resolve())) or not p.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(p)

    # ---------- 体检 ----------
    @app.get("/api/doctor")
    async def doctor() -> dict:
        from memhall.discovery import run_doctor
        rep = await asyncio.to_thread(run_doctor, True)
        return {
            "local": [asdict(f) for f in rep.local],
            "vm": [asdict(f) for f in rep.vm],
            "env": [asdict(c) for c in rep.env],
            "vm_error": rep.vm_error,
            "usable": rep.usable_adapters(),
        }

    # 分段端点：前端并行拉取，逐段点亮（体检总时长≈最慢一段而非三段之和）。
    # 异常也回结构化 JSON（而非裸 500），让前端能展示具体原因。
    @app.get("/api/doctor/local")
    async def doctor_local(fresh: bool = False) -> dict:
        from memhall.discovery import scan_local
        try:
            rep = await asyncio.to_thread(scan_local, 4, fresh)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"[:300]}
        return {"local": [asdict(f) for f in rep]}

    @app.get("/api/doctor/vm")
    async def doctor_vm() -> dict:
        from memhall.discovery import scan_vm
        try:
            vm, err = await asyncio.to_thread(scan_vm)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"[:300]}
        same = err == "SAME-MACHINE"
        return {"vm": [asdict(f) for f in vm],
                "vm_error": "" if same else err, "same": same}

    @app.get("/api/doctor/env")
    async def doctor_env() -> dict:
        from memhall.discovery import check_env
        try:
            env = await asyncio.to_thread(check_env)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"[:300]}
        return {"env": [asdict(c) for c in env]}

    @app.get("/api/agents")
    def agent_catalog() -> dict:
        from memhall.discovery import LOCAL_AGENTS
        return {"names": [a[0] for a in LOCAL_AGENTS]}

    @app.get("/api/meta")
    def meta() -> dict:
        import sys
        from importlib.metadata import PackageNotFoundError, version
        try:
            v = version("memhall")
        except PackageNotFoundError:
            v = "dev"
        return {"version": v, "python": sys.version.split()[0]}

    # ---------- 用例目录 ----------
    @app.get("/api/case-dirs")
    def case_dirs() -> dict:
        dirs: list[str] = []
        for root in _case_roots():
            base = root / "cases"
            if base.is_dir():
                dirs += sorted(str(d.relative_to(root)).replace("\\", "/")
                               for d in base.iterdir() if d.is_dir())
        seen: set[str] = set()
        dirs = [d for d in dirs if not (d in seen or seen.add(d))]
        return {"dirs": dirs or ["cases/full"]}

    # ---------- 运行会话 ----------
    @app.post("/api/start")
    async def start(body: dict) -> dict:
        if session.active:
            raise HTTPException(409, "已有评测在跑")
        adapter_name = body.get("adapter", "mock")
        case_dir = body.get("cases", "cases/full")
        judge_mode = body.get("judge", "scripted")
        out_root = _runs_root()

        loop = asyncio.get_running_loop()

        def emit(payload: dict) -> None:
            loop.call_soon_threadsafe(session.q.put_nowait, payload)

        def worker() -> None:
            from memhall.adapters.mock import MockAdapter
            from memhall.cli import _finish_run, load_cases
            from memhall.runner.orchestrator import run_suite
            from memhall.scoring.engine import evaluate_case
            from memhall.scoring.judge import OpenAICompatJudge
            try:
                case_path = next((r / case_dir for r in _case_roots()
                                  if (r / case_dir).is_dir()),
                                 REPO_ROOT / case_dir)
                cases = load_cases(case_path)
                if not cases:
                    emit({"type": "error", "msg": f"未找到用例: {case_dir}"})
                    return
                adapters: dict = {"mock": MockAdapter}
                if adapter_name == "hermes":
                    from memhall.adapters.hermes import HermesAdapter
                    adapters["hermes"] = HermesAdapter
                elif adapter_name == "kylinbot":
                    from memhall.adapters.kylinbot import KylinBotAdapter
                    adapters["kylinbot"] = KylinBotAdapter
                elif adapter_name == "hermes-local":
                    from memhall.adapters.hermes_local import LocalHermesAdapter
                    adapters["hermes-local"] = LocalHermesAdapter
                elif adapter_name == "opencode":
                    from memhall.adapters.opencode import OpenCodeAdapter
                    adapters["opencode"] = OpenCodeAdapter
                if adapter_name not in adapters:
                    emit({"type": "error",
                          "msg": f"未知适配器: {adapter_name}"})
                    return
                emit({"type": "start", "n_cases": len(cases),
                      "adapter": adapter_name, "cases": case_dir,
                      "judge": judge_mode})

                def on_case_done(cid: str, i: int, n: int) -> None:
                    if session.stop:
                        raise _RunAborted()
                    emit({"type": "case", "case": cid, "i": i, "n": n})

                adapter = adapters[adapter_name]()
                judges = (OpenAICompatJudge.pair_from_env()
                          if judge_mode == "dual" else None)
                run_id, stores = run_suite(adapter, cases, out_root,
                                           adapter_name,
                                           on_case_done=on_case_done,
                                           on_event=emit)
                emit({"type": "phase", "msg": "评测完成，开始判卷…"})
                verdicts = []
                for case, store in zip(cases, stores):
                    verdicts.extend(evaluate_case(case, store, run_id, judges))
                    if session.stop:
                        break
                run_dir = out_root / run_id
                manifest = json.loads(
                    (run_dir / "manifest.json").read_text(encoding="utf-8"))
                metrics = _finish_run(run_dir, run_id, manifest, verdicts,
                                      {c.case_id: c for c in cases},
                                      judge_mode=judge_mode)
                emit({"type": "done", "run_id": run_id,
                      "score": metrics["overall_score"],
                      "n_valid": metrics["n_valid"],
                      "n_total": metrics["n_probes_total"],
                      "caps": metrics["capability_scores"]})
                from memhall.notify import notify_run_done
                notify_run_done(adapter_name, metrics["overall_score"],
                                metrics["n_valid"], metrics["n_probes_total"],
                                str(run_dir), radar=str(run_dir / "radar.png"))
            except _RunAborted:
                emit({"type": "stopped",
                      "msg": "已中止（当前用例完成处停下，证据已落盘）"})
            except Exception as e:  # 后台线程兜底：报给前端而不是无声死掉
                emit({"type": "error", "msg": f"{type(e).__name__}: {e}"[:400]})
            finally:
                session.active = False

        session.reset()
        session.active = True
        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True}

    @app.post("/api/stop")
    def stop() -> dict:
        session.stop = True
        return {"ok": True, "note": "当前用例完成后停止"}

    @app.get("/api/events")
    async def events() -> StreamingResponse:
        async def gen():
            yield f"data: {json.dumps({'type': 'connected'}, ensure_ascii=False)}\n\n"
            while True:
                item = await session.q.get()
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                if item.get("type") in ("done", "stopped", "error"):
                    break
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store"})

    # ---------- 历史/报告 ----------
    @app.get("/api/runs")
    def list_runs() -> dict:
        entries = []
        root = _runs_root()
        if root.is_dir():
            for d in root.iterdir():
                if not (d / "manifest.json").is_file() or not d.name[0].isdigit():
                    continue
                entry = {"run_id": d.name,
                         "adapter": json.loads(
                             (d / "manifest.json").read_text(encoding="utf-8")
                         ).get("adapter", "?")}
                mfile = d / "metrics.json"
                if mfile.is_file():
                    m = json.loads(mfile.read_text(encoding="utf-8"))
                    entry["score"] = m.get("overall_score")
                    entry["n_valid"] = m.get("n_valid")
                    entry["n_total"] = m.get("n_probes_total")
                entries.append(entry)
        entries.sort(key=lambda e: e["run_id"], reverse=True)
        return {"runs": entries}

    @app.get("/api/runs/{run_id}/data")
    def run_data(run_id: str) -> dict:
        d = _safe_run_id(run_id)
        mfile = d / "metrics.json"
        if not mfile.is_file():
            raise HTTPException(404, "无指标（运行未完成或已中止）")
        metrics = json.loads(mfile.read_text(encoding="utf-8"))
        verdicts = []
        vfile = d / "verdicts.jsonl"
        if vfile.is_file():
            verdicts = [json.loads(line)
                        for line in vfile.read_text(encoding="utf-8").splitlines()]
        return {"metrics": metrics, "verdicts": verdicts}

    @app.get("/api/runs/{run_id}/radar")
    def run_radar(run_id: str) -> FileResponse:
        p = _safe_run_id(run_id) / "radar.png"
        if not p.is_file():
            raise HTTPException(404, "无雷达图")
        return FileResponse(p, media_type="image/png")

    @app.get("/api/runs/{run_id}/case/{case_id}/evidence")
    def case_evidence(run_id: str, case_id: str) -> dict:
        if not re.fullmatch(r"[\w.-]+", case_id):
            raise HTTPException(400, "非法 case_id")
        ev_file = _safe_run_id(run_id) / "cases" / case_id / "evidence.jsonl"
        if not ev_file.is_file():
            raise HTTPException(404, "无证据文件")
        phases: dict[str, dict] = {}
        memories: list[dict] = []
        fs_created: list[str] = []
        actions: list[dict] = []
        for line in ev_file.read_text(encoding="utf-8").splitlines():
            ev = json.loads(line)
            pay = ev.get("payload") or {}
            ph = ev.get("phase", "")
            if ev.get("type") == "dialogue":
                p = phases.setdefault(ph, {"name": ph, "clock_days": 0, "turns": []})
                msgs, reps = pay.get("messages", []), pay.get("replies", [])
                for i, msg in enumerate(msgs):
                    r = reps[i] if i < len(reps) else {}
                    p["turns"].append({
                        "user": msg,
                        "reply": r.get("text", ""),
                        "latency_ms": r.get("latency_ms"),
                        "session": r.get("session_id", ""),
                    })
            elif ev.get("type") == "memory_snapshot":
                memories.append({"phase": ph,
                                 "entries": [e.get("content", "")
                                             for e in pay.get("entries", [])]})
            elif ev.get("type") == "fs_diff":
                fs_created = [e["path"] for e in pay.get("entries", [])
                              if e.get("change") == "created"]
            elif ev.get("type") == "actions":
                actions = [{"tool": a.get("tool", ""),
                            "result": a.get("result", "")}
                           for a in pay.get("actions", [])]
            if ev.get("clock_offset_days"):
                p = phases.setdefault(ph, {"name": ph, "clock_days": 0, "turns": []})
                p["clock_days"] = ev["clock_offset_days"]
        order = {n: i for i, n in enumerate(["inject", "confound", "probe"])}
        return {"case_id": case_id,
                "phases": sorted(phases.values(),
                                 key=lambda p: order.get(p["name"], 9)),
                "memories": memories, "fs_created": fs_created,
                "actions": actions}

    @app.get("/api/compare")
    def compare(runs: str) -> FileResponse:
        ids = [r for r in runs.split(",") if r.strip()][:2]
        if len(ids) != 2:
            raise HTTPException(400, "需要恰好两个 run_id")
        from memhall.report.radar import render_radar
        scores = {}
        names = []
        for rid in ids:
            d = _safe_run_id(rid)
            mfile = d / "metrics.json"
            if not mfile.is_file():
                raise HTTPException(404, f"{rid} 无指标")
            m = json.loads(mfile.read_text(encoding="utf-8"))
            label = f"{m.get('adapter', rid)} ({m['overall_score']:.1%})"
            scores[label] = m["capability_scores"]
            names.append(label)
        out_dir = _runs_root() / "_compare"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{ids[0]}__{ids[1]}.png"
        render_radar(scores, str(out))
        return FileResponse(out, media_type="image/png")

    # ---------- 配置 ----------
    @app.get("/api/config")
    def get_config() -> dict:
        _load_env_file()
        items = []
        for k in KNOWN_KEYS:
            v = os.environ.get(k, "")
            if k in SECRET_KEYS:
                items.append({"key": k, "set": bool(v), "value": ""})
            else:
                items.append({"key": k, "set": bool(v), "value": v})
        return {"items": items, "env_path": str(ENV_PATH)}

    @app.post("/api/config")
    def save_config(body: dict) -> dict:
        updates = {k: (v or "").strip() for k, v in (body.get("updates") or {}).items()
                   if k in KNOWN_KEYS and (v or "").strip()}
        if not updates:
            return {"ok": True, "saved": 0}
        lines = (ENV_PATH.read_text(encoding="utf-8").splitlines()
                 if ENV_PATH.exists() else [])
        seen = set()
        out = []
        for line in lines:
            m = re.match(r"^([A-Z_0-9]+)\s*=", line)
            if m and m.group(1) in updates:
                out.append(f"{m.group(1)}={updates[m.group(1)]}")
                seen.add(m.group(1))
            else:
                out.append(line)
        for k, v in updates.items():
            if k not in seen:
                out.append(f"{k}={v}")
        ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
        for k, v in updates.items():
            os.environ[k] = v
        return {"ok": True, "saved": len(updates)}

    return app


def _load_env_file() -> None:
    """把 .env 装进 os.environ（已存在的环境变量优先，不覆盖）。"""
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.split(" #")[0].strip()
        if k and k not in os.environ:
            os.environ[k] = v
