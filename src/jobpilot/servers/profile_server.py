"""MCP server: read-only access to the user's profile facts."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from jobpilot.servers.common import ensure_ready, jsonable
from jobpilot.services.profile import ProfileService

mcp = FastMCP("profile", log_level="WARNING")
ensure_ready()


@mcp.tool()
def get_profile() -> dict:
    """Return the user's full profile: contact, summary, preferences, experience,
    education, projects and skills. This is the only source of truth for CV facts."""
    profile = ProfileService().get()
    return jsonable(profile)


@mcp.tool()
def search_experience(query: str, limit: int = 4) -> dict:
    """Find the most relevant experience entries for a query (job title, skills or
    requirements). Returns bullets and skills that can be cited in a tailored CV."""
    experiences = ProfileService().search_experience(query, limit=max(1, min(limit, 10)))
    return {"experiences": jsonable(experiences)}


@mcp.tool()
def list_skills(category: str = "") -> dict:
    """List the user's skills with level (1-5) and years. Optionally filter by category."""
    skills = ProfileService().list_skills(category)
    return {"skills": jsonable(skills)}


if __name__ == "__main__":
    mcp.run(transport="stdio")
