"""
Tests for the `11-export-data` feature (spec: `.claude/specs/11-export-data.md`).

Written directly against the spec's documented contract, not against the
implementation of `services/export_service.py`/`services/card_service.py`:

- `POST /cards/export` — org-authenticated, body `CardExportRequest
  {card_ids: list[UUID]}` with Pydantic `min_length=1, max_length=200` (same
  cap as `CardEnrichRequest`/`CardScoreRequest`). Returns `200`,
  `Content-Type: text/csv`, `Content-Disposition: attachment;
  filename="dashr-leads-<YYYY-MM-DD>.csv"`, one CSV row per *visible* card.
- Card ids not visible to the current user (wrong owner, different account)
  are silently omitted from the output — never `404`/`403` — even an
  all-invisible selection still returns `200` with a header-only CSV.
- Column order (exact): Full Name, Job Title, Company, Industry, Employee
  Count, Revenue Band, Primary Email, All Emails, Primary Phone, All Phones,
  Website, Address, GST Number, Products Offered, Designation Level, Lead
  Score, Special Remark, Exhibition, Status, Scanned On.
- `Primary Email`/`Primary Phone` come from the `is_primary` row; `All
  Emails`/`All Phones` are `; `-joined. A card with no linked company exports
  with `Company`/`Industry`/`Employee Count`/`Revenue Band` blank. A card
  with `lead_score IS NULL` exports `Lead Score` blank, not `0`.
- The endpoint is read-only: it never mutates `status`, `lead_score`, or any
  other card field as a side effect of exporting.

Fixture strategy: this feature only needs cards already in a known end-state
(a specific status/company/signals/emails/phones/exhibition combination), not
the upload -> extract -> enrich -> score pipeline that produced them — so
every card here is seeded directly via `db_session` ORM inserts
(`VisitingCard`, `Company`, `CompanySignals`, `CardEmail`, `CardPhone`,
`Exhibition`), mirroring `test_10_lead_scoring.py`'s guidance on when to
bypass the pipeline vs. drive it. No OCR/vision or enrichment-provider calls
are involved anywhere in this file, so nothing needs to be mocked.

The CSV response body is parsed with the stdlib `csv` module (`csv.DictReader`)
so assertions are against exact column values, never brittle substring/regex
matching on the raw body.

Judgment calls made in the absence of explicit spec text:
  1. **`companies` is not truncated** by `conftest.py`'s autouse
     `_clean_tables` fixture (confirmed by `test_10_lead_scoring.py`'s own
     docstring) — every `Company` created here uses a name containing a
     fresh `uuid.uuid4()` fragment so no test can collide with another's.
  2. **`Scanned On`'s exact string format is unspecified** by the spec (only
     that it reflects `created_at`) — tests assert it is non-blank for a
     card with a `created_at`, not a specific format.
  3. **The "first row if none flagged primary" tie-break** for
     `Primary Email`/`Primary Phone` depends on an ordering the spec does not
     pin down (first by what — insertion order? primary-key order?) — that
     specific tie-break path is intentionally not tested here to avoid
     encoding an assumption the spec doesn't make; the one documented,
     unambiguous case (exactly one row flagged `is_primary`) is covered
     instead.
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
# Auth helpers — mirrors test_10_lead_scoring.py's pattern.
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
# end-state.
# --------------------------------------------------------------------------


def _unescape_formula_guard(value: str) -> str:
    """export_service._csv_safe only ever prefixes a cell's leading
    character (a joined "All Emails"/"All Phones" cell is one CSV cell, so
    only its first character is a formula-injection risk — segments after
    the "; " separator are not independently re-escaped). Strips that one
    optional leading "'" so a test can assert on the underlying values
    without hard-coding which segment happened to land first."""
    return value[1:] if value.startswith("'") else value


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


def _parse_csv(text: str) -> list[dict]:
    """Parses the CSV response body with the stdlib csv module and asserts
    the header row matches the spec's exact column order before returning
    the data rows as dicts."""
    reader = csv.DictReader(io.StringIO(text))
    assert reader.fieldnames == EXPECTED_HEADERS, (
        f"CSV header row must match the spec's exact column order, got {reader.fieldnames}"
    )
    return list(reader)


def _assert_valid_csv_headers(resp) -> None:
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv"), resp.headers["content-type"]
    disposition = resp.headers["content-disposition"]
    assert re.match(
        r'^attachment; filename="dashr-leads-\d{4}-\d{2}-\d{2}\.csv"$', disposition
    ), f"unexpected Content-Disposition: {disposition!r}"


# ==========================================================================
# 1. Happy path — fully populated card (company + signals + two emails, one
#    primary + two phones + exhibition + score).
# ==========================================================================


def test_export_happy_path_full_data_returns_expected_csv_row(
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

    _assert_valid_csv_headers(resp)
    rows = _parse_csv(resp.text)
    assert len(rows) == 1, "one requested visible card must produce exactly one CSV data row"
    row = rows[0]

    assert row["Full Name"] == "Rohan Mehta"
    assert row["Job Title"] == "VP Procurement"
    assert row["Company"] == company_name
    assert row["Industry"] == "Industrial Machinery"
    assert int(row["Employee Count"]) == 250
    assert row["Revenue Band"] == "10-50 Cr"
    assert row["Primary Email"] == "rohan.mehta@fullexport.example.com"
    assert set(row["All Emails"].split("; ")) == {
        "rohan.mehta@fullexport.example.com",
        "rohan.alt@fullexport.example.com",
    }, f"All Emails must be a '; '-joined union of both emails, got {row['All Emails']!r}"
    # Phone numbers are prefixed with a leading "'" by export_service's CSV-
    # injection guard (_csv_safe): a bare leading "+" is on the CWE-1436
    # formula-trigger list, and E.164 numbers always start with one. Excel/
    # Sheets/LibreOffice all hide that leading quote and render the cell as
    # plain text on open. Only the cell's own first character is a risk (a
    # joined "All Phones" cell is one CSV field), so the guard prefixes the
    # cell once, not each "; "-separated segment — _unescape_formula_guard
    # strips that one optional leading quote before comparing values.
    assert row["Primary Phone"] == "'+919812345678"
    assert set(_unescape_formula_guard(row["All Phones"]).split("; ")) == {
        "+912212345678",
        "+919812345678",
    }, f"All Phones must be a '; '-joined union of both phones, got {row['All Phones']!r}"
    assert row["Website"] == "https://fullexport.example.com"
    assert row["Address"] == "Plot 12, MIDC, Pune"
    assert row["GST Number"] == "27ABCDE1234F1Z5"
    assert row["Products Offered"] == "CNC machined components"
    assert row["Designation Level"] == "vp"
    assert float(row["Lead Score"]) == 87
    assert row["Special Remark"] == "Wants a quote by Friday"
    assert row["Exhibition"] == "IndiaMFG Expo 2026"
    assert row["Status"] == "extracted"
    assert row["Scanned On"] != "", "Scanned On must reflect the card's created_at, not be blank"


# ==========================================================================
# 2. A card with no linked company exports with Company/Industry/Employee
#    Count/Revenue Band blank, plus the other genuinely-empty columns
#    (no exhibition, no emails, no phones) blank too, not "None"/errors.
# ==========================================================================


def test_export_card_with_no_linked_company_leaves_company_columns_blank(
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

    _assert_valid_csv_headers(resp)
    rows = _parse_csv(resp.text)
    assert len(rows) == 1
    row = rows[0]

    assert row["Full Name"] == "No Company Contact"
    assert row["Company"] == "", "no linked company must export Company blank, not an error/None"
    assert row["Industry"] == ""
    assert row["Employee Count"] == ""
    assert row["Revenue Band"] == ""
    assert row["Primary Email"] == "", "a card with no CardEmail rows must export a blank Primary Email"
    assert row["All Emails"] == ""
    assert row["Primary Phone"] == ""
    assert row["All Phones"] == ""
    assert row["Exhibition"] == "", "a card with no exhibition_id must export Exhibition blank"


# ==========================================================================
# 3. A card with lead_score IS NULL exports Lead Score blank, not "0".
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

    _assert_valid_csv_headers(resp)
    row = _parse_csv(resp.text)[0]
    assert row["Lead Score"] == "", (
        f"lead_score IS NULL must export as a blank cell, never '0', got {row['Lead Score']!r}"
    )


# ==========================================================================
# 3b. A card whose free-text fields start with a formula-triggering
#     character (=, +, -, @) must not export that character as the literal
#     first character of the CSV cell — CWE-1436 / CSV injection. Vision-LLM
#     card extraction is untrusted input (CLAUDE.md), so a poisoned physical
#     card scanned by a seller must not be able to execute a formula when
#     the exported CSV is later opened in Excel/Sheets.
# ==========================================================================


def test_export_neutralizes_formula_leading_characters_in_free_text_fields(
    client, fake_otp_provider, db_session
):
    user = _authenticated_user(client, fake_otp_provider)
    card_id = _make_card(
        db_session,
        user_id=uuid.UUID(user["user_id"]),
        full_name='=HYPERLINK("http://evil.example","click")',
        job_title="+1;calc",
        special_remark="-2+3+cmd|' /C calc'!A0",
        products_offered="@SUM(1,1)",
        status="extracted",
    )

    resp = client.post("/cards/export", json={"card_ids": [str(card_id)]})

    _assert_valid_csv_headers(resp)
    row = _parse_csv(resp.text)[0]
    for column in ("Full Name", "Job Title", "Special Remark", "Products Offered"):
        assert row[column][0] not in ("=", "+", "-", "@"), (
            f"{column} must not start with a formula-triggering character "
            f"in the raw CSV, got {row[column]!r}"
        )
    # The mitigation is a leading "'" (the standard fix — spreadsheet apps
    # hide it and render the cell as text), not truncation or replacement:
    # the rest of the original value must still be intact.
    assert row["Full Name"] == '\'=HYPERLINK("http://evil.example","click")'
    assert row["Job Title"] == "'+1;calc"
    assert row["Special Remark"] == "'-2+3+cmd|' /C calc'!A0"
    assert row["Products Offered"] == "'@SUM(1,1)"


# ==========================================================================
# 4. A card_ids list mixing a visible id with a foreign user's card id —
#    only the visible row appears; 200, not 403/404.
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

    resp = client.post(
        "/cards/export", json={"card_ids": [str(visible_id), str(foreign_id)]}
    )

    _assert_valid_csv_headers(resp)
    rows = _parse_csv(resp.text)
    assert len(rows) == 1, (
        "a foreign card id must be silently omitted from the CSV, not raise and not appear"
    )
    assert rows[0]["Full Name"] == "Visible Contact"


# ==========================================================================
# 5. An all-invisible card_ids list still returns 200 with a header-only CSV.
# ==========================================================================


def test_export_with_all_ids_invisible_returns_200_header_only_csv(
    client, fake_otp_provider, db_session
):
    _authenticated_user(client, fake_otp_provider)

    with TestClient(fastapi_app) as other_client:
        other_user = _authenticated_user(other_client, fake_otp_provider)
        foreign_id = _make_card(db_session, user_id=uuid.UUID(other_user["user_id"]))

    resp = client.post("/cards/export", json={"card_ids": [str(foreign_id)]})

    _assert_valid_csv_headers(resp)
    rows = _parse_csv(resp.text)
    assert rows == [], (
        "an all-invisible selection must return 200 with a header-only CSV, not an error"
    )


# ==========================================================================
# 6. card_ids: [] -> 422 (Pydantic min_length=1, before any DB access).
# ==========================================================================


def test_export_with_empty_card_ids_returns_422(client, fake_otp_provider):
    _authenticated_user(client, fake_otp_provider)

    resp = client.post("/cards/export", json={"card_ids": []})

    assert resp.status_code == 422, resp.text


# ==========================================================================
# 7. A 201-id card_ids list -> 422 (Pydantic max_length=200).
# ==========================================================================


def test_export_with_more_than_200_card_ids_returns_422(client, fake_otp_provider):
    _authenticated_user(client, fake_otp_provider)
    too_many_ids = [str(uuid.uuid4()) for _ in range(201)]

    resp = client.post("/cards/export", json={"card_ids": too_many_ids})

    assert resp.status_code == 422, resp.text


# ==========================================================================
# 8. Auth guard — unauthenticated request returns 401.
# ==========================================================================


def test_export_without_session_returns_401(client):
    resp = client.post("/cards/export", json={"card_ids": [str(uuid.uuid4())]})

    assert resp.status_code == 401, resp.text


# ==========================================================================
# 9. Read-only guarantee — exporting never mutates status/lead_score.
# ==========================================================================


def test_export_never_mutates_card_status_or_lead_score(client, fake_otp_provider, db_session):
    user = _authenticated_user(client, fake_otp_provider)
    card_id = _make_card(
        db_session,
        user_id=uuid.UUID(user["user_id"]),
        full_name="Untouched Contact",
        status="new",
        lead_score=None,
    )

    resp = client.post("/cards/export", json={"card_ids": [str(card_id)]})
    assert resp.status_code == 200, resp.text

    db_session.expire_all()
    card = db_session.get(VisitingCard, card_id)
    assert card.status == "new", "POST /cards/export must never mutate VisitingCard.status"
    assert card.lead_score is None, "POST /cards/export must never mutate VisitingCard.lead_score"
