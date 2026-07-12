"""Whole-codebase audit of `Settings` fields that look like credentials.

This is a standing regression test, not scoped to any single feature step —
unlike a step's own Definition-of-Done checks, it introspects the live
`app.core.config.Settings` class, which reflects every step's config
additions, not just one diff. It previously lived inside
`test_07_data_enrichment.py` (07-data-enrichment's DoD included "no new
paid-API credential is introduced by this step"), but a live-introspection
check can only ever assert "the current full set of secret-like fields
matches this allowlist" — it can't express "no new field in this specific
file's diff" — so keeping it in a step-scoped test file meant every later
step that legitimately added a real credential (e.g. 14-wallet-recharge's
Razorpay secrets, a deliberate, expected paid integration for billing per
CLAUDE.md) had to keep editing an unrelated feature's test file.

When a future step adds a genuine new credential/secret setting, update
`KNOWN_SECRET_LIKE_FIELDS` below in the same PR — that's the intended,
scoped place for this list to grow, not any individual step's test file.
"""
from app.core.config import settings

KNOWN_SECRET_LIKE_FIELDS = {
    "jwt_secret",  # auth (02-user-registration)
    "anthropic_api_key",  # vision/summary (05-parsing-visiting-card, 07-data-enrichment)
    "s3_secret_access_key",  # object storage (04-visiting-card-bulk-upload)
    "razorpay_key_secret",  # payments (14-wallet-recharge)
    "razorpay_webhook_secret",  # payments (14-wallet-recharge)
}


def test_settings_secret_like_fields_match_known_allowlist():
    """Every `_api_key`/`_secret`-matching field on `Settings` must be an
    intentional, reviewed addition — this test fails loudly (rather than
    silently) if a new one shows up without `KNOWN_SECRET_LIKE_FIELDS`
    being updated alongside it, forcing a conscious decision about whether
    the new credential is expected."""
    current_secret_like_fields = {
        name
        for name in type(settings).model_fields
        if "_api_key" in name or "_secret" in name
    }

    assert current_secret_like_fields == KNOWN_SECRET_LIKE_FIELDS, (
        "Settings gained or lost a '_api_key'/'_secret'-matching field — if this is an "
        "intentional new credential, add it to KNOWN_SECRET_LIKE_FIELDS in this file; "
        f"got {current_secret_like_fields!r}, expected {KNOWN_SECRET_LIKE_FIELDS!r}"
    )
