from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from .models import LiveObservation, LiveState

logger = logging.getLogger(__name__)


class TikTokChecker:
    """Thin adapter around TikTokLive. Every exception maps to UNKNOWN."""

    VIETNAM_WEB_PARAMS = {
        "app_language": "vi",
        "browser_language": "vi-VN",
        "tz_name": "Asia/Ho_Chi_Minh",
        "priority_region": "VN",
        "region": "VN",
        "webcast_language": "vi",
    }

    def __init__(self, username: str, timeout_seconds: float = 20):
        self.username = username.lstrip("@")
        self.timeout_seconds = timeout_seconds
        self._viewer_client: Any | None = None
        self._viewer_task: asyncio.Task | None = None
        self._viewer_room_id: str | None = None
        self._viewer_ready: asyncio.Event | None = None
        self._latest_viewer_count: int | None = None

    def _make_client(self) -> Any:
        from TikTokLive import TikTokLiveClient

        return TikTokLiveClient(
            unique_id=self.username,
            # TikTokLive 7.x forwards this nested mapping to httpx.AsyncClient.
            # TikTokLive otherwise picks a random country preset at import
            # time. That made the public API severely undercount a Vietnam
            # livestream and caused false low-viewer alerts.
            web_kwargs={
                "httpx_kwargs": {
                    "timeout": self.timeout_seconds,
                    "params": dict(self.VIETNAM_WEB_PARAMS),
                }
            },
        )

    async def check(self) -> LiveObservation:
        checked_at = datetime.now(UTC)
        client: Any | None = None
        try:
            from TikTokLive.client.web.routes.fetch_room_id_api import (
                FetchRoomIdAPIRoute,
            )

            client = self._make_client()
            room_data = await FetchRoomIdAPIRoute.fetch_user_room_data(
                client.web, self.username
            )
            payload = room_data["data"]
            live_room = payload["liveRoom"]
            if str(live_room["status"]) == "4":
                await self._stop_viewer_stream()
                return LiveObservation(
                    state=LiveState.OFFLINE,
                    checked_at=checked_at,
                    detail="TikTok live room status is offline",
                )

            room_id = self._parse_room_id(payload)
            if room_id is None:
                room_id = await self._fetch_room_id(client)
            viewer_count = self._parse_viewer_count(payload)
            if viewer_count is None and room_id is not None:
                try:
                    viewer_count = await self._get_stream_viewer_count(room_id)
                except Exception as exc:
                    # The LIVE state and room ID from the public endpoint are
                    # still valid. A temporary websocket/signing failure must
                    # not turn the whole observation into UNKNOWN.
                    logger.warning(
                        "LIVE detected but realtime viewer count unavailable: %s: %s",
                        type(exc).__name__,
                        exc,
                    )
            return LiveObservation(
                state=LiveState.LIVE,
                checked_at=checked_at,
                room_id=room_id,
                detail="TikTok live room status is live",
                viewer_count=viewer_count,
            )
        except Exception as exc:  # network, CAPTCHA, API changes, rate limits
            logger.warning("TikTok check failed; state is UNKNOWN: %s", exc)
            return LiveObservation(
                state=LiveState.UNKNOWN,
                checked_at=checked_at,
                detail=f"{type(exc).__name__}: {exc}",
            )
        finally:
            if client is not None:
                try:
                    await client.web.close()
                except Exception as exc:
                    logger.debug("Could not close TikTok polling client: %s", exc)

    async def _get_stream_viewer_count(self, room_id: str) -> int | None:
        from TikTokLive.events import RoomUserSeqEvent

        needs_start = (
            self._viewer_client is None
            or self._viewer_task is None
            or self._viewer_task.done()
            or self._viewer_room_id != room_id
        )
        if needs_start:
            await self._stop_viewer_stream()
            client = self._make_client()
            ready = asyncio.Event()

            async def on_viewer_count(event: Any) -> None:
                count = self._parse_stream_viewer_count(event)
                if count is None:
                    return
                self._latest_viewer_count = count
                ready.set()

            client.add_listener(RoomUserSeqEvent, on_viewer_count)
            try:
                task = await client.start(
                    room_id=int(room_id),
                    fetch_live_check=False,
                    fetch_room_info=False,
                    process_connect_events=True,
                )
            except Exception:
                await client.web.close()
                raise

            self._viewer_client = client
            self._viewer_task = task
            self._viewer_room_id = room_id
            self._viewer_ready = ready
            self._latest_viewer_count = None

        if self._latest_viewer_count is None and self._viewer_ready is not None:
            await asyncio.wait_for(
                self._viewer_ready.wait(),
                timeout=max(1.0, min(self.timeout_seconds, 10.0)),
            )
        return self._latest_viewer_count

    async def _stop_viewer_stream(self) -> None:
        client = self._viewer_client
        self._viewer_client = None
        self._viewer_task = None
        self._viewer_room_id = None
        self._viewer_ready = None
        self._latest_viewer_count = None
        if client is None:
            return
        try:
            await client.disconnect(close_client=False)
        except Exception as exc:
            logger.debug("Could not disconnect TikTok viewer stream: %s", exc)
        try:
            await client.web.close()
        except Exception as exc:
            logger.debug("Could not close TikTok viewer stream client: %s", exc)

    async def _fetch_room_id(self, client: object) -> str | None:
        web = getattr(client, "web", None)
        if web is None:
            return None
        errors: list[str] = []
        for method_name in ("fetch_room_id_from_html", "fetch_room_id_from_api"):
            method = getattr(web, method_name, None)
            if not callable(method):
                continue
            try:
                room_id = await method(self.username)
                if room_id:
                    return str(room_id)
            except Exception as exc:
                errors.append(f"{method_name}: {type(exc).__name__}")
        if errors:
            logger.warning("LIVE detected but room ID unavailable (%s)", ", ".join(errors))
        return None

    @staticmethod
    def _parse_room_id(payload: dict) -> str | None:
        value = (payload.get("user") or {}).get("roomId")
        if value in (None, "", 0, "0"):
            value = (payload.get("liveRoom") or {}).get("id")
        return str(value) if value not in (None, "", 0, "0") else None

    @staticmethod
    def _parse_viewer_count(payload: dict) -> int | None:
        live_room = payload.get("liveRoom") or {}
        candidates = (
            (payload.get("liveRoomStats") or {}).get("userCount"),
            (payload.get("liveRoomStats") or {}).get("user_count"),
            (live_room.get("liveRoomStats") or {}).get("userCount"),
            (live_room.get("liveRoomStats") or {}).get("user_count"),
            live_room.get("userCount"),
            live_room.get("user_count"),
            (live_room.get("stats") or {}).get("userCount"),
            (live_room.get("stats") or {}).get("user_count"),
        )
        for value in candidates:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            # TikTok's public user-room endpoint can report 0 while the LIVE
            # page shows hundreds of viewers. Treat zero as unavailable so it
            # cannot trigger a false low-viewer notification.
            if parsed > 0:
                return parsed
        return None

    @staticmethod
    def _parse_stream_viewer_count(event: Any) -> int | None:
        # TikTok's v3 RoomUserSeq event calls the current audience `total`;
        # older protocol versions expose the same field as `viewer_count`.
        # `total_user` is cumulative entries for the whole session, not the
        # current number of viewers, so it must never drive low-viewer alerts.
        for field in ("total", "viewer_count"):
            try:
                parsed = int(getattr(event, field, 0))
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                return parsed
        return None

