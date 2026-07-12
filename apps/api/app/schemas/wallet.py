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
    created_at: datetime


class WalletOut(BaseModel):
    balance_inr: Decimal
    currency: Literal["INR"] = "INR"
    # Most recent 20 transactions — GET /wallet/transactions is the
    # paginated full-ledger endpoint.
    transactions: list[WalletTransactionOut]


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
    amount_inr: Decimal
    currency: Literal["INR"] = "INR"
