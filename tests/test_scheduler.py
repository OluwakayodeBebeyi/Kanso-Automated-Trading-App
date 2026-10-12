from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.database import Store
from app.scheduler import ReportScheduler, period_bounds


def test_calendar_period_bounds_and_leap_year():
    assert period_bounds("daily", date(2024, 3, 1)) == (
        date(2024, 2, 29),
        date(2024, 2, 29),
    )
    assert period_bounds("weekly", date(2024, 1, 8)) == (
        date(2024, 1, 1),
        date(2024, 1, 7),
    )
    assert period_bounds("monthly", date(2024, 3, 1)) == (
        date(2024, 2, 1),
        date(2024, 2, 29),
    )
    assert period_bounds("annual", date(2024, 1, 1)) == (
        date(2023, 1, 1),
        date(2023, 12, 31),
    )


def test_daily_schedule_persistently_deduplicates_and_exports(tmp_path):
    store_path = str(tmp_path / "paper.sqlite3")
    store = Store(store_path, 1000)
    scheduler = ReportScheduler(
        store, tmp_path / "reports", formats=("csv", "json", "html")
    )
    instant = datetime(2024, 3, 1, 8, 1, tzinfo=ZoneInfo("Africa/Lagos"))
    outputs = scheduler.run_due(instant)
    assert len(outputs["daily"]) == 3
    assert all(path.exists() for path in outputs["daily"])
    assert "weekly" not in outputs
    assert scheduler.run_period("daily", instant) == []
    assert scheduler.history()[0]["status"] == "completed"
    restarted = ReportScheduler(Store(store_path, 1000), tmp_path / "reports")
    assert restarted.run_period("daily", instant) == []
    assert len(restarted.history()) == len(outputs)


def test_due_windows_follow_local_calendar_and_timezone():
    scheduler = ReportScheduler(
        Store(":memory:", 1000), timezone_name="America/New_York", report_time="08:00"
    )
    before = datetime(2024, 1, 1, 12, 59, tzinfo=ZoneInfo("UTC"))
    after = datetime(2024, 1, 1, 13, 1, tzinfo=ZoneInfo("UTC"))
    assert not scheduler._due("daily", before.astimezone(scheduler.zone))
    assert scheduler._due("daily", after.astimezone(scheduler.zone))
    assert scheduler._due("weekly", after.astimezone(scheduler.zone))
    assert scheduler._due("monthly", after.astimezone(scheduler.zone))
    assert scheduler._due("annual", after.astimezone(scheduler.zone))


def test_scheduler_retries_failed_export_only_to_configured_attempts(
    tmp_path, monkeypatch
):
    import app.scheduler as module

    scheduler = ReportScheduler(Store(":memory:", 1000), tmp_path, max_attempts=2)
    instant = datetime(2024, 3, 1, 9, tzinfo=ZoneInfo("Africa/Lagos"))
    monkeypatch.setattr(
        module,
        "export_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk unavailable")),
    )
    assert scheduler.run_period("daily", instant) == []
    assert scheduler.history()[0]["status"] == "failed"
    assert scheduler.run_period("daily", instant) == []
    assert scheduler.history()[0]["attempts"] == 2
    assert scheduler.run_period("daily", instant) == []
    assert scheduler.history()[0]["attempts"] == 2


class RecordingNotifier:
    def __init__(self):
        self.sent = []

    def send(self, notification, now=None):
        self.sent.append(notification)
        return True

    def history(self):
        return []


def test_scheduler_sends_one_report_availability_event(tmp_path):
    notifier = RecordingNotifier()
    scheduler = ReportScheduler(
        Store(":memory:", 1000), tmp_path, formats=("json",), notifier=notifier
    )
    instant = datetime(2024, 6, 4, 9, tzinfo=ZoneInfo("Africa/Lagos"))
    assert scheduler.run_period("daily", instant)
    assert len(notifier.sent) == 1
    assert notifier.sent[0].category == "report"
    scheduler.run_period("daily", instant)
    assert len(notifier.sent) == 1


def test_scheduler_retries_a_failed_report_notification(tmp_path):
    from app.notifications import NotificationService

    class FlakyAdapter:
        def __init__(self):
            self.calls = 0

        def deliver(self, notification):
            self.calls += 1
            if self.calls == 1:
                raise OSError("temporary transport failure")

    store = Store(":memory:", 1000)
    adapter = FlakyAdapter()
    notifier = NotificationService(store, adapter)
    scheduler = ReportScheduler(store, tmp_path, formats=("json",), notifier=notifier)
    instant = datetime(2024, 7, 3, 9, tzinfo=ZoneInfo("Africa/Lagos"))
    assert scheduler.run_period("daily", instant)
    assert notifier.history()[0]["status"] == "failed"
    scheduler.run_due(instant)
    assert adapter.calls == 2
    assert notifier.history()[0]["status"] == "delivered"


def test_daylight_saving_transition_uses_local_wall_clock():
    scheduler = ReportScheduler(
        Store(":memory:", 1000), timezone_name="America/New_York", report_time="03:00"
    )
    before_jump = datetime(2024, 3, 10, 6, 59, tzinfo=ZoneInfo("UTC"))
    after_jump = datetime(2024, 3, 10, 7, 1, tzinfo=ZoneInfo("UTC"))
    assert not scheduler._due("daily", before_jump.astimezone(scheduler.zone))
    assert scheduler._due("daily", after_jump.astimezone(scheduler.zone))
