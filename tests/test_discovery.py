"""Occupational, geographic and shared-discovery behaviour."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from jobpilot.core.models import Job, JobQuery
from jobpilot.pipelines.categories import category_from_query, classify_category
from jobpilot.pipelines.intent import interpret_query
from jobpilot.pipelines.locations import eligibility_label, resolve_location
from jobpilot.services.discovery import DiscoveryService
from jobpilot.services.jobs import JobService


def _job(
    name: str,
    title: str,
    *,
    location: str = "Lahore, Pakistan",
    description: str = "",
    category: str = "",
    workplace: str = "",
    remote_scope: str = "",
    eligible: list[str] | None = None,
) -> Job:
    return Job(
        id=f"job_{name}",
        source="talent",
        external_id=name,
        title=title,
        company="Example",
        url=f"https://example.com/{name}",
        description=description or title,
        location=location,
        category=category or "other",
        workplace_type=workplace,
        remote_scope=remote_scope,
        eligible_locations=eligible or [],
        posted_at=datetime.now(UTC) - timedelta(days=1),
        fingerprint=f"fp_{name}",
    )


def test_interpret_query_extracts_role_location_and_workplace():
    intent = interpret_query(query="remote senior AI engineer", location="Lahore, Pakistan")
    assert intent.category == "ai_ml"
    assert intent.seniority == "senior"
    assert intent.workplace_type == "remote"
    assert intent.resolved_location.known
    assert intent.resolved_location.city == "Lahore"
    assert intent.resolved_location.country == "Pakistan"


def test_category_filter_excludes_unrelated_occupations(clean_database):
    service = JobService()
    service.upsert(
        [
            _job(
                "ai",
                "Machine Learning Engineer",
                description="Build Python AI models.",
                category="ai_ml",
            ),
            _job(
                "accountant",
                "Senior Accountant",
                description="Own the general ledger and month-end close.",
                category="accounting",
            ),
            _job(
                "backend",
                "Backend Developer",
                description="Django APIs and PostgreSQL.",
                category="software",
            ),
        ]
    )
    result = service.list_cards(category="ai_ml", limit=20)
    ids = {item["id"] for item in result["items"]}
    assert "job_ai" in ids
    assert "job_accountant" not in ids
    # Software is a related family, but it only appears when the query is broad
    # enough to justify it.
    assert result["exact"] == 1


def test_related_role_requires_full_query_overlap(clean_database):
    service = JobService()
    service.upsert(
        [
            _job("ai", "AI Engineer", description="Python and LLM systems.", category="ai_ml"),
            _job(
                "backend",
                "Backend Developer",
                description="Django APIs and PostgreSQL.",
                category="software",
            ),
            _job(
                "platform",
                "AI Platform Engineer",
                description="Python AI infrastructure.",
                category="software",
            ),
        ]
    )
    specific = service.list_cards(query="python ai", limit=20)
    specific_ids = {item["id"] for item in specific["items"]}
    assert "job_ai" in specific_ids
    assert "job_platform" in specific_ids
    assert "job_backend" not in specific_ids

    broad = service.list_cards(query="ai", limit=20)
    broad_ids = {item["id"] for item in broad["items"]}
    assert "job_backend" in broad_ids


def test_remote_eligibility_is_not_advertised_location(clean_database):
    service = JobService()
    service.upsert(
        [
            _job(
                "worldwide",
                "AI Engineer",
                location="Remote",
                workplace="remote",
                remote_scope="worldwide",
                eligible=["Worldwide"],
                category="ai_ml",
            ),
            _job(
                "pakistan_only",
                "ML Engineer",
                location="Remote - Pakistan",
                workplace="remote",
                remote_scope="country",
                eligible=["Pakistan"],
                category="ai_ml",
            ),
            _job(
                "islamabad_only",
                "Data Scientist",
                location="Islamabad, Pakistan",
                workplace="onsite",
                category="data",
            ),
        ]
    )
    lahore = service.list_cards(location="Lahore", limit=20)
    ids = {item["id"] for item in lahore["items"]}
    assert "job_worldwide" in ids
    assert "job_pakistan_only" in ids
    assert "job_islamabad_only" not in ids

    dubai = service.list_cards(location="Dubai, United Arab Emirates", limit=20)
    dubai_ids = {item["id"] for item in dubai["items"]}
    assert "job_worldwide" in dubai_ids
    assert "job_pakistan_only" not in dubai_ids
    assert "job_islamabad_only" not in dubai_ids


def test_eligibility_labels_are_explicit():
    assert eligibility_label("remote", "worldwide", ["Worldwide"]) == "Remote · worldwide"
    assert "Pakistan" in eligibility_label("remote", "country", ["Pakistan"])
    assert eligibility_label("remote", "unknown", []) == "Remote · eligibility unknown"
    assert eligibility_label("onsite", "", [], "Lahore, Pakistan") == "On-site · Lahore, Pakistan"


def test_category_taxonomy_covers_non_tech_roles():
    assert classify_category("Senior Accountant", "General ledger and tax").category == "accounting"
    assert classify_category("Relationship Manager", "Retail banking and deposits").category == "banking"
    assert classify_category("Sales Executive", "B2B software sales").category == "sales"
    assert classify_category("HR Officer", "Recruitment and onboarding").category == "hr"
    assert classify_category("Staff Nurse", "Patient care in a hospital").category == "healthcare"
    assert category_from_query("devops engineer") == "devops"


def test_company_description_does_not_decide_the_occupation():
    # Limetax is an accounting company, but these are engineering roles.
    assert (
        classify_category(
            "Forward Deployed Engineer",
            "Limetax builds an agentic accounting and tax platform for German SMEs.",
        ).category
        == "engineering"
    )
    assert (
        classify_category(
            "Senior Customer Success Engineer, Audit",
            "Support customers using audit and accounting workflows.",
        ).category
        == "sales"
    )
    assert (
        classify_category(
            "Senior Product Manager II - Vendor Spend",
            "Accounting, finance and procurement data.",
        ).category
        == "management"
    )
    # A lone weak word is not enough to claim an occupation.
    assert classify_category("Shopify Assistant And Accounts Support").category == "other"


def test_postal_codes_disambiguate_cities():
    assert resolve_location("San Francisco, CA").country == "United States"
    assert resolve_location("London, ON").country == "Canada"
    assert resolve_location("London").country == "United Kingdom"
    assert resolve_location("Hyderabad, PK").country == "Pakistan"
    assert resolve_location("Hyderabad, IN").country == "India"
    assert resolve_location("Lahore, Punjab, PK").city == "Lahore"
    # A conflicting code must not silently turn this into Lahore, Pakistan.
    assert resolve_location("Lahore, MN, IN").country != "Pakistan"


def test_onsite_location_confidence_tracks_the_advertised_place():
    from jobpilot.pipelines.locations import parse_remote_info

    verified = parse_remote_info("Lahore, Punjab, PK", title="Accountant")
    assert verified.workplace_type == "onsite"
    assert verified.location_confidence == "verified"

    unknown = parse_remote_info("", title="Accountant")
    assert unknown.location_confidence == "unknown"


def test_query_role_is_not_degraded_to_a_body_keyword(clean_database):
    service = JobService(clean_database)
    service.upsert(
        [
            _job("analyst", "Data Analyst", description="SQL dashboards.", category="data"),
            _job(
                "procurement",
                "Procurement Officer",
                description="Work with data and suppliers.",
                category="operations",
            ),
        ]
    )
    result = service.list_cards(query="data analyst", limit=20)
    ids = {item["id"] for item in result["items"]}
    assert "job_analyst" in ids
    assert "job_procurement" not in ids
    exact = {item["id"] for item in result["items"] if item["match_kind"] == "exact"}
    assert exact == {"job_analyst"}


def test_discovery_caches_live_runs_and_reports_sources(clean_database):
    jobs = JobService(clean_database)
    service = DiscoveryService(clean_database, jobs)
    calls = {"count": 0}

    def fake_refresh(query):
        calls["count"] += 1
        jobs.upsert(
            [
                _job(
                    "ai",
                    "AI Engineer",
                    description="Python and LLM systems.",
                    category="ai_ml",
                )
            ]
        )
        return {
            "fetched": 1,
            "stored": 1,
            "errors": {"indeed": "403 blocked"},
            "counts": {"talent": 1, "indeed": 0},
            "sources": ["talent"],
            "source_status": [
                {"name": "talent", "ok": True, "state": "ok", "count": 1, "error": ""},
                {"name": "indeed", "ok": False, "state": "error", "count": 0, "error": "403 blocked"},
            ],
            "duration_ms": 12,
            "interpreted": {},
            "retrieved_at": datetime.now(UTC).isoformat(),
        }

    jobs.refresh = fake_refresh  # type: ignore[method-assign]
    query = JobQuery(query="AI engineer", location="Lahore")

    first = service.discover(query, limit=10)
    assert first.cached is False
    assert first.fetched == 1
    assert first.total == 1
    assert first.ok is True
    assert any(source["name"] == "indeed" and source["state"] == "error" for source in first.source_status)

    second = service.discover(query, limit=10)
    assert second.cached is True
    assert calls["count"] == 1

    forced = service.discover(query, force=True, limit=10)
    assert forced.cached is False
    assert calls["count"] == 2


def test_stored_source_health_matches_the_live_status_shape(clean_database):
    jobs = JobService(clean_database)
    now = datetime.now(UTC).isoformat()
    jobs.db.execute(
        "INSERT INTO source_health(name, ok, detail, latency_ms, checked_at) VALUES (?, ?, ?, ?, ?)",
        ("talent", 1, "12 results", 210, now),
    )
    jobs.db.execute(
        "INSERT INTO source_health(name, ok, detail, latency_ms, checked_at) VALUES (?, ?, ?, ?, ?)",
        ("indeed", 0, "403 blocked", None, now),
    )
    status = DiscoveryService(clean_database, jobs).status()
    by_name = {source["name"]: source for source in status["sources"]}
    assert by_name["talent"]["state"] == "ok"
    assert by_name["talent"]["count"] == 12
    assert by_name["indeed"]["state"] == "error"
    assert by_name["indeed"]["error"] == "403 blocked"


def test_discovery_never_claims_success_when_every_source_fails(clean_database):
    jobs = JobService(clean_database)
    service = DiscoveryService(clean_database, jobs)

    def failing_refresh(query):
        return {
            "fetched": 0,
            "stored": 0,
            "errors": {"linkedin": "timed out", "talent": "403 blocked"},
            "counts": {"linkedin": 0, "talent": 0},
            "sources": [],
            "source_status": [
                {"name": "linkedin", "ok": False, "state": "error", "count": 0, "error": "timed out"},
                {"name": "talent", "ok": False, "state": "error", "count": 0, "error": "403 blocked"},
            ],
            "duration_ms": 4,
            "interpreted": {},
            "retrieved_at": datetime.now(UTC).isoformat(),
        }

    jobs.refresh = failing_refresh  # type: ignore[method-assign]
    result = service.discover(JobQuery(query="AI engineer"), force=True, limit=10)
    assert result.ok is False
    assert result.total == 0
    assert "failed" in result.message.lower()
    assert set(result.errors) == {"linkedin", "talent"}
