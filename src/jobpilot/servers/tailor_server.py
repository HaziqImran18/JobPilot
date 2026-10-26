"""MCP server: verified CV tailoring and cover letters."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from jobpilot.servers.common import ensure_ready, jsonable
from jobpilot.services.tailoring import TailoringService

mcp = FastMCP("tailor", log_level="WARNING")
ensure_ready()


@mcp.tool()
def tailor_cv(job_id: str, use_llm: bool = True) -> dict:
    """Generate a tailored CV summary, bullets and cover letter for a job. Content is
    restricted to profile facts and passed through the claim verifier. Unsupported
    claims are removed and listed in `unsupported`; never reintroduce them."""
    result = TailoringService().tailor_cv(job_id, use_llm=use_llm)
    return jsonable(result)


@mcp.tool()
def draft_cover_letter(job_id: str) -> dict:
    """Draft only the cover letter for a job, using the same fact verification."""
    result = TailoringService().draft_cover_letter(job_id)
    return {
        "job_id": result.job_id,
        "job_title": result.job_title,
        "company": result.company,
        "cover_letter": result.cover_letter,
        "verified": result.verified,
        "unsupported": result.unsupported,
        "cv_path": result.cv_path,
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
