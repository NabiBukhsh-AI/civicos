"""Notification orchestration.

Decides *who* to tell, *how* to tell them and *in which language*, then queues
a row per channel and hands it to the dispatcher. Rows are persisted before
sending so a delivery failure is visible and retryable rather than lost.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from civicos.core.clock import utcnow
from civicos.core.config import get_settings
from civicos.core.i18n import translate
from civicos.core.pagination import PageParams
from civicos.domain.enums import NotificationChannel, NotificationStatus
from civicos.domain.identity import User
from civicos.domain.operations import Notification
from civicos.integrations.notifications import Envelope, get_dispatcher

logger = structlog.get_logger(__name__)

#: Channels that need a destination address rather than an account.
_ADDRESSED = {
    NotificationChannel.SMS,
    NotificationChannel.WHATSAPP,
    NotificationChannel.EMAIL,
    NotificationChannel.VOICE,
}

MAX_ATTEMPTS = 3


@dataclass(slots=True)
class Recipient:
    """Someone to notify - an account, a bare phone number, or both."""

    user: User | None = None
    phone: str | None = None
    email: str | None = None
    language: str = "en"
    channels: list[NotificationChannel] | None = None

    @classmethod
    def for_user(cls, user: User) -> Recipient:
        return cls(
            user=user,
            phone=user.phone,
            email=user.email,
            language=user.language,
            channels=[
                NotificationChannel(channel)
                for channel in user.notification_channels
                if channel in set(NotificationChannel)
            ]
            or [NotificationChannel.IN_APP],
        )

    def destination_for(self, channel: NotificationChannel) -> str | None:
        if channel in {
            NotificationChannel.SMS,
            NotificationChannel.WHATSAPP,
            NotificationChannel.VOICE,
        }:
            return self.phone
        if channel is NotificationChannel.EMAIL:
            return self.email
        return None


async def notify(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    recipients: Sequence[Recipient],
    *,
    template_key: str,
    params: dict[str, Any] | None = None,
    subject: str | None = None,
    body: str | None = None,
    channels: Sequence[NotificationChannel] | None = None,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    action_url: str | None = None,
    send_now: bool = True,
) -> list[Notification]:
    """Queue (and optionally send) a notification to several recipients.

    ``template_key`` is looked up in the i18n catalogue per recipient, so the
    same event reaches each person in their own language.
    """
    settings = get_settings()
    params = params or {}
    created: list[Notification] = []

    for recipient in recipients:
        target_channels = list(
            channels
            or recipient.channels
            or [NotificationChannel(c) for c in settings.notifications.default_channels]
        )
        message = body or translate(template_key, recipient.language, **params)

        for channel in target_channels:
            destination = recipient.destination_for(channel)
            if channel in _ADDRESSED and not destination:
                continue  # no address for this channel; skip silently
            if recipient.user is None and channel is NotificationChannel.IN_APP:
                continue  # an in-app message with nobody to show it to

            notification = Notification(
                tenant_id=tenant_id,
                user_id=recipient.user.id if recipient.user else None,
                destination=destination,
                channel=channel,
                template_key=template_key,
                subject=subject,
                body=message,
                language=recipient.language,
                action_url=action_url,
                entity_type=entity_type,
                entity_id=entity_id,
            )
            session.add(notification)
            created.append(notification)

    await session.flush()

    if send_now:
        for notification in created:
            await deliver(session, notification)
    return created


async def deliver(session: AsyncSession, notification: Notification) -> bool:
    """Attempt delivery of a single queued notification."""
    dispatcher = get_dispatcher()
    notification.attempts += 1

    result = await dispatcher.send(
        notification.channel,
        Envelope(
            destination=notification.destination or "",
            body=notification.body,
            subject=notification.subject,
            language=notification.language,
            action_url=notification.action_url,
            metadata={
                "entity_type": notification.entity_type,
                "entity_id": str(notification.entity_id) if notification.entity_id else None,
                "template": notification.template_key,
            },
        ),
    )

    now = utcnow()
    if result.suppressed:
        notification.status = NotificationStatus.SUPPRESSED
        notification.error = result.error
    elif result.delivered:
        notification.status = NotificationStatus.SENT
        notification.sent_at = now
        notification.provider_message_id = result.provider_message_id
        notification.error = None
    else:
        notification.status = NotificationStatus.FAILED
        notification.failed_at = now
        notification.error = result.error
        logger.warning(
            "notification_delivery_failed",
            channel=str(notification.channel),
            error=result.error,
            attempts=notification.attempts,
        )

    await session.flush()
    return result.delivered


async def retry_failed(session: AsyncSession, limit: int = 100) -> int:
    """Re-attempt failed notifications that still have attempts left."""
    rows = (
        await session.scalars(
            select(Notification)
            .where(
                Notification.status == NotificationStatus.FAILED,
                Notification.attempts < MAX_ATTEMPTS,
            )
            .order_by(Notification.created_at.asc())
            .limit(limit)
        )
    ).all()

    delivered = 0
    for notification in rows:
        if await deliver(session, notification):
            delivered += 1
    if rows:
        logger.info("notification_retry_batch", attempted=len(rows), delivered=delivered)
    return delivered


async def list_for_user(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    unread_only: bool = False,
    page: PageParams | None = None,
) -> tuple[Sequence[Notification], int]:
    statement = select(Notification).where(
        Notification.tenant_id == tenant_id,
        Notification.user_id == user_id,
        Notification.channel == NotificationChannel.IN_APP,
    )
    if unread_only:
        statement = statement.where(Notification.read_at.is_(None))

    total = int(await session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
    statement = statement.order_by(Notification.created_at.desc())
    if page:
        statement = statement.offset(page.offset).limit(page.limit)
    return (await session.scalars(statement)).all(), total


async def mark_read(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    notification_ids: list[uuid.UUID] | None = None,
) -> int:
    """Mark some (or all) in-app notifications as read."""
    statement = (
        update(Notification)
        .where(
            Notification.tenant_id == tenant_id,
            Notification.user_id == user_id,
            Notification.read_at.is_(None),
        )
        .values(read_at=utcnow(), status=NotificationStatus.READ)
    )
    if notification_ids:
        statement = statement.where(Notification.id.in_(notification_ids))
    result = await session.execute(statement)
    return int(result.rowcount or 0)


async def unread_count(session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.tenant_id == tenant_id,
                Notification.user_id == user_id,
                Notification.channel == NotificationChannel.IN_APP,
                Notification.read_at.is_(None),
            )
        )
        or 0
    )


def recipient_from_contact(
    phone: str | None, email: str | None, language: str = "en"
) -> Recipient | None:
    """Build a recipient for an anonymous reporter who left a contact detail."""
    if not phone and not email:
        return None
    channels: list[NotificationChannel] = []
    if phone:
        channels.append(NotificationChannel.SMS)
    if email:
        channels.append(NotificationChannel.EMAIL)
    return Recipient(phone=phone, email=email, language=language, channels=channels)
