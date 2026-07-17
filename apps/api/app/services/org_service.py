import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.org_invite import OrgInvite
from app.models.organization import Organization
from app.models.seller_profile import SellerProfile
from app.models.user import User
from app.services.exceptions import (
    AlreadyInOrganizationError,
    CannotTargetSelfError,
    DuplicatePendingInviteError,
    InviteEmailMismatchError,
    InviteNotFoundError,
    UserNotFoundError,
)
from app.services.invite_email_provider import InviteEmailProvider

# How long an invite link stays acceptable — configurable in one place
# rather than a magic number scattered across create/preview/accept.
INVITE_EXPIRY = timedelta(days=7)


class InviteWithOrgName(NamedTuple):
    """An OrgInvite paired with its org's name — named fields instead of a
    positional tuple, since `invite, org_name = ...` reads worse the more
    call sites reuse it."""

    invite: OrgInvite
    org_name: str

# Product-fit / company-profile fields shared org-wide, copied from the
# admin's SellerProfile onto a new member's when they join (and seeded from
# signup's company_name for a new admin). Deliberately excludes gst_no and
# billing_address: those are per-user billing fields tied to that user's own
# Invoices and must never be inherited from the org (CLAUDE.md).
SHARED_SELLER_PROFILE_FIELDS = (
    "company_name",
    "industry",
    "product_lines",
    "last_year_revenue",
    "revenue_currency",
    "target_customer_description",
    "target_regions",
)


def _get_or_create_seller_profile(db: Session, user_id: uuid.UUID) -> SellerProfile:
    profile = db.scalar(select(SellerProfile).where(SellerProfile.user_id == user_id))
    if profile is None:
        profile = SellerProfile(user_id=user_id)
        db.add(profile)
    return profile


def create_org_with_admin(db: Session, user: User, company_name: str | None) -> None:
    """Mutates `user` in place (org_id/role) and stages a new Organization
    on `db`, but does not commit — called from auth_service.signup() before
    its own commit, so both land in the same transaction as the User
    insert. A no-op when company_name is blank: signup without a company
    name must keep yielding today's org-less user (org_id=None, role=None),
    exactly as before this feature existed."""
    if company_name is None or not company_name.strip():
        return

    org = Organization(name=company_name.strip())
    db.add(org)
    db.flush()  # populates org.org_id for the FK below

    user.org_id = org.org_id
    user.role = "admin"

    # The company name the admin typed at signup is also their own product-
    # fit profile's company_name — without this, /profile stays blank even
    # though they just told us their company name a moment ago.
    db.flush()  # populates user.user_id for the FK below
    profile = _get_or_create_seller_profile(db, user.user_id)
    profile.company_name = company_name.strip()


def create_invite(
    db: Session, admin: User, email: str, provider: InviteEmailProvider
) -> OrgInvite:
    invite = OrgInvite(
        org_id=admin.org_id,
        email=email,
        role="member",
        token=secrets.token_urlsafe(32),
        invited_by_user_id=admin.user_id,
        expires_at=datetime.now(timezone.utc) + INVITE_EXPIRY,
    )
    db.add(invite)
    try:
        db.commit()
    except IntegrityError:
        # A pending invite for this (org_id, email) already exists — the
        # partial unique index is the real guard, this just gives callers a
        # domain exception instead of a raw IntegrityError.
        db.rollback()
        raise DuplicatePendingInviteError()
    db.refresh(invite)

    org = db.get(Organization, admin.org_id)
    accept_url = f"{settings.frontend_url}/login?invite={invite.token}"
    provider.send(to_email=invite.email, org_name=org.name, accept_url=accept_url)
    return invite


def list_invites(db: Session, admin: User) -> list[OrgInvite]:
    stmt = (
        select(OrgInvite)
        .where(OrgInvite.org_id == admin.org_id)
        .order_by(OrgInvite.created_at.desc())
    )
    return list(db.scalars(stmt))


def revoke_invite(db: Session, admin: User, invite_id: uuid.UUID) -> None:
    invite = db.get(OrgInvite, invite_id)
    if (
        invite is None
        or invite.org_id != admin.org_id
        or invite.status != "pending"
    ):
        raise InviteNotFoundError()

    invite.status = "revoked"
    db.commit()


def _is_invite_live(invite: OrgInvite | None) -> bool:
    if invite is None or invite.status != "pending":
        return False
    expires_at = invite.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at > datetime.now(timezone.utc)


def get_invite_preview(db: Session, token: str) -> InviteWithOrgName:
    """For the public pre-signup banner."""
    invite = db.scalar(select(OrgInvite).where(OrgInvite.token == token))
    if not _is_invite_live(invite):
        raise InviteNotFoundError()

    org = db.get(Organization, invite.org_id)
    return InviteWithOrgName(invite=invite, org_name=org.name)


def list_my_invites(db: Session, current_user: User) -> list[InviteWithOrgName]:
    """Pending invites addressed to the calling user's own email — lets an
    invitee discover and accept an invite from their own account instead of
    only via the out-of-band accept link (which, in dev, is only ever
    logged server-side by ConsoleInviteEmailProvider, not actually
    emailed). Unlike the admin-facing list, this includes each invite's
    token: the caller IS the invite's rightful recipient here, so handing
    it back is what lets the frontend call the existing accept-by-token
    endpoint directly.

    Joins Organization in the same query rather than one lookup per invite
    — invite lists are small, but there's no reason to N+1 it."""
    now = datetime.now(timezone.utc)
    stmt = (
        select(OrgInvite, Organization.name)
        .join(Organization, Organization.org_id == OrgInvite.org_id)
        .where(
            func.lower(OrgInvite.email) == current_user.email.lower(),
            OrgInvite.status == "pending",
            OrgInvite.expires_at > now,
        )
        .order_by(OrgInvite.created_at.desc())
    )
    return [InviteWithOrgName(invite=invite, org_name=org_name) for invite, org_name in db.execute(stmt)]


def accept_invite(db: Session, current_user: User, token: str) -> User:
    invite = db.scalar(select(OrgInvite).where(OrgInvite.token == token))
    if not _is_invite_live(invite):
        raise InviteNotFoundError()

    if invite.email.lower() != current_user.email.lower():
        raise InviteEmailMismatchError()

    if current_user.org_id is not None:
        raise AlreadyInOrganizationError()

    current_user.org_id = invite.org_id
    current_user.role = "member"
    invite.status = "accepted"
    invite.accepted_by_user_id = current_user.user_id
    invite.accepted_at = datetime.now(timezone.utc)

    _sync_seller_profile_from_admin(db, invite.org_id, current_user.user_id)

    db.commit()
    db.refresh(current_user)
    return current_user


def _sync_seller_profile_from_admin(db: Session, org_id: uuid.UUID, member_user_id: uuid.UUID) -> None:
    """A joining member's product-fit profile should describe the same
    company as their org's admin — copies SHARED_SELLER_PROFILE_FIELDS from
    the admin's SellerProfile onto the member's own (get-or-create), so
    /profile reflects the org's company details immediately on accept
    rather than staying blank or mismatched. A no-op if the admin never
    filled in their own profile — nothing to copy in that case."""
    admin = db.scalar(select(User).where(User.org_id == org_id, User.role == "admin"))
    if admin is None:
        return

    admin_profile = db.scalar(select(SellerProfile).where(SellerProfile.user_id == admin.user_id))
    if admin_profile is None:
        return

    member_profile = _get_or_create_seller_profile(db, member_user_id)
    for field in SHARED_SELLER_PROFILE_FIELDS:
        setattr(member_profile, field, getattr(admin_profile, field))


def list_members(db: Session, admin: User) -> list[User]:
    stmt = select(User).where(User.org_id == admin.org_id)
    return list(db.scalars(stmt))


def _get_target_member(
    db: Session, admin: User, user_id: uuid.UUID, *, require_active: bool = False
) -> User:
    # Re-fetched fresh (not passed in from an earlier read) so two
    # concurrent admin actions on the same target both validate against the
    # live row, not a value that's gone stale mid-request.
    target = db.get(User, user_id)
    if target is None or target.org_id != admin.org_id:
        raise UserNotFoundError()
    if require_active and not target.is_active:
        raise UserNotFoundError()
    return target


def deactivate_member(db: Session, admin: User, user_id: uuid.UUID) -> User:
    # There is only ever one admin per org (uq_users_org_admin), and that
    # admin is always the caller here (get_current_admin requires it) — so
    # "target is an admin who isn't the caller" can never happen. This
    # self-check is therefore the complete admin-immunity guard, not just
    # part of one.
    if user_id == admin.user_id:
        raise CannotTargetSelfError()

    target = _get_target_member(db, admin, user_id)
    target.is_active = False
    db.commit()
    db.refresh(target)
    return target


def reactivate_member(db: Session, admin: User, user_id: uuid.UUID) -> User:
    target = _get_target_member(db, admin, user_id)

    target.is_active = True
    db.commit()
    db.refresh(target)
    return target


def make_admin(db: Session, admin: User, user_id: uuid.UUID) -> None:
    if user_id == admin.user_id:
        raise CannotTargetSelfError()

    # require_active=True: promoting a deactivated user would leave the org
    # with zero usable admins — the new admin can't log in (is_active
    # gates get_current_user) and the old admin just demoted themselves
    # away, with no in-app path back. The spec calls for 404 here, matching
    # every other "target not found/eligible" case in this file.
    target = _get_target_member(db, admin, user_id, require_active=True)

    # Order is load-bearing: `uq_users_org_admin` is a non-deferrable
    # partial unique index, checked immediately after each UPDATE rather
    # than at commit. Demoting the current admin first removes it from the
    # index before the promoting UPDATE would otherwise collide with it —
    # promote-then-demote raises IntegrityError and rolls back.
    admin.role = "member"
    db.flush()
    target.role = "admin"
    db.commit()
