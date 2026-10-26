"""Profile storage and retrieval — the single source of truth for CV facts."""

from __future__ import annotations

import uuid

from jobpilot.core.db import Database, get_db
from jobpilot.core.models import (
    Education,
    Experience,
    Preferences,
    Profile,
    Project,
    Skill,
)
from jobpilot.core.utils import json_dumps, json_loads, utc_now_iso
from jobpilot.memory.embeddings import tokenize


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class ProfileService:
    def __init__(self, db: Database | None = None) -> None:
        self.db = db or get_db()

    # --------------------------------------------------------------------- read
    def get(self) -> Profile:
        row = self.db.query_one("SELECT * FROM profile WHERE id = 1")
        if row is None:
            return self.ensure()

        experiences = [
            Experience(
                id=item["id"],
                company=item["company"],
                title=item["title"],
                start_date=item["start_date"],
                end_date=item["end_date"],
                location=item["location"],
                bullets=json_loads(item["bullets"], []) or [],
                skills=json_loads(item["skills"], []) or [],
                is_current=bool(item["is_current"]),
            )
            for item in self.db.query(
                "SELECT * FROM experiences ORDER BY sort_order, start_date DESC"
            )
        ]
        education = [
            Education(
                id=item["id"],
                institution=item["institution"],
                degree=item["degree"],
                field_of_study=item["field_of_study"],
                start_date=item["start_date"],
                end_date=item["end_date"],
                details=item["details"],
            )
            for item in self.db.query("SELECT * FROM education ORDER BY sort_order, end_date DESC")
        ]
        projects = [
            Project(
                id=item["id"],
                name=item["name"],
                description=item["description"],
                url=item["url"],
                skills=json_loads(item["skills"], []) or [],
            )
            for item in self.db.query("SELECT * FROM projects ORDER BY sort_order")
        ]
        skills = [
            Skill(
                name=item["name"],
                level=int(item["level"]),
                years=float(item["years"]),
                category=item["category"],
            )
            for item in self.db.query("SELECT * FROM skills ORDER BY category, level DESC, name")
        ]
        return Profile(
            full_name=row["full_name"],
            headline=row["headline"],
            email=row["email"],
            phone=row["phone"],
            location=row["location"],
            links=json_loads(row["links"], {}) or {},
            summary=row["summary"],
            preferences=Preferences.model_validate(json_loads(row["preferences"], {}) or {}),
            experiences=experiences,
            education=education,
            projects=projects,
            skills=skills,
            updated_at=row["updated_at"],
        )

    # -------------------------------------------------------------------- write
    def save(self, profile: Profile) -> Profile:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO profile(id, full_name, headline, email, phone, location, links, summary, preferences, updated_at) "
                "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET full_name = excluded.full_name, headline = excluded.headline, "
                "email = excluded.email, phone = excluded.phone, location = excluded.location, links = excluded.links, "
                "summary = excluded.summary, preferences = excluded.preferences, updated_at = excluded.updated_at",
                (
                    profile.full_name,
                    profile.headline,
                    profile.email,
                    profile.phone,
                    profile.location,
                    json_dumps(profile.links),
                    profile.summary,
                    json_dumps(profile.preferences.model_dump(mode="json")),
                    now,
                ),
            )
            conn.execute("DELETE FROM experiences")
            conn.executemany(
                "INSERT INTO experiences(id, company, title, start_date, end_date, location, bullets, skills, is_current, sort_order) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        exp.id or _id("exp"),
                        exp.company,
                        exp.title,
                        exp.start_date,
                        exp.end_date,
                        exp.location,
                        json_dumps(exp.bullets),
                        json_dumps(exp.skills),
                        int(exp.is_current),
                        index,
                    )
                    for index, exp in enumerate(profile.experiences)
                ],
            )
            conn.execute("DELETE FROM education")
            conn.executemany(
                "INSERT INTO education(id, institution, degree, field_of_study, start_date, end_date, details, sort_order) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        edu.id or _id("edu"),
                        edu.institution,
                        edu.degree,
                        edu.field_of_study,
                        edu.start_date,
                        edu.end_date,
                        edu.details,
                        index,
                    )
                    for index, edu in enumerate(profile.education)
                ],
            )
            conn.execute("DELETE FROM projects")
            conn.executemany(
                "INSERT INTO projects(id, name, description, url, skills, sort_order) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        project.id or _id("prj"),
                        project.name,
                        project.description,
                        project.url,
                        json_dumps(project.skills),
                        index,
                    )
                    for index, project in enumerate(profile.projects)
                ],
            )
            conn.execute("DELETE FROM skills")
            conn.executemany(
                "INSERT INTO skills(id, name, level, years, category) VALUES (?, ?, ?, ?, ?)",
                [
                    (skill.id or _id("skl"), skill.name, skill.level, skill.years, skill.category)
                    for skill in profile.skills
                ],
            )
        return self.get()

    # ------------------------------------------------------------------- search
    def search_experience(self, query: str, limit: int = 4) -> list[Experience]:
        profile = self.get()
        query_tokens = set(tokenize(query))
        scored: list[tuple[float, Experience]] = []
        for exp in profile.experiences:
            text = " ".join([exp.title, exp.company, *exp.bullets, *exp.skills])
            tokens = set(tokenize(text))
            overlap = (
                len(query_tokens & tokens) / max(len(query_tokens), 1) if query_tokens else 0.0
            )
            recency_bonus = 0.05 if exp.is_current else 0.0
            scored.append((overlap + recency_bonus, exp))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [exp for _, exp in scored[:limit]] or profile.experiences[:limit]

    def list_skills(self, category: str = "") -> list[Skill]:
        profile = self.get()
        if category:
            return [skill for skill in profile.skills if skill.category.lower() == category.lower()]
        return profile.skills

    # ------------------------------------------------------------------- ensure
    def ensure(self) -> Profile:
        """Create an empty profile for a new account.

        No identity, experience or skills are invented. The Profile page
        becomes the onboarding form for a fresh workspace.
        """
        row = self.db.query_one("SELECT id FROM profile WHERE id = 1")
        if row is not None:
            return self.get()
        return self.save(Profile(updated_at=utc_now_iso()))


__all__ = ["ProfileService"]
