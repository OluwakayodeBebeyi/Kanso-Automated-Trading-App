from datetime import datetime, time, timezone

from app.database import Store
from app.notifications import Notification, NotificationPreferences, NotificationService


class FakeAdapter:
    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.delivered = []

    def deliver(self, notification):
        if self.fail_times:
            self.fail_times -= 1
            raise OSError("private transport detail")
        self.delivered.append(notification)


def test_notifications_are_idempotent_and_retry_failures():
    store = Store(":memory:", 1000)
    adapter = FakeAdapter(fail_times=1)
    service = NotificationService(store, adapter)
    event = Notification("order-1", "execution", "Paper fill", "Simulated order filled")
    now = datetime(2024, 4, 1, tzinfo=timezone.utc)
    assert service.send(event, now) is False
    assert service.history()[0]["status"] == "failed"
    assert "private transport detail" not in str(service.history())
    assert service.send(event, now) is True
    assert service.send(event, now) is False
    assert len(adapter.delivered) == 1
    assert service.history()[0]["attempts"] == 2


def test_quiet_hours_category_and_frequency_preferences():
    store = Store(":memory:", 1000)
    adapter = FakeAdapter()
    prefs = NotificationPreferences(
        timezone_name="Africa/Lagos",
        categories=frozenset({"risk"}),
        quiet_start=time(22),
        quiet_end=time(7),
        max_per_hour=1,
    )
    service = NotificationService(store, adapter, prefs)
    night = datetime(2024, 4, 1, 23, tzinfo=timezone.utc)
    assert not service.send(Notification("quiet", "risk", "Risk", "Stopped"), night)
    day = datetime(2024, 4, 1, 12, tzinfo=timezone.utc)
    assert not service.send(
        Notification("category", "signal", "Signal", "Candidate"), day
    )
    assert service.send(Notification("risk-1", "risk", "Risk", "Stopped"), day)
    assert not service.send(Notification("risk-2", "risk", "Risk", "Stopped"), day)
    assert [item["status"] for item in service.history()].count("delivered") == 1
