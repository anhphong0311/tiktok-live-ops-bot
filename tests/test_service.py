from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from tiktok_zalo_bot.models import (
    LiveObservation,
    LiveState,
    NotificationEvent,
    NotificationStatus,
)
from tiktok_zalo_bot.service import NotificationService
from tiktok_zalo_bot.state import StateStore
from tiktok_zalo_bot.zalo import DeliveryResult, ZaloAutomationError


def make_config(tmp_path: Path, *, real_send: bool = False) -> dict:
    return {
        "_base_dir": str(tmp_path),
        "_config_path": str(tmp_path / "config.yaml"),
        "tiktok": {
            "username": "bohiubimsua",
            "live_url": "https://www.tiktok.com/@bohiubimsua/live",
            "poll_interval_seconds": 60,
            "request_timeout_seconds": 20,
        },
        "notification": {
            "timezone": "Asia/Bangkok",
            "template": "LIVE\n{time}\n{live_url}",
            "end_template": (
                "ENDED\n{start_time}\n{end_time}\n{duration}\n{live_url}"
            ),
        },
        "viewer_alert": {
            "enabled": True,
            "minimum_viewers": 100,
            "confirmations": 2,
            "recovery_margin": 10,
            "template": "LOW {viewer_count}/{minimum_viewers} {time} {live_url}",
        },
        "zalo": {
            "group_name": "Test group",
            "group_confirmed": True,
            "send_enabled": real_send,
            "dry_run": not real_send,
        },
        "runtime": {"max_retry_before_send": 3, "offline_confirmations": 3},
    }


class FakeZalo:
    def __init__(self, error: ZaloAutomationError | None = None):
        self.calls: list[bool] = []
        self.error = error

    def deliver(self, message: str, *, real_send: bool) -> DeliveryResult:
        self.calls.append(real_send)
        if self.error:
            raise self.error
        return DeliveryResult(not real_send, Path("preview.png"))


def run_immediate(coroutine):
    """Run a coroutine path that intentionally performs no asynchronous I/O."""
    try:
        coroutine.send(None)
    except StopIteration as finished:
        return finished.value
    raise AssertionError("Test coroutine unexpectedly awaited external I/O")


def test_dry_run_previews_only_once(tmp_path):
    store = StateStore(tmp_path / "state.db")
    zalo = FakeZalo()
    service = NotificationService(make_config(tmp_path), store, zalo=zalo)
    live = LiveObservation(LiveState.LIVE, datetime(2026, 10, 15, tzinfo=UTC), "1")

    run_immediate(service.run_once(live))
    run_immediate(service.run_once(live))

    assert zalo.calls == [False]
    assert store.list_notifications()[0]["status"] == NotificationStatus.PREVIEWED.value


def test_preview_can_be_sent_once_after_explicit_enable(tmp_path):
    store = StateStore(tmp_path / "state.db")
    config = make_config(tmp_path)
    zalo = FakeZalo()
    service = NotificationService(config, store, zalo=zalo)
    live = LiveObservation(LiveState.LIVE, datetime(2026, 10, 15, tzinfo=UTC), "2")
    run_immediate(service.run_once(live))

    config["zalo"]["send_enabled"] = True
    config["zalo"]["dry_run"] = False
    run_immediate(service.run_once(live))
    run_immediate(service.run_once(live))

    assert zalo.calls == [False, True]
    assert store.list_notifications()[0]["status"] == NotificationStatus.SENT.value


def test_uncertain_send_is_never_retried(tmp_path):
    store = StateStore(tmp_path / "state.db")
    zalo = FakeZalo(ZaloAutomationError("unknown", send_may_have_happened=True))
    service = NotificationService(make_config(tmp_path, real_send=True), store, zalo=zalo)
    live = LiveObservation(LiveState.LIVE, datetime(2026, 10, 15, tzinfo=UTC), "3")

    run_immediate(service.run_once(live))
    run_immediate(service.run_once(live))

    assert zalo.calls == [True]
    row = store.list_notifications()[0]
    assert row["status"] == NotificationStatus.UNCERTAIN.value
    assert row["attempts"] == 1


def test_unknown_does_not_create_notification(tmp_path):
    store = StateStore(tmp_path / "state.db")
    zalo = FakeZalo()
    service = NotificationService(make_config(tmp_path), store, zalo=zalo)
    unknown = LiveObservation(
        LiveState.UNKNOWN, datetime(2026, 10, 15, tzinfo=UTC), detail="network"
    )

    run_immediate(service.run_once(unknown))

    assert zalo.calls == []
    assert store.list_notifications() == []


def test_notification_text_and_timezone(tmp_path):
    service = NotificationService(make_config(tmp_path), StateStore(tmp_path / "state.db"))
    text = service.format_message(datetime(2026, 10, 15, 3, 4, 5, tzinfo=UTC))

    assert text == (
        "LIVE\n15/10/2026 10:04:05\n"
        "https://www.tiktok.com/@bohiubimsua/live"
    )


def test_live_end_is_sent_once_with_start_end_and_duration(tmp_path):
    store = StateStore(tmp_path / "state.db")
    zalo = FakeZalo()
    service = NotificationService(make_config(tmp_path, real_send=True), store, zalo=zalo)
    started_at = datetime(2026, 10, 15, 3, 4, 5, tzinfo=UTC)
    ended_at = datetime(2026, 10, 15, 5, 7, 9, tzinfo=UTC)

    run_immediate(
        service.run_once(LiveObservation(LiveState.LIVE, started_at, "end-room"))
    )
    for _ in range(3):
        run_immediate(
            service.run_once(LiveObservation(LiveState.OFFLINE, ended_at))
        )
    run_immediate(service.run_once(LiveObservation(LiveState.OFFLINE, ended_at)))

    assert zalo.calls == [True, True]
    rows = store.list_notifications()
    assert rows[0]["event_type"] == NotificationEvent.LIVE_ENDED.value
    assert rows[0]["status"] == NotificationStatus.SENT.value
    assert rows[0]["message"] == (
        "ENDED\n15/10/2026 10:04:05\n15/10/2026 12:07:09\n"
        "2 giờ 3 phút 4 giây\nhttps://www.tiktok.com/@bohiubimsua/live"
    )


def test_end_notification_retries_only_after_safe_failure(tmp_path):
    store = StateStore(tmp_path / "state.db")
    config = make_config(tmp_path, real_send=True)
    zalo = FakeZalo()
    service = NotificationService(config, store, zalo=zalo)
    started_at = datetime(2026, 10, 15, 3, 0, tzinfo=UTC)
    ended_at = datetime(2026, 10, 15, 4, 0, tzinfo=UTC)
    run_immediate(service.run_once(LiveObservation(LiveState.LIVE, started_at, "r")))

    zalo.error = ZaloAutomationError("locked", send_may_have_happened=False)
    for _ in range(3):
        run_immediate(service.run_once(LiveObservation(LiveState.OFFLINE, ended_at)))
    zalo.error = None
    run_immediate(service.run_once(LiveObservation(LiveState.OFFLINE, ended_at)))

    assert zalo.calls == [True, True, True]
    assert store.list_notifications()[0]["status"] == NotificationStatus.SENT.value


def test_false_offline_during_same_room_does_not_spam(tmp_path):
    store = StateStore(tmp_path / "state.db")
    zalo = FakeZalo()
    service = NotificationService(make_config(tmp_path, real_send=True), store, zalo=zalo)
    started_at = datetime(2026, 10, 15, 3, 0, tzinfo=UTC)

    run_immediate(
        service.run_once(LiveObservation(LiveState.LIVE, started_at, "same-room"))
    )
    run_immediate(
        service.run_once(LiveObservation(LiveState.OFFLINE, started_at))
    )
    run_immediate(
        service.run_once(LiveObservation(LiveState.LIVE, started_at, "same-room"))
    )

    assert zalo.calls == [True]
    assert len(store.list_notifications()) == 1


def test_low_viewer_alert_is_sent_once_after_two_checks(tmp_path):
    store = StateStore(tmp_path / "state.db")
    zalo = FakeZalo()
    service = NotificationService(make_config(tmp_path, real_send=True), store, zalo=zalo)
    checked_at = datetime(2026, 10, 15, 3, 4, 5, tzinfo=UTC)

    live = LiveObservation(LiveState.LIVE, checked_at, "low-room", "test", 90)
    run_immediate(service.run_once(live))
    run_immediate(service.run_once(live))
    run_immediate(service.run_once(live))
    run_immediate(service.run_once(live))

    assert zalo.calls == [True, True]
    rows = store.list_notifications()
    assert rows[0]["event_type"] == NotificationEvent.VIEWERS_LOW.value
    assert rows[0]["message"] == (
        "LOW 90/100 15/10/2026 10:04:05 "
        "https://www.tiktok.com/@bohiubimsua/live"
    )


def test_exhausted_start_notification_does_not_block_low_viewer_alert(tmp_path):
    store = StateStore(tmp_path / "state.db")
    config = make_config(tmp_path, real_send=True)
    zalo = FakeZalo(ZaloAutomationError("locked", send_may_have_happened=False))
    service = NotificationService(config, store, zalo=zalo)
    checked_at = datetime(2026, 10, 15, 3, 4, 5, tzinfo=UTC)
    live = LiveObservation(LiveState.LIVE, checked_at, "blocked-room", "test", 90)

    # Exhaust the start notification's safe retry budget.
    for _ in range(3):
        run_immediate(service.run_once(live))

    # Once the old notification is quarantined, later viewer checks must still
    # advance and produce their own alert.
    zalo.error = None
    run_immediate(service.run_once(live))
    run_immediate(service.run_once(live))

    rows = store.list_notifications()
    viewer_rows = [
        row for row in rows if row["event_type"] == NotificationEvent.VIEWERS_LOW.value
    ]
    assert len(viewer_rows) == 1
    assert viewer_rows[0]["status"] == NotificationStatus.SENT.value
    assert zalo.calls == [True, True, True, True]

