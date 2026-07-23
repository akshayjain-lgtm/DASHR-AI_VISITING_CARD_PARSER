import logging
from typing import Protocol

logger = logging.getLogger("dashr.support_query")

SUPPORT_INBOX = "info@dashrtech.com"


class SupportQueryEmailProvider(Protocol):
    def send(
        self, ticket_id: str, user_name: str | None, user_email: str, subject: str, message: str
    ) -> None: ...


class ConsoleSupportQueryEmailProvider:
    """Dev-only query delivery: logs the ticket instead of emailing
    SUPPORT_INBOX.

    Mirrors ConsoleContactEmailProvider — `deps.get_support_query_email_provider`
    refuses to hand this out when `settings.environment == "production"`;
    swap in a real email provider later by writing a class with the same
    `send` signature and wiring it there.
    """

    def send(
        self, ticket_id: str, user_name: str | None, user_email: str, subject: str, message: str
    ) -> None:
        logger.info(
            "[SUPPORT %s] Query for %s from %s <%s>: %s — %s",
            ticket_id,
            SUPPORT_INBOX,
            user_name or "(no name)",
            user_email,
            subject,
            message,
        )
