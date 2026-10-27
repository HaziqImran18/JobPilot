"""Turn a natural-language request or board filters into structured search intent."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jobpilot.core.utils import normalize_text, stable_hash
from jobpilot.pipelines.categories import (
    category_from_query,
    category_label,
    query_terms,
)
from jobpilot.pipelines.locations import ResolvedLocation, resolve_location

_SENIORITY = (
    ("intern", re.compile(r"\b(?:intern|internship|trainee)\b", re.I)),
    ("entry", re.compile(r"\b(?:entry[- ]level|junior|graduate|fresher|associate)\b", re.I)),
    ("lead", re.compile(r"\b(?:lead|principal|staff|head|director|vp|chief)\b", re.I)),
    ("senior", re.compile(r"\bsenior\b", re.I)),
    ("mid", re.compile(r"\b(?:mid[- ]level|intermediate)\b", re.I)),
)
_WORKPLACE = (
    ("remote", re.compile(r"\b(?:remote|work from home|wfh|telecommute)\b", re.I)),
    ("hybrid", re.compile(r"\bhybrid\b", re.I)),
    ("onsite", re.compile(r"\b(?:on-?site|in[- ]office|office[- ]based)\b", re.I)),
)
_EMPLOYMENT = (
    ("internship", re.compile(r"\binternship\b", re.I)),
    ("part-time", re.compile(r"\bpart[- ]time\b", re.I)),
    ("full-time", re.compile(r"\bfull[- ]time\b", re.I)),
    ("contract", re.compile(r"\bcontract(?:or)?\b", re.I)),
    ("freelance", re.compile(r"\bfreelance\b", re.I)),
)
_LOCATION_PHRASE = re.compile(
    r"\b(?:in|near|around|based in|located in|within|from)\s+"
    r"([A-Z][A-Za-z .'-]{1,40}|[a-z][a-z .'-]{2,40})",
)


@dataclass
class SearchIntent:
    query: str = ""
    search_query: str = ""
    location: str = ""
    resolved_location: ResolvedLocation = field(default_factory=ResolvedLocation)
    category: str = ""
    seniority: str = ""
    workplace_type: str = ""
    employment_type: str = ""
    posted_within_days: int | None = None
    sources: list[str] = field(default_factory=list)
    include_related: bool = True
    applied_filters: dict[str, str] = field(default_factory=dict)

    @property
    def category_label(self) -> str:
        return category_label(self.category) if self.category else ""

    @property
    def signature(self) -> str:
        return stable_hash(
            normalize_text(self.search_query),
            normalize_text(self.location),
            self.category,
            self.seniority,
            self.workplace_type,
            self.employment_type,
            str(self.posted_within_days or ""),
            ",".join(sorted(self.sources)),
            length=20,
        )

    def as_dict(self) -> dict[str, object]:
        display = (
            self.resolved_location.display
            if (self.resolved_location.known or self.resolved_location.remote_only)
            else ""
        )
        payload: dict[str, object] = {
            "query": self.search_query or self.query,
            "location": display or self.location,
            "category": self.category,
            "category_label": self.category_label,
            "seniority": self.seniority,
            "workplace_type": self.workplace_type,
            "employment_type": self.employment_type,
            "include_related": self.include_related,
        }
        if self.posted_within_days:
            payload["posted_within_days"] = self.posted_within_days
        if self.sources:
            payload["sources"] = self.sources
        if self.resolved_location.known:
            payload["resolved_location"] = self.resolved_location.as_dict()
        return {
            key: value
            for key, value in payload.items()
            if value not in ("", None) and value != []
        }


def _find_seniority(text: str) -> str:
    for label, pattern in _SENIORITY:
        if pattern.search(text):
            return label
    return ""


def _find_workplace(text: str) -> str:
    for label, pattern in _WORKPLACE:
        if pattern.search(text):
            return label
    return ""


def _find_employment(text: str) -> str:
    for label, pattern in _EMPLOYMENT:
        if pattern.search(text):
            return label
    return ""


def _find_location(text: str, explicit: str = "") -> tuple[ResolvedLocation, str]:
    if explicit.strip():
        return resolve_location(explicit), explicit.strip()
    # Prefer an explicit "in <place>" phrase; fall back to scanning the text for
    # a known city/country. This avoids treating "remote" as the only location
    # when the user also said "in Toronto".
    for match in _LOCATION_PHRASE.finditer(text):
        candidate = match.group(1).strip()
        resolved = resolve_location(candidate)
        if resolved.known:
            return resolved, candidate
    resolved = resolve_location(text)
    if resolved.known:
        return resolved, resolved.display
    if resolved.remote_only:
        return resolved, "Remote"
    return resolved, ""


def _strip_location(text: str, resolved: ResolvedLocation, raw: str) -> str:
    cleaned = text
    phrases = [raw]
    if resolved.known or resolved.remote_only:
        phrases.extend([resolved.city, resolved.region, resolved.country, resolved.display])
    for phrase in phrases:
        if phrase and len(phrase) > 1:
            cleaned = re.sub(rf"(?<![A-Za-z0-9]){re.escape(phrase)}(?![A-Za-z0-9])", " ", cleaned, flags=re.I)
    return cleaned


def interpret_query(
    *,
    query: str = "",
    location: str = "",
    remote: bool | None = None,
    category: str = "",
    seniority: str = "",
    workplace_type: str = "",
    employment_type: str = "",
    posted_within_days: int | None = None,
    sources: list[str] | None = None,
    include_related: bool = True,
) -> SearchIntent:
    raw_query = (query or "").strip()
    text = " ".join(part for part in (raw_query, location or "") if part)
    resolved, raw_location = _find_location(raw_query, location)
    detected_category = category.strip().lower() or category_from_query(text)
    detected_seniority = (seniority or _find_seniority(text)).lower()
    detected_workplace = workplace_type.strip().lower() or _find_workplace(text)
    if remote is True:
        detected_workplace = "remote"
    elif remote is False and not detected_workplace:
        detected_workplace = "onsite"
    detected_employment = (employment_type or _find_employment(text)).lower()

    # The retrieval query should describe the occupation, not the location or
    # the words "jobs in". Keep the user's phrase when no category is obvious.
    without_place = _strip_location(raw_query, resolved, raw_location)
    terms = query_terms(without_place, detected_category)
    search_query = " ".join(terms)
    if not search_query and detected_category:
        search_query = category_label(detected_category)

    applied = {
        "query": search_query,
        "location": (
            resolved.display
            if (resolved.known or resolved.remote_only)
            else raw_location
        ),
        "category": category_label(detected_category) if detected_category else "",
        "seniority": detected_seniority,
        "workplace": detected_workplace,
        "employment": detected_employment,
        "posted": f"last {posted_within_days} days" if posted_within_days else "",
    }
    return SearchIntent(
        query=raw_query,
        search_query=search_query,
        location=raw_location or (resolved.display if resolved.known else ""),
        resolved_location=resolved,
        category=detected_category,
        seniority=detected_seniority,
        workplace_type=detected_workplace,
        employment_type=detected_employment,
        posted_within_days=posted_within_days,
        sources=[source.lower() for source in (sources or []) if source],
        include_related=include_related,
        applied_filters={key: value for key, value in applied.items() if value},
    )


__all__ = ["SearchIntent", "interpret_query"]
