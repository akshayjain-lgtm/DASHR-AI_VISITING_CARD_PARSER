"""
Tests for the `19-data-enrichment-indiamart` feature (spec:
`.claude/specs/19-data-enrichment-indiamart.md`).

This is a focused follow-up to `07-data-enrichment`'s local-presence signal
(`test_07_data_enrichment.py` already covers a large share of this feature's
contract, and `test_local_presence_provider.py` already unit-tests every
free-text parser/URL-ranking helper in isolation). This file adds the
specific coverage the task asked for that isn't already exercised elsewhere,
without re-deriving assertions from the implementation:

1. `GET /cards/{card_id}` exposes every one of the 13 IndiaMART-specific
   `CardCompanyOut` fields the spec's "API endpoints" section lists by name
   — null before enrichment, correctly populated after.
2. Tenant isolation on that same endpoint, specifically for the IndiaMART
   fields (not just the six `07-data-enrichment` headline fields
   `test_07_data_enrichment.py` already checks).
3. Lookup #12 (`indiamart_supplier_profile`) gating on lookup #11 having
   found a `catalog_url`, including the "billed but empty" case (zero
   dataset rows still writes a `CompanyEnrichment` audit row) — the one DoD
   line `test_07_data_enrichment.py` doesn't already cover directly.
4. `marketplace_vintage_years`'s derivation, including the `0` boundary
   (a brand-new supplier joining this same year) — `0` is falsy in Python,
   so a naive "was it set" check could wrongly treat it as null.
5. The documented `company_id`-only scoping (no `org_id`) on
   `company_signals`/`company_enrichment` — a standing structural
   assertion, not a behavioral one.
6. Four validation/edge-case rules from the spec's "Rules for
   implementation", each exercised **end-to-end through the real
   `ApifyLocalPresenceProvider` class** (only its `httpx.post` boundary
   mocked, via `get_local_presence_provider` monkeypatched to return a real
   instance) rather than calling its methods directly the way
   `test_local_presence_provider.py` does — proving the whole pipeline
   (worker -> `enrichment_service` -> the real provider -> `company_signals`)
   wires these rules together correctly, not just that the helper functions
   are individually correct in isolation:
     a. `_parse_member_since_year` on a direct 4-digit year.
     b. `_parse_member_since_year` on a duration ("N yrs") format, together
        with `_validate_gstin` rejecting a badge-label `gstNumber`.
     c. `_pick_one_product`'s generic-business-type-word filter, as it
        actually flows from a card's own `products_offered` field through
        `enrich_company_task`'s Apify query construction.
     d. The city/address disambiguation tie-break, as it actually flows
        from a card's own `address` field through the same path.

Mocking strategy (matching this codebase's established convention):
- `get_local_presence_provider()` factory monkeypatched to a small static
  fake (mirroring `test_07_data_enrichment.py`'s `_StaticLocalPresenceProvider`)
  for tests 1-4, where only the *result shape* matters.
- `get_local_presence_provider()` monkeypatched instead to return a REAL
  `ApifyLocalPresenceProvider()` instance for tests 6a-6d, with only
  `local_presence_provider.httpx.post` mocked — never a real network call,
  same environment-gating guarantee (`ENVIRONMENT=test`) as every other
  provider in this codebase, just exercised at a different boundary.
- The Anthropic text-completion call inside `enrichment_summary.
  generate_summary` is mocked via the same `_get_client()` seam
  `test_07_data_enrichment.py` already establishes.
- Vision extraction is mocked via `app.services.vision_client.
  extract_card_fields`, matching `test_05_parsing_visiting_card.py`'s
  convention.

Judgment calls made in the absence of explicit spec text:
  1. **Distinct company names per test.** `companies` is not truncated by
     `conftest.py`'s autouse `_clean_tables` fixture, so every directly-
     created `Company` row uses a name containing a fresh `uuid.uuid4()`
     fragment, per `test_07_data_enrichment.py`'s established convention.
  2. **Real-provider end-to-end tests (6a-6d) use a single alphanumeric
     "company name" token** (no spaces) wherever the test doesn't itself
     need `_looks_like_same_company_by_url`'s multi-word significant-word
     matching to hold across a specific incident scenario — this makes the
     relevance check trivially satisfiable without depending on exactly how
     many of several words must match, keeping these tests robust to that
     internal detail while still proving the real provider class (not a
     fake stand-in) is what's wired through `enrich_company_task`. The one
     exception is the city-disambiguation test (6d), which deliberately
     reuses the exact "AGGARWAL ENTERPRISES"/Kanpur-vs-Delhi incident
     `test_local_presence_provider.py` already established, since that's
     the specific scenario the spec documents.
"""

from __future__ import annotations

import io
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app.main import app as fastapi_app
from app.models.company import Company
from app.models.company_enrichment import CompanyEnrichment
from app.models.company_signals import CompanySignals
from app.models.visiting_card import VisitingCard
from app.services.enrichment_providers.local_presence_provider import (
    ApifyLocalPresenceProvider,
    MarketplaceResult,
    PlacesResult,
    SupplierProfileResult,
)
from app.workers.card_processing import process_card
from app.workers.enrichment_processing import enrich_company_task
from conftest import create_verified_user

# --------------------------------------------------------------------------
# The exact 13 IndiaMART-specific fields the spec's "API endpoints" section
# lists on `CardCompanyOut`, in the order given there.
# --------------------------------------------------------------------------

INDIAMART_CARD_COMPANY_FIELDS = [
    "catalog_url",
    "marketplace_verified_badge",
    "marketplace_vintage_years",
    "indiamart_rating",
    "indiamart_rating_count",
    "indiamart_member_since_year",
    "indiamart_business_type",
    "indiamart_employee_count_band",
    "indiamart_annual_turnover_band",
    "indiamart_year_established",
    "indiamart_gst_number",
    "indiamart_gst_registration_year",
    "indiamart_call_response_rate",
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
    name = name or _unique_company_name("IndiaMART Target")
    company = Company(name=name, normalized_name=name.strip().lower(), website=website)
    db_session.add(company)
    db_session.commit()
    db_session.refresh(company)
    assert company.enrichment_status == "pending", "fixture setup: a fresh company must start pending"
    return company.company_id


# --------------------------------------------------------------------------
# Anthropic text-completion mocking for enrichment_summary.generate_summary
# — the only LLM boundary this feature's task calls. Never a real network call.
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

    def create(self, **kwargs):
        return _FakeAnthropicResponse(self._text)


class _FakeAnthropicClient:
    def __init__(self, text: str) -> None:
        self.messages = _FakeAnthropicMessages(text)


def _patch_summary_client(monkeypatch: pytest.MonkeyPatch, text: str = "Fixed test summary text.") -> None:
    monkeypatch.setattr(
        "app.services.enrichment_summary._get_client", lambda: _FakeAnthropicClient(text)
    )


# --------------------------------------------------------------------------
# Static local-presence-provider fake — used for tests 1-4, where only the
# result *shape* matters (mirrors test_07_data_enrichment.py's
# _StaticLocalPresenceProvider, written independently here to keep this file
# self-contained).
# --------------------------------------------------------------------------


class _StaticLocalPresenceProvider:
    def __init__(
        self,
        marketplace_result: MarketplaceResult,
        supplier_profile_result: SupplierProfileResult | None = None,
        expect_supplier_profile_call: bool = True,
    ) -> None:
        self._marketplace_result = marketplace_result
        self._supplier_profile_result = supplier_profile_result
        self._expect_supplier_profile_call = expect_supplier_profile_call

    def lookup_places(self, company_name: str, address: str | None) -> PlacesResult:
        return PlacesResult()

    def lookup_marketplace(
        self,
        company_name: str,
        email_domain: str | None = None,
        website: str | None = None,
        products_offered: str | None = None,
        address: str | None = None,
    ) -> MarketplaceResult:
        return self._marketplace_result

    def lookup_supplier_profile(self, catalog_url: str) -> SupplierProfileResult:
        if not self._expect_supplier_profile_call:
            raise AssertionError(
                "lookup_supplier_profile must never be called when lookup_marketplace found no catalog_url "
                "in this same run"
            )
        return self._supplier_profile_result


def _patch_local_presence_provider(monkeypatch: pytest.MonkeyPatch, provider) -> None:
    monkeypatch.setattr(
        "app.services.enrichment_providers.local_presence_provider.get_local_presence_provider",
        lambda: provider,
    )


# --------------------------------------------------------------------------
# Fake Apify HTTP response, for the "real provider, mocked httpx.post only"
# tests (6a-6d).
# --------------------------------------------------------------------------


class _FakeApifyResponse:
    def __init__(self, payload) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self):
        return self._payload


# --------------------------------------------------------------------------
# Vision-model mocking + upload helpers, matching test_05/test_07's
# established convention.
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


def _upload_one(client: TestClient, jpeg_bytes: bytes, filename: str = "card.jpg") -> str:
    resp = client.post(
        "/cards/bulk-upload",
        data={},
        files=[("files", (filename, jpeg_bytes, "image/jpeg"))],
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["cards"][0]["card_id"]


def _patch_vision(monkeypatch: pytest.MonkeyPatch, response: dict) -> None:
    monkeypatch.setattr("app.services.vision_client.extract_card_fields", lambda image_bytes, media_type: response)


def _fields(
    *,
    full_name: str | None = "Extracted Contact",
    company_name: str | None = None,
    website: str | None = None,
    address: str | None = None,
    products_offered: str | None = None,
    gst_number: str | None = None,
) -> dict:
    return {
        "is_back_of_card": False,
        "full_name": full_name,
        "job_title": None,
        "company_name": company_name,
        "website": website,
        "address": address,
        "products_offered": products_offered,
        "special_remark": None,
        "raw_ocr_text": "verbatim card text",
        "emails": [],
        "phones": [],
        "gst_number": gst_number,
    }


# ==========================================================================
# 1. GET /cards/{card_id} exposes every spec-listed IndiaMART field, null
#    until enrichment has run and found something.
# ==========================================================================


def test_get_card_detail_all_spec_listed_indiamart_fields_are_null_before_enrichment(
    client, fake_otp_provider, jpeg_bytes, monkeypatch
):
    """Spec: 'All null until enrichment has run and found something.' A card
    whose company is still "pending" (no enrichment ever run) must expose
    every one of the 13 IndiaMART fields as null, not an error or a missing
    key."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    _patch_vision(
        monkeypatch,
        _fields(company_name=_unique_company_name("Pending IndiaMart Co")),
    )
    process_card(card_id)

    resp = client.get(f"/cards/{card_id}")
    assert resp.status_code == 200, resp.text
    company_out = resp.json()["company"]
    assert company_out["enrichment_status"] == "pending"
    for field in INDIAMART_CARD_COMPANY_FIELDS:
        assert field in company_out, f"CardCompanyOut is missing the spec-listed field {field!r}"
        assert company_out[field] is None, (
            f"{field} must be null before enrichment has ever run, got {company_out[field]!r}"
        )


def test_get_card_detail_returns_all_spec_listed_indiamart_fields_once_enrichment_finds_them(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """Converse of the above: once both lookup #11 and lookup #12 find real
    data, every one of the 13 spec-listed fields must round-trip through
    `GET /cards/{card_id}` with the exact values the providers returned."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    company_name = _unique_company_name("Populated IndiaMart Co")
    _patch_vision(monkeypatch, _fields(company_name=company_name))
    process_card(card_id)

    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    company_id = card.company_id
    assert company_id is not None, "fixture setup: extraction must have linked a company"

    _patch_summary_client(monkeypatch)
    catalog_url = "https://www.indiamart.com/populated-indiamart-co/"
    member_since_year = datetime.now(timezone.utc).year - 4
    _patch_local_presence_provider(
        monkeypatch,
        _StaticLocalPresenceProvider(
            marketplace_result=MarketplaceResult(
                catalog_url=catalog_url, source_tag="indiamart", raw_payload={"found": True}
            ),
            supplier_profile_result=SupplierProfileResult(
                marketplace_verified_badge=True,
                indiamart_rating=4.7,
                indiamart_rating_count=210,
                indiamart_member_since_year=member_since_year,
                indiamart_business_type="Trader",
                indiamart_employee_count_band="26 to 50 People",
                indiamart_annual_turnover_band="5 - 10 Cr",
                indiamart_year_established="2005",
                indiamart_gst_number="29AAAAA1111A1Z5",
                indiamart_gst_registration_year=2016,
                indiamart_call_response_rate="91%",
                source_tag="indiamart_supplier_profile",
                raw_payload={"items": [{"companyName": company_name}]},
            ),
        ),
    )

    enrich_company_task(str(company_id))

    resp = client.get(f"/cards/{card_id}")
    assert resp.status_code == 200, resp.text
    company_out = resp.json()["company"]
    assert company_out["catalog_url"] == catalog_url
    assert company_out["marketplace_verified_badge"] is True
    assert company_out["marketplace_vintage_years"] == 4, (
        "must be derived as current_year - indiamart_member_since_year"
    )
    assert company_out["indiamart_rating"] == 4.7
    assert company_out["indiamart_rating_count"] == 210
    assert company_out["indiamart_member_since_year"] == member_since_year
    assert company_out["indiamart_business_type"] == "Trader"
    assert company_out["indiamart_employee_count_band"] == "26 to 50 People"
    assert company_out["indiamart_annual_turnover_band"] == "5 - 10 Cr"
    assert company_out["indiamart_year_established"] == "2005"
    assert company_out["indiamart_gst_number"] == "29AAAAA1111A1Z5"
    assert company_out["indiamart_gst_registration_year"] == 2016
    assert company_out["indiamart_call_response_rate"] == "91%"


# ==========================================================================
# 2. Tenant isolation — GET /cards/{card_id} for the IndiaMART fields
#    specifically (company_signals itself is intentionally NOT org-scoped,
#    per the spec's documented shared-cache exception, but the *card* they
#    hang off must still be).
# ==========================================================================


def test_get_card_detail_indiamart_fields_do_not_leak_to_a_different_org(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """A user in a different org must get a plain 404 for someone else's
    card — never a leaked (even partial) catalog_url/indiamart_* payload."""
    _authenticated_user(client, fake_otp_provider)

    with TestClient(fastapi_app) as other_client:
        _authenticated_user(other_client, fake_otp_provider)
        their_card_id = _upload_one(other_client, jpeg_bytes)
        company_name = _unique_company_name("Other Org IndiaMart Co")
        _patch_vision(monkeypatch, _fields(company_name=company_name))
        process_card(their_card_id)

        their_card = db_session.get(VisitingCard, uuid.UUID(their_card_id))
        company_id = their_card.company_id
        assert company_id is not None, "fixture setup: extraction must have linked a company"

        _patch_summary_client(monkeypatch)
        catalog_url = "https://www.indiamart.com/other-org-indiamart-co/"
        _patch_local_presence_provider(
            monkeypatch,
            _StaticLocalPresenceProvider(
                marketplace_result=MarketplaceResult(
                    catalog_url=catalog_url, source_tag="indiamart", raw_payload={"found": True}
                ),
                supplier_profile_result=SupplierProfileResult(
                    indiamart_gst_number="27AAAAA0000A1Z5",
                    source_tag="indiamart_supplier_profile",
                    raw_payload={"items": [{"gstNumber": "27AAAAA0000A1Z5"}]},
                ),
            ),
        )
        enrich_company_task(str(company_id))

        # Sanity check: the owning org can see its own data.
        own_resp = other_client.get(f"/cards/{their_card_id}")
        assert own_resp.status_code == 200, own_resp.text
        assert own_resp.json()["company"]["catalog_url"] == catalog_url

        resp = client.get(f"/cards/{their_card_id}")

    assert resp.status_code == 404, (
        f"a user in a different org must never be able to fetch another org's card (nor its catalog_url/"
        f"indiamart_* fields through it), got {resp.status_code}: {resp.text}"
    )


# ==========================================================================
# 3. Lookup #12 gating on lookup #11's catalog_url, including the
#    "billed but empty" case.
# ==========================================================================


def test_supplier_profile_lookup_never_fires_when_marketplace_lookup_found_no_catalog_url(
    db_session, monkeypatch
):
    """Spec DoD: 'With no catalog_url found, lookup #12 never fires — no
    second Apify call, no indiamart_supplier_profile audit row, all its
    columns stay None.'"""
    company_id = _create_pending_company(db_session)
    _patch_summary_client(monkeypatch)
    _patch_local_presence_provider(
        monkeypatch,
        _StaticLocalPresenceProvider(
            marketplace_result=MarketplaceResult(source_tag="indiamart", raw_payload={"empty": True}),
            expect_supplier_profile_call=False,
        ),
    )

    enrich_company_task(str(company_id))  # must not raise via the AssertionError guard above

    db_session.expire_all()
    signals = db_session.get(CompanySignals, company_id)
    assert signals is not None
    assert signals.catalog_url is None
    for field in INDIAMART_CARD_COMPANY_FIELDS:
        if field == "catalog_url":
            continue
        assert getattr(signals, field) is None, f"{field} must stay null — lookup #12 must never fire"

    audit_rows = db_session.scalars(
        select(CompanyEnrichment).where(CompanyEnrichment.company_id == company_id)
    ).all()
    assert [row.source for row in audit_rows] == ["indiamart"], (
        "only lookup #11's own audit row may exist — no indiamart_supplier_profile row"
    )


def test_supplier_profile_lookup_billed_but_empty_response_still_writes_audit_row(
    db_session, monkeypatch
):
    """Spec DoD: 'A billed-but-empty lookup_supplier_profile response (zero
    dataset rows) still writes a CompanyEnrichment audit row
    (raw_payload={"items": []}), not a silently-dropped call.' Lookup #12
    DOES fire here (a catalog_url was found) but the second actor call comes
    back with nothing usable — this must still be visible in the audit
    trail, not indistinguishable from "never called"."""
    company_id = _create_pending_company(db_session)
    _patch_summary_client(monkeypatch)
    catalog_url = "https://www.indiamart.com/some-billed-empty-co/"
    _patch_local_presence_provider(
        monkeypatch,
        _StaticLocalPresenceProvider(
            marketplace_result=MarketplaceResult(
                catalog_url=catalog_url, source_tag="indiamart", raw_payload={"found": True}
            ),
            supplier_profile_result=SupplierProfileResult(
                source_tag="indiamart_supplier_profile", raw_payload={"items": []}
            ),
        ),
    )

    enrich_company_task(str(company_id))

    db_session.expire_all()
    signals = db_session.get(CompanySignals, company_id)
    assert signals is not None
    assert signals.catalog_url == catalog_url, "lookup #11 itself still succeeded"
    for field in INDIAMART_CARD_COMPANY_FIELDS:
        if field == "catalog_url":
            continue
        assert getattr(signals, field) is None, (
            f"{field} must stay null — the billed supplier-profile call came back empty"
        )

    audit_rows = db_session.scalars(
        select(CompanyEnrichment).where(CompanyEnrichment.company_id == company_id)
    ).all()
    assert sorted(row.source for row in audit_rows) == ["indiamart", "indiamart_supplier_profile"], (
        "the billed-but-empty lookup #12 call must still write its own audit row"
    )
    supplier_profile_row = next(r for r in audit_rows if r.source == "indiamart_supplier_profile")
    assert supplier_profile_row.payload == {"items": []}, (
        "the audit row's payload must be {'items': []}, never null/None, so a billed-but-empty call is "
        "never indistinguishable from a call that was never made at all"
    )


# ==========================================================================
# 4. marketplace_vintage_years derivation, including the 0-years boundary.
# ==========================================================================


def test_marketplace_vintage_years_of_zero_is_not_confused_with_null(db_session, monkeypatch):
    """A supplier who joined IndiaMART this same year has a vintage of
    exactly 0 — since 0 is falsy in Python, a naive "if vintage:" check
    could wrongly leave marketplace_vintage_years null instead of 0. Must
    surface as a real 0, not None."""
    company_id = _create_pending_company(db_session)
    _patch_summary_client(monkeypatch)
    this_year = datetime.now(timezone.utc).year
    catalog_url = "https://www.indiamart.com/brand-new-member-co/"
    _patch_local_presence_provider(
        monkeypatch,
        _StaticLocalPresenceProvider(
            marketplace_result=MarketplaceResult(
                catalog_url=catalog_url, source_tag="indiamart", raw_payload={"found": True}
            ),
            supplier_profile_result=SupplierProfileResult(
                indiamart_member_since_year=this_year,
                source_tag="indiamart_supplier_profile",
                raw_payload={"items": [{"memberSince": str(this_year)}]},
            ),
        ),
    )

    enrich_company_task(str(company_id))

    db_session.expire_all()
    signals = db_session.get(CompanySignals, company_id)
    assert signals.indiamart_member_since_year == this_year
    assert signals.marketplace_vintage_years == 0, (
        f"a same-year member must have vintage 0 (not null) — got {signals.marketplace_vintage_years!r}"
    )


# ==========================================================================
# 5. company_signals/company_enrichment are company_id-scoped only, never
#    org_id — the spec's documented shared-cache exception.
# ==========================================================================


def test_company_signals_and_company_enrichment_carry_no_org_id_column():
    """Spec: 'No org_id on any of the three migrations — company_signals is
    the shared, non-tenant-scoped cache established in 07-data-enrichment.'
    A standing structural assertion against the live ORM model, not a
    behavioral one, so a future column addition can't silently reintroduce
    org-scoping onto this deliberately-shared cache table."""
    signals_columns = set(CompanySignals.__table__.columns.keys())
    enrichment_columns = set(CompanyEnrichment.__table__.columns.keys())

    assert "org_id" not in signals_columns, "company_signals must never carry an org_id column"
    assert "org_id" not in enrichment_columns, "company_enrichment must never carry an org_id column"
    assert "company_id" in signals_columns
    assert "company_id" in enrichment_columns


# ==========================================================================
# 6. Validation/edge-case rules, exercised end-to-end through the REAL
#    ApifyLocalPresenceProvider class (only httpx.post mocked) rather than
#    calling its methods directly — proving the whole pipeline wires these
#    rules together, not just that the helper functions work in isolation.
# ==========================================================================


def test_real_provider_end_to_end_parses_direct_four_digit_member_since_year(db_session, monkeypatch):
    """`_parse_member_since_year` must correctly handle a direct 4-digit
    year (the actor's originally *declared* schema format), threaded all the
    way through enrich_company_task into company_signals — not just at the
    unit level."""
    company_name = f"aone{uuid.uuid4().hex[:10]}supplier"
    company_id = _create_pending_company(db_session, name=company_name)
    _patch_summary_client(monkeypatch)

    catalog_url = f"https://www.indiamart.com/{company_name}/profile.html"
    member_since_year = 2015

    def _fake_post(url, headers=None, json=None, timeout=None):
        if (json or {}).get("mode") == "supplierProfile":
            return _FakeApifyResponse([{"memberSince": str(member_since_year)}])
        return _FakeApifyResponse([{"organicResults": [{"url": catalog_url}]}])

    monkeypatch.setattr(
        "app.services.enrichment_providers.local_presence_provider.httpx.post", _fake_post
    )
    _patch_local_presence_provider(monkeypatch, ApifyLocalPresenceProvider())

    enrich_company_task(str(company_id))

    db_session.expire_all()
    signals = db_session.get(CompanySignals, company_id)
    assert signals.catalog_url == catalog_url
    assert signals.indiamart_member_since_year == member_since_year
    assert signals.marketplace_vintage_years == datetime.now(timezone.utc).year - member_since_year


def test_real_provider_end_to_end_parses_duration_member_since_year_and_rejects_badge_label_gstin(
    db_session, monkeypatch
):
    """Combines two Phase-3 live-verified rules in one end-to-end run: (a)
    `_parse_member_since_year` handling a tenure-duration ("N yrs") format,
    not just a bare year; (b) `_validate_gstin` rejecting a badge label
    ("TrustSEAL", the exact confirmed-live value) so it never gets stored as
    if it were a real GSTIN. Both threaded through the real provider class,
    not asserted directly against the helper functions."""
    company_name = f"bone{uuid.uuid4().hex[:10]}supplier"
    company_id = _create_pending_company(db_session, name=company_name)
    _patch_summary_client(monkeypatch)

    catalog_url = f"https://www.indiamart.com/{company_name}/profile.html"
    duration_years = 6
    gst_registration_year = 2018

    def _fake_post(url, headers=None, json=None, timeout=None):
        if (json or {}).get("mode") == "supplierProfile":
            return _FakeApifyResponse([{
                "memberSince": f"{duration_years} yrs",
                "gstNumber": "TrustSEAL",
                "gstRegistrationDate": str(gst_registration_year),
                "callResponseRate": "77%",
            }])
        return _FakeApifyResponse([{"organicResults": [{"url": catalog_url}]}])

    monkeypatch.setattr(
        "app.services.enrichment_providers.local_presence_provider.httpx.post", _fake_post
    )
    _patch_local_presence_provider(monkeypatch, ApifyLocalPresenceProvider())

    enrich_company_task(str(company_id))

    db_session.expire_all()
    signals = db_session.get(CompanySignals, company_id)
    assert signals.catalog_url == catalog_url
    expected_member_since_year = datetime.now(timezone.utc).year - duration_years
    assert signals.indiamart_member_since_year == expected_member_since_year, (
        "a tenure-duration memberSince ('6 yrs') must resolve to current_year - 6, not be silently dropped"
    )
    assert signals.marketplace_vintage_years == duration_years
    assert signals.indiamart_gst_number is None, (
        f"a badge label ('TrustSEAL') must never be stored as indiamart_gst_number, "
        f"got {signals.indiamart_gst_number!r}"
    )
    assert signals.indiamart_gst_registration_year == gst_registration_year
    assert signals.indiamart_call_response_rate == "77%"


def test_real_provider_end_to_end_threads_products_offered_through_generic_word_filter(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """`_pick_one_product` must skip a leading business-type/role word
    ("Manufacturer") and use the real product ("Toys") instead — proven here
    by inspecting the actual Apify query the real provider sent, sourced
    from a real card's own products_offered field via enrich_company_task's
    source_card_id wiring (not by calling _pick_one_product directly)."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    company_name = f"toyfilterco{uuid.uuid4().hex[:10]}"
    _patch_vision(
        monkeypatch,
        _fields(company_name=company_name, products_offered="Manufacturer, Importers, Traders of Toys"),
    )
    process_card(card_id)

    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    company_id = card.company_id
    assert company_id is not None, "fixture setup: extraction must have linked a company"

    _patch_summary_client(monkeypatch)
    captured_queries: list[str] = []

    def _fake_post(url, headers=None, json=None, timeout=None):
        if (json or {}).get("mode") == "supplierProfile":
            return _FakeApifyResponse([])
        captured_queries.append(json["queries"])
        return _FakeApifyResponse([{"organicResults": []}])  # every search step misses

    monkeypatch.setattr(
        "app.services.enrichment_providers.local_presence_provider.httpx.post", _fake_post
    )
    _patch_local_presence_provider(monkeypatch, ApifyLocalPresenceProvider())

    enrich_company_task(str(company_id), str(card_id))

    assert captured_queries, "fixture setup: at least one search query must have been sent"
    assert any("toys" in q.lower() for q in captured_queries), (
        f"the real product ('Toys') must appear in at least one Apify query, got: {captured_queries!r}"
    )
    assert not any("manufacturer" in q.lower() for q in captured_queries), (
        f"the business-type preamble word ('Manufacturer') must never be used as the product term, "
        f"got: {captured_queries!r}"
    )


def test_real_provider_end_to_end_applies_city_address_disambiguation_tie_break(
    client, fake_otp_provider, db_session, jpeg_bytes, monkeypatch
):
    """Reuses the exact "AGGARWAL ENTERPRISES" (Kanpur vs. Delhi)
    production incident `test_local_presence_provider.py` already
    established, but here proven end-to-end: the card's own `address` field
    flows through `enrich_company_task` (source_card_id) into the real
    provider's `lookup_marketplace` call and its city tie-break, not
    asserted directly against `_pick_best_indiamart_url`."""
    _authenticated_user(client, fake_otp_provider)
    card_id = _upload_one(client, jpeg_bytes)
    company_name = f"AGGARWAL ENTERPRISES {uuid.uuid4().hex[:8].upper()}"
    address = "Off.: 141, Jaynarayan Market, Sadar Bazar, Delhi-110006"
    _patch_vision(monkeypatch, _fields(company_name=company_name, address=address))
    process_card(card_id)

    card = db_session.get(VisitingCard, uuid.UUID(card_id))
    company_id = card.company_id
    assert company_id is not None, "fixture setup: extraction must have linked a company"
    assert card.address == address, "fixture setup: the card must actually carry the address we're checking for"

    _patch_summary_client(monkeypatch)
    kanpur_url = "https://www.indiamart.com/aggarwalenterpriseskanpur/profile.html"
    delhi_url = "https://www.indiamart.com/aggarwalenterprisesnewdelhi/profile.html"

    def _fake_post(url, headers=None, json=None, timeout=None):
        if (json or {}).get("mode") == "supplierProfile":
            return _FakeApifyResponse([])
        return _FakeApifyResponse([{
            "organicResults": [{"url": kanpur_url}, {"url": delhi_url}],
        }])

    monkeypatch.setattr(
        "app.services.enrichment_providers.local_presence_provider.httpx.post", _fake_post
    )
    _patch_local_presence_provider(monkeypatch, ApifyLocalPresenceProvider())

    enrich_company_task(str(company_id), str(card_id))

    db_session.expire_all()
    signals = db_session.get(CompanySignals, company_id)
    assert signals.catalog_url == delhi_url, (
        "the same-city (Delhi) candidate must be chosen over the equally name-relevant Kanpur one, "
        f"given the card's own Delhi address — got {signals.catalog_url!r}"
    )
