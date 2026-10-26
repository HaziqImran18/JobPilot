"""Write-policy for memory: the model proposes, the harness decides."""

from __future__ import annotations

from jobpilot.core.models import MemoryKind, MemoryProposal, Profile

PROFILE_AFFECTING_KEYS = {
    "target_roles",
    "locations",
    "remote",
    "salary_floor",
    "seniority",
    "skills_to_avoid",
    "excluded_companies",
    "excluded_keywords",
    "work_authorization",
}

HIGH_IMPORTANCE_KEYS = PROFILE_AFFECTING_KEYS | {"constraint", "preference"}


class MemoryPolicy:
    """Rules for turning a proposed memory into a stored one.

    * Explicit user requests ("remember that ...") are stored immediately.
    * Episodic facts (what happened) are stored immediately.
    * Profile-affecting semantic facts need confirmation in the UI.
    * Low-confidence observations are parked for confirmation instead of dropped.
    """

    def evaluate(
        self,
        *,
        text: str,
        key: str = "",
        value: str = "",
        kind: MemoryKind = MemoryKind.semantic,
        importance: float | None = None,
        confidence: float = 0.8,
        explicit: bool = False,
        profile: Profile | None = None,
    ) -> MemoryProposal:
        text = " ".join(text.split()).strip()
        key = key.strip().lower()
        if not importance:
            importance = 0.8 if key in HIGH_IMPORTANCE_KEYS else 0.5
        requires_confirmation = False
        reason = ""

        if kind is MemoryKind.semantic and not explicit:
            requires_confirmation = True
            reason = "Profile-affecting memory needs your confirmation."
        if confidence < 0.55 and not explicit:
            requires_confirmation = True
            reason = "Low-confidence observation; confirm before it is used."

        if (
            profile is not None
            and key == "excluded_companies"
            and value
            and value.strip().lower()
            in {company.lower() for company in profile.preferences.excluded_companies}
        ):
            requires_confirmation = False
            reason = "Already present in your profile."

        return MemoryProposal(
            text=text,
            kind=kind,
            key=key,
            value=value,
            importance=max(0.0, min(1.0, importance)),
            confidence=max(0.0, min(1.0, confidence)),
            requires_confirmation=requires_confirmation,
            reason=reason,
        )


__all__ = ["MemoryPolicy", "PROFILE_AFFECTING_KEYS", "HIGH_IMPORTANCE_KEYS"]
