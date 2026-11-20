from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class LiveState(StrEnum):
    UNKNOWN = "UNKNOWN"
    OFFLINE = "OFFLINE"
    LIVE = "LIVE"


class NotificationStatus(StrEnum):
    NEW = "NEW"
    PREVIEWED = "PREVIEWED"
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED_SAFE = "FAILED_SAFE"
    UNCERTAIN = "UNCERTAIN"


class NotificationEvent(StrEnum):
    LIVE_STARTED = "LIVE_STARTED"
    LIVE_ENDED = "LIVE_ENDED"
    VIEWERS_LOW = "VIEWERS_LOW"


@dataclass(frozen=True)
class LiveObservation:
    state: LiveState
    checked_at: datetime
    room_id: str | None = None
    detail: str | None = None
    viewer_count: int | None = None


@dataclass(frozen=True)
class Notification:
    id: int
    dedupe_key: str
    room_id: str | None
    live_started_at: str
    message: str
    status: NotificationStatus
    attempts: int
    last_error: str | None
    event_type: NotificationEvent = NotificationEvent.LIVE_STARTED

