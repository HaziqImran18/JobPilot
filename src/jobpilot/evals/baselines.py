"""Cheap baselines for ranking eval cases.

The keyword baseline is deliberately simple. The optional TF-IDF baseline uses
scikit-learn (installed by ``pip install .[eval]``) and is skipped gracefully
when the extra is not available.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from jobpilot.core.models import Job, Profile
from jobpilot.memory.embeddings import tokenize


def profile_text(profile: Profile) -> str:
    parts = [
        profile.headline,
        profile.summary,
        " ".join(skill.name for skill in profile.skills),
        " ".join(profile.preferences.target_roles),
        " ".join(profile.preferences.locations),
    ]
    return "\n".join(part for part in parts if part)


def job_text(job: Job) -> str:
    return "\n".join(
        [
            job.title,
            job.title,
            job.company,
            " ".join(job.tags),
            job.description,
        ]
    )


def keyword_scores(profile: Profile, jobs: Sequence[Job]) -> dict[str, float]:
    """Weighted lexical overlap, scaled to a 0-100 range for readability."""
    profile_terms = set(tokenize(profile_text(profile)))
    if not profile_terms:
        return {job.id: 0.0 for job in jobs}
    scores: dict[str, float] = {}
    for job in jobs:
        terms = Counter(tokenize(job_text(job)))
        matched = sum(1 for term in profile_terms if terms[term])
        scores[job.id] = round(100 * matched / len(profile_terms), 2)
    return scores


def tfidf_scores(profile: Profile, jobs: Sequence[Job]) -> dict[str, float] | None:
    """Cosine similarity baseline. Returns ``None`` if scikit-learn is absent."""
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
    except ImportError:  # pragma: no cover - only when the eval extra is missing
        return None

    documents = [profile_text(profile), *[job_text(job) for job in jobs]]
    vectorizer = TfidfVectorizer(ngram_range=(1, 2), stop_words="english", sublinear_tf=True)
    matrix = vectorizer.fit_transform(documents)
    profile_vector = matrix[0]
    similarities = (matrix[1:] @ profile_vector.T).toarray().ravel()
    return {
        job.id: round(float(score) * 100, 2) for job, score in zip(jobs, similarities, strict=True)
    }
