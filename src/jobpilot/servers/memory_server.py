"""MCP server: persistent semantic and episodic memory."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from jobpilot.core.models import Memory, MemoryKind, MemoryStatus
from jobpilot.memory.policy import MemoryPolicy
from jobpilot.memory.store import MemoryStore
from jobpilot.servers.common import ensure_ready, jsonable
from jobpilot.services.profile import ProfileService

mcp = FastMCP("memory", log_level="WARNING")
ensure_ready()


@mcp.tool()
def remember(
    text: str,
    kind: str = "semantic",
    key: str = "",
    value: str = "",
    importance: float = 0.6,
    confidence: float = 0.8,
    explicit: bool = False,
) -> dict:
    """Propose a durable memory. The harness decides whether to store it immediately
    or park it for user confirmation. Use kind='episodic' for what happened
    (rejections, feedback); use kind='semantic' for durable preferences."""
    profile = ProfileService().get()
    proposal = MemoryPolicy().evaluate(
        text=text,
        key=key,
        value=value,
        kind=MemoryKind(kind),
        importance=importance,
        confidence=confidence,
        explicit=explicit,
        profile=profile,
    )
    status = (
        MemoryStatus.needs_confirmation if proposal.requires_confirmation else MemoryStatus.active
    )
    memory, action = MemoryStore().add(
        proposal.text,
        kind=proposal.kind,
        key=proposal.key,
        value=proposal.value,
        importance=proposal.importance,
        confidence=proposal.confidence,
        provenance={"source": "agent", "explicit": explicit},
        status=status,
    )
    return {
        "memory": jsonable(memory),
        "action": action,
        "requires_confirmation": memory.status is MemoryStatus.needs_confirmation,
        "policy_reason": proposal.reason,
    }


@mcp.tool()
def recall(query: str, k: int = 5, kind: str = "") -> dict:
    """Recall the most relevant active memories using embedding similarity, recency
    and importance. Omit kind to search both semantic and episodic memories."""
    memory_kind = MemoryKind(kind) if kind in {"semantic", "episodic"} else None
    memories = MemoryStore().recall(query, k=max(1, min(k, 20)), kind=memory_kind)
    return {"memories": jsonable(memories)}


@mcp.tool()
def list_memories(status: str = "active", kind: str = "", limit: int = 50) -> dict:
    """List stored memories. Status can be active, needs_confirmation, forgotten or
    empty for all. This is how the user audits what the system remembers."""
    status_value = MemoryStatus(status) if status in {item.value for item in MemoryStatus} else None
    kind_value = MemoryKind(kind) if kind in {"semantic", "episodic"} else None
    memories = MemoryStore().list(
        status=status_value, kind=kind_value, limit=max(1, min(limit, 200))
    )
    return {"memories": jsonable(memories)}


@mcp.tool()
def forget(memory_id: str) -> dict:
    """Forget a memory permanently. It stops appearing in recall immediately."""
    ok = MemoryStore().forget(memory_id)
    return {"ok": ok, "memory_id": memory_id}


@mcp.tool()
def confirm_memory(memory_id: str) -> dict:
    """Approve a memory that was parked for confirmation."""
    memory = MemoryStore().confirm(memory_id)
    return {"memory": jsonable(memory) if isinstance(memory, Memory) else None}


if __name__ == "__main__":
    mcp.run(transport="stdio")
