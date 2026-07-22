"""
Tests for the `24-company-linkage-tiered-expiry` feature (spec:
`.claude/specs/24-company-linkage-tiered-expiry.md`).

Two independent mechanisms:

1. `CompanySignals.factual_fetched_at`/`dynamic_fetched_at` — two freshness
   clocks replacing the old single `updated_at`, gating which lookup group
   `enrichment_service.run_all_signal_lookups`/`enrich_company_task` re-run
   on an already-`"enriched"` company. Shared, cross-org — never per-user.
2. `VisitingCard.company_enriched_at` — a separate, per-lead 30-day billed
   cooldown (`lead_cooldown_service.py`), independent of the clocks above,
   that unlocks a billed re-enrich or billed rescore on one specific lead
   regardless of whether the shared cache actually needed a re-fetch.

Plus `Company.linked_org_id` — tags a scanned company as itself being a
registered DASHR org, matched by normalized name against `SellerProfile.
company_name`, preferring that org's declared `product_lines` over a
scraped guess.

Mocking strategy mirrors `test_07_data_enrichment.py`/`test_20_field_correction.py`:
vision extraction via `app.services.vision_client.extract_card_fields`;
Celery `.delay()` calls mocked at their call-site import location; provider
factories monkeypatched to tracking stubs where a test needs to assert a
source was (or was not) invoked.
"""

from __future__ import annotations

import io
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app.models.company import Company
from app.models.company_signals import CompanySignals
from app.models.free_action_allowance import FreeActionAllowance
from app.models.organization import Organization
from app.models.seller_profile import SellerProfile
from app.models.user import User
from app.models.visiting_card import VisitingCard
from app.services import enrichment_service, lead_cooldown_service
from app.workers.card_processing import process_card
from app.workers.enrichment_processing import enrich_company_task
from app.workers.scoring_processing import score_card_task
from conftest import create_verified_user

# --------------------------------------------------------------------------
# Shared helpers (copied, not imported, from test_07_data_enrichment.py /
# test_20_field_correction.py — this repo's established per-file convention).
# --------------------------------------------------------------------------


def _unique_company_name(label: str) -> str:
    return f"{label} {uuid.uuid4().hex[:10]} Pvt Ltd"


def _image_bytes(fmt: str = "JPEG", size: tuple[int, int] = (20, 20), color: str = "blue") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format=fmt)
    return buf.getvalue()


@pytest.fixture
def jpeg_bytes() -> bytes:
    return _image_bytes("JPEG")


def _login(client: TestClient, user: dict) -> None:
    resp = client.post("/auth/login", json={"email": user["email"], "password": user["password"]})
    assert resp.status_code == 200, resp.text


def _authenticated_user(client: TestClient, fake_otp_provider, **overrides) -> dict:
    user = create_verified_user(client, fake_otp_provider, **overrides)
    _login(client, user)
    return user


def _upload_one(client: TestClient, jpeg_bytes: bytes, filename: str = "card.jpg") -> str:
    resp = client.post(
        "/cards/bulk-upload", data={}, files=[("files", (filename, jpeg_bytes, "image/jpeg"))]
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["cards"][0]["card_id"]


def _patch_vision(monkeypatch: pytest.MonkeyPatch, *responses) -> None:
    queue = list(responses)

    def _fake(image_bytes: bytes, media_type: str):
        return queue.pop(0)

    monkeypatch.setattr("app.services.vision_client.extract_card_fields", _fake)


def _fields(
    *,
    full_name: str | None = "Extracted Contact",
    job_title: str | None = None,
    company_name: str | None = None,
    website: str | None = None,
    address: str | None = None,
    products_offered: str | None = None,
    emails: list[dict] | None = None,
    phones: list[dict] | None = None,
    gst_number: str | None = None,
) -> dict:
    return {
        "is_back_of_card": False,
        "full_name": full_name,
        "job_title": job_title,
        "company_name": company_name,
        "website": website,
        "address": address,
        "products_offered": products_offered,
        "special_remark": None,
        "raw_ocr_text": "verbatim card text",
        "emails": [] if emails is None else emails,
        "phones": [] if phones is None else phones,
        "gst_number": gst_number,
    }


def _extracted_card(
    client: TestClient,
    jpeg_bytes: bytes,
    monkeypatch: pytest.MonkeyPatch,
    **field_overrides,
) -> str:
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(monkeypatch, _fields(**field_overrides))
    process_card(card_id)
    return card_id


def _create_pending_company(db_session, name: str | None = None, website: str | None = None) -> uuid.UUID:
    name = name or _unique_company_name("Linkage Target")
    company = Company(name=name, normalized_name=" ".join(name.strip().lower().split()), website=website)
    db_session.add(company)
    db_session.commit()
    db_session.refresh(company)
    assert company.enrichment_status == "pending", "fixture setup: a fresh company must start pending"
    return company.company_id


def _patch_enrich_delay(monkeypatch: pytest.MonkeyPatch) -> list[tuple[tuple, dict]]:
    captured: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        "app.services.card_service.enrich_company_task.delay",
        lambda *args, **kwargs: captured.append((args, kwargs)),
    )
    return captured


def _patch_score_delay(monkeypatch: pytest.MonkeyPatch) -> list[tuple[tuple, dict]]:
    captured: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        "app.services.card_service.score_card_task.delay",
        lambda *args, **kwargs: captured.append((args, kwargs)),
    )
    return captured


def _allowance_used_count(db_session, user_id: uuid.UUID, action_type: str) -> int:
    allowance = db_session.scalar(
        select(FreeActionAllowance).where(
            FreeActionAllowance.user_id == user_id, FreeActionAllowance.action_type == action_type
        )
    )
    return 0 if allowance is None else allowance.used_count


def _exhaust_free_allowance(db_session, user_id: uuid.UUID, action_type: str, count: int = 20) -> None:
    from app.services import billing
    for _ in range(count):
        billing.charge_for_action(db_session, user_id, action_type)


def _fund_wallet(db_session, user_id: uuid.UUID, amount_inr: str) -> None:
    from decimal import Decimal
    from app.services import billing
    billing.credit_wallet(db_session, user_id, Decimal(amount_inr), "recharge_credit")


def _mark_scored(db_session, card_id: uuid.UUID, scored_at: datetime | None = None) -> None:
    card = db_session.get(VisitingCard, card_id)
    card.lead_score = 42
    card.score_breakdown = {
        "designation_score": 10, "company_size_score": 10, "industry_fit_score": 10,
        "momentum_signal_score": 10, "remark_signal_score": 2, "total": 42, "version": "v1",
    }
    card.scored_at = scored_at or datetime.now(timezone.utc)
    db_session.commit()


def _make_org_admin_with_profile(
    db_session, client: TestClient, fake_otp_provider, company_name: str, product_lines: str | None = None
) -> tuple[Organization, dict]:
    """Signs up a real user (real bcrypt password path), then directly wires
    it up as an org admin with a SellerProfile — mirrors _mark_scored's
    "test eligibility, not the full flow" convention rather than driving the
    org-creation/invite HTTP flow, since this feature only reads
    SellerProfile/User/Organization, never writes them."""
    user = create_verified_user(client, fake_otp_provider)
    org = Organization(name=f"Org for {company_name}")
    db_session.add(org)
    db_session.flush()

    db_user = db_session.get(User, uuid.UUID(user["user_id"]))
    db_user.org_id = org.org_id
    db_user.role = "admin"
    db_session.add(
        SellerProfile(user_id=db_user.user_id, company_name=company_name, product_lines=product_lines)
    )
    db_session.commit()
    db_session.refresh(org)
    return org, user


class _RaisingWebsiteProvider:
    """A website provider that must never be called — used to prove the
    linked-org product-lines preference actually skips the scrape entirely,
    not just overwrites its result afterward."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def lookup(self, website: str):
        self.calls.append(website)
        raise AssertionError("website_signal_provider must not be called once linked_org_id is set")


class _TrackingRegistryProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def lookup(self, company_name: str):
        self.calls.append(company_name)
        from app.services.enrichment_providers.registry_provider import RegistryResult
        return RegistryResult()


class _TrackingHiringSignalProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def lookup(self, company_name: str):
        self.calls.append(company_name)
        from app.services.enrichment_providers.hiring_signal_provider import HiringSignalResult
        return HiringSignalResult()


# ==========================================================================
# 1. stale_tiers — pure function.
# ==========================================================================


def test_stale_tiers_none_row_returns_nothing_stale():
    """A missing CompanySignals row for an "enriched" company is an
    inconsistent state that can't happen via the real enrichment path
    (run_all_signal_lookups always creates one first) — treated
    conservatively as nothing-to-refresh rather than assuming both tiers
    need fetching."""
    assert enrichment_service.stale_tiers(None) == []


def test_stale_tiers_fresh_row_is_stale_in_neither_tier(db_session):
    company_id = _create_pending_company(db_session)
    now = datetime.now(timezone.utc)
    signals = CompanySignals(company_id=company_id, factual_fetched_at=now, dynamic_fetched_at=now)
    db_session.add(signals)
    db_session.commit()
    assert enrichment_service.stale_tiers(signals) == []


def test_stale_tiers_only_flags_the_expired_tier(db_session):
    company_id = _create_pending_company(db_session)
    now = datetime.now(timezone.utc)
    signals = CompanySignals(
        company_id=company_id,
        factual_fetched_at=now - timedelta(days=181),
        dynamic_fetched_at=now,
    )
    db_session.add(signals)
    db_session.commit()
    assert enrichment_service.stale_tiers(signals) == ["factual"]

    signals.factual_fetched_at = now
    signals.dynamic_fetched_at = now - timedelta(days=91)
    db_session.commit()
    assert enrichment_service.stale_tiers(signals) == ["dynamic"]


# ==========================================================================
# 2. First-ever enrichment sets both clocks; immediate re-call still 409s.
# ==========================================================================


def test_first_enrichment_sets_both_clocks_and_stamps_card_cooldown_anchor(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch, company_name=_unique_company_name("Fresh Co"))
    card = db_session.get(VisitingCard, uuid.UUID(card_id))

    enrich_company_task(str(card.company_id), str(card.card_id))

    db_session.expire_all()
    company = db_session.get(Company, card.company_id)
    signals = db_session.get(CompanySignals, card.company_id)
    assert company.enrichment_status in ("enriched", "not_found")
    assert signals.factual_fetched_at is not None
    assert signals.dynamic_fetched_at is not None

    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert card.company_enriched_at is not None, (
        "the triggering card's own cooldown anchor must be stamped on first-run completion"
    )


def test_enrich_endpoint_immediately_after_first_run_still_returns_409(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch, company_name=_unique_company_name("Immediate Co"))
    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    enrich_company_task(str(card.company_id), str(card.card_id))
    db_session.expire_all()

    company = db_session.get(Company, card.company_id)
    if company.enrichment_status != "enriched":
        company.enrichment_status = "enriched"  # force the branch under test regardless of stub outcome
        db_session.commit()

    resp = client.post(f"/cards/{card_id}/enrich-company")
    assert resp.status_code == 409, resp.text


# ==========================================================================
# 3. Tiered refresh — only the stale tier's lookups re-run at the task level.
# ==========================================================================


def test_refresh_only_reruns_the_stale_factual_tier(db_session, monkeypatch):
    company_id = _create_pending_company(db_session)
    now = datetime.now(timezone.utc)
    stale_factual_at = now - timedelta(days=181)
    fresh_dynamic_at = now
    signals = CompanySignals(
        company_id=company_id, factual_fetched_at=stale_factual_at, dynamic_fetched_at=fresh_dynamic_at
    )
    db_session.add(signals)
    company = db_session.get(Company, company_id)
    company.enrichment_status = "enriched"
    db_session.commit()

    registry_tracking = _TrackingRegistryProvider()
    hiring_tracking = _TrackingHiringSignalProvider()
    monkeypatch.setattr(
        "app.services.enrichment_service.registry_provider.get_registry_provider",
        lambda: registry_tracking,
    )
    monkeypatch.setattr(
        "app.services.enrichment_service.hiring_signal_provider.get_hiring_signal_provider",
        lambda: hiring_tracking,
    )

    enrich_company_task(str(company_id), refresh_tiers=["factual"])

    assert registry_tracking.calls != [], "the stale factual tier must actually re-run"
    assert hiring_tracking.calls == [], "the still-fresh dynamic tier must never be touched"

    db_session.expire_all()
    signals = db_session.get(CompanySignals, company_id)
    assert signals.factual_fetched_at is not None and signals.factual_fetched_at > stale_factual_at
    assert signals.dynamic_fetched_at == fresh_dynamic_at, (
        "dynamic_fetched_at must be left exactly as it was — this run never touched that tier"
    )
    refreshed_company = db_session.get(Company, company_id)
    assert refreshed_company.enrichment_status == "enriched", "a refresh never changes enrichment_status"


def test_refresh_intersects_caller_tiers_with_freshly_read_staleness(db_session, monkeypatch):
    """Closes the two-concurrent-refreshes race: even if the caller asks for
    a tier, the task itself re-checks staleness right before fetching — a
    tier that's actually fresh (e.g. another run just refreshed it) must
    never be re-fetched just because the caller's stale computation is now
    out of date."""
    company_id = _create_pending_company(db_session)
    now = datetime.now(timezone.utc)
    signals = CompanySignals(company_id=company_id, factual_fetched_at=now, dynamic_fetched_at=now)
    db_session.add(signals)
    company = db_session.get(Company, company_id)
    company.enrichment_status = "enriched"
    db_session.commit()

    registry_tracking = _TrackingRegistryProvider()
    monkeypatch.setattr(
        "app.services.enrichment_service.registry_provider.get_registry_provider",
        lambda: registry_tracking,
    )

    # Caller asks for "factual" as if it were stale, but it's actually fresh.
    enrich_company_task(str(company_id), refresh_tiers=["factual"])

    assert registry_tracking.calls == [], (
        "the task must re-derive staleness itself and skip a tier that's actually fresh"
    )


def test_refresh_never_transitions_through_enriching_status(db_session):
    company_id = _create_pending_company(db_session)
    now = datetime.now(timezone.utc)
    db_session.add(CompanySignals(company_id=company_id, factual_fetched_at=None, dynamic_fetched_at=now))
    company = db_session.get(Company, company_id)
    company.enrichment_status = "enriched"
    db_session.commit()

    # Sanity: a refresh run reads enrichment_status only to guard eligibility
    # (must be "enriched"), never flips it to "enriching" mid-run — verified
    # indirectly by asserting the final status is still "enriched" and the
    # task never raised on an "already enriching" collision guard.
    enrich_company_task(str(company_id), refresh_tiers=["factual"])
    db_session.expire_all()
    assert db_session.get(Company, company_id).enrichment_status == "enriched"


# ==========================================================================
# 4. refresh_available in GET /cards/{id}.
# ==========================================================================


def test_refresh_available_toggles_with_company_staleness(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch, company_name=_unique_company_name("Toggle Co"))
    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    enrich_company_task(str(card.company_id), str(card.card_id))
    db_session.expire_all()

    company = db_session.get(Company, card.company_id)
    company.enrichment_status = "enriched"
    db_session.commit()

    detail = client.get(f"/cards/{card_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["company"]["refresh_available"] is False, "freshly enriched, nothing stale or cooled down"

    signals = db_session.get(CompanySignals, card.company_id)
    signals.factual_fetched_at = datetime.now(timezone.utc) - timedelta(days=181)
    db_session.commit()

    detail = client.get(f"/cards/{card_id}")
    assert detail.json()["company"]["refresh_available"] is True, "a stale tier must flip this to true"


# ==========================================================================
# 5. Company org-linkage matching.
# ==========================================================================


def test_match_linked_org_single_match_links_company(client, fake_otp_provider, db_session):
    company_name = _unique_company_name("Matchable Customer")
    org, _user = _make_org_admin_with_profile(db_session, client, fake_otp_provider, company_name)
    company_id = _create_pending_company(db_session, name=company_name)
    company = db_session.get(Company, company_id)

    matched = enrichment_service.match_linked_org(db_session, company)
    assert matched is not None
    assert matched.org_id == org.org_id


def test_match_linked_org_ambiguous_names_stays_unlinked(client, fake_otp_provider, db_session):
    company_name = _unique_company_name("Ambiguous Customer")
    _make_org_admin_with_profile(db_session, client, fake_otp_provider, company_name)
    _make_org_admin_with_profile(db_session, client, fake_otp_provider, company_name)
    company_id = _create_pending_company(db_session, name=company_name)
    company = db_session.get(Company, company_id)

    assert enrichment_service.match_linked_org(db_session, company) is None


def test_match_linked_org_no_match_returns_none(db_session):
    company_id = _create_pending_company(db_session, name=_unique_company_name("Nobody Owns This Co"))
    company = db_session.get(Company, company_id)
    assert enrichment_service.match_linked_org(db_session, company) is None


def test_linked_org_product_lines_preferred_over_website_scrape(client, fake_otp_provider, db_session, monkeypatch):
    company_name = _unique_company_name("Linked Product Lines Co")
    _make_org_admin_with_profile(
        db_session, client, fake_otp_provider, company_name, product_lines="Industrial pumps and valves"
    )
    company_id = _create_pending_company(db_session, name=company_name, website="https://example.com")

    raising_website = _RaisingWebsiteProvider()
    monkeypatch.setattr(
        "app.services.enrichment_service.website_signal_provider.get_website_signal_provider",
        lambda: raising_website,
    )

    enrich_company_task(str(company_id))

    db_session.expire_all()
    company = db_session.get(Company, company_id)
    signals = db_session.get(CompanySignals, company_id)
    assert company.linked_org_id is not None, "the matching org must get linked on this same run"
    assert signals.product_lines_summary == "Industrial pumps and valves"
    assert raising_website.calls == [], "the website scrape must be skipped entirely once linked"


# ==========================================================================
# 6. Per-lead billed cooldown — enrich-company.
# ==========================================================================


def test_cooldown_only_trigger_still_succeeds_billed_with_empty_refresh_tiers(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch, company_name=_unique_company_name("Cooldown Co"))
    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    enrich_company_task(str(card.company_id), str(card.card_id))
    db_session.expire_all()

    company = db_session.get(Company, card.company_id)
    company.enrichment_status = "enriched"
    signals = db_session.get(CompanySignals, card.company_id)
    now = datetime.now(timezone.utc)
    signals.factual_fetched_at = now
    signals.dynamic_fetched_at = now
    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    card.company_enriched_at = now - timedelta(days=31)
    db_session.commit()
    anchor_before = now - timedelta(days=31)

    before_used = _allowance_used_count(db_session, user_id, "enrichment")
    captured = _patch_enrich_delay(monkeypatch)

    resp = client.post(f"/cards/{card_id}/enrich-company")
    assert resp.status_code == 200, resp.text

    assert len(captured) == 1
    args, kwargs = captured[0]
    assert kwargs["refresh_tiers"] == [], "company cache is fully fresh — nothing to actually re-fetch"
    assert _allowance_used_count(db_session, user_id, "enrichment") == before_used + 1, (
        "a cooldown-only trigger must still be billed like any other enrichment action"
    )

    db_session.expire_all()
    refreshed_card = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert refreshed_card.company_enriched_at > anchor_before, (
        "the triggering card's own cooldown anchor must reset"
    )


def test_neither_stale_nor_cooldown_elapsed_returns_409(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch, company_name=_unique_company_name("Still Fresh Co"))
    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    enrich_company_task(str(card.company_id), str(card.card_id))
    db_session.expire_all()

    company = db_session.get(Company, card.company_id)
    company.enrichment_status = "enriched"
    db_session.commit()

    resp = client.post(f"/cards/{card_id}/enrich-company")
    assert resp.status_code == 409, resp.text


def test_sibling_card_cooldown_anchor_untouched_by_another_cards_refresh(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """Two cards riding on the same company: card A triggers a
    cooldown-only refresh; card B's own company_enriched_at must be
    untouched by A's run (only A's is restarted)."""
    user = _authenticated_user(client, fake_otp_provider)
    company_name = _unique_company_name("Shared Company Co")
    card_a_id = _extracted_card(client, jpeg_bytes, monkeypatch, company_name=company_name)
    card_a = db_session.get(VisitingCard, uuid.UUID(card_a_id))
    enrich_company_task(str(card_a.company_id), str(card_a.card_id))
    db_session.expire_all()

    card_b_id = _upload_one(client, jpeg_bytes, filename="card-b.jpg")
    _patch_vision(monkeypatch, _fields(full_name="Second Contact", company_name=company_name))
    process_card(card_b_id)

    card_b = db_session.get(VisitingCard, uuid.UUID(card_b_id))
    assert card_b.company_id == card_a.company_id, "fixture setup: both cards must share the same company"
    assert card_b.company_enriched_at is not None, (
        "a card linked to an already-settled company must get its own cooldown anchor immediately"
    )
    card_b_anchor_before = card_b.company_enriched_at

    company = db_session.get(Company, card_a.company_id)
    company.enrichment_status = "enriched"
    signals = db_session.get(CompanySignals, card_a.company_id)
    now = datetime.now(timezone.utc)
    signals.factual_fetched_at = now
    signals.dynamic_fetched_at = now
    card_a = db_session.get(VisitingCard, uuid.UUID(card_a_id))
    card_a.company_enriched_at = now - timedelta(days=31)
    db_session.commit()

    _patch_enrich_delay(monkeypatch)
    resp = client.post(f"/cards/{card_a_id}/enrich-company")
    assert resp.status_code == 200, resp.text

    db_session.expire_all()
    card_b_after = db_session.get(VisitingCard, uuid.UUID(card_b_id))
    assert card_b_after.company_enriched_at == card_b_anchor_before, (
        "card B's own cooldown anchor must be untouched by card A's refresh"
    )


# ==========================================================================
# 7. Per-lead billed cooldown — rescore.
# ==========================================================================


def test_monthly_rescore_available_true_only_when_no_free_rescore_pending(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch, full_name="Monthly Rescore Name")
    _mark_scored(db_session, uuid.UUID(card_id))
    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    card.company_enriched_at = datetime.now(timezone.utc) - timedelta(days=31)
    db_session.commit()

    detail = client.get(f"/cards/{card_id}")
    assert detail.json()["rescore_available"] is False
    assert detail.json()["monthly_rescore_available"] is True


def test_score_card_cooldown_rescore_is_billed_not_free(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch, full_name="Billed Rescore Name")
    _mark_scored(db_session, uuid.UUID(card_id))
    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    card.company_enriched_at = datetime.now(timezone.utc) - timedelta(days=31)
    db_session.commit()

    # Exhaust the free "scoring" allowance and fund the wallet first, so this
    # test actually exercises the wallet-debit path — with free allowance
    # still available, charge_for_action legitimately returns billed=False
    # even for the cooldown-triggered reason (same as any other action type;
    # "billed" here means "a real charge_for_action call happened", not
    # necessarily a wallet debit).
    _exhaust_free_allowance(db_session, user_id, "scoring")
    _fund_wallet(db_session, user_id, "100.00")

    before_used = _allowance_used_count(db_session, user_id, "scoring")
    captured = _patch_score_delay(monkeypatch)

    resp = client.post(f"/cards/{card_id}/score")
    assert resp.status_code == 200, resp.text

    assert len(captured) == 1
    args, kwargs = captured[0]
    assert kwargs["billed"] is True, "a cooldown-triggered rescore must be billed, unlike the free correction path"
    assert _allowance_used_count(db_session, user_id, "scoring") == before_used + 1


def test_cooldown_triggered_rescore_rearms_the_cooldown(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """A cooldown-triggered rescore must reset company_enriched_at, closing
    both the double-click race and the "stays eligible forever" gap: an
    immediate second rescore attempt right after must 409, not silently
    allow unbounded repeat-billed rescores on the same card."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch, full_name="Rearm Cooldown Name")
    _mark_scored(db_session, uuid.UUID(card_id))
    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    anchor_before = datetime.now(timezone.utc) - timedelta(days=31)
    card.company_enriched_at = anchor_before
    db_session.commit()

    _patch_score_delay(monkeypatch)
    resp = client.post(f"/cards/{card_id}/score")
    assert resp.status_code == 200, resp.text

    db_session.expire_all()
    refreshed_card = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert refreshed_card.company_enriched_at > anchor_before, (
        "a cooldown-triggered rescore must re-arm this card's own cooldown anchor"
    )

    detail = client.get(f"/cards/{card_id}")
    assert detail.json()["monthly_rescore_available"] is False, (
        "immediately after use, the cooldown must no longer be elapsed"
    )

    second_resp = client.post(f"/cards/{card_id}/score")
    assert second_resp.status_code == 409, (
        "an immediate second rescore attempt must not slip through — the anchor was just reset"
    )


def test_free_correction_rescore_takes_priority_over_billed_cooldown(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """A card eligible for both the free (correction-triggered) and billed
    (cooldown-triggered) rescore must take the free path — never billed."""
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch, full_name="Both Eligible Name")
    _mark_scored(db_session, uuid.UUID(card_id))
    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    card.company_enriched_at = datetime.now(timezone.utc) - timedelta(days=31)
    db_session.commit()

    correct_resp = client.post(
        f"/cards/{card_id}/corrections",
        json={"field_name": "full_name", "corrected_value": "Both Eligible Name Fixed"},
    )
    assert correct_resp.status_code == 200, correct_resp.text

    before_used = _allowance_used_count(db_session, user_id, "scoring")
    captured = _patch_score_delay(monkeypatch)

    resp = client.post(f"/cards/{card_id}/score")
    assert resp.status_code == 200, resp.text

    args, kwargs = captured[0]
    assert kwargs["billed"] is False, "the free correction-triggered reason must win when both apply"
    assert _allowance_used_count(db_session, user_id, "scoring") == before_used


def test_no_rescore_reason_returns_409(client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch):
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch, full_name="No Reason Name")
    _mark_scored(db_session, uuid.UUID(card_id))

    resp = client.post(f"/cards/{card_id}/score")
    assert resp.status_code == 409, resp.text


# ==========================================================================
# 8. score_card_task's own race-safe re-check of the cooldown reason.
# ==========================================================================


def test_score_card_task_allows_cooldown_triggered_rescore(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch, full_name="Task Cooldown Name")
    scored_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    _mark_scored(db_session, uuid.UUID(card_id), scored_at=scored_at)
    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    card.company_enriched_at = datetime.now(timezone.utc) - timedelta(days=31)
    db_session.commit()

    score_card_task(card_id, billed=True)  # bare call — cooldown elapsed, must actually rescore

    db_session.expire_all()
    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert card.scored_at is not None and card.scored_at > scored_at


def test_score_card_task_skips_when_cooldown_not_elapsed_and_no_correction(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch, full_name="Task No Reason Name")
    scored_at = datetime.now(timezone.utc)
    _mark_scored(db_session, uuid.UUID(card_id), scored_at=scored_at)

    score_card_task(card_id)  # bare call — neither reason true, must no-op

    db_session.expire_all()
    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert card.scored_at == scored_at, "one-shot rule: neither rescore reason true, must not re-run"


def test_score_card_task_refunds_cooldown_rescore_on_permanent_failure(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """A cooldown-triggered rescore WAS billed via charge_for_action in
    card_service (unlike the free correction path) — a permanent failure
    must refund it, mirroring a first-ever score's refund behavior."""
    from app.services import billing

    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])
    card_id = _extracted_card(client, jpeg_bytes, monkeypatch, full_name="Refund Cooldown Name")
    _mark_scored(db_session, uuid.UUID(card_id))
    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    card.company_enriched_at = datetime.now(timezone.utc) - timedelta(days=31)
    db_session.commit()

    refund_calls: list[tuple] = []
    monkeypatch.setattr(
        "app.workers.scoring_processing.billing.refund_action",
        lambda db, uid, action_type, **kwargs: refund_calls.append((uid, action_type, kwargs.get("billed"))),
    )
    monkeypatch.setattr(
        "app.services.scoring.calculate_score",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("simulated scoring failure")),
    )

    # .apply() (not a bare call) so Celery's internal self.retry()/Retry
    # exception is handled by the task machinery itself, actually re-running
    # the task body up to max_retries synchronously before finally hitting
    # the MaxRetriesExceededError branch — a bare call would let the first
    # Retry exception escape uncaught instead.
    score_card_task.apply(args=(card_id,), kwargs={"billed": True})

    assert len(refund_calls) == 1, "a billed cooldown rescore must be refunded on permanent failure"
    assert refund_calls[0][0] == user_id
    assert refund_calls[0][1] == "scoring"
    assert refund_calls[0][2] is True
