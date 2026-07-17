import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import hash_password, verify_password
from app.models.organization import Organization
from app.models.user import User
from app.services import org_service, otp_service
from app.services.exceptions import (
    DuplicateEmailError,
    InvalidCredentialsError,
    PhoneAlreadyVerifiedError,
    PhoneNotVerifiedError,
    UserDeactivatedError,
    UserNotFoundError,
)
from app.services.otp_provider import OtpProvider
from app.schemas.auth import LoginRequest, SignupRequest, UserOut

# Precomputed so a nonexistent-email login still pays the same bcrypt cost as
# a real user with a wrong password — without this, verify_password would
# only run for real accounts, making lookup time itself an enumeration
# channel even though the response message is identical either way.
_DUMMY_PASSWORD_HASH = hash_password("dummy-password-for-timing-parity")


def _find_verified_phone_conflict(
    db: Session, phone_no: str, *, exclude_user_id: uuid.UUID | None = None
) -> User | None:
    """A phone number already verified on a DIFFERENT account. Shared by
    signup() (the common case, failing before anything is created) and
    verify_signup_otp() (the race where both accounts signed up before
    either verified, so signup's own check couldn't have caught it)."""
    stmt = select(User).where(User.phone_no == phone_no, User.phone_verified.is_(True))
    if exclude_user_id is not None:
        stmt = stmt.where(User.user_id != exclude_user_id)
    return db.scalar(stmt)


def to_user_out(db: Session, user: User) -> UserOut:
    """Builds the UserOut response shape with one explicit query for the
    org name and, for a non-admin member, one more for their org's admin
    contact details. Deliberately not a User model property: a property
    would fire these queries implicitly on every UserOut.model_validate()
    call (surprising, and an easy N+1 if UserOut is ever used in a list
    endpoint) and would go silently None for a detached instance instead
    of erroring — every caller building a UserOut should call this
    explicitly instead."""
    org_name = None
    admin_name = None
    admin_email = None

    if user.org_id is not None:
        org = db.get(Organization, user.org_id)
        org_name = org.name if org is not None else None

        if user.role != "admin":
            admin = db.scalar(
                select(User).where(User.org_id == user.org_id, User.role == "admin")
            )
            if admin is not None:
                admin_name = admin.name
                admin_email = admin.email

    return UserOut(
        user_id=user.user_id,
        name=user.name,
        email=user.email,
        phone_no=user.phone_no,
        org_id=user.org_id,
        org_name=org_name,
        role=user.role,
        phone_verified=user.phone_verified,
        is_active=user.is_active,
        admin_name=admin_name,
        admin_email=admin_email,
    )


def signup(db: Session, data: SignupRequest, provider: OtpProvider) -> User:
    existing = db.scalar(select(User).where(User.email == data.email))
    if existing is not None:
        raise DuplicateEmailError()

    # Checked before creating anything or sending an OTP: a phone number
    # already verified on another account can never complete verification
    # here either (uq_users_phone_no_verified blocks it), so failing fast
    # here means the caller never wastes a signup + OTP round-trip on a
    # phone number that was always going to be rejected.
    if _find_verified_phone_conflict(db, data.phone_no) is not None:
        raise PhoneAlreadyVerifiedError()

    user = User(
        name=data.name,
        email=data.email,
        phone_no=data.phone_no,
        password_hash=hash_password(data.password),
        org_id=None,
        role=None,
        phone_verified=False,
    )
    db.add(user)
    try:
        # No-ops when company_name is blank; otherwise creates the
        # Organization and sets user.org_id/role, flushing the pending User
        # insert along the way — wrapped in the same try/except as the
        # commit below since that flush can surface the same duplicate-email
        # race the commit guards against.
        org_service.create_org_with_admin(db, user, data.company_name)
        db.commit()
    except IntegrityError:
        # Two concurrent signups with the same email can both pass the
        # existence check above — the unique constraint is the real guard.
        db.rollback()
        raise DuplicateEmailError()
    db.refresh(user)

    otp_service.create_and_send_otp(db, user.user_id, user.phone_no, provider)
    return user


def verify_signup_otp(db: Session, user_id: uuid.UUID, otp_code: str) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise UserNotFoundError()

    # Checked before consuming the OTP (not just via the commit's
    # IntegrityError below): the signup-time check in signup() catches this
    # in the common case, but a phone number can still get verified on
    # another account in the gap between this user's signup and their OTP
    # entry. Failing here first means that race doesn't burn a one-time
    # code on a verification that was always going to be rejected — the
    # DB constraint below remains as the final defense-in-depth guard.
    if _find_verified_phone_conflict(db, user.phone_no, exclude_user_id=user_id) is not None:
        raise PhoneAlreadyVerifiedError()

    # Propagates InvalidOtpError from otp_service as-is — the router maps it
    # to the single generic 400 for every OTP failure mode.
    otp_service.verify_otp(db, user_id, otp_code)

    user.phone_verified = True
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise PhoneAlreadyVerifiedError()

    db.refresh(user)
    return user


def resend_signup_otp(db: Session, user_id: uuid.UUID, provider: OtpProvider) -> None:
    user = db.get(User, user_id)
    if user is None:
        raise UserNotFoundError()

    otp_service.resend_otp(db, user_id, user.phone_no, provider)


def login(db: Session, data: LoginRequest) -> User:
    user = db.scalar(select(User).where(User.email == data.email))
    hash_to_check = (
        user.password_hash if user is not None and user.password_hash else _DUMMY_PASSWORD_HASH
    )
    password_ok = verify_password(data.password, hash_to_check)

    if user is None or user.password_hash is None or not password_ok:
        raise InvalidCredentialsError()

    # Checked only after credentials match, so this never becomes a second
    # enumeration channel — it only reveals "unverified" to someone who
    # already proved they know the password.
    if not user.phone_verified:
        raise PhoneNotVerifiedError()

    # Checked last, after every other failure mode — a deactivated account
    # already proved its credentials and verification, so this is purely
    # about current access, not identity.
    if not user.is_active:
        raise UserDeactivatedError()

    return user
