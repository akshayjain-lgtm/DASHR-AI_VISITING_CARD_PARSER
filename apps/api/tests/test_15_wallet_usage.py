"""
Tests for the `15-wallet-usage` feature (spec: `.claude/specs/15-wallet-usage.md`).

Written directly against the spec's documented contract:

- Each user gets 20 free actions per action type (parse/enrichment/scoring),
  tracked as three independent `FreeActionAllowance` counters. Once a
  type's own free count is exhausted, further actions of that type are
  billed at that type's own `PricingRate` rate (parse=5, enrichment=3,
  scoring=2 INR, seeded by migration 0010) from the acting user's wallet.
- A billable action blocked by a zero/insufficient wallet balance must never
  run OCR/enrichment/scoring and never enqueue the Celery task — the
  single-card endpoints (`POST /cards/{id}/reprocess`,
  `/cards/{id}/enrich-company`, `/cards/{id}/score`) return 402 and leave
  the card/wallet/allowance state exactly as it was; the bulk endpoints
  (`POST /cards/process`, `/cards/enrich-companies`, `/cards/score`) count a
  blocked card in `wallet_blocked_count`, distinct from `enqueued_count`
  and `skipped_count`, without failing the rest of the batch.
- `GET /wallet` reports `free_actions_remaining` per action type, floored
  at 0 once exhausted (never negative).

Mocking strategy: matches test_05/test_09/test_10's established
conventions — `process_card` is called directly as a bare function
(bypassing `.delay()`) to drive cards into "new"/"failed"/"extracted"
states, with `vision_client.extract_card_fields` mocked via `_patch_vision`.
Endpoint tests that exercise the charge-then-enqueue path patch
`process_card.delay`/`enrich_company_task.delay`/`score_card_task.delay` at
their `app.services.card_service` call sites, so no real Celery broker is
needed.

Judgment calls made in the absence of explicit spec text:
  1. **Funding a wallet directly via `billing.credit_wallet`** rather than
     re-deriving the full Razorpay webhook mock flow from
     `test_14_wallet_recharge.py` — that flow is already covered there;
     this file only needs a funded wallet as a precondition.
  2. **Exhausting the free allowance directly via `billing.charge_for_action`**
     called 20 times, rather than driving 20 real cards through the full
     upload/extract pipeline — the free-allowance counter itself doesn't
     care which action produced the charge, and this keeps setup fast.
  3. **One literal multi-threaded race test**, added after a code review
     found the eligibility-check-then-charge sequence in `reprocess_card`
     had no row lock, letting two concurrent duplicate requests both charge
     for one reprocess (`test_reprocess_concurrent_duplicate_requests_charge_exactly_once`
     below) — this is the one test in this file that spins up real threads
     against separate DB sessions on the same connection pool, since the
     race it proves closed genuinely can't be demonstrated sequentially.
"""

from __future__ import annotations

import io
import threading
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app.models.free_action_allowance import FreeActionAllowance
from app.models.user import User
from app.models.visiting_card import VisitingCard
from app.models.wallet import Wallet
from app.models.wallet_transaction import WalletTransaction
from app.services import billing, card_service
from app.services.exceptions import InvalidReprocessStateError
from app.workers.card_processing import process_card
from app.workers.scoring_processing import score_card_task
from conftest import TestSessionLocal, create_verified_user

# --------------------------------------------------------------------------
# Auth / upload / vision-mocking helpers — copied verbatim from
# test_09_bulk_select_parse_enrich.py's established convention.
# --------------------------------------------------------------------------


def _login(client: TestClient, user: dict) -> None:
    resp = client.post("/auth/login", json={"email": user["email"], "password": user["password"]})
    assert resp.status_code == 200, resp.text


def _authenticated_user(client: TestClient, fake_otp_provider, **overrides) -> dict:
    user = create_verified_user(client, fake_otp_provider, **overrides)
    _login(client, user)
    return user


def _image_bytes(fmt: str = "JPEG", size: tuple[int, int] = (20, 20), color: str = "orange") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format=fmt)
    return buf.getvalue()


@pytest.fixture
def jpeg_bytes() -> bytes:
    return _image_bytes()


def _upload_files(client: TestClient, files: list[tuple[str, bytes, str]]):
    return client.post(
        "/cards/bulk-upload",
        data={},
        files=[("files", (name, content, ctype)) for name, content, ctype in files],
    )


def _upload_one(client: TestClient, jpeg_bytes: bytes, filename: str = "card.jpg") -> str:
    resp = _upload_files(client, [(filename, jpeg_bytes, "image/jpeg")])
    assert resp.status_code == 201, resp.text
    return resp.json()["cards"][0]["card_id"]


def _unique_company_name(label: str) -> str:
    """`companies` is not truncated between tests — every row created here
    must carry a name no other test could ever also create."""
    return f"{label} {uuid.uuid4().hex[:10]} Pvt Ltd"


def _patch_vision(monkeypatch: pytest.MonkeyPatch, *responses) -> list[tuple[bytes, str]]:
    queue = list(responses)
    calls: list[tuple[bytes, str]] = []

    def _fake(image_bytes: bytes, media_type: str):
        calls.append((image_bytes, media_type))
        if not queue:
            raise AssertionError("extract_card_fields called more times than this test scripted")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr("app.services.vision_client.extract_card_fields", _fake)
    return calls


def _fields(
    *,
    is_back_of_card: bool = False,
    full_name: str | None = "Extracted Contact",
    job_title: str | None = None,
    company_name: str | None = None,
    website: str | None = None,
    address: str | None = None,
    products_offered: str | None = None,
    special_remark: str | None = None,
    raw_ocr_text: str | None = "verbatim card text",
    emails: list[dict] | None = None,
    phones: list[dict] | None = None,
    gst_number: str | None = None,
) -> dict:
    return {
        "is_back_of_card": is_back_of_card,
        "full_name": full_name,
        "job_title": job_title,
        "company_name": company_name,
        "website": website,
        "address": address,
        "products_offered": products_offered,
        "special_remark": special_remark,
        "raw_ocr_text": raw_ocr_text,
        "emails": [] if emails is None else emails,
        "phones": [] if phones is None else phones,
        "gst_number": gst_number,
    }


def _empty_fields() -> dict:
    """A well-formed model response with no usable card fields at all — the
    permanent-failure case, used here only to drive a card to
    status='failed' (matches test_05/test_09's identically-named helper)."""
    return _fields(full_name=None, raw_ocr_text="blank or unrelated photo")


def _patch_process_delay(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    captured: list[str] = []
    monkeypatch.setattr(
        "app.services.card_service.process_card.delay",
        lambda cid, **kwargs: captured.append(cid),
    )
    return captured


def _patch_enrich_delay(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    captured: list[tuple] = []
    monkeypatch.setattr(
        "app.services.card_service.enrich_company_task.delay",
        lambda *args, **kwargs: captured.append((args, kwargs)),
    )
    return captured


def _patch_score_delay(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    captured: list[str] = []
    monkeypatch.setattr(
        "app.services.card_service.score_card_task.delay",
        lambda cid, **kwargs: captured.append(cid),
    )
    return captured


# --------------------------------------------------------------------------
# Wallet/allowance test-setup helpers — direct service calls against
# db_session, not re-deriving the full recharge/webhook flow (already
# covered by test_14_wallet_recharge.py).
# --------------------------------------------------------------------------


def _fund_wallet(db_session, user_id: uuid.UUID, amount_inr: str) -> None:
    billing.credit_wallet(db_session, user_id, Decimal(amount_inr), "recharge_credit")


def _exhaust_free_allowance(db_session, user_id: uuid.UUID, action_type: str, count: int = 20) -> None:
    for _ in range(count):
        billing.charge_for_action(db_session, user_id, action_type)


def _allowance_used_count(db_session, user_id: uuid.UUID, action_type: str) -> int:
    allowance = db_session.scalar(
        select(FreeActionAllowance).where(
            FreeActionAllowance.user_id == user_id, FreeActionAllowance.action_type == action_type
        )
    )
    return 0 if allowance is None else allowance.used_count


# ==========================================================================
# 1. Free tier — first 20 actions of a type are free regardless of balance.
# ==========================================================================


def test_reprocess_succeeds_free_at_zero_balance_and_increments_allowance(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(monkeypatch, _empty_fields())
    process_card(card_id)  # -> status="failed"

    _patch_process_delay(monkeypatch)
    resp = client.post(f"/cards/{card_id}/reprocess")
    assert resp.status_code == 200, resp.text

    assert _allowance_used_count(db_session, user_id, "parse") == 1
    assert db_session.scalar(select(WalletTransaction).where(WalletTransaction.user_id == user_id)) is None
    wallet = db_session.scalar(select(Wallet).where(Wallet.user_id == user_id))
    assert wallet is None or wallet.balance_inr == Decimal("0")


# ==========================================================================
# 2. Zero-balance hard stop — 21st action of a type, no funds, is blocked.
# ==========================================================================


def test_reprocess_returns_402_after_free_allowance_exhausted_at_zero_balance(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])

    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(monkeypatch, _empty_fields())
    process_card(card_id)  # -> status="failed"; a bare setup call, billed
    # defaults to False, so its own now-refunding failure path would
    # decrement used_count — exhaust the allowance AFTER this setup call,
    # not before, so that side effect doesn't undercut the precondition.
    _exhaust_free_allowance(db_session, user_id, "parse")

    delayed = _patch_process_delay(monkeypatch)
    resp = client.post(f"/cards/{card_id}/reprocess")

    assert resp.status_code == 402, resp.text
    assert delayed == [], "a blocked reprocess must never enqueue process_card"

    db_session.expire_all()
    row = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert row.status == "failed", "a blocked reprocess must leave the card exactly as it was"
    assert _allowance_used_count(db_session, user_id, "parse") == 20, "blocked action must not count"


def test_enrich_company_returns_402_after_free_allowance_exhausted_at_zero_balance(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(monkeypatch, _fields(company_name=_unique_company_name("EnrichBlocked")))
    process_card(card_id)  # -> status="extracted", company enrichment_status="pending"

    _exhaust_free_allowance(db_session, user_id, "enrichment")

    delayed = _patch_enrich_delay(monkeypatch)
    resp = client.post(f"/cards/{card_id}/enrich-company")

    assert resp.status_code == 402, resp.text
    assert delayed == []
    assert _allowance_used_count(db_session, user_id, "enrichment") == 20


def test_score_card_returns_402_after_free_allowance_exhausted_at_zero_balance(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(monkeypatch, _fields())
    process_card(card_id)  # -> status="extracted"

    _exhaust_free_allowance(db_session, user_id, "scoring")

    delayed = _patch_score_delay(monkeypatch)
    resp = client.post(f"/cards/{card_id}/score")

    assert resp.status_code == 402, resp.text
    assert delayed == []

    db_session.expire_all()
    row = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert row.lead_score is None, "a blocked score action must never run scoring"
    assert _allowance_used_count(db_session, user_id, "scoring") == 20


# ==========================================================================
# 3. Funded wallet — 21st action succeeds and debits exactly the rate.
# ==========================================================================


def test_reprocess_succeeds_and_debits_wallet_after_free_allowance_exhausted_when_funded(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])

    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(monkeypatch, _empty_fields())
    process_card(card_id)  # -> status="failed"; see the 402 test above for
    # why the allowance is exhausted after this bare setup call, not before.
    _exhaust_free_allowance(db_session, user_id, "parse")
    _fund_wallet(db_session, user_id, "100")

    delayed = _patch_process_delay(monkeypatch)
    resp = client.post(f"/cards/{card_id}/reprocess")
    assert resp.status_code == 200, resp.text
    assert delayed == [card_id]

    db_session.expire_all()
    wallet = db_session.scalar(select(Wallet).where(Wallet.user_id == user_id))
    assert wallet.balance_inr == Decimal("95"), "parse rate is INR 5 (seeded by migration 0010)"

    txn = db_session.scalar(
        select(WalletTransaction).where(
            WalletTransaction.user_id == user_id, WalletTransaction.transaction_type == "parse_debit"
        )
    )
    assert txn is not None
    assert txn.amount_inr == Decimal("-5")
    assert txn.balance_after_inr == Decimal("95")
    assert txn.reference_id == uuid.UUID(card_id)
    assert _allowance_used_count(db_session, user_id, "parse") == 21


# ==========================================================================
# 4. Independence — exhausting one action type never affects the others.
# ==========================================================================


def test_free_allowances_are_independent_across_action_types(client, fake_otp_provider, db_session):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    _exhaust_free_allowance(db_session, user_id, "parse")

    assert billing.get_free_actions_remaining(db_session, user_id, "parse") == 0
    assert billing.get_free_actions_remaining(db_session, user_id, "enrichment") == 20
    assert billing.get_free_actions_remaining(db_session, user_id, "scoring") == 20

    # A fresh enrichment/scoring action for this user is still free.
    billed_enrichment = billing.charge_for_action(db_session, user_id, "enrichment")
    billed_scoring = billing.charge_for_action(db_session, user_id, "scoring")
    assert billed_enrichment is False
    assert billed_scoring is False
    assert billing.get_free_actions_remaining(db_session, user_id, "enrichment") == 19
    assert billing.get_free_actions_remaining(db_session, user_id, "scoring") == 19


# ==========================================================================
# 5. GET /wallet reports free_actions_remaining, floored at 0.
# ==========================================================================


def test_get_wallet_returns_free_actions_remaining_for_all_three_types(
    client, fake_otp_provider, db_session
):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])

    resp = client.get("/wallet")
    assert resp.status_code == 200, resp.text
    assert resp.json()["free_actions_remaining"] == {"parse": 20, "enrichment": 20, "scoring": 20}

    _exhaust_free_allowance(db_session, user_id, "scoring")
    _fund_wallet(db_session, user_id, "10")
    billing.charge_for_action(db_session, user_id, "scoring")  # 21st — billed, not free

    resp2 = client.get("/wallet")
    assert resp2.status_code == 200, resp2.text
    remaining = resp2.json()["free_actions_remaining"]
    assert remaining["scoring"] == 0, "must floor at 0, never go negative"
    assert remaining["parse"] == 20
    assert remaining["enrichment"] == 20


# ==========================================================================
# 6. Bulk endpoints report wallet_blocked_count separately from
#    enqueued_count/skipped_count, without failing the rest of the batch.
# ==========================================================================


def test_bulk_process_reports_wallet_blocked_count_and_leaves_card_retryable(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    _exhaust_free_allowance(db_session, user_id, "parse")

    card_id = _upload_one(client, jpeg_bytes)  # status="new"

    delayed = _patch_process_delay(monkeypatch)
    resp = client.post("/cards/process", json={"card_ids": [card_id]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enqueued_count"] == 0
    assert body["wallet_blocked_count"] == 1
    assert delayed == []

    db_session.expire_all()
    row = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert row.status == "new", "a wallet-blocked card must stay retryable, not stuck mid-flip"


def test_bulk_enrich_reports_wallet_blocked_count_separately_from_skipped(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(monkeypatch, _fields(company_name=_unique_company_name("BulkEnrichBlocked")))
    process_card(card_id)  # -> extracted, company pending

    _exhaust_free_allowance(db_session, user_id, "enrichment")

    delayed = _patch_enrich_delay(monkeypatch)
    resp = client.post("/cards/enrich-companies", json={"card_ids": [card_id]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enqueued_count"] == 0
    assert body["skipped_count"] == 0
    assert body["wallet_blocked_count"] == 1
    assert delayed == []


def test_bulk_score_reports_wallet_blocked_count_separately_from_skipped(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(monkeypatch, _fields())
    process_card(card_id)  # -> extracted

    _exhaust_free_allowance(db_session, user_id, "scoring")

    delayed = _patch_score_delay(monkeypatch)
    resp = client.post("/cards/score", json={"card_ids": [card_id]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enqueued_count"] == 0
    assert body["skipped_count"] == 0
    assert body["wallet_blocked_count"] == 1
    assert delayed == []


# ==========================================================================
# 7. Bulk actions consolidate into a single collective WalletTransaction
#    (quantity=N), never one ledger row per card in the batch.
# ==========================================================================


def test_bulk_process_multiple_cards_writes_one_collective_transaction(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    _exhaust_free_allowance(db_session, user_id, "parse")
    _fund_wallet(db_session, user_id, "100")

    card_ids = [_upload_one(client, jpeg_bytes, filename=f"card-{i}.jpg") for i in range(3)]

    delayed = _patch_process_delay(monkeypatch)
    resp = client.post("/cards/process", json={"card_ids": card_ids})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enqueued_count"] == 3
    assert body["wallet_blocked_count"] == 0
    assert set(delayed) == set(card_ids)

    db_session.expire_all()
    txns = db_session.scalars(
        select(WalletTransaction).where(
            WalletTransaction.user_id == user_id, WalletTransaction.transaction_type == "parse_debit"
        )
    ).all()
    assert len(txns) == 1, "one batch of 3 cards must write exactly one collective ledger row, not three"
    txn = txns[0]
    assert txn.quantity == 3
    assert txn.amount_inr == Decimal("-15"), "3 cards * rate 5"
    assert txn.reference_id is None, "a genuinely collective (>1 card) row has no single card to reference"

    wallet = db_session.scalar(select(Wallet).where(Wallet.user_id == user_id))
    assert wallet.balance_inr == Decimal("85")
    assert _allowance_used_count(db_session, user_id, "parse") == 23


def test_bulk_process_charges_only_the_affordable_subset_as_one_collective_transaction(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    _exhaust_free_allowance(db_session, user_id, "parse")
    _fund_wallet(db_session, user_id, "10")  # only covers 2 of 3 cards at rate 5

    card_ids = [_upload_one(client, jpeg_bytes, filename=f"partial-{i}.jpg") for i in range(3)]

    delayed = _patch_process_delay(monkeypatch)
    resp = client.post("/cards/process", json={"card_ids": card_ids})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enqueued_count"] == 2
    assert body["wallet_blocked_count"] == 1
    assert len(delayed) == 2

    db_session.expire_all()
    txns = db_session.scalars(
        select(WalletTransaction).where(
            WalletTransaction.user_id == user_id, WalletTransaction.transaction_type == "parse_debit"
        )
    ).all()
    assert len(txns) == 1, "a partially-affordable batch must still write only one collective row"
    assert txns[0].quantity == 2
    assert txns[0].amount_inr == Decimal("-10")

    wallet = db_session.scalar(select(Wallet).where(Wallet.user_id == user_id))
    assert wallet.balance_inr == Decimal("0")

    # process_card.delay is mocked (never runs the real task), so a card's
    # status never flips regardless of whether it was enqueued — the only
    # real signal of "was this one charged and enqueued" is membership in
    # the captured delayed-calls list, not VisitingCard.status.
    not_enqueued = [cid for cid in card_ids if cid not in delayed]
    assert len(not_enqueued) == 1, "exactly one of the three cards must be left un-enqueued/blocked"
    assert all(db_session.get(VisitingCard, uuid.UUID(cid)).status == "new" for cid in card_ids), (
        "a mocked .delay() never mutates card state either way — all three stay 'new' regardless"
    )


def test_bulk_process_exactly_one_chargeable_card_keeps_its_reference_id(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    _exhaust_free_allowance(db_session, user_id, "parse")
    _fund_wallet(db_session, user_id, "5")

    card_id = _upload_one(client, jpeg_bytes, filename="solo.jpg")

    _patch_process_delay(monkeypatch)
    resp = client.post("/cards/process", json={"card_ids": [card_id]})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"enqueued_count": 1, "wallet_blocked_count": 0}

    db_session.expire_all()
    txn = db_session.scalar(
        select(WalletTransaction).where(
            WalletTransaction.user_id == user_id, WalletTransaction.transaction_type == "parse_debit"
        )
    )
    assert txn.quantity == 1
    assert txn.reference_id == uuid.UUID(card_id), (
        "a batch that happens to charge exactly one card should still carry its reference_id"
    )


# ==========================================================================
# 8. Code-review fixes: duplicate-id dedup, enqueue-failure refund, and the
#    reprocess_card row-lock closing a concurrent-duplicate-request race.
# ==========================================================================


def test_bulk_score_deduplicates_a_repeated_card_id_in_one_request(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """A client sending the same card_id N times in one /cards/score request
    must be charged/enqueued for it at most once, never N times over."""
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(monkeypatch, _fields())
    process_card(card_id)  # -> extracted

    delayed = _patch_score_delay(monkeypatch)
    resp = client.post("/cards/score", json={"card_ids": [card_id, card_id, card_id]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enqueued_count"] == 1, "one card repeated 3x must enqueue exactly once"
    assert body["skipped_count"] == 2, "the 2 duplicate repeats must be skipped, not charged"
    assert body["wallet_blocked_count"] == 0
    assert delayed == [card_id]
    assert _allowance_used_count(db_session, user_id, "scoring") == 1, (
        "a card repeated 3x in one request must count as exactly one scoring action"
    )


def test_reprocess_refunds_the_charge_when_enqueue_itself_fails(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """If process_card.delay() itself raises (never reaches the broker), the
    already-applied charge must be reversed — the work was paid for but
    never even queued, unlike a task that ran and failed."""
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])

    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(monkeypatch, _empty_fields())
    process_card(card_id)  # -> failed; see the 402 test's comment for why
    # the allowance is exhausted after this bare setup call, not before.
    _exhaust_free_allowance(db_session, user_id, "parse")
    _fund_wallet(db_session, user_id, "100")

    monkeypatch.setattr(
        "app.services.card_service.process_card.delay",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("broker unreachable")),
    )

    resp = client.post(f"/cards/{card_id}/reprocess")
    assert resp.status_code == 200, resp.text

    db_session.expire_all()
    wallet = db_session.scalar(select(Wallet).where(Wallet.user_id == user_id))
    assert wallet.balance_inr == Decimal("100"), "the debit must be refunded when enqueue fails"

    refund_txn = db_session.scalar(
        select(WalletTransaction).where(
            WalletTransaction.user_id == user_id, WalletTransaction.transaction_type == "parse_refund"
        )
    )
    assert refund_txn is not None
    assert refund_txn.amount_inr == Decimal("5")
    assert refund_txn.reference_id == uuid.UUID(card_id)

    assert _allowance_used_count(db_session, user_id, "parse") == 20, (
        "the refund must also undo the allowance increment the failed enqueue caused"
    )


def test_reprocess_concurrent_duplicate_requests_charge_exactly_once(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """Two concurrent reprocess requests for the same failed card must
    result in exactly one charge. get_visible_card(for_update=True) locks
    the card row for the whole eligibility-check + charge + status-flip
    sequence (folded into charge_for_action's own commit) — the second
    request's lock acquisition blocks until the first commits, then
    re-reads status='new' (already flipped) and is correctly rejected,
    instead of both requests racing past the 'failed' check and both
    getting charged for one reprocess."""
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(monkeypatch, _empty_fields())
    process_card(card_id)  # -> failed

    monkeypatch.setattr("app.services.card_service.process_card.delay", lambda *_a, **_k: None)

    results: list[str] = []
    barrier = threading.Barrier(2)

    def attempt() -> None:
        session = TestSessionLocal()
        try:
            barrier.wait(timeout=5)
            user_obj = session.get(User, user_id)
            try:
                card_service.reprocess_card(session, user_obj, uuid.UUID(card_id))
                results.append("ok")
            except InvalidReprocessStateError:
                results.append("rejected")
            except Exception as exc:  # pragma: no cover - diagnostic only
                results.append(f"error:{exc!r}")
        finally:
            session.close()

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert sorted(results) == ["ok", "rejected"], (
        f"exactly one of two concurrent reprocess attempts may succeed, got {results}"
    )
    assert _allowance_used_count(db_session, user_id, "parse") == 1, (
        "only one of the two concurrent requests may actually be charged"
    )


# ==========================================================================
# 9. Task-level refund: the Celery task itself refunds a charge when the
#    actual parse/enrich/score work permanently fails, not just when the
#    .delay() enqueue call itself never reaches the broker (section 8).
# ==========================================================================


def test_process_card_task_refunds_billed_charge_on_permanent_extraction_failure(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """A card that was billed (wallet-debited) for its parse must be
    refunded if extraction permanently fails — the work was attempted but
    never actually completed, so the user shouldn't be left charged for it."""
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    _exhaust_free_allowance(db_session, user_id, "parse")
    _fund_wallet(db_session, user_id, "100")

    card_id = _upload_one(client, jpeg_bytes)

    _patch_process_delay(monkeypatch)
    resp = client.post("/cards/process", json={"card_ids": [card_id]})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"enqueued_count": 1, "wallet_blocked_count": 0}

    db_session.expire_all()
    wallet = db_session.scalar(select(Wallet).where(Wallet.user_id == user_id))
    assert wallet.balance_inr == Decimal("95"), "fixture setup: the parse charge must land first"

    # Simulate the real (mocked-away) Celery task actually running, told it
    # was billed — matching what card_service now passes into .delay().
    _patch_vision(monkeypatch, _empty_fields())
    process_card.apply(args=(card_id,), kwargs={"billed": True})

    db_session.expire_all()
    row = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert row.status == "failed"

    wallet = db_session.scalar(select(Wallet).where(Wallet.user_id == user_id))
    assert wallet.balance_inr == Decimal("100"), "a permanently-failed parse must refund the charge"

    refund_txn = db_session.scalar(
        select(WalletTransaction).where(
            WalletTransaction.user_id == user_id, WalletTransaction.transaction_type == "parse_refund"
        )
    )
    assert refund_txn is not None
    assert refund_txn.amount_inr == Decimal("5")
    assert refund_txn.reference_id == uuid.UUID(card_id)
    assert _allowance_used_count(db_session, user_id, "parse") == 20, (
        "the refund must also undo the allowance increment the failed task caused"
    )


def test_process_card_task_free_action_refund_only_decrements_allowance(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """A free (not wallet-billed) parse that permanently fails must still
    give back its free-allowance slot, with no wallet/ledger involvement."""
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    card_id = _upload_one(client, jpeg_bytes)

    _patch_process_delay(monkeypatch)
    resp = client.post("/cards/process", json={"card_ids": [card_id]})
    assert resp.status_code == 200, resp.text

    assert _allowance_used_count(db_session, user_id, "parse") == 1

    _patch_vision(monkeypatch, _empty_fields())
    process_card.apply(args=(card_id,), kwargs={"billed": False})

    db_session.expire_all()
    assert _allowance_used_count(db_session, user_id, "parse") == 0, (
        "a permanently-failed free parse must give its free slot back"
    )
    assert db_session.scalar(select(WalletTransaction).where(WalletTransaction.user_id == user_id)) is None, (
        "a free action's refund must never touch the wallet/ledger"
    )


def test_score_card_task_refunds_billed_charge_on_permanent_scoring_failure(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """Scoring has no persistent 'failed' status (see scoring_processing.py),
    but a permanently-failed scoring attempt must still refund the charge —
    the refund is the only durable trace this failure leaves."""
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    _exhaust_free_allowance(db_session, user_id, "scoring")
    _fund_wallet(db_session, user_id, "100")

    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(monkeypatch, _fields())
    process_card(card_id)  # -> extracted

    _patch_score_delay(monkeypatch)
    resp = client.post(f"/cards/{card_id}/score")
    assert resp.status_code == 200, resp.text

    db_session.expire_all()
    wallet = db_session.scalar(select(Wallet).where(Wallet.user_id == user_id))
    assert wallet.balance_inr == Decimal("98"), "fixture setup: the scoring charge (rate 2) must land first"

    monkeypatch.setattr(
        "app.services.scoring.calculate_score",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("scoring blew up")),
    )
    score_card_task.apply(args=(card_id,), kwargs={"billed": True})

    db_session.expire_all()
    row = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert row.lead_score is None, "scoring never completed"

    wallet = db_session.scalar(select(Wallet).where(Wallet.user_id == user_id))
    assert wallet.balance_inr == Decimal("100"), "a permanently-failed scoring attempt must refund the charge"

    refund_txn = db_session.scalar(
        select(WalletTransaction).where(
            WalletTransaction.user_id == user_id, WalletTransaction.transaction_type == "scoring_refund"
        )
    )
    assert refund_txn is not None
    assert refund_txn.amount_inr == Decimal("2")
