"""aggregate 子命令：mean±std 聚合与口径混杂拒绝。"""

import json

import pytest

from memhall.report.aggregate import aggregate_runs, format_table


def _mk_run(tmp_path, adapter, cases, overall, caps, run_id="20260930-000001-x"):
    d = tmp_path / run_id
    d.mkdir()
    (d / "manifest.json").write_text(
        json.dumps({"adapter": adapter, "cases": cases, "run_id": run_id}),
        encoding="utf-8")
    (d / "metrics.json").write_text(
        json.dumps({"overall_score": overall, "capability_scores": caps}),
        encoding="utf-8")
    return d


CAPS_A = {"persist": 1.0, "recall": 0.5, "dynamic_update": 0.0,
          "discriminate": 1.0, "boundary": 0.5, "reuse": 0.0}


def test_mean_std(tmp_path):
    a = _mk_run(tmp_path, "mock", "cases/full", 0.50, dict(CAPS_A, persist=0.0))
    b = _mk_run(tmp_path, "mock", "cases/full", 0.80, dict(CAPS_A, persist=1.0),
                run_id="20260930-000002-x")
    out = tmp_path / "agg"
    r = aggregate_runs([a, b], out)
    assert r["n_runs"] == 2
    assert r["overall"]["mean"] == pytest.approx(0.65)
    # 样本标准差 ddof=1：|0.5-0.65|=0.15, |0.8-0.65|=0.15 → sqrt(0.045)≈0.2121
    assert r["overall"]["std"] == pytest.approx(0.2121, abs=1e-3)
    assert r["capabilities"]["persist"]["mean"] == pytest.approx(0.5)
    assert (out / "aggregate.json").exists()
    assert "总分" in format_table(r)


def test_single_run_std_zero(tmp_path):
    a = _mk_run(tmp_path, "mock", "cases/full", 0.5, CAPS_A)
    r = aggregate_runs([a], tmp_path / "agg1")
    assert r["overall"]["std"] == 0.0


def test_mixed_adapter_rejected(tmp_path):
    a = _mk_run(tmp_path, "mock", "cases/full", 0.5, CAPS_A)
    b = _mk_run(tmp_path, "hermes", "cases/full", 0.7, CAPS_A,
                run_id="20260930-000003-x")
    with pytest.raises(ValueError, match="口径混杂"):
        aggregate_runs([a, b], tmp_path / "agg2")
