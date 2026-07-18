import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SellerProfile(Base):
    """The signed-up user's own company/product profile, used to calibrate lead scoring.

    Distinct from `Company`, which holds prospect firmographics.
    """

    __tablename__ = "seller_profiles"

    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False, unique=True
    )
    designation: Mapped[str | None]
    company_name: Mapped[str | None]
    industry: Mapped[str | None]
    product_lines: Mapped[str | None]
    last_year_revenue: Mapped[Decimal | None] = mapped_column(Numeric)
    revenue_currency: Mapped[str] = mapped_column(server_default=text("'INR'"))
    target_customer_description: Mapped[str | None]
    target_regions: Mapped[str | None]
    gst_no: Mapped[str | None]
    billing_address: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()"), onupdate=text("now()")
    )
