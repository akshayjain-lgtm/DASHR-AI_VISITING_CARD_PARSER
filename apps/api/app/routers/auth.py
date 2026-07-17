import uuid

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import create_access_token
from app.deps import COOKIE_NAME, get_current_user, get_db, get_otp_provider
from app.models.user import User
from app.schemas.auth import (
    LoginRequest,
    ResendOtpRequest,
    SignupRequest,
    SignupResponse,
    UserOut,
    VerifyOtpRequest,
)
from app.services import auth_service
from app.services.exceptions import (
    DuplicateEmailError,
    InvalidCredentialsError,
    InvalidOtpError,
    OtpNotFoundError,
    PhoneAlreadyVerifiedError,
    PhoneNotVerifiedError,
    ResendCooldownError,
    UserDeactivatedError,
    UserNotFoundError,
)
from app.services.otp_provider import OtpProvider

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_session_cookie(response: Response, user_id: uuid.UUID) -> None:
    token = create_access_token(user_id)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
        max_age=settings.jwt_expire_minutes * 60,
    )


def _clear_session_cookie(response: Response) -> None:
    # Mirrors _set_session_cookie's path/secure/samesite exactly — mismatched
    # attributes can silently fail to clear a cookie that was set with Secure.
    response.delete_cookie(
        key=COOKIE_NAME,
        path="/",
        secure=settings.cookie_secure,
        samesite="lax",
    )


@router.post("/signup", status_code=201, response_model=SignupResponse)
def signup(
    data: SignupRequest,
    db: Session = Depends(get_db),
    provider: OtpProvider = Depends(get_otp_provider),
):
    try:
        user = auth_service.signup(db, data, provider)
    except DuplicateEmailError:
        raise HTTPException(status_code=409, detail="Email already registered")
    except PhoneAlreadyVerifiedError:
        raise HTTPException(
            status_code=409, detail="Phone number already verified on another account"
        )

    # No response.set_cookie call anywhere in this handler — signup alone
    # must never issue a session, only verify-otp does.
    return SignupResponse(user_id=user.user_id, phone_no=user.phone_no)


@router.post("/signup/verify-otp", response_model=UserOut)
def verify_otp(
    data: VerifyOtpRequest,
    response: Response,
    db: Session = Depends(get_db),
):
    try:
        user = auth_service.verify_signup_otp(db, data.user_id, data.otp_code)
    except InvalidOtpError:
        raise HTTPException(status_code=400, detail="Invalid or expired code")
    except PhoneAlreadyVerifiedError:
        raise HTTPException(
            status_code=409, detail="Phone number already verified on another account"
        )

    _set_session_cookie(response, user.user_id)
    return auth_service.to_user_out(db, user)


@router.post("/signup/resend-otp", status_code=204)
def resend_otp(
    data: ResendOtpRequest,
    db: Session = Depends(get_db),
    provider: OtpProvider = Depends(get_otp_provider),
):
    try:
        auth_service.resend_signup_otp(db, data.user_id, provider)
    except (UserNotFoundError, OtpNotFoundError):
        # Same generic response for "no such user" and "user exists but has
        # nothing pending" — resend-otp shouldn't leak more about account
        # state than verify-otp does.
        raise HTTPException(status_code=400, detail="Unable to resend code")
    except ResendCooldownError:
        raise HTTPException(
            status_code=429, detail="Please wait before requesting another code"
        )


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return auth_service.to_user_out(db, user)


@router.post("/login", response_model=UserOut)
def login(data: LoginRequest, response: Response, db: Session = Depends(get_db)):
    try:
        user = auth_service.login(db, data)
    except InvalidCredentialsError:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    except PhoneNotVerifiedError:
        raise HTTPException(status_code=403, detail="Phone number not verified")
    except UserDeactivatedError:
        raise HTTPException(status_code=403, detail="Account has been deactivated")

    _set_session_cookie(response, user.user_id)
    return auth_service.to_user_out(db, user)


@router.post("/logout", status_code=204)
def logout(response: Response, user: User = Depends(get_current_user)):
    _clear_session_cookie(response)
