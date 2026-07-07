"""Static seniority keyword table + classifier.

Kept as data (same rationale as CLAUDE.md's scoring-weights rule) so
06-lead-scoring's seniority criterion can reuse `classify()` without
re-deriving levels from raw job titles.
"""

_SENIORITY_KEYWORDS: dict[str, list[str]] = {
    "c_level": [
        "ceo", "cfo", "coo", "cto", "cmo", "chief", "founder", "co-founder",
        "president", "managing director", "proprietor", "owner", "partner",
    ],
    "director": ["director", "vp", "vice president", "head of", "general manager"],
    "manager": ["manager", "team lead", "supervisor", "senior engineer", "senior"],
}
_FALLBACK_LEVEL = "individual_contributor"
_LEVELS_IN_PRIORITY_ORDER = ("c_level", "director", "manager")


def classify(job_title: str | None) -> str | None:
    """Returns None only when job_title itself is missing/blank; otherwise one
    of "c_level"/"director"/"manager"/"individual_contributor", checked in
    that priority order so e.g. "Senior Manager" resolves to "manager", not
    misfired on "senior" alone."""
    if not job_title or not job_title.strip():
        return None
    normalized = job_title.strip().lower()
    for level in _LEVELS_IN_PRIORITY_ORDER:
        if any(keyword in normalized for keyword in _SENIORITY_KEYWORDS[level]):
            return level
    return _FALLBACK_LEVEL
