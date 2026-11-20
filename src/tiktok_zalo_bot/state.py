from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator

from .models import (
    LiveObservation,
    LiveState,
    Notification,
    NotificationEvent,
    NotificationStatus,
)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class StateStore:
    """Durable monitor state and transactional notification outbox."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=15000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS monitor_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    observed_state TEXT NOT NULL,
                    last_definitive_state TEXT,
                    active_session_key TEXT,
                    current_room_id TEXT,
                    active_started_at TEXT,
                    offline_streak INTEGER NOT NULL DEFAULT 0,
                    last_end_notification_key TEXT,
                    viewer_session_key TEXT,
                    viewer_low_streak INTEGER NOT NULL DEFAULT 0,
                    viewer_alert_active INTEGER NOT NULL DEFAULT 0,
                    viewer_alert_sequence INTEGER NOT NULL DEFAULT 0,
                    last_viewer_count INTEGER,
                    last_viewer_threshold INTEGER,
                    last_check_at TEXT,
                    last_detail TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    room_id TEXT,
                    live_started_at TEXT NOT NULL,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    sent_at TEXT,
                    event_type TEXT NOT NULL DEFAULT 'LIVE_STARTED'
                );

                CREATE INDEX IF NOT EXISTS idx_notifications_status
                ON notifications(status, id);
                """
            )
            now = utc_now_iso()
            db.execute(
                """
                INSERT OR IGNORE INTO monitor_state (
                    singleton, observed_state, updated_at
                ) VALUES (1, ?, ?)
                """,
                (LiveState.UNKNOWN.value, now),
            )
            monitor_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(monitor_state)")
            }
            if "active_started_at" not in monitor_columns:
                db.execute("ALTER TABLE monitor_state ADD COLUMN active_started_at TEXT")
            if "last_end_notification_key" not in monitor_columns:
                db.execute(
                    "ALTER TABLE monitor_state ADD COLUMN last_end_notification_key TEXT"
                )
            if "offline_streak" not in monitor_columns:
                db.execute(
                    "ALTER TABLE monitor_state ADD COLUMN offline_streak "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            viewer_columns = {
                "viewer_session_key": "TEXT",
                "viewer_low_streak": "INTEGER NOT NULL DEFAULT 0",
                "viewer_alert_active": "INTEGER NOT NULL DEFAULT 0",
                "viewer_alert_sequence": "INTEGER NOT NULL DEFAULT 0",
                "last_viewer_count": "INTEGER",
                "last_viewer_threshold": "INTEGER",
            }
            for column, sql_type in viewer_columns.items():
                if column not in monitor_columns:
                    db.execute(
                        f"ALTER TABLE monitor_state ADD COLUMN {column} {sql_type}"
                    )
            notification_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(notifications)")
            }
            if "event_type" not in notification_columns:
                db.execute(
                    "ALTER TABLE notifications ADD COLUMN event_type TEXT "
                    "NOT NULL DEFAULT 'LIVE_STARTED'"
                )
            db.execute(
                """
                UPDATE monitor_state
                SET active_started_at = (
                    SELECT live_started_at
                    FROM notifications
                    WHERE dedupe_key = monitor_state.active_session_key
                    LIMIT 1
                )
                WHERE singleton = 1
                  AND active_started_at IS NULL
                  AND active_session_key IS NOT NULL
                """
            )
            # Repair state produced by older versions that opened timestamped
            # sessions whenever TikTok briefly reported OFFLINE for a room
            # that was still live. Prefer the stable room key and its original
            # start time so restarting the fixed service cannot send again.
            db.execute(
                """
                UPDATE monitor_state
                SET active_session_key = 'room:' || current_room_id,
                    active_started_at = (
                        SELECT live_started_at
                        FROM notifications
                        WHERE dedupe_key = 'room:' || monitor_state.current_room_id
                        LIMIT 1
                    ),
                    offline_streak = 0
                WHERE singleton = 1
                  AND current_room_id IS NOT NULL
                  AND active_session_key != 'room:' || current_room_id
                  AND EXISTS (
                      SELECT 1 FROM notifications
                      WHERE dedupe_key = 'room:' || monitor_state.current_room_id
                  )
                """
            )

    def record_observation(
        self,
        observation: LiveObservation,
        username: str,
        message: str | None = None,
        end_message: str | None = None,
        offline_confirmations: int = 1,
    ) -> Notification | None:
        """Persist observation and create one outbox row per live session."""
        checked_at = observation.checked_at.isoformat(timespec="seconds")
        now = utc_now_iso()
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM monitor_state WHERE singleton = 1"
            ).fetchone()
            active_key = row["active_session_key"]
            last_definitive = row["last_definitive_state"]
            active_started_at = row["active_started_at"]
            current_room_id = row["current_room_id"]
            last_end_key = row["last_end_notification_key"]
            offline_streak = int(row["offline_streak"] or 0)

            if observation.state is LiveState.UNKNOWN:
                db.execute(
                    """
                    UPDATE monitor_state
                    SET observed_state = ?, last_check_at = ?, last_detail = ?,
                        updated_at = ?
                    WHERE singleton = 1
                    """,
                    (observation.state.value, checked_at, observation.detail, now),
                )
                return None

            if observation.state is LiveState.OFFLINE:
                notification_row = None
                end_key = None
                if (
                    last_definitive == LiveState.LIVE.value
                    and active_key
                    and active_started_at
                ):
                    next_offline_streak = offline_streak + 1
                    required = max(1, int(offline_confirmations))
                    if next_offline_streak < required:
                        db.execute(
                            """
                            UPDATE monitor_state
                            SET observed_state = ?, offline_streak = ?,
                                last_check_at = ?, last_detail = ?, updated_at = ?
                            WHERE singleton = 1
                            """,
                            (
                                observation.state.value,
                                next_offline_streak,
                                checked_at,
                                observation.detail,
                                now,
                            ),
                        )
                        return None
                    if end_message is not None:
                        end_key = f"end:{active_key}"
                        db.execute(
                            """
                            INSERT OR IGNORE INTO notifications (
                                dedupe_key, room_id, live_started_at, message, status,
                                created_at, updated_at, event_type
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                end_key,
                                current_room_id,
                                active_started_at,
                                end_message,
                                NotificationStatus.NEW.value,
                                now,
                                now,
                                NotificationEvent.LIVE_ENDED.value,
                            ),
                        )
                        notification_row = db.execute(
                            "SELECT * FROM notifications WHERE dedupe_key = ?",
                            (end_key,),
                        ).fetchone()
                elif last_end_key:
                    notification_row = db.execute(
                        "SELECT * FROM notifications WHERE dedupe_key = ?",
                        (last_end_key,),
                    ).fetchone()
                db.execute(
                    """
                    UPDATE monitor_state
                    SET observed_state = ?, last_definitive_state = ?,
                        active_session_key = NULL, current_room_id = NULL,
                        active_started_at = NULL, offline_streak = 0,
                        last_end_notification_key = COALESCE(?, last_end_notification_key),
                        last_check_at = ?, last_detail = ?, updated_at = ?
                    WHERE singleton = 1
                    """,
                    (
                        observation.state.value,
                        observation.state.value,
                        end_key,
                        checked_at,
                        observation.detail,
                        now,
                    ),
                )
                return self._to_notification(notification_row) if notification_row else None

            existing_started_at = None
            if observation.room_id:
                # A TikTok room ID identifies one real LIVE session. Keep the
                # key stable even when the availability endpoint briefly
                # flaps OFFLINE/UNKNOWN, so one room can never send duplicate
                # start notifications.
                dedupe_key = f"room:{observation.room_id}"
                existing = db.execute(
                    "SELECT live_started_at FROM notifications WHERE dedupe_key = ?",
                    (dedupe_key,),
                ).fetchone()
                if existing:
                    existing_started_at = existing["live_started_at"]
            elif active_key and last_definitive == LiveState.LIVE.value:
                dedupe_key = str(active_key)
            else:
                dedupe_key = f"fallback:{username}:{checked_at}"

            session_started_at = (
                str(existing_started_at)
                if existing_started_at
                else (
                    active_started_at
                    if active_key == dedupe_key and active_started_at
                    else checked_at
                )
            )

            db.execute(
                """
                UPDATE monitor_state
                SET observed_state = ?, last_definitive_state = ?,
                    active_session_key = ?, current_room_id = ?,
                    active_started_at = ?, offline_streak = 0,
                    last_end_notification_key = NULL,
                    last_check_at = ?, last_detail = ?, updated_at = ?
                WHERE singleton = 1
                """,
                (
                    observation.state.value,
                    observation.state.value,
                    dedupe_key,
                    observation.room_id,
                    session_started_at,
                    checked_at,
                    observation.detail,
                    now,
                ),
            )
            if message is None:
                return None
            db.execute(
                """
                INSERT OR IGNORE INTO notifications (
                    dedupe_key, room_id, live_started_at, message, status,
                    created_at, updated_at, event_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dedupe_key,
                    observation.room_id,
                    checked_at,
                    message,
                    NotificationStatus.NEW.value,
                    now,
                    now,
                    NotificationEvent.LIVE_STARTED.value,
                ),
            )
            notification_row = db.execute(
                "SELECT * FROM notifications WHERE dedupe_key = ?", (dedupe_key,)
            ).fetchone()
            return self._to_notification(notification_row)

    def record_viewer_observation(
        self,
        viewer_count: int,
        minimum_viewers: int,
        message: str,
        *,
        confirmations: int = 2,
        recovery_margin: int = 10,
        reminder_minutes: int = 15,
    ) -> Notification | None:
        """Create low-viewer alerts with hysteresis and a hard send cooldown."""
        now = utc_now_iso()
        required = max(1, int(confirmations))
        threshold = max(0, int(minimum_viewers))
        recovery_at = threshold + max(0, int(recovery_margin))
        reminder_after = timedelta(minutes=max(0, int(reminder_minutes)))
        count = max(0, int(viewer_count))

        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM monitor_state WHERE singleton = 1"
            ).fetchone()
            session_key = row["active_session_key"]
            if (
                not session_key
                or row["last_definitive_state"] != LiveState.LIVE.value
            ):
                return None

            same_session = row["viewer_session_key"] == session_key
            same_threshold = row["last_viewer_threshold"] == threshold
            low_streak = int(row["viewer_low_streak"] or 0)
            alert_active = bool(row["viewer_alert_active"])
            sequence = int(row["viewer_alert_sequence"] or 0)
            if not same_session:
                low_streak = 0
                alert_active = False
                sequence = 0
            elif not same_threshold:
                low_streak = 0
                alert_active = False

            # The reminder interval is also a hard minimum between successful
            # low-viewer notifications. Without this check, a brief recovery
            # above the hysteresis margin followed by another drop started a
            # new episode and could send again before 15 minutes had elapsed.
            cooldown_elapsed = True
            if reminder_after.total_seconds() > 0:
                latest_sent = db.execute(
                    """
                    SELECT sent_at FROM notifications
                    WHERE event_type = ? AND room_id = ? AND status = ?
                        AND sent_at IS NOT NULL
                    ORDER BY sent_at DESC LIMIT 1
                    """,
                    (
                        NotificationEvent.VIEWERS_LOW.value,
                        row["current_room_id"],
                        NotificationStatus.SENT.value,
                    ),
                ).fetchone()
                if latest_sent is not None:
                    last_sent_at = datetime.fromisoformat(str(latest_sent["sent_at"]))
                    current_at = datetime.fromisoformat(now)
                    cooldown_elapsed = current_at - last_sent_at >= reminder_after

            notification_row = None
            if count < threshold:
                if not alert_active:
                    low_streak += 1
                    if low_streak >= required and cooldown_elapsed:
                        sequence += 1
                        dedupe_key = f"viewers-low:{session_key}:{sequence}"
                        db.execute(
                            """
                            INSERT OR IGNORE INTO notifications (
                                dedupe_key, room_id, live_started_at, message, status,
                                created_at, updated_at, event_type
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                dedupe_key,
                                row["current_room_id"],
                                row["active_started_at"] or now,
                                message,
                                NotificationStatus.NEW.value,
                                now,
                                now,
                                NotificationEvent.VIEWERS_LOW.value,
                            ),
                        )
                        notification_row = db.execute(
                            "SELECT * FROM notifications WHERE dedupe_key = ?",
                            (dedupe_key,),
                        ).fetchone()
                        alert_active = True
                elif sequence > 0:
                    current_key = f"viewers-low:{session_key}:{sequence}"
                    current_alert = db.execute(
                        "SELECT * FROM notifications WHERE dedupe_key = ?",
                        (current_key,),
                    ).fetchone()
                    if current_alert is not None and current_alert["status"] in {
                        NotificationStatus.NEW.value,
                        NotificationStatus.PREVIEWED.value,
                        NotificationStatus.FAILED_SAFE.value,
                    }:
                        notification_row = current_alert
                    elif (
                        current_alert is not None
                        and current_alert["status"] == NotificationStatus.SENT.value
                        and reminder_after.total_seconds() > 0
                        and cooldown_elapsed
                    ):
                        sequence += 1
                        dedupe_key = f"viewers-low:{session_key}:{sequence}"
                        db.execute(
                            """
                            INSERT OR IGNORE INTO notifications (
                                dedupe_key, room_id, live_started_at, message,
                                status, created_at, updated_at, event_type
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                dedupe_key,
                                row["current_room_id"],
                                row["active_started_at"] or now,
                                message,
                                NotificationStatus.NEW.value,
                                now,
                                now,
                                NotificationEvent.VIEWERS_LOW.value,
                            ),
                        )
                        notification_row = db.execute(
                            "SELECT * FROM notifications WHERE dedupe_key = ?",
                            (dedupe_key,),
                        ).fetchone()
            else:
                low_streak = 0
                if count >= recovery_at:
                    alert_active = False

            db.execute(
                """
                UPDATE monitor_state
                SET viewer_session_key = ?, viewer_low_streak = ?,
                    viewer_alert_active = ?, viewer_alert_sequence = ?,
                    last_viewer_count = ?, last_viewer_threshold = ?, updated_at = ?
                WHERE singleton = 1
                """,
                (
                    session_key,
                    low_streak,
                    int(alert_active),
                    sequence,
                    count,
                    threshold,
                    now,
                ),
            )
            return self._to_notification(notification_row) if notification_row else None

    def get_actionable(self, send_enabled: bool) -> Notification | None:
        statuses = (
            [NotificationStatus.NEW.value, NotificationStatus.PREVIEWED.value,
             NotificationStatus.FAILED_SAFE.value]
            if send_enabled
            else [NotificationStatus.NEW.value, NotificationStatus.FAILED_SAFE.value]
        )
        placeholders = ",".join("?" for _ in statuses)
        with self._connect() as db:
            row = db.execute(
                f"SELECT * FROM notifications WHERE status IN ({placeholders}) "
                "ORDER BY id LIMIT 1",
                statuses,
            ).fetchone()
        return self._to_notification(row) if row else None

    def recover_inflight(self) -> int:
        """Quarantine interrupted sends; they are never retried automatically."""
        now = utc_now_iso()
        with self._connect() as db:
            cursor = db.execute(
                """
                UPDATE notifications
                SET status = ?, last_error = COALESCE(last_error,
                    'Process stopped while send result was unknown'), updated_at = ?
                WHERE status = ?
                """,
                (
                    NotificationStatus.UNCERTAIN.value,
                    now,
                    NotificationStatus.SENDING.value,
                ),
            )
            return int(cursor.rowcount)

    def update_notification(
        self,
        notification_id: int,
        status: NotificationStatus,
        *,
        error: str | None = None,
        increment_attempts: bool = False,
    ) -> None:
        now = utc_now_iso()
        with self._connect() as db:
            db.execute(
                """
                UPDATE notifications
                SET status = ?, last_error = ?, updated_at = ?,
                    attempts = attempts + ?,
                    sent_at = CASE WHEN ? = ? THEN ? ELSE sent_at END
                WHERE id = ?
                """,
                (
                    status.value,
                    error,
                    now,
                    int(increment_attempts),
                    status.value,
                    NotificationStatus.SENT.value,
                    now,
                    notification_id,
                ),
            )

    def get_monitor_state(self) -> dict[str, str | None]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM monitor_state WHERE singleton = 1"
            ).fetchone()
        return dict(row)

    def list_notifications(self, limit: int = 20) -> list[dict[str, object]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def resolve_uncertain(self, notification_id: int, outcome: str) -> None:
        with self._connect() as db:
            row = db.execute(
                "SELECT status FROM notifications WHERE id = ?", (notification_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"Không có notification id={notification_id}")
            if row["status"] not in {
                NotificationStatus.SENDING.value,
                NotificationStatus.UNCERTAIN.value,
            }:
                raise ValueError("Chỉ xử lý bản ghi SENDING/UNCERTAIN")
        target = (
            NotificationStatus.SENT
            if outcome == "sent"
            else NotificationStatus.FAILED_SAFE
        )
        self.update_notification(notification_id, target, error=None)

    @staticmethod
    def _to_notification(row: sqlite3.Row) -> Notification:
        return Notification(
            id=int(row["id"]),
            dedupe_key=str(row["dedupe_key"]),
            room_id=row["room_id"],
            live_started_at=str(row["live_started_at"]),
            message=str(row["message"]),
            status=NotificationStatus(row["status"]),
            attempts=int(row["attempts"]),
            last_error=row["last_error"],
            event_type=NotificationEvent(row["event_type"]),
        )

