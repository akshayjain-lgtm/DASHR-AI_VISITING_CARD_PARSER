"""
Tests for the `21-invoicing` feature (spec: `.claude/specs/21-invoicing.md`).

Covers: invoice generation as a side effect of a successful wallet-recharge
webhook credit (idempotent on wallet_transaction_id), the GST breakdown
snapshotted onto each invoice, self/admin visibility rules on GET
/invoices*, PDF content, and the "invoice generation must never roll back
or block the wallet credit" rule.

Mocking/DB strategy mirrors test_14_wallet_recharge.py exactly (real
Postgres via conftest.py, Razorpay mocked via the _get_client() seam, real
local HMAC webhook signing). storage_service talks to the real MinIO
container from infra/docker-compose.yml — not mocked — same as every other
test file that uploads card images.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from decimal import Decimal

import pytest
import razorpay
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import settings
from app.main import app as fastapi_app
from app.models.invoice import Invoice
from app.models.organization import Organization
from app.models.user import User
from app.models.wallet_transaction import WalletTransaction
from app.services import billing, storage_service
from conftest import create_verified_user

WEBHOOK_SECRET = "whsec_test_only_do_not_use_elsewhere_21"


# --------------------------------------------------------------------------
# Auth helpers — per-file convention (see test_14_wallet_recharge.py).
# --------------------------------------------------------------------------


def _login(client: TestClient, user: dict) -> None:
    resp = client.post("/auth/login", json={"email": user["email"], "password": user["password"]})
    assert resp.status_code == 200, resp.text


def _authenticated_user(client: TestClient, fake_otp_provider, **overrides) -> dict:
    user = create_verified_user(client, fake_otp_provider, **overrides)
    _login(client, user)
    return user


# --------------------------------------------------------------------------
# Razorpay mocking — identical pattern to test_14_wallet_recharge.py.
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
        if self._created is not None and order_id == self._order_id:
            return self._created
        raise razorpay.errors.BadRequestError(f"Order {order_id!r} not found")


class _FakeRazorpayClient:
    def __init__(self, order_id: str = "order_test_fixed") -> None:
        self.order = _FakeRazorpayOrders(order_id)
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
    order_id: str, payment_id: str, amount_paise: int, user_id: str, net_amount_inr: str
) -> dict:
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
                    "notes": {"user_id": user_id, "net_amount_inr": net_amount_inr},
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
    client: TestClient, monkeypatch: pytest.MonkeyPatch, user: dict, amount_inr: str, order_id: str
) -> tuple[dict, dict]:
    _patch_razorpay_client(monkeypatch, order_id=order_id)
    _set_webhook_secret(monkeypatch)

    recharge_resp = client.post("/wallet/recharge", json={"amount_inr": amount_inr})
    assert recharge_resp.status_code == 200, recharge_resp.text

    net = Decimal(amount_inr)
    _cgst, _sgst, gross = billing.compute_gst(net)
    payment_id = f"pay_{uuid.uuid4().hex[:14]}"
    payload = _captured_payload(
        order_id, payment_id, int(gross * 100), user["user_id"], net_amount_inr=str(net)
    )
    raw_body, signature = _sign(payload)
    webhook_resp = _post_webhook(client, payload, signature, raw_body)
    assert webhook_resp.status_code == 200, webhook_resp.text

    return recharge_resp.json(), webhook_resp.json()


def _set_profile(
    client: TestClient,
    *,
    gst_no: str | None = None,
    billing_address: str | None = None,
    company_name: str | None = None,
) -> None:
    body = {}
    if gst_no is not None:
        body["gst_no"] = gst_no
    if billing_address is not None:
        body["billing_address"] = billing_address
    if company_name is not None:
        body["company_name"] = company_name
    resp = client.put("/profile", json=body)
    assert resp.status_code == 200, resp.text


def _make_admin_and_sub_user(
    client: TestClient, fake_otp_provider, db_session
) -> tuple[dict, dict]:
    """Mirrors test_org_admin_cannot_see_sub_users_wallet_or_transactions's
    direct-ORM setup (no conftest helper exists for org/admin accounts)."""
    admin = _authenticated_user(client, fake_otp_provider)
    org = Organization(name=f"Org {uuid.uuid4().hex[:8]}")
    db_session.add(org)
    db_session.commit()
    db_session.refresh(org)

    admin_row = db_session.get(User, uuid.UUID(admin["user_id"]))
    admin_row.org_id = org.org_id
    admin_row.role = "admin"
    db_session.commit()

    with TestClient(fastapi_app) as sub_client:
        sub_user = _authenticated_user(sub_client, fake_otp_provider)
        sub_row = db_session.get(User, uuid.UUID(sub_user["user_id"]))
        sub_row.org_id = org.org_id
        db_session.commit()

    return admin, sub_user


# --------------------------------------------------------------------------
# End-to-end invoice creation on a real webhook credit.
# --------------------------------------------------------------------------


def test_recharge_credit_generates_exactly_one_invoice_with_correct_breakdown(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch, db_session
):
    user = _authenticated_user(client, fake_otp_provider)
    _set_profile(
        client,
        gst_no="07AAKCR2891K1ZT",
        billing_address="123 Test Street, Delhi 110033",
        company_name="RRM Trading International Pvt Ltd",
    )

    _recharge_and_credit(client, monkeypatch, user, "1000", order_id="order_invoice_e2e")

    invoices = db_session.scalars(
        select(Invoice).where(Invoice.user_id == uuid.UUID(user["user_id"]))
    ).all()
    assert len(invoices) == 1
    invoice = invoices[0]

    assert invoice.taxable_value_inr == Decimal("1000")
    assert invoice.cgst_amount_inr == Decimal("90.00")
    assert invoice.sgst_amount_inr == Decimal("90.00")
    assert invoice.total_inr == Decimal("1180.00")
    assert invoice.sac_code == "9983"
    assert invoice.service_description == (
        "Cardex Recharge - For Visiting Card Parsing,Enrichment and Scoring"
    )
    assert invoice.bill_to_gst_no == "07AAKCR2891K1ZT"
    assert invoice.bill_to_billing_address == "123 Test Street, Delhi 110033"
    assert invoice.bill_to_name == "RRM Trading International Pvt Ltd", (
        "a GST-registered buyer must be billed under their company name, not the individual user's name"
    )
    assert invoice.issuer_name == "DASHR Material Handling Solutions (OPC) Private Limited"
    assert invoice.issuer_gst_no == "06AAMCD5859M1ZX"
    assert invoice.pdf_storage_key == f"invoices/{user['user_id']}/{invoice.invoice_number}.pdf"

    import re

    assert re.fullmatch(r"DASHR-INV-\d{6}", invoice.invoice_number), invoice.invoice_number

    transaction = db_session.scalar(
        select(WalletTransaction).where(WalletTransaction.user_id == uuid.UUID(user["user_id"]))
    )
    assert invoice.wallet_transaction_id == transaction.wallet_transaction_id


def test_bill_to_name_falls_back_to_user_name_when_gst_set_but_company_name_blank(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch, db_session
):
    """gst_no present but company_name never filled in must never leave
    bill_to_name blank — falls back to the user's own name."""
    user = _authenticated_user(client, fake_otp_provider)
    _set_profile(client, gst_no="07AAKCR2891K1ZT")  # no company_name

    _recharge_and_credit(client, monkeypatch, user, "200", order_id="order_bill_to_fallback")

    invoice = db_session.scalar(
        select(Invoice).where(Invoice.user_id == uuid.UUID(user["user_id"]))
    )
    assert invoice.bill_to_name == "Priya Sharma"


def test_bill_to_name_uses_user_name_when_no_gst_even_if_company_name_set(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch, db_session
):
    """No GST No. on file means the buyer isn't presented as a registered
    business, even if a company_name happens to be on the profile —
    bill_to_name stays the individual user's own name."""
    user = _authenticated_user(client, fake_otp_provider)
    _set_profile(client, company_name="Some Company Pvt Ltd")  # no gst_no

    _recharge_and_credit(client, monkeypatch, user, "200", order_id="order_bill_to_no_gst")

    invoice = db_session.scalar(
        select(Invoice).where(Invoice.user_id == uuid.UUID(user["user_id"]))
    )
    assert invoice.bill_to_name == "Priya Sharma"
    assert invoice.bill_to_gst_no is None


def test_redelivered_webhook_does_not_create_a_second_invoice(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch, db_session
):
    user = _authenticated_user(client, fake_otp_provider)
    _patch_razorpay_client(monkeypatch, order_id="order_invoice_redeliver")
    _set_webhook_secret(monkeypatch)

    recharge_resp = client.post("/wallet/recharge", json={"amount_inr": "500"})
    assert recharge_resp.status_code == 200, recharge_resp.text

    net = Decimal("500")
    _cgst, _sgst, gross = billing.compute_gst(net)
    payload = _captured_payload(
        "order_invoice_redeliver", "pay_invoice_redeliver", int(gross * 100), user["user_id"],
        net_amount_inr=str(net),
    )
    raw_body, signature = _sign(payload)

    first = _post_webhook(client, payload, signature, raw_body)
    assert first.status_code == 200, first.text
    second = _post_webhook(client, payload, signature, raw_body)
    assert second.status_code == 200, second.text

    invoices = db_session.scalars(
        select(Invoice).where(Invoice.user_id == uuid.UUID(user["user_id"]))
    ).all()
    assert len(invoices) == 1


def test_blank_seller_profile_still_produces_a_valid_invoice(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch, db_session
):
    """A user who never saved a SellerProfile must not block invoice
    generation — bill_to_gst_no/bill_to_billing_address are just None."""
    user = _authenticated_user(client, fake_otp_provider)
    _recharge_and_credit(client, monkeypatch, user, "300", order_id="order_blank_profile")

    invoice = db_session.scalar(
        select(Invoice).where(Invoice.user_id == uuid.UUID(user["user_id"]))
    )
    assert invoice is not None
    assert invoice.bill_to_gst_no is None
    assert invoice.bill_to_billing_address is None
    assert invoice.taxable_value_inr == Decimal("300")


def test_billing_address_with_unescaped_markup_does_not_break_invoice_generation(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch, db_session
):
    """SellerProfile.billing_address/company_name are freeform user input,
    only length-validated — a value containing an unmatched ReportLab
    Paragraph tag (e.g. a stray "<b>") must not throw during PDF build and
    silently kill invoice generation for that user on every future
    recharge (the caller's broad except would swallow it forever). Values
    must render as literal escaped text, not be interpreted as markup."""
    user = _authenticated_user(client, fake_otp_provider)
    _set_profile(
        client,
        gst_no="07AAKCR2891K1ZT",
        billing_address='123 Test St <a href="http://evil.example">click me</a>',
        company_name="<b>Unclosed Bold Co",
    )

    _recharge_and_credit(client, monkeypatch, user, "150", order_id="order_unescaped_markup")

    invoice = db_session.scalar(
        select(Invoice).where(Invoice.user_id == uuid.UUID(user["user_id"]))
    )
    assert invoice is not None, "invoice generation must succeed even with markup-like profile text"
    assert invoice.bill_to_name == "<b>Unclosed Bold Co"

    pdf_resp = client.get(f"/invoices/{invoice.invoice_id}/pdf")
    assert pdf_resp.status_code == 200, pdf_resp.text
    assert pdf_resp.content[:4] == b"%PDF"


def test_invoice_snapshot_is_immutable_after_profile_edit(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch, db_session
):
    user = _authenticated_user(client, fake_otp_provider)
    _set_profile(client, gst_no="07ORIGINAL0000Z1", billing_address="Original Address")
    _recharge_and_credit(client, monkeypatch, user, "400", order_id="order_snapshot_immutable")

    invoice_id = db_session.scalar(
        select(Invoice.invoice_id).where(Invoice.user_id == uuid.UUID(user["user_id"]))
    )

    _set_profile(client, gst_no="07CHANGED000000Z", billing_address="Changed Address")

    db_session.expire_all()
    invoice = db_session.get(Invoice, invoice_id)
    assert invoice.bill_to_gst_no == "07ORIGINAL0000Z1"
    assert invoice.bill_to_billing_address == "Original Address"


# --------------------------------------------------------------------------
# GET /invoices — self-only, paginated, newest-first.
# --------------------------------------------------------------------------


def test_list_invoices_returns_only_callers_own_newest_first(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch
):
    user_a = _authenticated_user(client, fake_otp_provider)
    _recharge_and_credit(client, monkeypatch, user_a, "100", order_id="order_list_a1")
    _recharge_and_credit(client, monkeypatch, user_a, "200", order_id="order_list_a2")

    with TestClient(fastapi_app) as client_b:
        user_b = _authenticated_user(client_b, fake_otp_provider)
        _recharge_and_credit(client_b, monkeypatch, user_b, "999", order_id="order_list_b1")

    resp = client.get("/invoices")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 2
    assert [i["taxable_value_inr"] for i in body] == ["200", "100"]


# --------------------------------------------------------------------------
# GET /invoices/{id} and /pdf — visibility rules.
# --------------------------------------------------------------------------


def test_get_invoice_404s_for_unrelated_user(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch, db_session
):
    owner = _authenticated_user(client, fake_otp_provider)
    _recharge_and_credit(client, monkeypatch, owner, "150", order_id="order_visibility_owner")
    invoice_id = str(
        db_session.scalar(select(Invoice.invoice_id).where(Invoice.user_id == uuid.UUID(owner["user_id"])))
    )

    with TestClient(fastapi_app) as other_client:
        _authenticated_user(other_client, fake_otp_provider)
        resp = other_client.get(f"/invoices/{invoice_id}")
        assert resp.status_code == 404, resp.text
        pdf_resp = other_client.get(f"/invoices/{invoice_id}/pdf")
        assert pdf_resp.status_code == 404, pdf_resp.text


def test_get_invoice_200s_for_owner_and_returns_pdf(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch, db_session
):
    owner = _authenticated_user(client, fake_otp_provider)
    _recharge_and_credit(client, monkeypatch, owner, "150", order_id="order_visibility_owner_ok")

    invoice_id = str(
        db_session.scalar(select(Invoice.invoice_id).where(Invoice.user_id == uuid.UUID(owner["user_id"])))
    )
    resp = client.get(f"/invoices/{invoice_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["invoice_id"] == invoice_id

    pdf_resp = client.get(f"/invoices/{invoice_id}/pdf")
    assert pdf_resp.status_code == 200, pdf_resp.text
    assert pdf_resp.headers["content-type"] == "application/pdf"
    assert pdf_resp.content[:4] == b"%PDF"


def test_invoice_pdf_content_matches_spec_inclusion_exclusion_list(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch, db_session
):
    """Direct verification of .claude/specs/21-invoicing.md's PDF layout
    section — what must appear and what must never appear — rather than
    trusting the rendering code by inspection alone."""
    import pypdfium2 as pdfium

    user = _authenticated_user(client, fake_otp_provider)
    _set_profile(client, gst_no="07AAKCR2891K1ZT", billing_address="123 Test Street, Delhi 110033")
    _recharge_and_credit(client, monkeypatch, user, "1000", order_id="order_pdf_content")

    invoice = db_session.scalar(
        select(Invoice).where(Invoice.user_id == uuid.UUID(user["user_id"]))
    )
    pdf_resp = client.get(f"/invoices/{invoice.invoice_id}/pdf")
    assert pdf_resp.status_code == 200, pdf_resp.text

    pdf = pdfium.PdfDocument(pdf_resp.content)
    text = pdf[0].get_textpage().get_text_range()

    for expected in [
        invoice.invoice_number,
        "9983",
        "CGST",
        "SGST",
        "computer generated invoice",
        "06AAMCD5859M1ZX",
        "DASHR Material Handling Solutions",
        "Cardex Recharge",
    ]:
        assert expected in text, f"expected {expected!r} in invoice PDF text"

    for forbidden in [
        "Bank Details",
        "Ship To",
        "Place Of Supply",
        "Place of Supply",
        "IGST",
        "Balance Due",
        "Authorized Signature",
        "Zoho",
    ]:
        assert forbidden not in text, f"did not expect {forbidden!r} in invoice PDF text"


def test_get_invoice_404s_for_non_admin_org_mate(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch, db_session
):
    """A non-admin member of the same org as the invoice's owner must not
    see it — only the owner or an *admin* of that org."""
    owner = _authenticated_user(client, fake_otp_provider)
    org = Organization(name=f"Org {uuid.uuid4().hex[:8]}")
    db_session.add(org)
    db_session.commit()
    db_session.refresh(org)
    owner_row = db_session.get(User, uuid.UUID(owner["user_id"]))
    owner_row.org_id = org.org_id
    db_session.commit()

    _recharge_and_credit(client, monkeypatch, owner, "150", order_id="order_org_mate_blocked")
    invoice_id = str(
        db_session.scalar(select(Invoice.invoice_id).where(Invoice.user_id == uuid.UUID(owner["user_id"])))
    )

    with TestClient(fastapi_app) as mate_client:
        org_mate = _authenticated_user(mate_client, fake_otp_provider)
        mate_row = db_session.get(User, uuid.UUID(org_mate["user_id"]))
        mate_row.org_id = org.org_id  # member, not admin
        db_session.commit()

        resp = mate_client.get(f"/invoices/{invoice_id}")
        assert resp.status_code == 404, resp.text


def test_get_invoice_200s_for_org_admin(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch, db_session
):
    admin, sub_user = _make_admin_and_sub_user(client, fake_otp_provider, db_session)

    with TestClient(fastapi_app) as sub_client:
        _login(sub_client, sub_user)
        _recharge_and_credit(sub_client, monkeypatch, sub_user, "150", order_id="order_admin_sees_sub")

    invoice_id = str(
        db_session.scalar(select(Invoice.invoice_id).where(Invoice.user_id == uuid.UUID(sub_user["user_id"])))
    )
    resp = client.get(f"/invoices/{invoice_id}")
    assert resp.status_code == 200, resp.text


# --------------------------------------------------------------------------
# GET /invoices/org — admin-only, org-wide.
# --------------------------------------------------------------------------


def test_list_org_invoices_requires_admin(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch
):
    _authenticated_user(client, fake_otp_provider)
    resp = client.get("/invoices/org")
    assert resp.status_code == 403, resp.text


def test_list_org_invoices_returns_every_org_members_invoices(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch, db_session
):
    admin, sub_user = _make_admin_and_sub_user(client, fake_otp_provider, db_session)

    with TestClient(fastapi_app) as sub_client:
        _login(sub_client, sub_user)
        _recharge_and_credit(sub_client, monkeypatch, sub_user, "150", order_id="order_org_wide_sub")

    resp = client.get("/invoices/org")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["user_id"] == sub_user["user_id"]


def test_list_org_invoices_excludes_other_orgs(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch, db_session
):
    admin, _sub_user = _make_admin_and_sub_user(client, fake_otp_provider, db_session)

    with TestClient(fastapi_app) as other_client:
        other_user = _authenticated_user(other_client, fake_otp_provider)
        _recharge_and_credit(
            other_client, monkeypatch, other_user, "999", order_id="order_other_org_excluded"
        )

    resp = client.get("/invoices/org")
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


# --------------------------------------------------------------------------
# Invoice generation must never block/roll back the wallet credit.
# --------------------------------------------------------------------------


def test_invoice_generation_failure_never_blocks_the_wallet_credit(
    client: TestClient, fake_otp_provider, monkeypatch: pytest.MonkeyPatch, db_session
):
    def _boom(*args, **kwargs):
        raise RuntimeError("simulated S3 outage")

    monkeypatch.setattr(storage_service, "upload_file", _boom)

    user = _authenticated_user(client, fake_otp_provider)
    _patch_razorpay_client(monkeypatch, order_id="order_invoice_failure")
    _set_webhook_secret(monkeypatch)

    recharge_resp = client.post("/wallet/recharge", json={"amount_inr": "500"})
    assert recharge_resp.status_code == 200, recharge_resp.text

    net = Decimal("500")
    _cgst, _sgst, gross = billing.compute_gst(net)
    payload = _captured_payload(
        "order_invoice_failure", "pay_invoice_failure", int(gross * 100), user["user_id"],
        net_amount_inr=str(net),
    )
    raw_body, signature = _sign(payload)
    resp = _post_webhook(client, payload, signature, raw_body)

    assert resp.status_code == 200, resp.text

    wallet_resp = client.get("/wallet")
    assert wallet_resp.json()["balance_inr"] == "500"

    invoice = db_session.scalar(
        select(Invoice).where(Invoice.user_id == uuid.UUID(user["user_id"]))
    )
    assert invoice is None
