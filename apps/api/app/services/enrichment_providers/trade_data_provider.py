from dataclasses import dataclass
from typing import Protocol


@dataclass
class TradeDataResult:
    """The free teaser numbers on Volza's/ImportGenius's public
    company-profile pages (e.g. "X shipments in the last 12 months") — the
    detailed, itemized shipment records both platforms sell are out of
    scope. `source_tag` records which of the two answered."""

    import_export_activity: bool | None = None
    shipment_count_last_12m: int | None = None
    source_tag: str | None = None
    raw_payload: dict | None = None


class TradeDataProvider(Protocol):
    def lookup(self, company_name: str) -> TradeDataResult: ...


class StubTradeDataProvider:
    """Dev-only default: returns "no signal found"."""

    def lookup(self, company_name: str) -> TradeDataResult:
        return TradeDataResult()


def get_trade_data_provider() -> TradeDataProvider:
    return StubTradeDataProvider()
