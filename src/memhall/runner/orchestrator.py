"""三阶段剧本编排器（D 主线，正式 runner 实现）。

职责：按 MemoryCase 剧本驱动适配器、按契约 03 采集证据、落盘 JSONL + manifest。
不评判——判定全部在 scoring/engine.py。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from memhall.adapters.base import AgentAdapter
from memhall.adapters.base import AdapterError, NO_WINDOW
from memhall.schema.evidence import (
    ActionDump,
    Evidence,
    EvidencePhase,
    EvidenceType,
    FsDiff,
    FsDiffEntry,
    MemorySnapshot,
    Reply,
)
from memhall.schema.models_case import MemoryCase
from memhall.scoring.rules import EvidenceStore


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(payload: dict) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _phase_enum(name: str) -> EvidencePhase:
    return EvidencePhase(name)


class CaseRunner:
    """单用例执行器：剧本 → 适配器调用 → 证据采集落盘。"""

    def __init__(self, adapter: AgentAdapter, case: MemoryCase, run_id: str,
                 evidence_dir: Path, on_event=None):
        self.adapter = adapter
        self.case = case
        self.run_id = run_id
        self.evidence_dir = evidence_dir
        self.on_event = on_event
        self.store = EvidenceStore()
        self._seq = 0
        self.clock_offset = 0

    def _collect(self, phase: str, etype: EvidenceType, payload: dict) -> None:
        self._seq += 1
        ev = Evidence(
            evidence_id=f"ev-{self._seq:06d}",
            run_id=self.run_id,
            case_id=self.case.case_id,
            phase=_phase_enum(phase),
            type=etype,
            collected_at=_utc(),
            clock_offset_days=self.clock_offset,
            payload=payload,
            sha256=_sha256(payload),
        )
        self.store.add(ev)

    def run(self) -> EvidenceStore:
        self.adapter.reset()
        base_fs = self.adapter.fs_snapshot()
        session_id = "s-01"
        try:
            for phase in self.case.phases:
                se = phase.system_events
                if se is not None and se.clock_shift_days:
                    try:
                        self.adapter.clock_shift(se.clock_shift_days)
                    except AdapterError as e:
                        # 拨钟不支持（如 Windows 本机适配器）→ 本 case 运行无效，
                        # 不能让一个 case 的环境限制打崩整套
                        self._runtime_error = str(e)
                        break
                    self.clock_offset += se.clock_shift_days
                messages: list[str] = []
                replies: list[Reply] = []
                for step in phase.steps:
                    text = step.user if step.user is not None else step.task
                    assert text is not None
                    messages.append(text)
                    _safe_emit(self.on_event, {"type": "ask",
                                               "case": self.case.case_id,
                                               "phase": phase.name, "q": text})
                    try:
                        reply = self.adapter.send(session_id, text)
                        replies.append(reply)
                        _safe_emit(self.on_event, {"type": "reply",
                                                   "case": self.case.case_id,
                                                   "phase": phase.name,
                                                   "a": reply.text,
                                                   "ms": reply.latency_ms})
                    except AdapterError as e:
                        # 契约 01：适配器不可用 -> 后续步骤无意义，case 标运行无效
                        replies.append(Reply(
                            session_id=session_id,
                            text=f"[RUNTIME_ERROR] {e}",
                            sent_at=_utc(), reply_at=_utc(), latency_ms=0))
                        self._runtime_error = str(e)
                        _safe_emit(self.on_event, {"type": "err",
                                                   "case": self.case.case_id,
                                                   "msg": str(e)})
                        break
                if getattr(self, "_runtime_error", None):
                    break
                self._collect(phase.name, EvidenceType.DIALOGUE,
                              {"messages": messages,
                               "replies": [r.model_dump(mode="json") for r in replies]})
                # inject 后加采记忆快照（写入时机测试的数据源）
                if phase.name == "inject":
                    snap = self.adapter.dump_memory()
                    self._collect("inject", EvidenceType.MEMORY_SNAPSHOT,
                                  snap.model_dump(mode="json"))
                    _safe_emit(self.on_event, {"type": "memory",
                                               "case": self.case.case_id,
                                               "n": len(snap.entries)})
                if phase.end_session:
                    self.adapter.end_session(session_id)
                    n = int(session_id.split("-")[1]) + 1
                    session_id = f"s-{n:02d}"
            # probe 结束后全量采集
            snap = self.adapter.dump_memory()
            self._collect("probe", EvidenceType.MEMORY_SNAPSHOT, snap.model_dump(mode="json"))
            _safe_emit(self.on_event, {"type": "memory",
                                       "case": self.case.case_id,
                                       "n": len(snap.entries),
                                       "final": True})
            dump = self.adapter.dump_actions()
            self._collect("probe", EvidenceType.ACTIONS, dump.model_dump(mode="json"))
        finally:
            self.adapter.clock_restore()
        # 文件系统 diff（适配器支持时）：before 快照 vs after 快照
        after_fs = self.adapter.fs_snapshot()
        if base_fs is not None and after_fs is not None:
            created = sorted(set(after_fs) - set(base_fs))
            deleted = sorted(set(base_fs) - set(after_fs))
            fs_diff = FsDiff(entries=[FsDiffEntry(path=p, change="created")
                                      for p in created]
                             + [FsDiffEntry(path=p, change="deleted") for p in deleted],
                             before_snapshot=f"n={len(base_fs)}",
                             after_snapshot=f"n={len(after_fs)}")
            self._collect("probe", EvidenceType.FS_DIFF, fs_diff.model_dump(mode="json"))
        self._flush()
        return self.store

    def _flush(self) -> None:
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        path = self.evidence_dir / "evidence.jsonl"
        with path.open("a", encoding="utf-8") as f:
            for ev in self.store._items:
                f.write(ev.model_dump_json() + "\n")


def _git_hash() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True,
            creationflags=NO_WINDOW,
        ).stdout.strip()
    except Exception:
        return "unknown"


def _safe_emit(on_event, payload: dict) -> None:
    """事件流给 UI 看过程用——它坏掉不能打崩评测。"""
    if on_event is None:
        return
    try:
        on_event(payload)
    except Exception:
        pass


def run_suite(adapter: AgentAdapter, cases: list[MemoryCase], out_dir: Path,
              adapter_name: str,
              on_case_done=None, on_event=None) -> tuple[str, list[EvidenceStore]]:
    """跑整套用例，落盘 manifest，返回 (run_id, 每 case 的证据视图)。

    on_case_done(case_id, i, n)：每用例跑完后回调（UI 进度流用）；
        回调抛异常即中止（配合 UI 的停止按钮，已完成的用例证据已落盘）。
    on_event(ev)：逐条过程事件（ask/reply/memory/err/case_start），
        供 UI 直播问答过程；回调异常被吞，不影响评测。
    """
    run_id = _utc().strftime("%Y%m%d-%H%M%S") + f"-{adapter_name}"
    # 快机同秒跑两轮（mock 单轮 2 秒级）会互相覆盖，冲突时加序号后缀
    base_dir = out_dir / run_id
    run_dir, k = base_dir, 2
    while run_dir.exists():
        run_dir = out_dir / f"{run_id}-{k}"
        k += 1
    run_id = run_dir.name
    stores: list[EvidenceStore] = []
    for i, case in enumerate(cases):
        _safe_emit(on_event, {"type": "case_start", "case": case.case_id,
                              "i": i + 1, "n": len(cases)})
        runner = CaseRunner(adapter, case, run_id, run_dir / "cases" / case.case_id,
                            on_event=on_event)
        stores.append(runner.run())
        if on_case_done is not None:
            on_case_done(case.case_id, i + 1, len(cases))
    manifest = {
        "run_id": run_id,
        "tool": "memhall",
        "schema_version": "0.1",
        "adapter": adapter_name,
        "git_hash": _git_hash(),
        "started_at": run_id[:15],
        "finished_at": _utc().isoformat(),
        "cases": [c.case_id for c in cases],
        "n_probes_total": sum(len(c.probes) for c in cases),
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return run_id, stores
