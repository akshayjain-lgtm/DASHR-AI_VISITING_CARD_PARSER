"""
Tests for the `14-wallet-recharge` feature (spec:
`.claude/specs/14-wallet-recharge.md`).

These tests are written directly against the spec's documented contract for
`GET /wallet`, `GET /wallet/transactions`, `POST /wallet/recharge`, and
`POST /payments/webhook/razorpay` — never against the implementation of
`routers/wallet.py`, `routers/payments.py`, `services/billing.py`, or
`services/payments.py`, none of which were read to write these assertions.
`app/models/wallet.py`, `app/models/wallet_transaction.py`,
`app/models/pricing_rate.py`, and `app/schemas/wallet.py` *were* read — these
are the DB/API contract the spec itself already fully specifies field-by-field
(the spec's "Database changes" section enumerates every column by name), so
reading them is confirming the contract, not deriving test logic. Notably the
spec's prose says `GET /wallet` returns `balance: Decimal`, but the real
`WalletOut` schema field is `balance_inr` — tests use `balance_inr` (the
schema file, not the spec's shorthand prose, is the literal contract).
`services/razorpay_client.py` was also read (`get_client()` — a plain
zero-arg function returning `razorpay.Client(auth=(key_id, key_secret))`) —
purely to identify the correct monkeypatch target so the Razorpay SDK is
never called over the network, not to learn business logic.

Mocking strategy (per task instruction: "Mock the Razorpay SDK — never make a
real network call to Razorpay's API"):
  - `app.services.razorpay_client.get_client` is monkeypatched to return a
    `FakeRazorpayClient` exposing the same `order.create(data)` /
    `order.fetch(order_id)` / `utility.verify_webhook_signature(body,
    signature, secret)` surface as the real `razorpay.Client` (confirmed
    against the installed `razorpay==2.0.1` package's own
    `resources/order.py` and `utility/utility.py`), so no test ever depends
    on network access or real Razorpay credentials.
  - `FakeRazorpayClient.utility.verify_webhook_signature` is a faithful
    reimplementation of Razorpay's own published algorithm (HMAC-SHA256 of
    the raw body, keyed by the webhook secret, `hmac.compare_digest`,
    raising `razorpay.errors.SignatureVerificationError` on mismatch — this
    is copied from the real SDK's `Utility.verify_signature`, which is
    public, documented behavior, not DASHR AI's own implementation) so
    signature-verification tests exercise real crypto, not an
    always-true/false stub.
  - `settings.razorpay_webhook_secret` is monkeypatched to a fixed test
    value per test (via `monkeypatch.setattr`, since `Settings` is a mutable
    pydantic-settings singleton and every request reads `settings.<attr>`
    fresh) so signatures can be computed deterministically without depending
    on `.env`/CI secrets.

Judgment calls made in the absence of explicit spec text (documented inline
at point of use too):
  1. **How the webhook identifies which user's wallet to credit.** The spec
     documents exactly three new tables (`pricing_rates`, `wallets`,
     `wallet_transactions`) and no separate "pending order" tracking table,
     and explicitly forbids writing a `wallet_transactions` row before the
     webhook confirms payment. That means the order→user mapping must travel
     through Razorpay itself. The only architecturally sound way to do that
     with the documented schema is to attach `notes={"user_id": ...}` when
     creating the order and read it back from the (signature-verified)
     webhook payload's `payload.payment.entity.notes` — Razorpay's own
     documented behavior is to copy order `notes` onto the resulting payment
     entity. Tests build synthetic webhook bodies using this shape. If the
     real implementation keys its notes field differently, the webhook
     happy-path tests here would fail even though the endpoint contract
     (status codes, response shape, idempotency, signature checks) is
     followed correctly — that's an intentional, documented risk of writing
     an independent spec-first test file for an endpoint whose payload
     shape isn't fully pinned down in prose, not a gap being papered over.
  2. **`POST /wallet/recharge`'s success status code.** Not stated verbatim
     in the spec's endpoint table. The spec explicitly analogizes webhook
     handling to "the existing `POST /cards/{id}/reprocess`-style single-row
     writes", and that existing endpoint (checked for this reason only, as
     an established structural convention, not for wallet logic) has no
     `status_code=201` override, i.e. defaults to FastAPI's plain `200` for
     an action-style POST that doesn't persist a new local row. Tests assert
     `200` for `POST /wallet/recharge`, matching that same convention (it
     doesn't create a local DB row — the wallet row already exists or is
     lazily created with the same balance).
  3. **"Admin never sees a sub-user's wallet."** No conftest helper exists
     to put a user through a real org-invite/admin+member setup (same gap
     `test_04_visiting_card_bulk_upload.py`/`test_12-archive-upload.py`
     document), so a literal admin-session-vs-member-wallet test isn't
     constructed here via fabricated ORM rows. Critically though: none of
     the three wallet endpoints accept *any* target-user identifier — each
     one derives the acting user solely from `get_current_user`'s session,
     with no `user_id` path/query param anywhere in the documented contract.
     There is therefore no code path through which *any* role could name
     another user's wallet. The ordinary two-different-users isolation
     tests below are consequently a complete, not partial, test of this
     invariant for this endpoint surface — role is structurally irrelevant
     to it, since the surface never branches on role at all.
  4. **Unknown-but-well-formed `razorpay_order_id` / non-`payment.captured`
     event.** The spec's response contract is explicit: `{status: "ok"}
     (200) or 400 on bad signature/malformed payload`. Neither "references
     an order this server never created" nor "is a different, well-formed
     event type" is a signature failure or a malformed payload, so per the
     letter of that contract both must return `200` (ack, no-op) rather than
     `400` or `500` — tested accordingly, with the DB asserted unchanged.
  5. A genuinely **concurrent** redelivery test (two threads posting the
     identical signed payload at the same time) is included as a bonus
     hardening check beyond the DoD's literal "re-post the identical payload
     a second time" (sequential) wording, since the spec explicitly calls
     out a `SELECT ... FOR UPDATE` row lock and a partial unique index for
     exactly this race. It asserts only the DB-level financial invariant
     (exactly one `wallet_transactions` row, exactly one credit applied) and
     that neither response is a `5xx`, without over-asserting that both
     concurrent responses must individually be `200` — a genuinely racy
     unhandled-IntegrityError bug would be a real finding, not a false
     failure, under those looser assertions.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
import razorpay
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import settings
from app.main import app as fastapi_app
from app.models.wallet import Wallet
from app.models.wallet_transaction import WalletTransaction
from conftest import create_verified_user

TEST_WEBHOOK_SECRET = "pytest-only-webhook-secret-do-not-use-elsewhere"


# --------------------------------------------------------------------------
# Auth / login helpers — mirrors test_08-delete-card.py / test_12-archive-
# upload.py's identically-named local helpers.
# --------------------------------------------------------------------------


def _login(client: TestClient, user: dict) -> None:
    resp = client.post("/auth/login", json={"email": user["email"], "password": user["password"]})
    assert resp.status_code == 200, resp.text


def _authenticated_user(client: TestClient, fake_otp_provider, **overrides) -> dict:
    user = create_verified_user(client, fake_otp_provider, **overrides)
    _login(client, user)
    return user


# --------------------------------------------------------------------------
# Fake Razorpay SDK — never a real network call. Interface mirrors the
# installed razorpay==2.0.1 package's own `order.create`/`order.fetch`/
# `utility.verify_webhook_signature` (read only to get the monkeypatch
# target + call shape right, per this file's docstring).
# --------------------------------------------------------------------------


class _FakeOrderApi:
    def __init__(self) -> None:
        self.orders: dict[str, dict] = {}
        self.create_calls: list[dict] = []

    def create(self, data=None, **kwargs) -> dict:
        merged = dict(data or {})
        merged.update(kwargs)
        self.create_calls.append(merged)
        order_id = f"order_test_{uuid.uuid4().hex[:16]}"
        order = {
            "id": order_id,
            "entity": "order",
            "amount": merged.get("amount"),
            "currency": merged.get("currency", "INR"),
            "status": "created",
            "notes": merged.get("notes", {}),
        }
        self.orders[order_id] = order
        return order

    def fetch(self, order_id, data=None, **kwargs) -> dict:
        if order_id not in self.orders:
            raise razorpay.errors.BadRequestError(f"The id provided does not exist: {order_id}")
        return self.orders[order_id]


class _RealCryptoUtility:
    """Reimplements razorpay.Utility.verify_signature's real algorithm
    (HMAC-SHA256 over the raw body, keyed by the webhook secret) so
    signature tests exercise genuine crypto, never a stubbed pass/fail."""

    def verify_webhook_signature(self, body, signature, secret) -> bool:
        payload = body if isinstance(body, bytes) else str(body).encode()
        key = secret if isinstance(secret, bytes) else str(secret).encode()
        expected = hmac.new(key, payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, str(signature)):
            raise razorpay.errors.SignatureVerificationError("Razorpay Signature Verification Failed")
        return True


class FakeRazorpayClient:
    def __init__(self) -> None:
        self.order = _FakeOrderApi()
        self.utility = _RealCryptoUtility()


def _patch_razorpay(monkeypatch: pytest.MonkeyPatch) -> FakeRazorpayClient:
    fake_client = FakeRazorpayClient()
    monkeypatch.setattr("app.services.razorpay_client.get_client", lambda: fake_client)
    return fake_client


def _use_test_webhook_secret(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(settings, "razorpay_webhook_secret", TEST_WEBHOOK_SECRET)
    return TEST_WEBHOOK_SECRET


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------
# Webhook payload construction — standard/documented Razorpay
# `payment.captured` webhook shape. See judgment call (1) above re: the
# `notes.user_id` assumption used to identify the paying user.
# --------------------------------------------------------------------------


def _webhook_body(
    *,
    event: str,
    order_id: str,
    payment_id: str,
    amount_paise: int,
    user_id: str,
    currency: str = "INR",
) -> bytes:
    payload = {
        "entity": "event",
        "account_id": "acc_TestAccount",
        "event": event,
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "entity": "payment",
                    "amount": amount_paise,
                    "currency": currency,
                    "status": "captured" if event == "payment.captured" else "failed",
                    "order_id": order_id,
                    "method": "card",
                    "captured": event == "payment.captured",
                    "email": "buyer@example.com",
                    "contact": "+919876543210",
                    "notes": {"user_id": str(user_id)},
                    "created_at": 1735660800,
                }
            }
        },
        "created_at": 1735660800,
    }
    return json.dumps(payload, separators=(",", ":")).encode()


def _post_webhook(client: TestClient, body: bytes, signature: str | None):
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["X-Razorpay-Signature"] = signature
    return client.post("/payments/webhook/razorpay", content=body, headers=headers)


def _recharge(client: TestClient, amount_inr) -> dict:
    resp = client.post("/wallet/recharge", json={"amount_inr": amount_inr})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _credit_via_webhook(
    client: TestClient,
    secret: str,
    *,
    order_id: str,
    amount_inr,
    user_id: str,
    event: str = "payment.captured",
    payment_id: str | None = None,
    tamper_signature: bool = False,
):
    payment_id = payment_id or f"pay_test_{uuid.uuid4().hex[:16]}"
    amount_paise = int(Decimal(str(amount_inr)) * 100)
    body = _webhook_body(
        event=event, order_id=order_id, payment_id=payment_id, amount_paise=amount_paise, user_id=user_id
    )
    signature = _sign(secret, body)
    if tamper_signature:
        signature = signature[:-4] + ("0" * 4 if signature[-4:] != "0000" else "1111")
    resp = _post_webhook(client, body, signature)
    return resp, payment_id, body


def _wallet_row(db_session, user_id: str) -> Wallet | None:
    return db_session.execute(select(Wallet).where(Wallet.user_id == uuid.UUID(user_id))).scalar_one_or_none()


def _transactions_for_user(db_session, user_id: str) -> list[WalletTransaction]:
    return list(
        db_session.execute(
            select(WalletTransaction)
            .where(WalletTransaction.user_id == uuid.UUID(user_id))
            .order_by(WalletTransaction.created_at)
        )
        .scalars()
        .all()
    )


# ==========================================================================
# 1. Auth guard — GET /wallet, GET /wallet/transactions, POST
#    /wallet/recharge all require a session; the webhook does not.
# ==========================================================================


@pytest.mark.parametrize(
    "method, path, json_body",
    [
        ("get", "/wallet", None),
        ("get", "/wallet/transactions", None),
        ("post", "/wallet/recharge", {"amount_inr": 500}),
    ],
)
def test_wallet_endpoints_without_session_return_401(client, method, path, json_body):
    kwargs = {"json": json_body} if json_body is not None else {}
    resp = getattr(client, method)(path, **kwargs)
    assert resp.status_code == 401, (
        f"{method.upper()} {path} without a session must return 401, got "
        f"{resp.status_code}: {resp.text}"
    )


def test_webhook_endpoint_requires_no_session_but_still_enforces_signature(client, monkeypatch):
    """Spec: the webhook is 'public (no session cookie — Razorpay calls this
    server-to-server; auth is the signature, not a user session)'. Hitting it
    with zero auth of any kind must never be a 401 (that would mean the
    router is wrongly gated behind get_current_user) — an unsigned request
    is rejected on its own terms (400), not because of a missing session."""
    _patch_razorpay(monkeypatch)
    _use_test_webhook_secret(monkeypatch)
    resp = _post_webhook(client, b'{"event": "payment.failed"}', signature=None)
    assert resp.status_code != 401, (
        "the Razorpay webhook must be reachable without a session cookie — it must never "
        f"401 like the other wallet endpoints, got {resp.status_code}: {resp.text}"
    )
    assert resp.status_code == 400, "a request with no signature header at all must be rejected as 400"


# ==========================================================================
# 2. GET /wallet — happy path + lazy wallet creation.
# ==========================================================================


def test_get_wallet_for_new_user_lazily_creates_zero_balance_wallet(client, fake_otp_provider, db_session):
    user = _authenticated_user(client, fake_otp_provider)
    assert _wallet_row(db_session, user["user_id"]) is None, (
        "fixture assumption: a freshly-signed-up user must have no wallets row yet"
    )

    resp = client.get("/wallet")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert Decimal(str(body["balance_inr"])) == Decimal("0"), "a lazily-created wallet must start at balance 0"
    assert body["currency"] == "INR"
    assert body["transactions"] == [], "a brand-new wallet must have no transaction history"

    db_session.expire_all()
    row = _wallet_row(db_session, user["user_id"])
    assert row is not None, "GET /wallet must lazily create a wallets row on first access"
    assert Decimal(row.balance_inr) == Decimal("0")


def test_get_wallet_reflects_credited_balance_and_recent_transactions(
    client, fake_otp_provider, db_session, monkeypatch
):
    user = _authenticated_user(client, fake_otp_provider)
    _patch_razorpay(monkeypatch)
    secret = _use_test_webhook_secret(monkeypatch)

    order = _recharge(client, 1500)
    resp, payment_id, _ = _credit_via_webhook(
        client, secret, order_id=order["razorpay_order_id"], amount_inr=1500, user_id=user["user_id"]
    )
    assert resp.status_code == 200, resp.text

    wallet = client.get("/wallet")
    assert wallet.status_code == 200, wallet.text
    body = wallet.json()
    assert Decimal(str(body["balance_inr"])) == Decimal("1500")
    assert len(body["transactions"]) == 1
    txn = body["transactions"][0]
    assert txn["transaction_type"] == "recharge_credit"
    assert Decimal(str(txn["amount_inr"])) == Decimal("1500")
    assert Decimal(str(txn["balance_after_inr"])) == Decimal("1500")
    assert txn["razorpay_order_id"] == order["razorpay_order_id"]
    assert txn["razorpay_payment_id"] == payment_id


def test_get_wallet_caps_transactions_at_most_recent_twenty(client, fake_otp_provider, monkeypatch):
    """Spec: 'response: WalletOut { ... transactions: WalletTransactionOut[]
    (most recent 20) }'."""
    user = _authenticated_user(client, fake_otp_provider)
    _patch_razorpay(monkeypatch)
    secret = _use_test_webhook_secret(monkeypatch)

    order_ids: list[str] = []
    for _ in range(22):
        order = _recharge(client, 100)
        resp, _, _ = _credit_via_webhook(
            client, secret, order_id=order["razorpay_order_id"], amount_inr=100, user_id=user["user_id"]
        )
        assert resp.status_code == 200, resp.text
        order_ids.append(order["razorpay_order_id"])

    wallet = client.get("/wallet")
    assert wallet.status_code == 200, wallet.text
    body = wallet.json()
    assert Decimal(str(body["balance_inr"])) == Decimal("2200")
    assert len(body["transactions"]) == 20, "GET /wallet must return at most the 20 most recent transactions"

    returned_order_ids = [t["razorpay_order_id"] for t in body["transactions"]]
    most_recent_20 = list(reversed(order_ids))[:20]
    assert returned_order_ids == most_recent_20, (
        "the 20 returned transactions must be the newest 20, newest first"
    )


# ==========================================================================
# 3. POST /wallet/recharge — happy path, response contract, no crediting.
# ==========================================================================


def test_recharge_valid_amount_returns_order_and_does_not_change_balance(
    client, fake_otp_provider, db_session, monkeypatch
):
    user = _authenticated_user(client, fake_otp_provider)
    fake_client = _patch_razorpay(monkeypatch)

    resp = client.post("/wallet/recharge", json={"amount_inr": 2500})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body.keys()) == {"razorpay_order_id", "razorpay_key_id", "amount_inr", "currency"}, (
        "WalletRechargeOut must expose exactly the documented fields"
    )
    assert body["razorpay_order_id"], "a real Razorpay order id must be returned"
    assert Decimal(str(body["amount_inr"])) == Decimal("2500")
    assert body["currency"] == "INR"
    assert len(fake_client.order.create_calls) == 1, "exactly one Razorpay order must be created per call"

    db_session.expire_all()
    row = _wallet_row(db_session, user["user_id"])
    assert row is not None
    assert Decimal(row.balance_inr) == Decimal("0"), (
        "POST /wallet/recharge must never credit the wallet — only the webhook may do that"
    )
    assert _transactions_for_user(db_session, user["user_id"]) == [], (
        "POST /wallet/recharge must not write any wallet_transactions row itself"
    )


@pytest.mark.parametrize(
    "amount_inr",
    [99, Decimal("99.99"), 500001, 0, -100],
)
def test_recharge_amount_outside_bounds_returns_422(client, fake_otp_provider, amount_inr):
    """Spec: 'WalletRechargeRequest { amount_inr: Decimal (min 100, max
    500000) }'."""
    _authenticated_user(client, fake_otp_provider)

    resp = client.post("/wallet/recharge", json={"amount_inr": str(amount_inr)})

    assert resp.status_code == 422, f"amount_inr={amount_inr} must be rejected, got {resp.text}"


@pytest.mark.parametrize(
    "amount_inr",
    [100, 500000, "250.50"],
)
def test_recharge_amount_at_or_within_bounds_is_accepted(client, fake_otp_provider, monkeypatch, amount_inr):
    _authenticated_user(client, fake_otp_provider)
    _patch_razorpay(monkeypatch)

    resp = client.post("/wallet/recharge", json={"amount_inr": amount_inr})

    assert resp.status_code == 200, f"amount_inr={amount_inr} is within bounds and must be accepted: {resp.text}"


def test_recharge_missing_amount_returns_422(client, fake_otp_provider):
    _authenticated_user(client, fake_otp_provider)

    resp = client.post("/wallet/recharge", json={})

    assert resp.status_code == 422, resp.text


def test_recharge_non_numeric_amount_returns_422(client, fake_otp_provider):
    _authenticated_user(client, fake_otp_provider)

    resp = client.post("/wallet/recharge", json={"amount_inr": "not-a-number"})

    assert resp.status_code == 422, resp.text


# ==========================================================================
# 4. POST /payments/webhook/razorpay — signature verification.
# ==========================================================================


def test_webhook_valid_signature_credits_wallet_and_writes_ledger_row(
    client, fake_otp_provider, db_session, monkeypatch
):
    """DoD: 'Posting a validly-signed payment.captured webhook for that
    order credits the wallet exactly once, inserts one wallet_transactions
    row with transaction_type="recharge_credit" and correct
    balance_after_inr, and GET /wallet reflects the new balance.'"""
    user = _authenticated_user(client, fake_otp_provider)
    _patch_razorpay(monkeypatch)
    secret = _use_test_webhook_secret(monkeypatch)

    order = _recharge(client, 750)
    resp, payment_id, _ = _credit_via_webhook(
        client, secret, order_id=order["razorpay_order_id"], amount_inr=750, user_id=user["user_id"]
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ok"}

    db_session.expire_all()
    row = _wallet_row(db_session, user["user_id"])
    assert row is not None
    assert Decimal(row.balance_inr) == Decimal("750")

    txns = _transactions_for_user(db_session, user["user_id"])
    assert len(txns) == 1, "exactly one wallet_transactions row must be inserted for one webhook credit"
    txn = txns[0]
    assert txn.transaction_type == "recharge_credit"
    assert Decimal(txn.amount_inr) == Decimal("750"), "credits are recorded as a positive amount"
    assert Decimal(txn.balance_after_inr) == Decimal("750"), (
        "balance_after_inr must match wallets.balance_inr immediately after this entry"
    )
    assert txn.razorpay_order_id == order["razorpay_order_id"]
    assert txn.razorpay_payment_id == payment_id
    assert txn.reference_id is None, "reference_id is for debit rows (e.g. card_id), not recharge credits"
    assert txn.wallet_id == row.wallet_id


def test_webhook_second_credit_stacks_on_top_of_first(client, fake_otp_provider, db_session, monkeypatch):
    user = _authenticated_user(client, fake_otp_provider)
    _patch_razorpay(monkeypatch)
    secret = _use_test_webhook_secret(monkeypatch)

    order1 = _recharge(client, 500)
    resp1, _, _ = _credit_via_webhook(
        client, secret, order_id=order1["razorpay_order_id"], amount_inr=500, user_id=user["user_id"]
    )
    assert resp1.status_code == 200, resp1.text

    order2 = _recharge(client, 300)
    resp2, _, _ = _credit_via_webhook(
        client, secret, order_id=order2["razorpay_order_id"], amount_inr=300, user_id=user["user_id"]
    )
    assert resp2.status_code == 200, resp2.text

    db_session.expire_all()
    row = _wallet_row(db_session, user["user_id"])
    assert Decimal(row.balance_inr) == Decimal("800")

    txns = _transactions_for_user(db_session, user["user_id"])
    assert len(txns) == 2
    assert [Decimal(t.amount_inr) for t in txns] == [Decimal("500"), Decimal("300")]
    assert [Decimal(t.balance_after_inr) for t in txns] == [Decimal("500"), Decimal("800")], (
        "balance_after_inr on each row must be the running total after that entry, not a flat amount"
    )


def test_webhook_redelivered_identical_payload_does_not_double_credit(
    client, fake_otp_provider, db_session, monkeypatch
):
    """DoD: 'Re-posting the identical webhook payload a second time does not
    double-credit the wallet (idempotency check passes).'"""
    user = _authenticated_user(client, fake_otp_provider)
    _patch_razorpay(monkeypatch)
    secret = _use_test_webhook_secret(monkeypatch)

    order = _recharge(client, 1000)
    payment_id = f"pay_test_{uuid.uuid4().hex[:16]}"
    body = _webhook_body(
        event="payment.captured",
        order_id=order["razorpay_order_id"],
        payment_id=payment_id,
        amount_paise=100000,
        user_id=user["user_id"],
    )
    signature = _sign(secret, body)

    first = _post_webhook(client, body, signature)
    assert first.status_code == 200, first.text

    second = _post_webhook(client, body, signature)
    assert second.status_code == 200, (
        f"a redelivered webhook for an already-credited order must be a no-op 200, not an "
        f"error, got {second.status_code}: {second.text}"
    )

    db_session.expire_all()
    row = _wallet_row(db_session, user["user_id"])
    assert Decimal(row.balance_inr) == Decimal("1000"), "the wallet must be credited exactly once, not twice"
    txns = _transactions_for_user(db_session, user["user_id"])
    assert len(txns) == 1, "exactly one wallet_transactions row must exist after a redelivered webhook"


def test_webhook_concurrent_identical_redelivery_credits_exactly_once(
    client, fake_otp_provider, db_session, monkeypatch
):
    """Bonus race-safety check beyond the DoD's sequential re-post wording —
    see judgment call (5) in this file's module docstring."""
    user = _authenticated_user(client, fake_otp_provider)
    _patch_razorpay(monkeypatch)
    secret = _use_test_webhook_secret(monkeypatch)

    order = _recharge(client, 400)
    payment_id = f"pay_test_{uuid.uuid4().hex[:16]}"
    body = _webhook_body(
        event="payment.captured",
        order_id=order["razorpay_order_id"],
        payment_id=payment_id,
        amount_paise=40000,
        user_id=user["user_id"],
    )
    signature = _sign(secret, body)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_post_webhook, client, body, signature) for _ in range(2)]
        responses = [f.result() for f in futures]

    for resp in responses:
        assert resp.status_code < 500, (
            f"a racing redelivery must never 500 — the unique index/row lock must produce a "
            f"clean no-op, got {resp.status_code}: {resp.text}"
        )

    db_session.expire_all()
    row = _wallet_row(db_session, user["user_id"])
    assert Decimal(row.balance_inr) == Decimal("400"), (
        "two concurrent identical webhook deliveries must still credit the wallet exactly once"
    )
    txns = _transactions_for_user(db_session, user["user_id"])
    assert len(txns) == 1, "exactly one wallet_transactions row may exist even under a concurrent race"


def test_webhook_invalid_signature_returns_400_and_touches_nothing(
    client, fake_otp_provider, db_session, monkeypatch
):
    """DoD: 'Posting a webhook with an invalid/missing signature returns 400
    and leaves wallets/wallet_transactions unchanged.'"""
    user = _authenticated_user(client, fake_otp_provider)
    _patch_razorpay(monkeypatch)
    secret = _use_test_webhook_secret(monkeypatch)

    order = _recharge(client, 600)
    resp, _, _ = _credit_via_webhook(
        client,
        secret,
        order_id=order["razorpay_order_id"],
        amount_inr=600,
        user_id=user["user_id"],
        tamper_signature=True,
    )

    assert resp.status_code == 400, resp.text
    assert _wallet_row(db_session, user["user_id"]) is None or Decimal(
        _wallet_row(db_session, user["user_id"]).balance_inr
    ) == Decimal("0"), "an invalid signature must never credit the wallet"
    assert _transactions_for_user(db_session, user["user_id"]) == [], (
        "an invalid signature must write no wallet_transactions row at all"
    )


def test_webhook_wrong_secret_signature_returns_400(client, fake_otp_provider, db_session, monkeypatch):
    user = _authenticated_user(client, fake_otp_provider)
    _patch_razorpay(monkeypatch)
    _use_test_webhook_secret(monkeypatch)

    order = _recharge(client, 600)
    body = _webhook_body(
        event="payment.captured",
        order_id=order["razorpay_order_id"],
        payment_id="pay_test_wrong_secret",
        amount_paise=60000,
        user_id=user["user_id"],
    )
    wrong_signature = _sign("totally-different-secret", body)

    resp = _post_webhook(client, body, wrong_signature)

    assert resp.status_code == 400, resp.text
    assert _transactions_for_user(db_session, user["user_id"]) == []


def test_webhook_missing_signature_header_returns_400_and_touches_nothing(
    client, fake_otp_provider, db_session, monkeypatch
):
    user = _authenticated_user(client, fake_otp_provider)
    _patch_razorpay(monkeypatch)
    secret = _use_test_webhook_secret(monkeypatch)

    order = _recharge(client, 600)
    body = _webhook_body(
        event="payment.captured",
        order_id=order["razorpay_order_id"],
        payment_id="pay_test_no_sig_header",
        amount_paise=60000,
        user_id=user["user_id"],
    )
    # A valid signature exists for this body (proving the rejection below is
    # specifically about the *missing header*, not a coincidentally-invalid
    # signature) but is deliberately never attached.
    assert _sign(secret, body), "sanity: signature computation itself must succeed"

    resp = _post_webhook(client, body, signature=None)

    assert resp.status_code == 400, resp.text
    assert _transactions_for_user(db_session, user["user_id"]) == []


def test_webhook_malformed_json_body_with_otherwise_valid_signature_returns_400(
    client, fake_otp_provider, db_session, monkeypatch
):
    """DoD/edge case: malformed webhook payloads must be rejected with 400
    even when the signature over those exact (malformed) bytes is valid —
    signature verification and payload validity are independent checks."""
    user = _authenticated_user(client, fake_otp_provider)
    _patch_razorpay(monkeypatch)
    secret = _use_test_webhook_secret(monkeypatch)

    malformed_body = b"{not-valid-json---"
    signature = _sign(secret, malformed_body)

    resp = _post_webhook(client, malformed_body, signature)

    assert resp.status_code == 400, resp.text
    assert _transactions_for_user(db_session, user["user_id"]) == []


def test_webhook_well_formed_json_missing_required_fields_returns_400(
    client, fake_otp_provider, db_session, monkeypatch
):
    user = _authenticated_user(client, fake_otp_provider)
    _patch_razorpay(monkeypatch)
    secret = _use_test_webhook_secret(monkeypatch)

    incomplete_body = json.dumps({"event": "payment.captured"}).encode()
    signature = _sign(secret, incomplete_body)

    resp = _post_webhook(client, incomplete_body, signature)

    assert resp.status_code == 400, resp.text
    assert _transactions_for_user(db_session, user["user_id"]) == []


def test_webhook_non_captured_event_with_valid_signature_is_a_no_op_200(
    client, fake_otp_provider, db_session, monkeypatch
):
    """Spec's response contract is exactly '{status: "ok"} (200) or 400 on
    bad signature/malformed payload' — a well-formed, validly-signed event
    of a type other than payment.captured is neither, so it must ack 200
    without crediting anything."""
    user = _authenticated_user(client, fake_otp_provider)
    _patch_razorpay(monkeypatch)
    secret = _use_test_webhook_secret(monkeypatch)

    order = _recharge(client, 600)
    resp, _, _ = _credit_via_webhook(
        client,
        secret,
        order_id=order["razorpay_order_id"],
        amount_inr=600,
        user_id=user["user_id"],
        event="payment.failed",
    )

    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    row = _wallet_row(db_session, user["user_id"])
    assert row is None or Decimal(row.balance_inr) == Decimal("0"), (
        "a payment.failed event must never credit the wallet"
    )
    assert _transactions_for_user(db_session, user["user_id"]) == []


def test_webhook_unknown_order_id_with_valid_signature_is_a_no_op_200(
    client, fake_otp_provider, db_session, monkeypatch
):
    """A well-formed, validly-signed payment.captured event referencing an
    order_id this server never issued (via POST /wallet/recharge) is not a
    'bad signature'/'malformed payload' per the response contract, so it
    must not 400 either — but it must categorically never credit anything,
    since it isn't 'tied to a known razorpay_order_id' per the endpoint
    spec."""
    user = _authenticated_user(client, fake_otp_provider)
    _patch_razorpay(monkeypatch)
    secret = _use_test_webhook_secret(monkeypatch)

    fabricated_order_id = f"order_never_issued_{uuid.uuid4().hex[:12]}"
    resp, _, _ = _credit_via_webhook(
        client, secret, order_id=fabricated_order_id, amount_inr=600, user_id=user["user_id"]
    )

    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    row = _wallet_row(db_session, user["user_id"])
    assert row is None or Decimal(row.balance_inr) == Decimal("0")
    assert _transactions_for_user(db_session, user["user_id"]) == []


# ==========================================================================
# 5. GET /wallet/transactions — pagination + ordering.
# ==========================================================================


def test_list_transactions_defaults_and_ordering(client, fake_otp_provider, monkeypatch):
    user = _authenticated_user(client, fake_otp_provider)
    _patch_razorpay(monkeypatch)
    secret = _use_test_webhook_secret(monkeypatch)

    amounts = [100, 200, 300]
    for amount in amounts:
        order = _recharge(client, amount)
        resp, _, _ = _credit_via_webhook(
            client, secret, order_id=order["razorpay_order_id"], amount_inr=amount, user_id=user["user_id"]
        )
        assert resp.status_code == 200, resp.text

    listing = client.get("/wallet/transactions")
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert isinstance(body, list), "GET /wallet/transactions returns a bare WalletTransactionOut[] list"
    assert len(body) == 3
    returned_amounts = [Decimal(str(t["amount_inr"])) for t in body]
    assert returned_amounts == [Decimal("300"), Decimal("200"), Decimal("100")], (
        "the ledger must be returned newest first"
    )


def test_list_transactions_respects_limit_and_offset(client, fake_otp_provider, monkeypatch):
    user = _authenticated_user(client, fake_otp_provider)
    _patch_razorpay(monkeypatch)
    secret = _use_test_webhook_secret(monkeypatch)

    for amount in (100, 200, 300, 400):
        order = _recharge(client, amount)
        resp, _, _ = _credit_via_webhook(
            client, secret, order_id=order["razorpay_order_id"], amount_inr=amount, user_id=user["user_id"]
        )
        assert resp.status_code == 200, resp.text

    first_page = client.get("/wallet/transactions", params={"limit": 2, "offset": 0})
    assert first_page.status_code == 200, first_page.text
    first_amounts = [Decimal(str(t["amount_inr"])) for t in first_page.json()]
    assert first_amounts == [Decimal("400"), Decimal("300")]

    second_page = client.get("/wallet/transactions", params={"limit": 2, "offset": 2})
    assert second_page.status_code == 200, second_page.text
    second_amounts = [Decimal(str(t["amount_inr"])) for t in second_page.json()]
    assert second_amounts == [Decimal("200"), Decimal("100")]


def test_list_transactions_limit_over_max_returns_422(client, fake_otp_provider):
    """Spec: 'query params limit (default 50, max 200) ...'."""
    _authenticated_user(client, fake_otp_provider)

    resp = client.get("/wallet/transactions", params={"limit": 201})

    assert resp.status_code == 422, resp.text


def test_list_transactions_empty_for_new_user(client, fake_otp_provider):
    _authenticated_user(client, fake_otp_provider)

    resp = client.get("/wallet/transactions")

    assert resp.status_code == 200, resp.text
    assert resp.json() == []


# ==========================================================================
# 6. Cross-user isolation. Every wallet endpoint scopes strictly to the
#    authenticated session's own user_id — see judgment call (3) above for
#    why this also fully covers the "admin never sees a sub-user's wallet"
#    requirement given this endpoint surface's actual shape.
# ==========================================================================


def test_get_wallet_never_reflects_another_users_recharge(client, fake_otp_provider, monkeypatch):
    _patch_razorpay(monkeypatch)
    secret = _use_test_webhook_secret(monkeypatch)

    with TestClient(fastapi_app) as owner_client:
        owner = _authenticated_user(owner_client, fake_otp_provider)
        order = _recharge(owner_client, 1000)
        resp, _, _ = _credit_via_webhook(
            owner_client, secret, order_id=order["razorpay_order_id"], amount_inr=1000, user_id=owner["user_id"]
        )
        assert resp.status_code == 200, resp.text

    _authenticated_user(client, fake_otp_provider)
    wallet = client.get("/wallet")

    assert wallet.status_code == 200, wallet.text
    body = wallet.json()
    assert Decimal(str(body["balance_inr"])) == Decimal("0"), (
        "a different user's GET /wallet must never reflect another user's recharge"
    )
    assert body["transactions"] == []


def test_list_transactions_never_leaks_across_users(client, fake_otp_provider, db_session, monkeypatch):
    _patch_razorpay(monkeypatch)
    secret = _use_test_webhook_secret(monkeypatch)

    user_a = _authenticated_user(client, fake_otp_provider)
    order_a = _recharge(client, 500)
    resp_a, _, _ = _credit_via_webhook(
        client, secret, order_id=order_a["razorpay_order_id"], amount_inr=500, user_id=user_a["user_id"]
    )
    assert resp_a.status_code == 200, resp_a.text

    with TestClient(fastapi_app) as other_client:
        user_b = _authenticated_user(other_client, fake_otp_provider)
        order_b = _recharge(other_client, 900)
        resp_b, _, _ = _credit_via_webhook(
            other_client, secret, order_id=order_b["razorpay_order_id"], amount_inr=900, user_id=user_b["user_id"]
        )
        assert resp_b.status_code == 200, resp_b.text

        b_listing = other_client.get("/wallet/transactions")
        assert b_listing.status_code == 200, b_listing.text
        b_order_ids = {t["razorpay_order_id"] for t in b_listing.json()}
        assert b_order_ids == {order_b["razorpay_order_id"]}, (
            "user B's ledger must contain only user B's own transaction"
        )

    a_listing = client.get("/wallet/transactions")
    assert a_listing.status_code == 200, a_listing.text
    a_order_ids = {t["razorpay_order_id"] for t in a_listing.json()}
    assert a_order_ids == {order_a["razorpay_order_id"]}, (
        "user A's ledger must contain only user A's own transaction, never user B's"
    )

    db_session.expire_all()
    assert Decimal(_wallet_row(db_session, user_a["user_id"]).balance_inr) == Decimal("500")
    assert Decimal(_wallet_row(db_session, user_b["user_id"]).balance_inr) == Decimal("900")
    for txn in _transactions_for_user(db_session, user_a["user_id"]):
        assert str(txn.user_id) == user_a["user_id"], "every row returned for user A's ledger query must be user A's own"
    for txn in _transactions_for_user(db_session, user_b["user_id"]):
        assert str(txn.user_id) == user_b["user_id"], "every row returned for user B's ledger query must be user B's own"


def test_webhook_credits_only_the_notes_identified_user_never_the_order_creator_by_accident(
    client, fake_otp_provider, db_session, monkeypatch
):
    """Sanity check that crediting is keyed off the payload's identified
    user, not e.g. 'whichever wallet row was touched most recently' or some
    other accidental global default — two users each recharge in the same
    test process, interleaved, and each webhook must land on the correct
    wallet."""
    _patch_razorpay(monkeypatch)
    secret = _use_test_webhook_secret(monkeypatch)

    user_a = _authenticated_user(client, fake_otp_provider)
    order_a = _recharge(client, 111)

    with TestClient(fastapi_app) as other_client:
        user_b = _authenticated_user(other_client, fake_otp_provider)
        order_b = _recharge(other_client, 222)

        resp_b, _, _ = _credit_via_webhook(
            other_client, secret, order_id=order_b["razorpay_order_id"], amount_inr=222, user_id=user_b["user_id"]
        )
        assert resp_b.status_code == 200, resp_b.text

    resp_a, _, _ = _credit_via_webhook(
        client, secret, order_id=order_a["razorpay_order_id"], amount_inr=111, user_id=user_a["user_id"]
    )
    assert resp_a.status_code == 200, resp_a.text

    db_session.expire_all()
    assert Decimal(_wallet_row(db_session, user_a["user_id"]).balance_inr) == Decimal("111")
    assert Decimal(_wallet_row(db_session, user_b["user_id"]).balance_inr) == Decimal("222")
