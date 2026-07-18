import logging
from typing import Protocol

logger = logging.getLogger("dashr.contact")

CONTACT_INBOX = "info@dashrtech.com"


class ContactEmailProvider(Protocol):
    def send(self, name: str, phone_no: str, email: str, query: str) -> None: ...


class ConsoleContactEmailProvider:
    """Dev-only enquiry delivery: logs the enquiry instead of emailing
    CONTACT_INBOX.

    Mirrors ConsoleInviteEmailProvider — `deps.get_contact_email_provider`
    refuses to hand this out when `settings.environment == "production"`;
    swap in a real email provider later by writing a class with the same
    `send` signature and wiring it there.
    """

    def send(self, name: str, phone_no: str, email: str, query: str) -> None:
        logger.info(
            "[CONTACT] Enquiry for %s from %s <%s> (%s): %s",
            CONTACT_INBOX,
            name,
            email,
            phone_no,
            query,
        )
