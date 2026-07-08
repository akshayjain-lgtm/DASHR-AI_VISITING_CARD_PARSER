import io
import logging
import re

import phonenumbers
import pillow_heif
from email_validator import EmailNotValidError, validate_email
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.card_email import CardEmail
from app.models.card_phone import CardPhone
from app.models.company import Company
from app.models.user import User
from app.models.visiting_card import VisitingCard
from app.services import designation, storage_service, vision_client
from app.services.exceptions import ExtractionValidationError
from app.services.visibility import scope_to_visible_users

logger = logging.getLogger(__name__)

# Registers HEIC/HEIF as a Pillow-openable format. This runs in the Celery
# worker process, which is separate from the FastAPI process that registers
# it in card_service.py — each process needs its own registration since it's
# a global side-effect on that process's Pillow plugin registry, not shared
# state.
pillow_heif.register_heif_opener()

_MAX_IMAGE_EDGE_PX = 1568
_DEFAULT_PHONE_REGION = "IN"
_FOLDED_STATUSES = ("failed", "merged", "duplicate")
_GSTIN_PATTERN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z][Z][0-9A-Z]$")


def extract_card(db: Session, card: VisitingCard) -> str:
    """Top-level orchestration for one card's extraction.

    Returns "extracted" | "merged" | "duplicate" — the caller
    (card_processing.py) is solely responsible for setting
    card.status/processed_at and committing; this function only mutates ORM
    objects (via db.add()/attribute sets), flushing where a generated id
    must be read back immediately.

    Raises ExtractionValidationError for a permanent failure (no readable
    card fields at all). Lets VisionApiError from vision_client propagate
    untouched — the caller decides how to retry.
    """
    image_bytes, media_type = _download_and_downscale(card)
    raw = vision_client.extract_card_fields(image_bytes, media_type)
    fields = _normalize_fields(raw)

    if not _has_any_usable_field(fields):
        raise ExtractionValidationError("Vision model found no readable card fields")

    if _looks_like_back_of_card(fields):
        sibling = _find_back_of_card_sibling(db, card)
        if sibling is not None:
            logger.info(
                "card_id=%s looks like the back of card_id=%s, merging", card.card_id, sibling.card_id
            )
            _merge_fill_gaps(db, sibling, fields)
            card.merged_into_card_id = sibling.card_id
            card.raw_ocr_text = fields["raw_ocr_text"]
            return "merged"
        # No sibling found (first photo in the batch, or the true front was
        # uploaded in a different batch) — fall through and process this as
        # an ordinary card instead of losing the data.

    owner = db.get(User, card.user_id)
    duplicate = _find_duplicate_card(db, owner, card, fields)
    if duplicate is not None:
        logger.info(
            "card_id=%s is a duplicate of card_id=%s, merging", card.card_id, duplicate.card_id
        )
        _merge_fill_gaps(db, duplicate, fields)
        card.merged_into_card_id = duplicate.card_id
        card.raw_ocr_text = fields["raw_ocr_text"]
        return "duplicate"

    _apply_new_lead(db, card, fields)
    return "extracted"


def _download_and_downscale(card: VisitingCard) -> tuple[bytes, str]:
    """Downloads the card's stored image and downscales it (shrink-only,
    aspect-preserving) to a max 1568px edge before re-encoding as JPEG —
    controls vision token cost on large phone-camera photos. Always
    re-encodes to JPEG regardless of source format, sidestepping PNG/WEBP
    alpha-channel issues and — critically for HEIC/HEIF photos straight off
    an iPhone — Anthropic's vision API not accepting that format directly."""
    data = storage_service.download_file(card.image_url)
    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGB")
        img.thumbnail((_MAX_IMAGE_EDGE_PX, _MAX_IMAGE_EDGE_PX), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue(), "image/jpeg"


def _clean_str(value) -> str | None:
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


def _normalize_gst_number(value) -> str | None:
    """Strips whitespace the vision model sometimes inserts mid-GSTIN and
    validates against the standard 15-character GSTIN structure (2-digit
    state code + 10-char PAN + entity code + 'Z' + checksum). A value that
    doesn't match is treated as a misread/hallucination and dropped, same
    as an invalid email."""
    cleaned = _clean_str(value)
    if cleaned is None:
        return None
    normalized = "".join(cleaned.split()).upper()
    if not _GSTIN_PATTERN.match(normalized):
        return None
    return normalized


def _normalize_email(raw_email: dict) -> dict | None:
    email = _clean_str(raw_email.get("email"))
    if email is None:
        return None
    try:
        validated = validate_email(email, check_deliverability=False)
    except EmailNotValidError:
        return None
    return {"email": validated.normalized, "email_type": _clean_str(raw_email.get("email_type"))}


def _normalize_phone(raw_phone: dict) -> dict | None:
    raw = _clean_str(raw_phone.get("phone"))
    if raw is None:
        return None
    phone_type = _clean_str(raw_phone.get("phone_type"))
    try:
        parsed = phonenumbers.parse(raw, _DEFAULT_PHONE_REGION)
        if phonenumbers.is_valid_number(parsed):
            e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
            return {"phone_e164": e164, "phone_raw": raw, "phone_type": phone_type}
    except phonenumbers.NumberParseException:
        pass
    # Unparseable/invalid — never dropped, just kept without an E.164 form.
    return {"phone_e164": None, "phone_raw": raw, "phone_type": phone_type}


def _normalize_fields(raw: dict) -> dict:
    """The validator/normalizer pass — every vision-model field goes through
    here before it's ever persisted. Output keys match VisitingCard's model
    attribute names exactly (raw_ocr_text, not raw_text) so downstream merge
    logic never has to reconcile a naming mismatch."""
    emails = [e for e in (_normalize_email(e) for e in raw.get("emails") or []) if e is not None]
    for i, email in enumerate(emails):
        email["is_primary"] = i == 0

    phones = [p for p in (_normalize_phone(p) for p in raw.get("phones") or []) if p is not None]
    for i, phone in enumerate(phones):
        phone["is_primary"] = i == 0

    return {
        "is_back_of_card": bool(raw.get("is_back_of_card", False)),
        "full_name": _clean_str(raw.get("full_name")),
        "job_title": _clean_str(raw.get("job_title")),
        "company_name": _clean_str(raw.get("company_name")),
        "website": _clean_str(raw.get("website")),
        "address": _clean_str(raw.get("address")),
        "products_offered": _clean_str(raw.get("products_offered")),
        "gst_number": _normalize_gst_number(raw.get("gst_number")),
        "special_remark": _clean_str(raw.get("special_remark")),
        "raw_ocr_text": _clean_str(raw.get("raw_ocr_text")),
        "emails": emails,
        "phones": phones,
    }


def _has_any_usable_field(fields: dict) -> bool:
    return any(
        [
            fields["full_name"],
            fields["company_name"],
            fields["emails"],
            fields["phones"],
            fields["address"],
            fields["website"],
            fields["products_offered"],
            fields["gst_number"],
        ]
    )


def _looks_like_back_of_card(fields: dict) -> bool:
    return fields["is_back_of_card"] or not any(
        [fields["full_name"], fields["job_title"], fields["emails"], fields["phones"]]
    )


def _find_back_of_card_sibling(db: Session, card: VisitingCard) -> VisitingCard | None:
    if card.upload_batch_id is None or card.batch_sequence is None:
        return None
    stmt = (
        select(VisitingCard)
        .where(
            VisitingCard.upload_batch_id == card.upload_batch_id,
            VisitingCard.batch_sequence == card.batch_sequence - 1,
            VisitingCard.user_id == card.user_id,
            VisitingCard.status.notin_(_FOLDED_STATUSES),
        )
        .limit(1)
    )
    return db.scalar(stmt)


def _normalize_company_name(name: str) -> str:
    """Whitespace/case normalization only — no legal-suffix stripping or
    fuzzy matching. That richer normalization is 06-company-enrichment's
    job, not this one's."""
    return " ".join(name.strip().lower().split())


def _normalize_person_name(name: str) -> str:
    return " ".join(name.strip().lower().split())


def _find_duplicate_card(
    db: Session, owner: User, card: VisitingCard, fields: dict
) -> VisitingCard | None:
    """Three-tier lookup, in priority order, scoped through
    scope_to_visible_users so an admin's dedup check spans their whole org
    but never crosses tenant boundaries: primary email -> primary E.164
    phone -> (normalized full_name, normalized company name)."""
    base = scope_to_visible_users(select(VisitingCard), owner, VisitingCard.user_id).where(
        VisitingCard.card_id != card.card_id,
        VisitingCard.status.notin_(_FOLDED_STATUSES),
    )

    if fields["emails"]:
        primary_email = fields["emails"][0]["email"]
        stmt = base.join(CardEmail, CardEmail.card_id == VisitingCard.card_id).where(
            CardEmail.is_primary.is_(True),
            func.lower(CardEmail.email) == primary_email.lower(),
        )
        match = db.scalar(stmt.limit(1))
        if match is not None:
            return match

    primary_phone = fields["phones"][0]["phone_e164"] if fields["phones"] else None
    if primary_phone:
        stmt = base.join(CardPhone, CardPhone.card_id == VisitingCard.card_id).where(
            CardPhone.is_primary.is_(True),
            CardPhone.phone_e164 == primary_phone,
        )
        match = db.scalar(stmt.limit(1))
        if match is not None:
            return match

    if fields["full_name"] and fields["company_name"]:
        normalized_name = _normalize_person_name(fields["full_name"])
        normalized_company = _normalize_company_name(fields["company_name"])
        stmt = base.join(Company, Company.company_id == VisitingCard.company_id).where(
            VisitingCard.full_name.isnot(None),
            Company.normalized_name == normalized_company,
        )
        candidates = db.scalars(stmt).all()
        for candidate in candidates:
            if _normalize_person_name(candidate.full_name) == normalized_name:
                return candidate

    return None


def _get_or_create_company(db: Session, company_name: str | None, website: str | None) -> Company | None:
    if not company_name:
        return None
    normalized_name = _normalize_company_name(company_name)
    existing = db.scalar(select(Company).where(Company.normalized_name == normalized_name))
    if existing is not None:
        if website and not existing.website:
            existing.website = website
        return existing

    company = Company(name=company_name, normalized_name=normalized_name, website=website)
    db.add(company)
    db.flush()  # populate company.company_id (server_default gen_random_uuid()) before use
    return company


def _merge_fill_gaps(db: Session, canonical: VisitingCard, fields: dict) -> None:
    """Folds `fields` (extracted from a back-of-card or duplicate scan) onto
    `canonical` — the row that stays a real lead. Never overwrites a field
    canonical already has; only fills gaps."""
    scalar_attrs = (
        "full_name",
        "job_title",
        "website",
        "address",
        "products_offered",
        "gst_number",
        "special_remark",
        "raw_ocr_text",
    )
    for attr in scalar_attrs:
        if not getattr(canonical, attr) and fields.get(attr):
            setattr(canonical, attr, fields[attr])

    if not canonical.designation_level and canonical.job_title:
        canonical.designation_level = designation.classify(canonical.job_title)

    if canonical.company_id is None and fields["company_name"]:
        company = _get_or_create_company(db, fields["company_name"], fields["website"])
        if company is not None:
            canonical.company_id = company.company_id

    _merge_emails(db, canonical, fields["emails"])
    _merge_phones(db, canonical, fields["phones"])


def _merge_emails(db: Session, canonical: VisitingCard, new_emails: list[dict]) -> None:
    if not new_emails:
        return
    existing = db.scalars(
        select(CardEmail).where(CardEmail.card_id == canonical.card_id)
    ).all()
    existing_lower = {e.email.lower() for e in existing if e.email}
    has_primary = any(e.is_primary for e in existing)

    for email in new_emails:
        if email["email"].lower() in existing_lower:
            continue
        db.add(
            CardEmail(
                card_id=canonical.card_id,
                email=email["email"],
                email_type=email["email_type"],
                is_primary=not has_primary,
            )
        )
        has_primary = True
        existing_lower.add(email["email"].lower())


def _merge_phones(db: Session, canonical: VisitingCard, new_phones: list[dict]) -> None:
    if not new_phones:
        return
    existing = db.scalars(
        select(CardPhone).where(CardPhone.card_id == canonical.card_id)
    ).all()
    existing_e164 = {p.phone_e164 for p in existing if p.phone_e164}
    existing_raw = {p.phone_raw for p in existing if p.phone_raw}
    has_primary = any(p.is_primary for p in existing)

    for phone in new_phones:
        dedup_key_seen = (
            (phone["phone_e164"] and phone["phone_e164"] in existing_e164)
            or (not phone["phone_e164"] and phone["phone_raw"] in existing_raw)
        )
        if dedup_key_seen:
            continue
        db.add(
            CardPhone(
                card_id=canonical.card_id,
                phone_e164=phone["phone_e164"],
                phone_raw=phone["phone_raw"],
                phone_type=phone["phone_type"],
                is_primary=not has_primary,
            )
        )
        has_primary = True
        if phone["phone_e164"]:
            existing_e164.add(phone["phone_e164"])
        else:
            existing_raw.add(phone["phone_raw"])


def _apply_new_lead(db: Session, card: VisitingCard, fields: dict) -> None:
    card.full_name = fields["full_name"]
    card.job_title = fields["job_title"]
    card.designation_level = designation.classify(fields["job_title"])
    card.website = fields["website"]
    card.address = fields["address"]
    card.products_offered = fields["products_offered"]
    card.gst_number = fields["gst_number"]
    card.special_remark = fields["special_remark"]
    card.raw_ocr_text = fields["raw_ocr_text"]

    company = _get_or_create_company(db, fields["company_name"], fields["website"])
    if company is not None:
        card.company_id = company.company_id

    for email in fields["emails"]:
        db.add(
            CardEmail(
                card_id=card.card_id,
                email=email["email"],
                email_type=email["email_type"],
                is_primary=email["is_primary"],
            )
        )
    for phone in fields["phones"]:
        db.add(
            CardPhone(
                card_id=card.card_id,
                phone_e164=phone["phone_e164"],
                phone_raw=phone["phone_raw"],
                phone_type=phone["phone_type"],
                is_primary=phone["is_primary"],
            )
        )
