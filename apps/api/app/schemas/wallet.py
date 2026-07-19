import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WalletTransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    wallet_transaction_id: uuid.UUID
    transaction_type: str
    amount_inr: Decimal
    balance_after_inr: Decimal
    razorpay_order_id: str | None
    razorpay_payment_id: str | None
    reference_id: uuid.UUID | None
    # How many parse/enrich/score actions this row covers — 1 for a single
    # card, >1 for a collective bulk-batch debit (see charge_for_bulk_action).
    quantity: int
    created_at: datetime


class FreeActionsRemaining(BaseModel):
    """Each action type's own independent free-action count remaining,
    floored at 0 once exhausted — never a shared/blended pool (CLAUDE.md)."""

    parse: int
    enrichment: int
    scoring: int


class WalletOut(BaseModel):
    balance_inr: Decimal
    currency: Literal["INR"] = "INR"
    # Most recent 20 transactions — GET /wallet/transactions is the
    # paginated full-ledger endpoint.
    transactions: list[WalletTransactionOut]
    free_actions_remaining: FreeActionsRemaining


class WalletRechargeRequest(BaseModel):
    # Bounds must stay in sync with services/payments.py's
    # MIN_RECHARGE_AMOUNT_INR / MAX_RECHARGE_AMOUNT_INR — duplicated here
    # (rather than imported) because schemas stay dependency-free of
    # services in this codebase; payments.py re-checks the same bounds as
    # defense in depth against any caller that bypasses this schema.
    amount_inr: Decimal = Field(ge=100, le=500000)


class WalletRechargeOut(BaseModel):
    razorpay_order_id: str
    razorpay_key_id: str
    # Pre-tax amount that will be credited to the wallet on capture.
    net_amount_inr: Decimal
    cgst_amount_inr: Decimal
    sgst_amount_inr: Decimal
    # What the Razorpay Order actually charges (net + GST) — the frontend
    # must pass this (in paise) as the checkout widget's `amount`, since
    # that's the figure the created Order was actually opened for.
    gross_amount_inr: Decimal
    currency: Literal["INR"] = "INR"
