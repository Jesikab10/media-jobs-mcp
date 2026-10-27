"""Data-leak guards applied at the boundary, before upstream text reaches a model or a user."""

from __future__ import annotations

import re

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("PHONE", re.compile(r"(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)")),
    ("TOKEN", re.compile(r"\b(?:sk|pk|ghp|xox[abp])[-_][A-Za-z0-9_-]{10,}\b")),
    ("SIGNED_URL", re.compile(r"https?://\S*(?:X-Goog-Signature|X-Amz-Signature|sig=)\S*")),
]


def redact(text: str) -> str:
    for label, pattern in _PATTERNS:
        text = pattern.sub(f"[REDACTED_{label}]", text)
    return text


def find_leaks(text: str) -> list[str]:
    """Labels of sensitive patterns present in ``text`` (used by evals and output checks)."""
    return [label for label, pattern in _PATTERNS if pattern.search(text)]
