import anthropic

from app.core.config import settings


def get_client(timeout: int) -> anthropic.Anthropic:
    """Shared Claude client construction, reused by every service that
    calls the Anthropic API (vision extraction, text summarization, ...)
    so the credential/timeout wiring lives in exactly one place."""
    return anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=timeout)
