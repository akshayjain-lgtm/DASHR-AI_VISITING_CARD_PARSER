"""Best-effort share-price QOQ lookup for lead-scoring v2's shared
expansion/revenue-growth distress override (see
.claude/specs/10-lead-scoring.md "v2 rework only"). Most trade-show leads
are small private companies with no listed shares at all — that's the
common, expected, non-error case, and contributes nothing extra to
scoring (see spec: "average, no penalty" for an unlisted company).

Deliberately honest scope: there is no real financial-data API integration
here, only text-extraction over free public search results. This will
correctly return "figure unknown" far more often than it returns a number
— that's the accurate answer for the inputs available, not a bug.
"""
import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from app.core.config import settings
from app.services.enrichment_providers.news_summary_provider import (
    _article_matches_company,
    _search_news_rss,
)

logger = logging.getLogger(__name__)

_QOQ_DECLINE_WORDS = ("down", "fell", "declined", "dropped", "slipped", "tumbled")
_QOQ_GROWTH_WORDS = ("up", "rose", "gained", "surged", "jumped", "rallied")
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def _extract_qoq_percentage(text: str) -> Decimal | None:
    """Only extracts a figure when a clear numeric change with an explicit
    direction word is present — a bare unqualified percentage is too
    ambiguous to sign and returns None rather than guessing."""
    match = _PERCENT_RE.search(text)
    if not match:
        return None
    value = Decimal(match.group(1))
    normalized = text.lower()
    if any(word in normalized for word in _QOQ_DECLINE_WORDS):
        return -value
    if any(word in normalized for word in _QOQ_GROWTH_WORDS):
        return value
    return None


@dataclass
class SharePriceResult:
    is_publicly_listed: bool = False
    qoq_growth_pct: Decimal | None = None
    raw_payload: dict | None = None


class SharePriceProvider(Protocol):
    def lookup(self, company_name: str, hq_city: str | None = None) -> SharePriceResult: ...


class StubSharePriceProvider:
    """Dev-only default: returns "not listed"."""

    def lookup(self, company_name: str, hq_city: str | None = None) -> SharePriceResult:
        return SharePriceResult()


class RealSharePriceProvider:
    def lookup(self, company_name: str, hq_city: str | None = None) -> SharePriceResult:
        items = _search_news_rss(f"{company_name} share price")
        verified = [
            item for item in items if _article_matches_company(company_name, hq_city, item["headline"])
        ]
        if not verified:
            return SharePriceResult()

        percentage = _extract_qoq_percentage(verified[0]["headline"])
        # An identity-verified share-price result was found even if no
        # extractable percentage — treat as "listed, figure unknown", not
        # "not listed"; those are genuinely different facts.
        return SharePriceResult(
            is_publicly_listed=True,
            qoq_growth_pct=percentage,
            raw_payload={
                "query": f"{company_name} share price",
                "matched_headline": verified[0]["headline"],
            },
        )


def get_share_price_provider() -> SharePriceProvider:
    if settings.environment == "test":
        return StubSharePriceProvider()
    return RealSharePriceProvider()
