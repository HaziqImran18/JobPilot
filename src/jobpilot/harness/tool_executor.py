"""Shared tool execution with permission checks, approvals, tracing and events."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from jobpilot.core.models import AgentEvent, PermissionTier, ToolResult
from jobpilot.core.utils import truncate
from jobpilot.harness.mcp_client import MCPManager, get_mcp_manager
from jobpilot.harness.permissions import ApprovalBroker, PermissionManager, get_broker
from jobpilot.harness.tracing import RunTrace


class ToolExecutor:
    """Executes MCP tools and yields UI events as it goes.

    External-tier tools pause on an approval future. The Flask SSE stream stays
    open while the user clicks approve or deny in another request.
    """

    def __init__(
        self,
        *,
        session_id: str,
        run_id: str,
        trace: RunTrace,
        mcp: MCPManager | None = None,
        broker: ApprovalBroker | None = None,
        permissions: PermissionManager | None = None,
    ) -> None:
        self.session_id = session_id
        self.run_id = run_id
        self.trace = trace
        self.mcp = mcp or get_mcp_manager()
        self.broker = broker or get_broker()
        self.permissions = permissions or PermissionManager()
        self.tool_log: list[dict[str, Any]] = []
        self.cards: list[dict[str, Any]] = []

    def invoke(self, name: str, arguments: dict[str, Any]) -> Iterator[AgentEvent]:
        spec = self.mcp.tool(name)
        tier = spec.tier if spec else self.permissions.tier_for(name)
        yield AgentEvent(
            type="tool_call",
            data={
                "name": name,
                "arguments": arguments,
                "tier": tier.value,
                "server": spec.server if spec else "",
                "requires_approval": tier is PermissionTier.external,
            },
        )
        self.trace.tool_call(name, arguments, tier.value)

        if spec is None:
            result = ToolResult(
                call_id="",
                name=name,
                ok=False,
                error=f"Unknown tool `{name}`. The MCP server may have failed to start.",
                tier=PermissionTier.external,
            )
            yield self._result_event(result)
            return result

        approved = False
        if tier is PermissionTier.external:
            approval = self.broker.create(
                run_id=self.run_id,
                session_id=self.session_id,
                tool=name,
                arguments=arguments,
                reason=f"`{name}` is an external action and needs your explicit approval.",
            )
            yield AgentEvent(
                type="approval_required",
                data=approval.model_dump(mode="json"),
            )
            approved = self.broker.wait(approval.id)
            if not approved:
                result = ToolResult(
                    call_id="",
                    name=name,
                    ok=False,
                    error="Action denied or timed out. Nothing left the machine.",
                    tier=tier,
                )
                self.trace.tool_result(name, False, 0, "denied by user")
                self.tool_log.append(
                    {
                        "name": name,
                        "arguments": arguments,
                        "ok": False,
                        "error": "denied",
                        "tier": tier.value,
                    }
                )
                yield self._result_event(result)
                return result

        result = self.mcp.call(name, arguments)
        self.trace.tool_result(name, result.ok, result.duration_ms, result.content)
        self.tool_log.append(
            {
                "name": name,
                "arguments": arguments,
                "ok": result.ok,
                "duration_ms": result.duration_ms,
                "tier": tier.value,
                "summary": self._summary(name, result),
            }
        )
        self._collect_cards(result)
        yield self._result_event(result)

        if name == "remember" and isinstance(result.data, dict):
            memory = result.data.get("memory") or {}
            if result.data.get("requires_confirmation"):
                yield AgentEvent(
                    type="memory_proposal",
                    data={
                        "memory": memory,
                        "reason": result.data.get("policy_reason", ""),
                    },
                )
        return result

    def _result_event(self, result: ToolResult) -> AgentEvent:
        return AgentEvent(
            type="tool_result",
            data={
                "name": result.name,
                "ok": result.ok,
                "duration_ms": result.duration_ms,
                "tier": result.tier.value,
                "error": result.error,
                "summary": self._summary(result.name, result),
                "preview": truncate(result.content, 1500),
                "cards": self._extract_cards(result.data)[:8],
            },
        )

    def _collect_cards(self, result: ToolResult) -> None:
        self.cards.extend(self._extract_cards(result.data))

    @staticmethod
    def _extract_cards(data: Any) -> list[dict[str, Any]]:
        if not isinstance(data, dict):
            return []
        raw_items: list[dict[str, Any]] = []
        if isinstance(data.get("ranked"), list):
            for item in data["ranked"]:
                if isinstance(item, dict) and isinstance(item.get("job"), dict):
                    card = dict(item["job"])
                    match = item.get("match") or {}
                    card["score"] = match.get("score", card.get("score"))
                    card["matched_skills"] = match.get("matched_skills", [])
                    card["missing_skills"] = match.get("missing_skills", [])
                    raw_items.append(card)
        elif isinstance(data.get("jobs"), list):
            raw_items = [item for item in data["jobs"] if isinstance(item, dict)]
        elif isinstance(data.get("job"), dict):
            raw_items = [data["job"]]
        return [item for item in raw_items if item.get("id") and item.get("title")]

    @staticmethod
    def _summary(name: str, result: ToolResult) -> str:
        if not result.ok:
            return result.error or "Tool failed."
        data = result.data
        if isinstance(data, dict):
            if name in {"rank_jobs", "search_jobs"}:
                items = data.get("ranked") or data.get("jobs") or []
                return f"{len(items)} jobs returned."
            if name == "get_job":
                return f"Loaded {data.get('title', 'job')} at {data.get('company', '')}."
            if name == "tailor_cv":
                return (
                    f"Tailored CV for {data.get('job_title', 'job')} at {data.get('company', '')}; "
                    f"verified={data.get('verified')}, rejected={len(data.get('unsupported') or [])}."
                )
            if name == "remember":
                memory = data.get("memory") or {}
                return (
                    "Memory proposed for confirmation."
                    if data.get("requires_confirmation")
                    else f"Memory stored: {memory.get('text', '')[:120]}"
                )
            if name == "add_application":
                application = data.get("application") or {}
                return f"Tracked {application.get('job_title', 'application')} as {application.get('status', 'saved')}."
            if name == "list_applications":
                count = data.get("count", len(data.get("applications") or []))
                return f"{count} application{'s' if count != 1 else ''} on the board."
            if name == "mark_submitted":
                application = data.get("application") or {}
                return f"Marked {application.get('job_title', 'application')} as {application.get('status', 'applied')} after approval."
            if name == "update_status":
                application = data.get("application") or {}
                return f"Moved {application.get('job_title', 'application')} to {application.get('status', '')}."
            if name == "list_followups":
                count = data.get("count", len(data.get("followups") or []))
                return f"{count} follow-up{'s' if count != 1 else ''} due."
            if name == "score_job":
                return f"Fit score {data.get('score')}/100."
            if "error" in data and data.get("error"):
                return str(data["error"])[:200]
        if isinstance(data, list):
            return f"{len(data)} items returned."
        return truncate(result.content, 240)


__all__ = ["ToolExecutor"]
