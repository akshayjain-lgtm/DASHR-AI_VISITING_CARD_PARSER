import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.config import settings


class ExhibitionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    location: str | None = None
    # Required so the same-name+same-start_date dedupe check in
    # exhibition_service always has a date to compare against — an
    # exhibition recurs across years/venues, and the date is what tells two
    # otherwise-identical names apart.
    start_date: date
    end_date: date | None = None


class ExhibitionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    exhibition_id: uuid.UUID
    name: str | None
    location: str | None
    start_date: date | None
    end_date: date | None
    created_at: datetime


class CardOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    card_id: uuid.UUID
    user_id: uuid.UUID
    exhibition_id: uuid.UUID | None
    original_filename: str | None
    image_url: str
    # "new" | "processing" | "extracted" | "failed" | "duplicate" | "merged"
    status: str
    full_name: str | None
    job_title: str | None
    merged_into_card_id: uuid.UUID | None
    created_at: datetime
    company_id: uuid.UUID | None
    # Mirrors Company.name; null when the card has no linked company yet.
    company_name: str | None
    # Mirrors Company.enrichment_status; null when the card has no linked
    # company yet. "pending" | "enriching" | "enriched" | "not_found" | "failed"
    company_enrichment_status: str | None
    # float, not Decimal — VisitingCard.lead_score is Numeric at the ORM
    # layer, but Pydantic v2 serializes Decimal fields to JSON strings by
    # default; declaring float here makes from_attributes coerce it to a
    # real JSON number instead, matching the frontend's `number | null` type.
    lead_score: float | None
    # {designation_score, company_size_score, industry_fit_score,
    # momentum_signal_score, remark_signal_score, total, version}; null until scored
    score_breakdown: dict[str, int | str] | None
    scored_at: datetime | None
    # True when a field was corrected after this card's last score, meaning
    # a free rescore is allowed (see .claude/specs/20-field-correction.md's
    # billing amendment). Defaults False since list_cards' joined query
    # deliberately doesn't compute this per-row (would fan out into one
    # extra query per card on every page load) — only to_card_out/
    # get_card_detail's single-card paths ever set it to a real value.
    rescore_available: bool = False
    # True when this card's own per-lead cooldown has elapsed (see
    # lead_cooldown_service.py, .claude/specs/24-company-linkage-tiered-expiry.md)
    # and rescore_available is false — a rescore is still allowed, but billed
    # like a first-ever score rather than free. Same defaulting rationale as
    # rescore_available above.
    monthly_rescore_available: bool = False


class CardCompanyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    company_id: uuid.UUID
    name: str | None
    domain: str | None
    website: str | None
    # "pending" | "enriching" | "enriched" | "not_found" | "failed"
    enrichment_status: str
    summary: str | None
    summary_generated_at: datetime | None
    # True when Company.linked_org_id is set — this prospect is itself a
    # registered DASHR org. The raw org id is never exposed here (see
    # .claude/specs/24-company-linkage-tiered-expiry.md).
    is_linked_org: bool
    # True when enrichment_status == "enriched" and a refresh is available —
    # either a CompanySignals tier has gone stale, or this card's own
    # lead-cooldown has elapsed. Either way POST /cards/{card_id}/enrich-company
    # will succeed (billed) while this is true.
    refresh_available: bool = False
    linkedin_employee_count: int | None
    estimated_revenue_band: str | None
    gstin_verified: bool | None
    udyam_registered: bool | None
    hiring_signal: str | None
    google_rating: float | None
    # This supplier's public IndiaMART storefront/catalogue URL; null until
    # enrichment finds one (mirrors CompanySignals.catalog_url).
    catalog_url: str | None
    marketplace_verified_badge: bool | None
    marketplace_vintage_years: int | None
    # IndiaMART supplier-profile fields (Apify "IndiaMart Scraper" actor,
    # mode=supplierProfile, queried against catalog_url above).
    # float, not Decimal — same reason as lead_score above: Pydantic v2
    # would otherwise serialize this Numeric column to a JSON string.
    indiamart_rating: float | None
    indiamart_rating_count: int | None
    indiamart_member_since_year: int | None
    indiamart_business_type: str | None
    indiamart_employee_count_band: str | None
    indiamart_annual_turnover_band: str | None
    indiamart_year_established: str | None
    indiamart_gst_number: str | None
    # Only ever observed as a bare year (e.g. "2017"), never a full date.
    indiamart_gst_registration_year: int | None
    indiamart_call_response_rate: str | None


class CardEmailOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    email_id: uuid.UUID
    email: str | None
    email_type: str | None
    is_primary: bool


class CardPhoneOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    phone_id: uuid.UUID
    phone_e164: str | None
    phone_raw: str | None
    phone_type: str | None
    is_primary: bool


class CardDetailOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    card_id: uuid.UUID
    user_id: uuid.UUID
    exhibition_id: uuid.UUID | None
    original_filename: str | None
    image_url: str
    # "new" | "processing" | "extracted" | "failed" | "duplicate" | "merged"
    status: str
    full_name: str | None
    job_title: str | None
    designation_level: str | None
    special_remark: str | None
    website: str | None
    address: str | None
    products_offered: str | None
    gst_number: str | None
    raw_ocr_text: str | None
    extraction_error: str | None
    merged_into_card_id: uuid.UUID | None
    created_at: datetime
    lead_score: float | None
    score_breakdown: dict[str, int | str] | None
    scored_at: datetime | None
    # True when a field was corrected after this card's last score — a free
    # rescore is allowed (see .claude/specs/20-field-correction.md).
    rescore_available: bool = False
    # True when rescore_available is false but this card's per-lead cooldown
    # has elapsed — a rescore is still allowed, but billed (see
    # .claude/specs/24-company-linkage-tiered-expiry.md).
    monthly_rescore_available: bool = False
    company: CardCompanyOut | None
    emails: list[CardEmailOut]
    phones: list[CardPhoneOut]


class BulkUploadCardSummary(BaseModel):
    card_id: uuid.UUID
    original_filename: str | None
    status: str
    exhibition_id: uuid.UUID | None


class BulkUploadResponse(BaseModel):
    batch_size: int
    cards: list[BulkUploadCardSummary]


class CardProcessRequest(BaseModel):
    exhibition_id: uuid.UUID | None = None
    # When provided, narrows enqueueing to just these ids (still re-validated
    # server-side for visibility + status == "new"); when omitted, behavior is
    # unchanged — all "new" cards in scope. max_length matches
    # CardEnrichRequest/CardScoreRequest/CardBulkDeleteRequest — this
    # endpoint now drives a real wallet charge (charge_for_bulk_action), so
    # its billing surface is bounded the same way as the other bulk actions.
    card_ids: list[uuid.UUID] | None = Field(default=None, max_length=settings.max_bulk_upload_files)


class CardProcessResponse(BaseModel):
    enqueued_count: int
    # Matched but not enqueued because the acting user's free parse
    # allowance was exhausted and their wallet balance couldn't cover the
    # parse rate — distinct from enqueued_count, never silently merged into it.
    wallet_blocked_count: int


class CardEnrichRequest(BaseModel):
    # max_length is settings.max_bulk_upload_files itself, not a copy of it —
    # a caller-picked selection can never legitimately exceed the largest
    # batch that could have been uploaded, and this caps how many Celery
    # tasks/DB lookups one request can trigger. Deriving it here means
    # raising the upload cap can't silently drift out of sync with this one.
    card_ids: list[uuid.UUID] = Field(min_length=1, max_length=settings.max_bulk_upload_files)


class CardEnrichResponse(BaseModel):
    enqueued_count: int
    # Ineligible for enrichment (no linked company, company not "pending",
    # or a duplicate company already enqueued this batch) — never a wallet block.
    skipped_count: int
    # Eligible but not enqueued because the free enrichment allowance was
    # exhausted and the wallet balance couldn't cover the enrichment rate.
    wallet_blocked_count: int


class CardScoreRequest(BaseModel):
    card_ids: list[uuid.UUID] = Field(min_length=1, max_length=settings.max_bulk_upload_files)


class CardScoreResponse(BaseModel):
    enqueued_count: int
    # Ineligible for scoring (not "extracted" yet, or already scored) —
    # never a wallet block.
    skipped_count: int
    # Eligible but not enqueued because the free scoring allowance was
    # exhausted and the wallet balance couldn't cover the scoring rate.
    wallet_blocked_count: int


class CardExportRequest(BaseModel):
    # Deliberately NOT settings.max_bulk_upload_files (500) — this bounds the
    # synchronous, in-request query count in card_service.export_cards,
    # which does a per-card emails/phones query (see its docstring). Kept at
    # the old 200-id cap until export becomes a Celery task; raise it only
    # after re-evaluating that cost, not just to match the upload cap.
    card_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)


class CardBulkDeleteRequest(BaseModel):
    card_ids: list[uuid.UUID] = Field(min_length=1, max_length=settings.max_bulk_upload_files)
    # Same meaning as DELETE /cards/{card_id}'s confirm_cascade query param —
    # false on the first attempt; the caller resends the same request with
    # this set to true once the 409/child_count confirmation is accepted.
    confirm_cascade: bool = False


class CardBulkDeleteResponse(BaseModel):
    deleted_count: int
    # card_ids that weren't visible to the caller (wrong owner, different
    # org, or nonexistent) — silently skipped rather than failing the whole
    # batch, mirroring enqueue_enrichment/enqueue_scoring's best-effort
    # contract over a client-picked selection.
    skipped_count: int


class CardMergeRequest(BaseModel):
    # The card that stays a real lead — this endpoint's own card_id path
    # param is the one that gets folded into it and marked merged.
    target_card_id: uuid.UUID


class CardFieldCorrectionRequest(BaseModel):
    field_name: Literal[
        "full_name",
        "job_title",
        "address",
        "products_offered",
        "company_name",
        "email",
        "phone",
        "catalog_url",
    ]
    corrected_value: str = Field(min_length=1, max_length=2000)
    # Required (must identify a CardEmail.email_id/CardPhone.phone_id on this
    # card) when field_name is "email"/"phone"; must be omitted otherwise —
    # enforced below.
    record_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _validate_record_id(self) -> "CardFieldCorrectionRequest":
        needs_record_id = self.field_name in ("email", "phone")
        if needs_record_id and self.record_id is None:
            raise ValueError("record_id is required when field_name is 'email' or 'phone'")
        if not needs_record_id and self.record_id is not None:
            raise ValueError("record_id must be omitted for this field_name")
        return self
