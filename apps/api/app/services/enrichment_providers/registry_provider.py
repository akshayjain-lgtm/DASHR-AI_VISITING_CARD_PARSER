from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol


@dataclass
class RegistryResult:
    """Corporate registry lookup — MCA public master-data search, falling
    back to Zauba Corp's public mirror when MCA's captcha blocks an
    automated lookup. `source_tag` records which of the two actually
    answered, since a caller can't know that in advance."""

    cin: str | None = None
    incorporation_date: date | None = None
    registry_status: str | None = None
    registered_address: str | None = None
    authorized_capital: Decimal | None = None
    paid_up_capital: Decimal | None = None
    source_tag: str | None = None
    raw_payload: dict | None = None


class RegistryProvider(Protocol):
    def lookup(self, company_name: str) -> RegistryResult: ...


class StubRegistryProvider:
    """Default: returns "no signal found". Confirmed (not assumed) blocked
    from this deployment's network — `curl` to both sources returns:
      - zaubacorp.com: HTTP 403 with `cf-mitigated: challenge` — an active
        Cloudflare Turnstile/JS challenge. Not solvable with a plain HTTP
        client, and headless-browser automation (Playwright) is frequently
        still flagged by Cloudflare as a bot on datacenter IPs, so this
        isn't a guaranteed fix even with more tooling.
      - mca.gov.in: flat HTTP 403 (WAF/IP block, no challenge page) —
        unclear if it's a datacenter-IP block or a stricter bot filter;
        either way, not solvable by changing request headers alone.
    Revisit if this app is ever deployed from a network/IP range these
    sites don't block, or a scraping-proxy service becomes an acceptable
    (likely paid) dependency — neither is true today.
    """

    def lookup(self, company_name: str) -> RegistryResult:
        return RegistryResult()


def get_registry_provider() -> RegistryProvider:
    return StubRegistryProvider()
