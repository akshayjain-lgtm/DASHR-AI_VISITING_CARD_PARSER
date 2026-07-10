import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class ExhibitionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    location: str | None = None
    start_date: date | None = None
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
    linkedin_employee_count: int | None
    estimated_revenue_band: str | None
    gstin_verified: bool | None
    udyam_registered: bool | None
    hiring_signal: str | None
    google_rating: float | None


class CardEmailOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    email: str | None
    email_type: str | None
    is_primary: bool


class CardPhoneOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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
    # unchanged — all "new" cards in scope.
    card_ids: list[uuid.UUID] | None = None


class CardProcessResponse(BaseModel):
    enqueued_count: int


class CardEnrichRequest(BaseModel):
    card_ids: list[uuid.UUID] = Field(min_length=1)


class CardEnrichResponse(BaseModel):
    enqueued_count: int
    skipped_count: int
