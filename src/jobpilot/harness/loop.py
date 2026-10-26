"""The agent loop: plan, call tools, observe, repeat — with hard caps and trace."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import nullcontext, suppress
from typing import Any

from jobpilot.core.config import Settings, get_settings
from jobpilot.core.models import AgentEvent, ToolResult
from jobpilot.core.user_context import (
    clear_current_user,
    current_user_id,
    set_current_user,
    user_context,
)
from jobpilot.core.utils import json_dumps, truncate
from jobpilot.harness.context import ContextManager
from jobpilot.harness.llm import BudgetExceeded, LLMError, LLMGateway, get_llm
from jobpilot.harness.mcp_client import MCPManager, get_mcp_manager
from jobpilot.harness.tool_executor import ToolExecutor
from jobpilot.harness.tracing import TraceStore
from jobpilot.services.chat import ChatService
from jobpilot.services.profile import ProfileService


class AgentRunner:
    def __init__(
        self,
        *,
        user_id: str = "",
        settings: Settings | None = None,
        chat: ChatService | None = None,
        context: ContextManager | None = None,
        llm: LLMGateway | None = None,
        mcp: MCPManager | None = None,
        traces: TraceStore | None = None,
        profiles: ProfileService | None = None,
    ) -> None:
        self.user_id = user_id or current_user_id()
        self.settings = settings or get_settings()

        previous_user = current_user_id()
        if self.user_id:
            set_current_user(self.user_id)
        try:
            self.chat = chat or ChatService()
            self.context = context or ContextManager()
            self.llm = llm or get_llm()
            self.mcp = mcp or get_mcp_manager(self.user_id)
            self.traces = traces or TraceStore()
            self.profiles = profiles or ProfileService()
        finally:
            if previous_user:
                set_current_user(previous_user)
            else:
                clear_current_user()

    # -------------------------------------------------------------------- entry
    def stream(self, session_id: str, message: str) -> Iterator[AgentEvent]:
        context = user_context(self.user_id) if self.user_id else nullcontext()
        with context:
            yield from self._stream(session_id, message)

    def _stream(self, session_id: str, message: str) -> Iterator[AgentEvent]:
        if not self.llm.available:
            yield AgentEvent(
                type="error",
                data={
                    "text": "The AI model key is not configured. Open the Setup page, add OPENAI_API_KEY to .env, and restart the server."
                },
            )
            return

        session = self.chat.ensure_session(session_id)
        self.chat.add_message(session.id, "user", message)
        trace = self.traces.start(session.id)
        executor: ToolExecutor | None = None
        run_meta: dict[str, Any] = {"mode": "live"}
        status = "completed"

        try:
            yield AgentEvent(type="status", data={"text": "Connecting to the MCP tool servers…"})
            ready, error = self._ensure_mcp()
            if not ready:
                final_text = (
                    "The MCP servers did not start, so I can't use any tools right now.\n\n"
                    f"Details: {error}\n\n"
                    "Check the System page or run `jobpilot doctor --mcp`."
                )
                yield AgentEvent(type="error", data={"text": final_text})
            else:
                executor = ToolExecutor(
                    session_id=session.id,
                    run_id=trace.run_id,
                    trace=trace,
                    mcp=self.mcp,
                )
                tools = self.mcp.tools()
                yield AgentEvent(
                    type="status",
                    data={
                        "text": f"{len(tools)} tools discovered across {len(self.mcp.servers)} MCP servers."
                    },
                )
                final_text = yield from self._live(
                    session_id=session.id,
                    message=message,
                    executor=executor,
                    trace=trace,
                    run_meta=run_meta,
                )

            final_text = (final_text or "").strip()
            if not final_text:
                final_text = (
                    "I finished the run without producing an answer. Check the trace for details."
                )
            if not run_meta.get("streamed"):
                yield from self._stream_text(final_text)
            step_count = max(trace.steps, len(executor.tool_log) if executor else 0, 1)
            trace.steps = step_count
            meta = {
                "run_id": trace.run_id,
                "mode": run_meta["mode"],
                "tool_calls": (executor.tool_log[-12:] if executor else []),
                "cards": _unique_cards(executor.cards if executor else [])[:6],
                "cost_usd": round(trace.cost_usd, 6),
                "steps": step_count,
            }
            saved = self.chat.add_message(session.id, "assistant", final_text, meta)
            if executor and executor.tool_log:
                # Compaction is deliberately lazy; it only runs after long sessions.
                with suppress(Exception):
                    self.context.maybe_compact(self.chat, session.id)
            yield AgentEvent(
                type="done",
                data={
                    "session_id": session.id,
                    "message_id": saved.id,
                    "run_id": trace.run_id,
                    "mode": run_meta["mode"],
                    "steps": step_count,
                    "cost_usd": round(trace.cost_usd, 6),
                    "cards": meta["cards"],
                    "budget": self.llm.budget_status(session.id),
                    "message": final_text,
                },
            )
        except Exception as exc:  # noqa: BLE001 - never break the SSE stream without a final event
            status = "error"
            trace.event("error", message=str(exc)[:500])
            text = f"The run failed: {exc}"
            yield AgentEvent(type="error", data={"text": text})
            with suppress(Exception):
                self.chat.add_message(
                    session.id, "assistant", text, {"run_id": trace.run_id, "mode": "error"}
                )
        finally:
            trace.finish(status)

    # --------------------------------------------------------------------- live
    def _live(
        self,
        *,
        session_id: str,
        message: str,
        executor: ToolExecutor,
        trace,
        run_meta: dict[str, Any],
    ) -> Iterator[AgentEvent]:
        profile = self.profiles.get()
        history = self.chat.get_messages(session_id)
        summary, summary_count = self.chat.summary_state(session_id)
        bundle = self.context.build(
            user_message=message,
            profile=profile,
            history=history,
            summary=summary,
            summary_count=summary_count,
            session_id=session_id,
        )
        yield AgentEvent(
            type="status",
            data={
                "text": (
                    f"Context assembled: ~{bundle.estimated_tokens} tokens, "
                    f"{len(bundle.memories)} recalled memories."
                )
            },
        )

        tool_schemas = [_tool_schema(spec) for spec in self.mcp.tools()]
        if not tool_schemas:
            return (
                "No MCP tools were discovered, so I can't take actions. "
                "Check the System page for server errors."
            )

        messages: list[dict[str, Any]] = list(bundle.messages)
        consecutive_failures = 0
        for step in range(self.settings.max_steps_per_run):
            trace.step(step)
            yield AgentEvent(
                type="status",
                data={"text": f"Step {step + 1}/{self.settings.max_steps_per_run}: planning…"},
            )
            try:
                response = self.llm.complete(
                    messages,
                    model=self.settings.model_fast,
                    temperature=0.2,
                    max_tokens=1000,
                    tools=tool_schemas,
                    purpose="agent_step",
                    session_id=session_id,
                    run_id=trace.run_id,
                    trace=trace,
                )
            except (LLMError, BudgetExceeded) as exc:
                # Production fails loudly instead of quietly faking an answer.
                raise LLMError(f"The live model call failed: {exc}") from exc

            if response.tool_calls:
                messages.append(
                    response.assistant_message
                    or {
                        "role": "assistant",
                        "content": response.text,
                        "tool_calls": response.tool_calls,
                    }
                )
                for call in response.tool_calls:
                    name = call.get("function", {}).get("name", "")
                    raw_arguments = call.get("function", {}).get("arguments", "{}")
                    try:
                        arguments = json.loads(raw_arguments) if raw_arguments else {}
                        if not isinstance(arguments, dict):
                            raise ValueError("arguments must be an object")
                    except (json.JSONDecodeError, ValueError) as exc:
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.get("id", ""),
                                "content": f"Invalid arguments: {exc}",
                            }
                        )
                        consecutive_failures += 1
                        continue

                    result = yield from executor.invoke(name, arguments)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id", ""),
                            "content": _tool_content(
                                name, result, self.settings.max_tool_output_chars
                            ),
                        }
                    )
                    if result.ok:
                        consecutive_failures = 0
                    else:
                        consecutive_failures += 1
                if consecutive_failures >= self.settings.max_consecutive_failures:
                    return (
                        "I stopped after repeated tool failures. "
                        "The trace has the details; try a narrower request or check the System page."
                    )
                continue

            final_text = response.text.strip()
            if final_text:
                run_meta["streamed"] = True
                yield from self._stream_text(final_text)
                return final_text

            messages.append(
                {
                    "role": "user",
                    "content": "Please answer the original request now using what you have. Do not call more tools.",
                }
            )

        return (
            f"I reached the {self.settings.max_steps_per_run}-step limit before finishing. "
            "Try a narrower request, or raise JOBPILOT_MAX_STEPS_PER_RUN."
        )

    @staticmethod
    def _stream_text(text: str, chunk_size: int = 48) -> Iterator[AgentEvent]:
        for start in range(0, len(text), chunk_size):
            yield AgentEvent(type="token", data={"text": text[start : start + chunk_size]})

    # ------------------------------------------------------------------ helpers
    def _ensure_mcp(self) -> tuple[bool, str]:
        try:
            self.mcp.start()
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        if not self.mcp.tools():
            errors = "; ".join(f"{name}: {detail}" for name, detail in self.mcp.errors.items())
            return False, errors or "No tools discovered."
        return True, ""


def _tool_schema(spec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": truncate(spec.description, 900),
            "parameters": spec.input_schema or {"type": "object", "properties": {}},
        },
    }


def _tool_content(name: str, result: ToolResult, limit: int) -> str:
    if not result.ok:
        return truncate(result.error or "Tool failed.", limit)
    data = result.data
    if name in {"search_jobs", "rank_jobs"} and isinstance(data, dict):
        items = data.get("ranked") or data.get("jobs") or []
        compact = []
        for item in items[:10]:
            if isinstance(item, dict) and "job" in item:
                job = item["job"]
                match = item.get("match", {})
                compact.append(
                    {
                        "id": job.get("id"),
                        "title": job.get("title"),
                        "company": job.get("company"),
                        "location": job.get("location"),
                        "remote": job.get("remote"),
                        "salary": job.get("salary"),
                        "score": match.get("score"),
                        "matched_skills": match.get("matched_skills"),
                        "missing_skills": match.get("missing_skills"),
                    }
                )
            elif isinstance(item, dict):
                compact.append(
                    {
                        "id": item.get("id"),
                        "title": item.get("title"),
                        "company": item.get("company"),
                        "location": item.get("location"),
                        "remote": item.get("remote"),
                        "salary": item.get("salary_label") or item.get("salary"),
                        "score": item.get("score"),
                    }
                )
        return json_dumps({"jobs": compact, "errors": data.get("errors", {})})
    if name == "get_job" and isinstance(data, dict):
        return json_dumps(
            {
                "id": data.get("id"),
                "title": data.get("title"),
                "company": data.get("company"),
                "location": data.get("location"),
                "description": truncate(str(data.get("description", "")), limit // 2),
                "injection_warning": data.get("injection_warning"),
            }
        )
    if name == "tailor_cv" and isinstance(data, dict):
        return json_dumps(
            {
                "job_id": data.get("job_id"),
                "job_title": data.get("job_title"),
                "company": data.get("company"),
                "headline": data.get("headline"),
                "summary": data.get("summary"),
                "bullets": data.get("bullets"),
                "cover_letter": truncate(str(data.get("cover_letter", "")), 2000),
                "verified": data.get("verified"),
                "unsupported": data.get("unsupported"),
                "cv_path": data.get("cv_path"),
            }
        )
    text = json_dumps(data) if data is not None else result.content
    return truncate(text, limit)


def _unique_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for card in cards:
        job_id = str(card.get("id", ""))
        if not job_id or job_id in seen:
            continue
        seen.add(job_id)
        unique.append(card)
    return unique


__all__ = ["AgentRunner"]
