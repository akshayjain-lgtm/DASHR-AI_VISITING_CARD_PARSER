from collections.abc import Generator

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import decode_access_token
from app.db.session import SessionLocal
from app.models.user import User
from app.services.contact_email_provider import ConsoleContactEmailProvider, ContactEmailProvider
from app.services.invite_email_provider import ConsoleInviteEmailProvider, InviteEmailProvider
from app.services.otp_provider import ConsoleOtpProvider, OtpProvider

COOKIE_NAME = "dashr_session"


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get(COOKIE_NAME)
    if token is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user_id = decode_access_token(token)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Re-checked on every request against the live row (not cached on the
    # JWT), so an admin deactivating a teammate cuts off that teammate's
    # already-issued session cookie immediately, not just their next login.
    if not user.is_active:
        raise HTTPException(status_code=401, detail="Not authenticated")

    return user


def get_current_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin" or user.org_id is None:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def get_otp_provider() -> OtpProvider:
    if settings.environment == "production":
        # ConsoleOtpProvider only logs the code server-side — it must never
        # be what actually ships. Fail loudly rather than silently "deliver"
        # OTPs nobody receives.
        raise RuntimeError(
            "No production OTP provider configured — ConsoleOtpProvider "
            "must not be used when ENVIRONMENT=production"
        )
    return ConsoleOtpProvider()


def get_invite_email_provider() -> InviteEmailProvider:
    if settings.environment == "production":
        # Same rationale as get_otp_provider above: ConsoleInviteEmailProvider
        # only logs the accept link, it must never be what actually ships.
        raise RuntimeError(
            "No production invite email provider configured — "
            "ConsoleInviteEmailProvider must not be used when ENVIRONMENT=production"
        )
    return ConsoleInviteEmailProvider()


def get_contact_email_provider() -> ContactEmailProvider:
    if settings.environment == "production":
        # Same rationale as get_otp_provider above: ConsoleContactEmailProvider
        # only logs the enquiry, it must never be what actually ships.
        raise RuntimeError(
            "No production contact email provider configured — "
            "ConsoleContactEmailProvider must not be used when ENVIRONMENT=production"
        )
    return ConsoleContactEmailProvider()
