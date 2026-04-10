"""Role-family helpers for precision-first sourcing and matching."""

from __future__ import annotations

import re
from typing import Iterable

ROLE_FAMILY_PM_CORE = "pm_core"
ROLE_FAMILY_TPM_PGM = "tpm_pgm"
ROLE_FAMILY_ADJACENT = "adjacent"
ROLE_FAMILY_OTHER = "other"

DEFAULT_ALLOWED_ROLE_FAMILIES = [ROLE_FAMILY_PM_CORE, ROLE_FAMILY_TPM_PGM]
DEFAULT_EXCLUDED_TITLE_KEYWORDS = [
    "project manager",
    "project lead",
    "scrum master",
    "business analyst",
    "business operations",
    "bizops",
    "chief of staff",
    "growth manager",
    "growth marketing",
    "customer success",
    "account manager",
    "sales",
    "intern",
    "fresher",
]

PM_CORE_PATTERNS = [
    r"\bgroup product manager\b",
    r"\bsenior product manager\b",
    r"\blead product manager\b",
    r"\bprincipal product manager\b",
    r"\bproduct manager\b",
    r"\bproduct owner\b",
    r"\bhead of product\b",
    r"\bdirector of product\b",
    r"\bproduct lead\b",
]

TPM_PGM_PATTERNS = [
    r"\btechnical program manager\b",
    r"\btechnical programme manager\b",
    r"\btpm\b",
    r"\bprogram manager\b",
    r"\bprogramme manager\b",
    r"\bpgm\b",
]

ADJACENT_PATTERNS = [
    r"\bproject manager\b",
    r"\bgrowth product manager\b",
    r"\bgrowth manager\b",
    r"\bbusiness operations\b",
    r"\bstrategy manager\b",
    r"\bchief of staff\b",
]

PLACEHOLDER_PATTERNS = [
    r"^job$",
    r"^role$",
    r"^opening$",
    r"^details$",
    r"^view details$",
    r"^apply now$",
    r"^linkedin$",
    r"^naukri$",
    r"^foundit$",
    r"^wellfound$",
]


def normalize_text(value: str) -> str:
    """Normalize text for matching and classification."""
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def classify_role_family(title: str, description: str = "") -> str:
    """Classify a role title into the supported sourcing/matching families."""
    text = normalize_text(f"{title} {description}")
    if not text:
        return ROLE_FAMILY_OTHER

    if _matches_any(text, PM_CORE_PATTERNS):
        return ROLE_FAMILY_PM_CORE
    if _matches_any(text, TPM_PGM_PATTERNS):
        return ROLE_FAMILY_TPM_PGM
    if _matches_any(text, ADJACENT_PATTERNS):
        return ROLE_FAMILY_ADJACENT
    return ROLE_FAMILY_OTHER


def is_excluded_title(title: str, excluded_keywords: Iterable[str] | None = None) -> bool:
    """Check whether the title contains any explicitly excluded keywords."""
    normalized = normalize_text(title)
    if not normalized:
        return False

    keywords = list(excluded_keywords or DEFAULT_EXCLUDED_TITLE_KEYWORDS)
    return any(normalize_text(keyword) in normalized for keyword in keywords if keyword)


def is_allowed_role(
    title: str,
    description: str = "",
    allowed_families: Iterable[str] | None = None,
    excluded_keywords: Iterable[str] | None = None,
) -> bool:
    """Return True when a role is in-scope for precision-first ingestion."""
    if is_excluded_title(title, excluded_keywords=excluded_keywords):
        return False

    allowed = set(allowed_families or DEFAULT_ALLOWED_ROLE_FAMILIES)
    return classify_role_family(title, description) in allowed


def is_placeholder_text(value: str) -> bool:
    """Detect obviously placeholder-quality scraped text."""
    normalized = normalize_text(value)
    if not normalized:
        return True
    if len(normalized) < 3:
        return True
    return _matches_any(normalized, PLACEHOLDER_PATTERNS)


def _matches_any(text: str, patterns: Iterable[str]) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)
