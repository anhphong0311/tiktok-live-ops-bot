from __future__ import annotations

import asyncio
import copy
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from .config import ConfigError, load_config
from .dashboard import (
    DashboardAccessError,
    DashboardSnapshot,
    ProductRevenue,
    TikTokShopDashboardReader,
)
from .gmv_state import GmvStateStore, ReportNotification
from .models import LiveObservation, LiveState, NotificationStatus
from .tiktok import TikTokChecker
from .zalo import ZaloAutomation, ZaloAutomationError

logger = logging.getLogger(__name__)


def format_vnd(value: int) -> str:
    return f"{max(0, int(value)):,}".replace(",", ".") + "đ"


def format_duration(started_at: datetime, ended_at: datetime) -> str:
    seconds = max(0, int((ended_at - started_at).total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours} giờ {minutes} phút {seconds} giây"


@dataclass(frozen=True)
class ProductRevenueDelta:
    product: ProductRevenue
    attributed_gmv_delta_vnd: int


def product_revenue_deltas(
    previous: DashboardSnapshot, current: DashboardSnapshot
) -> list[ProductRevenueDelta]:
    previous_gmv = {
        product.product_id: product.attributed_gmv_vnd
        for product in previous.products
    }
    deltas = [
        ProductRevenueDelta(
            product=product,
            attributed_gmv_delta_vnd=(
                product.attributed_gmv_vnd - previous_gmv.get(product.product_id, 0)
            ),
        )
        for product in current.products
    ]
    return sorted(
        (item for item in deltas if item.attributed_gmv_delta_vnd > 0),
        key=lambda item: item.attributed_gmv_delta_vnd,
        reverse=True,
    )


def _product_delta_block(index: int, item: ProductRevenueDelta) -> str:
    product = item.product
    return (
        f"{index}. +{format_vnd(item.attributed_gmv_delta_vnd)} | "
        f"Hiện tại: {format_vnd(product.attributed_gmv_vnd)}\n"
        f"{product.name}\n"
        f"🆔 {product.product_id}"
    )


def build_product_delta_messages(
    previous: DashboardSnapshot,
    current: DashboardSnapshot,
    *,
    report_time: datetime,
    timezone: ZoneInfo,
    max_message_chars: int,
    window_minutes: int = 5,
) -> list[str]:
    deltas = product_revenue_deltas(previous, current)
    if not deltas:
        return []

    local_start = previous.captured_at.astimezone(timezone).strftime(
        "%d/%m/%Y %H:%M:%S"
    )
    local_report = report_time.astimezone(timezone).strftime("%d/%m/%Y %H:%M:%S")
    heading = (
        f"🛒 SẢN PHẨM ĐANG RA TIỀN TRONG {max(1, window_minutes)} PHÚT\n"
        f"🕒 Từ: {local_start}\n"
        f"🕒 Đến: {local_report}\n"
        f"📦 Sản phẩm phát sinh GMV: {len(deltas)}"
    )
    blocks = [
        _product_delta_block(index, item) for index, item in enumerate(deltas, 1)
    ]
    limit = max(500, int(max_message_chars))
    joined = heading + "\n\n" + "\n\n".join(blocks)
    if len(joined) <= limit:
        return [joined]

    messages: list[str] = []
    current_blocks: list[str] = []
    current_length = 0
    reserve = len(heading) + 40
    for block in blocks:
        addition = len(block) + (2 if current_blocks else 0)
        if current_blocks and current_length + addition > limit - reserve:
            messages.append("\n\n".join(current_blocks))
            current_blocks = []
            current_length = 0
        current_blocks.append(block)
        current_length += len(block) + (2 if len(current_blocks) > 1 else 0)
    if current_blocks:
        messages.append("\n\n".join(current_blocks))

    total = len(messages)
    return [
        f"{heading}\n📄 Phần {index}/{total}\n\n{message}"
        for index, message in enumerate(messages, 1)
    ]


def build_report_messages(
    snapshot: DashboardSnapshot,
    *,
    started_at: datetime,
    report_time: datetime,
    timezone: ZoneInfo,
    max_message_chars: int,
    final: bool = False,
    ended_at: datetime | None = None,
) -> list[str]:
    local_start = started_at.astimezone(timezone).strftime("%d/%m/%Y %H:%M:%S")
    local_report = report_time.astimezone(timezone).strftime("%d/%m/%Y %H:%M:%S")
    ratio = snapshot.ads_to_gmv_percent
    if final:
        actual_end = ended_at or report_time
        local_end = actual_end.astimezone(timezone).strftime("%d/%m/%Y %H:%M:%S")
        title = "🏁 TỔNG KẾT PHIÊN LIVE"
        timing = (
            f"🕒 Bắt đầu: {local_start}\n"
            f"🕒 Kết thúc: {local_end}\n"
            f"⏱️ Thời lượng: {format_duration(started_at, actual_end)}"
        )
    else:
        elapsed_hours = max(
            1, int((report_time - started_at).total_seconds() // 3600)
        )
        title = f"📊 BÁO CÁO LIVE - MỐC {elapsed_hours} GIỜ"
        timing = f"🕒 Bắt đầu: {local_start}\n🕒 Báo cáo: {local_report}"

    return [
        (
            f"{title}\n"
            f"{timing}\n"
            f"💰 Attributed GMV: {format_vnd(snapshot.attributed_gmv_vnd)}\n"
            f"📢 Ads Cost: {format_vnd(snapshot.ads_cost_vnd)}\n"
            f"📈 Ads Cost/GMV: {ratio:.2f}%"
        )
    ]


def build_gmv_zalo_config(config: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(config)
    target = copy.deepcopy(config["zalo"])
    target.update(copy.deepcopy(config["gmv_report"].get("zalo", {})))
    merged["zalo"] = target
    return merged


class GmvReportService:
    def __init__(
        self,
        config: dict[str, Any],
        store: GmvStateStore,
        *,
        checker: TikTokChecker | None = None,
        dashboard: TikTokShopDashboardReader | None = None,
        zalo: ZaloAutomation | None = None,
    ):
        self.config = config
        self.settings = config["gmv_report"]
        self.store = store
        self.checker = checker or TikTokChecker(
            config["tiktok"]["username"],
            float(config["tiktok"].get("request_timeout_seconds", 20)),
        )
        self.dashboard = dashboard or TikTokShopDashboardReader(config)
        self.zalo_config = build_gmv_zalo_config(config)
        self.zalo = zalo or ZaloAutomation(self.zalo_config)
        self.timezone = ZoneInfo(
            config["notification"].get("timezone", "Asia/Bangkok")
        )

    async def run_once(
        self,
        observation: LiveObservation | None = None,
        *,
        now: datetime | None = None,
    ) -> LiveObservation:
        observed = observation or await self.checker.check()
        current = (now or observed.checked_at).astimezone(UTC)
        logger.info(
            "GMV reporter TikTok state=%s room_id=%s",
            observed.state.value,
            observed.room_id,
        )

        if observed.state is LiveState.LIVE and observed.room_id:
            await self._handle_live(observed, current)
        elif observed.state is LiveState.OFFLINE:
            await self._handle_offline(observed, current)

        pending_final = self.store.get_pending_final_session()
        if pending_final and self.store.dashboard_retry_due(
            str(pending_final["room_id"]),
            current,
            int(self.settings.get("dashboard_retry_minutes", 5)),
        ):
            await self._try_final_report(pending_final, current)

        self._drain_outbox()
        return observed

    async def _handle_live(
        self, observation: LiveObservation, current: datetime
    ) -> None:
        room_id = str(observation.room_id)
        interval = int(self.settings.get("interval_minutes", 60))
        product_interval = int(self.settings.get("product_interval_minutes", 5))
        session = self.store.observe_live(
            room_id,
            observation.checked_at,
            interval,
            product_interval,
        )
        first_fetch = not session.get("last_snapshot_json")
        product_baseline_missing = not session.get("last_product_snapshot_json")
        was_dashboard_error = bool(session.get("dashboard_error_active"))
        retry_due = was_dashboard_error and self.store.dashboard_retry_due(
            room_id,
            current,
            int(self.settings.get("dashboard_retry_minutes", 5)),
        )
        hourly_due = self.store.hourly_due(room_id, current)
        product_due = self.store.product_due(room_id, current)
        should_fetch = (
            retry_due
            if was_dashboard_error
            else first_fetch or product_baseline_missing or product_due or hourly_due
        )
        if not should_fetch:
            return

        previous_product_snapshot = self.store.load_product_snapshot(room_id)
        snapshot = self._fetch_dashboard(room_id, current)
        if snapshot is None:
            return
        self.store.save_snapshot(snapshot)
        if snapshot.started_at is not None:
            session = self.store.align_started_at(
                room_id, snapshot.started_at, interval
            )
        else:
            session = self.store.get_session(room_id) or session

        if product_baseline_missing or was_dashboard_error:
            self.store.complete_product_check(
                room_id,
                snapshot,
                [],
                current,
                product_interval,
            )
            logger.info("Stored GMV product baseline room=%s", room_id)
        elif product_due and previous_product_snapshot is not None:
            product_messages = build_product_delta_messages(
                previous_product_snapshot,
                snapshot,
                report_time=current,
                timezone=self.timezone,
                max_message_chars=int(self.settings.get("max_message_chars", 1800)),
                window_minutes=product_interval,
            )
            self.store.complete_product_check(
                room_id,
                snapshot,
                product_messages,
                current,
                product_interval,
            )
            if product_messages:
                logger.info(
                    "Queued GMV product delta room=%s parts=%s",
                    room_id,
                    len(product_messages),
                )
            else:
                logger.info("No GMV product delta in current window room=%s", room_id)

        if self.store.hourly_due(room_id, current):
            started_at = datetime.fromisoformat(str(session["started_at"]))
            messages = build_report_messages(
                snapshot,
                started_at=started_at,
                report_time=current,
                timezone=self.timezone,
                max_message_chars=int(self.settings.get("max_message_chars", 1800)),
            )
            sequence = self.store.enqueue_hourly(
                room_id, messages, current, interval
            )
            logger.info(
                "Queued GMV hourly report room=%s sequence=%s parts=%s",
                room_id,
                sequence,
                len(messages),
            )

    async def _handle_offline(
        self, observation: LiveObservation, current: datetime
    ) -> None:
        session = self.store.get_active_session()
        if not session:
            return
        room_id = str(session["room_id"])
        ended = self.store.note_offline(
            room_id,
            observation.checked_at,
            int(self.settings.get("offline_confirmations", 4)),
        )
        if ended:
            updated = self.store.get_session(room_id)
            if updated:
                await self._try_final_report(updated, current)

    async def _try_final_report(
        self, session: dict[str, object], current: datetime
    ) -> None:
        room_id = str(session["room_id"])
        snapshot = self._fetch_dashboard(room_id, current)
        if snapshot is None:
            return
        self.store.save_snapshot(snapshot)
        started_at = datetime.fromisoformat(str(session["started_at"]))
        ended_at = (
            snapshot.dashboard_time
            or (
                datetime.fromisoformat(str(session["ended_at"]))
                if session.get("ended_at")
                else current
            )
        )
        messages = build_report_messages(
            snapshot,
            started_at=started_at,
            report_time=current,
            ended_at=ended_at,
            final=True,
            timezone=self.timezone,
            max_message_chars=int(self.settings.get("max_message_chars", 1800)),
        )
        self.store.enqueue_final(room_id, messages)
        logger.info("Queued GMV final report room=%s parts=%s", room_id, len(messages))

    def _fetch_dashboard(
        self, room_id: str, current: datetime
    ) -> DashboardSnapshot | None:
        self.store.mark_dashboard_attempt(room_id, current)
        try:
            snapshot = self.dashboard.fetch(room_id)
        except DashboardAccessError as exc:
            error = str(exc)
            logger.error("GMV dashboard unavailable room=%s: %s", room_id, error)
            if self.store.record_dashboard_failure(
                room_id,
                error,
                current,
                int(self.settings.get("error_reminder_minutes", 30)),
            ):
                local = current.astimezone(self.timezone).strftime("%d/%m/%Y %H:%M:%S")
                bucket = int(
                    current.timestamp()
                    // (max(1, int(self.settings.get("error_reminder_minutes", 30))) * 60)
                )
                self.store.enqueue_notice(
                    room_id,
                    "DASHBOARD_ERROR",
                    f"dashboard-error:{room_id}:{bucket}",
                    "⚠️ KHÔNG TRUY CẬP ĐƯỢC DASHBOARD LIVE!\n"
                    f"🕒 Thời gian: {local}\n"
                    f"🆔 Room ID: {room_id}\n"
                    f"ℹ️ Lỗi: {error}\n"
                    "Vui lòng kiểm tra Microsoft Edge và đăng nhập TikTok Shop.",
                )
            return None

        if self.store.record_dashboard_recovery(room_id):
            local = current.astimezone(self.timezone).strftime("%d/%m/%Y %H:%M:%S")
            self.store.enqueue_notice(
                room_id,
                "DASHBOARD_RECOVERED",
                f"dashboard-recovered:{room_id}:{int(current.timestamp())}",
                "✅ DASHBOARD LIVE ĐÃ HOẠT ĐỘNG LẠI!\n"
                f"🕒 Thời gian: {local}\n"
                f"🆔 Room ID: {room_id}\n"
                "Hệ thống tiếp tục báo cáo GMV bình thường.",
            )
        return snapshot

    def _drain_outbox(self) -> None:
        zalo = self.zalo_config["zalo"]
        if not zalo.get("group_confirmed", False):
            logger.warning("GMV Zalo target is not confirmed; queued messages are retained")
            return
        if not zalo.get("send_enabled", False) or zalo.get("dry_run", True):
            logger.warning("GMV real send is disabled; queued messages are retained")
            return

        max_attempts = int(self.settings.get("max_retry_before_send", 3))
        for _ in range(int(self.settings.get("max_messages_per_cycle", 20))):
            notification = self.store.get_actionable(max_attempts)
            if notification is None:
                return
            if not self._send_notification(notification):
                return

    def _send_notification(self, notification: ReportNotification) -> bool:
        self.store.update_notification(
            notification.id,
            NotificationStatus.SENDING,
            increment_attempts=True,
        )
        try:
            result = self.zalo.deliver(notification.message, real_send=True)
        except ZaloAutomationError as exc:
            target = (
                NotificationStatus.UNCERTAIN
                if exc.send_may_have_happened
                else NotificationStatus.FAILED_SAFE
            )
            self.store.update_notification(
                notification.id, target, error=str(exc)
            )
            logger.error(
                "GMV Zalo send failed id=%s status=%s: %s",
                notification.id,
                target.value,
                exc,
            )
            return False
        self.store.update_notification(notification.id, NotificationStatus.SENT)
        logger.info(
            "GMV notification sent id=%s event=%s screenshot=%s",
            notification.id,
            notification.event_type,
            result.screenshot_path,
        )
        return True

    async def run_forever(self) -> None:
        recovered = self.store.recover_inflight()
        if recovered:
            logger.error("Quarantined %s interrupted GMV send(s)", recovered)
        interval = int(self.settings.get("poll_interval_seconds", 60))
        while True:
            config_path = self.config.get("_config_path")
            if config_path:
                try:
                    latest = load_config(str(config_path))
                except ConfigError:
                    logger.exception("Could not reload GMV reporter configuration")
                else:
                    if not latest.get("gmv_report", {}).get("enabled", False):
                        logger.info("GMV reporter disabled in config; stopping process")
                        return
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unexpected GMV reporter error")
            await asyncio.sleep(max(15, interval))
