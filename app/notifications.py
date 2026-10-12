"""Optional, idempotent notifications for paper trading events."""

from __future__ import annotations

import logging
import os
import smtplib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from email.message import EmailMessage
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Notification:
    event_key: str
    category: str
    title: str
    message: str


@dataclass(frozen=True)
class NotificationPreferences:
    timezone_name: str = "Africa/Lagos"
    categories: frozenset[str] | None = None
    quiet_start: time | None = None
    quiet_end: time | None = None
    max_per_hour: int = 20


class NotificationAdapter(Protocol):
    def deliver(self, notification: Notification) -> None: ...


class EmailSMTPAdapter:
    """SMTP email adapter; credentials are read from environment variables."""

    def __init__(self, host, port, username, password, sender, recipient):
        self.host, self.port = host, int(port)
        self.username, self.password = username, password
        self.sender, self.recipient = sender, recipient

    @classmethod
    def from_env(cls):
        required = (
            "SMTP_HOST",
            "SMTP_USERNAME",
            "SMTP_PASSWORD",
            "SMTP_FROM",
            "SMTP_TO",
        )
        values = {name: os.getenv(name) for name in required}
        if any(not value for value in values.values()):
            raise ValueError(
                "Email notifications need SMTP_HOST/USERNAME/PASSWORD/FROM/TO"
            )
        return cls(
            values["SMTP_HOST"],
            os.getenv("SMTP_PORT", "587"),
            values["SMTP_USERNAME"],
            values["SMTP_PASSWORD"],
            values["SMTP_FROM"],
            values["SMTP_TO"],
        )

    def deliver(self, notification):
        message = EmailMessage()
        message["Subject"] = notification.title
        message["From"] = self.sender
        message["To"] = self.recipient
        message.set_content(notification.message)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=15) as client:
                client.starttls()
                client.login(self.username, self.password)
                client.send_message(message)
        except Exception:
            raise RuntimeError("Email notification delivery failed") from None


class TelegramAdapter:
    """Telegram bot adapter; the bot token is never logged or returned."""

    def __init__(self, token, chat_id, timeout=15):
        if not token or not chat_id:
            raise ValueError("Telegram token and chat ID are required")
        self._token, self.chat_id, self.timeout = token, str(chat_id), timeout

    @classmethod
    def from_env(cls):
        token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            raise ValueError(
                "Telegram notifications need TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID"
            )
        return cls(token, chat_id)

    def deliver(self, notification):
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        body = urllib.parse.urlencode(
            {
                "chat_id": self.chat_id,
                "text": f"{notification.title}\n{notification.message}",
            }
        ).encode()
        request = urllib.request.Request(url, data=body, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise RuntimeError("Telegram notification delivery failed")
        except Exception:
            raise RuntimeError("Telegram notification delivery failed") from None


class NotificationService:
    """Persist notification attempts and suppress duplicates/rate bursts."""

    def __init__(self, store, adapter, preferences=None, max_attempts=3):
        self.store = store
        self.adapter = adapter
        self.preferences = preferences or NotificationPreferences()
        self.max_attempts = max_attempts
        if max_attempts < 1 or self.preferences.max_per_hour < 1:
            raise ValueError("Notification retry and frequency limits must be positive")
        try:
            self.zone = ZoneInfo(self.preferences.timezone_name)
        except ZoneInfoNotFoundError as error:
            raise ValueError("Unknown notification timezone") from error
        with store.connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS notifications(
                    event_key TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error TEXT
                )"""
            )

    def _quiet(self, local_time):
        start, end = self.preferences.quiet_start, self.preferences.quiet_end
        if start is None or end is None or start == end:
            return False
        if start < end:
            return start <= local_time < end
        return local_time >= start or local_time < end

    def _claim(self, notification, now):
        stamp = now.astimezone(timezone.utc).isoformat()
        stale_before = (
            now.astimezone(timezone.utc) - timedelta(minutes=15)
        ).isoformat()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO notifications VALUES(?,?,?,'pending',0,?,?,NULL)",
                (
                    notification.event_key,
                    notification.category,
                    notification.title,
                    stamp,
                    stamp,
                ),
            )
            row = db.execute(
                "SELECT * FROM notifications WHERE event_key=?",
                (notification.event_key,),
            ).fetchone()
            if row["status"] == "delivered" or row["attempts"] >= self.max_attempts:
                return False
            if row["status"] == "sending" and row["updated_at"] > stale_before:
                return False
            local_now = now.astimezone(self.zone)
            if self._quiet(local_now.timetz().replace(tzinfo=None)):
                db.execute(
                    "UPDATE notifications SET status='skipped',updated_at=?,error='quiet hours' WHERE event_key=?",
                    (stamp, notification.event_key),
                )
                return False
            if (
                self.preferences.categories is not None
                and notification.category not in self.preferences.categories
            ):
                db.execute(
                    "UPDATE notifications SET status='skipped',updated_at=?,error='category disabled' WHERE event_key=?",
                    (stamp, notification.event_key),
                )
                return False
            hour_ago = (now.astimezone(timezone.utc) - timedelta(hours=1)).isoformat()
            count = db.execute(
                "SELECT COUNT(*) FROM notifications WHERE status='delivered' AND updated_at>=?",
                (hour_ago,),
            ).fetchone()[0]
            if count >= self.preferences.max_per_hour:
                db.execute(
                    "UPDATE notifications SET status='skipped',updated_at=?,error='frequency limit' WHERE event_key=?",
                    (stamp, notification.event_key),
                )
                return False
            db.execute(
                "UPDATE notifications SET status='sending',attempts=attempts+1,updated_at=?,error=NULL WHERE event_key=?",
                (stamp, notification.event_key),
            )
            return True

    def send(self, notification, now=None):
        if not isinstance(notification, Notification):
            raise TypeError("send expects a Notification")
        if not notification.event_key.strip() or not notification.category.strip():
            raise ValueError("Notification event key and category are required")
        if len(notification.title) > 120 or len(notification.message) > 1000:
            raise ValueError("Notification title or message is too long")
        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            raise ValueError("Notification timestamp must be timezone-aware")
        if not self._claim(notification, instant):
            LOG.info(
                "Notification skipped or already claimed: %s", notification.event_key
            )
            return False
        try:
            self.adapter.deliver(notification)
        except Exception as error:
            with self.store.connect() as db:
                db.execute(
                    "UPDATE notifications SET status='failed',updated_at=?,error=? WHERE event_key=?",
                    (
                        datetime.now(timezone.utc).isoformat(),
                        type(error).__name__,
                        notification.event_key,
                    ),
                )
            LOG.error(
                "Notification delivery failed for event %s (%s)",
                notification.event_key,
                type(error).__name__,
            )
            return False
        with self.store.connect() as db:
            db.execute(
                "UPDATE notifications SET status='delivered',updated_at=?,error=NULL WHERE event_key=?",
                (datetime.now(timezone.utc).isoformat(), notification.event_key),
            )
        LOG.info("Notification delivered: %s", notification.event_key)
        return True

    def history(self):
        with self.store.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT event_key,category,title,status,attempts,created_at,updated_at,error FROM notifications ORDER BY created_at DESC"
                )
            ]
