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

from app.models.free_action_allowance import FreeActionAllowance
from app.models.pricing_rate import PricingRate
from app.models.wallet import Wallet
from app.models.wallet_transaction import WalletTransaction
from app.services.exceptions import InsufficientBalanceError

# The three billable action types, each with its own independent rate and
# free allowance (CLAUDE.md: never a shared/blended pool). Single source of
# truth so callers (routers/wallet.py's GET /wallet handler, in particular)
# never hand-maintain their own copy of this list.
ACTION_TYPES = ("parse", "enrichment", "scoring")


def _get_or_create_wallet(
    db: Session, user_id: uuid.UUID, *, lock: bool, commit: bool = True
) -> Wallet:
    """commit=False flushes the lazily-created row instead of committing it
    — used by charge_for_action/charge_for_bulk_action, which stage other
    changes (an allowance increment, sometimes a caller's own staged card
    mutation — see reprocess_card) in the SAME transaction as this lazy
    creation. Committing here would end that transaction early and persist
    those other staged changes before the caller has decided whether the
    charge actually succeeds, defeating the point of one atomic commit (or
    rollback) covering the whole sequence."""
    stmt = select(Wallet).where(Wallet.user_id == user_id)
    if lock:
        stmt = stmt.with_for_update()
    wallet = db.scalar(stmt)
    if wallet is not None:
        return wallet

    wallet = Wallet(user_id=user_id)
    db.add(wallet)
    try:
        if commit:
            db.commit()
        else:
            db.flush()
    except IntegrityError:
        # Lost a race with a concurrent first-ever wallet creation for this
        # user (unique constraint on user_id) — the other insert won, so
        # fall back to reading the row it just created. Rolls back the
        # whole transaction either way, same as the commit=True path.
        db.rollback()
        stmt = select(Wallet).where(Wallet.user_id == user_id)
        if lock:
            stmt = stmt.with_for_update()
        wallet = db.scalar(stmt)
    else:
        # Refresh either way (not just when commit=True) — this reads the
        # just-flushed row back within the same still-open transaction, so
        # server-generated defaults (wallet_id, balance_inr) are populated
        # regardless of whether the row is committed yet.
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


def _lock_or_create_wallet(db: Session, user_id: uuid.UUID, *, commit: bool = True) -> Wallet:
    """Locks the caller's wallet row for the rest of this DB transaction
    (SELECT ... FOR UPDATE) if it already exists, creating it with a zero
    balance on first use otherwise. credit_wallet/debit_wallet/refund_action
    use the default commit=True (nothing else is staged in their sessions at
    that point); charge_for_action/charge_for_bulk_action pass commit=False
    (see _get_or_create_wallet's docstring) since they stage further changes
    in the same transaction as this lazy creation. Concurrent mutations for
    the same user always serialize on this row lock rather than racing
    (CLAUDE.md: concurrent bulk uploads by the same user must never be able
    to overdraw their wallet)."""
    return _get_or_create_wallet(db, user_id, lock=True, commit=commit)


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
    """Not called by card_service — charge_for_action/charge_for_bulk_action
    inline their own lock-debit-ledger sequence instead of calling this, so
    each can commit the allowance increment together with the wallet debit
    and ledger insert as one atomic step (see their docstrings). Exercised
    directly by test_14_wallet_recharge.py's debit-primitive tests, and
    kept as the primitive a future non-card debit path (e.g. a support
    adjustment) could still reuse."""
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


def _get_current_pricing_rate(db: Session, action_type: str) -> PricingRate:
    """Reads the currently-effective PricingRate row for action_type — never
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
    return rate


def get_current_rate(db: Session, action_type: str) -> Decimal:
    return _get_current_pricing_rate(db, action_type).rate_inr


def get_free_limit(db: Session, action_type: str) -> int:
    return _get_current_pricing_rate(db, action_type).free_limit


def _get_or_create_allowance(
    db: Session, user_id: uuid.UUID, action_type: str, *, lock: bool, commit: bool = True
) -> FreeActionAllowance:
    """commit=False mirrors _get_or_create_wallet's same parameter — flushes
    the lazily-created row instead of committing it, so a caller staging
    further changes in the same transaction (charge_for_action/
    charge_for_bulk_action) decides the whole sequence's fate with one
    final commit or rollback, rather than this lazy creation ending the
    transaction early on its own."""
    stmt = select(FreeActionAllowance).where(
        FreeActionAllowance.user_id == user_id, FreeActionAllowance.action_type == action_type
    )
    if lock:
        stmt = stmt.with_for_update()
    allowance = db.scalar(stmt)
    if allowance is not None:
        return allowance

    allowance = FreeActionAllowance(user_id=user_id, action_type=action_type)
    db.add(allowance)
    try:
        if commit:
            db.commit()
        else:
            db.flush()
    except IntegrityError:
        # Lost a race with a concurrent first-ever allowance row for this
        # (user_id, action_type) pair (unique constraint) — fall back to
        # reading the row the other insert just created, mirroring
        # _get_or_create_wallet's same race handling.
        db.rollback()
        stmt = select(FreeActionAllowance).where(
            FreeActionAllowance.user_id == user_id, FreeActionAllowance.action_type == action_type
        )
        if lock:
            stmt = stmt.with_for_update()
        allowance = db.scalar(stmt)
    else:
        db.refresh(allowance)
    return allowance


def get_free_actions_remaining(db: Session, user_id: uuid.UUID, action_type: str) -> int:
    allowance = _get_or_create_allowance(db, user_id, action_type, lock=False)
    free_limit = get_free_limit(db, action_type)
    return max(free_limit - allowance.used_count, 0)


def _lock_allowance_and_rate(
    db: Session, user_id: uuid.UUID, action_type: str, *, commit: bool = True
) -> tuple[FreeActionAllowance, PricingRate]:
    """Shared first step of charge_for_action/charge_for_bulk_action: lock
    the caller's (user_id, action_type) FreeActionAllowance row and read the
    currently-effective PricingRate. Locking the allowance row here (before
    either function decides free-vs-paid) is what makes concurrent charges
    for the same user/action_type serialize instead of racing. Both callers
    pass commit=False (see _get_or_create_allowance's docstring)."""
    allowance = _get_or_create_allowance(db, user_id, action_type, lock=True, commit=commit)
    rate = _get_current_pricing_rate(db, action_type)
    return allowance, rate


def charge_for_action(
    db: Session,
    user_id: uuid.UUID,
    action_type: str,
    *,
    reference_id: uuid.UUID | None = None,
) -> bool:
    """The single entry point card_service uses to gate + pay for one
    parse/enrichment/scoring action. Must be called before any Celery
    .delay() for that action, and before any DB mutation to the card's own
    state that a failure here couldn't cleanly undo (CLAUDE.md: the balance
    check happens ahead of the OCR/enrichment/scoring call, never after).

    Locks the caller's (user_id, action_type) FreeActionAllowance row first,
    then — only if that type's free allowance is already exhausted — the
    wallet row, always in that same order so concurrent charges for the same
    user never deadlock.

    Returns True if this action was billed from the wallet, False if it was
    covered by the free allowance. Raises InsufficientBalanceError, leaving
    used_count/balance untouched, if the free allowance is exhausted and the
    wallet can't cover action_type's current rate.

    Passes commit=False to the allowance/wallet lazy-creation lookups: some
    callers (reprocess_card) stage their own card-state change in this same
    session before calling this function, expecting it to land in the same
    atomic commit (or be rolled back together on InsufficientBalanceError).
    If a first-ever allowance/wallet row's own lazy creation committed
    early, it would silently persist that staged change ahead of knowing
    whether the charge actually succeeds.
    """
    allowance, rate = _lock_allowance_and_rate(db, user_id, action_type, commit=False)

    if allowance.used_count < rate.free_limit:
        allowance.used_count += 1
        db.commit()
        return False

    wallet = _lock_or_create_wallet(db, user_id, commit=False)
    if wallet.balance_inr < rate.rate_inr:
        # Nothing was mutated yet — roll back to release both row locks
        # cleanly rather than leaving an open transaction.
        db.rollback()
        raise InsufficientBalanceError(
            f"Wallet balance {wallet.balance_inr} is less than the rate {rate.rate_inr} "
            f"for action_type={action_type!r}"
        )

    wallet.balance_inr = wallet.balance_inr - rate.rate_inr
    transaction = WalletTransaction(
        user_id=user_id,
        wallet_id=wallet.wallet_id,
        transaction_type=f"{action_type}_debit",
        amount_inr=-rate.rate_inr,
        balance_after_inr=wallet.balance_inr,
        reference_id=reference_id,
    )
    db.add(transaction)
    allowance.used_count += 1
    # Allowance increment, balance decrement, and ledger insert all commit
    # together — never call debit_wallet() here, since it commits on its own
    # and would split this into two transactions, risking a wallet debit
    # with no matching allowance update if the second commit failed.
    db.commit()
    return True


def refund_action(
    db: Session,
    user_id: uuid.UUID,
    action_type: str,
    *,
    billed: bool,
    reference_id: uuid.UUID | None = None,
) -> None:
    """Reverses exactly one charge_for_action call whose action never
    actually ran at all — e.g. the Celery .delay() call itself raised, so
    the work was paid for but never even queued. Distinct from (and never
    used for) a task that WAS successfully enqueued and later failed during
    execution, e.g. a bad OCR read — that stays non-refundable/prepaid-
    spend-only per CLAUDE.md, since the work was genuinely attempted.

    Decrements FreeActionAllowance.used_count by 1 (floored at 0) and, if
    `billed` (the original charge_for_action call returned True, i.e. it was
    paid from the wallet rather than free), credits the wallet back the
    current rate via a `{action_type}_refund` WalletTransaction — all in one
    commit, mirroring charge_for_action's own atomicity.
    """
    allowance = _get_or_create_allowance(db, user_id, action_type, lock=True)
    allowance.used_count = max(allowance.used_count - 1, 0)

    if billed:
        rate = _get_current_pricing_rate(db, action_type)
        wallet = _lock_or_create_wallet(db, user_id)
        wallet.balance_inr = wallet.balance_inr + rate.rate_inr
        db.add(
            WalletTransaction(
                user_id=user_id,
                wallet_id=wallet.wallet_id,
                transaction_type=f"{action_type}_refund",
                amount_inr=rate.rate_inr,
                balance_after_inr=wallet.balance_inr,
                reference_id=reference_id,
                quantity=1,
            )
        )
    db.commit()


def charge_for_bulk_action(
    db: Session,
    user_id: uuid.UUID,
    action_type: str,
    count: int,
    *,
    reference_id: uuid.UUID | None = None,
) -> tuple[int, int]:
    """Bulk counterpart to charge_for_action — used by card_service's
    enqueue_processing/enqueue_enrichment/enqueue_scoring so a batch of N
    cards produces at most one collective WalletTransaction (quantity=N)
    instead of one ledger row per card, keeping transaction history
    readable for a large batch.

    Consumes the free allowance first, same as charge_for_action, then bills
    however many of the remaining `count` actions the wallet can actually
    afford as a single row (amount_inr = rate * quantity). Never raises —
    returns `(free_used, paid_used)`, so the caller can enqueue Celery work
    for only the first `free_used + paid_used` of its `count` candidates
    (treating the rest as wallet-blocked) *and* know, per card by list
    position, whether that specific card's charge was free or billed — the
    first `free_used` of the chargeable subset were free, the remaining
    `paid_used` were billed. This lets card_service pass an accurate
    `billed` flag into each Celery task for later refund-on-task-failure,
    without needing a separate ledger row per card to reconstruct it from.

    `reference_id` is only ever stored when exactly one action is billed
    (`paid_used == 1`) — a genuinely collective row covering more than one
    card has no single card to point at, so its reference_id is always
    NULL; use `quantity` to see how many actions one row covers.
    """
    if count <= 0:
        return 0, 0

    allowance, rate = _lock_allowance_and_rate(db, user_id, action_type, commit=False)

    free_available = max(rate.free_limit - allowance.used_count, 0)
    free_used = min(free_available, count)
    remaining = count - free_used

    if remaining == 0:
        allowance.used_count += free_used
        db.commit()
        return free_used, 0

    wallet = _lock_or_create_wallet(db, user_id, commit=False)
    affordable = min(remaining, int(wallet.balance_inr // rate.rate_inr))
    charged_total = free_used + affordable

    if affordable > 0:
        total_amount = rate.rate_inr * affordable
        wallet.balance_inr = wallet.balance_inr - total_amount
        db.add(
            WalletTransaction(
                user_id=user_id,
                wallet_id=wallet.wallet_id,
                transaction_type=f"{action_type}_debit",
                amount_inr=-total_amount,
                balance_after_inr=wallet.balance_inr,
                reference_id=reference_id if affordable == 1 else None,
                quantity=affordable,
            )
        )

    allowance.used_count += charged_total
    db.commit()
    return free_used, affordable
