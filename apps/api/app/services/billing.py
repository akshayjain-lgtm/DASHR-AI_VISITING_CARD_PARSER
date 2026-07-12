"""Wallet ledger primitives. Wallet.balance_inr is never assigned outside
this module, and every change here is preceded (in the same DB transaction)
by a WalletTransaction insert, so the ledger is always the source of truth
and the cached balance is always derivable from it (CLAUDE.md).

Every function here is scoped to one user_id at a time and takes no org_id
— Wallet/WalletTransaction are User-scoped, not tenant-scoped, unlike most
tables in this codebase (CLAUDE.md: no shared org wallet, no admin spending
authority over a sub-user's wallet).
"""
import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.pricing_rate import PricingRate
from app.models.wallet import Wallet
from app.models.wallet_transaction import WalletTransaction
from app.services.exceptions import InsufficientBalanceError


def _get_or_create_wallet(db: Session, user_id: uuid.UUID, *, lock: bool) -> Wallet:
    stmt = select(Wallet).where(Wallet.user_id == user_id)
    if lock:
        stmt = stmt.with_for_update()
    wallet = db.scalar(stmt)
    if wallet is not None:
        return wallet

    wallet = Wallet(user_id=user_id)
    db.add(wallet)
    try:
        db.commit()
    except IntegrityError:
        # Lost a race with a concurrent first-ever wallet creation for this
        # user (unique constraint on user_id) — the other insert won, so
        # fall back to reading the row it just created.
        db.rollback()
        stmt = select(Wallet).where(Wallet.user_id == user_id)
        if lock:
            stmt = stmt.with_for_update()
        wallet = db.scalar(stmt)
    else:
        db.refresh(wallet)
    return wallet


def get_wallet(db: Session, user_id: uuid.UUID) -> Wallet:
    """Lazily creates the caller's wallet with a zero balance on first use,
    without taking a row lock. Safe for read paths (GET /wallet, get_balance)
    since nothing here mutates balance_inr — locking every balance read
    against concurrent credit_wallet/debit_wallet calls would be needless
    contention. credit_wallet/debit_wallet use _lock_or_create_wallet
    instead, which actually needs the lock."""
    return _get_or_create_wallet(db, user_id, lock=False)


def _lock_or_create_wallet(db: Session, user_id: uuid.UUID) -> Wallet:
    """Locks the caller's wallet row for the rest of this DB transaction
    (SELECT ... FOR UPDATE) if it already exists, creating it with a zero
    balance on first use otherwise. Only credit_wallet/debit_wallet call
    this, so concurrent mutations for the same user always serialize on this
    row lock rather than racing (CLAUDE.md: concurrent bulk uploads by the
    same user must never be able to overdraw their wallet)."""
    return _get_or_create_wallet(db, user_id, lock=True)


def get_balance(db: Session, user_id: uuid.UUID) -> Decimal:
    return get_wallet(db, user_id).balance_inr


def list_transactions(
    db: Session, user_id: uuid.UUID, limit: int = 50, offset: int = 0
) -> list[WalletTransaction]:
    stmt = (
        select(WalletTransaction)
        .where(WalletTransaction.user_id == user_id)
        .order_by(WalletTransaction.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(db.scalars(stmt))


def credit_wallet(
    db: Session,
    user_id: uuid.UUID,
    amount_inr: Decimal,
    transaction_type: str,
    *,
    razorpay_order_id: str | None = None,
    razorpay_payment_id: str | None = None,
) -> WalletTransaction:
    """Only ever called from the Razorpay webhook handler once the payment
    signature is verified — never from a client-facing recharge response
    (CLAUDE.md: a wallet is only credited on a signature-verified webhook,
    never a client-side callback)."""
    wallet = _lock_or_create_wallet(db, user_id)
    wallet.balance_inr = wallet.balance_inr + amount_inr
    transaction = WalletTransaction(
        user_id=user_id,
        wallet_id=wallet.wallet_id,
        transaction_type=transaction_type,
        amount_inr=amount_inr,
        balance_after_inr=wallet.balance_inr,
        razorpay_order_id=razorpay_order_id,
        razorpay_payment_id=razorpay_payment_id,
    )
    db.add(transaction)
    db.commit()
    db.refresh(transaction)
    return transaction


def debit_wallet(
    db: Session,
    user_id: uuid.UUID,
    amount_inr: Decimal,
    transaction_type: str,
    *,
    reference_id: uuid.UUID | None = None,
) -> WalletTransaction:
    """Not called by any router yet — parse/enrich/score actions aren't
    wired to debit a wallet in this feature (a future step). Implemented
    now, sharing _lock_or_create_wallet's row-lock, so that future wiring
    needs no changes to this module's transactional logic."""
    wallet = _lock_or_create_wallet(db, user_id)
    if wallet.balance_inr < amount_inr:
        raise InsufficientBalanceError(
            f"Wallet balance {wallet.balance_inr} is less than the requested debit {amount_inr}"
        )
    wallet.balance_inr = wallet.balance_inr - amount_inr
    transaction = WalletTransaction(
        user_id=user_id,
        wallet_id=wallet.wallet_id,
        transaction_type=transaction_type,
        amount_inr=-amount_inr,
        balance_after_inr=wallet.balance_inr,
        reference_id=reference_id,
    )
    db.add(transaction)
    db.commit()
    db.refresh(transaction)
    return transaction


def get_current_rate(db: Session, action_type: str) -> Decimal:
    """Reads the currently-effective PricingRate for action_type — never
    hardcoded inline, mirroring CLAUDE.md's scoring-weights rule.

    Filters on effective_from <= now() so a future-dated rate (inserted
    ahead of a scheduled price change) only takes effect once its date
    actually arrives, rather than immediately outranking the current rate
    by sort order alone."""
    stmt = (
        select(PricingRate)
        .where(PricingRate.action_type == action_type, PricingRate.effective_from <= func.now())
        .order_by(PricingRate.effective_from.desc())
        .limit(1)
    )
    rate = db.scalar(stmt)
    if rate is None:
        raise ValueError(f"No pricing rate configured for action_type={action_type!r}")
    return rate.rate_inr
