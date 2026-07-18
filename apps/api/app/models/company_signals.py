import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Numeric, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CompanySignals(Base):
    """One row per `Company`, flattened enrichment signals ready for scoring
    to read directly without parsing `company_enrichment.payload`.

    `company_id` is the primary key (not a surrogate id + unique FK, unlike
    `SellerProfile`'s user_id pattern) because this table has no identity of
    its own — it's a value-object extension of `Company` — which also makes
    the upsert a plain `db.get(CompanySignals, company_id)` and gives the
    cascade-delete-with-its-company behavior for free.
    """

    __tablename__ = "company_signals"

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.company_id", ondelete="CASCADE"),
        primary_key=True,
    )

    # Registry (MCA public master-data search / Zauba Corp public mirror)
    cin: Mapped[str | None]
    incorporation_date: Mapped[date | None]
    registry_status: Mapped[str | None]
    registered_address: Mapped[str | None]
    authorized_capital: Mapped[Decimal | None] = mapped_column(Numeric)
    paid_up_capital: Mapped[Decimal | None] = mapped_column(Numeric)

    # Compliance (GST portal public taxpayer search / Udyam public registration search)
    gstin_verified: Mapped[bool | None] = mapped_column(Boolean)
    gstin_status: Mapped[str | None]
    udyam_registered: Mapped[bool | None] = mapped_column(Boolean)
    udyam_category: Mapped[str | None]

    # Firmographics (public LinkedIn company page)
    linkedin_employee_count: Mapped[int | None]
    linkedin_follower_count: Mapped[int | None]

    # Revenue signal — derived from udyam_category/paid_up_capital, never
    # fetched directly (no free public source publishes an exact figure)
    estimated_revenue_band: Mapped[str | None]

    # Website-derived
    product_lines_summary: Mapped[str | None]
    plant_size_signal: Mapped[str | None]

    # Growth/momentum (Naukri + LinkedIn public job pages / GeM public tender
    # search / Volza-ImportGenius public teaser numbers / Google News RSS)
    active_job_postings_count: Mapped[int | None]
    hiring_signal: Mapped[str | None]
    gem_tender_count: Mapped[int | None]
    gem_total_tender_value: Mapped[Decimal | None] = mapped_column(Numeric)
    import_export_activity: Mapped[bool | None] = mapped_column(Boolean)
    shipment_count_last_12m: Mapped[int | None]
    recent_news_signals: Mapped[list | None] = mapped_column(JSONB)

    # Local presence (public Google Maps search results / IndiaMART-TradeIndia-JustDial listings)
    google_rating: Mapped[Decimal | None] = mapped_column(Numeric)
    google_review_count: Mapped[int | None]
    marketplace_vintage_years: Mapped[int | None]
    marketplace_verified_badge: Mapped[bool | None] = mapped_column(Boolean)
    marketplace_located_in_industrial_area: Mapped[bool | None] = mapped_column(Boolean)
    catalog_url: Mapped[str | None]

    # Set explicitly by enrichment_service.run_all_signal_lookups on every
    # upsert — no ORM-level onupdate= here, since that would be dead code
    # next to a call site that always overwrites it anyway.
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
