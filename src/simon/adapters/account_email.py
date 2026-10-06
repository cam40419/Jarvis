"""Transactional account email through UI-managed Resend or TLS-only SMTP."""

import smtplib
import ssl
from email.message import EmailMessage
from uuid import UUID

import httpx

from simon.domain.errors import ValidationError
from simon.services.integrations import IntegrationService


class AccountEmail:
    def __init__(self, integrations: IntegrationService) -> None:
        self.integrations = integrations

    @property
    def configured(self) -> bool:
        return self.integrations.email_connection() is not None

    def send(self, recipient: str, subject: str, text: str, identifier: UUID) -> None:
        record = self.integrations.email_connection()
        if record is None:
            raise ValidationError(
                "Ask your administrator to configure email delivery in Connections"
            )
        settings = record.settings
        secret = self.integrations.decrypt(record.encrypted_secret)
        try:
            if settings["transport"] == "resend":
                with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as client:
                    response = client.post(
                        "https://api.resend.com/emails",
                        headers={
                            "Authorization": "Bearer " + secret,
                            "Idempotency-Key": str(identifier),
                        },
                        json={
                            "from": settings["from_email"],
                            "to": [recipient],
                            "subject": subject,
                            "text": text,
                        },
                    )
                    response.raise_for_status()
            else:
                message = EmailMessage()
                message["From"], message["To"], message["Subject"] = (
                    settings["from_email"],
                    recipient,
                    subject,
                )
                message.set_content(text)
                context = ssl.create_default_context()
                connection: smtplib.SMTP
                if settings["smtp_port"] == 465:
                    connection = smtplib.SMTP_SSL(
                        settings["smtp_host"], 465, timeout=15, context=context
                    )
                else:
                    connection = smtplib.SMTP(settings["smtp_host"], 587, timeout=15)
                with connection:
                    if settings["smtp_port"] == 587:
                        connection.starttls(context=context)
                    connection.login(settings["smtp_username"], secret)
                    connection.send_message(message)
        except (httpx.HTTPError, smtplib.SMTPException, OSError, ValueError):
            raise ValidationError(
                "Email delivery failed; check the sender settings and try again"
            ) from None
