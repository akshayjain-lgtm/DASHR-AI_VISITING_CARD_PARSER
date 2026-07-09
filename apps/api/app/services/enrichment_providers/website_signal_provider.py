from dataclasses import dataclass
from typing import Protocol


@dataclass
class WebsiteSignalResult:
    """The company's own public website, scraped for text signals no
    structured source publishes: what it says it sells, and any mention of
    plant/facility scale. `plant_size_signal` is free text, not a number to
    compute on — no source exposes a structured "plant size" field."""

    product_lines_summary: str | None = None
    plant_size_signal: str | None = None
    raw_payload: dict | None = None


class WebsiteSignalProvider(Protocol):
    def lookup(self, website: str) -> WebsiteSignalResult: ...


class StubWebsiteSignalProvider:
    """Dev-only default: returns "no signal found"."""

    def lookup(self, website: str) -> WebsiteSignalResult:
        return WebsiteSignalResult()


def get_website_signal_provider() -> WebsiteSignalProvider:
    return StubWebsiteSignalProvider()
