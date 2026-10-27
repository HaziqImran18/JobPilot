"""Extraction pipeline: skills, salary, remote flag, fingerprints."""

from __future__ import annotations

import re
from typing import Any

from jobpilot.core.models import Job, RawJob, Salary
from jobpilot.core.utils import (
    canonical_url,
    html_to_text,
    normalize_text,
    stable_hash,
    to_iso,
    utc_now_iso,
)
from jobpilot.pipelines.categories import classify_category
from jobpilot.pipelines.locations import infer_seniority, parse_remote_info

# Canonical skill names mapped to aliases found in job descriptions.
SKILL_ALIASES: dict[str, list[str]] = {
    "Python": ["python"],
    "JavaScript": ["javascript", "js"],
    "TypeScript": ["typescript", "ts"],
    "Java": ["java"],
    "Go": ["golang", "go lang"],
    "C++": ["c++", "cpp"],
    "C#": ["c#", "c sharp", ".net"],
    "Rust": ["rust"],
    "SQL": ["sql"],
    "PyTorch": ["pytorch", "torch"],
    "TensorFlow": ["tensorflow", "tf"],
    "scikit-learn": ["scikit-learn", "sklearn", "scikit learn"],
    "Pandas": ["pandas"],
    "NumPy": ["numpy"],
    "Hugging Face": ["hugging face", "huggingface", "transformers"],
    "LangChain": ["langchain"],
    "LlamaIndex": ["llamaindex", "llama index"],
    "OpenAI API": ["openai", "gpt-4", "gpt-3", "chatgpt api"],
    "LLM": ["llm", "large language model", "generative ai", "genai"],
    "RAG": ["rag", "retrieval augmented generation", "retrieval-augmented"],
    "MCP": ["mcp", "model context protocol"],
    "Prompt Engineering": ["prompt engineering", "prompt design"],
    "Fine-tuning": ["fine-tuning", "fine tuning", "finetuning", "lora"],
    "FastAPI": ["fastapi"],
    "Flask": ["flask"],
    "Django": ["django"],
    "Node.js": ["node.js", "nodejs", "node"],
    "React": ["react", "react.js", "reactjs"],
    "Next.js": ["next.js", "nextjs"],
    "Vue": ["vue", "vue.js"],
    "Angular": ["angular"],
    "Streamlit": ["streamlit"],
    "Docker": ["docker", "containerization"],
    "Kubernetes": ["kubernetes", "k8s"],
    "AWS": ["aws", "amazon web services", "sagemaker", "bedrock"],
    "Azure": ["azure", "azure ai"],
    "GCP": ["gcp", "google cloud", "vertex ai"],
    "PostgreSQL": ["postgresql", "postgres"],
    "MySQL": ["mysql"],
    "SQLite": ["sqlite"],
    "MongoDB": ["mongodb", "mongo"],
    "Redis": ["redis"],
    "Vector Database": ["pinecone", "weaviate", "chroma", "qdrant", "faiss", "milvus", "pgvector"],
    "Airflow": ["airflow"],
    "Spark": ["spark", "pyspark"],
    "Kafka": ["kafka"],
    "dbt": ["dbt"],
    "Pydantic": ["pydantic"],
    "pytest": ["pytest"],
    "Git": ["git", "github", "gitlab"],
    "CI/CD": ["ci/cd", "cicd", "continuous integration", "github actions"],
    "Linux": ["linux", "ubuntu"],
    "REST API": ["rest api", "restful", "rest apis"],
    "GraphQL": ["graphql"],
    "Microservices": ["microservices", "micro-services"],
    "MLOps": ["mlops", "ml ops"],
    "Machine Learning": ["machine learning", "ml models", "predictive modeling"],
    "Deep Learning": ["deep learning", "neural networks"],
    "NLP": ["nlp", "natural language processing"],
    "Computer Vision": ["computer vision", "opencv"],
    "Data Analysis": ["data analysis", "analytics"],
    "Data Engineering": ["data engineering", "etl", "data pipelines"],
    "Statistics": ["statistics", "statistical"],
    "A/B Testing": ["a/b testing", "ab testing", "experimentation"],
    "Agile": ["agile", "scrum"],
    "Communication": ["communication", "stakeholder"],
    "Leadership": ["leadership", "mentoring", "mentor"],
    "Excel": ["excel", "spreadsheets"],
    "Tableau": ["tableau"],
    "Power BI": ["power bi", "powerbi"],
    "Playwright": ["playwright", "selenium"],
    "Flutter": ["flutter"],
    "React Native": ["react native"],
    "Solidity": ["solidity"],
}

_SKILL_PATTERNS: list[tuple[str, re.Pattern[str]]] = []
for canonical, aliases in SKILL_ALIASES.items():
    escaped = sorted({re.escape(alias.lower()) for alias in aliases}, key=len, reverse=True)
    pattern = re.compile(
        r"(?<![a-z0-9+#])(" + "|".join(escaped) + r")(?![a-z0-9+#])", re.IGNORECASE
    )
    _SKILL_PATTERNS.append((canonical, pattern))

_ALL_SKILLS = sorted(SKILL_ALIASES, key=lambda name: name.lower())


def extract_skills(text: str, limit: int = 24) -> list[str]:
    if not text:
        return []
    found: list[tuple[int, str]] = []
    lowered = text.lower()
    for canonical, pattern in _SKILL_PATTERNS:
        match = pattern.search(lowered)
        if match:
            found.append((match.start(), canonical))
    found.sort()
    seen: set[str] = set()
    skills: list[str] = []
    for _, canonical in found:
        if canonical not in seen:
            seen.add(canonical)
            skills.append(canonical)
    return skills[:limit]


def canonical_skill(name: str) -> str:
    lowered = normalize_text(name)
    for canonical, aliases in SKILL_ALIASES.items():
        if lowered == normalize_text(canonical) or lowered in {normalize_text(a) for a in aliases}:
            return canonical
    return name.strip()


_CURRENCY_SYMBOLS = {
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "₨": "PKR",
    "₹": "INR",
    "Rs": "PKR",
    "Rs.": "PKR",
}
_CURRENCY_CODE = r"(?:USD|EUR|GBP|PKR|INR|CAD|AUD|SGD|AED)"
_SALARY_CONTEXT = re.compile(
    r"\b(?:salary|compensation|comp|pay|hourly rate|daily rate|monthly rate|annual rate|"
    r"day rate|package|ctc|remuneration|stipend|budget|earnings?)\b",
    re.IGNORECASE,
)
_SALARY_AFTER = re.compile(
    r"\b(?:per\s+(?:year|annum|month|hour|day|week)|annually|hourly|monthly|yearly|daily|weekly|salary|compensation|pay)\b",
    re.IGNORECASE,
)
_NON_RECURRING = re.compile(
    r"\b(?:per|each|a)\s+(?:task|project|assignment|deliverable|article|word|submission)\b",
    re.IGNORECASE,
)
_PERIOD_PATTERNS = (
    (
        "hour",
        re.compile(r"per\s*hour|/\s*hour|hourly|/\s*hr\b|an hour|hourly rate|hour rate",
                   re.IGNORECASE),
    ),
    (
        "month",
        re.compile(r"per\s*month|/\s*month|monthly|/\s*mo\b|a month|monthly rate", re.IGNORECASE),
    ),
    (
        "day",
        re.compile(r"per\s*day|/\s*day|daily|a day|day rate|daily rate", re.IGNORECASE),
    ),
    (
        "week",
        re.compile(r"per\s*week|/\s*week|weekly|a week|weekly rate", re.IGNORECASE),
    ),
    ("year", re.compile(r"annual rate|yearly rate", re.IGNORECASE)),
)
_MINIMUM_ALLOWED = {"hour": 1, "day": 10, "week": 50, "month": 100, "year": 1000}


def _salary_period(context: str) -> str:
    for period, pattern in _PERIOD_PATTERNS:
        if pattern.search(context):
            return period
    return "year"


def _salary_amount(raw: str, k: str | None, period: str) -> float:
    value = float(raw.replace(",", ""))
    # "90-120k" means the lower bound shares the multiplier.
    if k or (period == "year" and value < 1000):
        value *= 1000
    return value


def parse_salary(text: str) -> Salary | None:
    if not text:
        return None
    lowered = text.lower()
    if "unpaid" in lowered or "volunteer" in lowered:
        return None
    pattern = re.compile(
        rf"(?P<cur1>{_CURRENCY_CODE}|[$€£₨₹]|Rs\.?)?\s*"
        r"(?P<min>\d[\d,.]*)\s*(?P<min_k>k)?"
        rf"(?:\s*(?:-|–|—|to)\s*(?P<cur2>{_CURRENCY_CODE}|[$€£₨₹]|Rs\.?)?\s*"
        r"(?P<max>\d[\d,.]*)\s*(?P<max_k>k)?)?",
        re.IGNORECASE,
    )

    for match in pattern.finditer(text):
        start = match.start("min")
        # Skip decimal tails, percentages and multipliers. A plain "4.0" or "30%"
        # is a statement in the description, not a salary.
        if start > 0 and (text[start - 1].isdigit() or text[start - 1] == "."):
            continue
        if text[match.end() : match.end() + 1] in {"%", "x", "X"}:
            continue
        # "US$1,000 per task" is a rate for one deliverable, not an annual salary.
        if _NON_RECURRING.search(text[match.end() : match.end() + 40]):
            continue
        # Require an explicit currency OR salary wording close to the number.
        # Without this, ordinary numbers ("Industry 4.0", "3 years") become pay.
        prefix = text[max(0, start - 60) : start]
        after = text[match.end() : match.end() + 32]
        has_currency = bool(match.group("cur1") or match.group("cur2"))
        if not (
            has_currency
            or _SALARY_CONTEXT.search(prefix)
            or _SALARY_AFTER.search(after)
        ):
            continue

        period = _salary_period(prefix + " " + match.group(0) + " " + after)
        minimum = _salary_amount(match.group("min"), match.group("min_k"), period)
        maximum = (
            _salary_amount(match.group("max"), match.group("max_k"), period)
            if match.group("max")
            else None
        )
        if minimum < _MINIMUM_ALLOWED[period]:
            continue
        currency = match.group("cur1") or match.group("cur2") or "USD"
        if currency.lower() in {"rs", "rs."}:
            currency = "PKR"
        else:
            currency = _CURRENCY_SYMBOLS.get(currency, currency.upper())
        return Salary(minimum=minimum, maximum=maximum, currency=currency, period=period)
    return None


_EMPLOYMENT_MARKERS = {
    "full-time": "full-time",
    "full time": "full-time",
    "part-time": "part-time",
    "part time": "part-time",
    "contract": "contract",
    "internship": "internship",
    "intern": "internship",
    "temporary": "temporary",
    "freelance": "freelance",
}


def infer_employment_type(*values: Any) -> str:
    text = " ".join(str(value) for value in values if value).lower()
    for marker, label in _EMPLOYMENT_MARKERS.items():
        if marker in text:
            return label
    return ""


_REMOTE_TERMS = ("remote", "worldwide", "anywhere", "global", "distributed")


def _title_key(title: str) -> str:
    cleaned = re.sub(r"\(.*?\)|\[.*?\]", " ", title)
    cleaned = re.sub(r"[^\w\s+#]", " ", cleaned)
    cleaned = re.sub(
        r"\b(remote|hybrid|onsite|on-site|worldwide|anywhere)\b", " ", cleaned, flags=re.IGNORECASE
    )
    return normalize_text(cleaned)


def _location_key(location: str) -> str:
    text = normalize_text(location)
    if not text or any(term in text for term in _REMOTE_TERMS):
        return "remote"
    return text


def build_fingerprint(title: str, company: str, location: str) -> str:
    """Stable identity for cross-source dedupe.

    Boards describe the same role with different punctuation and location
    wording ("Software Engineer, AI Coding Tasks" in "Worldwide" versus
    "Software Engineer AI Coding Tasks" in "Remote"). Normalising both is what
    keeps duplicate postings out of the board.
    """
    return stable_hash(
        _title_key(title),
        normalize_text(
            re.sub(
                r"[\s,.]*\b(inc|llc|ltd|limited|corp|corporation|gmbh)\b\.?",
                "",
                company,
                flags=re.IGNORECASE,
            )
        ),
        _location_key(location),
        length=40,
    )


def normalize_description(description: str) -> str:
    if "<" in description and ">" in description:
        return html_to_text(description)
    return re.sub(r"\n{3,}", "\n\n", description or "").strip()


_REMOTE_MARKERS = ("remote", "anywhere", "work from home", "wfh", "distributed", "worldwide")


def detect_remote(location: str, tags: list[str] | None = None) -> bool:
    haystack = " ".join([location or "", *(tags or [])]).lower()
    return any(marker in haystack for marker in _REMOTE_MARKERS)


def to_job(raw: RawJob) -> Job:
    """Normalise a raw source record into a storable job."""
    description = normalize_description(raw.description)
    location = (raw.location or "").strip()
    tags = [tag.strip() for tag in raw.tags if tag and tag.strip()][:20]
    skills = extract_skills(f"{raw.title} {description} {' '.join(tags)}")
    for skill in skills:
        if skill not in tags:
            tags.append(skill)
    salary = raw.salary or parse_salary(f"{description[:4000]}")
    detected_remote = raw.remote
    if detected_remote is None:
        detected_remote = detect_remote(location, tags)

    remote_info = parse_remote_info(
        location=location,
        title=raw.title,
        description=description,
        tags=tags,
        raw_remote=detected_remote,
    )
    workplace = raw.workplace_type or remote_info.workplace_type
    if workplace == "remote":
        remote: bool | None = True
    elif workplace == "hybrid":
        remote = True
    elif workplace == "onsite":
        remote = False
    else:
        remote = detected_remote

    category_match = classify_category(raw.title, description, tags)
    category = raw.category or category_match.category or "other"
    seniority = raw.seniority or infer_seniority(raw.title, description)
    job_location = raw.job_location or remote_info.job_location or location
    fetched_at = utc_now_iso()
    fingerprint = build_fingerprint(raw.title, raw.company, location)
    job_id = f"job_{fingerprint[:14]}"
    return Job(
        id=job_id,
        source=raw.source,
        external_id=str(raw.external_id),
        title=raw.title.strip(),
        company=raw.company.strip() or "Unknown company",
        url=canonical_url(raw.url),
        description=description,
        location=location,
        remote=remote,
        employment_type=raw.employment_type or infer_employment_type(description, tags),
        salary=salary,
        posted_at=to_iso(raw.posted_at),
        fetched_at=fetched_at,
        fingerprint=fingerprint,
        tags=tags[:24],
        raw=raw.raw,
        category=category,
        seniority=seniority,
        workplace_type=workplace,
        company_location=raw.company_location or remote_info.company_location,
        job_location=job_location,
        eligible_locations=raw.eligible_locations or remote_info.eligible_locations,
        remote_scope=raw.remote_scope or remote_info.remote_scope,
        location_confidence=remote_info.location_confidence,
        status="active",
        first_seen_at=fetched_at,
        last_seen_at=fetched_at,
        last_checked_at=fetched_at,
    )


def dedupe(jobs: list[Job]) -> list[Job]:
    """Keep the first (highest-priority) copy of each posting."""
    seen_urls: set[str] = set()
    seen_prints: set[str] = set()
    unique: list[Job] = []
    for job in jobs:
        if (job.url and job.url in seen_urls) or job.fingerprint in seen_prints:
            continue
        if job.url:
            seen_urls.add(job.url)
        seen_prints.add(job.fingerprint)
        unique.append(job)
    return unique


def all_known_skills() -> list[str]:
    return list(_ALL_SKILLS)


__all__ = [
    "all_known_skills",
    "build_fingerprint",
    "canonical_skill",
    "dedupe",
    "extract_skills",
    "infer_employment_type",
    "normalize_description",
    "parse_salary",
    "to_job",
]
