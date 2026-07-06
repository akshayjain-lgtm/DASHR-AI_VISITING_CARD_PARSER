import uuid
from datetime import date, datetime

from sqlalchemy import ForeignKey, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Exhibition(Base):
    __tablename__ = "exhibitions"

    exhibition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str | None]
    location: Mapped[str | None]
    start_date: Mapped[date | None]
    end_date: Mapped[date | None]
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
