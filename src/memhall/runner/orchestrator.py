"""三阶段剧本编排器（D 主线，正式 runner 实现）。

职责：按 MemoryCase 剧本驱动适配器、按契约 03 采集证据、落盘 JSONL + manifest。
不评判——判定全部在 scoring/engine.py。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import platform
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml

from memhall import __version__
from memhall.adapters.base import NO_WINDOW, AdapterError, AgentAdapter
from memhall.cost import summarize, usage_delta, usage_snapshot
from memhall.schema.evidence import (
    Evidence,
    EvidencePhase,
    EvidenceType,
    FsDiff,
    FsDiffEntry,
    Reply,
)
from memhall.schema.models_case import MemoryCase
from memhall.scoring.rules import EvidenceStore

log = logging.getLogger(__name__)


def _utc() -> datetime:
    return datetime.now(UTC)


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
        # 用例快照先行：run 目录自包含，report/换机重渲染不依赖源码树用例库
        # （heldout 题目文本不入仓库，快照是它唯一的持久载体）
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        (self.evidence_dir / "case.yaml").write_text(
            yaml.safe_dump(self.case.model_dump(mode="json"),
                           allow_unicode=True, sort_keys=False),
            encoding="utf-8")
        self.adapter.reset()
        # R02/R23：reset 彻底性防线——残留记忆会把上一个 case 的答案带进来
        # （同问异答题库里是定向毒药），宁可本 case 中止也不静默污染
        self.adapter.verify_reset()
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
                        log.warning("%s 拨钟不支持（case 运行无效）: %s",
                                    self.case.case_id, e)
                        self._runtime_error = str(e)
                        break
                    log.info("%s 拨钟 %+d 天（累计 %+d）", self.case.case_id,
                             se.clock_shift_days, self.clock_offset + se.clock_shift_days)
                    self.clock_offset += se.clock_shift_days
                messages: list[str] = []
                replies: list[Reply] = []
                for step in phase.steps:
                    text = step.user if step.user is not None else step.task
                    if text is None:
                        raise ValueError(f"用例 {self.case.case_id} 阶段 {phase.name} "
                                         "存在既无 user 也无 task 的步骤")
                    log.debug("%s 阶段 %s 问: %s", self.case.case_id, phase.name,
                              text[:60])
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
                        log.error("%s 阶段 %s 适配器错误: %s", self.case.case_id,
                                  phase.name, e)
                        replies.append(Reply(
                            session_id=session_id,
                            text=f"[RUNTIME_ERROR] {e}",
                            sent_at=_utc(), reply_at=_utc(), latency_ms=0))
                        self._runtime_error = str(e)
                        _safe_emit(self.on_event, {"type": "err",
                                                   "case": self.case.case_id,
                                                   "msg": str(e)})
                        break
                # 部分对话也落盘：[RUNTIME_ERROR] 回复标记进证据，判卷层据此
                # 标 INVALID_RUN——适配器中途挂掉不能静默降级成 omission
                self._collect(phase.name, EvidenceType.DIALOGUE,
                              {"messages": messages,
                               "replies": [r.model_dump(mode="json") for r in replies]})
                if getattr(self, "_runtime_error", None):
                    break
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
        log.debug("%s 证据落盘 %d 条", self.case.case_id, len(self.store.items()))
        return self.store

    def _flush(self) -> None:
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        path = self.evidence_dir / "evidence.jsonl"
        with path.open("a", encoding="utf-8") as f:
            for ev in self.store.items():
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
    with contextlib.suppress(Exception):
        on_event(payload)


def run_suite(adapter: AgentAdapter, cases: list[MemoryCase], out_dir: Path,
              adapter_name: str, case_source: str = "",
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
    log.info("评测开始: %s × %d 用例 × %d 探测点 → %s", adapter_name, len(cases),
             sum(len(c.probes) for c in cases), run_dir)
    stores: list[EvidenceStore] = []
    failed: list[str] = []
    usage_before = usage_snapshot()
    try:
        for i, case in enumerate(cases):
            log.info("[%d/%d] %s 开跑", i + 1, len(cases), case.case_id)
            t0 = time.monotonic()
            _safe_emit(on_event, {"type": "case_start", "case": case.case_id,
                                  "i": i + 1, "n": len(cases)})
            runner = CaseRunner(adapter, case, run_id, run_dir / "cases" / case.case_id,
                                on_event=on_event)
            # R23：单 case 未预期异常不再引爆整套马拉松——记录失败续跑，
            # 已完成用例的证据/manifest 照常落盘（40 题挂 1 题不报废整轮）
            try:
                stores.append(runner.run())
            except Exception:  # noqa: BLE001 隔离层必须兜住一切
                log.exception("[%d/%d] %s 异常中止（记入 failed_cases，续跑）",
                              i + 1, len(cases), case.case_id)
                failed.append(case.case_id)
                continue
            log.info("[%d/%d] %s 完成（%.1fs）", i + 1, len(cases), case.case_id,
                     time.monotonic() - t0)
            if on_case_done is not None:
                on_case_done(case.case_id, i + 1, len(cases))
    finally:
        _write_manifest(run_dir, run_id, adapter_name, case_source, cases,
                        failed, usage_before, adapter)
    log.info("评测完成: run_id=%s", run_id)
    return run_id, stores


def pair_stores(cases: list[MemoryCase],
                stores: list[EvidenceStore]) -> list[tuple[MemoryCase, EvidenceStore]]:
    """stores 与 cases 按证据内 case_id 配对（failed_cases 无 store，跳过）。"""
    by_id: dict[str, EvidenceStore] = {}
    for s in stores:
        items = s.items()
        if items:
            by_id.setdefault(items[0].case_id, s)
    return [(c, st) for c in cases if (st := by_id.get(c.case_id)) is not None]


def _write_manifest(run_dir: Path, run_id: str, adapter_name: str,
                    case_source: str, cases: list[MemoryCase], failed: list[str],
                    usage_before, adapter: AgentAdapter) -> None:
    manifest = {
        "run_id": run_id,
        "tool": "memhall",
        "tool_version": __version__,
        "python": platform.python_version(),
        "schema_version": "0.1",
        "adapter": adapter_name,
        "case_source": case_source,
        "git_hash": _git_hash(),
        "started_at": run_id[:15],
        "finished_at": _utc().isoformat(),
        "cases": [c.case_id for c in cases],
        "n_probes_total": sum(len(c.probes) for c in cases),
    }
    if failed:
        manifest["failed_cases"] = failed
    # 网关记账差值（直连模式/无记账文件时为 None，不落键）
    token_usage = summarize(usage_delta(usage_before, usage_snapshot()))
    if token_usage:
        manifest["token_usage"] = token_usage
        log.info("本轮网关记账: %d 请求 / %d tokens",
                 token_usage["requests"], token_usage["total_tokens"])
    # 被测智能体版本（报告可复现性元数据；探测失败静默跳过）
    try:
        version = adapter.version_info()
    except Exception:  # noqa: BLE001 元数据探测不阻塞评测
        version = None
    if version:
        manifest["agent_version"] = version
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
