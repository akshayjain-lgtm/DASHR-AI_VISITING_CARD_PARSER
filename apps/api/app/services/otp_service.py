import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import generate_otp_code, hash_otp, verify_otp_hash
from app.models.phone_otp_verification import PhoneOtpVerification
from app.services.exceptions import InvalidOtpError, OtpNotFoundError, ResendCooldownError
from app.services.otp_provider import OtpProvider


def _get_pending_otp(db: Session, user_id: uuid.UUID) -> PhoneOtpVerification | None:
    return db.scalar(
        select(PhoneOtpVerification)
        .where(
            PhoneOtpVerification.user_id == user_id,
            PhoneOtpVerification.verified_at.is_(None),
        )
        .order_by(PhoneOtpVerification.created_at.desc())
    )


def create_and_send_otp(
    db: Session, user_id: uuid.UUID, phone_no: str, provider: OtpProvider
) -> None:
    code = generate_otp_code()
    now = datetime.now(timezone.utc)
    otp = PhoneOtpVerification(
        user_id=user_id,
        phone_no=phone_no,
        otp_code_hash=hash_otp(code),
        expires_at=now + timedelta(minutes=settings.otp_expire_minutes),
    )
    db.add(otp)
    db.commit()
    provider.send(phone_no, code)


def verify_otp(db: Session, user_id: uuid.UUID, otp_code: str) -> None:
    row = _get_pending_otp(db, user_id)

    if row is None:
        raise InvalidOtpError()

    now = datetime.now(timezone.utc)
    if row.expires_at < now:
        # Expiry is a different failure mode than a wrong guess — don't burn
        # an attempt for a code that simply timed out.
        raise InvalidOtpError()

    if row.attempts >= settings.otp_max_attempts:
        raise InvalidOtpError()

    if not verify_otp_hash(otp_code, row.otp_code_hash):
        row.attempts += 1
        db.commit()
        raise InvalidOtpError()

    row.verified_at = now
    db.commit()


def resend_otp(
    db: Session, user_id: uuid.UUID, phone_no: str, provider: OtpProvider
) -> None:
    row = _get_pending_otp(db, user_id)

    if row is None:
        raise OtpNotFoundError()

    now = datetime.now(timezone.utc)
    cooldown = timedelta(seconds=settings.otp_resend_cooldown_seconds)
    if now - row.created_at < cooldown:
        raise ResendCooldownError()

    db.delete(row)
    db.flush()
    create_and_send_otp(db, user_id, phone_no, provider)
