from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


@dataclass
class GemTenderResult:
    """GeM (Government e-Marketplace) portal's public tender/bid-history
    search for this company."""

    gem_tender_count: int | None = None
    gem_total_tender_value: Decimal | None = None
    raw_payload: dict | None = None


class GemTenderProvider(Protocol):
    def lookup(self, company_name: str) -> GemTenderResult: ...


class StubGemTenderProvider:
    """Default: returns "no signal found". Confirmed (not assumed) blocked:
    `curl` to gem.gov.in gets "Connection refused" outright (no HTTP
    response at all) from this deployment's network — likely a geo/IP
    restriction rather than anything fixable with different request
    headers. Revisit if deployed from a network this site doesn't block."""

    def lookup(self, company_name: str) -> GemTenderResult:
        return GemTenderResult()


def get_gem_tender_provider() -> GemTenderProvider:
    return StubGemTenderProvider()
