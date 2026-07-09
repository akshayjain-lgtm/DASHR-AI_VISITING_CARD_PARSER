from dataclasses import dataclass
from typing import Protocol


@dataclass
class GstinVerificationResult:
    """GST portal's public taxpayer-search result for one GSTIN."""

    gstin_verified: bool | None = None
    gstin_status: str | None = None
    raw_payload: dict | None = None


@dataclass
class UdyamResult:
    """Udyam/MSME public registration-search result. `udyam_category` is
    one of "micro"/"small"/"medium" — Udyam's own turnover-threshold-based
    classification, which also doubles as a free public revenue-band proxy
    (see `enrichment_service.classify_revenue_band`)."""

    udyam_registered: bool | None = None
    udyam_category: str | None = None
    raw_payload: dict | None = None


class ComplianceProvider(Protocol):
    def verify_gstin(self, gstin: str) -> GstinVerificationResult: ...
    def lookup_udyam(self, company_name: str, gstin: str | None) -> UdyamResult: ...


class StubComplianceProvider:
    """Default: returns "no signal found" for both lookups.

    `verify_gstin` — GST portal's public taxpayer search is captcha-gated
    in practice (see spec Overview); not automatable with a plain HTTP
    client.

    `lookup_udyam` — confirmed (not assumed) infeasible, and for a
    different reason than a network block: udyamregistration.gov.in is
    reachable (HTTP 200 with a browser User-Agent), but its public "Verify
    Udyam Registration" tool only verifies a *given* Udyam/UAM number — it
    has no company-name search at all. Without already knowing a company's
    Udyam number (which we don't), there's nothing to look up here.
    """

    def verify_gstin(self, gstin: str) -> GstinVerificationResult:
        return GstinVerificationResult()

    def lookup_udyam(self, company_name: str, gstin: str | None) -> UdyamResult:
        return UdyamResult()


def get_compliance_provider() -> ComplianceProvider:
    return StubComplianceProvider()
