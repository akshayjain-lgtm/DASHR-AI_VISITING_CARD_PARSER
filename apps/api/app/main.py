import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.routers.archive_uploads import router as archive_uploads_router
from app.routers.auth import router as auth_router
from app.routers.cards import router as cards_router
from app.routers.exhibitions import router as exhibitions_router
from app.routers.payments import router as payments_router
from app.routers.profile import router as profile_router
from app.routers.wallet import router as wallet_router
from app.services.razorpay_client import guard_production_credentials as guard_production_razorpay_credentials
from app.services.storage_service import ensure_bucket_exists, guard_production_credentials

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="DASHR AI API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Content-Type"],
)

app.include_router(archive_uploads_router)
app.include_router(auth_router)
app.include_router(cards_router)
app.include_router(exhibitions_router)
app.include_router(payments_router)
app.include_router(profile_router)
app.include_router(wallet_router)


@app.on_event("startup")
def on_startup() -> None:
    guard_production_credentials()
    guard_production_razorpay_credentials()
    ensure_bucket_exists()
