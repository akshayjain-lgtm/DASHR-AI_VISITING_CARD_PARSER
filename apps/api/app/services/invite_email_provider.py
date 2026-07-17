import logging
from typing import Protocol

logger = logging.getLogger("dashr.invite")


class InviteEmailProvider(Protocol):
    def send(self, to_email: str, org_name: str, accept_url: str) -> None: ...


class ConsoleInviteEmailProvider:
    """Dev-only invite delivery: logs the accept link instead of sending a
    real email.

    `deps.get_invite_email_provider` refuses to hand this out when
    `settings.environment == "production"`, mirroring
    `deps.get_otp_provider`'s guard for `ConsoleOtpProvider` — swap in a
    real email provider later by writing a class with the same `send`
    signature and wiring it there; nothing else in `services/` imports this
    class directly.
    """

    def send(self, to_email: str, org_name: str, accept_url: str) -> None:
        logger.info("[INVITE] %s invited to join %s: %s", to_email, org_name, accept_url)
