from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from tiktok_zalo_bot.dashboard import (
    DashboardAccessError,
    DashboardSnapshot,
    ProductRevenue,
)
from tiktok_zalo_bot.gmv_service import GmvReportService, build_report_messages
from tiktok_zalo_bot.gmv_state import GmvStateStore
from tiktok_zalo_bot.models import LiveObservation, LiveState, NotificationStatus
from tiktok_zalo_bot.zalo import DeliveryResult


def run_immediate(coroutine):
    """Drive coroutines that do not perform external asynchronous I/O."""
    try:
        coroutine.send(None)
    except StopIteration as finished:
        return finished.value
    raise AssertionError("Test coroutine unexpectedly awaited external I/O")


def make_config(tmp_path: Path) -> dict:
    return {
        "_base_dir": str(tmp_path),
        "_config_path": str(tmp_path / "config.yaml"),
        "tiktok": {
            "username": "bohiubimsua",
            "live_url": "https://www.tiktok.com/@bohiubimsua/live",
            "request_timeout_seconds": 20,
        },
        "notification": {"timezone": "Asia/Bangkok"},
        "zalo": {
            "group_name": "Chạy mắt",
            "group_confirmed": True,
            "send_enabled": True,
            "dry_run": False,
        },
        "runtime": {"zalo_lock_path": "data/zalo_ui.lock"},
        "gmv_report": {
            "enabled": True,
            "interval_minutes": 60,
            "poll_interval_seconds": 60,
            "offline_confirmations": 2,
            "error_reminder_minutes": 30,
            "dashboard_retry_minutes": 5,
            "max_message_chars": 1800,
            "max_retry_before_send": 3,
            "max_messages_per_cycle": 20,
            "dashboard": {},
            "zalo": {
                "group_name": "GMV - BHBS",
                "group_confirmed": True,
                "send_enabled": True,
                "dry_run": False,
            },
        },
    }


def snapshot(
    room_id: str, at: datetime, *, started_at: datetime | None = None
) -> DashboardSnapshot:
    return DashboardSnapshot(
        room_id=room_id,
        captured_at=at,
        attributed_gmv_vnd=8_533_273,
        ads_cost_vnd=157_390,
        products=(
            ProductRevenue("2", "Product B", 424_724),
            ProductRevenue("1", "Product A", 281_714),
        ),
        started_at=started_at or at,
        dashboard_time=at,
    )


class FakeDashboard:
    def __init__(self, value: DashboardSnapshot | Exception):
        self.value = value
        self.calls: list[str] = []

    def fetch(self, room_id: str) -> DashboardSnapshot:
        self.calls.append(room_id)
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class FakeZalo:
    def __init__(self):
        self.messages: list[str] = []

    def deliver(self, message: str, *, real_send: bool) -> DeliveryResult:
        assert real_send
        self.messages.append(message)
        return DeliveryResult(False, Path("gmv.png"))


def test_hourly_report_is_anchored_to_live_start(tmp_path: Path) -> None:
    started = datetime(2026, 11, 20, 2, 31, tzinfo=UTC)
    store = GmvStateStore(tmp_path / "gmv.db")
    dashboard = FakeDashboard(snapshot("room", started))
    zalo = FakeZalo()
    service = GmvReportService(
        make_config(tmp_path), store, dashboard=dashboard, zalo=zalo
    )

    run_immediate(
        service.run_once(
            LiveObservation(LiveState.LIVE, started, "room"), now=started
        )
    )
    assert zalo.messages == []

    one_hour = started + timedelta(hours=1)
    dashboard.value = snapshot("room", one_hour, started_at=started)
    run_immediate(
        service.run_once(
            LiveObservation(LiveState.LIVE, one_hour, "room"), now=one_hour
        )
    )

    assert len(zalo.messages) == 1
    assert "MỐC 1 GIỜ" in zalo.messages[0]
    assert "8.533.273đ" in zalo.messages[0]
    assert "1.84%" in zalo.messages[0]
    assert store.list_notifications()[0]["status"] == NotificationStatus.SENT.value


def test_final_report_is_sent_after_confirmed_offline(tmp_path: Path) -> None:
    started = datetime(2026, 11, 20, 2, 31, tzinfo=UTC)
    store = GmvStateStore(tmp_path / "gmv.db")
    dashboard = FakeDashboard(snapshot("room", started))
    zalo = FakeZalo()
    service = GmvReportService(
        make_config(tmp_path), store, dashboard=dashboard, zalo=zalo
    )
    run_immediate(service.run_once(LiveObservation(LiveState.LIVE, started, "room")))

    ended = started + timedelta(hours=2)
    dashboard.value = snapshot("room", ended)
    run_immediate(service.run_once(LiveObservation(LiveState.OFFLINE, ended), now=ended))
    assert zalo.messages == []
    run_immediate(service.run_once(LiveObservation(LiveState.OFFLINE, ended), now=ended))

    assert any("TỔNG KẾT PHIÊN LIVE" in message for message in zalo.messages)
    session = store.get_session("room")
    assert session and session["final_enqueued"] == 1


def test_dashboard_error_notice_and_recovery_are_not_spammed(tmp_path: Path) -> None:
    started = datetime(2026, 11, 20, 2, 31, tzinfo=UTC)
    store = GmvStateStore(tmp_path / "gmv.db")
    dashboard = FakeDashboard(DashboardAccessError("hết đăng nhập"))
    zalo = FakeZalo()
    service = GmvReportService(
        make_config(tmp_path), store, dashboard=dashboard, zalo=zalo
    )
    live = LiveObservation(LiveState.LIVE, started, "room")

    run_immediate(service.run_once(live, now=started))
    run_immediate(service.run_once(live, now=started + timedelta(minutes=1)))
    assert len(zalo.messages) == 1
    assert "KHÔNG TRUY CẬP" in zalo.messages[0]

    recovered_at = started + timedelta(minutes=5)
    dashboard.value = snapshot("room", recovered_at)
    run_immediate(service.run_once(live, now=recovered_at))
    assert len(zalo.messages) == 2
    assert "ĐÃ HOẠT ĐỘNG LẠI" in zalo.messages[1]


def test_long_product_list_is_split_without_dropping_products(tmp_path: Path) -> None:
    started = datetime(2026, 11, 20, 2, 31, tzinfo=UTC)
    products = tuple(
        ProductRevenue(str(index), f"Sản phẩm rất dài số {index} " * 4, index * 1_000)
        for index in range(1, 21)
    )
    value = DashboardSnapshot(
        room_id="room",
        captured_at=started,
        attributed_gmv_vnd=1_000_000,
        ads_cost_vnd=100_000,
        products=products,
    )

    messages = build_report_messages(
        value,
        started_at=started,
        report_time=started + timedelta(hours=1),
        timezone=ZoneInfo("Asia/Bangkok"),
        max_message_chars=600,
    )

    assert len(messages) > 2
    joined = "\n".join(messages)
    for index in range(1, 21):
        assert f"🆔 {index} |" in joined
