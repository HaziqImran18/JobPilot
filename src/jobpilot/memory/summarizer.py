"""Rolling conversation summaries used by context compaction."""

from __future__ import annotations

from typing import Any

from jobpilot.core.utils import truncate

SUMMARY_PROMPT = """You compress a job-search conversation so it can continue later.
Keep only: the user's goal, hard constraints, decisions made, jobs discussed
(title + company), and unresolved next steps. Do not invent facts. Write at most
120 words in plain text, no markdown headings.
"""


class Summarizer:
    def __init__(self, llm: Any | None = None) -> None:
        self._llm = llm

    def summarize(self, messages: list[dict[str, str]], previous_summary: str = "") -> str:
        transcript = "\n".join(
            f"{message.get('role', 'user')}: {truncate(message.get('content', ''), 700)}"
            for message in messages
            if message.get("role") in {"user", "assistant"} and message.get("content")
        )
        if not transcript.strip():
            return previous_summary

        if self._llm is not None and getattr(self._llm, "available", False):
            try:
                response = self._llm.complete(
                    [
                        {"role": "system", "content": SUMMARY_PROMPT},
                        {
                            "role": "user",
                            "content": f"Previous summary:\n{previous_summary or '(none)'}\n\nNew turns:\n{transcript}",
                        },
                    ],
                    purpose="summarize",
                    temperature=0.1,
                    max_tokens=250,
                )
                if response.text.strip():
                    return response.text.strip()
            except Exception:
                pass

        # Offline fallback: keep the previous summary and the most recent turns.
        tail = transcript[-900:]
        combined = f"{previous_summary}\n{tail}".strip()
        return truncate(combined, 1200)


__all__ = ["Summarizer", "SUMMARY_PROMPT"]
