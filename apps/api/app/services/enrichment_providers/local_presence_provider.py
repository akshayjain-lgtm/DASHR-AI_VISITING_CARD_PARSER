from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


@dataclass
class PlacesResult:
    """Public Google Maps search results for this company (rating/review
    count). Google Maps renders results client-side, so a real
    implementation needs a headless browser, not a plain HTTP GET — out of
    scope for this pass (see spec Overview). This is a Maps page scrape,
    not the paid Google Places API."""

    google_rating: Decimal | None = None
    google_review_count: int | None = None
    raw_payload: dict | None = None


@dataclass
class MarketplaceResult:
    """A public IndiaMART/TradeIndia/JustDial listing for this company.
    `marketplace_located_in_industrial_area` is derived by pattern-matching
    the listing's address for industrial-estate markers (e.g. "MIDC",
    "GIDC", "Industrial Area", "SIDCO") — no directory exposes this as a
    field directly. `source_tag` records which directory answered."""

    marketplace_vintage_years: int | None = None
    marketplace_verified_badge: bool | None = None
    marketplace_located_in_industrial_area: bool | None = None
    source_tag: str | None = None
    raw_payload: dict | None = None


class LocalPresenceProvider(Protocol):
    def lookup_places(self, company_name: str, address: str | None) -> PlacesResult: ...
    def lookup_marketplace(self, company_name: str) -> MarketplaceResult: ...


class StubLocalPresenceProvider:
    """Dev-only default: returns "no signal found" for both lookups."""

    def lookup_places(self, company_name: str, address: str | None) -> PlacesResult:
        return PlacesResult()

    def lookup_marketplace(self, company_name: str) -> MarketplaceResult:
        return MarketplaceResult()


def get_local_presence_provider() -> LocalPresenceProvider:
    return StubLocalPresenceProvider()
