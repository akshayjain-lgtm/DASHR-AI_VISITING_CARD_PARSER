import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import hash_password, verify_password
from app.models.user import User
from app.services import otp_service
from app.services.exceptions import (
    DuplicateEmailError,
    InvalidCredentialsError,
    PhoneAlreadyVerifiedError,
    PhoneNotVerifiedError,
    UserNotFoundError,
)
from app.services.otp_provider import OtpProvider
from app.schemas.auth import LoginRequest, SignupRequest

# Precomputed so a nonexistent-email login still pays the same bcrypt cost as
# a real user with a wrong password — without this, verify_password would
# only run for real accounts, making lookup time itself an enumeration
# channel even though the response message is identical either way.
_DUMMY_PASSWORD_HASH = hash_password("dummy-password-for-timing-parity")


def signup(db: Session, data: SignupRequest, provider: OtpProvider) -> User:
    existing = db.scalar(select(User).where(User.email == data.email))
    if existing is not None:
        raise DuplicateEmailError()

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
    # Propagates InvalidOtpError from otp_service as-is — the router maps it
    # to the single generic 400 for every OTP failure mode.
    otp_service.verify_otp(db, user_id, otp_code)

    user = db.get(User, user_id)
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

    return user
