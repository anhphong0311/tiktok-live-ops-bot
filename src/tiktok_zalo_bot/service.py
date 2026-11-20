from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .config import ConfigError, load_config
from .models import LiveObservation, LiveState, Notification, NotificationStatus
from .state import StateStore
from .tiktok import TikTokChecker
from .zalo import ZaloAutomation, ZaloAutomationError

logger = logging.getLogger(__name__)


class NotificationService:
    def __init__(
        self,
        config: dict[str, Any],
        store: StateStore,
        checker: TikTokChecker | None = None,
        zalo: ZaloAutomation | None = None,
    ):
        self.config = config
        self.store = store
        self.checker = checker or TikTokChecker(
            config["tiktok"]["username"],
            float(config["tiktok"].get("request_timeout_seconds", 20)),
        )
        self.zalo = zalo or ZaloAutomation(config)

    def format_message(self, observed_at: datetime) -> str:
        timezone = ZoneInfo(self.config["notification"].get("timezone", "Asia/Bangkok"))
        local_time = observed_at.astimezone(timezone).strftime("%d/%m/%Y %H:%M:%S")
        return str(self.config["notification"]["template"]).format(
            time=local_time,
            live_url=self.config["tiktok"]["live_url"],
        )

    def format_end_message(self, started_at: datetime, ended_at: datetime) -> str:
        timezone = ZoneInfo(self.config["notification"].get("timezone", "Asia/Bangkok"))
        local_start = started_at.astimezone(timezone).strftime("%d/%m/%Y %H:%M:%S")
        local_end = ended_at.astimezone(timezone).strftime("%d/%m/%Y %H:%M:%S")
        duration_seconds = max(0, int((ended_at - started_at).total_seconds()))
        hours, remainder = divmod(duration_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        duration = f"{hours} giờ {minutes} phút {seconds} giây"
        template = self.config["notification"].get(
            "end_template",
            "⚫ BỐ HÍU BỈM SỮA ĐÃ KẾT THÚC LIVE!\n"
            "🕒 Bắt đầu: {start_time}\n"
            "🕒 Kết thúc: {end_time}\n"
            "⏱️ Thời lượng: {duration}",
        )
        return str(template).format(
            start_time=local_start,
            end_time=local_end,
            duration=duration,
            live_url=self.config["tiktok"]["live_url"],
        )

    def format_viewer_alert(
        self, observed_at: datetime, viewer_count: int, minimum_viewers: int
    ) -> str:
        timezone = ZoneInfo(self.config["notification"].get("timezone", "Asia/Bangkok"))
        local_time = observed_at.astimezone(timezone).strftime("%d/%m/%Y %H:%M:%S")
        template = self.config.get("viewer_alert", {}).get(
            "template",
            "⚠️ MẮT LIVE ĐANG THẤP!\n"
            "👁️ Hiện tại: {viewer_count} mắt\n"
            "🎯 Mức tối thiểu: {minimum_viewers} mắt\n"
            "🕒 Thời gian: {time}\n"
            "🔗 {live_url}",
        )
        return str(template).format(
            viewer_count=viewer_count,
            minimum_viewers=minimum_viewers,
            time=local_time,
            live_url=self.config["tiktok"]["live_url"],
        )

    async def run_once(self, observation: LiveObservation | None = None) -> LiveObservation:
        observation = observation or await self.checker.check()
        prior_state = self.store.get_monitor_state()
        message = (
            self.format_message(observation.checked_at)
            if observation.state is LiveState.LIVE
            else None
        )
        end_message = None
        if (
            observation.state is LiveState.OFFLINE
            and prior_state.get("last_definitive_state") == LiveState.LIVE.value
            and prior_state.get("active_started_at")
        ):
            started_at = datetime.fromisoformat(str(prior_state["active_started_at"]))
            end_message = self.format_end_message(started_at, observation.checked_at)
        notification = self.store.record_observation(
            observation,
            self.config["tiktok"]["username"],
            message,
            end_message,
            offline_confirmations=int(
                self.config["runtime"].get("offline_confirmations", 4)
            ),
        )
        logger.info(
            "TikTok state=%s room_id=%s viewers=%s detail=%s",
            observation.state.value,
            observation.room_id,
            observation.viewer_count,
            observation.detail,
        )
        if self._handle_notification(notification):
            return observation

        viewer_alert = self.config.get("viewer_alert", {})
        if (
            observation.state is LiveState.LIVE
            and observation.viewer_count is not None
            and bool(viewer_alert.get("enabled", False))
        ):
            minimum = int(viewer_alert.get("minimum_viewers", 0))
            viewer_notification = self.store.record_viewer_observation(
                observation.viewer_count,
                minimum,
                self.format_viewer_alert(
                    observation.checked_at, observation.viewer_count, minimum
                ),
                confirmations=int(viewer_alert.get("confirmations", 2)),
                recovery_margin=int(viewer_alert.get("recovery_margin", 10)),
                reminder_minutes=int(viewer_alert.get("reminder_minutes", 15)),
            )
            self._handle_notification(viewer_notification)
        return observation

    def _handle_notification(self, notification: Notification | None) -> bool:
        if notification is None:
            return False
        send_enabled = bool(self.config["zalo"].get("send_enabled", False))
        dry_run = bool(self.config["zalo"].get("dry_run", True))
        real_send = send_enabled and not dry_run
        actionable = (
            {NotificationStatus.NEW, NotificationStatus.FAILED_SAFE}
            if not real_send
            else {
                NotificationStatus.NEW,
                NotificationStatus.PREVIEWED,
                NotificationStatus.FAILED_SAFE,
            }
        )
        if notification.status not in actionable:
            logger.info(
                "Notification %s already handled with status=%s",
                notification.dedupe_key,
                notification.status.value,
            )
            return False

        if not self.config["zalo"].get("group_confirmed", False):
            logger.warning(
                "Notification pending: Zalo group is not confirmed"
            )
            return True

        max_attempts = int(self.config["runtime"].get("max_retry_before_send", 3))
        if notification.attempts >= max_attempts:
            logger.error(
                "Notification id=%s reached safe retry limit (%s); manual action required",
                notification.id,
                max_attempts,
            )
            # This notification is permanently quarantined until a human handles it.
            # Do not let it block independent event processing (for example, a
            # low-viewer alert for the same live session) on every later poll.
            return False

        if not real_send:
            try:
                result = self.zalo.deliver(notification.message, real_send=False)
                self.store.update_notification(
                    notification.id,
                    NotificationStatus.PREVIEWED,
                    increment_attempts=True,
                )
                logger.info("Preview filled; screenshot=%s", result.screenshot_path)
            except ZaloAutomationError as exc:
                self.store.update_notification(
                    notification.id,
                    NotificationStatus.FAILED_SAFE,
                    error=str(exc),
                    increment_attempts=True,
                )
                logger.error("Zalo preview failed safely: %s", exc)
            return True

        # Commit SENDING before pressing Enter. A crash here is quarantined on restart.
        self.store.update_notification(
            notification.id,
            NotificationStatus.SENDING,
            increment_attempts=True,
        )
        try:
            result = self.zalo.deliver(notification.message, real_send=True)
            self.store.update_notification(notification.id, NotificationStatus.SENT)
            logger.info("Notification sent; screenshot=%s", result.screenshot_path)
        except ZaloAutomationError as exc:
            target = (
                NotificationStatus.UNCERTAIN
                if exc.send_may_have_happened
                else NotificationStatus.FAILED_SAFE
            )
            self.store.update_notification(notification.id, target, error=str(exc))
            logger.error("Zalo send failed status=%s: %s", target.value, exc)
        return True

    def _reload_viewer_alert(self) -> None:
        config_path = self.config.get("_config_path")
        if not config_path:
            return
        try:
            fresh = load_config(str(config_path))
        except (ConfigError, OSError, ValueError) as exc:
            logger.warning("Không thể nạp lại ngưỡng mắt; giữ cấu hình cũ: %s", exc)
            return
        self.config["viewer_alert"] = fresh.get("viewer_alert", {})

    async def run_forever(self) -> None:
        recovered = self.store.recover_inflight()
        if recovered:
            logger.error(
                "Quarantined %s interrupted send(s) as UNCERTAIN; no automatic retry",
                recovered,
            )
        interval = int(self.config["tiktok"].get("poll_interval_seconds", 60))
        unknown_streak = 0
        while True:
            try:
                self._reload_viewer_alert()
                result = await self.run_once()
                unknown_streak = unknown_streak + 1 if result.state is LiveState.UNKNOWN else 0
            except asyncio.CancelledError:
                raise
            except Exception:
                unknown_streak += 1
                logger.exception("Unexpected monitor error")
            delay = min(interval * max(1, 2 ** min(unknown_streak - 1, 3)), 600)
            logger.info("Next check in %s seconds", delay)
            await asyncio.sleep(delay)

