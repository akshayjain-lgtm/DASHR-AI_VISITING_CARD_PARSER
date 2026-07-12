import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ArchiveUploadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    archive_id: uuid.UUID
    exhibition_id: uuid.UUID | None
    original_filename: str | None
    # "zip" | "pdf"
    container_type: str
    # "processing" | "completed" | "completed_with_errors" | "failed"
    status: str
    error_message: str | None
    created_at: datetime
