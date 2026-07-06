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
    status: str
    full_name: str | None
    job_title: str | None
    created_at: datetime


class BulkUploadCardSummary(BaseModel):
    card_id: uuid.UUID
    original_filename: str | None
    status: str
    exhibition_id: uuid.UUID | None


class BulkUploadResponse(BaseModel):
    batch_size: int
    cards: list[BulkUploadCardSummary]
