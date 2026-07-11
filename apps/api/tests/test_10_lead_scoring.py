"""
Tests for the `10-lead-scoring` feature (spec: `.claude/specs/10-lead-scoring.md`).

Written directly against the spec's documented contract, not against the
implementation of `services/scoring.py` or `workers/scoring_processing.py`:

- `score_card_task(card_id)` (Celery task) computes a 0-100 `lead_score` +
  5-component `score_breakdown` JSONB (`designation_score`,
  `company_size_score`, `industry_fit_score`, `momentum_signal_score`,
  `remark_signal_score`, plus `total`/`version`) from the card's
  `designation_level`/`special_remark`, its linked `Company`/`CompanySignals`
  (if any), and the card owner's `SellerProfile`. It never mutates
  `VisitingCard.status` and is a no-op (no eligibility to score) unless the
  card's `status == "extracted"`.
- Scoring is never auto-triggered — the sole triggers are the explicit
  `POST /cards/{card_id}/score` (single) and `POST /cards/score` (bulk, a
  best-effort skip-and-count over a caller-picked selection) endpoints.
- Re-scoring an already-scored, still-`extracted` card is allowed (no
  "already scored" guard) — this is how a seller picks up new
  `CompanySignals` data after running enrichment.
- A card with no linked company (`company_id is None`) must still score
  successfully, with the three company-derived components at 0.

Mocking strategy: `score_card_task` itself makes no outbound calls, so most tests
call it directly as a plain function (bypassing `.delay()`), exactly as
`test_07_data_enrichment.py` calls `enrich_company_task` directly. Endpoint
tests patch `app.services.card_service.score_card_task.delay` (mirroring that
file's `_patch_enrich_delay`) to assert enqueue/skip behavior without a real
Celery worker. Vision extraction is mocked via
`app.services.vision_client.extract_card_fields`, matching every other test
file's convention. The one cross-feature test (re-scoring after enrichment)
reuses `enrich_company_task` directly with a monkeypatched
`firmographics_provider` factory and a monkeypatched Anthropic summary
client, mirroring `test_07_data_enrichment.py`'s own patterns rather than
importing them cross-module.

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

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.main import app as fastapi_app
from app.models.company import Company
from app.models.company_signals import CompanySignals
from app.models.seller_profile import SellerProfile
from app.models.visiting_card import VisitingCard
from app.services.enrichment_providers.firmographics_provider import FirmographicsResult
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
# 6. Re-scoring after enrichment completes picks up new CompanySignals data.
# ==========================================================================


def test_rescoring_after_enrichment_picks_up_new_company_signals(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """DoD: 'Re-scoring an already-scored card ... updates lead_score/
    score_breakdown/scored_at to reflect the newly available company
    signals ... company_size_score ... visibly change between the pre- and
    post-enrichment scores.'"""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    company_name = _unique_company_name("Pre Enrichment Co")
    _patch_vision(monkeypatch, _fields(full_name="Pre Enrichment Contact", company_name=company_name))
    process_card(card_id)

    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    company_id = card.company_id
    assert company_id is not None
    company = db_session.get(Company, company_id)
    assert company.enrichment_status == "pending", "fixture setup: company must start unenriched"

    score_card_task(card_id)
    db_session.expire_all()
    first = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert first.score_breakdown["company_size_score"] == 0, (
        "no CompanySignals row exists yet — company_size_score must be 0"
    )
    first_total = first.lead_score

    _patch_summary_client(monkeypatch)
    monkeypatch.setattr(
        "app.services.enrichment_providers.firmographics_provider.get_firmographics_provider",
        lambda: _StaticFirmographicsProvider(
            FirmographicsResult(linkedin_employee_count=600, linkedin_follower_count=5000)
        ),
    )
    enrich_company_task(str(company_id))

    db_session.expire_all()
    assert db_session.get(Company, company_id).enrichment_status == "enriched"

    score_card_task(card_id)
    db_session.expire_all()
    second = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert second.score_breakdown["company_size_score"] == 25, (
        "600 LinkedIn employees must now land the top company-size band"
    )
    assert second.lead_score > first_total, (
        "re-scoring after enrichment must increase the total once company size data exists"
    )


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
