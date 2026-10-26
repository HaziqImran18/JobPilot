"""Deterministic planner used only by the offline evaluation harness.

It maps a small set of intents to the same MCP tool names the live agent uses,
which lets us measure tool routing without calling a model. It is not imported
by the runtime application.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from jobpilot.core.models import AgentEvent
from jobpilot.harness.tool_executor import ToolExecutor
from jobpilot.services.profile import ProfileService

_JOB_ID = re.compile(r"\bjob_[a-z0-9]+\b", re.IGNORECASE)


class DeterministicPlanner:
    def __init__(
        self,
        *,
        executor: ToolExecutor,
        session_id: str,
        profile_service: ProfileService | None = None,
    ) -> None:
        self.executor = executor
        self.session_id = session_id
        self.profiles = profile_service or ProfileService()

    def run(self, message: str) -> Iterator[AgentEvent]:
        lowered = message.lower()
        yield AgentEvent(type="status", data={"text": "Routing this request to the MCP tools."})

        if any(word in lowered for word in ("follow up", "followup", "follow-up")):
            return (yield from self._followups())
        if "submitted" in lowered or ("submit" in lowered and "application" in lowered):
            return (yield from self._mark_submitted(message))
        if any(
            word in lowered for word in ("application", "applied", "tracker", "tracking", "track")
        ):
            if any(word in lowered for word in ("add", "track", "save")):
                job_id = self._job_id(message)
                return (yield from self._track(job_id))
            return (yield from self._applications())
        if any(word in lowered for word in ("what do you remember", "memories", "memory")):
            return (yield from self._memories())
        if "remember" in lowered or "note that" in lowered:
            return (yield from self._remember(message))
        if any(word in lowered for word in ("tailor", "cover letter", "resume", "cv")):
            return (yield from self._tailor(message))
        if "score" in lowered and self._job_id(message):
            return (yield from self._score(self._job_id(message)))
        if any(
            word in lowered
            for word in ("job", "role", "find", "search", "match", "hiring", "opening", "vacancy")
        ):
            return (yield from self._search(message))
        return (yield from self._help(message))

    # ------------------------------------------------------------------ intents
    def _search(self, message: str) -> Iterator[AgentEvent]:
        profile = self.profiles.get()
        query = self._query(message, profile)
        remote = True if "remote" in message.lower() else None
        location = (
            ""
            if remote
            else (profile.preferences.locations[0] if profile.preferences.locations else "")
        )
        result = yield from self.executor.invoke(
            "rank_jobs",
            {
                "query": query,
                "location": location,
                "remote": remote,
                "limit": 6,
                "refresh": False,
                "use_llm": False,
            },
        )
        if result.ok and isinstance(result.data, dict) and not result.data.get("ranked"):
            result = yield from self.executor.invoke(
                "rank_jobs",
                {
                    "query": query,
                    "location": location,
                    "remote": remote,
                    "limit": 6,
                    "refresh": True,
                    "use_llm": False,
                },
            )
        if not result.ok:
            return (
                "I couldn't reach the job sources.\n\n"
                f"Reason: {result.error or 'unknown error'}\n\n"
                "Check that the free sources are reachable on the System page."
            )

        ranked = (result.data or {}).get("ranked", []) if isinstance(result.data, dict) else []
        if not ranked:
            return (
                "No jobs matched that search yet.\n\n"
                "Try a broader query, or check that the free sources are reachable "
                "on the System page."
            )

        lines = [f"### Top matches for “{query}”", ""]
        for index, item in enumerate(ranked, start=1):
            job = item.get("job", {})
            match = item.get("match", {})
            salary = job.get("salary") or ""
            details = " · ".join(
                part
                for part in [
                    job.get("company"),
                    job.get("location"),
                    salary,
                    f"{match.get('score', 0):.0f}/100",
                ]
                if part
            )
            lines.append(f"**{index}. {job.get('title')}** — {details}")
            matched = ", ".join(match.get("matched_skills", [])[:5]) or "none yet"
            missing = ", ".join(match.get("missing_skills", [])[:4])
            lines.append(f"Matched: {matched}" + (f" · Gaps: {missing}" if missing else ""))
            lines.append(f"`{job.get('id')}`")
            lines.append("")
        lines.append(
            "Ask **“tailor the top match”** for a verified CV draft, or **“track the top match”** to save it."
        )
        return "\n".join(lines)

    def _tailor(self, message: str) -> Iterator[AgentEvent]:
        job_id = self._job_id(message)
        if not job_id:
            ranked = yield from self.executor.invoke(
                "rank_jobs",
                {
                    "query": self._query(message, self.profiles.get()),
                    "limit": 3,
                    "refresh": False,
                    "use_llm": False,
                },
            )
            items = (
                (ranked.data or {}).get("ranked", [])
                if ranked.ok and isinstance(ranked.data, dict)
                else []
            )
            if not items:
                return "I need a job first. Say **“find AI Engineer jobs”** and then ask me to tailor the top match."
            index = 1 if "second" in message.lower() else 0
            job_id = items[min(index, len(items) - 1)]["job"]["id"]

        result = yield from self.executor.invoke("tailor_cv", {"job_id": job_id, "use_llm": False})
        if not result.ok or not isinstance(result.data, dict):
            return f"I couldn't tailor that CV: {result.error or 'unknown error'}"

        data = result.data
        bullets = "\n".join(f"- {bullet}" for bullet in data.get("bullets", []))
        rejected = data.get("unsupported") or []
        note = (
            f"\n\n> Verifier rejected {len(rejected)} unsupported claim(s) and removed them."
            if rejected
            else "\n\n> Every factual claim passed the profile verifier."
        )
        path = data.get("cv_path", "")
        return (
            f"## Tailored CV — {data.get('job_title')} at {data.get('company')}\n\n"
            f"**Headline**\n{data.get('headline')}\n\n"
            f"**Summary**\n{data.get('summary')}\n\n"
            f"**Selected experience**\n{bullets}{note}\n\n"
            f"The full CV and cover letter were saved to `{path}`. "
            "Open the job card to download it, or ask me to track this application."
        )

    def _applications(self) -> Iterator[AgentEvent]:
        result = yield from self.executor.invoke("list_applications", {"limit": 50})
        if not result.ok or not isinstance(result.data, dict):
            return f"I couldn't read the tracker: {result.error}"
        applications = result.data.get("applications", [])
        if not applications:
            return "Your tracker is empty. Ask me to **track the top match** after a search."
        lines = ["### Applications", ""]
        for application in applications:
            lines.append(
                f"- **{application.get('job_title')}** at {application.get('company')} — "
                f"`{application.get('status')}` · updated {application.get('updated_at', '')[:10]}"
            )
        return "\n".join(lines)

    def _followups(self) -> Iterator[AgentEvent]:
        result = yield from self.executor.invoke("list_followups", {"within_days": 7})
        if not result.ok or not isinstance(result.data, dict):
            return f"I couldn't read follow-ups: {result.error}"
        followups = result.data.get("followups", [])
        if not followups:
            return "No follow-ups are due in the next 7 days."
        return "### Follow-ups due\n\n" + "\n".join(
            f"- **{item.get('job_title')}** at {item.get('company')} — {item.get('next_action_at')}"
            for item in followups
        )

    def _remember(self, message: str) -> Iterator[AgentEvent]:
        text = re.sub(
            r"^(please\s+)?(remember|note)\s+(that\s+)?", "", message.strip(), flags=re.IGNORECASE
        )
        result = yield from self.executor.invoke(
            "remember",
            {"text": text or message, "kind": "semantic", "importance": 0.8, "explicit": True},
        )
        if not result.ok:
            return f"I couldn't store that: {result.error}"
        data = result.data or {}
        memory = data.get("memory", {})
        if data.get("requires_confirmation"):
            return f"I saved this for your confirmation: “{memory.get('text')}”."
        return f"Stored. I'll apply this in future searches: “{memory.get('text')}”."

    def _memories(self) -> Iterator[AgentEvent]:
        result = yield from self.executor.invoke("list_memories", {"limit": 20})
        if not result.ok or not isinstance(result.data, dict):
            return f"I couldn't read memories: {result.error}"
        memories = result.data.get("memories", [])
        if not memories:
            return "I don't have any active memories yet. Say **“remember that I prefer remote roles”**."
        return "### What I remember\n\n" + "\n".join(
            f"- {memory.get('text')} *(importance {float(memory.get('importance', 0)):.1f})*"
            for memory in memories
        )

    def _score(self, job_id: str) -> Iterator[AgentEvent]:
        result = yield from self.executor.invoke("score_job", {"job_id": job_id, "use_llm": False})
        if not result.ok or not isinstance(result.data, dict):
            return f"I couldn't score that job: {result.error}"
        data = result.data
        breakdown = "\n".join(
            f"- {name.replace('_', ' ').title()}: {value:.0f}/100"
            for name, value in data.get("breakdown", {}).items()
        )
        return (
            f"### Fit score: {data.get('score')}/100\n\n{data.get('rationale')}\n\n"
            f"**Breakdown**\n{breakdown}\n\n"
            f"Matched: {', '.join(data.get('matched_skills', [])) or 'none'}\n\n"
            f"Gaps: {', '.join(data.get('missing_skills', [])) or 'none'}"
        )

    def _track(self, job_id: str) -> Iterator[AgentEvent]:
        if not job_id:
            ranked = yield from self.executor.invoke(
                "rank_jobs",
                {
                    "query": self._query("", self.profiles.get()),
                    "limit": 3,
                    "refresh": False,
                    "use_llm": False,
                },
            )
            items = (
                (ranked.data or {}).get("ranked", [])
                if ranked.ok and isinstance(ranked.data, dict)
                else []
            )
            if not items:
                return "I need a job first. Say **“find AI Engineer jobs”** and then ask me to track the top match."
            job_id = items[0]["job"]["id"]
        result = yield from self.executor.invoke(
            "add_application", {"job_id": job_id, "status": "saved"}
        )
        if not result.ok:
            return f"I couldn't track that job: {result.error}"
        application = (result.data or {}).get("application", {})
        return (
            f"Tracked **{application.get('job_title')}** at {application.get('company')} "
            f"as `{application.get('status')}`."
        )

    def _mark_submitted(self, message: str) -> Iterator[AgentEvent]:
        listed = yield from self.executor.invoke("list_applications", {"limit": 50})
        if not listed.ok or not isinstance(listed.data, dict):
            return f"I couldn't read the tracker: {listed.error}"
        applications = listed.data.get("applications", [])
        if not applications:
            return "There is nothing on the board yet. Ask me to **track the top match** first."
        requested = re.search(r"app_[a-zA-Z0-9]+", message)
        application = applications[0]
        if requested:
            application = next(
                (item for item in applications if item.get("id") == requested.group(0)), application
            )
        result = yield from self.executor.invoke(
            "mark_submitted",
            {"application_id": application["id"], "note": "Submitted from the eval planner."},
        )
        if not result.ok:
            return f"I left the application unchanged: {result.error}"
        updated = (result.data or {}).get("application", {})
        return (
            f"Marked **{updated.get('job_title', application.get('job_title'))}** as "
            f"`{updated.get('status', 'submitted')}` after your approval."
        )

    # ------------------------------------------------------------------ helpers
    def _help(self, message: str) -> str:
        profile = self.profiles.get()
        name = profile.full_name.split()[0] if profile.full_name else "there"
        return (
            f"Hi {name}. This is the offline evaluation planner, driving the same MCP "
            "tools a live agent would use.\n\n"
            "Try:\n"
            "- **Find AI Engineer jobs** — rank roles\n"
            "- **Tailor the top match** — verified CV draft + cover letter\n"
            "- **Track the top match** — add it to the application board\n"
            "- **What do you remember?** — inspect durable memory\n"
            "- **Remember that I prefer remote roles**\n"
        )

    @staticmethod
    def _job_id(message: str) -> str:
        match = _JOB_ID.search(message)
        return match.group(0).lower() if match else ""

    @staticmethod
    def _query(message: str, profile) -> str:
        lowered = message.lower()
        detected = [
            role
            for role in profile.preferences.target_roles
            if role.lower() in lowered or role.lower().replace(" engineer", "") in lowered
        ]
        for alias, canonical in {
            "ml ": "Machine Learning Engineer",
            "llm ": "LLM Engineer",
            "computer vision": "Computer Vision Engineer",
            "data scien": "Data Scientist",
            "mlops": "MLOps Engineer",
            "backend": "Python Backend Engineer",
        }.items():
            if alias in lowered and canonical not in detected:
                detected.append(canonical)
        if detected:
            return " ".join(detected[:2])
        return " ".join(profile.preferences.target_roles[:2]) or "AI Engineer"


__all__ = ["DeterministicPlanner"]
