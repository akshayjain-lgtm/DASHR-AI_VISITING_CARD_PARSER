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
