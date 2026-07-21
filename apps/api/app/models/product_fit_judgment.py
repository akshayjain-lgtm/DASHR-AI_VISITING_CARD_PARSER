import uuid
from datetime import datetime

from sqlalchemy import Index, Text, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ProductFitJudgment(Base):
    """Cross-org shared cache (no org_id) for
    product_fit_service.get_or_judge_fit — the underlying question ("does a
    business of type X in industry Y need product Z") has no seller- or
    org-specific answer, same rationale as Company/CompanySignals.

    buyer_industry_normalized/buyer_business_type are non-nullable ("" for
    unknown) rather than nullable, since SQL NULL never equals NULL in an
    equality-based cache lookup.
    """

    __tablename__ = "product_fit_judgments"
    __table_args__ = (
        Index(
            "ix_product_fit_judgments_lookup",
            "product_signature_hash",
            "buyer_industry_normalized",
            "buyer_business_type",
        ),
    )

    judgment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    product_signature_hash: Mapped[str] = mapped_column(nullable=False)
    buyer_industry_normalized: Mapped[str] = mapped_column(nullable=False, server_default="")
    buyer_business_type: Mapped[str] = mapped_column(nullable=False, server_default="")
    # "needs" / "partial" / "no_need"
    verdict: Mapped[str] = mapped_column(nullable=False)
    reasoning: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
