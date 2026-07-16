"""
Tests for the `07-data-enrichment` feature (spec:
`.claude/specs/07-data-enrichment.md`).

These tests are written directly against the spec's documented contract, not
against the implementation of `services/enrichment_service.py`,
`services/enrichment_summary.py`, `workers/enrichment_processing.py`, or the
nine `services/enrichment_providers/*.py` modules:

- `enrich_company_task(company_id, source_card_id=None)` (Celery task) fans
  out over eleven independent public-source lookups (`enrichment_service.
  run_all_signal_lookups`), each wrapped in its own try/except so one source
  failing never blocks another. It upserts a single `company_signals` row,
  generates a short summary (`enrichment_summary.generate_summary`), and sets
  `enrichment_status` to `"enriched"` (>=1 source found something) or
  `"not_found"` (every source empty) — never left at `"pending"`. It is a
  no-op (no lookups invoked) whenever the company isn't currently `"pending"`
  — the idempotency guard `CLAUDE.md` requires before any repeated outbound
  lookup.
- Every provider module ships only a `Protocol` + a `Stub*` class + a
  `get_*_provider()` factory, and every stub returns "no signal found" — so
  the default (no monkeypatching) run of `enrich_company_task` must land on
  `"not_found"`, never `"enriched"`, with all `company_signals` columns null.
- Enrichment is **never** auto-triggered by `card_processing.process_card` —
  parsing a card only ever leaves its linked `Company` at `enrichment_status
  == "pending"`. The sole trigger is the explicit `POST
  /cards/{card_id}/enrich-company` endpoint (`card_service.
  enrich_company_now`), which requires the given card's own `company_id` to
  be set and that company to still be `"pending"` (`400`/`409` otherwise),
  then enqueues `enrich_company_task.delay(company_id, card_id)` — the *card
  id*, never the raw GSTIN (a deliberate security fix: the GSTIN is
  re-loaded from that card id inside the worker instead of crossing the
  Celery argument boundary).
- `GET /cards/{card_id}` (`card_service.get_card_detail`) now also joins
  `company_signals` for a handful of headline fields, null-safe when no
  signals row exists yet for that company.

Mocking strategy: this feature's only two external boundaries are (a) the
nine enrichment provider factories (`get_*_provider()`), monkeypatched per
the DASHR AI test-engineering convention of "monkeypatching one factory to
return a fake object that returns real data is the standard way to simulate
'one source found something'" — the default (unpatched) stub already returns
"no signal" so most tests need no provider mocking at all; and (b) the
Anthropic text-completion call inside `enrichment_summary.generate_summary`,
mocked via the same `_get_client()` seam `vision_client.py` already
establishes (`app.services.enrichment_summary._get_client`), never the raw
`anthropic` SDK and never a real network call. Vision extraction itself is
mocked exactly as `test_05_parsing_visiting_card.py` does, via
`app.services.vision_client.extract_card_fields`.

Judgment calls made in the absence of explicit spec text:
  1. **Task-level tests (`enrich_company_task` in isolation) construct a
     `Company` row directly via the ORM** rather than driving a full
     card-upload-and-extract flow, since `enrich_company_task` only ever
     reads/writes `Company`/`CompanyEnrichment`/`CompanySignals` by
     `company_id` — none of its logic depends on how the company came to
     exist. The end-to-end "the explicit CTA enqueues enrichment for the
     right company/card id" wiring is covered separately in the
     "POST /cards/{card_id}/enrich-company" section, which *does* drive a
     full upload+extract flow.
  2. **Idempotency (item 4)** is covered by two tests: one exercising a
     genuine second `enrich_company_task` call after a real first run
     reaches `"not_found"`, and one directly setting `enrichment_status =
     "enriched"` (equivalent terminal state, cheaper to set up) to confirm
     the same guard holds for the other terminal status.
  3. **Distinct company names per test.** Per task instructions,
     `companies` is not truncated by `conftest.py`'s autouse `_clean_tables`
     fixture (no FK path back to `users`), so every test that creates a
     `Company` row uses a name containing a fresh `uuid.uuid4()` fragment to
     guarantee no accidental match with a row left behind by another test.
  4. **Tenant isolation** targets `GET /cards/{card_id}` and `POST
     /cards/{card_id}/enrich-company` — the two endpoints this feature adds
     or changes — not `Company`/`CompanyEnrichment`/`CompanySignals`
     directly, since the spec is explicit that those three tables are a
     deliberate, documented exception to per-org scoping (a shared cache
     keyed by normalized company name). The thing worth regression-testing
     is that the *card* gate (`get_visible_card`) still blocks a different
     org's access to, or mutation of, a card even now that its `company`
     payload carries richer summary/signal data and can trigger enrichment.
  5. **The "no new paid-API setting" Definition-of-Done line** is encoded as
     a standing structural assertion against `Settings.model_fields` (the
     set of `_api_key`/`_secret`-matching field names must equal the
     baseline that already existed before this step), rather than a
     git-diff grep, since a diff-based check isn't something a pytest run
     can express directly.
"""

from __future__ import annotations

import io
import uuid

import anthropic
import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app.core.config import settings
from app.main import app as fastapi_app
from app.services import enrichment_summary
from app.services.enrichment_providers.news_signal_provider import RealNewsSignalProvider
from app.models.company import Company
from app.models.company_enrichment import CompanyEnrichment
from app.models.company_signals import CompanySignals
from app.models.visiting_card import VisitingCard
from app.services.enrichment_providers.firmographics_provider import FirmographicsResult
from app.workers.card_processing import process_card
from app.workers.enrichment_processing import enrich_company_task
from conftest import create_verified_user

# --------------------------------------------------------------------------
# Company/signal columns that must stay null unless a specific source
# populated them — reused across several assertions below.
# --------------------------------------------------------------------------

ALL_SIGNAL_COLUMNS = [
    "cin", "incorporation_date", "registry_status", "registered_address",
    "authorized_capital", "paid_up_capital", "gstin_verified", "gstin_status",
    "udyam_registered", "udyam_category", "linkedin_employee_count",
    "linkedin_follower_count", "estimated_revenue_band", "product_lines_summary",
    "plant_size_signal", "active_job_postings_count", "hiring_signal",
    "gem_tender_count", "gem_total_tender_value", "import_export_activity",
    "shipment_count_last_12m", "recent_news_signals", "google_rating",
    "google_review_count", "marketplace_vintage_years", "marketplace_verified_badge",
    "marketplace_located_in_industrial_area",
]


def _unique_company_name(label: str) -> str:
    """`companies` is not truncated between tests — every row created here
    must carry a name no other test could ever also create."""
    return f"{label} {uuid.uuid4().hex[:10]} Pvt Ltd"


def _create_pending_company(db_session, name: str | None = None, website: str | None = None) -> uuid.UUID:
    """Constructs a bare `Company` row exactly like `extraction_service.
    _get_or_create_company` does for a never-seen company name: name +
    normalized_name set, `enrichment_status` left at its 'pending' server
    default."""
    name = name or _unique_company_name("Enrichment Target")
    company = Company(name=name, normalized_name=name.strip().lower(), website=website)
    db_session.add(company)
    db_session.commit()
    db_session.refresh(company)
    assert company.enrichment_status == "pending", "fixture setup: a fresh company must start pending"
    return company.company_id


# --------------------------------------------------------------------------
# Anthropic text-completion mocking for enrichment_summary.generate_summary
# — the ONLY LLM boundary this feature calls when at least one signal was
# found. Never a real network call.
# --------------------------------------------------------------------------


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
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeAnthropicResponse(self._text)


class _FakeAnthropicClient:
    def __init__(self, text: str) -> None:
        self.messages = _FakeAnthropicMessages(text)


def _patch_summary_client(monkeypatch: pytest.MonkeyPatch, text: str = "Fixed test summary text.") -> _FakeAnthropicClient:
    fake_client = _FakeAnthropicClient(text)
    monkeypatch.setattr("app.services.enrichment_summary._get_client", lambda: fake_client)
    return fake_client


# --------------------------------------------------------------------------
# Provider fakes — used to simulate "one source found something" by
# monkeypatching that source's `get_*_provider()` factory.
# --------------------------------------------------------------------------


class _StaticFirmographicsProvider:
    """Returns a fixed, fully-populated LinkedIn result regardless of input
    — used to simulate exactly one succeeding source without touching any
    other provider's (stub, empty) behavior."""

    def __init__(self, result: FirmographicsResult) -> None:
        self._result = result

    def lookup_linkedin(self, company_name: str, website: str | None) -> FirmographicsResult:
        return self._result


class _RaisingRegistryProvider:
    """Simulates a source that is down/blocked/erroring — every call raises."""

    def lookup(self, company_name: str):
        raise RuntimeError("simulated MCA/Zauba scrape failure")


class _TrackingRegistryProvider:
    """Records every call it receives, so a test can assert a provider was
    (or, for idempotency, was NOT) invoked at all."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def lookup(self, company_name: str):
        from app.services.enrichment_providers.registry_provider import RegistryResult

        self.calls.append(company_name)
        return RegistryResult()


# --------------------------------------------------------------------------
# Vision-model mocking — reused for the "trigger hook" and GET /cards/{id}
# tests, matching test_05_parsing_visiting_card.py's established convention.
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


def _upload_two(client: TestClient, jpeg_bytes: bytes) -> tuple[str, str]:
    """Two files in one bulk-upload call share one `upload_batch_id` with
    sequential `batch_sequence` (0, 1) — required for the back-of-card
    sibling merge that produces the "merged" trigger-hook scenario."""
    resp = _upload_files(client, [("front.jpg", jpeg_bytes, "image/jpeg"), ("back.jpg", jpeg_bytes, "image/jpeg")])
    assert resp.status_code == 201, resp.text
    cards = resp.json()["cards"]
    return cards[0]["card_id"], cards[1]["card_id"]


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
    permanent-failure ('this wasn't a business card') case, used here only to
    drive a card to status='failed' for the "no enqueue on failure" test."""
    return _fields(full_name=None, raw_ocr_text="blank or unrelated photo")


# ==========================================================================
# 1. `enrich_company_task` against a company with every provider stubbed
#    (default, no monkeypatching).
# ==========================================================================


def test_enrich_company_task_default_stubs_results_in_not_found_with_null_signals_and_graceful_summary(
    db_session,
):
    """DoD: 'A run against Company with every source stubbed (default dev
    config) still produces exactly one company_signals row (all columns
    null) and a non-null companies.summary (a graceful "no public data
    found" style message), with enrichment_status="not_found" and no
    unhandled exception in the worker log.'"""
    company_id = _create_pending_company(db_session)

    enrich_company_task(str(company_id))  # bare call: no retry path involved

    db_session.expire_all()
    company = db_session.get(Company, company_id)
    assert company.enrichment_status == "not_found", (
        "with every provider stubbed to 'no signal', the run must resolve to 'not_found', "
        "never left at 'pending'"
    )
    assert company.summary, "a graceful summary message must be generated even with zero signals found"
    assert "no public data found" in company.summary.lower(), (
        f"expected a graceful 'no public data found' style message, got: {company.summary!r}"
    )
    assert company.summary_generated_at is not None
    assert company.enriched_at is not None

    signals_rows = db_session.scalars(
        select(CompanySignals).where(CompanySignals.company_id == company_id)
    ).all()
    assert len(signals_rows) == 1, "exactly one company_signals row must exist, never zero or duplicated"
    signals = signals_rows[0]
    for column in ALL_SIGNAL_COLUMNS:
        assert getattr(signals, column) is None, f"{column} must be null when every source is a stub"

    audit_rows = db_session.scalars(
        select(CompanyEnrichment).where(CompanyEnrichment.company_id == company_id)
    ).all()
    assert audit_rows == [], "zero company_enrichment audit rows must be written when no source answers"


# ==========================================================================
# 2. One provider factory monkeypatched to return real data, others default.
# ==========================================================================


def test_enrich_company_task_one_source_answering_marks_enriched_and_writes_one_audit_row(
    db_session, monkeypatch
):
    """DoD: 'A successful run writes one company_enrichment audit row per
    source that returned data ... each with the correct source tag and a
    non-null payload' + 'enrichment_status="enriched"' once at least one
    source found something, with the other columns staying null."""
    company_id = _create_pending_company(db_session)
    _patch_summary_client(monkeypatch, text="ABC Industries has ~120 LinkedIn employees.")

    fake_payload = {"employees": 120, "followers": 4500}
    monkeypatch.setattr(
        "app.services.enrichment_providers.firmographics_provider.get_firmographics_provider",
        lambda: _StaticFirmographicsProvider(
            FirmographicsResult(
                linkedin_employee_count=120,
                linkedin_follower_count=4500,
                raw_payload=fake_payload,
            )
        ),
    )

    enrich_company_task(str(company_id))

    db_session.expire_all()
    company = db_session.get(Company, company_id)
    assert company.enrichment_status == "enriched", (
        "at least one source (LinkedIn) returning data must land enrichment_status='enriched'"
    )
    assert company.summary == "ABC Industries has ~120 LinkedIn employees.", (
        "the mocked Claude summary must be persisted verbatim onto companies.summary"
    )
    assert company.summary_generated_at is not None

    signals = db_session.get(CompanySignals, company_id)
    assert signals is not None
    assert signals.linkedin_employee_count == 120
    assert signals.linkedin_follower_count == 4500

    untouched_columns = [c for c in ALL_SIGNAL_COLUMNS if c not in ("linkedin_employee_count", "linkedin_follower_count")]
    for column in untouched_columns:
        assert getattr(signals, column) is None, (
            f"{column} must stay null — only the LinkedIn source returned data in this run"
        )

    audit_rows = db_session.scalars(
        select(CompanyEnrichment).where(CompanyEnrichment.company_id == company_id)
    ).all()
    assert len(audit_rows) == 1, "exactly one audit row must be written — one per source that answered"
    assert audit_rows[0].source == "linkedin"
    assert audit_rows[0].payload == fake_payload


# ==========================================================================
# 3. One provider raises while another succeeds — the failure never
#    propagates and never blocks the succeeding source.
# ==========================================================================


def test_enrich_company_task_one_source_raising_does_not_abort_the_others(db_session, monkeypatch):
    """DoD: 'Simulating one source raising an exception (e.g. monkeypatching
    registry_provider to raise) while the others succeed still results in
    enrichment_status="enriched", a populated company_signals row for the
    sources that succeeded ... the one failure never aborts the run.'"""
    company_id = _create_pending_company(db_session)
    _patch_summary_client(monkeypatch)

    monkeypatch.setattr(
        "app.services.enrichment_providers.registry_provider.get_registry_provider",
        lambda: _RaisingRegistryProvider(),
    )
    fake_payload = {"employees": 55}
    monkeypatch.setattr(
        "app.services.enrichment_providers.firmographics_provider.get_firmographics_provider",
        lambda: _StaticFirmographicsProvider(
            FirmographicsResult(linkedin_employee_count=55, linkedin_follower_count=None, raw_payload=fake_payload)
        ),
    )

    enrich_company_task(str(company_id))  # must not raise/propagate the registry failure

    db_session.expire_all()
    company = db_session.get(Company, company_id)
    assert company.enrichment_status == "enriched", (
        "the succeeding LinkedIn source must still land enrichment_status='enriched' despite the "
        "registry source raising"
    )
    assert company.summary is not None

    signals = db_session.get(CompanySignals, company_id)
    assert signals is not None
    assert signals.linkedin_employee_count == 55, "the succeeding source's data must still persist"
    for column in ("cin", "incorporation_date", "registry_status", "registered_address",
                   "authorized_capital", "paid_up_capital"):
        assert getattr(signals, column) is None, (
            f"{column} must stay null — the registry source raised and must contribute nothing"
        )

    audit_rows = db_session.scalars(
        select(CompanyEnrichment).where(CompanyEnrichment.company_id == company_id)
    ).all()
    assert len(audit_rows) == 1, "the raising source must write zero audit rows"
    assert audit_rows[0].source == "linkedin", "only the succeeding source's audit row must exist"


# ==========================================================================
# 4. Idempotency — a company not in "pending" status is never re-run.
# ==========================================================================


def test_enrich_company_task_second_call_after_reaching_not_found_is_a_noop(db_session, monkeypatch):
    """DoD: 'A second card naming the same company ... after the first is
    already "enriched"/"not_found" does not enqueue another lookup —
    verified by no new company_enrichment rows being written for that
    company_id.' Exercised here as two direct calls to the task itself."""
    company_id = _create_pending_company(db_session)

    enrich_company_task(str(company_id))  # first run -> not_found (all stubs)
    db_session.expire_all()
    assert db_session.get(Company, company_id).enrichment_status == "not_found", (
        "fixture setup: the first run must reach a terminal 'not_found' status"
    )

    tracking = _TrackingRegistryProvider()
    monkeypatch.setattr(
        "app.services.enrichment_providers.registry_provider.get_registry_provider",
        lambda: tracking,
    )

    enrich_company_task(str(company_id))  # second run — must skip entirely

    db_session.expire_all()
    company = db_session.get(Company, company_id)
    assert company.enrichment_status == "not_found", "a second run must not change an already-terminal status"
    assert tracking.calls == [], (
        "a company whose status is not 'pending' must never invoke any provider lookup on a re-run"
    )
    audit_rows = db_session.scalars(
        select(CompanyEnrichment).where(CompanyEnrichment.company_id == company_id)
    ).all()
    assert audit_rows == [], "no new company_enrichment rows must be written on an idempotent skip"


def test_enrich_company_task_skips_lookups_when_company_already_enriched(db_session, monkeypatch):
    """Companion idempotency case for the other terminal status: a company
    already 'enriched' must also never be re-run."""
    company_id = _create_pending_company(db_session)
    company = db_session.get(Company, company_id)
    company.enrichment_status = "enriched"
    db_session.commit()

    tracking = _TrackingRegistryProvider()
    monkeypatch.setattr(
        "app.services.enrichment_providers.registry_provider.get_registry_provider",
        lambda: tracking,
    )

    enrich_company_task(str(company_id))

    db_session.expire_all()
    refreshed = db_session.get(Company, company_id)
    assert refreshed.enrichment_status == "enriched", "an already-enriched company's status must be untouched"
    assert tracking.calls == [], "an already-enriched company must never invoke any provider lookup"
    signals = db_session.get(CompanySignals, company_id)
    assert signals is None, (
        "no company_signals row should be created for a company that was never actually run "
        "through the fan-out (status was set directly to 'enriched' for this test)"
    )


# ==========================================================================
# 5. GET /cards/{card_id} — summary + headline signal fields.
# ==========================================================================


def test_get_card_detail_returns_summary_and_headline_fields_once_company_is_enriched(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """Spec: 'GET /cards/{card_id} for a card whose company is "enriched"
    returns summary and the six headline fields in its company object.'"""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    company_name = _unique_company_name("Vertex Signals Co")
    _patch_vision(monkeypatch, _fields(full_name="Test Contact", company_name=company_name))
    process_card(card_id)

    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    company_id = card.company_id
    assert company_id is not None, "fixture setup: extraction must have linked a company"

    _patch_summary_client(monkeypatch, text="Vertex Signals Co is a mid-size industrial supplier.")
    monkeypatch.setattr(
        "app.services.enrichment_providers.firmographics_provider.get_firmographics_provider",
        lambda: _StaticFirmographicsProvider(
            FirmographicsResult(linkedin_employee_count=88, linkedin_follower_count=900, raw_payload={"n": 88})
        ),
    )
    enrich_company_task(str(company_id))

    resp = client.get(f"/cards/{card_id}")
    assert resp.status_code == 200, resp.text
    company_out = resp.json()["company"]
    assert company_out["enrichment_status"] == "enriched"
    assert company_out["summary"] == "Vertex Signals Co is a mid-size industrial supplier."
    assert company_out["summary_generated_at"] is not None
    assert company_out["linkedin_employee_count"] == 88
    assert company_out["estimated_revenue_band"] is None, (
        "no revenue-band-relevant source (udyam/paid-up capital) answered in this run"
    )
    assert company_out["gstin_verified"] is None
    assert company_out["udyam_registered"] is None
    assert company_out["hiring_signal"] is None
    assert company_out["google_rating"] is None


def test_get_card_detail_headline_fields_are_null_while_company_is_still_pending(
    client, fake_otp_provider, jpeg_bytes, monkeypatch
):
    """Converse of the above: a company that hasn't been enriched yet
    (enrichment_status='pending', no company_signals row at all) must
    expose null summary/headline fields, not an error."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(
        monkeypatch,
        _fields(full_name="Pending Contact", company_name=_unique_company_name("Pending Signals Co")),
    )
    process_card(card_id)

    resp = client.get(f"/cards/{card_id}")
    assert resp.status_code == 200, resp.text
    company_out = resp.json()["company"]
    assert company_out["enrichment_status"] == "pending"
    assert company_out["summary"] is None
    assert company_out["summary_generated_at"] is None
    for field in (
        "linkedin_employee_count", "estimated_revenue_band", "gstin_verified",
        "udyam_registered", "hiring_signal", "google_rating",
    ):
        assert company_out[field] is None, (
            f"{field} must be null (not an error) when no company_signals row exists yet"
        )


# ==========================================================================
# 6. POST /cards/{card_id}/enrich-company — the explicit "Enrich Company"
#    CTA. process_card never auto-enqueues enrichment (that earlier design
#    was deliberately reversed — see spec's Background jobs "Trigger"
#    note): a seller must call this endpoint per card, mirroring how
#    "Parse Cards" is itself a separate explicit action from upload.
# ==========================================================================


def _patch_enrich_delay(monkeypatch: pytest.MonkeyPatch) -> list[tuple[tuple, dict]]:
    captured: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        "app.services.card_service.enrich_company_task.delay",
        lambda *args, **kwargs: captured.append((args, kwargs)),
    )
    return captured


def _assert_gstin_never_appears(captured_calls: list[tuple[tuple, dict]], gstin: str) -> None:
    for args, kwargs in captured_calls:
        assert gstin not in args, f"the raw GSTIN must never be a positional arg to enrich_company_task.delay, got {args!r}"
        assert gstin not in kwargs.values(), f"the raw GSTIN must never be a kwarg value, got {kwargs!r}"
        for value in args:
            assert gstin not in str(value), (
                f"the raw GSTIN must never appear anywhere in a captured enqueue call, got {args!r}"
            )


def test_process_card_never_auto_enqueues_enrichment(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """Spec: 'process_card does not auto-enqueue enrichment after parsing.'
    A freshly extracted card with a new company must leave that company at
    enrichment_status='pending' and must never call enrich_company_task.delay
    on its own."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    company_name = _unique_company_name("No Auto Enrich Co")
    _patch_vision(monkeypatch, _fields(full_name="Fresh Contact", company_name=company_name))
    captured = _patch_enrich_delay(monkeypatch)

    process_card(card_id)

    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert card.status == "extracted"
    assert card.company_id is not None
    company = db_session.get(Company, card.company_id)
    assert company.enrichment_status == "pending", (
        "parsing alone must never start enrichment — it stays pending until the explicit CTA is used"
    )
    assert captured == [], "process_card must never call enrich_company_task.delay itself"


def test_enrich_company_endpoint_enqueues_with_company_and_card_id_never_gstin(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """POST /cards/{card_id}/enrich-company on an extracted card whose
    company is still 'pending' must enqueue enrich_company_task.delay with
    (company_id, card_id) — the card id, never the raw GSTIN (the same
    security fix the old auto-trigger had, now on the explicit endpoint)."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    gstin = "27ABCDE1234F1Z5"
    company_name = _unique_company_name("New Extraction Co")
    _patch_vision(monkeypatch, _fields(full_name="Fresh Contact", company_name=company_name, gst_number=gstin))
    process_card(card_id)
    captured = _patch_enrich_delay(monkeypatch)

    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert card.gst_number == gstin, "fixture setup: the card must actually carry the GSTIN we're checking for"

    resp = client.post(f"/cards/{card_id}/enrich-company")
    assert resp.status_code == 200, resp.text

    assert len(captured) == 1, "exactly one enrich_company_task.delay call must be made"
    args, kwargs = captured[0]
    assert args == (str(card.company_id), str(card.card_id)), (
        "enrich_company_task.delay must be called with (company_id, card_id) positionally"
    )
    # billed=False: a fresh user's first enrichment is covered by the free
    # allowance (15-wallet-usage), not billed from the wallet.
    assert kwargs == {"billed": False}
    _assert_gstin_never_appears(captured, gstin)


def test_enrich_company_endpoint_on_card_with_no_company_returns_400_and_never_enqueues(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """A card that never got linked to a company (extraction failed
    entirely) has nothing to enrich — the endpoint must reject it and never
    enqueue anything."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(monkeypatch, _empty_fields())
    process_card(card_id)
    captured = _patch_enrich_delay(monkeypatch)

    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    assert card.status == "failed", "fixture setup: this card must end in status='failed'"
    assert card.company_id is None

    resp = client.post(f"/cards/{card_id}/enrich-company")
    assert resp.status_code == 400, resp.text
    assert captured == [], "a card with no linked company must never enqueue enrich_company_task"


def test_enrich_company_endpoint_on_already_enriched_company_returns_409_and_never_enqueues(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """Enrichment is a one-shot action per company (DoD: only enrichment_status
    == "pending" is eligible) — calling the CTA again once a company is past
    "pending" must be rejected, not silently re-run."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(
        monkeypatch,
        _fields(full_name="Already Enriched Contact", company_name=_unique_company_name("Already Enriched Co")),
    )
    process_card(card_id)

    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    company = db_session.get(Company, card.company_id)
    company.enrichment_status = "enriched"
    db_session.commit()
    captured = _patch_enrich_delay(monkeypatch)

    resp = client.post(f"/cards/{card_id}/enrich-company")
    assert resp.status_code == 409, resp.text
    assert captured == [], "a company that's already past 'pending' must never be re-enqueued"


def test_enrich_company_endpoint_for_merged_back_card_returns_400_since_its_own_company_id_is_null(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """A merged back-of-card row's own company_id stays null (see
    extraction_service._merge_fill_gaps — only the canonical front card gets
    it). The endpoint operates on the given card's own company_id (matching
    how GET /cards/{card_id} already shows company=None for a merged card),
    not resolved through merged_into_card_id, so calling it on the back card
    must 400; calling it on the canonical front card must succeed."""
    _authenticated_user(client, fake_otp_provider)
    front_id, back_id = _upload_two(client, jpeg_bytes)
    company_name = _unique_company_name("Merge Trigger Co")

    _patch_vision(monkeypatch, _fields(full_name="Front Contact", company_name=company_name))
    process_card(front_id)
    _patch_vision(
        monkeypatch,
        _fields(is_back_of_card=True, full_name=None, company_name=None, address="Back side address"),
    )
    process_card(back_id)

    front = db_session.get(VisitingCard, uuid.UUID(front_id))
    back = db_session.get(VisitingCard, uuid.UUID(back_id))
    assert back.status == "merged"
    assert back.company_id is None, "fixture setup: a merged back-of-card row must never get its own company_id"
    assert front.company_id is not None

    captured = _patch_enrich_delay(monkeypatch)

    back_resp = client.post(f"/cards/{back_id}/enrich-company")
    assert back_resp.status_code == 400, back_resp.text
    assert captured == [], "the back card's own (null) company_id must never enqueue anything"

    front_resp = client.post(f"/cards/{front_id}/enrich-company")
    assert front_resp.status_code == 200, front_resp.text
    assert len(captured) == 1
    assert captured[0][0] == (str(front.company_id), str(front.card_id))


def test_enrich_company_endpoint_for_another_users_card_returns_404(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """Tenant isolation: this new mutation endpoint reuses the existing
    org-scoped get_visible_card — a user must never be able to trigger
    enrichment for (or even discover the existence of) another org's card."""
    _authenticated_user(client, fake_otp_provider)

    with TestClient(fastapi_app) as other_client:
        _authenticated_user(other_client, fake_otp_provider)
        their_card_id = _upload_one(other_client, jpeg_bytes)
        _patch_vision(
            monkeypatch,
            _fields(full_name="Owner Contact", company_name=_unique_company_name("Owner Only Co")),
        )
        process_card(their_card_id)
        captured = _patch_enrich_delay(monkeypatch)

        resp = client.post(f"/cards/{their_card_id}/enrich-company")

    assert resp.status_code == 404, (
        f"a user in a different org must never be able to trigger enrichment for another org's "
        f"card, got {resp.status_code}: {resp.text}"
    )
    assert captured == [], "another org's card must never be enqueued for enrichment"


# ==========================================================================
# 8. Deleting a Company row cascades to delete its company_signals row.
# ==========================================================================


def test_deleting_company_cascades_delete_of_company_signals_row(db_session):
    """DoD: 'Deleting a Company row cascade-deletes its company_signals row
    (FK ondelete="CASCADE"), leaving no orphaned signal row.'"""
    company_id = _create_pending_company(db_session)
    enrich_company_task(str(company_id))  # default stubs -> still creates one company_signals row

    assert db_session.get(CompanySignals, company_id) is not None, (
        "fixture setup: a company_signals row must exist before the delete"
    )

    company = db_session.get(Company, company_id)
    db_session.delete(company)
    db_session.commit()

    db_session.expire_all()
    assert db_session.get(Company, company_id) is None
    assert db_session.get(CompanySignals, company_id) is None, (
        "deleting the parent Company row must cascade-delete its company_signals row, leaving no orphan"
    )


# ==========================================================================
# 9. Tenant isolation on GET /cards/{card_id} — the endpoint whose response
#    shape this feature changes. `Company`/`CompanyEnrichment`/`CompanySignals`
#    are intentionally NOT org-scoped (documented exception, see spec's
#    "Database changes" section), but the *card* they're attached to still is
#    — a user in one org must never be able to read another org's card (and,
#    through it, its now-richer `company` payload: summary + headline
#    signals) by guessing/reusing a card id.
# ==========================================================================


def test_get_card_detail_for_another_users_enriched_card_returns_404(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """Tenant-isolation regression for this feature specifically: even once
    a card's company has real summary/headline-signal data attached, a user
    from a different org must still get a plain 404 for someone else's card
    — never a leaked (even partial) company payload."""
    _authenticated_user(client, fake_otp_provider)

    with TestClient(fastapi_app) as other_client:
        _authenticated_user(other_client, fake_otp_provider)
        their_card_id = _upload_one(other_client, jpeg_bytes)
        company_name = _unique_company_name("Other Org Enriched Co")
        _patch_vision(monkeypatch, _fields(full_name="Their Contact", company_name=company_name))
        process_card(their_card_id)

        their_card = db_session.get(VisitingCard, uuid.UUID(their_card_id))
        assert their_card.company_id is not None, "fixture setup: extraction must have linked a company"

        _patch_summary_client(monkeypatch, text="This other org's company summary must never leak.")
        monkeypatch.setattr(
            "app.services.enrichment_providers.firmographics_provider.get_firmographics_provider",
            lambda: _StaticFirmographicsProvider(
                FirmographicsResult(linkedin_employee_count=42, linkedin_follower_count=7, raw_payload={"n": 42})
            ),
        )
        enrich_company_task(str(their_card.company_id))

        # Sanity check: the other org's own client CAN see its own enriched data.
        own_resp = other_client.get(f"/cards/{their_card_id}")
        assert own_resp.status_code == 200, own_resp.text
        assert own_resp.json()["company"]["summary"] == "This other org's company summary must never leak."

        resp = client.get(f"/cards/{their_card_id}")

    assert resp.status_code == 404, (
        f"a user in a different org must never be able to fetch another org's card detail (nor its "
        f"enriched company summary/headline fields through it), got {resp.status_code}: {resp.text}"
    )


# ==========================================================================
# 10. No new paid-API/credential setting is introduced by this step.
#
# The standing regression version of this check now lives in
# test_core_config_known_secrets.py — it inherently can't be scoped to just
# this step's diff (it introspects the live Settings class, which reflects
# every step's config additions, not just 07's), so keeping it here would
# force every future step that legitimately adds a credential (e.g.
# 14-wallet-recharge's Razorpay secrets) to keep editing this unrelated
# file. See that file's docstring for the current whole-codebase allowlist.
# ==========================================================================


# ==========================================================================
# 11. Real (non-stub) implementations: Google News RSS parsing and the
#     Wikipedia-sourced summary-only context, both confirmed live against
#     the real network during development but exercised here against a
#     canned HTTP response only — this suite must never make a real
#     network call.
# ==========================================================================


class _FakeHttpResponse:
    def __init__(self, status_code: int = 200, content: bytes = b"", json_data=None):
        self.status_code = status_code
        self.content = content
        self._json_data = json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)

    def json(self):
        return self._json_data


_SAMPLE_RSS = b"""<?xml version="1.0"?>
<rss><channel>
<item><title>Acme Corp raises Series B funding round</title><link>https://example.com/a</link><pubDate>Mon, 01 Jan 2026 00:00:00 GMT</pubDate></item>
<item><title>Acme Corp announces expansion into new markets</title><link>https://example.com/b</link><pubDate>Tue, 02 Jan 2026 00:00:00 GMT</pubDate></item>
<item><title>Acme Corp quarterly results released</title><link>https://example.com/c</link><pubDate>Wed, 03 Jan 2026 00:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_real_news_signal_provider_parses_rss_and_classifies_signal_types(monkeypatch):
    """The real (non-stub) Google News RSS provider, exercised against a
    canned RSS payload (never a live request) — confirms XML parsing and
    keyword-based signal_type classification."""
    monkeypatch.setattr(
        "app.services.enrichment_providers.news_signal_provider.httpx.get",
        lambda *a, **k: _FakeHttpResponse(content=_SAMPLE_RSS),
    )
    result = RealNewsSignalProvider().lookup("Acme Corp")

    assert result.recent_news_signals is not None
    assert len(result.recent_news_signals) == 3
    assert result.recent_news_signals[0]["signal_type"] == "funding"
    assert result.recent_news_signals[1]["signal_type"] == "expansion"
    assert result.recent_news_signals[2]["signal_type"] == "other"
    assert result.raw_payload is not None, "a successful lookup must populate raw_payload for the audit row"


def test_real_news_signal_provider_returns_no_signal_when_feed_is_empty(monkeypatch):
    monkeypatch.setattr(
        "app.services.enrichment_providers.news_signal_provider.httpx.get",
        lambda *a, **k: _FakeHttpResponse(content=b"<?xml version='1.0'?><rss><channel></channel></rss>"),
    )
    result = RealNewsSignalProvider().lookup("Nonexistent Co")
    assert result.recent_news_signals is None
    assert result.raw_payload is None


def test_get_news_signal_provider_returns_stub_in_test_env_and_real_otherwise(monkeypatch):
    """The environment gate itself: tests always get the stub (this suite's
    own safety net against accidental live network calls), but the
    factory must resolve to the real implementation outside test env."""
    from app.services.enrichment_providers import news_signal_provider as mod

    assert isinstance(mod.get_news_signal_provider(), mod.StubNewsSignalProvider), (
        "ENVIRONMENT=test (set by this suite's conftest.py) must always yield the stub"
    )

    monkeypatch.setattr(settings, "environment", "development")
    assert isinstance(mod.get_news_signal_provider(), mod.RealNewsSignalProvider)


def test_wikipedia_context_resolves_legal_name_to_brand_name_article(monkeypatch):
    """The two-step resolution this feature specifically needs: a card's
    legal/registered company name (e.g. 'InterGlobe Aviation Limited')
    often differs from its Wikipedia article title (the brand name,
    'IndiGo') — confirms the search-then-summary flow, against canned
    responses only."""
    monkeypatch.setattr(enrichment_summary.settings, "environment", "development")

    calls = []

    def fake_get(url, params=None, timeout=None, headers=None):
        calls.append(url)
        if "action" in (params or {}):
            return _FakeHttpResponse(
                json_data={"query": {"search": [{"title": "IndiGo"}]}}
            )
        return _FakeHttpResponse(
            json_data={
                "extract": "IndiGo is an Indian low-cost airline.",
                "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/IndiGo"}},
            }
        )

    monkeypatch.setattr(enrichment_summary.httpx, "get", fake_get)

    result = enrichment_summary._fetch_wikipedia_context('InterGlobe Aviation Limited ("IndiGo")')
    assert result == ("IndiGo is an Indian low-cost airline.", "https://en.wikipedia.org/wiki/IndiGo")
    assert len(calls) == 2, "must call the search API first, then the summary API for the resolved title"


def test_wikipedia_context_returns_none_when_search_has_no_results(monkeypatch):
    monkeypatch.setattr(enrichment_summary.settings, "environment", "development")
    monkeypatch.setattr(
        enrichment_summary.httpx,
        "get",
        lambda *a, **k: _FakeHttpResponse(json_data={"query": {"search": []}}),
    )
    assert enrichment_summary._fetch_wikipedia_context("Some Obscure Pvt Ltd") is None


def test_wikipedia_context_is_never_fetched_in_test_environment():
    """Belt-and-suspenders: even without any monkeypatching, calling this
    directly in the suite's own ENVIRONMENT=test must short-circuit before
    any network call is attempted."""
    assert enrichment_summary._fetch_wikipedia_context("Any Company") is None


def test_generate_summary_cites_wikipedia_inline_when_no_signals_found(monkeypatch, db_session):
    """The exact bug report this feature addresses: a well-known company
    with zero signal-source matches (enrichment_status would be
    'not_found') must still get a real, cited summary when Wikipedia has a
    match — 'No public data found' must not be the only thing shown for a
    company like this."""
    company_id = _create_pending_company(db_session, name=_unique_company_name("Big Public Co"))
    company = db_session.get(Company, company_id)
    signals = CompanySignals(company_id=company_id)  # no signal columns populated at all

    monkeypatch.setattr(
        enrichment_summary, "_fetch_wikipedia_context",
        lambda name: ("Big Public Co is a well-known multinational.", "https://en.wikipedia.org/wiki/Big_Public_Co"),
    )
    _patch_summary_client(monkeypatch, text="Big Public Co is a well-known multinational (Source: Wikipedia).")

    summary = enrichment_summary.generate_summary(company, signals)
    assert summary == "Big Public Co is a well-known multinational (Source: Wikipedia)."


def test_generate_summary_falls_back_to_wikipedia_citation_when_claude_call_fails(monkeypatch, db_session):
    """If the Claude call itself fails, the deterministic fallback must
    still surface the Wikipedia extract with its citation — never silently
    drop back to 'No public data found' when Wikipedia actually had data."""
    company_id = _create_pending_company(db_session, name=_unique_company_name("Fallback Wiki Co"))
    company = db_session.get(Company, company_id)
    signals = CompanySignals(company_id=company_id)

    monkeypatch.setattr(
        enrichment_summary, "_fetch_wikipedia_context",
        lambda name: ("Fallback Wiki Co makes widgets.", "https://en.wikipedia.org/wiki/Fallback_Wiki_Co"),
    )

    class _RaisingMessages:
        def create(self, **kwargs):
            raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))

    class _RaisingAnthropicClient:
        def __init__(self) -> None:
            self.messages = _RaisingMessages()

    monkeypatch.setattr(enrichment_summary, "_get_client", lambda: _RaisingAnthropicClient())

    summary = enrichment_summary.generate_summary(company, signals)
    assert "Fallback Wiki Co makes widgets." in summary
    assert "Source: Wikipedia" in summary
    assert "https://en.wikipedia.org/wiki/Fallback_Wiki_Co" in summary
