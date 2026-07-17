import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OrgInvite(Base):
    """A pending/resolved invite for an org admin to add a teammate as a
    'member'. Admin seats are never invited directly — see
    org_service.make_admin — so `role` is fixed to 'member' here."""

    __tablename__ = "org_invites"
    __table_args__ = (
        CheckConstraint("role = 'member'", name="ck_org_invites_role_valid"),
        CheckConstraint(
            "status IN ('pending', 'accepted', 'revoked', 'expired')",
            name="ck_org_invites_status_valid",
        ),
        Index(
            "uq_org_invites_org_email_pending",
            "org_id",
            text("lower(email)"),
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    invite_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.org_id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str]
    role: Mapped[str]
    token: Mapped[str] = mapped_column(unique=True)
    status: Mapped[str] = mapped_column(server_default=text("'pending'"))
    invited_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False
    )
    accepted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
