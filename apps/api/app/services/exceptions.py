class DuplicateEmailError(Exception):
    """Raised when signing up with an email that's already registered."""


class InvalidOtpError(Exception):
    """Raised for any OTP verification failure — wrong code, expired, or attempts exhausted.

    Deliberately one exception type for all failure modes so the router maps
    every one of them to the same generic 400 response, by construction.
    """


class ResendCooldownError(Exception):
    """Raised when resend-otp is called again before the cooldown window elapses."""


class OtpNotFoundError(Exception):
    """Raised when resend-otp is called for a user with no pending unverified OTP."""


class UserNotFoundError(Exception):
    """Raised when an operation references a user_id that doesn't exist."""


class PhoneAlreadyVerifiedError(Exception):
    """Raised when a phone number is already verified on a different account."""


class InvalidCredentialsError(Exception):
    """Raised when email+password does not match a real, usable account."""


class PhoneNotVerifiedError(Exception):
    """Raised when credentials are correct but the account never completed OTP verification."""


class EmptyBatchError(Exception):
    """Raised when a bulk-upload request contains no files at all."""


class UnsupportedFileTypeError(Exception):
    """Raised when an uploaded file's content-type is not in the allowed image list,
    or when its actual bytes don't decode to a real image matching that type."""


class FileTooLargeError(Exception):
    """Raised when an uploaded file exceeds the configured max size."""


class BatchTooLargeError(Exception):
    """Raised when a bulk-upload request contains more files than the configured max batch size."""


class ExhibitionNotFoundError(Exception):
    """Raised when a referenced exhibition_id doesn't exist or isn't visible to the caller."""


class ArchiveNotFoundError(Exception):
    """Raised when a referenced archive_id doesn't exist or isn't visible to the caller."""


class CorruptArchiveError(Exception):
    """Raised when an uploaded zip/pdf's bytes don't actually decode as that container
    format, even though it passed the content-type/magic-byte pre-filter."""


class CardNotFoundError(Exception):
    """Raised when a referenced card_id doesn't exist or isn't visible to the caller."""


class InvalidReprocessStateError(Exception):
    """Raised when POST /cards/{card_id}/reprocess is called on a card whose status isn't 'failed'."""


class ExtractionValidationError(Exception):
    """Raised by extraction_service when the vision model's output contains no name,
    no company, no contact info, and no address/website/products at all — signals the
    image wasn't a readable business card, not a bug to swallow."""


class VisionApiError(Exception):
    """Raised by vision_client for transient failures (timeout, rate limit, 5xx, or an
    unparseable response) calling the vision API. Retryable by Celery — never a final
    extraction outcome on its own."""


class CardHasNoCompanyError(Exception):
    """Raised by POST /cards/{card_id}/enrich-company when the card has no linked
    Company yet — extraction never attached one, so there's nothing to enrich."""


class CompanyNotEligibleForEnrichmentError(Exception):
    """Raised by POST /cards/{card_id}/enrich-company when the linked Company's
    enrichment_status isn't 'pending' — already enriching, enriched, not_found, or
    failed. Enrichment is a one-shot action per company, not re-triggerable on demand
    from this endpoint."""


class CardHasMergedChildrenError(Exception):
    """Raised by DELETE /cards/{card_id} and POST /cards/bulk-delete when a
    target card has other cards merged into it (merged_into_card_id pointing
    at it) and the caller hasn't confirmed the cascade via
    confirm_cascade=true. Carries child_count so the router can tell the
    caller how many cards would also be deleted."""

    def __init__(self, child_count: int):
        self.child_count = child_count
        child_word = "child" if child_count == 1 else "children"
        super().__init__(f"Card has {child_count} merged {child_word}; cascade not confirmed")


class CardStateChangedError(Exception):
    """Raised by DELETE /cards/{card_id} and POST /cards/bulk-delete when a
    concurrent request merged a new child onto a card between the children
    lookup and the commit, causing the self-referencing merged_into_card_id
    FK to reject the delete at commit time. The caller should retry the
    request."""


class CardNotEligibleForScoringError(Exception):
    """Raised by POST /cards/{card_id}/score when the card's status isn't
    'extracted' — scoring requires a card to have finished parsing. Blocks
    cards still 'new'/'processing'/'failed'/'merged'/'duplicate'. Distinct
    from CardAlreadyScoredError, which blocks re-scoring an eligible card
    that's already been scored."""


class CardAlreadyScoredError(Exception):
    """Raised by POST /cards/{card_id}/score when the card already has a
    lead_score. Scoring is one-shot per card — once scored, a card can never
    be re-scored, even after enrichment brings in better company data. There
    is deliberately no "already scored" bypass, unlike the original design;
    sellers must enrich a company before scoring a card, not after."""


class InvalidRechargeAmountError(Exception):
    """Raised when a wallet recharge amount falls outside the allowed band —
    defense in depth behind the WalletRechargeRequest Pydantic validation."""


class WebhookSignatureError(Exception):
    """Raised by payments.verify_webhook_signature when the X-Razorpay-Signature
    header is missing or doesn't match the raw request body. A wallet must
    never be credited on the strength of a client-side callback alone, so
    every recharge credit path runs through this check first."""


class InsufficientBalanceError(Exception):
    """Raised by billing.debit_wallet when the wallet's balance is lower than
    the requested debit amount. Not yet raised by any router in this feature
    — parse/enrich/score actions aren't wired to debit_wallet yet — but the
    check is race-safe from day one so wiring it in later needs no rework."""


class InvalidRechargeRequestError(Exception):
    """Raised by payments.create_recharge_order when Razorpay rejects the
    order request as malformed (its BadRequestError) — a client-caused 400,
    distinct from PaymentProviderError's transient/server-side failures."""


class PaymentProviderError(Exception):
    """Raised by payments.py when a Razorpay SDK call fails for a reason
    that isn't the caller's fault (GatewayError, ServerError, or any other
    unexpected SDK failure) — routers never import `razorpay` or catch its
    exception types directly; this is the one domain exception they see,
    keeping the vendor SDK boundary inside services/payments.py."""


class MalformedWebhookPayloadError(Exception):
    """Raised by payments.handle_payment_captured when a payment.captured
    event is missing required fields, or has fields that can't be parsed
    (order_id/payment_id/amount/notes.user_id). The payload has already
    passed signature verification by this point — this is a genuine
    Razorpay event this app can't act on, not a forgery attempt — so it's
    surfaced to the caller as 400 (malformed payload), never silently
    treated as a no-op 200 and never allowed to crash into an unhandled
    500. Distinct from an unrecognized-order-id or a different event type,
    both of which remain legitimate 200 no-ops."""
