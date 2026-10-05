"""Three-phase scenario runner and durable evidence recorder.

The runner owns orchestration and evidence persistence. Scoring remains isolated in
memhall.scoring so a completed evidence bundle can always be scored again.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import locale
import os
import platform
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from memhall.adapters.base import AgentAdapter, NO_WINDOW
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


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _sha256(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def _phase_enum(name: str) -> EvidencePhase:
    return EvidencePhase(name)


def _error_text(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def _snapshot_digest(snapshot: Mapping[str, str] | None) -> str:
    if snapshot is None:
        return "unavailable"
    return f"sha256:{_sha256(dict(sorted(snapshot.items())))};n={len(snapshot)}"


def _fs_diff(before: Mapping[str, str] | None,
             after: Mapping[str, str] | None) -> FsDiff:
    if before is None or after is None:
        return FsDiff(before_snapshot=_snapshot_digest(before),
                      after_snapshot=_snapshot_digest(after), entries=[])
    old_paths = set(before)
    new_paths = set(after)
    entries = [FsDiffEntry(path=path, change="created")
               for path in sorted(new_paths - old_paths)]
    entries.extend(FsDiffEntry(path=path, change="modified")
                   for path in sorted(old_paths & new_paths)
                   if before[path] != after[path])
    entries.extend(FsDiffEntry(path=path, change="deleted")
                   for path in sorted(old_paths - new_paths))
    return FsDiff(before_snapshot=_snapshot_digest(before),
                  after_snapshot=_snapshot_digest(after), entries=entries)


class CaseRunner:
    """Run one scenario and append each evidence record before continuing."""

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
        self.runtime_error: str | None = None
        self.cleanup_error: str | None = None
        self.capture_errors: list[str] = []
        self.phase_results: list[dict[str, Any]] = []
        self.agent_tokens = _new_token_counter()
        self.started_at = _utc()

    @property
    def evidence_path(self) -> Path:
        return self.evidence_dir / "evidence.jsonl"

    def _collect(self, phase: str, etype: EvidenceType,
                 payload: dict[str, Any]) -> Evidence:
        self._seq += 1
        ev = Evidence(
            evidence_id=f"ev-{self.case.case_id}-{self._seq:04d}",
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
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        with self.evidence_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(ev.model_dump_json() + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        _safe_emit(self.on_event, {
            "type": "evidence", "case": self.case.case_id,
            "phase": phase, "evidence_type": etype.value,
            "evidence_id": ev.evidence_id,
        })
        return ev

    def _mark_runtime_error(self, where: str, error: BaseException | str) -> str:
        detail = error if isinstance(error, str) else _error_text(error)
        message = f"{where}: {detail}"
        if self.runtime_error is None:
            self.runtime_error = message
        _safe_emit(self.on_event, {
            "type": "err", "case": self.case.case_id, "msg": message,
        })
        return message

    def _safe_fs_snapshot(self, phase: str, point: str) -> dict[str, str] | None:
        try:
            return self.adapter.fs_snapshot_hashes()
        except Exception as error:
            self.capture_errors.append(
                f"{phase}.{point}.fs_snapshot: {_error_text(error)}")
            return None

    def _capture_memory(self, phase: str, point: str) -> None:
        try:
            payload = self.adapter.dump_memory().model_dump(mode="json")
        except Exception as error:
            message = f"{phase}.{point}.memory: {_error_text(error)}"
            self.capture_errors.append(message)
            payload = MemorySnapshot(
                format="none", dumped_at=_utc(), entries=[], raw=None,
            ).model_dump(mode="json")
            payload["capture_error"] = message
        payload["capture_point"] = point
        self._collect(phase, EvidenceType.MEMORY_SNAPSHOT, payload)
        _safe_emit(self.on_event, {
            "type": "memory", "case": self.case.case_id,
            "phase": phase, "n": len(payload.get("entries", [])),
            "capture_point": point,
        })

    def _capture_actions(self, phase: str) -> None:
        try:
            payload = self.adapter.dump_actions().model_dump(mode="json")
        except Exception as error:
            message = f"{phase}.actions: {_error_text(error)}"
            self.capture_errors.append(message)
            payload = ActionDump(actions=[], coverage="unknown").model_dump(mode="json")
            payload["capture_error"] = message
        self._collect(phase, EvidenceType.ACTIONS, payload)

    def _capture_phase_tail(self, phase: str,
                            before_fs: Mapping[str, str] | None) -> None:
        self._capture_memory(phase, "phase_end")
        self._capture_actions(phase)
        after_fs = self._safe_fs_snapshot(phase, "end")
        payload = _fs_diff(before_fs, after_fs).model_dump(mode="json")
        if before_fs is None or after_fs is None:
            payload["capture_error"] = "file snapshot unavailable"
        self._collect(phase, EvidenceType.FS_DIFF, payload)

    def _capture_skipped_phase(self, phase: str, reason: str) -> None:
        now = _utc()
        reply = Reply(
            session_id="none", text=f"[RUNTIME_ERROR] {reason}",
            sent_at=now, reply_at=now, latency_ms=0,
        )
        self._collect(phase, EvidenceType.DIALOGUE, {
            "messages": [], "replies": [reply.model_dump(mode="json")],
            "runtime_error": reason, "skipped": True,
        })
        memory = MemorySnapshot(
            format="none", dumped_at=now, entries=[], raw=None,
        ).model_dump(mode="json")
        memory.update({"capture_point": "phase_end", "skipped": True})
        self._collect(phase, EvidenceType.MEMORY_SNAPSHOT, memory)
        actions = ActionDump(actions=[], coverage="unknown").model_dump(mode="json")
        actions["skipped"] = True
        self._collect(phase, EvidenceType.ACTIONS, actions)
        fs_payload = FsDiff(
            before_snapshot="skipped", after_snapshot="skipped", entries=[],
        ).model_dump(mode="json")
        fs_payload["skipped"] = True
        self._collect(phase, EvidenceType.FS_DIFF, fs_payload)
        self.phase_results.append({"phase": phase, "status": "skipped",
                                   "error": reason})

    def _apply_system_events(self, phase_name: str, events) -> None:
        if events is None:
            return
        if events.rollback:
            self.adapter.rollback()
        if events.reboot:
            self.adapter.reboot()
        if events.clock_shift_days:
            self.adapter.clock_shift(events.clock_shift_days)
            self.clock_offset += events.clock_shift_days
        if events.network_off:
            self.adapter.network_off()
        _safe_emit(self.on_event, {
            "type": "system_event", "case": self.case.case_id,
            "phase": phase_name,
            "reboot": events.reboot,
            "clock_shift_days": events.clock_shift_days,
            "network_off": events.network_off,
            "rollback": events.rollback,
        })

    def run(self) -> EvidenceStore:
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        _atomic_json(self.evidence_dir / "case.json", self._status("running"))
        session_id = "s-01"
        next_phase = 0
        try:
            try:
                self.adapter.reset()
            except Exception as error:
                self._mark_runtime_error("reset", error)

            for phase_index, phase in enumerate(self.case.phases):
                next_phase = phase_index + 1
                if self.runtime_error:
                    self._capture_skipped_phase(phase.name, self.runtime_error)
                    continue

                phase_started = time.monotonic()
                before_fs = self._safe_fs_snapshot(phase.name, "start")
                messages: list[str] = []
                replies: list[Reply] = []
                try:
                    self._apply_system_events(phase.name, phase.system_events)
                except Exception as error:
                    self._mark_runtime_error(f"{phase.name}.system_events", error)

                if not self.runtime_error:
                    for step_index, step in enumerate(phase.steps, start=1):
                        text = step.user if step.user is not None else step.task
                        assert text is not None
                        messages.append(text)
                        _safe_emit(self.on_event, {
                            "type": "ask", "case": self.case.case_id,
                            "phase": phase.name, "q": text,
                        })
                        try:
                            reply = self.adapter.send(session_id, text)
                        except Exception as error:
                            detail = self._mark_runtime_error(
                                f"{phase.name}.step_{step_index}.send", error)
                            now = _utc()
                            reply = Reply(
                                session_id=session_id,
                                text=f"[RUNTIME_ERROR] {detail}",
                                sent_at=now, reply_at=now, latency_ms=0,
                            )
                        replies.append(reply)
                        _record_tokens(self.agent_tokens, reply.token_usage)
                        _safe_emit(self.on_event, {
                            "type": "reply", "case": self.case.case_id,
                            "phase": phase.name, "a": reply.text,
                            "ms": reply.latency_ms,
                        })
                        if phase.name == "inject" and not self.runtime_error:
                            self._capture_memory(phase.name, f"after_step_{step_index}")
                        if self.runtime_error:
                            break

                dialogue: dict[str, Any] = {
                    "messages": messages,
                    "replies": [reply.model_dump(mode="json") for reply in replies],
                }
                if self.runtime_error:
                    dialogue["runtime_error"] = self.runtime_error
                self._collect(phase.name, EvidenceType.DIALOGUE, dialogue)
                self._capture_phase_tail(phase.name, before_fs)

                if phase.end_session and not self.runtime_error:
                    try:
                        self.adapter.end_session(session_id)
                        number = int(session_id.split("-")[1]) + 1
                        session_id = f"s-{number:02d}"
                    except Exception as error:
                        self._mark_runtime_error(f"{phase.name}.end_session", error)

                if phase.wait_minutes and not self.runtime_error:
                    time.sleep(phase.wait_minutes * 60)

                self.phase_results.append({
                    "phase": phase.name,
                    "status": "invalid" if self.runtime_error else "completed",
                    "duration_ms": int((time.monotonic() - phase_started) * 1000),
                    "evidence_count": self._seq,
                })
        except BaseException as error:
            self._mark_runtime_error("runner", error)
            for phase in self.case.phases[next_phase:]:
                self._capture_skipped_phase(phase.name, self.runtime_error)
            raise
        finally:
            try:
                self.adapter.network_restore()
            except Exception as error:
                self.cleanup_error = f"network_restore: {_error_text(error)}"
            try:
                self.adapter.clock_restore()
            except Exception as error:
                detail = f"clock_restore: {_error_text(error)}"
                self.cleanup_error = (f"{self.cleanup_error}; {detail}"
                                      if self.cleanup_error else detail)
            if self.cleanup_error:
                self._mark_runtime_error("cleanup", self.cleanup_error)
            status = "invalid" if self.runtime_error else "completed"
            _atomic_json(self.evidence_dir / "case.json", self._status(status))
        return self.store

    def _status(self, status: str) -> dict[str, Any]:
        finished = _utc() if status != "running" else None
        return {
            "case_id": self.case.case_id,
            "status": status,
            "started_at": self.started_at.isoformat(),
            "finished_at": finished.isoformat() if finished else None,
            "runtime_error": self.runtime_error,
            "cleanup_error": self.cleanup_error,
            "capture_errors": self.capture_errors,
            "phases": self.phase_results,
            "evidence_count": self._seq,
            "agent_tokens": _token_summary(self.agent_tokens),
            "evidence_sha256": (_file_sha256(self.evidence_path)
                                if self.evidence_path.exists() else None),
        }


def _git_hash() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True,
            creationflags=NO_WINDOW,
        ).stdout.strip()
    except Exception:
        return "unknown"


def _source_hash() -> str:
    """Fingerprint installed source even when a Git checkout is unavailable."""
    package_root = Path(__file__).resolve().parents[1]
    files: dict[str, str] = {}
    for path in sorted(package_root.rglob("*")):
        relative = path.relative_to(package_root)
        if (not path.is_file() or "__pycache__" in relative.parts
                or path.suffix in {".pyc", ".pyo"}):
            continue
        files[relative.as_posix()] = _file_sha256(path)
    return _sha256(files)


def _package_version() -> str:
    try:
        return importlib.metadata.version("memhall")
    except importlib.metadata.PackageNotFoundError:
        try:
            from memhall import __version__
            return __version__
        except ImportError:
            return "unknown"


def _dependency_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for name in ("pydantic", "PyYAML", "matplotlib", "paramiko", "fastapi"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
    return versions


def _environment(adapter: AgentAdapter) -> dict[str, Any]:
    host = {
        "os": platform.platform(),
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "machine": platform.machine(),
        "locale": locale.getlocale(),
        "timezone": time.tzname,
    }
    try:
        target = adapter.environment_info()
    except Exception as error:
        target = {"error": _error_text(error)}
    return {"runner_host": host, "target": target,
            "dependencies": _dependency_versions()}


def _case_fingerprints(cases: list[MemoryCase]) -> tuple[dict[str, str], str]:
    hashes = {
        case.case_id: _sha256(case.model_dump(mode="json", by_alias=True))
        for case in cases
    }
    return hashes, _sha256(hashes)


def _bundle_hash(case_results: list[dict[str, Any]]) -> str:
    files = {
        result["case_id"]: result.get("evidence_sha256")
        for result in case_results
    }
    return _sha256(files)


def _new_token_counter() -> dict[str, int]:
    return {
        "requests": 0,
        "reported_replies": 0,
        "complete_replies": 0,
        "prompt": 0,
        "completion": 0,
    }


def _record_tokens(counter: dict[str, int], usage: Any) -> None:
    counter["requests"] += 1
    if usage is None:
        return
    prompt = getattr(usage, "prompt", None)
    completion = getattr(usage, "completion", None)
    if prompt is None and completion is None:
        return
    counter["reported_replies"] += 1
    if prompt is not None and completion is not None:
        counter["complete_replies"] += 1
    counter["prompt"] += int(prompt or 0)
    counter["completion"] += int(completion or 0)


def _merge_token_summary(counter: dict[str, int], summary: Mapping[str, Any]) -> None:
    for field in counter:
        counter[field] += int(summary.get(field, 0) or 0)


def _token_summary(counter: Mapping[str, int]) -> dict[str, Any]:
    requests = int(counter.get("requests", 0))
    reported = int(counter.get("reported_replies", 0))
    prompt = int(counter.get("prompt", 0))
    completion = int(counter.get("completion", 0))
    return {
        "requests": requests,
        "reported_replies": reported,
        "unreported_replies": requests - reported,
        "complete_replies": int(counter.get("complete_replies", 0)),
        "coverage": round(reported / requests, 4) if requests else None,
        "prompt": prompt,
        "completion": completion,
        "total": prompt + completion,
    }


def _aggregate_agent_tokens(case_results: list[dict[str, Any]]) -> dict[str, Any]:
    counter = _new_token_counter()
    for result in case_results:
        _merge_token_summary(counter, result.get("agent_tokens", {}))
    return _token_summary(counter)


def _safe_emit(on_event, payload: dict[str, Any]) -> None:
    """UI progress must never affect the measurement."""
    if on_event is None:
        return
    try:
        on_event(payload)
    except Exception:
        pass


def run_suite(adapter: AgentAdapter, cases: list[MemoryCase], out_dir: Path,
              adapter_name: str, on_case_done=None, on_event=None, *,
              case_sample_seed: int = 42, repeat_of: str | None = None,
              repeat_group: str | None = None, repeat_index: int = 1,
              repeat_count: int = 1) -> tuple[str, list[EvidenceStore]]:
    """Run a deterministic case list and maintain a crash-readable manifest."""
    started_at = _utc()
    run_id = started_at.strftime("%Y%m%d-%H%M%S-%f") + f"-{adapter_name}"
    run_dir = out_dir / run_id
    case_hashes, cases_version = _case_fingerprints(cases)
    git_hash = _git_hash()
    source_hash = _source_hash()
    manifest: dict[str, Any] = {
        "run_id": run_id,
        "tool": "memhall",
        "tool_version": _package_version(),
        "schema_version": "0.2",
        "status": "running",
        "adapter": adapter_name,
        "agent": adapter_name,
        "git_hash": git_hash,
        "source_sha256": source_hash,
        "code_version": f"sha256:{source_hash}",
        "started_at": started_at.isoformat(),
        "finished_at": None,
        "cases": [case.case_id for case in cases],
        "case_hashes": case_hashes,
        "cases_version": f"sha256:{cases_version}",
        "case_capabilities": {
            case.case_id: case.capability.value for case in cases
        },
        "case_phases": {
            case.case_id: [phase.name for phase in case.phases] for case in cases
        },
        "case_sample_seed": case_sample_seed,
        "n_probes_total": sum(len(case.probes) for case in cases),
        "repeat_of": repeat_of,
        "repeat_group": repeat_group,
        "repeat_index": repeat_index,
        "repeat_count": repeat_count,
        "env": _environment(adapter),
        "cost": {
            "agent_tokens": _token_summary(_new_token_counter()),
            "judge_tokens": {"mode": "not_run", "requests": 0},
        },
        "case_results": [],
        "evidence_bundle_sha256": None,
    }
    cases_snapshot = {
        "schema_version": "0.1",
        "cases": [case.model_dump(mode="json", by_alias=True) for case in cases],
    }
    _atomic_json(run_dir / "cases.json", cases_snapshot)
    manifest["cases_snapshot_sha256"] = _file_sha256(run_dir / "cases.json")
    _atomic_json(run_dir / "manifest.json", manifest)

    stores: list[EvidenceStore] = []
    caught: BaseException | None = None
    try:
        for index, case in enumerate(cases, start=1):
            _safe_emit(on_event, {
                "type": "case_start", "case": case.case_id,
                "i": index, "n": len(cases),
            })
            runner = CaseRunner(
                adapter, case, run_id, run_dir / "cases" / case.case_id,
                on_event=on_event,
            )
            try:
                store = runner.run()
            except BaseException:
                status_path = runner.evidence_dir / "case.json"
                if status_path.is_file():
                    result = json.loads(status_path.read_text(encoding="utf-8"))
                    manifest["case_results"].append(result)
                    manifest["cost"]["agent_tokens"] = _aggregate_agent_tokens(
                        manifest["case_results"])
                    manifest["evidence_bundle_sha256"] = _bundle_hash(
                        manifest["case_results"])
                    _atomic_json(run_dir / "manifest.json", manifest)
                raise
            stores.append(store)
            result = json.loads(
                (runner.evidence_dir / "case.json").read_text(encoding="utf-8")
            )
            manifest["case_results"].append(result)
            manifest["cost"]["agent_tokens"] = _aggregate_agent_tokens(
                manifest["case_results"])
            manifest["evidence_bundle_sha256"] = _bundle_hash(
                manifest["case_results"])
            _atomic_json(run_dir / "manifest.json", manifest)
            if on_case_done is not None:
                try:
                    on_case_done(case.case_id, index, len(cases))
                except Exception:
                    pass
    except BaseException as error:
        caught = error
        manifest["status"] = "aborted"
        manifest["error"] = _error_text(error)
        raise
    finally:
        try:
            adapter.close()
        except Exception as error:
            manifest["adapter_close_error"] = _error_text(error)
            manifest["status"] = "aborted"
        manifest["finished_at"] = _utc().isoformat()
        if caught is None and "adapter_close_error" not in manifest:
            manifest["status"] = "completed"
        manifest["n_cases_completed"] = len(manifest["case_results"])
        manifest["n_cases_invalid"] = sum(
            result.get("status") != "completed"
            for result in manifest["case_results"]
        )
        manifest["evidence_bundle_sha256"] = _bundle_hash(
            manifest["case_results"])
        manifest["cost"]["agent_tokens"] = _aggregate_agent_tokens(
            manifest["case_results"])
        _atomic_json(run_dir / "manifest.json", manifest)
    return run_id, stores
