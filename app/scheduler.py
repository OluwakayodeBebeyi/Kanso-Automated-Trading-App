"""Persistent, idempotent local paper-report scheduler."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.reporting import build_report, export_report

LOG = logging.getLogger(__name__)
PERIODS = ("daily", "weekly", "monthly", "annual")


def period_bounds(period: str, reference_day: date) -> tuple[date, date]:
    """Return the last fully completed calendar period before reference_day."""
    if period == "daily":
        day = reference_day - timedelta(days=1)
        return day, day
    if period == "weekly":
        end = reference_day - timedelta(days=reference_day.weekday() + 1)
        return end - timedelta(days=6), end
    if period == "monthly":
        first_this_month = reference_day.replace(day=1)
        end = first_this_month - timedelta(days=1)
        return end.replace(day=1), end
    if period == "annual":
        end = date(reference_day.year - 1, 12, 31)
        return date(end.year, 1, 1), end
    raise ValueError(f"Unsupported report period: {period}")


class ReportScheduler:
    """Creates reports at local schedule boundaries with DB-backed deduplication."""

    def __init__(
        self,
        store,
        output_dir="reports",
        timezone_name="Africa/Lagos",
        report_time="08:00",
        formats=("csv", "json", "html"),
        max_attempts=3,
        stale_claim_minutes=15,
        notifier=None,
    ):
        try:
            self.zone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"Unknown reporting timezone: {timezone_name}") from error
        try:
            hour, minute = (int(part) for part in report_time.split(":"))
            self.report_time = time(hour, minute)
        except (ValueError, TypeError) as error:
            raise ValueError("REPORT_TIME must use HH:MM 24-hour format") from error
        if max_attempts < 1 or stale_claim_minutes < 1:
            raise ValueError("Retry and claim time limits must be positive")
        self.store = store
        self.output_dir = Path(output_dir)
        self.formats = tuple(formats)
        self.max_attempts = max_attempts
        self.stale_claim_minutes = stale_claim_minutes
        self.notifier = notifier
        with self.store.connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS report_runs(
                    run_key TEXT PRIMARY KEY,
                    period TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    artifacts TEXT,
                    error TEXT
                )"""
            )

    def _due(self, period, local_now):
        if local_now.timetz().replace(tzinfo=None) < self.report_time:
            return False
        if period == "weekly":
            return local_now.weekday() == 0
        if period == "monthly":
            return local_now.day == 1
        if period == "annual":
            return local_now.month == 1 and local_now.day == 1
        return True

    def _claim(self, run_key, period, start, end, now):
        stamp = now.astimezone(timezone.utc).isoformat()
        stale_before = (
            now.astimezone(timezone.utc) - timedelta(minutes=self.stale_claim_minutes)
        ).isoformat()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO report_runs(run_key,period,start_date,end_date,status,attempts,updated_at) VALUES(?,?,?,?,?,?,?)",
                (
                    run_key,
                    period,
                    start.isoformat(),
                    end.isoformat(),
                    "pending",
                    0,
                    stamp,
                ),
            )
            row = db.execute(
                "SELECT * FROM report_runs WHERE run_key=?", (run_key,)
            ).fetchone()
            if row["status"] == "completed" or row["attempts"] >= self.max_attempts:
                return False
            if row["status"] == "running" and row["updated_at"] > stale_before:
                return False
            db.execute(
                "UPDATE report_runs SET status='running', attempts=attempts+1, updated_at=?, error=NULL WHERE run_key=?",
                (stamp, run_key),
            )
            return True

    def run_period(self, period, now=None, prices=None):
        """Generate/retry one completed period; return artifacts or an empty list."""
        if period not in PERIODS:
            raise ValueError(f"Unsupported report period: {period}")
        instant = now or datetime.now(self.zone)
        if instant.tzinfo is None:
            raise ValueError("Scheduler timestamp must be timezone-aware")
        local_now = instant.astimezone(self.zone)
        start, end = period_bounds(period, local_now.date())
        run_key = f"{period}:{start.isoformat()}:{end.isoformat()}"
        if not self._claim(run_key, period, start, end, local_now):
            LOG.info("Report skipped (already completed or claimed): %s", run_key)
            return []
        try:
            report = build_report(
                self.store, prices, period=period, start=start, end=end
            )
            artifacts = export_report(report, self.output_dir, self.formats)
            with self.store.connect() as db:
                db.execute(
                    "UPDATE report_runs SET status='completed',updated_at=?,artifacts=?,error=NULL WHERE run_key=?",
                    (
                        datetime.now(timezone.utc).isoformat(),
                        json.dumps([str(path) for path in artifacts]),
                        run_key,
                    ),
                )
            LOG.info("Report generated: %s (%s)", run_key, len(artifacts))
            if self.notifier is not None:
                from app.notifications import Notification

                self.notifier.send(
                    Notification(
                        f"report:{period}:{start}:{end}",
                        "report",
                        "Paper report available",
                        f"{period.title()} paper report for {start} through {end} is ready.",
                    )
                )
            return artifacts
        except Exception as error:
            with self.store.connect() as db:
                db.execute(
                    "UPDATE report_runs SET status='failed',updated_at=?,error=? WHERE run_key=?",
                    (
                        datetime.now(timezone.utc).isoformat(),
                        f"{type(error).__name__}: {error}",
                        run_key,
                    ),
                )
            LOG.exception("Report generation failed: %s", run_key)
            return []

    def run_due(self, now=None, force=False):
        instant = now or datetime.now(self.zone)
        if instant.tzinfo is None:
            raise ValueError("Scheduler timestamp must be timezone-aware")
        local_now = instant.astimezone(self.zone)
        if self.notifier is not None:
            from app.notifications import Notification

            for event in self.notifier.history():
                if event["category"] == "report" and event["status"] == "failed":
                    parts = event["event_key"].split(":")
                    if len(parts) == 4:
                        period, start, end = parts[1:]
                        self.notifier.send(
                            Notification(
                                event["event_key"],
                                "report",
                                event["title"],
                                f"{period.title()} paper report for {start} through {end} is ready.",
                            ),
                            local_now,
                        )
        completed = {}
        for period in PERIODS:
            if force or self._due(period, local_now):
                completed[period] = self.run_period(period, local_now)
        return completed

    def history(self):
        with self.store.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM report_runs ORDER BY start_date DESC,period"
                )
            ]


def main():
    """Run a due check; Windows Task Scheduler can invoke this periodically."""
    import argparse
    import os
    import time as sleep_time
    from datetime import time

    from app.config import Settings
    from app.database import Store
    from app.notifications import (
        EmailSMTPAdapter,
        NotificationPreferences,
        NotificationService,
        TelegramAdapter,
    )

    parser = argparse.ArgumentParser(description="Paper report scheduler")
    parser.add_argument("--once", action="store_true", help="Check due reports once")
    parser.add_argument(
        "--interval", type=int, default=300, help="Loop interval seconds"
    )
    args = parser.parse_args()
    if args.interval < 10:
        parser.error("--interval must be at least 10 seconds")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    settings = Settings.from_env()
    store = Store(settings.database_path, settings.initial_cash)
    adapter = None
    try:
        if os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_CHAT_ID"):
            adapter = TelegramAdapter.from_env()
        elif any(
            os.getenv(name)
            for name in (
                "SMTP_HOST",
                "SMTP_USERNAME",
                "SMTP_PASSWORD",
                "SMTP_FROM",
                "SMTP_TO",
            )
        ):
            adapter = EmailSMTPAdapter.from_env()
    except ValueError:
        logging.warning(
            "Notification configuration is incomplete; reports will still run"
        )
    notifier = None
    if adapter is not None:
        preferences = NotificationPreferences(
            timezone_name=os.getenv("REPORT_TIMEZONE", "Africa/Lagos"),
            categories=frozenset(
                x.strip()
                for x in os.getenv("NOTIFICATION_CATEGORIES", "report").split(",")
                if x.strip()
            ),
            quiet_start=time.fromisoformat(
                os.getenv("NOTIFICATION_QUIET_START", "22:00")
            ),
            quiet_end=time.fromisoformat(os.getenv("NOTIFICATION_QUIET_END", "07:00")),
            max_per_hour=int(os.getenv("NOTIFICATION_MAX_PER_HOUR", "20")),
        )
        notifier = NotificationService(store, adapter, preferences)
    scheduler = ReportScheduler(
        store,
        timezone_name=os.getenv("REPORT_TIMEZONE", "Africa/Lagos"),
        report_time=os.getenv("REPORT_TIME", "08:00"),
        max_attempts=int(os.getenv("REPORT_MAX_ATTEMPTS", "3")),
        notifier=notifier,
    )
    while True:
        scheduler.run_due()
        if args.once:
            return
        sleep_time.sleep(args.interval)


if __name__ == "__main__":
    main()
