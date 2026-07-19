"""Razorpay integration: order creation for wallet recharge, and
signature-verified webhook handling that's the *only* path allowed to
credit a wallet (CLAUDE.md: a Razorpay payment is only ever considered
successful, and a wallet only credited, after webhook signature verification
server-side — never on the strength of a client-side redirect/callback).
"""
import logging
import uuid
from decimal import Decimal

import razorpay
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.user import User
from app.models.wallet_transaction import WalletTransaction
from app.services import billing, invoicing, razorpay_client
from app.services.exceptions import (
    InvalidRechargeAmountError,
    InvalidRechargeRequestError,
    MalformedWebhookPayloadError,
    PaymentProviderError,
    WebhookSignatureError,
)

logger = logging.getLogger(__name__)

MIN_RECHARGE_AMOUNT_INR = Decimal("100")
MAX_RECHARGE_AMOUNT_INR = Decimal("500000")


def _get_client() -> razorpay.Client:
    """Private seam over razorpay_client.get_client() — the same
    indirection vision_client._get_client() adds over
    anthropic_client.get_client(), and the exact function tests monkeypatch."""
    return razorpay_client.get_client()


def create_recharge_order(db: Session, user: User, amount_inr: Decimal) -> dict:
    """Creates a Razorpay Order for the requested amount. Deliberately does
    NOT credit anything — recharges are only ever credited by
    handle_payment_captured, once Razorpay's webhook confirms the payment
    with a verified signature. Does lazily create the caller's `wallets`
    row (at balance_inr=0, via billing.get_wallet — the same lazy-create
    GET /wallet uses) per spec: a wallet exists from a user's first GET
    /wallet *or* POST /wallet/recharge call, whichever comes first.

    Translates the Razorpay SDK's exception types into this codebase's
    domain exceptions here, at the service boundary — routers never import
    `razorpay` or know its exception taxonomy, matching how every other
    router in this codebase only ever catches app.services.exceptions types
    (e.g. archive_uploads.py catches CorruptArchiveError, never
    zipfile.BadZipFile)."""
    if amount_inr < MIN_RECHARGE_AMOUNT_INR or amount_inr > MAX_RECHARGE_AMOUNT_INR:
        raise InvalidRechargeAmountError(
            f"amount_inr must be between {MIN_RECHARGE_AMOUNT_INR} and {MAX_RECHARGE_AMOUNT_INR}"
        )
    billing.get_wallet(db, user.user_id)
    # GST is charged on top of the requested recharge amount — the wallet is
    # credited only amount_inr (see handle_payment_captured below), while
    # Razorpay actually collects amount_inr + GST. net_amount_inr travels in
    # the order's notes (alongside user_id) so the webhook can recover the
    # exact pre-tax figure to credit, independent of whatever gross amount
    # Razorpay reports as captured.
    _cgst, _sgst, gross_amount_inr = billing.compute_gst(amount_inr)
    amount_paise = int(gross_amount_inr * 100)
    try:
        return _get_client().order.create(
            {
                "amount": amount_paise,
                "currency": "INR",
                "notes": {"user_id": str(user.user_id), "net_amount_inr": str(amount_inr)},
            }
        )
    except razorpay.errors.BadRequestError as exc:
        # The SDK's own error text isn't authored by this app and may carry
        # account/config-specific detail — logged in full server-side, never
        # forwarded verbatim to the client.
        logger.warning("Razorpay rejected recharge order request: %s", exc)
        raise InvalidRechargeRequestError("Could not start recharge, please try again") from exc
    except (razorpay.errors.GatewayError, razorpay.errors.ServerError) as exc:
        logger.error("Razorpay order creation failed: %s", exc)
        raise PaymentProviderError("Payment provider error, please try again") from exc


def verify_webhook_signature(raw_body: bytes, signature: str | None) -> None:
    """Pure local HMAC verification against RAZORPAY_WEBHOOK_SECRET — no
    network call. Must run, and pass, before any DB write in the webhook
    handler.

    Explicitly rejects a blank secret rather than letting an HMAC keyed
    with "" silently "verify" against it — defense in depth alongside
    razorpay_client.guard_production_credentials()'s startup check, so a
    misconfigured deployment can't have its webhook forged even if that
    startup guard were ever bypassed or weakened."""
    if not signature:
        raise WebhookSignatureError("Missing X-Razorpay-Signature header")
    if not settings.razorpay_webhook_secret:
        raise WebhookSignatureError("RAZORPAY_WEBHOOK_SECRET is not configured")
    try:
        _get_client().utility.verify_webhook_signature(
            raw_body.decode(), signature, settings.razorpay_webhook_secret
        )
    except razorpay.errors.SignatureVerificationError as exc:
        raise WebhookSignatureError(str(exc)) from exc


def handle_payment_captured(db: Session, payload: dict) -> None:
    """Credits the paying user's wallet for a `payment.captured` webhook
    event. Idempotent on razorpay_order_id: a redelivered webhook for an
    already-credited order (Razorpay retries on any non-2xx response) is a
    silent no-op here, and is additionally guarded at the DB level by the
    partial unique index on wallet_transactions.razorpay_order_id.

    Two distinct "nothing to credit" outcomes, handled differently:
    - A `payment.captured` event missing/unparseable required fields is a
      genuine-but-unusable event (already signature-verified, so not a
      forgery attempt) — raises MalformedWebhookPayloadError, mapped to 400
      by the router. Never silently swallowed as a 200, never left to crash
      into an unhandled 500.
    - A well-formed event whose order_id isn't a real, fetchable order on
      this Razorpay account is a legitimate no-op — returns normally (200).
      This distinction matters because otherwise a validly-signed payload
      with a fabricated order_id and a plausible notes.user_id could credit
      a wallet for money never actually collected through
      POST /wallet/recharge; the order_id is only trusted once verified
      against Razorpay's own Orders API, not taken at face value from the
      payload alone.

    The already-credited pre-check SELECT below is check-then-act, not
    atomic — two concurrent redeliveries can both pass it before either
    commits. The unique index prevents an actual double-credit either way,
    so the IntegrityError catch around credit_wallet exists only to turn
    that race into a clean no-op instead of an unhandled 500 (which would
    otherwise make Razorpay retry indefinitely and could leave the session
    dirty for reuse on the same connection)."""
    payment = payload.get("payload", {}).get("payment", {}).get("entity", {})
    order_id = payment.get("order_id")
    payment_id = payment.get("id")
    # payment.amount is the GROSS, tax-inclusive figure Razorpay actually
    # captured (amount_inr + GST, per create_recharge_order above) — never
    # credited to the wallet directly. notes.net_amount_inr is the pre-tax
    # figure the user actually requested and what the wallet must be
    # credited; it's only ever used for logging/reconciliation below.
    gross_amount_paise = payment.get("amount")
    notes = payment.get("notes")
    user_id_raw = notes.get("user_id") if isinstance(notes, dict) else None
    net_amount_inr_raw = notes.get("net_amount_inr") if isinstance(notes, dict) else None

    if not order_id or not payment_id or gross_amount_paise is None or not user_id_raw or not net_amount_inr_raw:
        raise MalformedWebhookPayloadError(
            "payment.captured event is missing a required field "
            "(order_id/payment_id/amount/notes.user_id/notes.net_amount_inr)"
        )

    try:
        user_id = uuid.UUID(user_id_raw)
        gross_amount_inr_from_payload = Decimal(gross_amount_paise) / Decimal(100)
        net_amount_inr = Decimal(net_amount_inr_raw)
    except (ValueError, ArithmeticError, TypeError) as exc:
        raise MalformedWebhookPayloadError(
            f"Unusable payment.captured payload for order {order_id}: {exc}"
        ) from exc

    already_credited = db.scalar(
        select(WalletTransaction).where(WalletTransaction.razorpay_order_id == order_id)
    )
    if already_credited is not None:
        return

    try:
        order = _get_client().order.fetch(order_id)
    except razorpay.errors.BadRequestError:
        # Well-formed payload, but Razorpay doesn't recognize this
        # order_id on this account — nothing this server ever issued via
        # create_recharge_order, so nothing safe to credit. Legitimate 200
        # no-op, not a malformed-payload error.
        logger.warning("payment.captured referenced unknown order_id %s — no-op", order_id)
        return
    except (razorpay.errors.GatewayError, razorpay.errors.ServerError) as exc:
        # Transient failure verifying the order — must NOT silently drop a
        # possibly-legitimate credit. Propagating lets the router return a
        # 5xx so Razorpay's webhook retry policy gives this another try
        # once the transient issue clears, rather than losing the credit.
        logger.error("Razorpay order verification failed for %s: %s", order_id, exc)
        raise PaymentProviderError("Could not verify order with payment provider") from exc

    # Defense in depth: cross-check the payload's self-reported amount/notes
    # against the order record fetched fresh from Razorpay above (not
    # trusted from the payload alone). Not exploitable today — the whole
    # payload is already signature-verified — but guards against any future
    # change to how notes/amount are threaded (e.g. an order.update() call
    # added elsewhere) silently diverging from what actually gets credited.
    order_notes = order.get("notes") if isinstance(order, dict) else None
    order_net_amount_inr_raw = (
        order_notes.get("net_amount_inr") if isinstance(order_notes, dict) else None
    )
    if order.get("amount") != gross_amount_paise or order_net_amount_inr_raw != net_amount_inr_raw:
        raise MalformedWebhookPayloadError(
            f"payment.captured payload for order {order_id} disagrees with the order's own "
            "amount/notes on file with Razorpay"
        )

    logger.info(
        "payment.captured for order %s: crediting net %s (gross collected %s)",
        order_id,
        net_amount_inr,
        gross_amount_inr_from_payload,
    )

    try:
        transaction = billing.credit_wallet(
            db,
            user_id,
            net_amount_inr,
            "recharge_credit",
            razorpay_order_id=order_id,
            razorpay_payment_id=payment_id,
        )
    except IntegrityError:
        db.rollback()
        return

    # Invoice generation must never roll back or fail an already-verified
    # wallet credit — the payment was genuinely captured by Razorpay and the
    # wallet already credited by this point, so a PDF/S3 failure here is
    # logged and left for manual/retried generation later, not propagated
    # (CLAUDE.md: never lose or reverse real money over a PDF bug).
    try:
        invoicing.generate_invoice_for_transaction(db, transaction)
    except Exception:
        logger.exception(
            "Invoice generation failed for wallet_transaction_id=%s (order %s) — "
            "wallet credit stands, invoice can be regenerated later",
            transaction.wallet_transaction_id,
            order_id,
        )
