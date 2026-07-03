import uuid
from datetime import datetime

from sqlalchemy import Index, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (
        Index(
            "uq_companies_domain",
            "domain",
            unique=True,
            postgresql_where=text("domain IS NOT NULL"),
        ),
        Index("ix_companies_normalized_name", "normalized_name"),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str | None]
    normalized_name: Mapped[str | None]
    domain: Mapped[str | None]
    website: Mapped[str | None]
    industry: Mapped[str | None]
    size_bucket: Mapped[str | None]
    hq_city: Mapped[str | None]
    hq_country: Mapped[str | None]
    linkedin_url: Mapped[str | None]
    enrichment_status: Mapped[str] = mapped_column(server_default=text("'pending'"))
    enriched_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
