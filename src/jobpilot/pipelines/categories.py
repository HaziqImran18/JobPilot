"""Occupation/category interpretation and job classification.

The old pipeline treated every query as a technology search because the source
adapters only read technology categories and ranking compared every posting to
the profile. This module provides the missing vocabulary: a query is mapped to
an occupation *before* retrieval, and each job is classified from its own title
and body. Related occupations are explicit and can be labelled as related
instead of being silently mixed into exact results.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from jobpilot.core.utils import normalize_text

# Category key -> human label. Order matters only for stable display.
CATEGORY_LABELS: dict[str, str] = {
    "software": "Software Engineering",
    "ai_ml": "AI / Machine Learning",
    "data": "Data & Analytics",
    "devops": "DevOps & Cloud",
    "security": "Cybersecurity",
    "engineering": "Engineering",
    "finance": "Finance",
    "banking": "Banking",
    "accounting": "Accounting",
    "sales": "Sales",
    "marketing": "Marketing",
    "hr": "Human Resources",
    "support": "Customer Support",
    "operations": "Operations",
    "management": "Management",
    "healthcare": "Healthcare",
    "education": "Education",
    "legal": "Legal",
    "design": "Design",
    "admin": "Administration",
    "other": "Other",
}

# Weighted keywords. Title matches are weighted much more heavily than body
# matches, so "Sales Engineer" is sales while "Backend Engineer (sales tools)"
# stays software.
_KEYWORDS: dict[str, tuple[str, ...]] = {
    "finance": (
        "finance", "financial", "fp&a", "treasury", "investment", "investments",
        "equity research", "valuation", "wealth management", "portfolio", "credit analysis",
        "financial analyst", "finance manager", "finance officer", "finance executive",
        "financial controller", "budgeting", "forecast", "forecasting", "private equity",
        "venture capital", "asset management", "fund accounting", "risk analyst",
    ),
    "banking": (
        "banking", "bank", "banker", "loan", "loans", "credit officer", "credit analyst",
        "branch manager", "relationship manager", "teller", "mortgage", "aml", "kyc",
        "trade finance", "corporate banking", "retail banking", "islamic banking",
        "payment operations", "card operations", "treasury operations",
    ),
    "accounting": (
        "accounting", "accountant", "accounts", "bookkeeping", "bookkeeper", "tax",
        "taxation", "payable", "receivable", "ledger", "audit", "auditor", "cpa", "acca",
        "chartered accountant", "reconciliation", "payroll", "financial reporting",
        "accounts assistant", "accounts manager", "cost accountant", "internal audit",
    ),
    "software": (
        "software engineer", "software developer", "software development", "full stack",
        "full-stack", "frontend", "front-end", "backend", "back-end", "web developer",
        "mobile developer", "android", "ios developer", "flutter", "react", "node.js",
        "java developer", "python developer", "php", "laravel", "django", "spring boot",
        "golang", "c++", "c#", ".net", "programmer", "application developer",
        "api developer", "wordpress", "salesforce developer", "qa engineer",
        "test automation", "sdet",
    ),
    "ai_ml": (
        "machine learning", "ml engineer", "ai engineer", "artificial intelligence",
        "deep learning", "data scientist", "data science", "nlp", "natural language",
        "computer vision", "llm", "large language model", "generative ai", "genai",
        "mlops", "prompt engineer", "ai research", "research scientist", "rag",
    ),
    "data": (
        "data analyst", "data analytics", "business intelligence", "bi analyst",
        "data engineer", "etl", "data warehouse", "power bi", "tableau", "looker",
        "sql developer", "database administrator", "dba", "statistician", "analytics engineer",
        "reporting analyst", "data governance",
    ),
    "devops": (
        "devops", "dev ops", "site reliability", "sre", "platform engineer", "cloud engineer",
        "aws engineer", "azure engineer", "gcp engineer", "kubernetes", "docker",
        "infrastructure engineer", "systems administrator", "sysadmin", "network engineer",
        "cloud architect", "ci/cd",
    ),
    "security": (
        "cybersecurity", "cyber security", "security engineer", "security analyst",
        "information security", "soc analyst", "penetration tester", "pentester",
        "ethical hacker", "grc", "iam", "siem", "security architect", "appsec",
    ),
    "engineering": (
        "mechanical engineer", "electrical engineer", "civil engineer", "chemical engineer",
        "industrial engineer", "structural engineer", "electronics engineer", "architect",
        "site engineer", "project engineer", "maintenance engineer", "quality engineer",
        "manufacturing engineer", "automotive engineer", "hvac", "instrumentation",
    ),
    "sales": (
        "sales", "sales executive", "sales manager", "business development",
        "account executive", "account manager", "territory manager", "inside sales",
        "field sales", "channel sales", "retail sales", "sales representative",
        "sales officer", "sales associate", "revenue", "pre-sales", "presales",
        "customer success", "partnerships",
    ),
    "marketing": (
        "marketing", "digital marketing", "growth", "seo", "sem", "social media",
        "content marketing", "content creator", "copywriter", "brand", "branding",
        "advertising", "media buyer", "google ads", "meta ads", "campaign",
        "marketing manager", "marketing executive", "public relations", "communications",
        "email marketing", "product marketing",
    ),
    "hr": (
        "human resources", "hr manager", "hr officer", "hr executive", "hr generalist",
        "recruiter", "recruitment", "talent acquisition", "people operations",
        "compensation and benefits", "learning and development", "hr business partner",
        "payroll officer", "organization development",
    ),
    "support": (
        "customer support", "customer service", "customer care", "call center",
        "call centre", "help desk", "helpdesk", "technical support", "support engineer",
        "client support", "customer experience", "complaints", "service desk",
        "customer support representative", "csr",
    ),
    "operations": (
        "operations", "operations manager", "operations executive", "supply chain",
        "logistics", "procurement", "purchasing", "warehouse", "inventory",
        "production planning", "facilities", "fleet", "dispatch", "order management",
        "business operations", "process improvement", "quality assurance",
    ),
    "management": (
        "general manager", "operations director", "country manager", "program manager",
        "project manager", "product manager", "management consultant", "strategy manager",
        "chief of staff", "managing director", "team lead", "head of department",
    ),
    "healthcare": (
        "healthcare", "health care", "medical", "nurse", "nursing", "doctor", "physician",
        "pharmacist", "pharmacy", "clinical", "patient", "hospital", "dentist",
        "radiolog", "therapist", "public health", "medical representative",
    ),
    "education": (
        "teacher", "teaching", "tutor", "lecturer", "professor", "education",
        "academic", "curriculum", "school", "instructor", "trainer", "counselor",
        "student", "principal",
    ),
    "legal": (
        "legal", "lawyer", "advocate", "attorney", "paralegal", "compliance officer",
        "corporate counsel", "litigation", "contracts manager",
    ),
    "design": (
        "designer", "graphic design", "ui designer", "ux designer", "product design",
        "interior design", "visual design", "motion design", "illustrator",
    ),
    "admin": (
        "administrative", "administration", "admin assistant", "executive assistant",
        "office manager", "receptionist", "virtual assistant", "data entry",
        "back office", "secretary",
    ),
}

# Explicit query words that should map to a category even if they are not job
# titles. Longest phrases are checked first.
_QUERY_ALIASES: dict[str, str] = {
    "artificial intelligence": "ai_ml",
    "machine learning": "ai_ml",
    "deep learning": "ai_ml",
    "data science": "data",
    "data scientist": "ai_ml",
    "ai": "ai_ml",
    "genai": "ai_ml",
    "llm": "ai_ml",
    "rag": "ai_ml",
    "mlops": "ai_ml",
    "computer vision": "ai_ml",
    "data analytics": "data",
    "data analyst": "data",
    "data engineer": "data",
    "business analyst": "data",
    "business intelligence": "data",
    "financial analyst": "finance",
    "finance analyst": "finance",
    "software engineering": "software",
    "software engineer": "software",
    "software developer": "software",
    "web development": "software",
    "full stack": "software",
    "cyber security": "security",
    "cybersecurity": "security",
    "information security": "security",
    "devops": "devops",
    "dev ops": "devops",
    "cloud computing": "devops",
    "site reliability": "devops",
    "finance": "finance",
    "financial": "finance",
    "banking": "banking",
    "bank": "banking",
    "accounting": "accounting",
    "accountant": "accounting",
    "marketing": "marketing",
    "digital marketing": "marketing",
    "sales": "sales",
    "business development": "sales",
    "human resources": "hr",
    "human resource": "hr",
    "hr": "hr",
    "recruitment": "hr",
    "recruiter": "hr",
    "customer support": "support",
    "customer service": "support",
    "call center": "support",
    "call centre": "support",
    "operations": "operations",
    "supply chain": "operations",
    "logistics": "operations",
    "management": "management",
    "project management": "management",
    "project manager": "management",
    "product manager": "management",
    "program manager": "management",
    "operations manager": "operations",
    "sales manager": "sales",
    "marketing manager": "marketing",
    "hr manager": "hr",
    "account manager": "sales",
    "office manager": "admin",
    "customer success": "sales",
    "healthcare": "healthcare",
    "health care": "healthcare",
    "nursing": "healthcare",
    "education": "education",
    "teaching": "education",
    "legal": "legal",
    "design": "design",
    "administrative": "admin",
    "administration": "admin",
    "engineering": "engineering",
}

_RELATED: dict[str, tuple[str, ...]] = {
    "software": ("ai_ml", "data", "devops", "security"),
    "ai_ml": ("software", "data"),
    "data": ("ai_ml", "software", "finance"),
    "devops": ("software", "security"),
    "security": ("devops", "software"),
    "engineering": ("operations",),
    "finance": ("accounting", "banking"),
    "banking": ("finance", "accounting"),
    "accounting": ("finance", "banking"),
    "sales": ("marketing",),
    "marketing": ("sales", "design"),
    "hr": ("admin",),
    "support": ("operations",),
    "operations": ("support", "management"),
    "management": ("operations",),
    "healthcare": ("support",),
    "education": ("support",),
    "legal": ("finance",),
    "design": ("marketing", "software"),
    "admin": ("hr", "operations"),
    "other": (),
}

# Role heads that are too generic to classify a title alone but are still
# meaningful search words. "data analyst" must not degrade to just "data".
_QUERY_ROLE_NOUNS = {
    "analyst",
    "engineer",
    "developer",
    "manager",
    "designer",
    "consultant",
    "specialist",
    "officer",
    "assistant",
    "coordinator",
    "architect",
    "administrator",
}

# Text that should not, by itself, decide an occupation.
_GENERIC_TITLE_WORDS = {
    "job", "jobs", "role", "roles", "vacancy", "vacancies", "opening", "openings",
    "position", "positions", "opportunity", "opportunities", "career", "careers",
    "remote", "hybrid", "onsite", "on-site", "work", "from", "home", "full",
    "part", "time", "contract", "internship", "entry", "level", "junior",
    "senior", "lead", "principal", "staff", "mid", "manager", "executive",
    "officer", "assistant", "associate", "specialist", "consultant", "coordinator",
    "intern", "trainee", "head", "director", "engineer", "developer", "analyst",
}

# A single word can appear in a title without defining the occupation:
# "Customer Success Engineer, Audit" is a support/sales role, not accounting.
# Multi-word phrases are specific enough to decide on their own; these words
# only count when nothing stronger is present.
_WEAK_TITLE_WORDS = {
    "tax",
    "audit",
    "accounts",
    "credit",
    "risk",
    "growth",
    "brand",
    "campaign",
    "revenue",
    "partnerships",
    "compliance",
    "training",
    "financial",
    "engineering",
    "architect",
    "support",
    "operations",
    "design",
    "inventory",
    "procurement",
    "purchasing",
    "warehouse",
    "logistics",
    "facilities",
    "quality assurance",
    "communications",
}

# When a title has no occupational keyword, its role head still tells us what
# family it belongs to. "Forward Deployed Engineer" is engineering even when
# the description is about the client's accounting product.
_ROLE_HEADS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("engineering", ("member of technical staff", "technical staff", "engineer")),
    ("software", ("developer", "programmer", "sde")),
    ("design", ("designer",)),
    ("accounting", ("accountant", "bookkeeper", "auditor")),
    ("healthcare", ("nurse", "doctor", "physician", "pharmacist", "dentist")),
    ("education", ("teacher", "tutor", "lecturer", "professor", "instructor")),
    ("hr", ("recruiter",)),
    ("management", ("manager", "director")),
)


_COMPILED: dict[str, tuple[tuple[re.Pattern[str], float], ...]] = {}
for _key, _words in _KEYWORDS.items():
    _entries: list[tuple[re.Pattern[str], float]] = []
    for _word in _words:
        _normalized = normalize_text(_word)
        if not _normalized:
            continue
        if " " in _normalized or "&" in _normalized or "/" in _normalized:
            _weight = 6.0
        elif _normalized in _WEAK_TITLE_WORDS:
            _weight = 2.0
        else:
            _weight = 4.0
        _entries.append(
            (
                re.compile(
                    rf"(?<![a-z0-9]){re.escape(_normalized)}(?![a-z0-9])"
                ),
                _weight,
            )
        )
    _COMPILED[_key] = tuple(_entries)


@dataclass(frozen=True)
class CategoryMatch:
    category: str
    confidence: float
    evidence: str = ""

    @property
    def label(self) -> str:
        return CATEGORY_LABELS.get(self.category, "Other")


def category_label(category: str) -> str:
    return CATEGORY_LABELS.get(category, "Other")


def related_categories(category: str) -> tuple[str, ...]:
    return _RELATED.get(category, ())


def is_related(category: str, requested: str) -> bool:
    if not category or not requested:
        return False
    return category == requested or category in related_categories(requested)


def _role_head(title_text: str) -> str:
    for category, heads in _ROLE_HEADS:
        if any(
            re.search(rf"(?<![a-z0-9]){re.escape(head)}(?![a-z0-9])", title_text)
            for head in heads
        ):
            return category
    return ""


def classify_category(title: str, description: str = "", tags: list[str] | None = None) -> CategoryMatch:
    """Classify a posting from its title, using the body only to break ties.

    Body text describes the employer as much as the role: a job at an accounting
    firm can mention accounting dozens of times while being an engineering role.
    The title is therefore the primary evidence. Descriptions are consulted only
    when two title categories are genuinely close, and cannot invent a category
    that the title never mentioned.
    """
    title_text = normalize_text(title)
    if not title_text:
        return CategoryMatch("other", 0.0)
    tag_text = normalize_text(" ".join(tags or []))
    body_text = normalize_text(description[:5000])

    scores: dict[str, float] = {}
    evidence: dict[str, str] = {}
    for category, patterns in _COMPILED.items():
        score = 0.0
        for pattern, weight in patterns:
            if pattern.search(title_text):
                score += weight
                evidence.setdefault(category, pattern.pattern)
        if score:
            scores[category] = score

    role_category = _role_head(title_text)
    if not scores:
        if role_category:
            return CategoryMatch(role_category, 0.5, "role head")
        return CategoryMatch("other", 0.0)

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    best_category, best_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0

    # A technical role head beats a domain modifier in the title: a "Python
    # Finance Developer" is a developer, not a finance analyst.
    domain_modifiers = {"finance", "accounting", "banking", "healthcare", "legal", "education"}
    if (
        role_category
        and role_category in {"software", "engineering", "design"}
        and best_category in domain_modifiers
        and best_score <= 4.0
    ):
        return CategoryMatch(role_category, 0.5, "role head")

    # Two equally weighted occupations in one title ("Accountant / Social Media
    # Manager") are ambiguous. Prefer an honest "other" over a wrong exact hit.
    if second_score and best_score - second_score <= 1.0 and best_score >= 4.0:
        # Tags/body may break the tie, but only between title-backed categories.
        tied = [category for category, score in ranked if best_score - score <= 1.0]
        body_scores = {
            category: sum(
                len(pattern.findall(body_text)) + len(pattern.findall(tag_text))
                for pattern, _weight in _COMPILED[category]
            )
            for category in tied
        }
        body_best = max(body_scores, key=lambda category: body_scores[category])
        body_values = list(body_scores.values())
        if body_scores[body_best] >= 2 and body_values.count(body_scores[body_best]) == 1:
            best_category = body_best
        else:
            return CategoryMatch("other", 0.3, "ambiguous title")

    confidence = min(1.0, best_score / 8.0)
    if confidence < 0.4:
        # A lone weak word ("accounts", "audit") is not enough to claim an
        # occupation. Keep it uncategorised rather than pollute exact matches.
        return CategoryMatch("other", round(confidence, 2), "weak title evidence")
    return CategoryMatch(best_category, round(confidence, 2), evidence.get(best_category, ""))


def category_from_query(text: str) -> str:
    """Find the explicitly requested occupation in a natural-language query."""
    lowered = normalize_text(text)
    # Longest alias first so "customer support" beats "support".
    for alias in sorted(_QUERY_ALIASES, key=len, reverse=True):
        if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", lowered):
            return _QUERY_ALIASES[alias]
    # Fall back to the job-title classifier, but strip filler words first.
    cleaned = " ".join(
        word for word in re.findall(r"[a-z0-9+#.]+", lowered) if word not in _GENERIC_TITLE_WORDS
    )
    if cleaned:
        match = classify_category(cleaned)
        if match.category != "other" and match.confidence >= 0.35:
            return match.category
    return ""


def _title_tokens(text: str) -> set[str]:
    return {
        word
        for word in re.findall(r"[a-z0-9+#.]+", normalize_text(text))
        if word not in _GENERIC_TITLE_WORDS and len(word) > 1
    }


def title_match(title: str, query_terms: str) -> float:
    """How much of the requested role appears in the job title."""
    wanted = _title_tokens(query_terms)
    if not wanted:
        return 0.5
    have = _title_tokens(title)
    if not have:
        return 0.0
    shared = wanted & have
    if not shared:
        # Allow simple stemming: "banking" ~ "bank", "accounting" ~ "accountant".
        for want in wanted:
            stem = want[:5]
            if len(stem) >= 4 and any(word.startswith(stem) for word in have):
                shared.add(want)
    coverage = len(shared) / len(wanted)
    precision = len(shared) / max(len(have), 1)
    return max(0.0, min(1.0, 0.75 * coverage + 0.25 * precision))


def query_terms(text: str, category: str = "") -> list[str]:
    """Remove filler, location and workplace words from the retrieval query."""
    words = re.findall(r"[A-Za-z0-9+#.]+", text or "")
    kept: list[str] = []
    for word in words:
        lowered = normalize_text(word)
        if not lowered or (
            lowered in _GENERIC_TITLE_WORDS and lowered not in _QUERY_ROLE_NOUNS
        ):
            continue
        if lowered in {"and", "or", "the", "a", "an", "with", "for", "of", "to", "my", "me"}:
            continue
        if any(lowered.startswith(place) for place in ("karachi", "lahore", "islamabad")):
            continue
        kept.append(word)
    if not kept and category:
        aliases = [alias for alias, key in _QUERY_ALIASES.items() if key == category]
        if aliases:
            kept = [max(aliases, key=len)]
    return kept[:8]


__all__ = [
    "CATEGORY_LABELS",
    "CategoryMatch",
    "category_from_query",
    "category_label",
    "classify_category",
    "is_related",
    "query_terms",
    "related_categories",
    "title_match",
]
