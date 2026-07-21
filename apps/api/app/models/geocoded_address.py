from datetime import datetime
from decimal import Decimal

from sqlalchemy import Numeric, Text, text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class GeocodedAddress(Base):
    """Cross-org shared cache (no org_id) for geocode_service.get_or_geocode
    — reused for both a card's prospect address and a seller's billing
    address, same cache/key scheme either way.

    address_hash (a sha256 hex digest of the normalized address text) is
    the primary key directly — it's already a natural, collision-safe
    unique key, no surrogate id needed. latitude/longitude both NULL means
    "we tried and it failed" (still cached, so it isn't retried every
    score); the row not existing at all means "never attempted".
    """

    __tablename__ = "geocoded_addresses"

    address_hash: Mapped[str] = mapped_column(primary_key=True)
    raw_address: Mapped[str] = mapped_column(Text, nullable=False)
    latitude: Mapped[Decimal | None] = mapped_column(Numeric)
    longitude: Mapped[Decimal | None] = mapped_column(Numeric)
    geocoded_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
