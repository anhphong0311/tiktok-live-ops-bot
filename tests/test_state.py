from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tiktok_zalo_bot.models import (
    LiveObservation,
    LiveState,
    NotificationEvent,
    NotificationStatus,
)
from tiktok_zalo_bot.state import StateStore


NOW = datetime(2026, 10, 15, 3, 0, tzinfo=UTC)


def observation(
    state: LiveState, room_id: str | None = None, offset: int = 0
) -> LiveObservation:
    return LiveObservation(state, NOW + timedelta(seconds=offset), room_id, "test")


def test_room_id_deduplicates_across_repeated_checks_and_restart(tmp_path):
    path = tmp_path / "state.db"
    store = StateStore(path)
    first = store.record_observation(
        observation(LiveState.LIVE, "123"), "bohiubimsua", "message"
    )
    second = store.record_observation(
        observation(LiveState.LIVE, "123", 60), "bohiubimsua", "different"
    )
    restarted = StateStore(path)
    third = restarted.record_observation(
        observation(LiveState.LIVE, "123", 120), "bohiubimsua", "again"
    )

    assert first is not None
    assert first.id == second.id == third.id
    assert first.dedupe_key == "room:123"
    assert len(restarted.list_notifications()) == 1
    assert third.message == "message"


def test_unknown_does_not_turn_live_into_offline_or_open_new_fallback_session(tmp_path):
    store = StateStore(tmp_path / "state.db")
    first = store.record_observation(
        observation(LiveState.LIVE), "bohiubimsua", "message"
    )
    store.record_observation(
        observation(LiveState.UNKNOWN, offset=60), "bohiubimsua"
    )
    second = store.record_observation(
        observation(LiveState.LIVE, offset=120), "bohiubimsua", "message2"
    )

    assert first is not None and second is not None
    assert first.id == second.id
    assert len(store.list_notifications()) == 1


def test_definitive_offline_allows_next_fallback_live_session(tmp_path):
    store = StateStore(tmp_path / "state.db")
    first = store.record_observation(
        observation(LiveState.LIVE), "bohiubimsua", "one"
    )
    store.record_observation(
        observation(LiveState.OFFLINE, offset=60), "bohiubimsua"
    )
    second = store.record_observation(
        observation(LiveState.LIVE, offset=120), "bohiubimsua", "two"
    )

    assert first is not None and second is not None
    assert first.id != second.id
    assert len(store.list_notifications()) == 2


def test_reused_room_id_never_opens_duplicate_start_notification(tmp_path):
    store = StateStore(tmp_path / "state.db")
    first = store.record_observation(
        observation(LiveState.LIVE, "123"), "bohiubimsua", "one"
    )
    store.record_observation(
        observation(LiveState.OFFLINE, offset=60), "bohiubimsua"
    )
    second = store.record_observation(
        observation(LiveState.LIVE, "123", offset=120), "bohiubimsua", "two"
    )

    assert first is not None and second is not None
    assert first.id == second.id
    assert first.dedupe_key == "room:123"
    assert second.dedupe_key == "room:123"
    assert len(store.list_notifications()) == 1


def test_offline_requires_configured_confirmations_before_ending(tmp_path):
    store = StateStore(tmp_path / "state.db")
    started = store.record_observation(
        observation(LiveState.LIVE, "stable-room"), "bohiubimsua", "started"
    )

    first_offline = store.record_observation(
        observation(LiveState.OFFLINE, offset=60),
        "bohiubimsua",
        end_message="ended",
        offline_confirmations=3,
    )
    resumed = store.record_observation(
        observation(LiveState.LIVE, "stable-room", offset=120),
        "bohiubimsua",
        "duplicate",
        offline_confirmations=3,
    )

    assert started is not None and resumed is not None
    assert first_offline is None
    assert started.id == resumed.id
    assert len(store.list_notifications()) == 1
    assert store.get_monitor_state()["offline_streak"] == 0


def test_restart_repairs_legacy_timestamped_active_room_key(tmp_path):
    path = tmp_path / "state.db"
    store = StateStore(path)
    started = store.record_observation(
        observation(LiveState.LIVE, "repair-room"), "bohiubimsua", "started"
    )
    assert started is not None

    with store._connect() as db:
        db.execute(
            """
            UPDATE monitor_state
            SET active_session_key = ?, active_started_at = ?
            WHERE singleton = 1
            """,
            ("room:repair-room:started:later", observation(LiveState.LIVE, offset=90).checked_at.isoformat(timespec="seconds")),
        )

    restarted = StateStore(path)
    monitor = restarted.get_monitor_state()
    assert monitor["active_session_key"] == "room:repair-room"
    assert monitor["active_started_at"] == NOW.isoformat(timespec="seconds")


def test_live_to_offline_creates_one_deduplicated_end_notification(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.record_observation(
        observation(LiveState.LIVE, "room-1"), "bohiubimsua", "started"
    )
    ended = store.record_observation(
        observation(LiveState.OFFLINE, offset=3600),
        "bohiubimsua",
        end_message="ended",
    )
    repeated = store.record_observation(
        observation(LiveState.OFFLINE, offset=3660),
        "bohiubimsua",
        end_message="must not replace",
    )

    assert ended is not None and repeated is not None
    assert ended.id == repeated.id
    assert ended.event_type is NotificationEvent.LIVE_ENDED
    assert ended.live_started_at == NOW.isoformat(timespec="seconds")
    assert ended.message == "ended"
    assert len(store.list_notifications()) == 2


def test_unknown_does_not_end_live_session(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.record_observation(
        observation(LiveState.LIVE, "room-2"), "bohiubimsua", "started"
    )
    result = store.record_observation(
        observation(LiveState.UNKNOWN, offset=60),
        "bohiubimsua",
        end_message="ended",
    )

    assert result is None
    assert len(store.list_notifications()) == 1
    assert store.get_monitor_state()["last_definitive_state"] == LiveState.LIVE.value


def test_interrupted_send_is_quarantined_and_not_actionable(tmp_path):
    store = StateStore(tmp_path / "state.db")
    item = store.record_observation(
        observation(LiveState.LIVE, "55"), "bohiubimsua", "message"
    )
    assert item is not None
    store.update_notification(item.id, NotificationStatus.SENDING)

    assert store.recover_inflight() == 1
    assert store.get_actionable(send_enabled=True) is None
    assert store.list_notifications()[0]["status"] == NotificationStatus.UNCERTAIN.value


def test_resolve_uncertain_requires_explicit_outcome(tmp_path):
    store = StateStore(tmp_path / "state.db")
    item = store.record_observation(
        observation(LiveState.LIVE, "99"), "bohiubimsua", "message"
    )
    assert item is not None
    store.update_notification(item.id, NotificationStatus.UNCERTAIN)
    store.resolve_uncertain(item.id, "sent")

    assert store.list_notifications()[0]["status"] == NotificationStatus.SENT.value


def test_viewer_alert_requires_confirmations_and_does_not_spam(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.record_observation(
        observation(LiveState.LIVE, "viewer-room"), "bohiubimsua", "started"
    )

    first = store.record_viewer_observation(99, 100, "low", confirmations=2)
    second = store.record_viewer_observation(98, 100, "low", confirmations=2)
    assert second is not None
    store.update_notification(second.id, NotificationStatus.SENT)
    repeated = store.record_viewer_observation(80, 100, "must not send", confirmations=2)

    assert first is None
    assert second.event_type is NotificationEvent.VIEWERS_LOW
    assert repeated is None
    assert len(store.list_notifications()) == 2


def test_viewer_alert_rearms_only_after_recovery_margin(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.record_observation(
        observation(LiveState.LIVE, "viewer-room"), "bohiubimsua", "started"
    )
    first = store.record_viewer_observation(
        90, 100, "low", confirmations=1, reminder_minutes=0
    )
    assert first is not None
    store.update_notification(first.id, NotificationStatus.SENT)

    store.record_viewer_observation(
        105, 100, "between", confirmations=1, recovery_margin=10,
        reminder_minutes=0,
    )
    assert store.record_viewer_observation(
        90, 100, "still same episode", confirmations=1, reminder_minutes=0
    ) is None

    store.record_viewer_observation(
        110, 100, "recovered", confirmations=1, recovery_margin=10,
        reminder_minutes=0,
    )
    again = store.record_viewer_observation(
        90, 100, "low again", confirmations=1, reminder_minutes=0
    )

    assert again is not None
    assert again.dedupe_key.endswith(":2")
    assert len(store.list_notifications()) == 3


def test_changing_viewer_threshold_starts_a_new_low_check(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.record_observation(
        observation(LiveState.LIVE, "viewer-room"), "bohiubimsua", "started"
    )
    store.record_viewer_observation(40, 50, "below 50", confirmations=1)

    changed = store.record_viewer_observation(100, 130, "below 130", confirmations=1)

    assert changed is not None
    assert changed.dedupe_key.endswith(":2")


def test_low_viewer_alert_repeats_after_reminder_interval(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.record_observation(
        observation(LiveState.LIVE, "viewer-room"), "bohiubimsua", "started"
    )
    first = store.record_viewer_observation(
        90, 100, "first", confirmations=1, reminder_minutes=15
    )
    assert first is not None
    store.update_notification(first.id, NotificationStatus.SENT)
    old_sent_at = (datetime.now(UTC) - timedelta(minutes=16)).isoformat(
        timespec="seconds"
    )
    with store._connect() as db:
        db.execute(
            "UPDATE notifications SET sent_at = ? WHERE id = ?",
            (old_sent_at, first.id),
        )

    reminder = store.record_viewer_observation(
        93, 100, "reminder", confirmations=1, reminder_minutes=15
    )

    assert reminder is not None
    assert reminder.dedupe_key.endswith(":2")
    assert reminder.message == "reminder"


def test_recovery_does_not_bypass_low_viewer_cooldown(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.record_observation(
        observation(LiveState.LIVE, "viewer-room"), "bohiubimsua", "started"
    )
    first = store.record_viewer_observation(
        90, 100, "first", confirmations=1, reminder_minutes=15
    )
    assert first is not None
    store.update_notification(first.id, NotificationStatus.SENT)

    # A real recovery re-arms the episode, but must not bypass the global
    # 15-minute minimum between messages.
    store.record_viewer_observation(
        110, 100, "recovered", confirmations=1, recovery_margin=10,
        reminder_minutes=15,
    )
    too_soon = store.record_viewer_observation(
        90, 100, "new episode", confirmations=1, recovery_margin=10,
        reminder_minutes=15,
    )

    assert too_soon is None
    assert len(store.list_notifications()) == 2

