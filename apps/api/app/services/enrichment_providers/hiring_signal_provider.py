from dataclasses import dataclass
from typing import Protocol


@dataclass
class HiringSignalResult:
    """Public Naukri job-search results and/or public LinkedIn job-search
    results for this company. `source_tag` records which sub-source
    actually answered ("naukri" or "linkedin_jobs")."""

    active_job_postings_count: int | None = None
    source_tag: str | None = None
    raw_payload: dict | None = None


class HiringSignalProvider(Protocol):
    def lookup(self, company_name: str) -> HiringSignalResult: ...


class StubHiringSignalProvider:
    """Dev-only default: returns "no signal found"."""

    def lookup(self, company_name: str) -> HiringSignalResult:
        return HiringSignalResult()


def get_hiring_signal_provider() -> HiringSignalProvider:
    return StubHiringSignalProvider()
