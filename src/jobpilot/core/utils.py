"""Small shared helpers used across the codebase."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gh_src",
    "source",
    "ref",
    "referrer",
}


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_now_iso() -> str:
    return utc_now().isoformat(timespec="seconds")


def to_iso(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds")


def parse_datetime(value: Any) -> datetime | None:
    """Best-effort datetime parser for feeds and APIs."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    try:
        from dateutil import parser as date_parser

        parsed = date_parser.parse(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (ValueError, OverflowError, TypeError):
        return None


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def json_loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def stable_hash(*parts: Any, length: int = 32) -> str:
    joined = "\x1f".join(normalize_text(str(part)) for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:length]


def normalize_text(text: str | None) -> str:
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_text = decomposed.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_text).strip().lower()


def slugify(text: str, max_length: int = 60) -> str:
    ascii_text = normalize_text(text)
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    return slug[:max_length].rstrip("-") or "item"


def canonical_url(url: str) -> str:
    """Strip tracking noise so the same posting is not stored twice."""
    if not url or not url.strip():
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def truncate(text: str, limit: int = 600) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
    except Exception:  # pragma: no cover - defensive fallback
        return re.sub(r"<[^>]+>", " ", html)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def money_label(minimum: float | None, maximum: float | None, currency: str | None) -> str | None:
    if minimum is None and maximum is None:
        return None
    currency = currency or "USD"
    symbol = {"USD": "$", "EUR": "€", "GBP": "£", "PKR": "₨", "INR": "₹"}.get(currency, "")
    if minimum is not None and maximum is not None:
        return f"{symbol}{minimum:,.0f} – {symbol}{maximum:,.0f}"
    value = minimum if minimum is not None else maximum
    return f"{symbol}{value:,.0f}"
