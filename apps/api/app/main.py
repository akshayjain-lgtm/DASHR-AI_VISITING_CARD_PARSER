import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.routers.auth import router as auth_router
from app.routers.cards import router as cards_router
from app.routers.exhibitions import router as exhibitions_router
from app.routers.profile import router as profile_router
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

app.include_router(auth_router)
app.include_router(cards_router)
app.include_router(exhibitions_router)
app.include_router(profile_router)


@app.on_event("startup")
def on_startup() -> None:
    guard_production_credentials()
    ensure_bucket_exists()
