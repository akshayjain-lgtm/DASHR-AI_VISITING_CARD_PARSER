import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IS NULL OR role IN ('admin', 'member')", name="ck_users_role_valid"),
        CheckConstraint("role <> 'admin' OR org_id IS NOT NULL", name="ck_users_admin_requires_org"),
        Index(
            "uq_users_org_admin",
            "org_id",
            unique=True,
            postgresql_where=text("role = 'admin'"),
        ),
        Index(
            "uq_users_phone_no_verified",
            "phone_no",
            unique=True,
            postgresql_where=text("phone_verified = true"),
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.org_id", ondelete="SET NULL")
    )
    role: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()")
    )
    name: Mapped[str | None]
    email: Mapped[str] = mapped_column(unique=True)
    phone_no: Mapped[str | None]
    phone_verified: Mapped[bool] = mapped_column(server_default=text("false"))
    password_hash: Mapped[str | None]
    is_active: Mapped[bool] = mapped_column(server_default=text("true"))
