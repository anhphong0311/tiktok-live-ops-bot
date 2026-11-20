from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator, Sequence

from .dashboard import DashboardSnapshot, ProductRevenue
from .models import NotificationStatus


def _utc_iso(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ReportNotification:
    id: int
    dedupe_key: str
    room_id: str
    event_type: str
    part_number: int
    message: str
    status: NotificationStatus
    attempts: int
    last_error: str | None


class GmvStateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=15000")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS gmv_sessions (
                    room_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    next_report_at TEXT NOT NULL,
                    report_sequence INTEGER NOT NULL DEFAULT 0,
                    next_product_report_at TEXT,
                    product_sequence INTEGER NOT NULL DEFAULT 0,
                    live_state TEXT NOT NULL DEFAULT 'LIVE',
                    offline_streak INTEGER NOT NULL DEFAULT 0,
                    ended_at TEXT,
                    final_enqueued INTEGER NOT NULL DEFAULT 0,
                    dashboard_error_active INTEGER NOT NULL DEFAULT 0,
                    last_error_notice_at TEXT,
                    last_dashboard_error TEXT,
                    last_snapshot_json TEXT,
                    last_product_snapshot_json TEXT,
                    last_dashboard_attempt_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS gmv_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    room_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    part_number INTEGER NOT NULL DEFAULT 1,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    sent_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_gmv_outbox_status
                ON gmv_outbox(status, id);
                """
            )
            self._ensure_column(db, "next_product_report_at", "TEXT")
            self._ensure_column(
                db, "product_sequence", "INTEGER NOT NULL DEFAULT 0"
            )
            self._ensure_column(db, "last_product_snapshot_json", "TEXT")

    @staticmethod
    def _ensure_column(
        db: sqlite3.Connection, column_name: str, definition: str
    ) -> None:
        columns = {
            str(row["name"])
            for row in db.execute("PRAGMA table_info(gmv_sessions)").fetchall()
        }
        if column_name not in columns:
            db.execute(
                f"ALTER TABLE gmv_sessions ADD COLUMN {column_name} {definition}"
            )

    def observe_live(
        self,
        room_id: str,
        observed_at: datetime,
        interval_minutes: int,
        product_interval_minutes: int = 5,
    ) -> dict[str, object]:
        room_id = str(room_id)
        observed = observed_at.astimezone(UTC)
        next_report = observed + timedelta(minutes=max(1, interval_minutes))
        next_product = observed + timedelta(
            minutes=max(1, product_interval_minutes)
        )
        now = _utc_iso()
        with self._connect() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO gmv_sessions (
                    room_id, started_at, next_report_at,
                    next_product_report_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    room_id,
                    _utc_iso(observed),
                    _utc_iso(next_report),
                    _utc_iso(next_product),
                    now,
                ),
            )
            db.execute(
                """
                UPDATE gmv_sessions
                SET live_state = 'LIVE', offline_streak = 0,
                    next_product_report_at = COALESCE(next_product_report_at, ?),
                    updated_at = ?
                WHERE room_id = ? AND final_enqueued = 0
                """,
                (_utc_iso(next_product), now, room_id),
            )
            row = db.execute(
                "SELECT * FROM gmv_sessions WHERE room_id = ?", (room_id,)
            ).fetchone()
        return dict(row)

    def align_started_at(
        self,
        room_id: str,
        started_at: datetime,
        interval_minutes: int,
    ) -> dict[str, object]:
        interval = timedelta(minutes=max(1, interval_minutes))
        started = started_at.astimezone(UTC)
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM gmv_sessions WHERE room_id = ?", (room_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Không có phiên room_id={room_id}")
            if int(row["report_sequence"] or 0) == 0:
                db.execute(
                    """
                    UPDATE gmv_sessions
                    SET started_at = ?, next_report_at = ?, updated_at = ?
                    WHERE room_id = ?
                    """,
                    (
                        _utc_iso(started),
                        _utc_iso(started + interval),
                        _utc_iso(),
                        room_id,
                    ),
                )
            row = db.execute(
                "SELECT * FROM gmv_sessions WHERE room_id = ?", (room_id,)
            ).fetchone()
        return dict(row)

    def get_session(self, room_id: str) -> dict[str, object] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM gmv_sessions WHERE room_id = ?", (str(room_id),)
            ).fetchone()
        return dict(row) if row else None

    def get_pending_final_session(self) -> dict[str, object] | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT * FROM gmv_sessions
                WHERE live_state = 'ENDED' AND final_enqueued = 0
                ORDER BY updated_at LIMIT 1
                """
            ).fetchone()
        return dict(row) if row else None

    def get_active_session(self) -> dict[str, object] | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT * FROM gmv_sessions
                WHERE live_state = 'LIVE' AND final_enqueued = 0
                ORDER BY updated_at DESC LIMIT 1
                """
            ).fetchone()
        return dict(row) if row else None

    def mark_dashboard_attempt(self, room_id: str, attempted_at: datetime) -> None:
        with self._connect() as db:
            db.execute(
                """
                UPDATE gmv_sessions SET last_dashboard_attempt_at = ?, updated_at = ?
                WHERE room_id = ?
                """,
                (_utc_iso(attempted_at), _utc_iso(), str(room_id)),
            )

    def dashboard_retry_due(
        self, room_id: str, now: datetime, retry_minutes: int
    ) -> bool:
        row = self.get_session(room_id)
        if not row:
            return False
        last = row.get("last_dashboard_attempt_at")
        if not last:
            return True
        return now.astimezone(UTC) - datetime.fromisoformat(str(last)) >= timedelta(
            minutes=max(1, retry_minutes)
        )

    def hourly_due(self, room_id: str, now: datetime) -> bool:
        row = self.get_session(room_id)
        return bool(
            row
            and row["live_state"] == "LIVE"
            and now.astimezone(UTC) >= datetime.fromisoformat(str(row["next_report_at"]))
        )

    def product_due(self, room_id: str, now: datetime) -> bool:
        row = self.get_session(room_id)
        next_report = row.get("next_product_report_at") if row else None
        return bool(
            row
            and row["live_state"] == "LIVE"
            and next_report
            and now.astimezone(UTC) >= datetime.fromisoformat(str(next_report))
        )

    def complete_product_check(
        self,
        room_id: str,
        snapshot: DashboardSnapshot,
        messages: Sequence[str],
        now: datetime,
        interval_minutes: int,
    ) -> int:
        interval = timedelta(minutes=max(1, interval_minutes))
        now_utc = now.astimezone(UTC)
        payload = self._snapshot_payload(snapshot)
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM gmv_sessions WHERE room_id = ?", (room_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Không có phiên room_id={room_id}")
            sequence = int(row["product_sequence"] or 0)
            if messages:
                sequence += 1
                self._insert_messages(
                    db,
                    room_id,
                    f"product:{room_id}:{sequence}",
                    "PRODUCT_DELTA",
                    messages,
                )
            next_report_raw = row["next_product_report_at"]
            next_report = (
                datetime.fromisoformat(str(next_report_raw))
                if next_report_raw
                else now_utc + interval
            )
            while next_report <= now_utc:
                next_report += interval
            db.execute(
                """
                UPDATE gmv_sessions
                SET product_sequence = ?, next_product_report_at = ?,
                    last_product_snapshot_json = ?, updated_at = ?
                WHERE room_id = ?
                """,
                (
                    sequence,
                    _utc_iso(next_report),
                    json.dumps(payload, ensure_ascii=False),
                    _utc_iso(),
                    room_id,
                ),
            )
        return sequence

    def enqueue_hourly(
        self,
        room_id: str,
        messages: Sequence[str],
        now: datetime,
        interval_minutes: int,
    ) -> int:
        interval = timedelta(minutes=max(1, interval_minutes))
        now_utc = now.astimezone(UTC)
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM gmv_sessions WHERE room_id = ?", (room_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Không có phiên room_id={room_id}")
            sequence = int(row["report_sequence"] or 0) + 1
            self._insert_messages(
                db,
                room_id,
                f"hourly:{room_id}:{sequence}",
                "HOURLY_REPORT",
                messages,
            )
            next_report = datetime.fromisoformat(str(row["next_report_at"]))
            while next_report <= now_utc:
                next_report += interval
            db.execute(
                """
                UPDATE gmv_sessions
                SET report_sequence = ?, next_report_at = ?, updated_at = ?
                WHERE room_id = ?
                """,
                (sequence, _utc_iso(next_report), _utc_iso(), room_id),
            )
            return sequence

    def note_offline(
        self,
        room_id: str,
        observed_at: datetime,
        confirmations: int,
    ) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM gmv_sessions WHERE room_id = ?", (room_id,)
            ).fetchone()
            if row is None or int(row["final_enqueued"] or 0):
                return False
            streak = int(row["offline_streak"] or 0) + 1
            ended = streak >= max(1, confirmations)
            db.execute(
                """
                UPDATE gmv_sessions
                SET offline_streak = ?, live_state = ?,
                    ended_at = CASE WHEN ? THEN COALESCE(ended_at, ?) ELSE ended_at END,
                    updated_at = ?
                WHERE room_id = ?
                """,
                (
                    streak,
                    "ENDED" if ended else str(row["live_state"]),
                    int(ended),
                    _utc_iso(observed_at),
                    _utc_iso(),
                    room_id,
                ),
            )
        return ended

    def enqueue_final(self, room_id: str, messages: Sequence[str]) -> None:
        with self._connect() as db:
            row = db.execute(
                "SELECT final_enqueued FROM gmv_sessions WHERE room_id = ?", (room_id,)
            ).fetchone()
            if row is None or int(row["final_enqueued"] or 0):
                return
            self._insert_messages(
                db, room_id, f"final:{room_id}", "FINAL_REPORT", messages
            )
            db.execute(
                """
                UPDATE gmv_sessions SET final_enqueued = 1, updated_at = ?
                WHERE room_id = ?
                """,
                (_utc_iso(), room_id),
            )

    def record_dashboard_failure(
        self,
        room_id: str,
        error: str,
        now: datetime,
        reminder_minutes: int,
    ) -> bool:
        now_utc = now.astimezone(UTC)
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM gmv_sessions WHERE room_id = ?", (room_id,)
            ).fetchone()
            if row is None:
                return False
            active = bool(row["dashboard_error_active"])
            last_notice = row["last_error_notice_at"]
            due = not active or not last_notice
            if last_notice and not due:
                due = now_utc - datetime.fromisoformat(str(last_notice)) >= timedelta(
                    minutes=max(1, reminder_minutes)
                )
            db.execute(
                """
                UPDATE gmv_sessions
                SET dashboard_error_active = 1, last_dashboard_error = ?,
                    last_error_notice_at = CASE WHEN ? THEN ? ELSE last_error_notice_at END,
                    updated_at = ?
                WHERE room_id = ?
                """,
                (error, int(due), _utc_iso(now_utc), _utc_iso(), room_id),
            )
        return due

    def record_dashboard_recovery(self, room_id: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT dashboard_error_active FROM gmv_sessions WHERE room_id = ?",
                (room_id,),
            ).fetchone()
            recovered = bool(row and row["dashboard_error_active"])
            if recovered:
                db.execute(
                    """
                    UPDATE gmv_sessions
                    SET dashboard_error_active = 0, last_dashboard_error = NULL,
                        updated_at = ? WHERE room_id = ?
                    """,
                    (_utc_iso(), room_id),
                )
        return recovered

    def enqueue_notice(
        self,
        room_id: str,
        event_type: str,
        dedupe_key: str,
        message: str,
    ) -> None:
        with self._connect() as db:
            self._insert_messages(
                db, room_id, dedupe_key, event_type, [message]
            )

    @staticmethod
    def _insert_messages(
        db: sqlite3.Connection,
        room_id: str,
        key_prefix: str,
        event_type: str,
        messages: Sequence[str],
    ) -> None:
        now = _utc_iso()
        for index, message in enumerate(messages, start=1):
            db.execute(
                """
                INSERT OR IGNORE INTO gmv_outbox (
                    dedupe_key, room_id, event_type, part_number, message,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"{key_prefix}:part:{index}",
                    room_id,
                    event_type,
                    index,
                    message,
                    NotificationStatus.NEW.value,
                    now,
                    now,
                ),
            )

    def save_snapshot(self, snapshot: DashboardSnapshot) -> None:
        payload = self._snapshot_payload(snapshot)
        with self._connect() as db:
            db.execute(
                """
                UPDATE gmv_sessions SET last_snapshot_json = ?, updated_at = ?
                WHERE room_id = ?
                """,
                (json.dumps(payload, ensure_ascii=False), _utc_iso(), snapshot.room_id),
            )

    def load_snapshot(self, room_id: str) -> DashboardSnapshot | None:
        row = self.get_session(room_id)
        if not row or not row.get("last_snapshot_json"):
            return None
        payload = json.loads(str(row["last_snapshot_json"]))
        return self._snapshot_from_payload(payload)

    def load_product_snapshot(self, room_id: str) -> DashboardSnapshot | None:
        row = self.get_session(room_id)
        if not row or not row.get("last_product_snapshot_json"):
            return None
        payload = json.loads(str(row["last_product_snapshot_json"]))
        return self._snapshot_from_payload(payload)

    @staticmethod
    def _snapshot_payload(snapshot: DashboardSnapshot) -> dict[str, object]:
        payload = asdict(snapshot)
        payload["captured_at"] = _utc_iso(snapshot.captured_at)
        payload["started_at"] = (
            _utc_iso(snapshot.started_at) if snapshot.started_at else None
        )
        payload["dashboard_time"] = (
            _utc_iso(snapshot.dashboard_time) if snapshot.dashboard_time else None
        )
        return payload

    @staticmethod
    def _snapshot_from_payload(payload: dict[str, object]) -> DashboardSnapshot:
        return DashboardSnapshot(
            room_id=str(payload["room_id"]),
            captured_at=datetime.fromisoformat(payload["captured_at"]),
            attributed_gmv_vnd=int(payload["attributed_gmv_vnd"]),
            ads_cost_vnd=int(payload["ads_cost_vnd"]),
            products=tuple(ProductRevenue(**item) for item in payload["products"]),
            started_at=(
                datetime.fromisoformat(payload["started_at"])
                if payload.get("started_at")
                else None
            ),
            dashboard_time=(
                datetime.fromisoformat(payload["dashboard_time"])
                if payload.get("dashboard_time")
                else None
            ),
        )

    def recover_inflight(self) -> int:
        with self._connect() as db:
            cursor = db.execute(
                """
                UPDATE gmv_outbox SET status = ?,
                    last_error = COALESCE(last_error, 'Process stopped during send'),
                    updated_at = ? WHERE status = ?
                """,
                (
                    NotificationStatus.UNCERTAIN.value,
                    _utc_iso(),
                    NotificationStatus.SENDING.value,
                ),
            )
            return int(cursor.rowcount)

    def get_actionable(self, max_attempts: int) -> ReportNotification | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT * FROM gmv_outbox
                WHERE status IN (?, ?) AND attempts < ?
                ORDER BY id LIMIT 1
                """,
                (
                    NotificationStatus.NEW.value,
                    NotificationStatus.FAILED_SAFE.value,
                    max(1, max_attempts),
                ),
            ).fetchone()
        return self._to_notification(row) if row else None

    def update_notification(
        self,
        notification_id: int,
        status: NotificationStatus,
        *,
        error: str | None = None,
        increment_attempts: bool = False,
    ) -> None:
        now = _utc_iso()
        with self._connect() as db:
            db.execute(
                """
                UPDATE gmv_outbox
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

    def list_notifications(self, limit: int = 20) -> list[dict[str, object]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM gmv_outbox ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _to_notification(row: sqlite3.Row) -> ReportNotification:
        return ReportNotification(
            id=int(row["id"]),
            dedupe_key=str(row["dedupe_key"]),
            room_id=str(row["room_id"]),
            event_type=str(row["event_type"]),
            part_number=int(row["part_number"]),
            message=str(row["message"]),
            status=NotificationStatus(row["status"]),
            attempts=int(row["attempts"]),
            last_error=row["last_error"],
        )
