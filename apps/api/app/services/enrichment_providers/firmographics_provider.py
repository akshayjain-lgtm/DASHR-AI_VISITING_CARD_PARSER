from dataclasses import dataclass
from typing import Protocol


@dataclass
class FirmographicsResult:
    """Public LinkedIn company-page lookup. LinkedIn renders company pages
    client-side, so a real implementation needs a headless browser, not a
    plain HTTP GET — out of scope for this pass (see spec Overview)."""

    linkedin_employee_count: int | None = None
    linkedin_follower_count: int | None = None
    raw_payload: dict | None = None


class FirmographicsProvider(Protocol):
    def lookup_linkedin(self, company_name: str, website: str | None) -> FirmographicsResult: ...


class StubFirmographicsProvider:
    """Dev-only default: returns "no signal found"."""

    def lookup_linkedin(self, company_name: str, website: str | None) -> FirmographicsResult:
        return FirmographicsResult()


def get_firmographics_provider() -> FirmographicsProvider:
    return StubFirmographicsProvider()
