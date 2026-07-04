import logging
from typing import Protocol

logger = logging.getLogger("dashr.otp")


class OtpProvider(Protocol):
    def send(self, phone_no: str, code: str) -> None: ...


def _mask_phone(phone_no: str) -> str:
    return f"{'*' * max(len(phone_no) - 4, 0)}{phone_no[-4:]}"


class ConsoleOtpProvider:
    """Dev-only OTP delivery: logs the code instead of sending a real SMS.

    `deps.get_otp_provider` refuses to hand this out when
    `settings.environment == "production"`, so the raw code below is only
    ever written to a local dev log — never a production one. The phone
    number is still masked here for defense-in-depth (shared dev logs,
    CI output, etc. can outlive a single local session).

    Swap in a real SMS gateway later by writing a class with the same
    `send` signature and wiring it in `deps.get_otp_provider` — nothing
    else in `services/` imports this class directly.
    """

    def send(self, phone_no: str, code: str) -> None:
        logger.info("[OTP] %s: %s", _mask_phone(phone_no), code)
