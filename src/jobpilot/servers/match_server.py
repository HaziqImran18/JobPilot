"""MCP server: job matching, ranking and skill gaps."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from jobpilot.core.models import JobQuery
from jobpilot.servers.common import ensure_ready, jsonable
from jobpilot.services.matching import MatchingService

mcp = FastMCP("match", log_level="WARNING")
ensure_ready()


@mcp.tool()
def score_job(job_id: str, force: bool = False, use_llm: bool = True) -> dict:
    """Score one stored job against the profile. Returns 0-100, a component breakdown,
    matched skills, missing skills and a short rationale. Cached per profile version."""
    result = MatchingService().score_job(job_id, force=force, use_llm=use_llm)
    return jsonable(result)


@mcp.tool()
def rank_jobs(
    query: str = "",
    location: str = "",
    remote: bool | None = None,
    limit: int = 10,
    refresh: bool = True,
    use_llm: bool = True,
) -> dict:
    """Rank jobs for the user. Fetches fresh postings when refresh=true, deduplicates
    them, scores fit with embeddings plus a skill rubric, then returns the best cards."""
    ranked = MatchingService().rank_jobs(
        JobQuery(query=query, location=location, remote=remote, limit=max(1, min(limit, 30))),
        limit=max(1, min(limit, 30)),
        refresh=refresh,
        use_llm=use_llm,
    )
    return {"ranked": jsonable(ranked), "count": len(ranked)}


@mcp.tool()
def skill_gap(job_id: str) -> dict:
    """List skills mentioned in a job that are missing from the profile, with importance."""
    gaps = MatchingService().skill_gap(job_id)
    return {"gaps": jsonable(gaps)}


if __name__ == "__main__":
    mcp.run(transport="stdio")
