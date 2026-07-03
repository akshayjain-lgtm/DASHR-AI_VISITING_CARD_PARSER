import uuid

from sqlalchemy import Boolean, ForeignKey, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CardEmail(Base):
    __tablename__ = "card_emails"
    __table_args__ = (UniqueConstraint("card_id", "email"),)

    email_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    card_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("visiting_cards.card_id", ondelete="CASCADE"),
        nullable=False,
    )
    email: Mapped[str | None]
    email_type: Mapped[str | None]
    is_primary: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
