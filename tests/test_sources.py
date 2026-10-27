"""Source parser tests and job-filter semantics (offline fixtures only)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from jobpilot.core.models import Job, Salary
from jobpilot.core.utils import parse_relative_date
from jobpilot.pipelines.extract import build_fingerprint
from jobpilot.services.jobs import JobService
from jobpilot.sources.indeed import parse_indeed_cards
from jobpilot.sources.linkedin import parse_linkedin_cards, parse_linkedin_detail
from jobpilot.sources.mustakbil import parse_mustakbil_cards
from jobpilot.sources.talent import parse_talent_cards

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_mustakbil_cards_are_parsed_with_rupee_salary():
    jobs = parse_mustakbil_cards(fixture("mustakbil_cards.html"))
    assert len(jobs) == 2
    first = jobs[0]
    assert first.source == "mustakbil"
    assert first.external_id == "1436818"
    assert first.title == "IoT Engineer (With DevOps)"
    assert first.company == "InMind Global"
    assert first.location == "Faisalabad, Pakistan"
    assert first.url == "https://www.mustakbil.com/jobs/job/1436818"
    assert first.salary is not None
    assert first.salary.currency == "PKR"
    assert first.salary.period == "month"
    assert first.salary.minimum == 90000
    assert first.posted_at is not None


def test_talent_cards_default_to_rupees_but_keep_explicit_usd():
    jobs = parse_talent_cards(fixture("talent_cards.html"))
    assert len(jobs) == 2
    rupees, dollars = jobs
    assert rupees.external_id == "632384923053856389"
    assert rupees.company == "HR Navigator"
    assert rupees.salary is not None
    assert rupees.salary.currency == "PKR"
    assert rupees.salary.period == "month"
    assert dollars.salary is not None
    assert dollars.salary.currency == "USD"


def test_linkedin_cards_and_detail_are_parsed():
    jobs = parse_linkedin_cards(fixture("linkedin_cards.html"))
    assert len(jobs) == 2
    first = jobs[0]
    assert first.external_id == "4100111222"
    assert first.title == "Senior Software Engineer - Python"
    assert first.company == "Convo"
    assert first.location == "Lahore, Punjab, Pakistan"
    assert first.url == "https://www.linkedin.com/jobs/view/senior-software-engineer-python-at-convo-4100111222"

    detail = parse_linkedin_detail(fixture("linkedin_detail.html"))
    assert "FastAPI, PostgreSQL and AWS" in str(detail["description"])
    salary = detail["salary"]
    assert salary is not None
    assert salary.currency == "PKR"
    assert salary.minimum == 400000


def test_indeed_cards_are_parsed():
    jobs = parse_indeed_cards(fixture("indeed_cards.html"))
    assert len(jobs) == 1
    job = jobs[0]
    assert job.external_id == "abc123def456"
    assert job.title == "Python Developer"
    assert job.company == "Systems Limited"
    assert job.location == "Lahore, Pakistan"
    assert job.salary is not None
    assert job.salary.minimum == 250000


def test_parse_relative_date_handles_board_phrasings():
    now = datetime.now(UTC)
    assert parse_relative_date("just posted") is not None
    assert (now - parse_relative_date("3 days ago")).days in (2, 3)
    assert (now - parse_relative_date("2 weeks ago")).days in (13, 14)
    assert (now - parse_relative_date("1 month ago")).days in (29, 30)
    assert parse_relative_date("") is None
    assert parse_relative_date("no date here") is None


def _job(name: str, location: str, remote: bool | None, days_ago: int = 1) -> Job:
    title = f"{name} Engineer"
    return Job(
        id=f"job_{name}",
        source="talent",
        external_id=name,
        title=title,
        company="Example",
        url=f"https://example.com/{name}",
        description=f"Role in {location}",
        location=location,
        remote=remote,
        salary=Salary(minimum=100000, maximum=150000, currency="PKR", period="month"),
        posted_at=datetime.now(UTC) - timedelta(days=days_ago),
        fingerprint=build_fingerprint(title, "Example", location),
    )


def _seed_jobs():
    service = JobService()
    service.upsert(
        [
            _job("lahore", "Lahore, Pakistan", False),
            _job("karachi", "Karachi, Pakistan", False),
            _job("islamabad", "Islamabad, Pakistan", True),
            _job("remote", "Remote - Worldwide", True),
            _job("berlin", "Berlin, Germany", False),
            _job("stale", "Lahore, Pakistan", False, days_ago=40),
        ]
    )
    return service


def test_location_filter_matches_the_requested_place_only(clean_database):
    service = _seed_jobs()
    result = service.list_cards(location="Lahore", limit=50)
    titles = {job["title"].split()[0] for job in result["items"]}
    # A city request accepts verified city matches plus roles explicitly open
    # worldwide. It must not accept a role advertised only in another city.
    assert titles == {"lahore", "stale", "remote"}

    pakistan = service.list_cards(location="Pakistan", limit=50)
    pak_titles = {job["title"].split()[0] for job in pakistan["items"]}
    # Country requests include every city in that country plus roles open
    # worldwide.
    assert pak_titles == {"lahore", "karachi", "islamabad", "stale", "remote"}
    assert "berlin" not in pak_titles


def test_remote_filter_never_bypasses_location(clean_database):
    service = _seed_jobs()
    remote = service.list_cards(remote=True, limit=50)
    remote_titles = {job["title"].split()[0] for job in remote["items"]}
    assert remote_titles == {"islamabad", "remote"}

    onsite = service.list_cards(remote=False, limit=50)
    onsite_titles = {job["title"].split()[0] for job in onsite["items"]}
    assert onsite_titles == {"lahore", "karachi", "berlin", "stale"}

    lahore_remote = service.list_cards(location="Lahore", remote=True, limit=50)
    # A remote role explicitly open worldwide is eligible for a Lahore
    # candidate. A role advertised only in Islamabad is not, even though it is
    # remote-capable.
    assert {job["title"].split()[0] for job in lahore_remote["items"]} == {"remote"}


def test_refetch_updates_a_stored_remote_flag(clean_database):
    service = JobService()
    onsite = _job("online", "Islamabad, Pakistan", False)
    service.upsert([onsite])
    assert service.list_cards(remote=True, limit=10)["items"] == []

    remote = onsite.model_copy(update={"remote": True})
    service.upsert([remote])
    titles = [job["title"] for job in service.list_cards(remote=True, limit=10)["items"]]
    assert titles == ["online Engineer"]
    assert service.list_cards(remote=False, limit=10)["items"] == []


def test_posted_filter_and_salary_sort(clean_database):
    service = _seed_jobs()
    recent = service.list_cards(posted_within_days=7, limit=50)
    assert "stale" not in {job["title"].split()[0] for job in recent["items"]}

    by_salary = service.list_cards(sort="salary", limit=2)
    assert len(by_salary["items"]) == 2


def test_fts_query_requires_every_term(clean_database):
    service = JobService()
    service.upsert(
        [
            Job(
                id="job_python_ai",
                source="talent",
                external_id="python-ai",
                title="Python AI Engineer",
                company="Example",
                url="https://example.com/python-ai",
                description="Build Python machine learning services.",
                location="Lahore, Pakistan",
                fingerprint=build_fingerprint("Python AI Engineer", "Example", "Lahore, Pakistan"),
            ),
            Job(
                id="job_python_only",
                source="talent",
                external_id="python-only",
                title="Python Developer",
                company="Example",
                url="https://example.com/python-only",
                description="Maintain Django APIs.",
                location="Lahore, Pakistan",
                fingerprint=build_fingerprint("Python Developer", "Example", "Lahore, Pakistan"),
            ),
        ]
    )
    result = service.list_cards(query="python ai", limit=50)
    assert [job["title"] for job in result["items"]] == ["Python AI Engineer"]
    fts = service._fts_query("python ai")
    assert " AND " in fts
