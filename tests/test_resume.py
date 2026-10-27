"""Resume extraction, review and profile import."""

from __future__ import annotations

import io

from jobpilot.pipelines.extract import extract_skills
from jobpilot.services.profile import ProfileService
from jobpilot.services.resume import ResumeError, ResumeService, extract_fields

RESUME_TEXT = """Ayesha Khan
Senior AI Engineer
ayesha.khan@example.com | +92 300 1234567
Lahore, Pakistan
https://linkedin.com/in/ayesha-khan
https://github.com/ayesha

Summary
AI engineer with 6 years of experience building production Python and
machine learning systems, including RAG pipelines and FastAPI services.

Skills
Python, PyTorch, SQL, FastAPI, Docker, AWS
"""


def _docx_bytes(text: str) -> bytes:
    import docx

    document = docx.Document()
    for line in text.splitlines():
        document.add_paragraph(line)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_extract_fields_finds_contact_skills_and_summary():
    fields = extract_fields(RESUME_TEXT)
    assert fields["full_name"] == "Ayesha Khan"
    assert fields["email"] == "ayesha.khan@example.com"
    assert "linkedin.com/in/ayesha-khan" in fields["links"]["linkedin"]
    assert fields["location"] == "Lahore, Pakistan"
    assert "Python" in fields["skills"]
    assert "PyTorch" in fields["skills"]
    assert fields["years_experience"] == 6
    assert "AI engineer" in fields["summary"]
    assert extract_skills("unrelated sentence") == []


def test_upload_extracts_docx_and_keeps_versions(clean_database):
    service = ResumeService(uploads_dir=clean_database.path.parent / "uploads")
    first = service.upload("first.docx", _docx_bytes(RESUME_TEXT))
    assert first.status == "ready"
    assert first.is_active
    assert "Python" in first.extracted["skills"]
    assert first.text

    second = service.upload("second.txt", RESUME_TEXT.encode("utf-8"))
    assert second.is_active
    assert service.active().id == second.id
    assert {resume.id for resume in service.list()} == {first.id, second.id}

    activated = service.activate(first.id)
    assert activated.is_active
    assert service.active().id == first.id

    service.delete(second.id)
    assert service.get(second.id) is None
    assert [resume.id for resume in service.list()] == [first.id]


def test_upload_rejects_wrong_type_and_fake_pdf(clean_database):
    service = ResumeService(uploads_dir=clean_database.path.parent / "uploads")
    try:
        service.upload("resume.exe", b"MZ binary")
    except ResumeError as exc:
        assert "PDF, DOCX or plain-text" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("Unsupported files must be rejected.")

    try:
        service.upload("resume.pdf", b"not a real pdf")
    except ResumeError as exc:
        assert "does not look like a valid PDF" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("A fake PDF must be rejected.")


def test_scanned_pdf_reports_manual_fallback(clean_database):
    service = ResumeService(uploads_dir=clean_database.path.parent / "uploads")
    # A valid but empty PDF signature is enough for the magic check; text
    # extraction finds nothing, so the record is stored with a clear error.
    stored = service.upload("scan.pdf", b"%PDF-1.4\n%%EOF\n")
    assert stored.status == "error"
    assert stored.error
    assert service.get(stored.id) is not None


def test_review_updates_and_apply_merges_profile(clean_database):
    service = ResumeService(uploads_dir=clean_database.path.parent / "uploads")
    resume = service.upload("resume.txt", RESUME_TEXT.encode("utf-8"))
    updated = service.update_fields(
        resume.id,
        {
            "full_name": "Ayesha K.",
            "headline": "Senior AI Engineer",
            "skills": ["Python", "PyTorch", "SQL"],
            "links": {"linkedin": "https://linkedin.com/in/ayesha-khan"},
        },
    )
    assert updated.extracted["full_name"] == "Ayesha K."

    profile = ProfileService().get()
    profile.headline = "Existing headline"
    ProfileService().save(profile)

    from jobpilot.core.models import Skill

    profile = ProfileService().get()
    profile = profile.model_copy(
        update={
            "full_name": updated.extracted["full_name"],
            "headline": updated.extracted["headline"],
            "links": {**profile.links, **updated.extracted["links"]},
            "skills": [
                *profile.skills,
                *[
                    Skill(name=name, level=3, years=0, category="general")
                    for name in updated.extracted["skills"]
                ],
            ],
        }
    )
    saved = ProfileService().save(profile)
    assert saved.full_name == "Ayesha K."
    assert saved.headline == "Senior AI Engineer"
    assert saved.links["linkedin"].endswith("ayesha-khan")
    assert {"Python", "PyTorch", "SQL"}.issubset({skill.name for skill in saved.skills})
