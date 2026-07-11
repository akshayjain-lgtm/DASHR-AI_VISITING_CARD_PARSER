"""Pure CSV-formatting logic for POST /cards/export — no DB/session/Celery
imports here, mirrors scoring.py's separation of pure computation from the
DB-touching service/router layer. card_service.export_cards assembles the
row dicts; this module only turns them into CSV text.
"""
import csv
import io

# Column order/headers as a module-level constant so a future column
# addition or reorder is a one-line change here, not a rewrite of build_csv.
_COLUMNS: tuple[str, ...] = (
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
)


# Leading characters that Excel/Google Sheets/LibreOffice interpret as the
# start of a formula (CWE-1436 / OWASP CSV injection). Every field in this
# export can originate from vision-LLM-extracted card text, which CLAUDE.md
# already treats as untrusted — a card OCR'd with e.g.
# `=HYPERLINK("http://evil.example",...)` in special_remark or full_name
# must not be able to execute when a seller opens the exported CSV.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: str) -> str:
    """Prefixes a value with a single quote if it starts with a
    formula-triggering character — the standard mitigation, since it
    disables formula evaluation while leaving the value's actual text
    otherwise unchanged and still human-readable."""
    if value and value[0] in _FORMULA_PREFIXES:
        return "'" + value
    return value


def _blank(value) -> str:
    return "" if value is None else _csv_safe(str(value))


def _primary_and_all(items: list[dict], value_key: str) -> tuple[str, str]:
    """`items` is a list of {value_key: str | None, "is_primary": bool},
    already ordered primary-first by the caller's query (CardEmail/CardPhone
    queried with .order_by(is_primary.desc())) — so picking the first
    truthy value already implements "the is_primary row, or the first row
    if none is flagged primary" without re-sorting here. Returns
    (primary, "; "-joined all); both "" when items is empty or every value
    is None (CardEmail.email/CardPhone.phone are nullable columns)."""
    values = [item[value_key] for item in items if item[value_key]]
    if not values:
        return "", ""
    primary = next(
        (item[value_key] for item in items if item["is_primary"] and item[value_key]),
        values[0],
    )
    return _csv_safe(primary), _csv_safe("; ".join(values))


def _format_lead_score(value: float | None) -> str:
    if value is None:
        return ""
    # scoring.calculate_score's "total" is always a whole number (sum of
    # int component scores) — render without a trailing ".0".
    return str(int(value)) if value == int(value) else str(value)


def _format_scanned_on(value) -> str:
    return value.strftime("%Y-%m-%d") if value is not None else ""


def build_csv(rows: list[dict]) -> str:
    """Turns the row dicts assembled by card_service.export_cards into CSV
    text via the stdlib csv module + io.StringIO. Pure — no DB/session
    reads. An empty `rows` list still produces a header-only CSV, never an
    error (POST /cards/export's "no visible ids" contract is the caller's
    responsibility, not this function's)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(_COLUMNS)

    for row in rows:
        primary_email, all_emails = _primary_and_all(row["emails"], "email")
        primary_phone, all_phones = _primary_and_all(row["phones"], "phone")
        writer.writerow(
            [
                _blank(row["full_name"]),
                _blank(row["job_title"]),
                _blank(row["company_name"]),
                _blank(row["industry"]),
                _blank(row["employee_count"]),
                _blank(row["revenue_band"]),
                primary_email,
                all_emails,
                primary_phone,
                all_phones,
                _blank(row["website"]),
                _blank(row["address"]),
                _blank(row["gst_number"]),
                _blank(row["products_offered"]),
                _blank(row["designation_level"]),
                _format_lead_score(row["lead_score"]),
                _blank(row["special_remark"]),
                _blank(row["exhibition_name"]),
                _blank(row["status"]),
                _format_scanned_on(row["scanned_on"]),
            ]
        )

    return buffer.getvalue()
