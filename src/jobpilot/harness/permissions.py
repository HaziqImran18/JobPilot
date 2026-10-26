"""Tool permission tiers and the human-approval gate for external actions."""

from __future__ import annotations

import threading
import uuid
from typing import Any

from jobpilot.core.db import Database, get_db
from jobpilot.core.models import ApprovalRequest, PermissionTier
from jobpilot.core.utils import json_dumps, json_loads, utc_now_iso

# The tier is declared by the harness, not by the model or the tool's own
# description. Unknown tools fail closed (external).
TOOL_TIERS: dict[str, PermissionTier] = {
    # jobs
    "search_jobs": PermissionTier.read,
    "get_job": PermissionTier.read,
    "refresh_jobs": PermissionTier.read,
    "add_job_from_url": PermissionTier.read,
    # profile (read-only to the agent)
    "get_profile": PermissionTier.read,
    "search_experience": PermissionTier.read,
    "list_skills": PermissionTier.read,
    # memory
    "remember": PermissionTier.write_local,
    "recall": PermissionTier.read,
    "forget": PermissionTier.write_local,
    "list_memories": PermissionTier.read,
    "confirm_memory": PermissionTier.write_local,
    # match
    "score_job": PermissionTier.read,
    "skill_gap": PermissionTier.read,
    "rank_jobs": PermissionTier.read,
    # tailor
    "tailor_cv": PermissionTier.write_local,
    "draft_cover_letter": PermissionTier.write_local,
    # tracker
    "add_application": PermissionTier.write_local,
    "update_status": PermissionTier.write_local,
    "list_followups": PermissionTier.read,
    "list_applications": PermissionTier.read,
    "mark_submitted": PermissionTier.external,
}


class PermissionDenied(RuntimeError):
    pass


class PermissionManager:
    def tier_for(self, tool_name: str) -> PermissionTier:
        return TOOL_TIERS.get(tool_name, PermissionTier.external)

    def requires_approval(self, tool_name: str) -> bool:
        return self.tier_for(tool_name) is PermissionTier.external

    def authorize(self, tool_name: str, *, approved: bool = False) -> None:
        if self.requires_approval(tool_name) and not approved:
            raise PermissionDenied(
                f"`{tool_name}` performs an external action and needs explicit approval."
            )


class ApprovalBroker:
    """Bridges the blocking agent loop and the UI's approve/deny endpoint.

    Approvals are also persisted, so the UI can show pending items after a
    reload. A waiter only exists in memory; stale rows expire on startup.
    """

    def __init__(self, db: Database | None = None, timeout_seconds: int = 180) -> None:
        self._db_override = db
        self.timeout_seconds = timeout_seconds
        self._waiters: dict[str, threading.Event] = {}
        self._decisions: dict[str, bool] = {}
        self._lock = threading.Lock()

    @property
    def db(self) -> Database:
        """Resolve the active account's database at call time.

        The broker itself is process-wide because it coordinates in-memory
        waiters. Persistence must still follow the current user context.
        """
        return self._db_override or get_db()

    def create(
        self,
        *,
        run_id: str,
        session_id: str,
        tool: str,
        arguments: dict[str, Any],
        reason: str = "",
    ) -> ApprovalRequest:
        request = ApprovalRequest(
            id=f"apr_{uuid.uuid4().hex[:12]}",
            run_id=run_id,
            session_id=session_id,
            tool=tool,
            arguments=arguments,
            reason=reason or "This action leaves the local machine and needs your approval.",
            created_at=utc_now_iso(),
        )
        self.db.execute(
            "INSERT INTO approvals(id, run_id, session_id, tool, arguments, reason, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
            (
                request.id,
                request.run_id,
                request.session_id,
                request.tool,
                json_dumps(request.arguments),
                request.reason,
                request.created_at,
            ),
        )
        with self._lock:
            self._waiters[request.id] = threading.Event()
        return request

    def wait(self, approval_id: str, timeout: int | None = None) -> bool:
        with self._lock:
            event = self._waiters.get(approval_id)
        if event is None:
            return False
        resolved = event.wait(timeout or self.timeout_seconds)
        with self._lock:
            decision = self._decisions.get(approval_id, False)
            self._waiters.pop(approval_id, None)
            self._decisions.pop(approval_id, None)
        if not resolved:
            self.db.execute(
                "UPDATE approvals SET status = 'expired', decided_at = ? WHERE id = ? AND status = 'pending'",
                (utc_now_iso(), approval_id),
            )
            return False
        return decision

    def resolve(self, approval_id: str, approved: bool) -> ApprovalRequest | None:
        row = self.db.query_one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        if not row or row["status"] != "pending":
            return None
        self.db.execute(
            "UPDATE approvals SET status = ?, decided_at = ? WHERE id = ?",
            ("approved" if approved else "denied", utc_now_iso(), approval_id),
        )
        with self._lock:
            self._decisions[approval_id] = approved
            event = self._waiters.get(approval_id)
            if event is not None:
                event.set()
        return self.get(approval_id)

    def get(self, approval_id: str) -> ApprovalRequest | None:
        row = self.db.query_one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        if not row:
            return None
        return ApprovalRequest(
            id=row["id"],
            run_id=row["run_id"],
            session_id=row["session_id"],
            tool=row["tool"],
            arguments=json_loads(row["arguments"], {}) or {},
            reason=row["reason"],
            status=row["status"],
            created_at=row["created_at"],
            decided_at=row["decided_at"],
        )

    def pending(self, session_id: str = "") -> list[ApprovalRequest]:
        if session_id:
            rows = self.db.query(
                "SELECT id FROM approvals WHERE status = 'pending' AND session_id = ? ORDER BY created_at DESC",
                (session_id,),
            )
        else:
            rows = self.db.query(
                "SELECT id FROM approvals WHERE status = 'pending' ORDER BY created_at DESC"
            )
        return [request for row in rows if (request := self.get(row["id"]))]

    def expire_stale(self) -> None:
        self.db.execute(
            "UPDATE approvals SET status = 'expired', decided_at = ? WHERE status = 'pending' "
            "AND created_at < datetime('now', '-30 minutes')",
            (utc_now_iso(),),
        )


_broker: ApprovalBroker | None = None
_broker_lock = threading.Lock()


def get_broker() -> ApprovalBroker:
    global _broker
    with _broker_lock:
        if _broker is None:
            _broker = ApprovalBroker()
        return _broker


def get_permissions() -> PermissionManager:
    return PermissionManager()
