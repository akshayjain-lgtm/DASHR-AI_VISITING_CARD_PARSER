import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ArchiveUpload(Base):
    __tablename__ = "archive_uploads"
    __table_args__ = (Index("ix_archive_uploads_user_id_status", "user_id", "status"),)

    archive_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False
    )
    exhibition_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("exhibitions.exhibition_id")
    )
    original_filename: Mapped[str | None]
    # "zip" | "pdf"
    container_type: Mapped[str]
    storage_key: Mapped[str]
    # "processing" | "completed" | "completed_with_errors" | "failed"
    status: Mapped[str] = mapped_column(server_default=text("'processing'"))
    error_message: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
