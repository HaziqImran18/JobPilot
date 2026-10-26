"""Application tracking with status events and follow-up reminders."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from jobpilot.core.db import Database, get_db
from jobpilot.core.models import Application, ApplicationEvent, ApplicationStatus
from jobpilot.core.utils import utc_now_iso
from jobpilot.services.jobs import JobService, get_jobs

STATUS_ORDER = [
    ApplicationStatus.saved,
    ApplicationStatus.tailoring,
    ApplicationStatus.ready,
    ApplicationStatus.applied,
    ApplicationStatus.interviewing,
    ApplicationStatus.offer,
    ApplicationStatus.rejected,
    ApplicationStatus.withdrawn,
]


class TrackingService:
    def __init__(self, db: Database | None = None, jobs: JobService | None = None) -> None:
        self.db = db or get_db()
        self.jobs = jobs or get_jobs()

    # -------------------------------------------------------------------- write
    def add(
        self,
        *,
        job_id: str = "",
        job_title: str = "",
        company: str = "",
        url: str = "",
        status: ApplicationStatus | str = ApplicationStatus.saved,
        notes: str = "",
        next_action_at: str | None = None,
    ) -> Application:
        if job_id:
            existing = self.db.query_one("SELECT id FROM applications WHERE job_id = ?", (job_id,))
            if existing:
                return self.get(existing["id"]) or self._missing()
            job = self.jobs.get(job_id)
            if job is None:
                raise ValueError(f"Unknown job id: {job_id}")
            job_title = job_title or job.title
            company = company or job.company
            url = url or job.url
        if not job_title:
            raise ValueError("job_title is required when no job_id is given.")

        status_value = ApplicationStatus(status).value
        now = utc_now_iso()
        app_id = f"app_{uuid.uuid4().hex[:12]}"
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO applications(id, job_id, job_title, company, url, status, notes, cv_path, "
                "cover_letter_path, next_action_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, '', '', ?, ?, ?)",
                (
                    app_id,
                    job_id or None,
                    job_title,
                    company,
                    url,
                    status_value,
                    notes,
                    next_action_at,
                    now,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO application_events(id, application_id, status, note, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    f"evt_{uuid.uuid4().hex[:10]}",
                    app_id,
                    status_value,
                    notes or "Application saved.",
                    now,
                ),
            )
        return self.get(app_id) or self._missing()

    def update_status(
        self,
        application_id: str,
        status: ApplicationStatus | str,
        note: str = "",
        *,
        next_action_at: str | None = None,
    ) -> Application:
        status_value = ApplicationStatus(status).value
        existing = self.get(application_id)
        if existing is None:
            raise ValueError(f"Unknown application id: {application_id}")
        now = utc_now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE applications SET status = ?, updated_at = ?, next_action_at = COALESCE(?, next_action_at) "
                "WHERE id = ?",
                (status_value, now, next_action_at, application_id),
            )
            conn.execute(
                "INSERT INTO application_events(id, application_id, status, note, created_at) VALUES (?, ?, ?, ?, ?)",
                (f"evt_{uuid.uuid4().hex[:10]}", application_id, status_value, note, now),
            )
        updated = self.get(application_id)
        assert updated is not None
        return updated

    def set_notes(self, application_id: str, notes: str) -> Application:
        self.db.execute(
            "UPDATE applications SET notes = ?, updated_at = ? WHERE id = ?",
            (notes, utc_now_iso(), application_id),
        )
        existing = self.get(application_id)
        if existing is None:
            raise ValueError(f"Unknown application id: {application_id}")
        return existing

    def attach_cv(self, application_id: str, cv_path: str) -> Application:
        self.db.execute(
            "UPDATE applications SET cv_path = ?, updated_at = ? WHERE id = ?",
            (cv_path, utc_now_iso(), application_id),
        )
        existing = self.get(application_id)
        if existing is None:
            raise ValueError(f"Unknown application id: {application_id}")
        return existing

    def mark_submitted(self, application_id: str, note: str = "") -> Application:
        return self.update_status(
            application_id,
            ApplicationStatus.applied,
            note or "Marked as submitted by the user (external action approved).",
        )

    def delete(self, application_id: str) -> bool:
        row = self.db.query_one("SELECT id FROM applications WHERE id = ?", (application_id,))
        if not row:
            return False
        self.db.execute("DELETE FROM applications WHERE id = ?", (application_id,))
        return True

    # --------------------------------------------------------------------- read
    def get(self, application_id: str) -> Application | None:
        row = self.db.query_one("SELECT * FROM applications WHERE id = ?", (application_id,))
        if not row:
            return None
        return self._row_to_application(row, with_events=True)

    def list(
        self,
        *,
        status: ApplicationStatus | None = None,
        job_id: str = "",
        limit: int = 100,
    ) -> list[Application]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if job_id:
            clauses.append("job_id = ?")
            params.append(job_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.query(
            f"SELECT * FROM applications {where} ORDER BY updated_at DESC LIMIT ?",  # noqa: S608
            (*params, limit),
        )
        return [self._row_to_application(row) for row in rows]

    def board(self) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {status.value: [] for status in STATUS_ORDER}
        for application in self.list(limit=300):
            grouped.setdefault(application.status.value, []).append(
                application.model_dump(mode="json")
            )
        return grouped

    def followups(self, within_days: int = 7) -> list[Application]:
        cutoff = (datetime.now(UTC) + timedelta(days=within_days)).date().isoformat()
        rows = self.db.query(
            "SELECT * FROM applications WHERE next_action_at IS NOT NULL AND next_action_at <= ? "
            "AND status NOT IN ('rejected', 'withdrawn', 'offer') ORDER BY next_action_at ASC",
            (cutoff,),
        )
        return [self._row_to_application(row) for row in rows]

    def stats(self) -> dict[str, int]:
        rows = self.db.query("SELECT status, COUNT(*) AS n FROM applications GROUP BY status")
        counts = {status.value: 0 for status in STATUS_ORDER}
        for row in rows:
            counts[row["status"]] = int(row["n"])
        counts["total"] = sum(counts.values())
        return counts

    # ------------------------------------------------------------------ helpers
    def _row_to_application(self, row: Any, *, with_events: bool = False) -> Application:
        events: list[ApplicationEvent] = []
        if with_events:
            event_rows = self.db.query(
                "SELECT * FROM application_events WHERE application_id = ? ORDER BY created_at ASC",
                (row["id"],),
            )
            events = [
                ApplicationEvent(
                    id=event["id"],
                    application_id=event["application_id"],
                    status=event["status"],
                    note=event["note"],
                    created_at=event["created_at"],
                )
                for event in event_rows
            ]
        return Application(
            id=row["id"],
            job_id=row["job_id"],
            job_title=row["job_title"],
            company=row["company"],
            url=row["url"],
            status=ApplicationStatus(row["status"]),
            notes=row["notes"],
            cv_path=row["cv_path"],
            cover_letter_path=row["cover_letter_path"],
            next_action_at=row["next_action_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            events=events,
        )

    @staticmethod
    def _missing() -> Application:
        raise RuntimeError("Application disappeared during the transaction.")


def get_tracking() -> TrackingService:
    return TrackingService()


__all__ = ["TrackingService", "get_tracking", "STATUS_ORDER"]
