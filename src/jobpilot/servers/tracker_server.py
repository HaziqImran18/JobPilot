"""MCP server: application tracking and follow-ups."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from jobpilot.core.models import ApplicationStatus
from jobpilot.servers.common import ensure_ready, jsonable
from jobpilot.services.tracking import TrackingService

mcp = FastMCP("tracker", log_level="WARNING")
ensure_ready()


@mcp.tool()
def add_application(
    job_id: str = "",
    job_title: str = "",
    company: str = "",
    url: str = "",
    status: str = "saved",
    notes: str = "",
    next_action_at: str = "",
) -> dict:
    """Add a job to the application tracker. Pass job_id for stored jobs. Status is one
    of saved, tailoring, ready, applied, interviewing, offer, rejected, withdrawn."""
    application = TrackingService().add(
        job_id=job_id,
        job_title=job_title,
        company=company,
        url=url,
        status=ApplicationStatus(status),
        notes=notes,
        next_action_at=next_action_at or None,
    )
    return {"application": jsonable(application)}


@mcp.tool()
def update_status(application_id: str, status: str, note: str = "") -> dict:
    """Move an application to a new status and append an event to its history."""
    application = TrackingService().update_status(application_id, ApplicationStatus(status), note)
    return {"application": jsonable(application)}


@mcp.tool()
def list_applications(status: str = "", limit: int = 100) -> dict:
    """List tracked applications, optionally filtered by status, newest updates first."""
    status_value = ApplicationStatus(status) if status else None
    applications = TrackingService().list(status=status_value, limit=max(1, min(limit, 300)))
    return {"applications": jsonable(applications), "count": len(applications)}


@mcp.tool()
def list_followups(within_days: int = 7) -> dict:
    """List applications with a follow-up date inside the next N days."""
    applications = TrackingService().followups(within_days=max(1, min(within_days, 60)))
    return {"followups": jsonable(applications)}


@mcp.tool()
def mark_submitted(application_id: str, note: str = "") -> dict:
    """Mark an application as submitted. This is an external-tier action and always
    requires explicit user approval through the harness."""
    application = TrackingService().mark_submitted(application_id, note)
    return {"application": jsonable(application)}


if __name__ == "__main__":
    mcp.run(transport="stdio")
