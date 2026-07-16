from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.config import settings
from app.deps import get_current_user, get_db
from app.models.user import User
from app.schemas.wallet import (
    FreeActionsRemaining,
    WalletOut,
    WalletRechargeOut,
    WalletRechargeRequest,
    WalletTransactionOut,
)
from app.services import billing, payments
from app.services.exceptions import (
    InvalidRechargeAmountError,
    InvalidRechargeRequestError,
    PaymentProviderError,
)

router = APIRouter(prefix="/wallet", tags=["wallet"])


@router.get("", response_model=WalletOut)
def get_wallet(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    wallet = billing.get_wallet(db, user.user_id)
    transactions = billing.list_transactions(db, user.user_id, limit=20, offset=0)
    free_actions_remaining = FreeActionsRemaining(
        **{
            action_type: billing.get_free_actions_remaining(db, user.user_id, action_type)
            for action_type in billing.ACTION_TYPES
        }
    )
    return WalletOut(
        balance_inr=wallet.balance_inr,
        transactions=[WalletTransactionOut.model_validate(t) for t in transactions],
        free_actions_remaining=free_actions_remaining,
    )


@router.get("/transactions", response_model=list[WalletTransactionOut])
def list_wallet_transactions(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    transactions = billing.list_transactions(db, user.user_id, limit=limit, offset=offset)
    return [WalletTransactionOut.model_validate(t) for t in transactions]


@router.post("/recharge", response_model=WalletRechargeOut)
def recharge_wallet(
    data: WalletRechargeRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # No rate limiting on this endpoint yet — each call hits the live
    # Razorpay Orders API, so an authenticated user could spam order
    # creation. Left as a follow-up since this codebase has no rate-limiting
    # infrastructure yet to hook into; not blocking for this feature.
    try:
        order = payments.create_recharge_order(db, user, data.amount_inr)
    except InvalidRechargeAmountError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except InvalidRechargeRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except PaymentProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return WalletRechargeOut(
        razorpay_order_id=order["id"],
        razorpay_key_id=settings.razorpay_key_id,
        amount_inr=data.amount_inr,
    )
