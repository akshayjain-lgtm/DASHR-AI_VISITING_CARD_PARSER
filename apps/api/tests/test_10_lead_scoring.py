"""
Tests for the `10-lead-scoring` feature (spec: `.claude/specs/10-lead-scoring.md`).

Written directly against the spec's documented contract, not against the
implementation of `services/scoring.py` or `workers/scoring_processing.py`:

- `score_card_task(card_id)` (Celery task) computes a 0-100 `lead_score` +
  `score_breakdown` JSONB from the card's designation/remark/job title,
  its linked `Company`/`CompanySignals` (if any), and the card owner's
  `SellerProfile`. It never mutates `VisitingCard.status` and is a no-op
  (no eligibility to score) unless the card's `status == "extracted"`.
- **Scoring is versioned** (`SCORING_VERSIONS`, `select_scoring_version`,
  `_SCORING_VERSION_ROLLOUT` in `scoring.py`): `v1` is the original frozen
  5-component model (`designation_score`/`company_size_score`/
  `industry_fit_score`/`momentum_signal_score`/`remark_signal_score`) kept
  only for historical cards. `v2` is the current, 100%-rollout-by-default
  8-component model (`remark_signal_score`/`product_fit_score`/
  `role_designation_score`/`proximity_score`/`expansion_signal_score`/
  `revenue_growth_score`/`company_size_score`/`marketplace_rating_score`)
  — see the "v2 scoring" test section below, including its two AI/geocoding-
  backed categories (`product_fit_score` via a cached Claude judgment,
  `proximity_score` via real geocoded aerial distance) and its two
  deliberate exceptions to the general avg-fallback rule (a blank remark
  and an unknown designation both score `0`, not the average). A free
  rescore always stays pinned to the version the card was originally
  scored under (see the "versioning immutability guarantees" section).
- Scoring is never auto-triggered — the sole triggers are the explicit
  `POST /cards/{card_id}/score` (single) and `POST /cards/score` (bulk, a
  best-effort skip-and-count over a caller-picked selection) endpoints.
- Scoring is one-shot per card: once `lead_score` is set, re-scoring is
  rejected, not allowed. `POST /cards/{card_id}/score` on an already-scored
  card returns 409 (`CardAlreadyScoredError`); the bulk endpoint silently
  skips it. `score_card_task` itself also re-checks this on every attempt,
  as defense-in-depth against two enqueues racing past the service-layer
  check.
- A card with no linked company (`company_id is None`) must still score
  successfully, with every enrichment-dependent v2 category landing on its
  average value.

Mocking strategy: `score_card_task` makes no outbound calls under `v1` (kept
frozen), so v1-focused tests call it directly as a plain function (bypassing
`.delay()`), exactly as `test_07_data_enrichment.py` calls
`enrich_company_task` directly. Under `v2`, the worker conditionally calls
`product_fit_service.get_or_judge_fit`/`geocode_service.get_or_geocode`
(gated to `v2` only, per `_VERSIONS_NEEDING_PRODUCT_FIT_AND_GEOCODING` —
see the dedicated cost-avoidance regression test) — v2-focused integration
tests either leave the seller's `product_lines`/`industry`/`billing_address`
blank (so the worker's own guards skip the calls) or monkeypatch those two
service functions directly. `_FakeAnthropicClient`/`_FakeAnthropicMessages`/
`_FakeTextBlock` stub Claude responses for `product_fit_service`/
`news_summary_provider` at the unit level. Endpoint tests patch
`app.services.card_service.score_card_task.delay` (mirroring
`test_07_data_enrichment.py`'s `_patch_enrich_delay`) to assert enqueue/skip
behavior without a real Celery worker. Vision extraction is mocked via
`app.services.vision_client.extract_card_fields`, matching every other test
file's convention.

Judgment calls made in the absence of explicit spec text:
  1. **Distinct company names per test** — `companies` is not truncated by
     `conftest.py`'s autouse `_clean_tables` fixture, so every test that
     creates a `Company` (directly or via extraction linking one) uses a
     name containing a fresh `uuid.uuid4()` fragment.
  2. **Exact score numbers in the "fully populated" case are computed to
     hit each component's max** (or a clearly-over-threshold value) so the
     test is a precise regression against `scoring.py`'s documented weights,
     not a loose "score went up" check.
"""

from __future__ import annotations

import io
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app.main import app as fastapi_app
from app.models.company import Company
from app.models.company_signals import CompanySignals
from app.models.seller_profile import SellerProfile
from app.models.visiting_card import VisitingCard
from app.services import enrichment_service, geocode_service, product_fit_service, scoring
from app.services.enrichment_providers import news_summary_provider, share_price_provider
from app.services.enrichment_providers.firmographics_provider import FirmographicsResult
from app.services.enrichment_providers.news_signal_provider import _classify_signal_type
from app.workers.card_processing import process_card
from app.workers.enrichment_processing import enrich_company_task
from app.workers.scoring_processing import score_card_task
from conftest import create_verified_user


def _unique_company_name(label: str) -> str:
    """`companies` is not truncated between tests — every row created here
    must carry a name no other test could ever also create."""
    return f"{label} {uuid.uuid4().hex[:10]} Pvt Ltd"


# --------------------------------------------------------------------------
# Vision-model mocking — matches test_05/test_07's established convention.
# --------------------------------------------------------------------------


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


def _patch_score_delay(monkeypatch: pytest.MonkeyPatch) -> list[tuple[tuple, dict]]:
    captured: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        "app.services.card_service.score_card_task.delay",
        lambda *args, **kwargs: captured.append((args, kwargs)),
    )
    return captured


def _force_scoring_version(monkeypatch: pytest.MonkeyPatch, version: str) -> None:
    """Pins a *fresh* score to a specific version, bypassing the rollout
    split — used by tests asserting an exact breakdown shape for a given
    version, which would otherwise be at the mercy of _SCORING_VERSION_ROLLOUT.
    Never affects a rescore, which pins to the card's own stored version
    regardless of this patch (see scoring_processing.py)."""
    monkeypatch.setattr("app.services.scoring.select_scoring_version", lambda user_id: version)


class _StaticFirmographicsProvider:
    """Returns a fixed, fully-populated LinkedIn result regardless of input
    — used to simulate the "enrichment ran and found company size" leg of
    the re-scoring test without touching any other provider."""

    def __init__(self, result: FirmographicsResult) -> None:
        self._result = result

    def lookup_linkedin(self, company_name: str, website: str | None) -> FirmographicsResult:
        return self._result


class _FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeAnthropicResponse:
    def __init__(self, text: str) -> None:
        self.content = [_FakeTextBlock(text)]


class _FakeAnthropicMessages:
    def __init__(self, text: str) -> None:
        self._text = text

    def create(self, **kwargs):
        return _FakeAnthropicResponse(self._text)


class _FakeAnthropicClient:
    def __init__(self, text: str) -> None:
        self.messages = _FakeAnthropicMessages(text)


def _patch_summary_client(monkeypatch: pytest.MonkeyPatch, text: str = "Fixed test summary text.") -> None:
    monkeypatch.setattr(
        "app.services.enrichment_summary._get_client", lambda: _FakeAnthropicClient(text)
    )


# ==========================================================================
# 1. score_card_task against a card with no company, no designation, no remark.
# ==========================================================================


def test_score_card_with_no_company_or_signals_lands_every_component_at_zero(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """DoD: 'A card scored with no linked company (company_id is None) still
    scores successfully, with company_size_score, industry_fit_score, and
    momentum_signal_score all 0.' A card with no job_title/special_remark
    must also land designation_score and remark_signal_score at 0."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(
        monkeypatch,
        _fields(full_name="No Company Contact", job_title=None, company_name=None),
    )
    process_card(card_id)

    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert card.status == "extracted", "fixture setup: this card must reach 'extracted'"
    assert card.company_id is None, "fixture setup: no company_name means no linked company"

    _force_scoring_version(monkeypatch, "v1")
    score_card_task(card_id)  # bare call: no retry path involved

    db_session.expire_all()
    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert card.score_breakdown == {
        "designation_score": 0,
        "company_size_score": 0,
        "industry_fit_score": 0,
        "momentum_signal_score": 0,
        "remark_signal_score": 0,
        "total": 0,
        "version": "v1",
    }
    assert card.lead_score == 0
    assert card.scored_at is not None


# ==========================================================================
# 2. score_card_task against a fully-populated card + company + signals + seller
#    profile — every component near/at its max.
# ==========================================================================


def test_score_card_fully_populated_case_hits_each_components_max(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """Regression against scoring.py's documented weights: c_level designation
    (30), 600 LinkedIn employees (25), 4+ overlapping industry keywords (25),
    all four momentum signals present (10), a matching intent keyword in the
    remark (10) -> total 100."""
    user = _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    company_name = _unique_company_name("Industrial Pumps Valves Fittings")
    _patch_vision(
        monkeypatch,
        _fields(
            full_name="Senior Contact",
            job_title="Chief Executive Officer",
            company_name=company_name,
            products_offered="industrial pumps and valves",
            special_remark="Very interested, please follow up soon",
        ),
    )
    process_card(card_id)

    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert card.designation_level == "c_level", "fixture setup: job_title must classify as c_level"
    assert card.company_id is not None, "fixture setup: company_name must link a Company"

    signals = CompanySignals(
        company_id=card.company_id,
        linkedin_employee_count=600,
        hiring_signal="expanding",
        gem_tender_count=1,
        import_export_activity=True,
        marketplace_verified_badge=True,
        product_lines_summary="Industrial fittings and pumps manufacturer",
    )
    db_session.add(signals)
    db_session.add(
        SellerProfile(
            user_id=uuid.UUID(user["user_id"]),
            industry="industrial machinery",
            product_lines="pumps valves fittings",
        )
    )
    db_session.commit()

    _force_scoring_version(monkeypatch, "v1")
    score_card_task(card_id)

    db_session.expire_all()
    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    breakdown = card.score_breakdown
    assert breakdown["designation_score"] == 30
    assert breakdown["company_size_score"] == 25
    assert breakdown["industry_fit_score"] == 25
    assert breakdown["momentum_signal_score"] == 10
    assert breakdown["remark_signal_score"] == 10
    assert breakdown["total"] == 100
    assert breakdown["version"] == "v1"
    assert card.lead_score == 100
    assert card.scored_at is not None

    # Round-trips through GET /cards/{card_id} with the same values.
    resp = client.get(f"/cards/{card_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["lead_score"] == 100
    assert body["score_breakdown"] == breakdown
    assert body["scored_at"] is not None


# ==========================================================================
# 3. POST /cards/{card_id}/score — eligibility (409 for non-extracted).
# ==========================================================================


def test_score_endpoint_on_non_extracted_card_returns_409_and_never_enqueues(
    client, fake_otp_provider, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)  # left at status="new" — never processed
    captured = _patch_score_delay(monkeypatch)

    resp = client.post(f"/cards/{card_id}/score")

    assert resp.status_code == 409, resp.text
    assert captured == [], "a non-extracted card must never enqueue score_card_task"


# ==========================================================================
# 4. POST /cards/{card_id}/score — tenant isolation (404 for another org).
# ==========================================================================


def test_score_endpoint_for_another_users_card_returns_404(
    client, fake_otp_provider, jpeg_bytes, monkeypatch
):
    """Tenant isolation: reuses the existing org-scoped get_visible_card — a
    user must never be able to trigger (or even discover) scoring for
    another org's card."""
    _authenticated_user(client, fake_otp_provider)

    with TestClient(fastapi_app) as other_client:
        _authenticated_user(other_client, fake_otp_provider)
        their_card_id = _upload_one(other_client, jpeg_bytes)
        _patch_vision(
            monkeypatch,
            _fields(full_name="Owner Contact", company_name=_unique_company_name("Owner Only Co")),
        )
        process_card(their_card_id)
        captured = _patch_score_delay(monkeypatch)

        resp = client.post(f"/cards/{their_card_id}/score")

    assert resp.status_code == 404, (
        f"a user in a different org must never be able to trigger scoring for another org's "
        f"card, got {resp.status_code}: {resp.text}"
    )
    assert captured == [], "another org's card must never be enqueued for scoring"


# ==========================================================================
# 5. POST /cards/score — bulk skip-counting over a mixed selection.
# ==========================================================================


def test_bulk_score_endpoint_skips_ineligible_and_foreign_cards(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    eligible_card_id = _upload_one(client, jpeg_bytes, filename="eligible.jpg")
    _patch_vision(
        monkeypatch,
        _fields(full_name="Eligible Contact", company_name=_unique_company_name("Eligible Co")),
    )
    process_card(eligible_card_id)
    not_extracted_card_id = _upload_one(client, jpeg_bytes, filename="not-extracted.jpg")

    with TestClient(fastapi_app) as other_client:
        _authenticated_user(other_client, fake_otp_provider)
        foreign_card_id = _upload_one(other_client, jpeg_bytes, filename="foreign.jpg")

        captured = _patch_score_delay(monkeypatch)
        resp = client.post(
            "/cards/score",
            json={"card_ids": [eligible_card_id, not_extracted_card_id, foreign_card_id]},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enqueued_count"] == 1
    assert body["skipped_count"] == 2
    assert len(captured) == 1
    assert captured[0][0] == (eligible_card_id,)


# ==========================================================================
# 6. Scoring is one-shot: an already-scored card cannot be re-scored, even
#    via the task directly, the single endpoint, or the bulk endpoint.
# ==========================================================================


def test_score_card_task_is_a_noop_against_an_already_scored_card(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """Direct-task-call analog of the one-shot rule: even bypassing the
    service layer entirely, a second score_card_task run against an
    already-scored card must not overwrite lead_score/score_breakdown/
    scored_at — defense-in-depth against two enqueues racing past the
    card_service eligibility check."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    company_name = _unique_company_name("Already Scored Co")
    _patch_vision(monkeypatch, _fields(full_name="Already Scored Contact", company_name=company_name))
    process_card(card_id)

    score_card_task(card_id)
    db_session.expire_all()
    first = db_session.get(VisitingCard, uuid.UUID(card_id))
    first_score, first_scored_at = first.lead_score, first.scored_at
    assert first_scored_at is not None

    # Enrichment landing new CompanySignals afterward must not matter — the
    # card is locked at its first score regardless of what data appears later.
    _patch_summary_client(monkeypatch)
    monkeypatch.setattr(
        "app.services.enrichment_providers.firmographics_provider.get_firmographics_provider",
        lambda: _StaticFirmographicsProvider(
            FirmographicsResult(linkedin_employee_count=600, linkedin_follower_count=5000)
        ),
    )
    enrich_company_task(str(first.company_id))

    score_card_task(card_id)  # second call against an already-scored card

    db_session.expire_all()
    second = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert second.lead_score == first_score
    assert second.scored_at == first_scored_at


def test_score_endpoint_on_already_scored_card_returns_409_and_never_reenqueues(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """A card that's already been scored is locked — POST /score on it again
    must be rejected (409, CardAlreadyScoredError) rather than silently
    re-scoring, and must never enqueue a second task."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(
        monkeypatch,
        _fields(full_name="Locked Contact", company_name=_unique_company_name("Locked Co")),
    )
    process_card(card_id)
    score_card_task(card_id)
    db_session.expire_all()
    before = db_session.get(VisitingCard, uuid.UUID(card_id))
    before_score, before_scored_at = before.lead_score, before.scored_at

    captured = _patch_score_delay(monkeypatch)
    resp = client.post(f"/cards/{card_id}/score")

    assert resp.status_code == 409, resp.text
    assert captured == [], "an already-scored card must never enqueue a second score_card_task"

    db_session.expire_all()
    after = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert after.lead_score == before_score
    assert after.scored_at == before_scored_at


def test_bulk_score_endpoint_skips_already_scored_cards(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    already_scored_id = _upload_one(client, jpeg_bytes, filename="already-scored.jpg")
    _patch_vision(
        monkeypatch,
        _fields(full_name="Already Scored", company_name=_unique_company_name("Already Scored Co")),
    )
    process_card(already_scored_id)
    score_card_task(already_scored_id)

    eligible_card_id = _upload_one(client, jpeg_bytes, filename="fresh-eligible.jpg")
    _patch_vision(
        monkeypatch,
        _fields(full_name="Fresh Eligible", company_name=_unique_company_name("Fresh Eligible Co")),
    )
    process_card(eligible_card_id)

    captured = _patch_score_delay(monkeypatch)
    resp = client.post(
        "/cards/score",
        json={"card_ids": [already_scored_id, eligible_card_id]},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enqueued_count"] == 1
    assert body["skipped_count"] == 1
    assert len(captured) == 1
    assert captured[0][0] == (eligible_card_id,)


# ==========================================================================
# 7. CardOut/CardDetailOut expose lead_score/score_breakdown/scored_at.
# ==========================================================================


def test_list_and_detail_endpoints_expose_scoring_fields(
    client, fake_otp_provider, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(
        monkeypatch,
        _fields(full_name="List Detail Contact", company_name=_unique_company_name("List Detail Co")),
    )
    process_card(card_id)

    list_resp = client.get("/cards")
    assert list_resp.status_code == 200, list_resp.text
    listed = next(c for c in list_resp.json() if c["card_id"] == card_id)
    assert listed["lead_score"] is None
    assert listed["score_breakdown"] is None
    assert listed["scored_at"] is None

    detail_resp = client.get(f"/cards/{card_id}")
    assert detail_resp.status_code == 200, detail_resp.text
    assert detail_resp.json()["lead_score"] is None

    score_card_task(card_id)

    list_resp = client.get("/cards")
    listed = next(c for c in list_resp.json() if c["card_id"] == card_id)
    assert listed["lead_score"] is not None
    assert listed["score_breakdown"] is not None
    assert listed["scored_at"] is not None


# ==========================================================================
# 8. lead_score is a JSON number, not a string (Decimal-serialization trap).
# ==========================================================================


def test_lead_score_serializes_as_a_json_number_not_a_string(
    client, fake_otp_provider, jpeg_bytes, monkeypatch
):
    """VisitingCard.lead_score is Decimal at the ORM layer; Pydantic v2
    serializes Decimal fields to JSON strings by default. The schema field
    must be declared float, not Decimal, so the wire type stays a number —
    this test is a direct regression against that trap."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(
        monkeypatch,
        _fields(full_name="Number Type Contact", company_name=_unique_company_name("Number Type Co")),
    )
    process_card(card_id)
    score_card_task(card_id)

    resp = client.get(f"/cards/{card_id}")
    assert resp.status_code == 200, resp.text
    lead_score = resp.json()["lead_score"]
    assert isinstance(lead_score, (int, float)) and not isinstance(lead_score, str), (
        f"lead_score must serialize as a JSON number, got {type(lead_score)}: {lead_score!r}"
    )


# ==========================================================================
# 9. scoring_processing is registered with the Celery app.
# ==========================================================================


def test_scoring_processing_module_is_registered_with_celery_app():
    """Cheap structural regression against forgetting the celery_app.py
    include-list edit — without it, score_card_task.delay() would enqueue a task
    no worker process ever imports/registers."""
    from app.workers.celery_app import celery_app

    assert "app.workers.scoring_processing" in celery_app.conf.include
    assert score_card_task.name == "app.workers.scoring_processing.score_card_task"


# ==========================================================================
# 10. v2 scoring — pure category unit tests. calculate_score() is called
#     directly with version="v2" against hand-built, never-persisted ORM
#     objects — no DB/client fixtures needed, mirroring the pure-unit-test
#     convention used elsewhere for scoring.py's helpers.
# ==========================================================================


def _card(**overrides) -> VisitingCard:
    defaults = dict(
        card_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        designation_level=None,
        special_remark=None,
        products_offered=None,
        job_title=None,
        address=None,
    )
    defaults.update(overrides)
    return VisitingCard(**defaults)


def _company(**overrides) -> Company:
    defaults = dict(
        company_id=uuid.uuid4(),
        name=_unique_company_name("Pure Unit Co"),
        hq_city=None,
    )
    defaults.update(overrides)
    return Company(**defaults)


def _signals(company_id: uuid.UUID, **overrides) -> CompanySignals:
    return CompanySignals(company_id=company_id, **overrides)


def _seller(**overrides) -> SellerProfile:
    defaults = dict(
        user_id=uuid.uuid4(),
        industry=None,
        product_lines=None,
        billing_address=None,
        target_regions=None,
    )
    defaults.update(overrides)
    return SellerProfile(**defaults)


def _v2(
    card, company, signals, seller, existing_phones=None,
    product_fit_verdict=None, distance_km=None,
) -> dict:
    return scoring.calculate_score(
        card, company, signals, seller, existing_phones or [],
        product_fit_verdict, distance_km, version="v2",
    )


def test_v2_remark_tier1_positive_intent_keyword():
    card = _card(special_remark="Very interested, please share a quote urgently")
    breakdown = _v2(card, None, None, _seller())
    assert breakdown["remark_signal_score"] == 24


def test_v2_remark_tier1_product_keyword_overlap_without_intent_keyword():
    card = _card(special_remark="Please quote for pumps")
    seller = _seller(product_lines="pumps valves")
    breakdown = _v2(card, None, None, seller)
    assert breakdown["remark_signal_score"] == 24


def test_v2_remark_tier2_new_phone_number_not_on_file():
    card = _card(special_remark="Alternate contact 9876543210")
    breakdown = _v2(card, None, None, _seller(), existing_phones=[])
    assert breakdown["remark_signal_score"] == 16


def test_v2_remark_phone_already_on_file_does_not_trigger_tier2():
    card = _card(special_remark="Alternate contact 9876543210")
    breakdown = _v2(card, None, None, _seller(), existing_phones=["+919876543210"])
    assert breakdown["remark_signal_score"] == 8  # falls to tier 3, not tier 2


def test_v2_remark_tier3_present_but_irrelevant():
    card = _card(special_remark="Nice office, good location")
    breakdown = _v2(card, None, None, _seller())
    assert breakdown["remark_signal_score"] == 8


def test_v2_remark_blank_or_null_scores_zero_not_avg():
    """A rep only writes a note when a lead seems worth flagging, so a
    blank note is mild negative evidence, not missing data — deliberate
    exception to the general avg-fallback rule."""
    for remark in (None, "", "   "):
        card = _card(special_remark=remark)
        breakdown = _v2(card, None, None, _seller())
        assert breakdown["remark_signal_score"] == 0


def test_v2_remark_buyer_only_product_mention_does_not_reach_tier1():
    """The seller-only reference-set change: a remark matching the buyer's
    enrichment product text, with no overlap against the seller's own
    product_lines, must not reach tier 1 on that basis."""
    card = _card(special_remark="They mainly deal in cement and steel")
    signals = _signals(uuid.uuid4(), product_lines_summary="cement steel")
    breakdown = _v2(card, None, signals, _seller(product_lines="pumps valves"))
    assert breakdown["remark_signal_score"] == 8  # falls to tier 3


def test_v2_product_fit_score_reads_verdict_directly():
    assert _v2(_card(), None, None, _seller(), product_fit_verdict="needs")[
        "product_fit_score"
    ] == 20
    assert _v2(_card(), None, None, _seller(), product_fit_verdict="partial")[
        "product_fit_score"
    ] == 12
    assert _v2(_card(), None, None, _seller(), product_fit_verdict="no_need")[
        "product_fit_score"
    ] == 0
    assert _v2(_card(), None, None, _seller(), product_fit_verdict=None)[
        "product_fit_score"
    ] == 10


def test_v2_role_designation_purchase_keyword_beats_designation_fallback():
    card = _card(job_title="Purchase Head", designation_level="individual_contributor")
    breakdown = _v2(card, None, None, _seller())
    assert breakdown["role_designation_score"] == 16


@pytest.mark.parametrize(
    "designation_level,expected",
    [("c_level", 13), ("director", 10), ("manager", 6), ("individual_contributor", 0)],
)
def test_v2_role_designation_seniority_fallback_ordering(designation_level, expected):
    card = _card(job_title=None, designation_level=designation_level)
    breakdown = _v2(card, None, None, _seller())
    assert breakdown["role_designation_score"] == expected


def test_v2_role_designation_unknown_scores_zero_not_avg():
    card = _card(job_title=None, designation_level=None)
    breakdown = _v2(card, None, None, _seller())
    assert breakdown["role_designation_score"] == 0


def test_v2_proximity_score_reflects_distance_km_tiers():
    assert _v2(_card(), None, None, _seller(), distance_km=10)["proximity_score"] == 12
    assert _v2(_card(), None, None, _seller(), distance_km=49)["proximity_score"] == 12
    assert _v2(_card(), None, None, _seller(), distance_km=51)["proximity_score"] == 9
    assert _v2(_card(), None, None, _seller(), distance_km=199)["proximity_score"] == 9
    assert _v2(_card(), None, None, _seller(), distance_km=499)["proximity_score"] == 5
    assert _v2(_card(), None, None, _seller(), distance_km=501)["proximity_score"] == 1
    assert _v2(_card(), None, None, _seller(), distance_km=None)["proximity_score"] == 6


def test_v2_expansion_score_reads_tag_and_distress_override():
    """expansion_signal_score reads Claude's own news_tags classification
    directly — it never re-derives it via a second keyword scan."""
    company = _company()
    has_expansion = _v2(
        _card(), company,
        _signals(
            company.company_id, news_summary="Company announces a new plant",
            news_tags=["expansion"], news_distress_detected=False,
        ),
        _seller(),
    )["expansion_signal_score"]
    other_only = _v2(
        _card(), company,
        _signals(
            company.company_id, news_summary="Company hosts a community event",
            news_tags=["funding"], news_distress_detected=False,
        ),
        _seller(),
    )["expansion_signal_score"]
    avg = _v2(
        _card(), company, _signals(company.company_id, news_summary=None, news_distress_detected=False), _seller()
    )["expansion_signal_score"]
    overridden = _v2(
        _card(), company,
        _signals(
            company.company_id, news_summary="Company announces a new plant",
            news_tags=["expansion"], news_distress_detected=True,
        ),
        _seller(),
    )["expansion_signal_score"]

    assert has_expansion == 10
    assert other_only == 3
    assert avg == 5
    assert overridden == 0, "distress override must win even when the expansion tag is present"


def test_v2_revenue_growth_score_reads_tag_and_distress_override():
    """revenue_growth_score reads Claude's own news_tags classification
    directly — it never re-derives it via a second keyword scan."""
    company = _company()
    has_growth = _v2(
        _card(), company,
        _signals(
            company.company_id, news_summary="Company posts record revenue this quarter",
            news_tags=["revenue_growth"], news_distress_detected=False,
        ),
        _seller(),
    )["revenue_growth_score"]
    other_only = _v2(
        _card(), company,
        _signals(
            company.company_id, news_summary="Company hosts a community event",
            news_tags=["funding"], news_distress_detected=False,
        ),
        _seller(),
    )["revenue_growth_score"]
    avg = _v2(
        _card(), company, _signals(company.company_id, news_summary=None, news_distress_detected=False), _seller()
    )["revenue_growth_score"]
    overridden = _v2(
        _card(), company,
        _signals(
            company.company_id, news_summary="Company posts record revenue this quarter",
            news_tags=["revenue_growth"], news_distress_detected=True,
        ),
        _seller(),
    )["revenue_growth_score"]

    assert has_growth == 8
    assert other_only == 2
    assert avg == 4
    assert overridden == 0, "distress override must win even when the revenue-growth tag is present"


def test_v2_distress_override_scoped_to_exactly_expansion_and_revenue_growth():
    """Direct DoD check: news_distress_detected must never affect any other
    category on the same card."""
    company = _company(name=_unique_company_name("Industrial Pumps Valves Fittings"))
    seller = _seller(industry="industrial machinery", product_lines="pumps valves")
    card = _card(
        special_remark="Very interested, please quote",
        job_title="Purchase Manager",
    )
    common_kwargs = dict(
        product_fit_verdict="needs", distance_km=25,
    )
    not_distressed = _signals(
        company.company_id,
        news_summary="Company announces a new plant",
        news_tags=["expansion"],
        news_distress_detected=False,
        indiamart_employee_count_band="100-500",
        indiamart_annual_turnover_band="25-100 crore",
        indiamart_rating=Decimal("5"),
    )
    distressed = _signals(
        company.company_id,
        news_summary="Company announces a new plant",
        news_tags=["expansion"],
        news_distress_detected=True,
        indiamart_employee_count_band="100-500",
        indiamart_annual_turnover_band="25-100 crore",
        indiamart_rating=Decimal("5"),
    )

    breakdown_a = _v2(card, company, not_distressed, seller, **common_kwargs)
    breakdown_b = _v2(card, company, distressed, seller, **common_kwargs)

    assert breakdown_a["expansion_signal_score"] == 10
    assert breakdown_b["expansion_signal_score"] == 0
    for key in (
        "remark_signal_score", "product_fit_score", "role_designation_score",
        "proximity_score", "company_size_score", "marketplace_rating_score",
    ):
        assert breakdown_a[key] == breakdown_b[key], f"{key} must be unaffected by news_distress_detected"


def test_v2_company_size_combines_bands_and_caps_bonuses():
    company = _company()
    signals = _signals(
        company.company_id,
        indiamart_employee_count_band="100-500",
        indiamart_annual_turnover_band="25-100 crore",
        import_export_activity=True,
        gem_tender_count=3,
    )
    breakdown = _v2(_card(), company, signals, _seller())
    assert breakdown["company_size_score"] == 6  # base 4 + 2 bonuses, capped at 6


def test_v2_company_size_avg_when_no_signals_row():
    breakdown = _v2(_card(), _company(), None, _seller())
    assert breakdown["company_size_score"] == 3


def test_v2_marketplace_rating_scales_and_ignores_google_rating():
    company = _company()
    full_rating = _v2(
        _card(),
        company,
        _signals(company.company_id, indiamart_rating=Decimal("5"), google_rating=Decimal("1")),
        _seller(),
    )["marketplace_rating_score"]
    avg = _v2(
        _card(),
        company,
        _signals(company.company_id, indiamart_rating=None, google_rating=Decimal("4.5")),
        _seller(),
    )["marketplace_rating_score"]

    assert full_rating == 4
    assert avg == 2


def test_v2_avg_fallbacks_and_the_two_explicit_zero_exceptions():
    """Consolidated DoD check: with every signal genuinely unavailable,
    product_fit/proximity/expansion/revenue_growth/company_size/
    marketplace_rating each land exactly on max // 2, while remark
    (blank) and role/designation (unknown) both score 0 — the two
    deliberate exceptions to the avg-fallback rule."""
    breakdown = _v2(_card(), None, None, _seller())
    assert breakdown == {
        "remark_signal_score": 0,
        "product_fit_score": 10,
        "role_designation_score": 0,
        "proximity_score": 6,
        "expansion_signal_score": 5,
        "revenue_growth_score": 4,
        "company_size_score": 3,
        "marketplace_rating_score": 2,
        "total": 30,
        "version": "v2",
    }


# ==========================================================================
# 11. v2 scoring — full-task integration tests, through score_card_task +
#     DB + GET, mirroring the old v1 "fully populated"/"no company" tests.
# ==========================================================================


def test_v2_fully_populated_card_hits_every_components_max(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    user = _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    company_name = _unique_company_name("Industrial Pumps Valves")
    _patch_vision(
        monkeypatch,
        _fields(
            full_name="V2 Max Contact",
            job_title="Purchase Manager",
            company_name=company_name,
            address="Sector 44, Gurugram, Haryana",
            products_offered="industrial pumps",
            special_remark="Very interested, please quote",
        ),
    )
    process_card(card_id)

    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert card.company_id is not None, "fixture setup: company_name must link a Company"

    db_session.add(
        CompanySignals(
            company_id=card.company_id,
            indiamart_business_type="Manufacturer",
            indiamart_employee_count_band="100-500",
            indiamart_annual_turnover_band="25-100 crore",
            import_export_activity=True,
            gem_tender_count=3,
            indiamart_rating=Decimal("5"),
            news_summary=(
                "Company announces a new plant and posts record revenue this quarter."
            ),
            news_tags=["expansion", "revenue_growth"],
            news_distress_detected=False,
        )
    )
    db_session.add(
        SellerProfile(
            user_id=uuid.UUID(user["user_id"]),
            industry="industrial machinery",
            product_lines="pumps valves",
            billing_address="Gurugram, Haryana",
        )
    )
    db_session.commit()

    monkeypatch.setattr(
        "app.workers.scoring_processing.product_fit_service.get_or_judge_fit",
        lambda *args, **kwargs: "needs",
    )
    monkeypatch.setattr(
        "app.workers.scoring_processing.geocode_service.get_or_geocode",
        lambda db, address: (12.9716, 77.5946) if address else None,
    )

    _force_scoring_version(monkeypatch, "v2")
    score_card_task(card_id)

    db_session.expire_all()
    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    breakdown = card.score_breakdown
    assert set(breakdown.keys()) == {
        "remark_signal_score",
        "product_fit_score",
        "role_designation_score",
        "proximity_score",
        "expansion_signal_score",
        "revenue_growth_score",
        "company_size_score",
        "marketplace_rating_score",
        "total",
        "version",
    }
    assert breakdown["remark_signal_score"] == 24
    assert breakdown["product_fit_score"] == 20
    assert breakdown["role_designation_score"] == 16
    assert breakdown["proximity_score"] == 12
    assert breakdown["expansion_signal_score"] == 10
    assert breakdown["revenue_growth_score"] == 8
    assert breakdown["company_size_score"] == 6
    assert breakdown["marketplace_rating_score"] == 4
    assert breakdown["total"] == 100
    assert breakdown["version"] == "v2"
    assert card.lead_score == 100

    resp = client.get(f"/cards/{card_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["lead_score"] == 100
    assert body["score_breakdown"] == breakdown


def test_v2_no_company_card_uses_avg_for_enrichment_dependent_categories(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(
        monkeypatch,
        _fields(
            full_name="No Company V2 Contact",
            job_title="Purchase Manager",
            company_name=None,
            special_remark="Very interested, please quote",
        ),
    )
    process_card(card_id)

    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert card.company_id is None, "fixture setup: no company_name means no linked company"

    _force_scoring_version(monkeypatch, "v2")
    score_card_task(card_id)

    db_session.expire_all()
    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    breakdown = card.score_breakdown
    assert breakdown["remark_signal_score"] == 24  # real — card-native field
    assert breakdown["role_designation_score"] == 16  # real — card-native field
    assert breakdown["product_fit_score"] == 10
    assert breakdown["proximity_score"] == 6
    assert breakdown["expansion_signal_score"] == 5
    assert breakdown["revenue_growth_score"] == 4
    assert breakdown["company_size_score"] == 3
    assert breakdown["marketplace_rating_score"] == 2
    assert breakdown["total"] == 70


def test_v1_scored_card_is_unaffected_by_v2_shipping(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """Coexistence guarantee: a card scored under v1 keeps its 5-key
    breakdown forever, even as new cards score under v2 by default."""
    _authenticated_user(client, fake_otp_provider)
    v1_card_id = _upload_one(client, jpeg_bytes, filename="v1-card.jpg")
    _patch_vision(
        monkeypatch,
        _fields(full_name="V1 Legacy Contact", company_name=_unique_company_name("V1 Legacy Co")),
    )
    process_card(v1_card_id)
    monkeypatch.setattr("app.services.scoring._SCORING_VERSION_ROLLOUT", {"v1": 100})
    score_card_task(v1_card_id)

    db_session.expire_all()
    v1_card = db_session.get(VisitingCard, uuid.UUID(v1_card_id))
    assert set(v1_card.score_breakdown.keys()) == {
        "designation_score",
        "company_size_score",
        "industry_fit_score",
        "momentum_signal_score",
        "remark_signal_score",
        "total",
        "version",
    }
    assert v1_card.score_breakdown["version"] == "v1"

    # A brand-new card, scored with the rollout restored to v2, lands on
    # the v2 shape.
    monkeypatch.setattr("app.services.scoring._SCORING_VERSION_ROLLOUT", {"v2": 100})
    v2_card_id = _upload_one(client, jpeg_bytes, filename="v2-card.jpg")
    _patch_vision(
        monkeypatch,
        _fields(full_name="V2 Fresh Contact", company_name=_unique_company_name("V2 Fresh Co")),
    )
    process_card(v2_card_id)
    score_card_task(v2_card_id)

    db_session.expire_all()
    v2_card = db_session.get(VisitingCard, uuid.UUID(v2_card_id))
    assert v2_card.score_breakdown["version"] == "v2"

    # Re-fetching the v1 card confirms it was never touched by v2 shipping.
    db_session.expire_all()
    v1_card_again = db_session.get(VisitingCard, uuid.UUID(v1_card_id))
    assert v1_card_again.score_breakdown == v1_card.score_breakdown


def test_v1_never_triggers_product_fit_or_geocoding_calls(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """Cost-avoidance regression: _VERSIONS_NEEDING_PRODUCT_FIT_AND_GEOCODING
    must keep v1 from paying for the Claude/geocoding I/O it never reads.
    Uses a card+seller profile with non-blank product_lines/billing_address
    (unlike the other v1 tests, which happen to leave these blank and so
    wouldn't catch this guard being silently removed)."""
    user = _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(
        monkeypatch,
        _fields(
            full_name="V1 Cost Avoidance Contact",
            company_name=_unique_company_name("V1 Cost Avoidance Co"),
            address="Some City",
        ),
    )
    process_card(card_id)

    db_session.add(
        SellerProfile(
            user_id=uuid.UUID(user["user_id"]),
            industry="industrial machinery",
            product_lines="pumps valves",
            billing_address="Some Other City",
        )
    )
    db_session.commit()

    def _raise_product_fit(*args, **kwargs):
        raise AssertionError("product_fit_service.get_or_judge_fit must not be called for v1")

    def _raise_geocode(*args, **kwargs):
        raise AssertionError("geocode_service.get_or_geocode must not be called for v1")

    monkeypatch.setattr(
        "app.workers.scoring_processing.product_fit_service.get_or_judge_fit", _raise_product_fit
    )
    monkeypatch.setattr(
        "app.workers.scoring_processing.geocode_service.get_or_geocode", _raise_geocode
    )

    _force_scoring_version(monkeypatch, "v1")
    score_card_task(card_id)  # must not raise — confirms neither service was called

    db_session.expire_all()
    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert card.score_breakdown["version"] == "v1"


# ==========================================================================
# 12. news_signal_provider — original 3 buckets, unaffected by this
#     rework's revert of the now-dead share_price_growth/revenue_growth
#     buckets (superseded by news_summary_provider.py for v2 scoring).
# ==========================================================================


@pytest.mark.parametrize(
    "headline,expected",
    [
        ("Company raises Series B funding round", "funding"),
        ("Company announces plant expansion", "expansion"),
        ("Company inaugurates new facility", "new_facility"),
    ],
)
def test_news_pre_existing_buckets_still_classify_correctly(headline, expected):
    assert _classify_signal_type(headline) == expected


# ==========================================================================
# 13. select_scoring_version — deterministic bucketing & rollout distribution.
# ==========================================================================


def test_select_scoring_version_is_deterministic_for_a_given_user():
    user_id = uuid.uuid4()
    first = scoring.select_scoring_version(user_id)
    for _ in range(10):
        assert scoring.select_scoring_version(user_id) == first


def test_select_scoring_version_distribution_roughly_matches_rollout(monkeypatch):
    monkeypatch.setattr("app.services.scoring._SCORING_VERSION_ROLLOUT", {"v1": 30, "v2": 70})
    sample_size = 2000
    counts = {"v1": 0, "v2": 0}
    for _ in range(sample_size):
        counts[scoring.select_scoring_version(uuid.uuid4())] += 1
    v1_pct = counts["v1"] / sample_size * 100
    assert 20 <= v1_pct <= 40, f"expected roughly 30% v1, got {v1_pct}%"


# ==========================================================================
# 14. Versioning immutability guarantees — previously-scored cards are
#     never affected by later rollout-config changes or new version rollouts.
# ==========================================================================


def test_rollout_config_change_never_alters_an_already_scored_card(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(
        monkeypatch,
        _fields(full_name="Immutable Contact", company_name=_unique_company_name("Immutable Co")),
    )
    process_card(card_id)
    _force_scoring_version(monkeypatch, "v2")
    score_card_task(card_id)

    db_session.expire_all()
    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    lead_score, score_breakdown, scored_at = card.lead_score, dict(card.score_breakdown), card.scored_at

    monkeypatch.setattr("app.services.scoring._SCORING_VERSION_ROLLOUT", {"v1": 100})

    resp = client.get(f"/cards/{card_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["lead_score"] == float(lead_score)
    assert body["score_breakdown"] == score_breakdown
    assert body["scored_at"] is not None and scored_at is not None


def test_new_registry_entry_never_alters_existing_scored_cards(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    _authenticated_user(client, fake_otp_provider)
    old_card_id = _upload_one(client, jpeg_bytes, filename="old.jpg")
    _patch_vision(
        monkeypatch, _fields(full_name="Old Contact", company_name=_unique_company_name("Old Co"))
    )
    process_card(old_card_id)
    monkeypatch.setattr("app.services.scoring._SCORING_VERSION_ROLLOUT", {"v2": 100})
    score_card_task(old_card_id)

    db_session.expire_all()
    old_card = db_session.get(VisitingCard, uuid.UUID(old_card_id))
    old_breakdown = dict(old_card.score_breakdown)

    def _fake_v3(
        card, company, signals, seller_profile, existing_phones,
        product_fit_verdict, distance_km,
    ) -> dict:
        return {"total": 55, "version": "v3"}

    monkeypatch.setitem(scoring.SCORING_VERSIONS, "v3", _fake_v3)
    monkeypatch.setattr("app.services.scoring._SCORING_VERSION_ROLLOUT", {"v3": 100})

    new_card_id = _upload_one(client, jpeg_bytes, filename="new.jpg")
    _patch_vision(
        monkeypatch, _fields(full_name="New Contact", company_name=_unique_company_name("New Co"))
    )
    process_card(new_card_id)
    score_card_task(new_card_id)  # resolved via select_scoring_version -> "v3"

    db_session.expire_all()
    new_card = db_session.get(VisitingCard, uuid.UUID(new_card_id))
    assert new_card.score_breakdown["version"] == "v3"

    db_session.expire_all()
    old_card_again = db_session.get(VisitingCard, uuid.UUID(old_card_id))
    assert old_card_again.score_breakdown == old_breakdown


def test_free_rescore_stays_pinned_to_original_version_and_never_reselects(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """The core versioning/A-B guarantee: a free rescore (per
    20-field-correction) can change a card's score but must never change
    its scoring version — even if the rollout config now favors a
    different version at rescore time."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(
        monkeypatch,
        _fields(
            full_name="Pinned Version Contact",
            job_title="Manager",
            company_name=_unique_company_name("Pinned Version Co"),
        ),
    )
    process_card(card_id)
    _force_scoring_version(monkeypatch, "v1")
    score_card_task(card_id)

    db_session.expire_all()
    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert card.score_breakdown["version"] == "v1"

    correction_resp = client.post(
        f"/cards/{card_id}/corrections",
        json={"field_name": "job_title", "corrected_value": "Director"},
    )
    assert correction_resp.status_code == 200, correction_resp.text
    assert correction_resp.json()["rescore_available"] is True

    # Rollout now exclusively favors v2 — a rescore must still never re-roll.
    monkeypatch.setattr("app.services.scoring._SCORING_VERSION_ROLLOUT", {"v2": 100})
    calls: list[uuid.UUID] = []
    real_select = scoring.select_scoring_version
    monkeypatch.setattr(
        "app.services.scoring.select_scoring_version",
        lambda user_id: (calls.append(user_id), real_select(user_id))[1],
    )

    score_card_task(card_id)

    assert calls == [], "a rescore must never re-roll the experiment assignment"
    db_session.expire_all()
    rescored = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert rescored.score_breakdown["version"] == "v1"


def test_score_breakdown_version_is_queryable_via_jsonb_operator(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """Confirms score_breakdown->>'version' is directly queryable for
    per-version analysis, with no new column required."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(
        monkeypatch,
        _fields(full_name="Queryable Contact", company_name=_unique_company_name("Queryable Co")),
    )
    process_card(card_id)
    _force_scoring_version(monkeypatch, "v2")
    score_card_task(card_id)

    matches = db_session.execute(
        select(VisitingCard.card_id).where(VisitingCard.score_breakdown["version"].astext == "v2")
    ).scalars().all()
    assert uuid.UUID(card_id) in matches


# ==========================================================================
# 15. geocode_service — cache-first address geocoding + haversine distance.
# ==========================================================================


def test_geocode_service_blank_address_short_circuits(db_session):
    assert geocode_service.get_or_geocode(db_session, None) is None
    assert geocode_service.get_or_geocode(db_session, "   ") is None


def test_geocode_service_cache_hit_skips_lookup(db_session, monkeypatch):
    address = f"Unique Address {uuid.uuid4().hex[:8]}, Gurugram"
    monkeypatch.setattr(
        "app.services.geocode_service._lookup_nominatim",
        lambda addr: (Decimal("28.4"), Decimal("77.0")),
    )
    first = geocode_service.get_or_geocode(db_session, address)
    assert first == (28.4, 77.0)

    def _raise(addr):
        raise AssertionError("_lookup_nominatim must not be called on a cache hit")

    monkeypatch.setattr("app.services.geocode_service._lookup_nominatim", _raise)
    second = geocode_service.get_or_geocode(db_session, address)
    assert second == (28.4, 77.0)


def test_geocode_service_cached_failure_returns_none_without_relookup(db_session, monkeypatch):
    address = f"Unresolvable Address {uuid.uuid4().hex[:8]}"
    monkeypatch.setattr("app.services.geocode_service._lookup_nominatim", lambda addr: None)
    first = geocode_service.get_or_geocode(db_session, address)
    assert first is None

    def _raise(addr):
        raise AssertionError("_lookup_nominatim must not be called on a cached failure")

    monkeypatch.setattr("app.services.geocode_service._lookup_nominatim", _raise)
    second = geocode_service.get_or_geocode(db_session, address)
    assert second is None


def test_haversine_km_zero_for_identical_points():
    assert geocode_service.haversine_km((12.9716, 77.5946), (12.9716, 77.5946)) == 0.0


def test_haversine_km_known_city_pair_within_tolerance():
    # Delhi <-> Mumbai, real-world great-circle distance ~1150km.
    delhi = (28.6139, 77.2090)
    mumbai = (19.0760, 72.8777)
    distance = geocode_service.haversine_km(delhi, mumbai)
    assert 1100 <= distance <= 1200


# ==========================================================================
# 16. product_fit_service — cache-first AI judgment of buyer product fit.
# ==========================================================================


def test_product_fit_service_blank_signature_short_circuits(db_session):
    assert product_fit_service.get_or_judge_fit(db_session, None, "industrial", "manufacturer") is None
    assert product_fit_service.get_or_judge_fit(db_session, "   ", "industrial", "manufacturer") is None


def test_product_fit_service_cache_hit_skips_second_claude_call(db_session, monkeypatch):
    signature = f"pumps valves {uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(
        "app.services.product_fit_service._get_client",
        lambda: _FakeAnthropicClient("needs\nA manufacturer needs this."),
    )
    first = product_fit_service.get_or_judge_fit(db_session, signature, "industrial", "manufacturer")
    assert first == "needs"

    def _raise():
        raise AssertionError("Claude must not be called again on a cache hit")

    monkeypatch.setattr("app.services.product_fit_service._get_client", _raise)
    second = product_fit_service.get_or_judge_fit(db_session, signature, "industrial", "manufacturer")
    assert second == "needs"


def test_product_fit_service_unparseable_response_is_not_cached(db_session, monkeypatch):
    signature = f"pumps valves {uuid.uuid4().hex[:8]}"
    calls: list[int] = []

    def _get_client():
        calls.append(1)
        return _FakeAnthropicClient("I'm not sure, maybe?")

    monkeypatch.setattr("app.services.product_fit_service._get_client", _get_client)
    result = product_fit_service.get_or_judge_fit(db_session, signature, "industrial", "trader")
    assert result is None
    assert len(calls) == 1

    # A second call still reaches Claude — the failed response wasn't cached.
    result_again = product_fit_service.get_or_judge_fit(db_session, signature, "industrial", "trader")
    assert result_again is None
    assert len(calls) == 2


@pytest.mark.parametrize(
    "response_text,expected",
    [
        ("needs\nA manufacturer needs this.", "needs"),
        ("no_need\nThey resell the same product.", "no_need"),
        ("partial\nPlausible but not certain.", "partial"),
        ("NEEDS\nCase-insensitive.", "needs"),
    ],
)
def test_product_fit_service_parses_valid_verdicts(db_session, monkeypatch, response_text, expected):
    signature = f"machine {uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(
        "app.services.product_fit_service._get_client", lambda: _FakeAnthropicClient(response_text)
    )
    result = product_fit_service.get_or_judge_fit(db_session, signature, "industrial", "manufacturer")
    assert result == expected


# ==========================================================================
# 17. news_summary_provider — identity-verified article fetch + AI summary.
# ==========================================================================


def test_news_summary_provider_excludes_identity_mismatched_article(monkeypatch):
    monkeypatch.setattr(
        "app.services.enrichment_providers.news_summary_provider._search_news_rss",
        lambda query: [
            {"headline": "Alpha Industries announces new plant", "url": "https://example.com/a"},
            {"headline": "Totally unrelated startup raises funding", "url": "https://example.com/b"},
        ],
    )

    def _fake_fetch_html(url):
        if url == "https://example.com/a":
            return "<p>Alpha Industries Private Limited opens a new plant in Gurugram.</p>"
        return "<p>ZetaCorp Unrelated Startup raises a huge funding round from investors.</p>"

    monkeypatch.setattr(
        "app.services.enrichment_providers.news_summary_provider.website_fetch.fetch_html",
        _fake_fetch_html,
    )

    captured_prompts: list[str] = []

    class _CapturingMessages:
        def create(self, **kwargs):
            captured_prompts.append(kwargs["messages"][0]["content"])
            return _FakeAnthropicResponse('Summary text.\n{"tags": ["expansion"], "distress": false}')

    class _CapturingClient:
        def __init__(self) -> None:
            self.messages = _CapturingMessages()

    monkeypatch.setattr(
        "app.services.enrichment_providers.news_summary_provider.anthropic_client.get_client",
        lambda timeout: _CapturingClient(),
    )

    result = news_summary_provider.RealNewsSummaryProvider().summarize("Alpha Industries", "Gurugram")

    assert result.news_summary == "Summary text."
    assert "expansion" in result.tags
    assert result.distress_detected is False
    assert len(captured_prompts) == 1
    assert "ZetaCorp" not in captured_prompts[0], (
        "the identity-mismatched article must never reach the Claude prompt"
    )
    assert "Alpha Industries" in captured_prompts[0]


def test_news_summary_provider_detects_distress_from_json_tail():
    text = 'Company is closing operations.\n{"tags": [], "distress": true}'
    summary, tags, distress = news_summary_provider._parse_summary_response(text)
    assert summary == "Company is closing operations."
    assert distress is True
    assert tags == frozenset()


def test_news_summary_provider_malformed_json_tail_falls_back_gracefully():
    text = "Just a plain summary with no JSON tail at all."
    summary, tags, distress = news_summary_provider._parse_summary_response(text)
    assert summary == text
    assert tags == frozenset()
    assert distress is False


# ==========================================================================
# 18. share_price_provider — best-effort QOQ extraction.
# ==========================================================================


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Shares fell 12% after weak earnings", Decimal("-12")),
        ("Stock rose 8% on strong demand", Decimal("8")),
        ("Company reports 15% growth in some metric", None),  # no direction word
        ("No percentage mentioned here", None),
    ],
)
def test_share_price_extract_qoq_percentage(text, expected):
    assert share_price_provider._extract_qoq_percentage(text) == expected


def test_share_price_provider_stub_in_test_env():
    result = share_price_provider.StubSharePriceProvider().lookup("Any Company")
    assert result.is_publicly_listed is False
    assert result.qoq_growth_pct is None


# ==========================================================================
# 19. enrichment_service — the new news-summary + share-price lookups and
#     their shared distress-override combination step.
# ==========================================================================


def test_enrichment_service_distress_combines_news_and_share_price(db_session, monkeypatch):
    from app.services.enrichment_providers.news_summary_provider import NewsSummaryResult
    from app.services.enrichment_providers.share_price_provider import SharePriceResult

    company = Company(
        name=_unique_company_name("Distress Combo Co"),
        normalized_name="distress combo co",
    )
    db_session.add(company)
    db_session.commit()

    def _patch(news_distress: bool, qoq_pct: Decimal | None) -> None:
        class _NewsProvider:
            def summarize(self, name, hq_city=None):
                return NewsSummaryResult(news_summary="text", distress_detected=news_distress)

        class _PriceProvider:
            def lookup(self, name, hq_city=None):
                return SharePriceResult(is_publicly_listed=qoq_pct is not None, qoq_growth_pct=qoq_pct)

        monkeypatch.setattr(
            "app.services.enrichment_service.news_summary_provider.get_news_summary_provider",
            lambda: _NewsProvider(),
        )
        monkeypatch.setattr(
            "app.services.enrichment_service.share_price_provider.get_share_price_provider",
            lambda: _PriceProvider(),
        )

    _patch(news_distress=False, qoq_pct=None)
    signals, _ = enrichment_service.run_all_signal_lookups(db_session, company, gst_number=None)
    db_session.commit()
    assert signals.news_distress_detected is False

    _patch(news_distress=True, qoq_pct=None)
    signals, _ = enrichment_service.run_all_signal_lookups(db_session, company, gst_number=None)
    db_session.commit()
    assert signals.news_distress_detected is True

    _patch(news_distress=False, qoq_pct=Decimal("-10"))
    signals, _ = enrichment_service.run_all_signal_lookups(db_session, company, gst_number=None)
    db_session.commit()
    assert signals.news_distress_detected is True, "a decline at exactly the threshold must trigger the override"

    _patch(news_distress=False, qoq_pct=Decimal("-9"))
    signals, _ = enrichment_service.run_all_signal_lookups(db_session, company, gst_number=None)
    db_session.commit()
    assert signals.news_distress_detected is False, "a decline short of the threshold must not trigger the override"


# ==========================================================================
# 20. Full-worker integration: a "no_need" verdict and a >500km distance
#     land correctly through the real worker path, not just _v2().
# ==========================================================================


def test_v2_worker_integration_no_need_verdict_and_far_distance(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    user = _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(
        monkeypatch,
        _fields(
            full_name="Far No Need Contact",
            company_name=_unique_company_name("Far No Need Co"),
            address="Buyer City",
        ),
    )
    process_card(card_id)

    db_session.add(
        SellerProfile(
            user_id=uuid.UUID(user["user_id"]),
            industry="industrial machinery",
            product_lines="pumps valves",
            billing_address="Seller City",
        )
    )
    db_session.commit()

    monkeypatch.setattr(
        "app.workers.scoring_processing.product_fit_service.get_or_judge_fit",
        lambda *args, **kwargs: "no_need",
    )
    monkeypatch.setattr(
        "app.workers.scoring_processing.geocode_service.get_or_geocode",
        lambda db, address: (0.0, 0.0) if address == "Buyer City" else (10.0, 10.0),
    )

    _force_scoring_version(monkeypatch, "v2")
    score_card_task(card_id)

    db_session.expire_all()
    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    breakdown = card.score_breakdown
    assert breakdown["product_fit_score"] == 0
    assert breakdown["proximity_score"] == 1, "well over 500km apart -> beyond every tier"
