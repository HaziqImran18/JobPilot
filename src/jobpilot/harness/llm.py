"""Single gateway for every model call: cache, retries, cost tracking, budget cap."""

from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from jobpilot.core.config import Settings, get_settings
from jobpilot.core.db import Database, get_db
from jobpilot.core.utils import json_dumps, json_loads, utc_now, utc_now_iso
from jobpilot.harness.tracing import RunTrace

T = TypeVar("T", bound=BaseModel)

# USD per million tokens: (input, output). Unknown models are treated as free,
# so add your model here when you switch providers.
_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "o4-mini": (1.10, 4.40),
    "text-embedding-3-small": (0.02, 0.0),
}

_DEFAULT_TTL_HOURS = 24 * 7


class LLMError(RuntimeError):
    pass


class LLMNotConfigured(LLMError):
    pass


class BudgetExceeded(LLMError):
    pass


class LLMResponse(BaseModel):
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    cached: bool = False
    latency_ms: int = 0
    finish_reason: str = ""
    tool_calls: list[dict[str, Any]] = []
    assistant_message: dict[str, Any] = {}


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def cost_for(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    price_in, price_out = _PRICING.get(model, (0.0, 0.0))
    return round((prompt_tokens * price_in + completion_tokens * price_out) / 1_000_000, 6)


class LLMGateway:
    def __init__(self, settings: Settings | None = None, db: Database | None = None) -> None:
        self.settings = settings or get_settings()
        self.db = db or get_db()
        self._client: Any = None
        self._session_spend: dict[str, float] = {}

    @property
    def available(self) -> bool:
        return self.settings.is_live

    # ------------------------------------------------------------------ clients
    def _get_client(self) -> Any:
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=self.settings.openai_api_key,
                base_url=self.settings.openai_base_url or None,
                timeout=self.settings.request_timeout,
                max_retries=0,  # we implement retries with tracing
            )
        return self._client

    # ------------------------------------------------------------------- budget
    def today_spend(self) -> float:
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        row = self.db.query_one(
            "SELECT COALESCE(SUM(cost), 0) AS total FROM llm_usage WHERE created_at >= ?",
            (start.isoformat(timespec="seconds"),),
        )
        return round(float(row["total"]), 6) if row else 0.0

    def today_usage(self) -> tuple[int, int]:
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        row = self.db.query_one(
            "SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens, COUNT(*) AS calls "
            "FROM llm_usage WHERE created_at >= ?",
            (start.isoformat(timespec="seconds"),),
        )
        return (int(row["tokens"]), int(row["calls"])) if row else (0, 0)

    def session_spend(self, session_id: str) -> float:
        return round(self._session_spend.get(session_id, 0.0), 6)

    def budget_status(self, session_id: str = "") -> dict[str, float | int]:
        tokens, calls = self.today_usage()
        return {
            "session_spent": self.session_spend(session_id),
            "daily_spent": self.today_spend(),
            "session_cap": self.settings.max_budget_usd,
            "daily_cap": self.settings.daily_budget_usd,
            "tokens_today": tokens,
            "calls_today": calls,
        }

    def _check_budget(self, session_id: str, estimated: float) -> None:
        if self.today_spend() + estimated > self.settings.daily_budget_usd:
            raise BudgetExceeded(
                f"Daily LLM budget of ${self.settings.daily_budget_usd:.2f} reached. "
                "Raise JOBPILOT_DAILY_BUDGET_USD or continue tomorrow."
            )
        if self.session_spend(session_id) + estimated > self.settings.max_budget_usd:
            raise BudgetExceeded(
                f"Session LLM budget of ${self.settings.max_budget_usd:.2f} reached."
            )

    # -------------------------------------------------------------------- cache
    def _cache_key(
        self,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float,
        max_tokens: int,
        json_mode: bool,
        tools: list[dict[str, Any]] | None = None,
    ) -> str:
        payload = json_dumps(
            {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "json": json_mode,
                "tools": tools or [],
            }
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _cache_get(self, key: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM llm_cache WHERE key = ?", (key,))
        if not row:
            return None
        try:
            if datetime.fromisoformat(row["expires_at"]) < utc_now():
                self.db.execute("DELETE FROM llm_cache WHERE key = ?", (key,))
                return None
        except ValueError:
            return None
        return json_loads(row["response"], None)

    def _cache_put(self, key: str, model: str, response: dict[str, Any], ttl_hours: int) -> None:
        expires = utc_now() + timedelta(hours=ttl_hours)
        self.db.execute(
            "INSERT OR REPLACE INTO llm_cache(key, model, response, prompt_tokens, completion_tokens, cost, "
            "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key,
                model,
                json_dumps(response),
                int(response.get("prompt_tokens", 0)),
                int(response.get("completion_tokens", 0)),
                float(response.get("cost_usd", 0)),
                utc_now_iso(),
                expires.isoformat(timespec="seconds"),
            ),
        )

    # ------------------------------------------------------------------ records
    def _record_usage(
        self,
        *,
        run_id: str,
        session_id: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost: float,
        cached: bool,
        latency_ms: int,
        purpose: str,
        trace: RunTrace | None,
    ) -> None:
        self.db.execute(
            "INSERT INTO llm_usage(run_id, model, prompt_tokens, completion_tokens, cost, cached, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, model, prompt_tokens, completion_tokens, cost, int(cached), utc_now_iso()),
        )
        if not cached:
            self._session_spend[session_id] = self._session_spend.get(session_id, 0.0) + cost
        if trace is not None:
            trace.llm_call(
                model, purpose, prompt_tokens, completion_tokens, cost, latency_ms, cached
            )

    # --------------------------------------------------------------------- call
    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 900,
        json_mode: bool = False,
        tools: list[dict[str, Any]] | None = None,
        cache: bool = False,
        cache_ttl_hours: int = _DEFAULT_TTL_HOURS,
        purpose: str = "general",
        session_id: str = "",
        run_id: str = "",
        trace: RunTrace | None = None,
    ) -> LLMResponse:
        if not self.available:
            raise LLMNotConfigured("OPENAI_API_KEY is not configured.")

        model = model or self.settings.model_fast
        cache_key = self._cache_key(model, messages, temperature, max_tokens, json_mode, tools)
        if cache:
            cached_response = self._cache_get(cache_key)
            if cached_response is not None:
                response = LLMResponse(**cached_response)
                response.cached = True
                response.cost_usd = 0.0
                self._record_usage(
                    run_id=run_id,
                    session_id=session_id,
                    model=model,
                    prompt_tokens=response.prompt_tokens,
                    completion_tokens=response.completion_tokens,
                    cost=0.0,
                    cached=True,
                    latency_ms=0,
                    purpose=purpose,
                    trace=trace,
                )
                return response

        prompt_tokens_estimate = sum(
            estimate_tokens(message.get("content", "")) for message in messages
        )
        price_in, price_out = _PRICING.get(model, (0.0, 0.0))
        estimated_cost = (prompt_tokens_estimate * price_in + max_tokens * price_out) / 1_000_000
        self._check_budget(session_id, estimated_cost)

        client = self._get_client()
        last_error: Exception | None = None
        for attempt in range(self.settings.llm_max_retries):
            started = time.perf_counter()
            try:
                kwargs: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                if json_mode:
                    kwargs["response_format"] = {"type": "json_object"}
                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"
                completion = client.chat.completions.create(**kwargs)
                latency_ms = int((time.perf_counter() - started) * 1000)
                choice = completion.choices[0]
                text = (choice.message.content or "").strip()
                tool_calls = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments or "{}",
                        },
                    }
                    for call in (choice.message.tool_calls or [])
                ]
                assistant_message: dict[str, Any] = {"role": "assistant", "content": text}
                if tool_calls:
                    assistant_message["tool_calls"] = tool_calls
                prompt_tokens = int(
                    completion.usage.prompt_tokens if completion.usage else prompt_tokens_estimate
                )
                completion_tokens = int(
                    completion.usage.completion_tokens
                    if completion.usage
                    else estimate_tokens(text)
                )
                cost = cost_for(model, prompt_tokens, completion_tokens)
                response = LLMResponse(
                    text=text,
                    model=model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cost_usd=cost,
                    cached=False,
                    latency_ms=latency_ms,
                    finish_reason=choice.finish_reason or "",
                    tool_calls=tool_calls,
                    assistant_message=assistant_message,
                )
                self._record_usage(
                    run_id=run_id,
                    session_id=session_id,
                    model=model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cost=cost,
                    cached=False,
                    latency_ms=latency_ms,
                    purpose=purpose,
                    trace=trace,
                )
                if cache:
                    self._cache_put(cache_key, model, response.model_dump(), cache_ttl_hours)
                return response
            except Exception as exc:  # noqa: BLE001 - classified below
                last_error = exc
                if not _is_retryable(exc) or attempt == self.settings.llm_max_retries - 1:
                    break
                time.sleep(min(2**attempt, 8))
        raise LLMError(f"LLM call failed: {last_error}") from last_error

    def complete_json(
        self,
        messages: list[dict[str, str]],
        schema: type[T],
        *,
        model: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 1200,
        cache: bool = True,
        purpose: str = "json",
        session_id: str = "",
        run_id: str = "",
        trace: RunTrace | None = None,
    ) -> tuple[T, LLMResponse]:
        response = self.complete(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=True,
            cache=cache,
            purpose=purpose,
            session_id=session_id,
            run_id=run_id,
            trace=trace,
        )
        try:
            return schema.model_validate(_extract_json(response.text)), response
        except (ValueError, ValidationError) as first_error:
            repair_messages = [
                *messages,
                {"role": "assistant", "content": response.text},
                {
                    "role": "user",
                    "content": (
                        f"Your output was invalid: {first_error}. Return ONLY valid JSON that matches the "
                        f"required shape. JSON schema: {json_dumps(schema.model_json_schema())}"
                    ),
                },
            ]
            repaired = self.complete(
                repair_messages,
                model=model,
                temperature=0.0,
                max_tokens=max_tokens,
                json_mode=True,
                cache=False,
                purpose=f"{purpose}:repair",
                session_id=session_id,
                run_id=run_id,
                trace=trace,
            )
            try:
                return schema.model_validate(_extract_json(repaired.text)), repaired
            except (ValueError, ValidationError) as second_error:
                raise LLMError(
                    f"Model returned invalid JSON twice: {second_error}"
                ) from second_error

    def recent_usage(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT run_id, model, prompt_tokens, completion_tokens, cost, cached, created_at "
            "FROM llm_usage ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in rows]


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object found in model output.")
    parsed = json_loads(text[start : end + 1], None)
    if not isinstance(parsed, dict):
        raise ValueError("Model output is not a JSON object.")
    return parsed


def _is_retryable(exc: Exception) -> bool:
    name = type(exc).__name__
    if name in {"AuthenticationError", "PermissionDeniedError", "BadRequestError", "NotFoundError"}:
        return False
    return name in {
        "RateLimitError",
        "APIConnectionError",
        "APITimeoutError",
        "InternalServerError",
        "APIStatusError",
    }


def get_llm() -> LLMGateway:
    # The gateway stores usage and caches in the active user's database, so a
    # process-wide singleton would leak budget state between accounts.
    return LLMGateway()


__all__ = [
    "BudgetExceeded",
    "LLMError",
    "LLMGateway",
    "LLMNotConfigured",
    "LLMResponse",
    "cost_for",
    "estimate_tokens",
    "get_llm",
]
