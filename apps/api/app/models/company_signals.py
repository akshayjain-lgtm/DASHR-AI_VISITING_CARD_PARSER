import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Numeric, Text, text
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

    # Lead-scoring v2 refinement: an AI-generated combined summary of
    # multiple full news articles (see news_summary_provider.py) plus a
    # share-price QOQ lookup — additive alongside recent_news_signals
    # above, which keeps being populated unchanged for step 07's own
    # general enrichment/display purposes.
    news_summary: Mapped[str | None] = mapped_column(Text)
    news_summary_generated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    # Claude's own classification of the summarized articles (subset of
    # "funding"/"expansion"/"new_facility"/"revenue_growth") — scoring.py
    # reads this directly rather than re-deriving the same classification
    # via a second, independently-maintained keyword scan of news_summary.
    news_tags: Mapped[list | None] = mapped_column(JSONB)
    share_price_qoq_growth_pct: Mapped[Decimal | None] = mapped_column(Numeric)
    news_distress_detected: Mapped[bool | None] = mapped_column(Boolean)

    # Local presence (public Google Maps search results / IndiaMART-TradeIndia-JustDial listings)
    google_rating: Mapped[Decimal | None] = mapped_column(Numeric)
    google_review_count: Mapped[int | None]
    marketplace_vintage_years: Mapped[int | None]
    marketplace_verified_badge: Mapped[bool | None] = mapped_column(Boolean)
    marketplace_located_in_industrial_area: Mapped[bool | None] = mapped_column(Boolean)
    catalog_url: Mapped[str | None]

    # IndiaMART supplier-profile page (Apify "IndiaMart Scraper" actor,
    # mode=supplierProfile, queried against catalog_url above)
    indiamart_rating: Mapped[Decimal | None] = mapped_column(Numeric)
    indiamart_rating_count: Mapped[int | None]
    indiamart_member_since_year: Mapped[int | None]
    indiamart_business_type: Mapped[str | None]
    indiamart_employee_count_band: Mapped[str | None]
    indiamart_annual_turnover_band: Mapped[str | None]
    indiamart_year_established: Mapped[str | None]
    indiamart_gst_number: Mapped[str | None]
    # Only ever observed live as a bare year (e.g. "2017"), never a full
    # date — an int, not a fabricated Jan-1 calendar date.
    indiamart_gst_registration_year: Mapped[int | None]
    indiamart_call_response_rate: Mapped[str | None]

    # Two independent freshness clocks (see
    # .claude/specs/24-company-linkage-tiered-expiry.md), replacing the
    # single updated_at this table used to carry — a refresh only re-fetches
    # whichever half has actually gone stale. Set explicitly by
    # enrichment_service.run_all_signal_lookups, only for the tier(s) it
    # actually ran — no ORM-level onupdate=, same rationale as the old
    # updated_at column had.
    factual_fetched_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    dynamic_fetched_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
