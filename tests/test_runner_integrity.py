"""Durability, integrity, and repeatability checks for the production runner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memhall.adapters.base import AgentUnavailable
from memhall.adapters.mock import MockAdapter
from memhall.cli import _finish_run, load_cases
from memhall.report.stability import analyze_stability
from memhall.runner.orchestrator import _fs_diff, run_suite
from memhall.runner.verify import verify_run
from memhall.schema.evidence import (
    Evidence,
    EvidencePhase,
    EvidenceType,
    TokenUsage,
    VerdictValue,
)
from memhall.scoring.engine import evaluate_case
from memhall.scoring.judge import OpenAICompatJudge

REPO = Path(__file__).parent.parent


def _finish_mock_run(root: Path, cases):
    run_id, stores = run_suite(MockAdapter(), cases, root, "mock")
    run_dir = root / run_id
    verdicts = [
        verdict
        for case, store in zip(cases, stores)
        for verdict in evaluate_case(case, store, run_id)
    ]
    manifest = json.loads(
        (run_dir / "manifest.json").read_text(encoding="utf-8"))
    _finish_run(
        run_dir, run_id, manifest, verdicts,
        {case.case_id: case for case in cases},
    )
    return run_dir, verdicts


def test_every_phase_has_four_durable_evidence_types(tmp_path: Path):
    case = load_cases(REPO / "cases" / "quick")[:1]
    run_dir, _ = _finish_mock_run(tmp_path, case)
    evidence = [
        Evidence.model_validate(json.loads(line))
        for line in next((run_dir / "cases").iterdir()).joinpath(
            "evidence.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len({item.evidence_id for item in evidence}) == len(evidence)
    for phase in EvidencePhase:
        assert {item.type for item in evidence if item.phase == phase} == set(EvidenceType)
    assert sum(item.type == EvidenceType.MEMORY_SNAPSHOT
               and item.phase == EvidencePhase.INJECT for item in evidence) >= 2
    assert verify_run(run_dir).ok


def test_verifier_accepts_case_without_optional_confound_phase(tmp_path: Path):
    case = next(item for item in load_cases(REPO / "cases" / "full")
                if item.case_id == "recall-001")
    assert [phase.name for phase in case.phases] == ["inject", "probe"]
    run_dir, _ = _finish_mock_run(tmp_path, [case])
    result = verify_run(run_dir)
    assert result.ok, result.errors


def test_filesystem_diff_detects_same_path_modification():
    before = {"~/work/report.md": "file:old", "~/work/old.txt": "file:x"}
    after = {"~/work/report.md": "file:new", "~/work/new.txt": "file:y"}
    changes = {(item.path, item.change) for item in _fs_diff(before, after).entries}
    assert changes == {
        ("~/work/report.md", "modified"),
        ("~/work/old.txt", "deleted"),
        ("~/work/new.txt", "created"),
    }


def test_verifier_detects_payload_tampering(tmp_path: Path):
    cases = load_cases(REPO / "cases" / "quick")[:1]
    run_dir, _ = _finish_mock_run(tmp_path, cases)
    evidence_path = next((run_dir / "cases").iterdir()) / "evidence.jsonl"
    lines = evidence_path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["payload"]["tampered"] = True
    lines[0] = json.dumps(record, ensure_ascii=False)
    evidence_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = verify_run(run_dir)
    assert not result.ok
    assert any("payload SHA-256" in error for error in result.errors)
    assert any("文件哈希" in error for error in result.errors)


def test_verifier_detects_scoring_output_tampering(tmp_path: Path):
    cases = load_cases(REPO / "cases" / "quick")[:1]
    run_dir, _ = _finish_mock_run(tmp_path, cases)
    verdict_path = run_dir / "verdicts.jsonl"
    records = verdict_path.read_text(encoding="utf-8").splitlines()
    verdict = json.loads(records[0])
    verdict["explanation"] = "tampered"
    records[0] = json.dumps(verdict, ensure_ascii=False)
    verdict_path.write_text("\n".join(records) + "\n", encoding="utf-8")
    result = verify_run(run_dir)
    assert not result.ok
    assert "评分产物哈希不一致: verdicts.jsonl" in result.errors


class _FailingAdapter(MockAdapter):
    def send(self, session_id: str, message: str):
        raise AgentUnavailable("backend unavailable")


class _TokenAdapter(MockAdapter):
    def send(self, session_id: str, message: str):
        reply = super().send(session_id, message)
        reply.token_usage = TokenUsage(prompt=11, completion=7)
        return reply


class _InterruptingAdapter(MockAdapter):
    def send(self, session_id: str, message: str):
        raise KeyboardInterrupt("operator stopped run")


def test_runtime_failure_is_preserved_and_invalidates_all_probes(tmp_path: Path):
    cases = load_cases(REPO / "cases" / "quick")[:1]
    run_id, stores = run_suite(_FailingAdapter(), cases, tmp_path, "failing")
    verdicts = evaluate_case(cases[0], stores[0], run_id)
    assert verdicts
    assert all(item.verdict == VerdictValue.INVALID_RUN for item in verdicts)
    run_dir = tmp_path / run_id
    assert verify_run(run_dir).ok
    case_status = json.loads(next((run_dir / "cases").iterdir()).joinpath(
        "case.json").read_text(encoding="utf-8"))
    assert case_status["status"] == "invalid"
    assert "backend unavailable" in case_status["runtime_error"]


def test_external_interrupt_seals_current_case_in_aborted_manifest(tmp_path: Path):
    cases = load_cases(REPO / "cases" / "quick")[:1]
    with pytest.raises(KeyboardInterrupt):
        run_suite(_InterruptingAdapter(), cases, tmp_path, "interrupted")
    run_dir = next(tmp_path.iterdir())
    manifest = json.loads(
        (run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "aborted"
    assert manifest["n_cases_completed"] == 1
    assert len(manifest["case_results"]) == 1
    assert manifest["case_results"][0]["status"] == "invalid"
    assert manifest["evidence_bundle_sha256"]


def test_repeat_analysis_reports_pass_to_k(tmp_path: Path):
    cases = load_cases(REPO / "cases" / "quick")[:1]
    first, _ = _finish_mock_run(tmp_path / "a", cases)
    second, _ = _finish_mock_run(tmp_path / "b", cases)
    out = tmp_path / "stability"
    result = analyze_stability([first, second], out)
    assert result["verdict_agreement_rate"] == 1.0
    assert result["n_flips"] == 0
    assert 0.0 <= result["pass_all_rate"] <= 1.0
    assert (out / "stability.json").is_file()
    assert "pass^2" in (out / "stability.md").read_text(encoding="utf-8")


def test_manifest_records_source_and_agent_token_cost(tmp_path: Path):
    cases = load_cases(REPO / "cases" / "quick")[:1]
    run_id, _ = run_suite(_TokenAdapter(), cases, tmp_path, "token-agent")
    manifest = json.loads(
        (tmp_path / run_id / "manifest.json").read_text(encoding="utf-8"))
    usage = manifest["cost"]["agent_tokens"]
    assert manifest["code_version"].startswith("sha256:")
    assert len(manifest["source_sha256"]) == 64
    assert usage["requests"] == usage["reported_replies"] > 0
    assert usage["coverage"] == 1.0
    assert usage["total"] == usage["requests"] * 18


def test_judge_tracks_reported_and_missing_usage():
    judge = OpenAICompatJudge("judge-a", "https://example.invalid/v1", "model-a", "x")
    judge._record_usage({
        "prompt_tokens": 23,
        "completion_tokens": 5,
        "total_tokens": 28,
    })
    judge._record_usage(None)
    usage = judge.usage_summary()
    assert usage["completions"] == 2
    assert usage["reported_completions"] == 1
    assert usage["unreported_completions"] == 1
    assert usage["coverage"] == 0.5
    assert usage["total"] == 28
