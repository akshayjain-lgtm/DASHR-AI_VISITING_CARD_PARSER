import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.deps import get_db
from app.services import payments
from app.services.exceptions import (
    MalformedWebhookPayloadError,
    PaymentProviderError,
    WebhookSignatureError,
)

router = APIRouter(prefix="/payments", tags=["payments"])


@router.post("/webhook/razorpay")
async def razorpay_webhook(request: Request, db: Session = Depends(get_db)):
    """Server-to-server only — deliberately not behind get_current_user.
    Razorpay authenticates itself via the signed payload, not a session
    cookie; a wallet is only ever credited once that signature is verified
    (CLAUDE.md)."""
    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature")

    try:
        payments.verify_webhook_signature(raw_body, signature)
    except WebhookSignatureError:
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Malformed webhook payload")

    # Razorpay sends many event types to the same webhook URL — anything
    # other than a captured payment is acknowledged and ignored, not an error.
    if payload.get("event") == "payment.captured":
        try:
            payments.handle_payment_captured(db, payload)
        except MalformedWebhookPayloadError:
            raise HTTPException(status_code=400, detail="Malformed webhook payload")
        except PaymentProviderError as exc:
            raise HTTPException(
                status_code=502, detail="Could not verify payment with provider"
            ) from exc

    return {"status": "ok"}
