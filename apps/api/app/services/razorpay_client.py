import razorpay

from app.core.config import settings


def get_client() -> razorpay.Client:
    """Shared Razorpay client construction, reused by every service that
    calls the Razorpay API — mirrors anthropic_client.get_client()'s role
    for the vision/summary services."""
    return razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))


def guard_production_credentials() -> None:
    """Mirrors storage_service.guard_production_credentials's production
    guard. An HMAC keyed with an empty/missing RAZORPAY_WEBHOOK_SECRET is
    trivially reproducible by anyone — a signature check against a blank
    secret verifies nothing — so a deployment that forgets to set real
    Razorpay credentials must fail loudly at boot rather than silently
    accepting forged webhook payloads and crediting arbitrary wallets."""
    if settings.environment != "production":
        return
    if (
        not settings.razorpay_key_id
        or not settings.razorpay_key_secret
        or not settings.razorpay_webhook_secret
    ):
        raise RuntimeError(
            "RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET/RAZORPAY_WEBHOOK_SECRET must all be set "
            "when ENVIRONMENT=production"
        )
