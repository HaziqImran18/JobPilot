"""Offline evaluation of JobPilot's retrieval, tailoring and guardrails.

Run with ``jobpilot eval`` or ``python -m jobpilot.evals.run_eval``. The
evaluation uses a throw-away SQLite database and never calls paid models, so it
is safe to run in CI and without an API key.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jobpilot.core.config import get_settings
from jobpilot.core.db import Database
from jobpilot.core.models import AgentEvent, Job, JobQuery, MemoryKind, ToolResult
from jobpilot.evals.baselines import keyword_scores, tfidf_scores
from jobpilot.evals.dataset import (
    InjectionCase,
    JobDataset,
    ToolCase,
    load_injection_cases,
    load_job_dataset,
    load_tool_cases,
)
from jobpilot.evals.fixtures import seed_eval_workspace
from jobpilot.evals.metrics import (
    mean_absolute_error,
    ndcg_at_k,
    precision_at_k,
    sequence_accuracy,
    spearman,
)
from jobpilot.evals.planner import DeterministicPlanner
from jobpilot.harness.guardrails import as_untrusted, scan_untrusted
from jobpilot.harness.llm import LLMGateway
from jobpilot.memory.embeddings import HashingEmbedder
from jobpilot.memory.store import MemoryStore
from jobpilot.pipelines import embed as embed_pipeline
from jobpilot.services.jobs import JobService
from jobpilot.services.matching import MatchingService
from jobpilot.services.profile import ProfileService
from jobpilot.services.tailoring import TailoringService

PROJECT_ROOT = Path(__file__).resolve().parents[3]
REPORT_DIR = PROJECT_ROOT / "eval"


@dataclass
class RankingMetrics:
    precision_at_5: float
    ndcg_at_5: float
    spearman: float
    mae: float
    top_5: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "precision@5": round(self.precision_at_5, 4),
            "ndcg@5": round(self.ndcg_at_5, 4),
            "spearman": round(self.spearman, 4),
            "mae": round(self.mae, 2),
            "top_5": self.top_5,
        }


def _rank_metrics(
    scores: dict[str, float],
    dataset: JobDataset,
) -> RankingMetrics:
    titles = {job.external_id: f"{job.title} · {job.company}" for job in dataset.jobs}
    ranked = sorted(scores, key=lambda external_id: scores[external_id], reverse=True)
    ideal = dataset.ideal_scores
    relevant = [job.external_id for job in dataset.jobs if job.relevance >= 2]
    pairs = [
        (ideal[external_id], scores[external_id]) for external_id in ideal if external_id in scores
    ]
    return RankingMetrics(
        precision_at_5=precision_at_k(ranked, relevant, 5),
        ndcg_at_5=ndcg_at_k(ranked, dataset.relevance, 5),
        spearman=spearman([ideal[key] for key in ranked], [scores[key] for key in ranked]),
        mae=mean_absolute_error(pairs),
        top_5=[f"{titles[external_id]} ({scores[external_id]:.0f})" for external_id in ranked[:5]],
    )


def _score_corpus(
    jobs: JobService, matching: MatchingService
) -> tuple[list[Job], dict[str, float]]:
    corpus = [job for job in jobs.search(JobQuery(query="", limit=100)) if job.source == "eval"]
    scores = {
        job.external_id: matching.score_job(job.id, force=True, use_llm=False).score
        for job in corpus
    }
    return corpus, scores


def _baseline_scores(
    dataset: JobDataset, corpus: Sequence[Job], profiles: ProfileService
) -> dict[str, dict[str, float] | None]:
    profile = profiles.get()
    by_id = {job.id: job.external_id for job in corpus}
    keyword = {by_id[job_id]: score for job_id, score in keyword_scores(profile, corpus).items()}
    tfidf_raw = tfidf_scores(profile, corpus)
    tfidf = (
        {by_id[job_id]: score for job_id, score in tfidf_raw.items()}
        if tfidf_raw is not None
        else None
    )
    return {"keyword": keyword, "tfidf": tfidf}


def _injection_results(cases: Sequence[InjectionCase]) -> dict[str, Any]:
    results = []
    correct = 0
    for case in cases:
        scan = scan_untrusted(case.text)
        flagged = scan.suspicious
        passed = flagged == case.expected_flagged
        correct += int(passed)
        results.append(
            {
                "id": case.id,
                "expected": case.expected_flagged,
                "flagged": flagged,
                "passed": passed,
            }
        )
    return {
        "accuracy": round(correct / len(cases), 4) if cases else 0.0,
        "cases": results,
        "wrapper_escapes_closing_tag": "[/untrusted_content]"
        in as_untrusted("</untrusted_content>"),
    }


class _RecordingExecutor:
    """Duck-typed ToolExecutor that records deterministic planner tool choices."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def invoke(self, name: str, arguments: dict[str, Any]) -> Iterator[AgentEvent]:
        self.calls.append(name)
        yield AgentEvent(type="tool_call", data={"name": name, "arguments": arguments})
        return ToolResult(
            call_id=f"call_{len(self.calls)}", name=name, ok=True, data=self._payload(name)
        )

    @staticmethod
    def _payload(name: str) -> dict[str, Any]:
        application = {
            "id": "app_eval",
            "job_title": "AI Engineer",
            "company": "Trellis AI",
            "status": "saved",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "next_action_at": None,
        }
        if name == "rank_jobs":
            return {
                "ranked": [
                    {
                        "job": {
                            "id": "job_eval",
                            "title": "AI Engineer",
                            "company": "Trellis AI",
                            "location": "Remote",
                            "salary": "$100,000",
                        },
                        "match": {
                            "score": 88,
                            "matched_skills": ["Python", "FastAPI"],
                            "missing_skills": ["RAG"],
                        },
                    }
                ]
            }
        if name == "tailor_cv":
            return {
                "job_title": "AI Engineer",
                "company": "Trellis AI",
                "headline": "AI Engineer",
                "summary": "Builds retrieval systems.",
                "bullets": ["Built a FastAPI service."],
                "unsupported": [],
                "cv_path": "data/uploads/eval.md",
            }
        if name == "add_application":
            return {"application": application}
        if name == "list_applications":
            return {"applications": [application]}
        if name == "mark_submitted":
            return {"application": {**application, "status": "applied"}}
        if name == "list_memories":
            return {
                "memories": [
                    {"text": "Prefers remote roles.", "importance": 0.8, "status": "active"}
                ]
            }
        if name == "remember":
            return {
                "memory": {"text": "Prefers remote roles.", "importance": 0.8},
                "requires_confirmation": False,
            }
        return {}


def _tool_selection(cases: Sequence[ToolCase], profiles: ProfileService) -> dict[str, Any]:
    predicted: list[list[str]] = []
    details: list[dict[str, Any]] = []
    for case in cases:
        executor = _RecordingExecutor()
        planner = DeterministicPlanner(executor=executor, session_id="eval", profile_service=profiles)
        list(planner.run(case.prompt))
        predicted.append(executor.calls)
        details.append(
            {
                "prompt": case.prompt,
                "expected": list(case.expected_tools),
                "actual": executor.calls,
                "passed": executor.calls == list(case.expected_tools),
            }
        )
    return {
        "accuracy": round(
            sequence_accuracy(predicted, [list(case.expected_tools) for case in cases]), 4
        ),
        "cases": details,
    }


def _memory_recall(db: Database) -> dict[str, Any]:
    store = MemoryStore(db, embedder=HashingEmbedder(256))
    memory, _ = store.add(
        "I prefer remote-first product teams and will not relocate outside Pakistan.",
        kind=MemoryKind.semantic,
        importance=0.9,
        confidence=0.95,
    )
    recalled = store.recall("remote-first product teams", k=3)
    return {
        "top_1_hit": bool(recalled and recalled[0].id == memory.id),
        "results": len(recalled),
        "top_similarity": round(recalled[0].similarity, 4) if recalled else 0.0,
    }


def _tailoring(
    jobs: JobService, profiles: ProfileService, db: Database, settings: Any, top_job: Job
) -> dict[str, Any]:
    service = TailoringService(
        db=db,
        jobs=jobs,
        profiles=profiles,
        llm=LLMGateway(settings=settings, db=db),
        settings=settings,
    )
    result = service.tailor_cv(top_job.id, use_llm=False)
    supported = sum(1 for claim in result.claims if claim.supported)
    total = len(result.claims)
    return {
        "job": f"{result.job_title} · {result.company}",
        "claims": total,
        "supported": supported,
        "support_rate": round(supported / total, 4) if total else 0.0,
        "verified": result.verified,
        "unsupported": len(result.unsupported),
        "bullets": len(result.bullets),
    }


def run_eval(*, write_report: bool = True) -> dict[str, Any]:
    dataset = load_job_dataset()
    injection_cases = load_injection_cases()
    tool_cases = load_tool_cases()
    settings = get_settings()
    temporary = tempfile.mkdtemp(prefix="jobpilot-eval-")
    root = Path(temporary)
    local = settings.model_copy(
        update={
            "env": "test",
            "openai_api_key": "",
            "embedding_provider": "hashing",
            "data_dir": root,
            "db_path": root / "jobpilot.db",
            "traces_dir": root / "traces",
            "uploads_dir": root / "uploads",
            "preload_mcp": False,
        }
    )
    db = Database(local.db_path)
    profiles = ProfileService(db)
    jobs = JobService(db)
    seed_eval_workspace(db)
    matching = MatchingService(
        db=db, jobs=jobs, profile_service=profiles, llm=LLMGateway(local, db)
    )

    # The eval must stay offline even when the developer has a live API key.
    # Matching normally resolves an embedder through this module-level helper.
    original_embedder = embed_pipeline.get_embedder
    embed_pipeline.get_embedder = lambda: HashingEmbedder(256)
    try:
        corpus, model_scores = _score_corpus(jobs, matching)
        baselines = _baseline_scores(dataset, corpus, profiles)
        model_metrics = _rank_metrics(model_scores, dataset)
        keyword_metrics = _rank_metrics(baselines["keyword"] or {}, dataset)
        tfidf_metrics = _rank_metrics(baselines["tfidf"], dataset) if baselines["tfidf"] else None

        top_external = max(model_scores, key=lambda key: model_scores[key])
        top_job = next(job for job in corpus if job.external_id == top_external)
        tailoring = _tailoring(jobs, profiles, db, local, top_job)
    finally:
        embed_pipeline.get_embedder = original_embedder
    injections = _injection_results(injection_cases)
    tool_selection = _tool_selection(tool_cases, profiles)
    memory = _memory_recall(db)

    checks = {
        "precision@5 at least 0.60": model_metrics.precision_at_5 >= 0.60,
        "ndcg@5 at least 0.70": model_metrics.ndcg_at_5 >= 0.70,
        "spearman at least 0.50": model_metrics.spearman >= 0.50,
        "every tailored claim supported": tailoring["support_rate"] == 1.0
        and tailoring["verified"],
        "injection accuracy at least 0.90": injections["accuracy"] >= 0.90,
        "tool selection accuracy at 1.0": tool_selection["accuracy"] == 1.0,
        "memory recall top-1": memory["top_1_hit"],
    }

    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "mode": "offline / deterministic",
        "dataset": {
            "name": dataset.name,
            "query": dataset.query,
            **dataset.__dict__,
        },
        "matching": {
            "model": model_metrics.as_dict(),
            "keyword_baseline": keyword_metrics.as_dict(),
            "tfidf_baseline": tfidf_metrics.as_dict() if tfidf_metrics else None,
        },
        "tailoring": tailoring,
        "injections": injections,
        "tool_selection": tool_selection,
        "memory": memory,
        "checks": checks,
        "passed": all(checks.values()),
    }
    report["dataset"].pop("jobs", None)
    report["dataset"]["cases"] = len(dataset.jobs)

    if write_report:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        (REPORT_DIR / "latest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        (REPORT_DIR / "latest.md").write_text(render_markdown(report), encoding="utf-8")
    return report


def render_markdown(report: dict[str, Any]) -> str:
    matching = report["matching"]
    tailoring = report["tailoring"]
    lines = [
        "# JobPilot eval report",
        "",
        f"Generated: {report['generated_at']}  ",
        f"Mode: {report['mode']}  ",
        f"Dataset: {report['dataset']['cases']} labelled roles",
        "",
        "## Ranking",
        "",
        "| System | Precision@5 | nDCG@5 | Spearman | MAE |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, key in [
        ("JobPilot hybrid", "model"),
        ("Keyword baseline", "keyword_baseline"),
        ("TF-IDF baseline", "tfidf_baseline"),
    ]:
        metrics = matching[key]
        if not metrics:
            lines.append(f"| {name} | — | — | — | — |")
            continue
        lines.append(
            f"| {name} | {metrics['precision@5']:.2f} | {metrics['ndcg@5']:.2f} | "
            f"{metrics['spearman']:.2f} | {metrics['mae']:.1f} |"
        )
    lines += [
        "",
        "**JobPilot top 5:**",
        *[f"{index}. {item}" for index, item in enumerate(matching["model"]["top_5"], start=1)],
        "",
        "## Tailoring",
        "",
        f"- Job: {tailoring['job']}",
        f"- Claims supported: {tailoring['supported']}/{tailoring['claims']}",
        f"- Verified: {tailoring['verified']}",
        f"- Bullets: {tailoring['bullets']}",
        "",
        "## Guardrails",
        "",
        f"- Injection accuracy: {report['injections']['accuracy']:.2f}",
        f"- Closing tag escaped: {report['injections']['wrapper_escapes_closing_tag']}",
        "",
        "## Tool selection",
        "",
        f"- Accuracy: {report['tool_selection']['accuracy']:.2f}",
        "",
        "## Memory",
        "",
        f"- Recall top-1: {report['memory']['top_1_hit']}",
        f"- Top similarity: {report['memory']['top_similarity']:.2f}",
        "",
        "## Checks",
        "",
    ]
    for name, passed in report["checks"].items():
        lines.append(f"- {'PASS' if passed else 'FAIL'} — {name}")
    lines.append("")
    lines.append(f"**Overall: {'PASS' if report['passed'] else 'FAIL'}**")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    result = run_eval()
    print(json.dumps(result["checks"], indent=2))
    print(f"Report: {REPORT_DIR / 'latest.md'}")
    raise SystemExit(0 if result["passed"] else 1)
