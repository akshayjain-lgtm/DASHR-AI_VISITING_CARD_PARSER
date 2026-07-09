import logging
from dataclasses import dataclass
from typing import Protocol
from xml.etree import ElementTree

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_NEWS_RSS_URL = "https://news.google.com/rss/search"
_REQUEST_TIMEOUT_SECONDS = 10
_MAX_ITEMS = 5

# Simple keyword buckets, checked in this priority order — named constants,
# not inline branches, so they're one place to tune later.
_SIGNAL_TYPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "funding": ("funding", "raises", "raised", "investment", "investor", "series a", "series b", "series c", "valuation"),
    "expansion": ("expansion", "expands", "expand", "new plant", "new unit", "acquire", "acquisition", "merger"),
    "new_facility": ("new facility", "inaugurat", "commissions", "commissioned", "new factory", "groundbreaking"),
}
_DEFAULT_SIGNAL_TYPE = "other"


def _classify_signal_type(headline: str) -> str:
    normalized = headline.lower()
    for signal_type, keywords in _SIGNAL_TYPE_KEYWORDS.items():
        if any(keyword in normalized for keyword in keywords):
            return signal_type
    return _DEFAULT_SIGNAL_TYPE


@dataclass
class NewsSignalResult:
    """Google News' public search RSS feed (no key required) for this
    company. Each item in `recent_news_signals` is expected to look like
    `{"headline": str, "url": str, "published_at": str, "signal_type":
    "funding" | "expansion" | "new_facility" | "other"}`."""

    recent_news_signals: list[dict] | None = None
    raw_payload: dict | None = None


class NewsSignalProvider(Protocol):
    def lookup(self, company_name: str) -> NewsSignalResult: ...


class StubNewsSignalProvider:
    """Dev-only default: returns "no signal found"."""

    def lookup(self, company_name: str) -> NewsSignalResult:
        return NewsSignalResult()


class RealNewsSignalProvider:
    """Queries Google News' public search RSS feed — a genuinely free,
    unauthenticated, server-rendered public source (confirmed reachable;
    unlike MCA/Zauba/GeM, nothing here requires a browser or hits a
    bot-detection challenge)."""

    def lookup(self, company_name: str) -> NewsSignalResult:
        response = httpx.get(
            _NEWS_RSS_URL,
            params={"q": company_name, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()

        root = ElementTree.fromstring(response.content)
        items = root.findall("./channel/item")[:_MAX_ITEMS]
        if not items:
            return NewsSignalResult()

        signals = []
        raw_items = []
        for item in items:
            headline = (item.findtext("title") or "").strip()
            url = (item.findtext("link") or "").strip()
            published_at = (item.findtext("pubDate") or "").strip()
            if not headline:
                continue
            signals.append(
                {
                    "headline": headline,
                    "url": url,
                    "published_at": published_at,
                    "signal_type": _classify_signal_type(headline),
                }
            )
            raw_items.append({"title": headline, "link": url, "pubDate": published_at})

        if not signals:
            return NewsSignalResult()

        return NewsSignalResult(
            recent_news_signals=signals,
            raw_payload={"query": company_name, "items": raw_items},
        )


def get_news_signal_provider() -> NewsSignalProvider:
    # Real by default — Google News RSS is a plain public feed, confirmed
    # reachable and requiring no key/browser. Tests always run with
    # ENVIRONMENT=test (see conftest.py) and get the stub instead, so the
    # suite never makes a real network call.
    if settings.environment == "test":
        return StubNewsSignalProvider()
    return RealNewsSignalProvider()
