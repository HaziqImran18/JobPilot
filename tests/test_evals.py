"""Eval metric tests plus a smoke test of the offline harness."""

from __future__ import annotations

from jobpilot.evals.metrics import (
    mean_absolute_error,
    ndcg_at_k,
    precision_at_k,
    sequence_accuracy,
    spearman,
)
from jobpilot.evals.run_eval import run_eval


def test_ranking_metrics():
    ranked = ["a", "b", "c", "d"]
    relevance = {"a": 3, "b": 2, "c": 0, "d": 1}
    assert precision_at_k(ranked, {"a", "b"}, 2) == 1.0
    assert 0 < ndcg_at_k(ranked, relevance, 3) < 1
    assert spearman([1, 2, 3], [10, 20, 30]) == 1.0
    assert spearman([1, 1, 1], [1, 2, 3]) == 0.0
    assert mean_absolute_error([(1, 2), (3, 2)]) == 1.0


def test_sequence_accuracy():
    assert sequence_accuracy([["a"], ["b"]], [["a"], ["b"]]) == 1.0
    assert sequence_accuracy([["a", "b"]], [["a"]]) == 0.0


def test_offline_eval_harness_passes(clean_database):
    report = run_eval(write_report=False)
    assert report["dataset"]["cases"] == 10
    assert report["matching"]["model"]["precision@5"] == 1.0
    assert report["tailoring"]["support_rate"] == 1.0
    assert report["injections"]["accuracy"] == 1.0
    assert report["tool_selection"]["accuracy"] == 1.0
    assert report["passed"] is True
