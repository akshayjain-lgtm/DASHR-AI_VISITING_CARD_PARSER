import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr


class InviteCreate(BaseModel):
    email: EmailStr


class InviteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    invite_id: uuid.UUID
    email: str
    role: str
    status: str
    created_at: datetime
    expires_at: datetime
    accepted_at: datetime | None


class InvitePreviewOut(BaseModel):
    org_name: str
    invitee_email: str
    status: str


class MyInviteOut(BaseModel):
    """Includes `token`, unlike InviteOut — the caller here is always the
    invite's own rightful recipient (see org_service.list_my_invites)."""

    invite_id: uuid.UUID
    org_name: str
    token: str
    expires_at: datetime


class OrgMemberOut(BaseModel):
    """Deliberately excludes wallet balance / free-action-allowance fields —
    this is a membership/visibility surface for an admin, never a
    spend-authority one (CLAUDE.md: wallets are per-user, never
    admin-controlled)."""

    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    name: str | None
    email: str
    role: str | None
    phone_no: str | None
    phone_verified: bool
    is_active: bool
    created_at: datetime
