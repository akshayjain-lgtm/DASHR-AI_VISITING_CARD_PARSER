"""
Tests for the `14-wallet-recharge` feature (spec:
`.claude/specs/14-wallet-recharge.md`).

Written directly against the spec's documented contract:

- `GET /wallet` lazily creates a `wallets` row (`balance_inr=0`) for the
  requesting user on first access and returns it plus the 20 most recent
  ledger rows. `GET /wallet/transactions` is the paginated full ledger.
- `POST /wallet/recharge` creates a Razorpay Order and lazily creates the
  caller's `wallets` row (still at `balance_inr=0`) if it doesn't already
  exist — but never credits it. A wallet is credited only by
  `POST /payments/webhook/razorpay`, and only after that endpoint verifies
  the `X-Razorpay-Signature` header against the raw request body AND
  independently confirms the referenced `order_id` is a real, fetchable
  order on this Razorpay account (never trusting the payload's self-
  reported order_id/notes at face value).
- Webhook crediting is idempotent on `razorpay_order_id`: redelivering the
  identical `payment.captured` payload must never double-credit.
- A `payment.captured` event missing required fields (or with a real-but-
  unrecognized order_id) is handled distinctly: missing/unparseable fields
  → 400 (a genuine-but-unusable event); an order_id Razorpay doesn't
  recognize on this account → 200 no-op (a legitimate, just non-actionable
  event) — see `services/payments.handle_payment_captured`'s docstring for
  why these are kept distinct.
- `wallets`/`wallet_transactions` are User-scoped, not Organization-scoped —
  unlike every other visibility rule in this codebase (`scope_to_visible_
  users`), an org admin must NOT be able to see a sub-user's wallet/ledger
  through any wallet endpoint.

Mocking strategy: `services/payments.py` calls the Razorpay SDK through a
private `_get_client()` seam, mirroring `vision_client._get_client()` /
`enrichment_summary._get_client()` — order creation AND the order-fetch
verification the webhook handler now performs are both mocked via the same
`_FakeRazorpayClient` (its `.order.fetch()` returns whatever `.order.create()`
most recently stored, raising `razorpay.errors.BadRequestError` for any
other order_id, mirroring the real SDK's behavior for an unrecognized
order). Webhook signature verification is pure local HMAC-SHA256
computation (no network call), so it is exercised for real: tests compute a
genuine signature against a monkeypatched `settings.razorpay_webhook_secret`,
rather than faking the verification call itself.

Judgment calls made in the absence of explicit spec text:
  1. **No conftest helper exists for org/admin accounts** (documented as a
     known gap in `test_04_visiting_card_bulk_upload.py` and
     `test_05_parsing_visiting_card.py` — `02-user-registration` only ever
     produces `org_id=NULL, role=NULL` accounts). The admin-cannot-see-
     sub-user-wallet test therefore constructs the `Organization`/`User.org_
     id`/`User.role` state directly via the `db_session` fixture rather than
     through the API.
  2. **`pricing_rates` is not truncated by conftest's autouse `_clean_
     tables` fixture** (it has no FK to `users`, so `TRUNCATE users CASCADE`
     never reaches it) — its seeded-by-migration rows persist across the
     whole test session, so the seed-values test reads them directly rather
     than assuming a clean table.
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
from app.models.organization import Organization
from app.models.pricing_rate import PricingRate
from app.models.user import User
from app.models.wallet import Wallet
from app.models.wallet_transaction import WalletTransaction
from app.services import billing
from app.services.exceptions import InsufficientBalanceError
from conftest import create_verified_user

WEBHOOK_SECRET = "whsec_test_only_do_not_use_elsewhere"


# --------------------------------------------------------------------------
# Auth helpers — copied from test_08_delete_card.py / test_10_lead_scoring.py's
# established per-file convention.
# --------------------------------------------------------------------------


def _login(client: TestClient, user: dict) -> None:
    resp = client.post("/auth/login", json={"email": user["email"], "password": user["password"]})
    assert resp.status_code == 200, resp.text


def _authenticated_user(client: TestClient, fake_otp_provider, **overrides) -> dict:
    user = create_verified_user(client, fake_otp_provider, **overrides)
    _login(client, user)
    return user


# --------------------------------------------------------------------------
# Razorpay mocking — mirrors test_07_data_enrichment.py's _FakeAnthropicClient
# pattern of patching the module's private _get_client() seam.
# --------------------------------------------------------------------------


class _FakeRazorpayOrders:
    def __init__(self, order_id: str) -> None:
        self._order_id = order_id
        self.calls: list[dict] = []
        self._created: dict | None = None

    def create(self, data: dict) -> dict:
        self.calls.append(data)
        order = {
            "id": self._order_id,
            "entity": "order",
            "amount": data["amount"],
            "amount_paid": 0,
            "amount_due": data["amount"],
            "currency": data["currency"],
            "status": "created",
            # Real Razorpay echoes notes back on the order object (and onto
            # the resulting payment entity) — handle_payment_captured's
            # order-record cross-check relies on this being present here.
            "notes": data.get("notes", {}),
        }
        self._created = order
        return order

    def fetch(self, order_id: str) -> dict:
        # Mirrors the real SDK: fetching an order_id this account never
        # created (via .create()) raises BadRequestError — the same "order
        # not recognized" signal handle_payment_captured treats as a
        # legitimate no-op.
        if self._created is not None and order_id == self._order_id:
            return self._created
        raise razorpay.errors.BadRequestError(f"Order {order_id!r} not found")


class _FakeRazorpayClient:
    def __init__(self, order_id: str = "order_test_fixed") -> None:
        self.order = _FakeRazorpayOrders(order_id)
        # A real Utility instance — verify_webhook_signature is pure local
        # HMAC computation with no dependency on self.client, so tests
        # exercise the genuine signature-checking code path even though
        # order creation is faked.
        self.utility = razorpay.Utility()


def _patch_razorpay_client(
    monkeypatch: pytest.MonkeyPatch, order_id: str = "order_test_fixed"
) -> _FakeRazorpayClient:
    fake_client = _FakeRazorpayClient(order_id)
    monkeypatch.setattr("app.services.payments._get_client", lambda: fake_client)
    return fake_client


def _set_webhook_secret(monkeypatch: pytest.MonkeyPatch, secret: str = WEBHOOK_SECRET) -> None:
    monkeypatch.setattr(settings, "razorpay_webhook_secret", secret)


def _sign(payload: dict, secret: str = WEBHOOK_SECRET) -> tuple[bytes, str]:
    raw_body = json.dumps(payload).encode()
    signature = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return raw_body, signature


def _captured_payload(
    order_id: str,
    payment_id: str,
    amount_paise: int,
    user_id: str,
    net_amount_inr: str | None = None,
) -> dict:
    notes: dict = {"user_id": user_id}
    if net_amount_inr is not None:
        notes["net_amount_inr"] = net_amount_inr
    return {
        "entity": "event",
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": order_id,
                    "amount": amount_paise,
                    "currency": "INR",
                    "status": "captured",
                    "notes": notes,
                }
            }
        },
    }


def _post_webhook(client: TestClient, payload: dict, signature: str | None, raw_body: bytes):
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["X-Razorpay-Signature"] = signature
    return client.post("/payments/webhook/razorpay", content=raw_body, headers=headers)


def _recharge_and_credit(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    user: dict,
    amount_inr: str,
    order_id: str,
) -> tuple[dict, dict]:
    """Full happy-path: mocked order creation via POST /wallet/recharge,
    then a validly-signed webhook crediting it. Returns (recharge_response_json,
    webhook_response_json).

    amount_inr is the pre-tax amount requested/credited — the webhook's
    captured amount is the GROSS (amount_inr + 18% GST) figure Razorpay
    actually collects, per billing.compute_gst, mirroring exactly what
    create_recharge_order/handle_payment_captured do in production."""
    _patch_razorpay_client(monkeypatch, order_id=order_id)
    _set_webhook_secret(monkeypatch)

    recharge_resp = client.post("/wallet/recharge", json={"amount_inr": amount_inr})
    assert recharge_resp.status_code == 200, recharge_resp.text

    net = Decimal(amount_inr)
    _cgst, _sgst, gross = billing.compute_gst(net)
    payment_id = f"pay_{uuid.uuid4().hex[:14]}"
    amount_paise = int(gross * 100)
    payload = _captured_payload(
        order_id, payment_id, amount_paise, user["user_id"], net_amount_inr=str(net)
    )
    raw_body, signature = _sign(payload)
    webhook_resp = _post_webhook(client, payload, signature, raw_body)
    assert webhook_resp.status_code == 200, webhook_resp.text

    return recharge_resp.json(), webhook_resp.json()


# --------------------------------------------------------------------------
# Migration / seed data
# --------------------------------------------------------------------------


def test_migration_seeds_launch_pricing_rates(db_session):
    rows = {
        row.action_type: row.rate_inr
        for row in db_session.scalars(select(PricingRate))
    }
    assert rows["parse"] == Decimal("5")
    assert rows["enrichment"] == Decimal("3")
    assert rows["scoring"] == Decimal("2")


def test_get_current_rate_returns_seeded_rate(db_session):
    assert billing.get_current_rate(db_session, "parse") == Decimal("5")
    assert billing.get_current_rate(db_session, "enrichment") == Decimal("3")
    assert billing.get_current_rate(db_session, "scoring") == Decimal("2")


# --------------------------------------------------------------------------
# GET /wallet — lazy creation
# --------------------------------------------------------------------------


def test_get_wallet_lazily_creates_zero_balance_wallet_for_new_user(
    client: TestClient, fake_otp_provider, db_session
):
    user = _authenticated_user(client, fake_otp_provider)

    resp = client.get("/wallet")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["balance_inr"] == "0"
    assert body["currency"] == "INR"
    assert body["transactions"] == []

    wallet = db_session.scalar(select(Wallet).where(Wallet.user_id == uuid.UUID(user["user_id"])))
    assert wallet is not None
    assert wallet.balance_inr == Decimal("0")


def test_get_wallet_requires_authentication(client: TestClient):
    resp = client.get("/wallet")
    assert resp.status_code == 401, resp.text


# --------------------------------------------------------------------------
# POST /wallet/recharge
# --------------------------------------------------------------------------


def test_recharge_returns_order_and_does_not_change_balance(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch, db_session
):
    user = _authenticated_user(client, fake_otp_provider)
    fake_client = _patch_razorpay_client(monkeypatch, order_id="order_abc123")

    resp = client.post("/wallet/recharge", json={"amount_inr": "500"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["razorpay_order_id"] == "order_abc123"
    assert body["net_amount_inr"] == "500"
    assert body["cgst_amount_inr"] == "45.00"
    assert body["sgst_amount_inr"] == "45.00"
    assert body["gross_amount_inr"] == "590.00"
    assert body["currency"] == "INR"
    assert "razorpay_key_id" in body
    assert len(fake_client.order.calls) == 1
    assert fake_client.order.calls[0]["amount"] == 59000  # paise — gross = 500 * 1.18
    assert fake_client.order.calls[0]["notes"]["net_amount_inr"] == "500"

    # Lazily created (balance_inr=0) by create_recharge_order, same as a
    # first GET /wallet call would — but never credited by this endpoint.
    wallet = db_session.scalar(select(Wallet).where(Wallet.user_id == uuid.UUID(user["user_id"])))
    assert wallet is not None
    assert wallet.balance_inr == Decimal("0")
    assert db_session.scalar(select(WalletTransaction)) is None


@pytest.mark.parametrize("amount", ["50", "600000"])
def test_recharge_rejects_amount_outside_allowed_band(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch, amount: str
):
    _authenticated_user(client, fake_otp_provider)
    _patch_razorpay_client(monkeypatch)

    resp = client.post("/wallet/recharge", json={"amount_inr": amount})
    assert resp.status_code == 422, resp.text  # Pydantic Field(ge=100, le=500000)


# --------------------------------------------------------------------------
# POST /payments/webhook/razorpay
# --------------------------------------------------------------------------


def test_valid_webhook_credits_wallet_exactly_once(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch, db_session
):
    user = _authenticated_user(client, fake_otp_provider)
    _recharge_and_credit(client, monkeypatch, user, "500", order_id="order_credit_once")

    wallet = db_session.scalar(select(Wallet).where(Wallet.user_id == uuid.UUID(user["user_id"])))
    assert wallet.balance_inr == Decimal("500")

    transactions = db_session.scalars(
        select(WalletTransaction).where(WalletTransaction.user_id == uuid.UUID(user["user_id"]))
    ).all()
    assert len(transactions) == 1
    txn = transactions[0]
    assert txn.transaction_type == "recharge_credit"
    assert txn.amount_inr == Decimal("500")
    assert txn.balance_after_inr == Decimal("500")
    assert txn.razorpay_order_id == "order_credit_once"

    wallet_resp = client.get("/wallet")
    assert wallet_resp.status_code == 200, wallet_resp.text
    assert wallet_resp.json()["balance_inr"] == "500"
    assert len(wallet_resp.json()["transactions"]) == 1


def test_redelivered_webhook_does_not_double_credit(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch, db_session
):
    user = _authenticated_user(client, fake_otp_provider)
    _patch_razorpay_client(monkeypatch, order_id="order_redelivered")
    _set_webhook_secret(monkeypatch)

    recharge_resp = client.post("/wallet/recharge", json={"amount_inr": "250"})
    assert recharge_resp.status_code == 200, recharge_resp.text

    net = Decimal("250")
    _cgst, _sgst, gross = billing.compute_gst(net)
    payload = _captured_payload(
        "order_redelivered", "pay_redelivered_1", int(gross * 100), user["user_id"],
        net_amount_inr=str(net),
    )
    raw_body, signature = _sign(payload)

    first = _post_webhook(client, payload, signature, raw_body)
    assert first.status_code == 200, first.text
    second = _post_webhook(client, payload, signature, raw_body)
    assert second.status_code == 200, second.text

    transactions = db_session.scalars(
        select(WalletTransaction).where(WalletTransaction.user_id == uuid.UUID(user["user_id"]))
    ).all()
    assert len(transactions) == 1

    wallet = db_session.scalar(select(Wallet).where(Wallet.user_id == uuid.UUID(user["user_id"])))
    assert wallet.balance_inr == Decimal("250")


def test_webhook_with_invalid_signature_returns_400_and_changes_nothing(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch, db_session
):
    user = _authenticated_user(client, fake_otp_provider)
    _set_webhook_secret(monkeypatch)

    payload = _captured_payload("order_bad_sig", "pay_bad_sig", 10000, user["user_id"])
    raw_body = json.dumps(payload).encode()

    resp = _post_webhook(client, payload, "0" * 64, raw_body)
    assert resp.status_code == 400, resp.text

    assert db_session.scalar(select(WalletTransaction)) is None
    assert db_session.scalar(select(Wallet).where(Wallet.user_id == uuid.UUID(user["user_id"]))) is None


def test_webhook_with_missing_signature_header_returns_400(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch
):
    user = _authenticated_user(client, fake_otp_provider)
    _set_webhook_secret(monkeypatch)

    payload = _captured_payload("order_no_sig", "pay_no_sig", 10000, user["user_id"])
    raw_body = json.dumps(payload).encode()

    resp = _post_webhook(client, payload, None, raw_body)
    assert resp.status_code == 400, resp.text


def test_webhook_ignores_non_captured_events(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch, db_session
):
    user = _authenticated_user(client, fake_otp_provider)
    _set_webhook_secret(monkeypatch)

    payload = {
        "entity": "event",
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_failed",
                    "order_id": "order_failed",
                    "amount": 10000,
                    "notes": {"user_id": user["user_id"]},
                }
            }
        },
    }
    raw_body, signature = _sign(payload)
    resp = _post_webhook(client, payload, signature, raw_body)
    assert resp.status_code == 200, resp.text
    assert db_session.scalar(select(WalletTransaction)) is None


def test_webhook_captured_event_missing_required_field_returns_400(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch, db_session
):
    """A payment.captured event that's missing notes.user_id (or any other
    required field) is a genuine-but-unusable event, not a legitimate
    no-op — distinct from test_webhook_ignores_non_captured_events (a
    different event type entirely) and the unknown-order-id case below (a
    well-formed event this account just doesn't recognize)."""
    user = _authenticated_user(client, fake_otp_provider)
    _set_webhook_secret(monkeypatch)

    payload = {
        "entity": "event",
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_no_notes",
                    "order_id": "order_no_notes",
                    "amount": 10000,
                    # notes.user_id deliberately omitted
                }
            }
        },
    }
    raw_body, signature = _sign(payload)
    resp = _post_webhook(client, payload, signature, raw_body)
    assert resp.status_code == 400, resp.text
    assert db_session.scalar(select(WalletTransaction)) is None


def test_webhook_captured_event_missing_net_amount_inr_returns_400(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch, db_session
):
    """notes.net_amount_inr is as required as notes.user_id since GST landed
    — the wallet must never be credited the gross, tax-inclusive payment.amount
    figure, so a payload with no pre-tax amount to credit is malformed, not a
    legitimate no-op."""
    user = _authenticated_user(client, fake_otp_provider)
    _set_webhook_secret(monkeypatch)

    payload = {
        "entity": "event",
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_no_net_amount",
                    "order_id": "order_no_net_amount",
                    "amount": 59000,
                    "notes": {"user_id": user["user_id"]},  # net_amount_inr deliberately omitted
                }
            }
        },
    }
    raw_body, signature = _sign(payload)
    resp = _post_webhook(client, payload, signature, raw_body)
    assert resp.status_code == 400, resp.text
    assert db_session.scalar(select(WalletTransaction)) is None


def test_webhook_unknown_order_id_is_a_no_op_not_a_credit(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch, db_session
):
    """A well-formed, validly-signed payment.captured payload referencing
    an order_id this account never actually issued (via create_recharge_
    order) must never credit a wallet — order_id is verified against
    Razorpay's Orders API, never trusted from the payload alone."""
    user = _authenticated_user(client, fake_otp_provider)
    # Patches the fake client but never calls POST /wallet/recharge, so the
    # fake's .order.fetch() has nothing stored for "order_never_created".
    _patch_razorpay_client(monkeypatch, order_id="order_a_different_one")
    _set_webhook_secret(monkeypatch)

    payload = _captured_payload(
        "order_never_created", "pay_for_fake_order", 100000, user["user_id"],
        net_amount_inr="1000",
    )
    raw_body, signature = _sign(payload)
    resp = _post_webhook(client, payload, signature, raw_body)
    assert resp.status_code == 200, resp.text

    assert db_session.scalar(select(WalletTransaction)) is None
    wallet = db_session.scalar(select(Wallet).where(Wallet.user_id == uuid.UUID(user["user_id"])))
    assert wallet is None or wallet.balance_inr == Decimal("0")


# --------------------------------------------------------------------------
# GET /wallet/transactions — pagination + per-user isolation
# --------------------------------------------------------------------------


def test_transactions_endpoint_orders_newest_first_and_paginates(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch
):
    user = _authenticated_user(client, fake_otp_provider)
    for i, amount in enumerate(["100", "200", "300"]):
        _recharge_and_credit(client, monkeypatch, user, amount, order_id=f"order_page_{i}")

    resp = client.get("/wallet/transactions", params={"limit": 2, "offset": 0})
    assert resp.status_code == 200, resp.text
    page1 = resp.json()
    assert len(page1) == 2
    assert [t["amount_inr"] for t in page1] == ["300", "200"]  # newest first

    resp2 = client.get("/wallet/transactions", params={"limit": 2, "offset": 2})
    assert resp2.status_code == 200, resp2.text
    page2 = resp2.json()
    assert len(page2) == 1
    assert page2[0]["amount_inr"] == "100"


def test_second_user_never_sees_first_users_wallet_or_transactions(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch
):
    # `client`'s fixture already overrides get_otp_provider on the shared
    # `fastapi_app` instance for the duration of this test, so a second
    # TestClient wrapping that same app instance picks up the same
    # override automatically — no extra plumbing needed. Each TestClient
    # keeps its own cookie jar, so the two logins never collide.
    user_a = _authenticated_user(client, fake_otp_provider)

    with TestClient(fastapi_app) as client_b:
        user_b = _authenticated_user(client_b, fake_otp_provider)

        _recharge_and_credit(client, monkeypatch, user_a, "777", order_id="order_isolation_a")

        b_wallet = client_b.get("/wallet")
        assert b_wallet.status_code == 200, b_wallet.text
        assert b_wallet.json()["balance_inr"] == "0"
        assert b_wallet.json()["transactions"] == []

        b_txns = client_b.get("/wallet/transactions")
        assert b_txns.status_code == 200, b_txns.text
        assert b_txns.json() == []

    a_wallet = client.get("/wallet")
    assert a_wallet.json()["balance_inr"] == "777"


# --------------------------------------------------------------------------
# Admin must never see a sub-user's wallet (deliberate deviation from
# services/visibility.py's scope_to_visible_users, per CLAUDE.md).
# --------------------------------------------------------------------------


def test_org_admin_cannot_see_sub_users_wallet_or_transactions(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch, db_session
):
    admin = _authenticated_user(client, fake_otp_provider)

    org = Organization(name=f"Org {uuid.uuid4().hex[:8]}")
    db_session.add(org)
    db_session.commit()
    db_session.refresh(org)

    admin_row = db_session.get(User, uuid.UUID(admin["user_id"]))
    admin_row.org_id = org.org_id
    admin_row.role = "admin"
    db_session.commit()

    # Sub-user in the same org, via a second TestClient/cookie jar on the
    # same shared `fastapi_app` instance — see the isolation test above for
    # why no extra get_otp_provider plumbing is needed here.
    with TestClient(fastapi_app) as sub_client:
        sub_user = _authenticated_user(sub_client, fake_otp_provider)

        sub_row = db_session.get(User, uuid.UUID(sub_user["user_id"]))
        sub_row.org_id = org.org_id
        db_session.commit()

        _recharge_and_credit(sub_client, monkeypatch, sub_user, "999", order_id="order_admin_isolation")

    admin_wallet_resp = client.get("/wallet")
    assert admin_wallet_resp.status_code == 200, admin_wallet_resp.text
    assert admin_wallet_resp.json()["balance_inr"] == "0"
    assert admin_wallet_resp.json()["transactions"] == []

    admin_txns_resp = client.get("/wallet/transactions")
    assert admin_txns_resp.status_code == 200, admin_txns_resp.text
    assert admin_txns_resp.json() == []


# --------------------------------------------------------------------------
# billing.debit_wallet — not wired to any router yet, tested directly.
# --------------------------------------------------------------------------


def test_debit_wallet_reduces_balance_and_writes_ledger_row(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch, db_session
):
    user = _authenticated_user(client, fake_otp_provider)
    _recharge_and_credit(client, monkeypatch, user, "500", order_id="order_debit_source")

    txn = billing.debit_wallet(
        db_session, uuid.UUID(user["user_id"]), Decimal("50"), "parse_debit"
    )
    assert txn.amount_inr == Decimal("-50")
    assert txn.balance_after_inr == Decimal("450")

    wallet = db_session.scalar(select(Wallet).where(Wallet.user_id == uuid.UUID(user["user_id"])))
    assert wallet.balance_inr == Decimal("450")


def test_debit_wallet_raises_and_writes_nothing_when_balance_insufficient(
    client: TestClient, fake_otp_provider, db_session
):
    user = _authenticated_user(client, fake_otp_provider)
    # Lazily creates a zero-balance wallet.
    client.get("/wallet")

    with pytest.raises(InsufficientBalanceError):
        billing.debit_wallet(db_session, uuid.UUID(user["user_id"]), Decimal("10"), "parse_debit")

    wallet = db_session.scalar(select(Wallet).where(Wallet.user_id == uuid.UUID(user["user_id"])))
    assert wallet.balance_inr == Decimal("0")
    assert db_session.scalar(select(WalletTransaction)) is None


# --------------------------------------------------------------------------
# billing.compute_gst — the single source of GST math for both the
# Razorpay charge and the invoice PDF (.claude/specs/21-invoicing.md).
# --------------------------------------------------------------------------


def test_compute_gst_splits_18_percent_evenly_as_cgst_and_sgst():
    cgst, sgst, gross = billing.compute_gst(Decimal("1000"))
    assert cgst == Decimal("90.00")
    assert sgst == Decimal("90.00")
    assert gross == Decimal("1180.00")


def test_compute_gst_rounds_half_up_on_an_odd_amount():
    # 133 * 0.09 = 11.97 exactly — no rounding ambiguity here, but confirms
    # the two halves stay equal and sum correctly for a non-round amount.
    cgst, sgst, gross = billing.compute_gst(Decimal("133"))
    assert cgst == Decimal("11.97")
    assert sgst == Decimal("11.97")
    assert gross == Decimal("156.94")
    assert cgst + sgst + Decimal("133") == gross


def test_compute_gst_max_recharge_amount_does_not_misround():
    cgst, sgst, gross = billing.compute_gst(Decimal("500000"))
    assert cgst == Decimal("45000.00")
    assert sgst == Decimal("45000.00")
    assert gross == Decimal("590000.00")


# --------------------------------------------------------------------------
# Additional coverage merged in from the now-deleted test_14-wallet-recharge.py
# (a separate, independently-written "blind, spec-first" suite that
# overlapped heavily with the tests above but also covered a handful of
# edge cases this file didn't). Ported here and made GST-aware rather than
# maintaining two overlapping test files for the same feature.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method, path, json_body",
    [
        ("get", "/wallet", None),
        ("get", "/wallet/transactions", None),
        ("post", "/wallet/recharge", {"amount_inr": "500"}),
    ],
)
def test_wallet_endpoints_without_session_return_401(
    client: TestClient, method: str, path: str, json_body: dict | None
):
    kwargs = {"json": json_body} if json_body is not None else {}
    resp = getattr(client, method)(path, **kwargs)
    assert resp.status_code == 401, f"{method.upper()} {path} without a session must return 401"


def test_get_wallet_caps_transactions_at_most_recent_twenty(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch
):
    """Spec: 'response: WalletOut { ... transactions: WalletTransactionOut[]
    (most recent 20) }'."""
    user = _authenticated_user(client, fake_otp_provider)
    order_ids = []
    for i in range(22):
        order_id = f"order_cap_{i}"
        _recharge_and_credit(client, monkeypatch, user, "100", order_id=order_id)
        order_ids.append(order_id)

    wallet_resp = client.get("/wallet")
    assert wallet_resp.status_code == 200, wallet_resp.text
    body = wallet_resp.json()
    assert body["balance_inr"] == "2200"
    assert len(body["transactions"]) == 20, "GET /wallet must cap transactions at the 20 most recent"

    returned_order_ids = [t["razorpay_order_id"] for t in body["transactions"]]
    assert returned_order_ids == list(reversed(order_ids))[:20]


@pytest.mark.parametrize("amount_inr", ["100", "500000", "250.50"])
def test_recharge_amount_at_or_within_bounds_is_accepted(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch, amount_inr: str
):
    _authenticated_user(client, fake_otp_provider)
    _patch_razorpay_client(monkeypatch)

    resp = client.post("/wallet/recharge", json={"amount_inr": amount_inr})
    assert resp.status_code == 200, f"amount_inr={amount_inr} is within bounds: {resp.text}"


def test_recharge_missing_amount_returns_422(client: TestClient, fake_otp_provider):
    _authenticated_user(client, fake_otp_provider)
    resp = client.post("/wallet/recharge", json={})
    assert resp.status_code == 422, resp.text


def test_recharge_non_numeric_amount_returns_422(client: TestClient, fake_otp_provider):
    _authenticated_user(client, fake_otp_provider)
    resp = client.post("/wallet/recharge", json={"amount_inr": "not-a-number"})
    assert resp.status_code == 422, resp.text


def test_webhook_second_credit_stacks_on_top_of_first(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch, db_session
):
    user = _authenticated_user(client, fake_otp_provider)
    _recharge_and_credit(client, monkeypatch, user, "500", order_id="order_stack_1")
    _recharge_and_credit(client, monkeypatch, user, "300", order_id="order_stack_2")

    wallet = db_session.scalar(select(Wallet).where(Wallet.user_id == uuid.UUID(user["user_id"])))
    assert wallet.balance_inr == Decimal("800")

    txns = db_session.scalars(
        select(WalletTransaction)
        .where(WalletTransaction.user_id == uuid.UUID(user["user_id"]))
        .order_by(WalletTransaction.created_at)
    ).all()
    assert [t.amount_inr for t in txns] == [Decimal("500"), Decimal("300")]
    assert [t.balance_after_inr for t in txns] == [Decimal("500"), Decimal("800")], (
        "balance_after_inr on each row must be the running total, not a flat amount"
    )


def test_webhook_concurrent_identical_redelivery_credits_exactly_once(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch, db_session
):
    """Bonus race-safety check beyond the DoD's sequential re-post wording —
    two threads posting the identical signed payload at the same time must
    still credit exactly once, backstopped by the unique index on
    razorpay_order_id."""
    user = _authenticated_user(client, fake_otp_provider)
    _patch_razorpay_client(monkeypatch, order_id="order_concurrent")
    _set_webhook_secret(monkeypatch)

    recharge_resp = client.post("/wallet/recharge", json={"amount_inr": "400"})
    assert recharge_resp.status_code == 200, recharge_resp.text

    net = Decimal("400")
    _cgst, _sgst, gross = billing.compute_gst(net)
    payload = _captured_payload(
        "order_concurrent", "pay_concurrent_1", int(gross * 100), user["user_id"],
        net_amount_inr=str(net),
    )
    raw_body, signature = _sign(payload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_post_webhook, client, payload, signature, raw_body) for _ in range(2)]
        responses = [f.result() for f in futures]

    for resp in responses:
        assert resp.status_code < 500, f"a racing redelivery must never 500, got {resp.status_code}"

    db_session.expire_all()
    wallet = db_session.scalar(select(Wallet).where(Wallet.user_id == uuid.UUID(user["user_id"])))
    assert wallet.balance_inr == Decimal("400")
    txns = db_session.scalars(
        select(WalletTransaction).where(WalletTransaction.user_id == uuid.UUID(user["user_id"]))
    ).all()
    assert len(txns) == 1


def test_webhook_malformed_json_body_with_otherwise_valid_signature_returns_400(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch, db_session
):
    """Malformed webhook payloads must be rejected with 400 even when the
    signature over those exact (malformed) bytes is valid — signature
    verification and payload validity are independent checks."""
    _authenticated_user(client, fake_otp_provider)
    _set_webhook_secret(monkeypatch)

    malformed_body = b"{not-valid-json---"
    signature = hmac.new(WEBHOOK_SECRET.encode(), malformed_body, hashlib.sha256).hexdigest()

    resp = _post_webhook(client, malformed_body, signature, malformed_body)
    assert resp.status_code == 400, resp.text
    assert db_session.scalar(select(WalletTransaction)) is None


def test_list_transactions_limit_over_max_returns_422(client: TestClient, fake_otp_provider):
    """Spec: 'query params limit (default 50, max 200) ...'."""
    _authenticated_user(client, fake_otp_provider)
    resp = client.get("/wallet/transactions", params={"limit": 201})
    assert resp.status_code == 422, resp.text


def test_list_transactions_empty_for_new_user(client: TestClient, fake_otp_provider):
    _authenticated_user(client, fake_otp_provider)
    resp = client.get("/wallet/transactions")
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


def test_webhook_credits_only_the_notes_identified_user_never_the_order_creator_by_accident(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch, db_session
):
    """Sanity check that crediting is keyed off the payload's identified
    user (and net_amount_inr), not e.g. whichever wallet row was touched
    most recently — two users recharge interleaved, and each webhook must
    land on the correct wallet with the correct (pre-tax) amount."""
    _patch_razorpay_client(monkeypatch, order_id="order_interleave")
    _set_webhook_secret(monkeypatch)

    user_a = _authenticated_user(client, fake_otp_provider)
    with TestClient(fastapi_app) as client_b:
        user_b = _authenticated_user(client_b, fake_otp_provider)
        _recharge_and_credit(client_b, monkeypatch, user_b, "222", order_id="order_interleave_b")

    _recharge_and_credit(client, monkeypatch, user_a, "111", order_id="order_interleave_a")

    db_session.expire_all()
    wallet_a = db_session.scalar(select(Wallet).where(Wallet.user_id == uuid.UUID(user_a["user_id"])))
    wallet_b = db_session.scalar(select(Wallet).where(Wallet.user_id == uuid.UUID(user_b["user_id"])))
    assert wallet_a.balance_inr == Decimal("111")
    assert wallet_b.balance_inr == Decimal("222")
