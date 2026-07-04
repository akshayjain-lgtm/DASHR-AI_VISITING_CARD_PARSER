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
