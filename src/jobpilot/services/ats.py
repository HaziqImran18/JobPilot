"""ATS compatibility scoring between the active resume and a job posting.

The score is computed from the resume text the user uploaded (the same resume
stored on their profile) against the job's title, description and tags. It is
a heuristic, evidence-based measure -- not a guarantee of anything -- and is
shown alongside the existing profile-fit score so the candidate can see both:

* Profile fit score: how the whole profile (experience, education, skills,
  preferences) matches the job.
* ATS score: how the resume text itself aligns with the job's keywords and
  requirements, as an applicant tracking system might read it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from jobpilot.core.db import Database, get_db
from jobpilot.core.models import Job
from jobpilot.core.utils import normalize_text
from jobpilot.pipelines.extract import extract_skills
from jobpilot.services.profile import ProfileService
from jobpilot.services.resume import get_resumes

_YEARS_REQ = re.compile(r"(\d{1,2})\+?\s*(?:-|to)?\s*(\d{1,2})?\s*years?", re.IGNORECASE)
_DEGREE_HINTS = (
    "bachelor", "bs", "b.sc", "bsc", "be", "b.eng", "btech",
    "master", "ms", "m.sc", "msc", "me", "m.eng", "mtech",
    "phd", "doctorate", "associate", "diploma",
)
_SKILL_WEIGHT = 0.45
_KEYWORD_WEIGHT = 0.25
_EXPERIENCE_WEIGHT = 0.20
_EDUCATION_WEIGHT = 0.10


@dataclass
class ATSScore:
    score: float = 0.0  # 0-100
    breakdown: dict[str, float] = field(default_factory=dict)
    matched_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    matched_keywords: list[str] = field(default_factory=list)
    missing_keywords: list[str] = field(default_factory=list)
    rationale: str = ""
    model: str = "heuristic"


def _token_set(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9+#.\-]+", normalize_text(text or "")) if len(t) >= 2}


def _skill_overlap(resume_text: str, job_text: str) -> tuple[float, list[str], list[str]]:
    resume_skills = set(extract_skills(resume_text, limit=50))
    job_skills = set(extract_skills(job_text, limit=50))
    if not job_skills:
        return 0.0, [], []
    matched = sorted(resume_skills & job_skills)
    missing = sorted(job_skills - resume_skills)
    coverage = len(matched) / len(job_skills)
    return min(1.0, coverage), matched, missing


def _keyword_overlap(resume_tokens: set[str], job_tokens: set[str]) -> tuple[float, list[str], list[str]]:
    if not job_tokens:
        return 0.0, [], []
    matched = sorted(resume_tokens & job_tokens)
    missing = sorted(job_tokens - resume_tokens)
    coverage = len(matched) / len(job_tokens)
    return min(1.0, coverage), matched, missing


def _experience_fit(resume_text: str, job_text: str) -> tuple[float, str]:
    """Compare years of experience claimed in the resume vs required by the job."""
    resume_years = 0.0
    for match in _YEARS_REQ.finditer(resume_text):
        try:
            resume_years = max(resume_years, float(match.group(1)))
        except ValueError:
            continue

    required = [
        int(m.group(1))
        for m in _YEARS_REQ.finditer(job_text)
        if int(m.group(1)) <= 15
    ]
    if not required:
        return 0.7, "No explicit years requirement."
    min_required = min(required)
    if resume_years >= min_required:
        return 1.0, f"Resume shows {resume_years:g}+ years; role asks for {min_required}+."
    if resume_years >= min_required - 2:
        return 0.6, f"Close: {resume_years:g} years vs {min_required}+ requested."
    return 0.3, f"Gap: {resume_years:g} years vs {min_required}+ requested."


def _education_fit(resume_text: str, job_text: str) -> tuple[float, str]:
    resume_tokens = _token_set(resume_text)
    job_tokens = _token_set(job_text)
    resume_degrees = {d for d in _DEGREE_HINTS if d in resume_tokens}
    job_degrees = {d for d in _DEGREE_HINTS if d in job_tokens}
    if not job_degrees:
        return 0.7, "No specific degree requirement stated."
    if resume_degrees & job_degrees:
        return 1.0, "Degree requirement met."
    if resume_degrees:
        return 0.5, "Has a degree, but not the one requested."
    return 0.2, "No matching degree found on resume."


class ATSScoringService:
    def __init__(
        self,
        db: Database | None = None,
        jobs: Any = None,
        resumes=None,
        profiles: ProfileService | None = None,
    ) -> None:
        self.db = db or get_db()
        if jobs is None:
            from jobpilot.services.jobs import JobService

            jobs = JobService(self.db)
        self.jobs = jobs
        self.resumes = resumes or get_resumes()
        self.profiles = profiles or ProfileService(self.db)

    def active_resume_text(self) -> str:
        resume = self.resumes.active()
        if resume is None:
            return ""
        # Prefer the reviewed/extracted structured text, fall back to raw text.
        extracted = resume.extracted or {}
        parts: list[str] = []
        for key in ("full_name", "headline", "summary", "location"):
            value = extracted.get(key)
            if value:
                parts.append(str(value))
        skills = extracted.get("skills") or []
        if isinstance(skills, list):
            parts.extend(str(s) for s in skills)
        if resume.text:
            parts.append(resume.text)
        return "\n".join(part for part in parts if part)

    def score_ats(self, job: Job, resume_text: str | None = None) -> ATSScore:
        if resume_text is None:
            resume_text = self.active_resume_text()
        if not resume_text:
            return ATSScore(
                score=0.0,
                rationale="No active resume uploaded. Upload a resume on Profile to see ATS scores.",
                model="none",
            )

        job_text = f"{job.title}\n{job.description}\n{' '.join(job.tags)}"

        skills_score, matched_skills, missing_skills = _skill_overlap(resume_text, job_text)
        resume_tokens = _token_set(resume_text)
        job_tokens = _token_set(job_text)
        keyword_score, matched_keywords, missing_keywords = _keyword_overlap(
            resume_tokens, job_tokens
        )
        experience_score, experience_note = _experience_fit(resume_text, job_text)
        education_score, education_note = _education_fit(resume_text, job_text)

        breakdown = {
            "skills": round(skills_score * 100, 1),
            "keywords": round(keyword_score * 100, 1),
            "experience": round(experience_score * 100, 1),
            "education": round(education_score * 100, 1),
        }

        score = round(
            (skills_score * _SKILL_WEIGHT
            + keyword_score * _KEYWORD_WEIGHT
            + experience_score * _EXPERIENCE_WEIGHT
            + education_score * _EDUCATION_WEIGHT) * 100, 1)

        # Cap low-skill-overlap resumes so keyword stuffing cannot fake a fit.
        if skills_score < 0.15 and not matched_skills:
            score = min(score, 25.0)

        rationale_parts = [
            f"Skills match: {len(matched_skills)} of {len(set(extract_skills(job_text, limit=50)))} requested.",
        ]
        if missing_skills:
            rationale_parts.append(
                f"Missing: {', '.join(missing_skills[:5])}."
            )
        rationale_parts.append(experience_note)
        rationale_parts.append(education_note)

        return ATSScore(
            score=round(max(0.0, min(100.0, score)), 1),
            breakdown=breakdown,
            matched_skills=matched_skills[:8],
            missing_skills=missing_skills[:8],
            matched_keywords=matched_keywords[:10],
            missing_keywords=missing_keywords[:10],
            rationale=" ".join(rationale_parts),
            model="heuristic",
        )


def get_ats_scoring() -> ATSScoringService:
    return ATSScoringService()
