"""Service tests: ranking, tailoring, tracking, memory and chat persistence."""

from __future__ import annotations

import pytest

from jobpilot.core.models import ApplicationStatus, JobQuery, MemoryKind
from jobpilot.harness.guardrails import ClaimVerifier
from jobpilot.memory.embeddings import HashingEmbedder
from jobpilot.memory.policy import MemoryPolicy
from jobpilot.memory.store import MemoryStore
from jobpilot.services.chat import ChatService
from jobpilot.services.jobs import JobService
from jobpilot.services.matching import MatchingService
from jobpilot.services.profile import ProfileService
from jobpilot.services.tailoring import TailoringService
from jobpilot.services.tracking import TrackingService


@pytest.fixture
def services(seeded_workspace):
    db = seeded_workspace
    jobs = JobService(db)
    profiles = ProfileService(db)
    matching = MatchingService(db=db, jobs=jobs, profile_service=profiles)
    return {"db": db, "jobs": jobs, "profiles": profiles, "matching": matching}


def test_jobs_are_ranked_with_a_profile(services):
    assert services["jobs"].count() == 10
    ranked = services["matching"].rank_jobs(
        JobQuery(query="AI Engineer", limit=6), limit=6, refresh=False, use_llm=False
    )
    assert len(ranked) == 6
    scores = [item.match.score for item in ranked]
    assert scores == sorted(scores, reverse=True)
    top_titles = {item.job.title for item in ranked[:3]}
    assert "AI Engineer" in top_titles
    assert ranked[0].match.score >= 60
    assert ranked[0].match.matched_skills


def test_title_fit_prefers_the_target_role():
    from jobpilot.core.models import Preferences, Profile

    profile = Profile(
        headline="AI Engineer",
        preferences=Preferences(target_roles=["AI Engineer"]),
    )
    relevant = MatchingService._title_fit("AI Application Engineer (LLM & RAG)", profile)
    unrelated = MatchingService._title_fit("Sales Engineer", profile)
    assert relevant > unrelated
    assert unrelated < 0.6


def test_new_profile_starts_blank(clean_database):
    profile = ProfileService(clean_database).get()
    assert profile.full_name == ""
    assert profile.experiences == []
    assert profile.skills == []


def test_search_uses_sqlite_fts(services):
    result = services["jobs"].list_cards(
        profile_version="test", query="retrieval augmented", limit=10
    )
    assert result["total"] >= 1
    assert any("Trellis" in item["company"] for item in result["items"])


def test_deterministic_tailoring_is_verified(services):
    jobs, profiles, db = services["jobs"], services["profiles"], services["db"]
    first = jobs.search(JobQuery(query="AI Engineer", limit=1))[0]
    tailoring = TailoringService(db=db, jobs=jobs, profiles=profiles)
    result = tailoring.tailor_cv(first.id, use_llm=False)
    assert result.job_id == first.id
    assert result.bullets
    assert result.verified is True
    assert result.unsupported == []
    assert result.cv_path.endswith(".md")
    assert result.claims and all(claim.supported for claim in result.claims)


def test_tracking_pipeline(services):
    jobs, db = services["jobs"], services["db"]
    first = jobs.search(JobQuery(query="AI Engineer", limit=1))[0]
    tracking = TrackingService(db=db, jobs=jobs)
    application = tracking.add(job_id=first.id, notes="Apply after review.")
    assert application.status == ApplicationStatus.saved
    assert application.job_title == first.title

    moved = tracking.update_status(
        application.id, ApplicationStatus.applied, "Submitted via portal."
    )
    assert moved.status == ApplicationStatus.applied
    detail = tracking.get(application.id)
    assert detail is not None
    assert len(detail.events) == 2
    assert tracking.stats()["applied"] == 1
    assert tracking.board()["applied"][0]["id"] == application.id

    assert tracking.delete(application.id) is True
    assert tracking.get(application.id) is None


def test_memory_policy_and_recall(clean_database):
    db = clean_database
    store = MemoryStore(db, embedder=HashingEmbedder(256))
    policy = MemoryPolicy()
    profile = ProfileService(db).get()

    inferred = policy.evaluate(
        text="I prefer remote roles.", kind=MemoryKind.semantic, profile=profile
    )
    assert inferred.requires_confirmation is True

    explicit = policy.evaluate(
        text="I prefer remote roles.", kind=MemoryKind.semantic, explicit=True, profile=profile
    )
    assert explicit.requires_confirmation is False

    later = policy.evaluate(
        text="Rejected a role because it required relocation.",
        kind=MemoryKind.episodic,
        profile=profile,
    )
    assert later.requires_confirmation is False

    memory, action = store.add(
        "I prefer remote-first teams and will not relocate outside Pakistan.",
        kind=MemoryKind.semantic,
        importance=0.9,
        confidence=0.95,
    )
    assert action in {"created", "merged", "confirmed"}
    recalled = store.recall("remote-first teams")
    assert recalled and recalled[0].id == memory.id
    assert recalled[0].similarity is not None
    assert store.forget(memory.id) is True
    assert store.recall("remote-first teams") == []


def test_chat_sessions_and_summary_state(clean_database):
    chat = ChatService(clean_database)
    session = chat.create_session()
    chat.add_message(session.id, "user", "Find AI roles in Islamabad.")
    chat.add_message(session.id, "assistant", "I found three strong matches.", {"steps": 2})
    messages = chat.get_messages(session.id)
    assert [message.role for message in messages] == ["user", "assistant"]
    assert chat.list_sessions()[0]["title"] == "Find AI roles in Islamabad."
    chat.delete_session(session.id)
    assert chat.get_session(session.id) is None


def test_claim_verifier_rejects_fabrication(seeded_workspace):
    profile = ProfileService(seeded_workspace).get()
    verifier = ClaimVerifier()
    supported = verifier.verify("Built a FastAPI retrieval service with Python.", profile)
    assert supported.supported is True
    fabricated = verifier.verify("Led a team of 12 engineers at Google.", profile)
    assert fabricated.supported is False
