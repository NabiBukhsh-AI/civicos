"""Outbound notification channels.

Every channel implements the same tiny interface, so the service layer decides
*what* to say and this package decides *how* it leaves the building. The
console channel is the default: a town evaluating the platform gets a fully
working notification pipeline in the logs before it has procured an SMS gateway.
"""

from __future__ import annotations

import asyncio
import smtplib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

import httpx
import structlog

from civicos.core.config import get_settings
from civicos.core.telemetry import NOTIFICATIONS_SENT
from civicos.domain.enums import NotificationChannel

logger = structlog.get_logger(__name__)

__all__ = [
    "Channel",
    "DeliveryResult",
    "Envelope",
    "get_dispatcher",
    "reset_dispatcher",
]


@dataclass(slots=True)
class Envelope:
    """One message, addressed and ready to send."""

    destination: str
    body: str
    subject: str | None = None
    language: str = "en"
    action_url: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(slots=True)
class DeliveryResult:
    delivered: bool
    provider_message_id: str | None = None
    error: str | None = None
    suppressed: bool = False


class Channel(ABC):
    name: str = "base"

    @abstractmethod
    async def send(self, envelope: Envelope) -> DeliveryResult: ...

    async def health(self) -> bool:
        return True


class ConsoleChannel(Channel):
    """Logs instead of sending. The safe default for dev and evaluation."""

    def __init__(self, name: str = "console") -> None:
        self.name = name

    async def send(self, envelope: Envelope) -> DeliveryResult:
        logger.info(
            "notification_console",
            channel=self.name,
            destination=envelope.destination,
            subject=envelope.subject,
            body=envelope.body[:400],
        )
        return DeliveryResult(delivered=True, provider_message_id="console")


class DisabledChannel(Channel):
    """Explicitly off - records a suppression rather than a failure."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def send(self, envelope: Envelope) -> DeliveryResult:
        return DeliveryResult(
            delivered=False, suppressed=True, error=f"{self.name} channel is disabled"
        )


class TwilioSmsChannel(Channel):
    name = "sms"

    def __init__(self, account_sid: str, auth_token: str, from_number: str) -> None:
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.from_number = from_number

    async def send(self, envelope: Envelope) -> DeliveryResult:
        url = (
            f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Messages.json"
        )
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(
                    url,
                    auth=(self.account_sid, self.auth_token),
                    data={
                        "From": self.from_number,
                        "To": envelope.destination,
                        "Body": envelope.body[:1600],
                    },
                )
            if response.status_code >= 400:
                return DeliveryResult(False, error=f"{response.status_code}: {response.text[:200]}")
            return DeliveryResult(True, provider_message_id=response.json().get("sid"))
        except Exception as exc:
            return DeliveryResult(False, error=str(exc)[:300])

    async def health(self) -> bool:
        return bool(self.account_sid and self.auth_token and self.from_number)


class WebhookChannel(Channel):
    """Generic POST - how most local SMS aggregators are actually integrated."""

    def __init__(self, url: str, name: str = "webhook") -> None:
        self.url = url
        self.name = name

    async def send(self, envelope: Envelope) -> DeliveryResult:
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(
                    self.url,
                    json={
                        "to": envelope.destination,
                        "subject": envelope.subject,
                        "body": envelope.body,
                        "language": envelope.language,
                        "action_url": envelope.action_url,
                        "metadata": envelope.metadata or {},
                    },
                )
            if response.status_code >= 400:
                return DeliveryResult(False, error=f"{response.status_code}")
            return DeliveryResult(True, provider_message_id=response.headers.get("x-message-id"))
        except Exception as exc:
            return DeliveryResult(False, error=str(exc)[:300])


class SmtpEmailChannel(Channel):
    name = "email"

    def __init__(
        self,
        host: str,
        port: int,
        username: str | None,
        password: str | None,
        sender: str,
        use_tls: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.sender = sender
        self.use_tls = use_tls

    async def send(self, envelope: Envelope) -> DeliveryResult:
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = envelope.destination
        message["Subject"] = envelope.subject or "Notification"
        body = envelope.body
        if envelope.action_url:
            body = f"{body}\n\n{envelope.action_url}"
        message.set_content(body)

        def _send() -> None:
            with smtplib.SMTP(self.host, self.port, timeout=20) as server:
                if self.use_tls:
                    server.starttls()
                if self.username and self.password:
                    server.login(self.username, self.password)
                server.send_message(message)

        try:
            await asyncio.to_thread(_send)
            return DeliveryResult(True)
        except Exception as exc:
            return DeliveryResult(False, error=str(exc)[:300])

    async def health(self) -> bool:
        return bool(self.host and self.sender)


class MetaWhatsAppChannel(Channel):
    name = "whatsapp"

    def __init__(self, phone_number_id: str, access_token: str) -> None:
        self.phone_number_id = phone_number_id
        self.access_token = access_token

    async def send(self, envelope: Envelope) -> DeliveryResult:
        url = f"https://graph.facebook.com/v21.0/{self.phone_number_id}/messages"
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {self.access_token}"},
                    json={
                        "messaging_product": "whatsapp",
                        "to": envelope.destination,
                        "type": "text",
                        "text": {"body": envelope.body[:4096]},
                    },
                )
            if response.status_code >= 400:
                return DeliveryResult(False, error=f"{response.status_code}: {response.text[:200]}")
            payload = response.json()
            message_id = (payload.get("messages") or [{}])[0].get("id")
            return DeliveryResult(True, provider_message_id=message_id)
        except Exception as exc:
            return DeliveryResult(False, error=str(exc)[:300])


class InAppChannel(Channel):
    """A no-op sender: the notification row *is* the delivery."""

    name = "in_app"

    async def send(self, envelope: Envelope) -> DeliveryResult:
        return DeliveryResult(True, provider_message_id="in_app")


class Dispatcher:
    """Routes an envelope to the channel implementation configured for it."""

    def __init__(self, channels: dict[NotificationChannel, Channel]) -> None:
        self.channels = channels

    async def send(
        self, channel: NotificationChannel, envelope: Envelope
    ) -> DeliveryResult:
        implementation = self.channels.get(channel)
        if implementation is None:
            return DeliveryResult(
                False, suppressed=True, error=f"No implementation for '{channel}'"
            )
        result = await implementation.send(envelope)
        NOTIFICATIONS_SENT.labels(
            channel=str(channel),
            outcome="suppressed" if result.suppressed else ("sent" if result.delivered else "failed"),
        ).inc()
        return result

    async def health(self) -> dict[str, bool]:
        return {
            str(name): await channel.health() for name, channel in self.channels.items()
        }


_dispatcher: Dispatcher | None = None


def get_dispatcher() -> Dispatcher:
    """Build the dispatcher from settings on first use."""
    global _dispatcher
    if _dispatcher is not None:
        return _dispatcher

    config = get_settings().notifications
    channels: dict[NotificationChannel, Channel] = {
        NotificationChannel.IN_APP: InAppChannel(),
    }

    if config.sms_backend == "twilio" and config.twilio_account_sid:
        channels[NotificationChannel.SMS] = TwilioSmsChannel(
            config.twilio_account_sid,
            config.twilio_auth_token or "",
            config.twilio_from_number or "",
        )
    elif config.sms_backend == "webhook" and config.webhook_url:
        channels[NotificationChannel.SMS] = WebhookChannel(config.webhook_url, "sms")
    elif config.sms_backend == "console":
        channels[NotificationChannel.SMS] = ConsoleChannel("sms")
    else:
        channels[NotificationChannel.SMS] = DisabledChannel("sms")

    if config.email_backend == "smtp" and config.smtp_host:
        channels[NotificationChannel.EMAIL] = SmtpEmailChannel(
            config.smtp_host,
            config.smtp_port,
            config.smtp_user,
            config.smtp_password,
            config.smtp_from,
            config.smtp_use_tls,
        )
    elif config.email_backend == "console":
        channels[NotificationChannel.EMAIL] = ConsoleChannel("email")
    else:
        channels[NotificationChannel.EMAIL] = DisabledChannel("email")

    if config.whatsapp_backend == "meta" and config.meta_phone_number_id:
        channels[NotificationChannel.WHATSAPP] = MetaWhatsAppChannel(
            config.meta_phone_number_id, config.meta_access_token or ""
        )
    elif config.whatsapp_backend == "console":
        channels[NotificationChannel.WHATSAPP] = ConsoleChannel("whatsapp")
    else:
        channels[NotificationChannel.WHATSAPP] = DisabledChannel("whatsapp")

    channels[NotificationChannel.PUSH] = (
        ConsoleChannel("push")
        if config.push_backend == "console"
        else DisabledChannel("push")
    )
    if config.webhook_url:
        channels[NotificationChannel.WEBHOOK] = WebhookChannel(config.webhook_url)

    _dispatcher = Dispatcher(channels)
    logger.info("notification_dispatcher_ready", channels=sorted(str(c) for c in channels))
    return _dispatcher


def reset_dispatcher() -> None:
    global _dispatcher
    _dispatcher = None
