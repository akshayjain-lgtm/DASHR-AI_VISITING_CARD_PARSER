import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.models.user import User
from app.services import otp_service
from app.services.exceptions import (
    DuplicateEmailError,
    PhoneAlreadyVerifiedError,
    UserNotFoundError,
)
from app.services.otp_provider import OtpProvider
from app.schemas.auth import SignupRequest


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
