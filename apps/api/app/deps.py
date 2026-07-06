from collections.abc import Generator

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import decode_access_token
from app.db.session import SessionLocal
from app.models.user import User
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
