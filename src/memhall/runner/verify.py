"""Offline integrity verification for a MemHall run directory."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from memhall.runner.orchestrator import _file_sha256, _sha256
from memhall.schema.evidence import (
    ActionDump,
    Evidence,
    EvidencePhase,
    EvidenceType,
    FsDiff,
    MemorySnapshot,
    Reply,
    Verdict,
)


@dataclass
class VerificationResult:
    run_id: str = ""
    ok: bool = False
    n_cases: int = 0
    n_evidence: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    computed_bundle_sha256: str = ""

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


def _read_json(path: Path, result: VerificationResult) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        result.errors.append(f"缺少文件: {path.name}")
        return None
    except (OSError, json.JSONDecodeError) as error:
        result.errors.append(f"无法读取 {path.name}: {type(error).__name__}: {error}")
        return None
    if not isinstance(value, dict):
        result.errors.append(f"{path.name} 顶层必须是对象")
        return None
    return value


def _validate_payload(evidence: Evidence) -> None:
    """Validate the type-specific payload while allowing recorder metadata."""
    if evidence.type == EvidenceType.DIALOGUE:
        messages = evidence.payload.get("messages")
        replies = evidence.payload.get("replies")
        if not isinstance(messages, list) or not all(
                isinstance(item, str) for item in messages):
            raise ValueError("dialogue.messages 必须是字符串数组")
        if not isinstance(replies, list):
            raise ValueError("dialogue.replies 必须是数组")
        for reply in replies:
            Reply.model_validate(reply)
        if not evidence.payload.get("skipped") and len(messages) != len(replies):
            raise ValueError("dialogue.messages/replies 数量不一致")
    elif evidence.type == EvidenceType.MEMORY_SNAPSHOT:
        MemorySnapshot.model_validate(evidence.payload)
    elif evidence.type == EvidenceType.ACTIONS:
        ActionDump.model_validate(evidence.payload)
    elif evidence.type == EvidenceType.FS_DIFF:
        FsDiff.model_validate(evidence.payload)


def verify_run(run_dir: Path) -> VerificationResult:
    """Validate schema, payload hashes, phase coverage, and bundle seals."""
    run_dir = run_dir.resolve()
    result = VerificationResult()
    manifest = _read_json(run_dir / "manifest.json", result)
    if manifest is None:
        return result

    result.run_id = str(manifest.get("run_id", ""))
    if run_dir.name != result.run_id:
        result.warnings.append(
            f"目录名 {run_dir.name} 与 run_id {result.run_id} 不一致")
    if manifest.get("status") != "completed":
        result.errors.append(f"运行状态不是 completed: {manifest.get('status')}")

    expected_phases: dict[str, set[EvidencePhase]] = {}
    cases_snapshot = _read_json(run_dir / "cases.json", result)
    if cases_snapshot is not None:
        expected = manifest.get("cases_snapshot_sha256")
        actual = _file_sha256(run_dir / "cases.json")
        if expected and expected != actual:
            result.errors.append("cases.json 哈希与 manifest 不一致")
        snapshot_cases = cases_snapshot.get("cases", [])
        if not isinstance(snapshot_cases, list):
            result.errors.append("cases.json.cases 必须是数组")
            snapshot_cases = []
        snapshot_ids = []
        snapshot_hashes: dict[str, str] = {}
        for item in snapshot_cases:
            if not isinstance(item, dict) or not item.get("case_id"):
                result.errors.append("cases.json 含无效用例对象")
                continue
            case_id = str(item["case_id"])
            snapshot_ids.append(case_id)
            snapshot_hashes[case_id] = _sha256(item)
            phases: set[EvidencePhase] = set()
            for phase in item.get("phases", []):
                try:
                    phases.add(EvidencePhase(phase["name"]))
                except (KeyError, TypeError, ValueError):
                    result.errors.append(f"{case_id}: cases.json 含无效阶段")
            expected_phases[case_id] = phases
        if snapshot_ids != manifest.get("cases", []):
            result.errors.append("cases.json 的用例顺序与 manifest 不一致")
        if snapshot_hashes != manifest.get("case_hashes"):
            result.errors.append("cases.json 的逐题哈希与 manifest 不一致")
        if f"sha256:{_sha256(snapshot_hashes)}" != manifest.get("cases_version"):
            result.errors.append("cases.json 的题库指纹与 manifest 不一致")

    seen_ids: dict[str, str] = {}
    case_file_hashes: dict[str, str | None] = {}
    raw_case_results = [
        item for item in manifest.get("case_results", [])
        if isinstance(item, dict) and item.get("case_id")
    ]
    case_results = {
        item.get("case_id"): item
        for item in raw_case_results
    }
    if len(case_results) != len(raw_case_results):
        result.errors.append("manifest.case_results 含重复 case_id")
    expected_cases = manifest.get("cases", [])
    if not isinstance(expected_cases, list):
        result.errors.append("manifest.cases 必须是数组")
        expected_cases = []

    for case_id in expected_cases:
        case_dir = run_dir / "cases" / str(case_id)
        evidence_path = case_dir / "evidence.jsonl"
        case_status = _read_json(case_dir / "case.json", result)
        recorded = case_results.get(str(case_id))
        if recorded is None:
            result.errors.append(f"{case_id}: manifest 缺少 case_result")
            recorded = case_status or {}
        elif case_status is not None and _sha256(recorded) != _sha256(case_status):
            result.errors.append(f"{case_id}: case.json 与 manifest.case_results 不一致")
        if not evidence_path.is_file():
            result.errors.append(f"{case_id}: 缺少 evidence.jsonl")
            case_file_hashes[str(case_id)] = None
            continue

        file_hash = _file_sha256(evidence_path)
        case_file_hashes[str(case_id)] = file_hash
        if recorded.get("evidence_sha256") != file_hash:
            result.errors.append(f"{case_id}: evidence.jsonl 文件哈希不一致")

        phase_types: dict[EvidencePhase, set[EvidenceType]] = {
            phase: set() for phase in EvidencePhase
        }
        memory_counts: dict[EvidencePhase, int] = {
            phase: 0 for phase in EvidencePhase
        }
        count = 0
        try:
            lines = evidence_path.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            result.errors.append(f"{case_id}: 读取证据失败: {error}")
            continue
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                result.warnings.append(f"{case_id}:{line_number}: 空行")
                continue
            try:
                evidence = Evidence.model_validate(json.loads(line))
                _validate_payload(evidence)
            except Exception as error:
                result.errors.append(
                    f"{case_id}:{line_number}: 证据格式错误: "
                    f"{type(error).__name__}: {error}")
                continue
            count += 1
            result.n_evidence += 1
            if evidence.evidence_id in seen_ids:
                result.errors.append(
                    f"{case_id}:{line_number}: 重复 evidence_id "
                    f"{evidence.evidence_id}")
            seen_ids[evidence.evidence_id] = str(case_id)
            if evidence.run_id != result.run_id:
                result.errors.append(
                    f"{case_id}:{line_number}: run_id 不一致")
            if evidence.case_id != case_id:
                result.errors.append(
                    f"{case_id}:{line_number}: case_id 不一致")
            if _sha256(evidence.payload) != evidence.sha256:
                result.errors.append(
                    f"{case_id}:{line_number}: payload SHA-256 不一致")
            phase_types[evidence.phase].add(evidence.type)
            if evidence.type == EvidenceType.MEMORY_SNAPSHOT:
                memory_counts[evidence.phase] += 1

        required_phases = expected_phases.get(str(case_id), set(EvidencePhase))
        observed_phases = {
            phase for phase, types in phase_types.items() if types
        }
        for phase in sorted(observed_phases - required_phases, key=lambda x: x.value):
            result.errors.append(f"{case_id}:{phase.value}: 出现题目未定义的阶段证据")
        expected_types = set(EvidenceType)
        for phase in sorted(required_phases, key=lambda x: x.value):
            missing = expected_types - phase_types[phase]
            if missing:
                result.errors.append(
                    f"{case_id}:{phase.value}: 缺少证据 "
                    + ", ".join(sorted(item.value for item in missing)))
        if (EvidencePhase.INJECT in required_phases
                and case_status and case_status.get("status") == "completed"
                and memory_counts[EvidencePhase.INJECT] < 2):
            result.errors.append(f"{case_id}: inject 缺少即时记忆快照")
        if case_status and case_status.get("evidence_count") != count:
            result.errors.append(
                f"{case_id}: case.json evidence_count 与实际不一致")
        result.n_cases += 1

    result.computed_bundle_sha256 = _sha256(case_file_hashes)
    if manifest.get("evidence_bundle_sha256") != result.computed_bundle_sha256:
        result.errors.append("整包证据哈希与 manifest 不一致")
    if manifest.get("n_cases_completed") != len(case_results):
        result.errors.append("manifest.n_cases_completed 与 case_results 不一致")
    if set(case_results) != {str(item) for item in expected_cases}:
        result.errors.append("manifest.case_results 与用例清单不一致")

    verdict_path = run_dir / "verdicts.jsonl"
    seen_verdict_ids: set[str] = set()
    if verdict_path.is_file():
        for line_number, line in enumerate(
                verdict_path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                verdict = Verdict.model_validate(json.loads(line))
            except Exception as error:
                result.errors.append(
                    f"verdicts.jsonl:{line_number}: 判定格式错误: {error}")
                continue
            if verdict.run_id != result.run_id:
                result.errors.append(
                    f"verdicts.jsonl:{line_number}: run_id 不一致")
            if verdict.verdict_id in seen_verdict_ids:
                result.errors.append(
                    f"verdicts.jsonl:{line_number}: 重复 verdict_id "
                    f"{verdict.verdict_id}")
            seen_verdict_ids.add(verdict.verdict_id)
            for reference in verdict.evidence_refs:
                if isinstance(reference, str) and reference.startswith("ev-"):
                    if reference not in seen_ids:
                        result.errors.append(
                            f"verdicts.jsonl:{line_number}: "
                            f"引用不存在的证据 {reference}")
                    elif seen_ids[reference] != verdict.case_id:
                        result.errors.append(
                            f"verdicts.jsonl:{line_number}: 引用了其他用例的证据 "
                            f"{reference}")

    output_hashes = manifest.get("output_hashes")
    if output_hashes is not None:
        if not isinstance(output_hashes, dict):
            result.errors.append("manifest.output_hashes 必须是对象")
        else:
            allowed = {"verdicts.jsonl", "metrics.json", "report.md", "radar.png"}
            if set(output_hashes) != allowed:
                result.errors.append("manifest.output_hashes 文件集合不完整")
            for name in sorted(allowed):
                path = run_dir / name
                if not path.is_file():
                    result.errors.append(f"缺少评分产物: {name}")
                elif output_hashes.get(name) != _file_sha256(path):
                    result.errors.append(f"评分产物哈希不一致: {name}")

    result.ok = not result.errors
    return result
