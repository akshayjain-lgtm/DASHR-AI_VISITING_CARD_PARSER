import base64

import anthropic

from app.core.config import settings
from app.services import anthropic_client
from app.services.exceptions import VisionApiError

_TOOL_NAME = "record_card_fields"

_TOOL_SCHEMA = {
    "name": _TOOL_NAME,
    "description": "Record the structured fields read from a business card photo.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_back_of_card": {
                "type": "boolean",
                "description": (
                    "true if this photo shows only a logo/address/QR/back side "
                    "with no personal name or contact details visible"
                ),
            },
            "full_name": {"type": ["string", "null"]},
            "job_title": {"type": ["string", "null"]},
            "company_name": {"type": ["string", "null"]},
            "website": {"type": ["string", "null"]},
            "address": {"type": ["string", "null"]},
            "products_offered": {
                "type": ["string", "null"],
                "description": "What the company deals in/sells, if stated on the card",
            },
            "gst_number": {
                "type": ["string", "null"],
                "description": (
                    "Indian GSTIN printed on the card, if present, e.g. "
                    "27ABCDE1234F1Z5"
                ),
            },
            "special_remark": {
                "type": ["string", "null"],
                "description": "Any handwritten or marginal note visible on the card",
            },
            "raw_ocr_text": {
                "type": ["string", "null"],
                "description": "Verbatim transcription of all visible printed and handwritten text",
            },
            "emails": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "email": {"type": "string"},
                        "email_type": {"type": ["string", "null"]},
                    },
                    "required": ["email"],
                },
            },
            "phones": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "phone": {"type": "string"},
                        "phone_type": {"type": ["string", "null"]},
                    },
                    "required": ["phone"],
                },
            },
        },
        "required": ["is_back_of_card"],
    },
}

_EXTRACTION_INSTRUCTION = "Read this business card photo and record its fields."


def _get_client() -> anthropic.Anthropic:
    return anthropic_client.get_client(settings.vision_request_timeout_seconds)


def extract_card_fields(image_bytes: bytes, media_type: str) -> dict:
    """Calls the vision model with a base64-encoded card image and returns the
    raw structured-output dict (unvalidated — extraction_service.py owns all
    validation/normalization). Uses forced tool-use rather than free-text JSON
    so the response is guaranteed to match the schema, with no markdown-fence
    stripping or JSON-parsing fragility.

    Raises VisionApiError for timeouts, connection errors, rate limits, or
    5xx responses — all retryable. A genuine 4xx (bad request/auth) is a real
    bug, not a per-card retry case, and is left to propagate as-is."""
    encoded = base64.b64encode(image_bytes).decode("ascii")
    try:
        response = _get_client().messages.create(
            model=settings.vision_model,
            max_tokens=1024,
            tools=[_TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": _TOOL_NAME},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": encoded,
                            },
                        },
                        {"type": "text", "text": _EXTRACTION_INSTRUCTION},
                    ],
                }
            ],
        )
    except (
        anthropic.APITimeoutError,
        anthropic.APIConnectionError,
        anthropic.RateLimitError,
    ) as exc:
        raise VisionApiError(str(exc)) from exc
    except anthropic.APIStatusError as exc:
        if exc.status_code >= 500:
            raise VisionApiError(str(exc)) from exc
        raise

    tool_use_blocks = [block for block in response.content if block.type == "tool_use"]
    if not tool_use_blocks:
        raise VisionApiError("Vision model did not return a tool_use block")
    return tool_use_blocks[0].input
