"""Conversation sessions and message persistence."""

from __future__ import annotations

import uuid
from typing import Any

from jobpilot.core.db import Database, get_db
from jobpilot.core.models import ChatMessage, ChatSession
from jobpilot.core.utils import json_dumps, json_loads, truncate, utc_now_iso


class ChatService:
    def __init__(self, db: Database | None = None) -> None:
        self.db = db or get_db()

    # ------------------------------------------------------------------ sessions
    def create_session(self, title: str = "New conversation") -> ChatSession:
        session = ChatSession(
            id=f"ses_{uuid.uuid4().hex[:12]}",
            title=title,
            created_at=utc_now_iso(),
            updated_at=utc_now_iso(),
        )
        self.db.execute(
            "INSERT INTO chat_sessions(id, title, rolling_summary, pinned, created_at, updated_at) "
            "VALUES (?, ?, '', '[]', ?, ?)",
            (session.id, session.title, session.created_at, session.updated_at),
        )
        return session

    def get_session(self, session_id: str) -> ChatSession | None:
        row = self.db.query_one("SELECT * FROM chat_sessions WHERE id = ?", (session_id,))
        if not row:
            return None
        return ChatSession(
            id=row["id"],
            title=row["title"],
            rolling_summary=row["rolling_summary"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def ensure_session(self, session_id: str | None) -> ChatSession:
        if session_id:
            session = self.get_session(session_id)
            if session:
                return session
        return self.create_session()

    def list_sessions(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT s.*, "
            "(SELECT COUNT(*) FROM chat_messages m WHERE m.session_id = s.id) AS message_count, "
            "(SELECT content FROM chat_messages m WHERE m.session_id = s.id ORDER BY m.created_at DESC LIMIT 1) AS last_message "
            "FROM chat_sessions s ORDER BY s.updated_at DESC LIMIT ?",
            (limit,),
        )
        return [
            {
                "id": row["id"],
                "title": row["title"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "rolling_summary": row["rolling_summary"],
                "message_count": int(row["message_count"]),
                "preview": truncate(row["last_message"] or "", 90),
            }
            for row in rows
        ]

    def rename_session(self, session_id: str, title: str) -> None:
        self.db.execute(
            "UPDATE chat_sessions SET title = ?, updated_at = ? WHERE id = ?",
            (truncate(title.strip(), 70) or "New conversation", utc_now_iso(), session_id),
        )

    def delete_session(self, session_id: str) -> None:
        self.db.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
        self.db.execute("DELETE FROM kv WHERE key = ?", (f"summary_count:{session_id}",))

    # ------------------------------------------------------------------ messages
    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        meta: dict[str, Any] | None = None,
    ) -> ChatMessage:
        message = ChatMessage(
            id=f"msg_{uuid.uuid4().hex[:12]}",
            session_id=session_id,
            role=role,  # type: ignore[arg-type]
            content=content,
            meta=meta or {},
            created_at=utc_now_iso(),
        )
        self.db.execute(
            "INSERT INTO chat_messages(id, session_id, role, content, meta, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (message.id, session_id, role, content, json_dumps(message.meta), message.created_at),
        )
        self.db.execute(
            "UPDATE chat_sessions SET updated_at = ? WHERE id = ?", (message.created_at, session_id)
        )
        session = self.get_session(session_id)
        if session and session.title == "New conversation" and role == "user":
            self.rename_session(session_id, content)
        return message

    def get_messages(self, session_id: str, limit: int = 200) -> list[ChatMessage]:
        rows = self.db.query(
            "SELECT * FROM chat_messages WHERE session_id = ? ORDER BY created_at ASC, rowid ASC LIMIT ?",
            (session_id, limit),
        )
        return [
            ChatMessage(
                id=row["id"],
                session_id=row["session_id"],
                role=row["role"],  # type: ignore[arg-type]
                content=row["content"],
                meta=json_loads(row["meta"], {}) or {},
                created_at=row["created_at"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------- summary
    def summary_state(self, session_id: str) -> tuple[str, int]:
        session = self.get_session(session_id)
        if not session:
            return "", 0
        count = int(self.db.get_kv(f"summary_count:{session_id}", "0") or 0)
        return session.rolling_summary, count

    def set_summary(self, session_id: str, summary: str, compacted_count: int) -> None:
        self.db.execute(
            "UPDATE chat_sessions SET rolling_summary = ?, updated_at = ? WHERE id = ?",
            (summary, utc_now_iso(), session_id),
        )
        self.db.set_kv(f"summary_count:{session_id}", str(compacted_count))


__all__ = ["ChatService"]
