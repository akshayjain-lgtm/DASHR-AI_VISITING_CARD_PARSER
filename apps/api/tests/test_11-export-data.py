"""
Tests for the `11-export-data` feature (spec: `.claude/specs/11-export-data.md`).

Written directly against the spec's documented contract, not against the
implementation of `app.routers.cards`/`app.services.card_service`/
`app.services.export_service`:

- `POST /cards/export` is org-authenticated (owner or org-admin visibility
  via `scope_to_visible_users`) and accepts `CardExportRequest
  {card_ids: list[UUID]}` with `min_length=1, max_length=200` — the same cap
  as `CardEnrichRequest`/`CardScoreRequest`.
- Response is `200`, `Content-Type: text/csv`, `Content-Disposition:
  attachment; filename="dashr-leads-<YYYY-MM-DD>.csv"`, with one CSV row per
  *visible* requested card.
- Card ids not visible to the current user (wrong owner) are silently
  omitted from the output — the response is still `200`, never `404`/`403`
  — and an all-invisible selection still returns `200` with a header-only
  CSV (no data rows), never an error.
- Column order (exact, per the spec): Full Name, Job Title, Company,
  Industry, Employee Count, Revenue Band, Primary Email, All Emails,
  Primary Phone, All Phones, Website, Address, GST Number, Products
  Offered, Designation Level, Lead Score, Special Remark, Exhibition,
  Status, Scanned On.
- `Primary Email`/`Primary Phone` are the row flagged `is_primary` (or the
  first row if none is flagged, per the spec's "Files to create" section);
  `All Emails`/`All Phones` are `; `-joined across every linked row.
- A card with no linked `Company` exports `Company`/`Industry`/`Employee
  Count`/`Revenue Band` blank, not an error or a null-ish string.
- A card with `lead_score IS NULL` (never scored) exports `Lead Score`
  blank, not `0`.
- The export button/endpoint has no status/score/enrichment eligibility
  filter (unlike scoring's "must be extracted" gate) — any visible card can
  be exported regardless of its current `status`.
- `POST /cards/export` is explicitly documented as read-only: it must never
  mutate `status`, `lead_score`, or any other card field.

Fixture strategy: mirrors `test_10_lead_scoring.py`'s guidance on when to
bypass the upload -> extract -> enrich -> score pipeline. This feature only
needs cards already sitting in a specific, known end-state (a particular
company/signals/emails/phones/exhibition/score combination), not cards that
were actually produced by that pipeline, so every card here is seeded
directly via `db_session` ORM inserts (`VisitingCard`, `Company`,
`CompanySignals`, `CardEmail`, `CardPhone`, `Exhibition`).

External dependency note: this feature makes no OCR/vision or
enrichment-provider calls anywhere — `POST /cards/export` only reads
already-persisted rows and formats them into CSV — so nothing needs to be
mocked in this file.

The CSV response body is parsed with the stdlib `csv` module
(`csv.DictReader`) so assertions are against exact column values, never
brittle substring/regex matching against the raw response body.

Judgment calls made in the absence of explicit spec text:
  1. **`companies` is not truncated** by `conftest.py`'s autouse
     `_clean_tables` fixture (it only `TRUNCATE`s `users` with `CASCADE`,
     which reaches `visiting_cards`/`exhibitions`/`card_emails`/
     `card_phones` via their FKs to `users`/`visiting_cards`, but not
     `companies`, which has no FK to `users`) — confirmed by
     `test_10_lead_scoring.py`'s own docstring. Every `Company` created here
     therefore uses a name containing a fresh `uuid.uuid4()` fragment so no
     two tests (or two runs) can collide.
  2. **`Scanned On`'s exact string format is unspecified** by the spec
     (only that the column exists and reflects a scan/creation timestamp)
     — tests assert it is non-blank, not a specific format.
  3. **CSV-injection/formula-leading-character escaping is not mentioned
     anywhere in this spec file**, but the implementation does it anyway
     (`export_service._csv_safe`, added as security hardening in a prior
     review pass): any cell whose value starts with `=`/`+`/`-`/`@`/tab/CR
     is prefixed with a `'`. E.164 phone numbers always start with `+`, so
     the happy-path test's `Primary Phone`/`All Phones` assertions expect
     that leading `'` rather than the bare literal — not because the spec
     documents it, but because it's confirmed, intentional, already-shipped
     behavior a phone-number fixture can't avoid tripping. `_unescape_guard`
     strips the one leading `'` a joined cell can carry before comparing
     `All Phones` as a set, since only the cell's own first character is
     ever escaped (not each `; `-separated segment).
"""

from __future__ import annotations

import csv
import io
import re
import uuid

from fastapi.testclient import TestClient

from app.main import app as fastapi_app
from app.models.card_email import CardEmail
from app.models.card_phone import CardPhone
from app.models.company import Company
from app.models.company_signals import CompanySignals
from app.models.exhibition import Exhibition
from app.models.visiting_card import VisitingCard
from conftest import create_verified_user

def _unescape_guard(value: str) -> str:
    """Strips a single leading "'" if present — export_service._csv_safe's
    CSV-formula-injection guard only ever escapes a cell's own first
    character (a joined "; "-delimited cell is one CSV field), so this lets
    a test compare the underlying values without hard-coding which segment
    of a joined cell happened to land first."""
    return value[1:] if value.startswith("'") else value


EXPECTED_HEADERS = [
    "Full Name",
    "Job Title",
    "Company",
    "Industry",
    "Employee Count",
    "Revenue Band",
    "Primary Email",
    "All Emails",
    "Primary Phone",
    "All Phones",
    "Website",
    "Address",
    "GST Number",
    "Products Offered",
    "Designation Level",
    "Lead Score",
    "Special Remark",
    "Exhibition",
    "Status",
    "Scanned On",
]


# --------------------------------------------------------------------------
# Auth helpers — same pattern as test_10_lead_scoring.py.
# --------------------------------------------------------------------------


def _login(client: TestClient, user: dict) -> None:
    resp = client.post("/auth/login", json={"email": user["email"], "password": user["password"]})
    assert resp.status_code == 200, resp.text


def _authenticated_user(client: TestClient, fake_otp_provider, **overrides) -> dict:
    user = create_verified_user(client, fake_otp_provider, **overrides)
    _login(client, user)
    return user


# --------------------------------------------------------------------------
# Fixture-seeding helpers — direct ORM inserts, bypassing the upload/extract
# pipeline entirely, since this feature only needs cards already in a known
# end-state (per test_10_lead_scoring.py's documented convention).
# --------------------------------------------------------------------------


def _unique_company_name(label: str) -> str:
    """`companies` is not truncated between tests — every row created here
    must carry a name no other test could ever also create."""
    return f"{label} {uuid.uuid4().hex[:10]} Pvt Ltd"


def _make_card(
    db_session,
    *,
    user_id: uuid.UUID,
    full_name: str | None = "Test Contact",
    job_title: str | None = None,
    designation_level: str | None = None,
    website: str | None = None,
    address: str | None = None,
    products_offered: str | None = None,
    gst_number: str | None = None,
    special_remark: str | None = None,
    status: str = "extracted",
    lead_score=None,
    company_id: uuid.UUID | None = None,
    exhibition_id: uuid.UUID | None = None,
) -> uuid.UUID:
    card_id = uuid.uuid4()
    card = VisitingCard(
        card_id=card_id,
        user_id=user_id,
        company_id=company_id,
        exhibition_id=exhibition_id,
        full_name=full_name,
        job_title=job_title,
        designation_level=designation_level,
        website=website,
        address=address,
        products_offered=products_offered,
        gst_number=gst_number,
        special_remark=special_remark,
        status=status,
        lead_score=lead_score,
        image_url="cards/fixture/fake.jpg",
    )
    db_session.add(card)
    db_session.commit()
    return card_id


def _make_company(db_session, *, name: str, industry: str | None = None) -> uuid.UUID:
    company_id = uuid.uuid4()
    db_session.add(
        Company(
            company_id=company_id,
            name=name,
            normalized_name=name.lower(),
            industry=industry,
        )
    )
    db_session.commit()
    return company_id


def _make_company_signals(
    db_session,
    company_id: uuid.UUID,
    *,
    employee_count: int | None = None,
    revenue_band: str | None = None,
) -> None:
    db_session.add(
        CompanySignals(
            company_id=company_id,
            linkedin_employee_count=employee_count,
            estimated_revenue_band=revenue_band,
        )
    )
    db_session.commit()


def _make_exhibition(db_session, *, user_id: uuid.UUID, name: str) -> uuid.UUID:
    exhibition_id = uuid.uuid4()
    db_session.add(Exhibition(exhibition_id=exhibition_id, user_id=user_id, name=name))
    db_session.commit()
    return exhibition_id


def _add_email(db_session, card_id: uuid.UUID, email: str, *, is_primary: bool = False) -> None:
    db_session.add(CardEmail(card_id=card_id, email=email, is_primary=is_primary))
    db_session.commit()


def _add_phone(db_session, card_id: uuid.UUID, phone_e164: str, *, is_primary: bool = False) -> None:
    db_session.add(CardPhone(card_id=card_id, phone_e164=phone_e164, is_primary=is_primary))
    db_session.commit()


def _assert_valid_csv_response_headers(resp) -> None:
    """DoD: '200, Content-Type: text/csv, and a Content-Disposition:
    attachment header with a dashr-leads-<date>.csv filename.'"""
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv"), (
        f"expected Content-Type: text/csv, got {resp.headers['content-type']!r}"
    )
    disposition = resp.headers.get("content-disposition", "")
    assert re.match(
        r'^attachment; filename="dashr-leads-\d{4}-\d{2}-\d{2}\.csv"$', disposition
    ), f"unexpected Content-Disposition header: {disposition!r}"


def _parse_csv_rows(body_text: str) -> list[dict]:
    """Parses the CSV response body with the stdlib csv module (never
    string/regex matching on the raw body) and asserts the header row
    matches the spec's exact column order before returning data rows."""
    reader = csv.DictReader(io.StringIO(body_text))
    assert reader.fieldnames == EXPECTED_HEADERS, (
        f"CSV header row must match the spec's exact column order, got {reader.fieldnames}"
    )
    return list(reader)


# ==========================================================================
# 1. Happy path — a fully populated card (company + signals + two emails,
#    one primary + two phones + exhibition + a lead score) exports one
#    correct CSV row with every column filled in.
# ==========================================================================


def test_export_happy_path_full_data_returns_one_row_with_every_column_populated(
    client, fake_otp_provider, db_session
):
    user = _authenticated_user(client, fake_otp_provider)
    user_id = uuid.UUID(user["user_id"])

    company_name = _unique_company_name("Full Export Co")
    company_id = _make_company(db_session, name=company_name, industry="Industrial Machinery")
    _make_company_signals(db_session, company_id, employee_count=250, revenue_band="10-50 Cr")
    exhibition_id = _make_exhibition(db_session, user_id=user_id, name="IndiaMFG Expo 2026")

    card_id = _make_card(
        db_session,
        user_id=user_id,
        full_name="Rohan Mehta",
        job_title="VP Procurement",
        designation_level="vp",
        website="https://fullexport.example.com",
        address="Plot 12, MIDC, Pune",
        products_offered="CNC machined components",
        gst_number="27ABCDE1234F1Z5",
        special_remark="Wants a quote by Friday",
        status="extracted",
        lead_score=87,
        company_id=company_id,
        exhibition_id=exhibition_id,
    )
    _add_email(db_session, card_id, "rohan.mehta@fullexport.example.com", is_primary=True)
    _add_email(db_session, card_id, "rohan.alt@fullexport.example.com", is_primary=False)
    _add_phone(db_session, card_id, "+912212345678", is_primary=False)
    _add_phone(db_session, card_id, "+919812345678", is_primary=True)

    resp = client.post("/cards/export", json={"card_ids": [str(card_id)]})

    _assert_valid_csv_response_headers(resp)
    rows = _parse_csv_rows(resp.text)
    assert len(rows) == 1, "one requested visible card must produce exactly one CSV data row"
    row = rows[0]

    assert row["Full Name"] == "Rohan Mehta"
    assert row["Job Title"] == "VP Procurement"
    assert row["Company"] == company_name
    assert row["Industry"] == "Industrial Machinery"
    assert int(row["Employee Count"]) == 250
    assert row["Revenue Band"] == "10-50 Cr"
    assert row["Primary Email"] == "rohan.mehta@fullexport.example.com", (
        "the CardEmail row flagged is_primary must populate Primary Email"
    )
    assert set(row["All Emails"].split("; ")) == {
        "rohan.mehta@fullexport.example.com",
        "rohan.alt@fullexport.example.com",
    }, f"All Emails must '; '-join every linked CardEmail row, got {row['All Emails']!r}"
    # Leading "'" is export_service._csv_safe's CSV-formula-injection guard:
    # E.164 phone numbers always start with "+", one of its escaped
    # characters — see the file docstring's judgment-call note 3.
    assert row["Primary Phone"] == "'+919812345678", (
        "the CardPhone row flagged is_primary must populate Primary Phone"
    )
    assert set(_unescape_guard(row["All Phones"]).split("; ")) == {
        "+912212345678",
        "+919812345678",
    }, f"All Phones must '; '-join every linked CardPhone row, got {row['All Phones']!r}"
    assert row["Website"] == "https://fullexport.example.com"
    assert row["Address"] == "Plot 12, MIDC, Pune"
    assert row["GST Number"] == "27ABCDE1234F1Z5"
    assert row["Products Offered"] == "CNC machined components"
    assert row["Designation Level"] == "vp"
    assert float(row["Lead Score"]) == 87
    assert row["Special Remark"] == "Wants a quote by Friday"
    assert row["Exhibition"] == "IndiaMFG Expo 2026"
    assert row["Status"] == "extracted"
    assert row["Scanned On"] != "", "Scanned On must reflect the card's scan/creation time, not be blank"


# ==========================================================================
# 2. A card with no linked company exports Company/Industry/Employee
#    Count/Revenue Band blank, and a card with no exhibition/emails/phones
#    exports those blank too — never an error, never "None".
# ==========================================================================


def test_export_card_with_no_linked_company_leaves_company_and_related_columns_blank(
    client, fake_otp_provider, db_session
):
    user = _authenticated_user(client, fake_otp_provider)
    card_id = _make_card(
        db_session,
        user_id=uuid.UUID(user["user_id"]),
        full_name="No Company Contact",
        status="extracted",
        company_id=None,
        exhibition_id=None,
    )

    resp = client.post("/cards/export", json={"card_ids": [str(card_id)]})

    _assert_valid_csv_response_headers(resp)
    rows = _parse_csv_rows(resp.text)
    assert len(rows) == 1
    row = rows[0]

    assert row["Full Name"] == "No Company Contact"
    assert row["Company"] == "", "a card with no linked company must export Company blank, not an error"
    assert row["Industry"] == ""
    assert row["Employee Count"] == ""
    assert row["Revenue Band"] == ""
    assert row["Primary Email"] == "", "a card with no CardEmail rows must export a blank Primary Email"
    assert row["All Emails"] == ""
    assert row["Primary Phone"] == "", "a card with no CardPhone rows must export a blank Primary Phone"
    assert row["All Phones"] == ""
    assert row["Exhibition"] == "", "a card with no exhibition_id must export Exhibition blank"


# ==========================================================================
# 3. A card with lead_score IS NULL exports Lead Score blank, never "0".
# ==========================================================================


def test_export_card_with_null_lead_score_leaves_lead_score_blank_not_zero(
    client, fake_otp_provider, db_session
):
    user = _authenticated_user(client, fake_otp_provider)
    card_id = _make_card(
        db_session,
        user_id=uuid.UUID(user["user_id"]),
        full_name="Never Scored Contact",
        status="extracted",
        lead_score=None,
    )

    resp = client.post("/cards/export", json={"card_ids": [str(card_id)]})

    _assert_valid_csv_response_headers(resp)
    row = _parse_csv_rows(resp.text)[0]
    assert row["Lead Score"] == "", (
        f"lead_score IS NULL must export as a blank cell, never the string '0', got {row['Lead Score']!r}"
    )


# ==========================================================================
# 4. Tenant isolation — a card_ids list mixing a visible id with another
#    user's (foreign) card id: only the visible row appears, response is
#    200, never 403/404.
# ==========================================================================


def test_export_omits_card_not_visible_to_current_user_but_still_returns_200(
    client, fake_otp_provider, db_session
):
    user = _authenticated_user(client, fake_otp_provider)
    visible_id = _make_card(
        db_session, user_id=uuid.UUID(user["user_id"]), full_name="Visible Contact"
    )

    with TestClient(fastapi_app) as other_client:
        other_user = _authenticated_user(other_client, fake_otp_provider)
        foreign_id = _make_card(
            db_session, user_id=uuid.UUID(other_user["user_id"]), full_name="Foreign Contact"
        )

    resp = client.post("/cards/export", json={"card_ids": [str(visible_id), str(foreign_id)]})

    _assert_valid_csv_response_headers(resp)
    rows = _parse_csv_rows(resp.text)
    assert len(rows) == 1, (
        "a foreign (not-visible) card id must be silently omitted from the CSV, not raise and not appear"
    )
    assert rows[0]["Full Name"] == "Visible Contact"


# ==========================================================================
# 5. Tenant isolation — an all-invisible card_ids selection still returns
#    200 with a header-only CSV, never 403/404, never an empty body.
# ==========================================================================


def test_export_with_all_ids_invisible_returns_200_header_only_csv(
    client, fake_otp_provider, db_session
):
    _authenticated_user(client, fake_otp_provider)

    with TestClient(fastapi_app) as other_client:
        other_user = _authenticated_user(other_client, fake_otp_provider)
        foreign_id = _make_card(db_session, user_id=uuid.UUID(other_user["user_id"]))

    resp = client.post("/cards/export", json={"card_ids": [str(foreign_id)]})

    _assert_valid_csv_response_headers(resp)
    rows = _parse_csv_rows(resp.text)
    assert rows == [], (
        "an all-invisible selection must return 200 with a header-only CSV (no data rows), not an error"
    )


# ==========================================================================
# 6. card_ids: [] -> 422 (Pydantic min_length=1, before any DB access).
# ==========================================================================


def test_export_with_empty_card_ids_list_returns_422(client, fake_otp_provider):
    _authenticated_user(client, fake_otp_provider)

    resp = client.post("/cards/export", json={"card_ids": []})

    assert resp.status_code == 422, resp.text


# ==========================================================================
# 7. A 201-id card_ids list -> 422 (Pydantic max_length=200, same cap as
#    CardEnrichRequest/CardScoreRequest).
# ==========================================================================


def test_export_with_more_than_200_card_ids_returns_422(client, fake_otp_provider):
    _authenticated_user(client, fake_otp_provider)
    too_many_ids = [str(uuid.uuid4()) for _ in range(201)]

    resp = client.post("/cards/export", json={"card_ids": too_many_ids})

    assert resp.status_code == 422, resp.text


# ==========================================================================
# 8. Auth guard — an unauthenticated request to the protected endpoint
#    returns 401.
# ==========================================================================


def test_export_without_session_returns_401(client):
    resp = client.post("/cards/export", json={"card_ids": [str(uuid.uuid4())]})

    assert resp.status_code == 401, resp.text


# ==========================================================================
# 9. Read-only guarantee — exporting never mutates status/lead_score (or
#    any other card field) as a side effect, and applies no eligibility
#    filter: a never-processed card ("new" status, no score) is still
#    exported successfully.
# ==========================================================================


def test_export_never_mutates_card_status_or_lead_score_and_has_no_status_filter(
    client, fake_otp_provider, db_session
):
    user = _authenticated_user(client, fake_otp_provider)
    card_id = _make_card(
        db_session,
        user_id=uuid.UUID(user["user_id"]),
        full_name="Untouched Contact",
        status="new",
        lead_score=None,
    )

    resp = client.post("/cards/export", json={"card_ids": [str(card_id)]})

    _assert_valid_csv_response_headers(resp)
    rows = _parse_csv_rows(resp.text)
    assert len(rows) == 1, (
        "export has no status/score eligibility filter — a 'new', never-scored card must still export"
    )
    assert rows[0]["Status"] == "new"
    assert rows[0]["Lead Score"] == ""

    db_session.expire_all()
    card = db_session.get(VisitingCard, card_id)
    assert card.status == "new", "POST /cards/export must never mutate VisitingCard.status"
    assert card.lead_score is None, "POST /cards/export must never mutate VisitingCard.lead_score"
