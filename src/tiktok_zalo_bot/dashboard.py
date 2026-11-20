from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urlencode

logger = logging.getLogger(__name__)


class DashboardAccessError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProductRevenue:
    product_id: str
    name: str
    attributed_gmv_vnd: int


@dataclass(frozen=True)
class DashboardSnapshot:
    room_id: str
    captured_at: datetime
    attributed_gmv_vnd: int
    ads_cost_vnd: int
    products: tuple[ProductRevenue, ...]
    started_at: datetime | None = None
    dashboard_time: datetime | None = None

    @property
    def ads_to_gmv_percent(self) -> float:
        if self.attributed_gmv_vnd <= 0:
            return 0.0
        return self.ads_cost_vnd * 100.0 / self.attributed_gmv_vnd


_PRODUCT_RE = re.compile(
    r"^\s*\d+\s+(?P<name>.+?)\s+ID:\s*(?P<id>\d+)\s*$", re.DOTALL
)
_MONEY_RE = re.compile(r"^[\d.,\s]+₫$")
_COMPACT_RE = re.compile(r"^(?P<number>[\d.,]+)\s*(?P<suffix>[KMB]?)$", re.I)
_DATE_RANGE_RE = re.compile(
    r"(?P<start_month>[A-Z][a-z]{2})\s+(?P<start_day>\d{1,2}),\s*"
    r"(?P<start_time>\d{2}:\d{2}:\d{2})\s*-\s*"
    r"(?P<end_month>[A-Z][a-z]{2})\s+(?P<end_day>\d{1,2}),\s*"
    r"(?P<end_time>\d{2}:\d{2}:\d{2})\s*UTC(?P<offset>[+-]\d{2}:\d{2})"
)


def parse_vnd(value: str) -> int:
    digits = re.sub(r"\D", "", value)
    if not digits:
        raise ValueError(f"Không đọc được số tiền: {value!r}")
    return int(digits)


def parse_compact_number(value: str) -> int:
    compact = re.sub(r"\s+", "", value).upper()
    match = _COMPACT_RE.fullmatch(compact)
    if not match:
        raise ValueError(f"Không đọc được số rút gọn: {value!r}")
    number = match.group("number")
    suffix = match.group("suffix")
    if suffix:
        # The dashboard uses a dot as the decimal separator for K/M values.
        parsed = float(number.replace(",", ""))
        multiplier = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[suffix]
        return int(round(parsed * multiplier))
    return parse_vnd(number)


def parse_product_rows(rows: Iterable[Sequence[str]]) -> tuple[ProductRevenue, ...]:
    products: list[ProductRevenue] = []
    seen: set[str] = set()
    for cells in rows:
        cleaned = [re.sub(r"\s+", " ", str(cell)).strip() for cell in cells]
        if not cleaned:
            continue
        match = _PRODUCT_RE.match(cleaned[0])
        if not match:
            continue
        money = next((cell for cell in cleaned[1:] if _MONEY_RE.fullmatch(cell)), None)
        if money is None:
            continue
        gmv = parse_vnd(money)
        product_id = match.group("id")
        if gmv <= 0 or product_id in seen:
            continue
        seen.add(product_id)
        products.append(
            ProductRevenue(
                product_id=product_id,
                name=match.group("name").strip(),
                attributed_gmv_vnd=gmv,
            )
        )
    products.sort(key=lambda item: (-item.attributed_gmv_vnd, item.product_id))
    return tuple(products)


def _find_metric_segment(
    texts: Sequence[str], label: str, terminator: str, *, maximum: int = 40
) -> list[str]:
    candidates: list[list[str]] = []
    for index, text in enumerate(texts):
        if text.strip() != label:
            continue
        tail = list(texts[index + 1 : index + 1 + maximum])
        try:
            end = next(i for i, value in enumerate(tail) if value.strip() == terminator)
        except StopIteration:
            continue
        candidates.append(tail[:end])
    if not candidates:
        raise DashboardAccessError(f"Không tìm thấy chỉ số {label}")
    return candidates[-1]


def _parse_main_gmv(texts: Sequence[str]) -> int:
    segment = _find_metric_segment(texts, "Attributed GMV", "Attributed items sold")
    tokens = [
        token.strip()
        for token in segment
        if re.fullmatch(r"[\d,.]+", token.strip())
    ]
    joined = "".join(tokens)
    if not joined:
        raise DashboardAccessError("Không đọc được Attributed GMV")
    return parse_vnd(joined)


def _parse_ads_cost(texts: Sequence[str]) -> int:
    segment = _find_metric_segment(texts, "Ads Cost", "Impressions", maximum=12)
    tokens = [
        token.strip()
        for token in segment
        if re.fullmatch(r"[\d.,]+|[KMB]", token.strip(), re.I)
    ]
    joined = "".join(tokens)
    if not joined:
        raise DashboardAccessError("Không đọc được Ads Cost")
    return parse_compact_number(joined)


def parse_dashboard_times(
    texts: Sequence[str], *, now: datetime
) -> tuple[datetime | None, datetime | None]:
    match = next(
        (match for text in texts if (match := _DATE_RANGE_RE.search(text))), None
    )
    if match is None:
        return None, None
    sign = 1 if match.group("offset").startswith("+") else -1
    hours, minutes = map(int, match.group("offset")[1:].split(":"))
    tz = timezone(sign * timedelta(hours=hours, minutes=minutes))

    def parse_side(month: str, day: str, clock: str) -> datetime:
        value = datetime.strptime(
            f"{now.year} {month} {day} {clock}", "%Y %b %d %H:%M:%S"
        )
        return value.replace(tzinfo=tz).astimezone(UTC)

    started = parse_side(
        match.group("start_month"), match.group("start_day"), match.group("start_time")
    )
    ended = parse_side(
        match.group("end_month"), match.group("end_day"), match.group("end_time")
    )
    # Handle the rare New Year's Eve session whose end falls in the next year.
    if ended < started:
        ended = ended.replace(year=ended.year + 1)
    return started, ended


def parse_dashboard_snapshot(
    room_id: str,
    texts: Sequence[str],
    product_rows: Iterable[Sequence[str]],
    *,
    captured_at: datetime | None = None,
) -> DashboardSnapshot:
    captured = captured_at or datetime.now(UTC)
    normalized = [str(text).strip() for text in texts if str(text).strip()]
    started_at, dashboard_time = parse_dashboard_times(normalized, now=captured)
    return DashboardSnapshot(
        room_id=str(room_id),
        captured_at=captured,
        attributed_gmv_vnd=_parse_main_gmv(normalized),
        ads_cost_vnd=_parse_ads_cost(normalized),
        products=parse_product_rows(product_rows),
        started_at=started_at,
        dashboard_time=dashboard_time,
    )


class TikTokShopDashboardReader:
    """Read the authenticated TikTok Shop dashboard through Windows UIA."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.settings = config["gmv_report"]["dashboard"]
        self._active_room_id: str | None = None
        self._window: Any | None = None

    def fetch(self, room_id: str) -> DashboardSnapshot:
        room_id = str(room_id)
        window = self._ensure_window(room_id)
        timeout = float(self.settings.get("load_timeout_seconds", 60))
        retry = float(self.settings.get("read_retry_seconds", 2))
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                texts, rows = self._read_window(window)
                snapshot = parse_dashboard_snapshot(room_id, texts, rows)
                self._active_room_id = room_id
                self._window = window
                return snapshot
            except (DashboardAccessError, RuntimeError, OSError) as exc:
                last_error = exc
                time.sleep(retry)
        detail = f": {last_error}" if last_error else ""
        raise DashboardAccessError(
            "Không đọc được dashboard TikTok Shop; hãy kiểm tra Edge đã đăng nhập"
            + detail
        )

    def _ensure_window(self, room_id: str) -> Any:
        if self._window is not None and self._active_room_id == room_id:
            try:
                if self._window.exists(timeout=0.5):
                    return self._window
            except Exception:
                self._window = None

        existing = self._find_window(room_id)
        if existing is not None:
            self._active_room_id = room_id
            self._window = existing
            return existing

        executable = self._browser_executable()
        url = self._dashboard_url(room_id)
        profile = str(self.settings.get("browser_profile", "Default"))
        try:
            subprocess.Popen(
                [
                    str(executable),
                    f"--app={url}",
                    "--start-maximized",
                    f"--profile-directory={profile}",
                ],
                cwd=str(executable.parent),
                close_fds=True,
            )
        except OSError as exc:
            raise DashboardAccessError(f"Không mở được Microsoft Edge: {exc}") from exc

        timeout = float(self.settings.get("launch_timeout_seconds", 30))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(1)
            window = self._find_window(room_id)
            if window is not None:
                self._active_room_id = room_id
                self._window = window
                return window
        raise DashboardAccessError(
            "Đã mở dashboard nhưng không thấy cửa sổ dữ liệu LIVE hợp lệ"
        )

    def _find_window(self, room_id: str) -> Any | None:
        try:
            from pywinauto import Desktop

            title = str(
                self.settings.get(
                    "window_title_contains", "TikTok Ecommerce Live Data Screen"
                )
            ).casefold()
            windows = [
                window
                for window in Desktop(backend="uia").windows()
                if title in (window.window_text() or "").casefold()
            ]
            matching = [
                window for window in windows if room_id in self._window_url(window)
            ]
            if len(matching) == 1:
                return matching[0]
            if len(matching) > 1:
                return matching[-1]
            return None
        except Exception as exc:
            logger.debug("Could not enumerate dashboard windows: %s", exc)
            return None

    @staticmethod
    def _window_url(window: Any) -> str:
        try:
            documents = window.descendants(control_type="Document")
        except Exception:
            return ""
        for document in documents:
            for getter in (
                lambda: document.get_value(),
                lambda: document.legacy_properties().get("Value", ""),
            ):
                try:
                    value = str(getter() or "")
                except Exception:
                    continue
                if value.startswith("http"):
                    return value
        return ""

    def _read_window(self, window: Any) -> tuple[list[str], list[list[str]]]:
        try:
            documents = window.descendants(control_type="Document")
            document = next(
                item
                for item in documents
                if "shop.tiktok.com/workbench/live/overview" in self._node_value(item)
            )
        except (Exception, StopIteration) as exc:
            raise DashboardAccessError(
                "Dashboard chưa tải hoặc phiên đăng nhập TikTok Shop đã hết"
            ) from exc

        texts = [
            self._node_text(item)
            for item in document.descendants(control_type="Text")
        ]
        texts = [text for text in texts if text]
        if "Attributed GMV" not in texts or "Ads Cost" not in texts:
            raise DashboardAccessError(
                "Dashboard thiếu chỉ số GMV/Ads Cost; có thể phiên đăng nhập đã hết"
            )

        rows: list[list[str]] = []
        for table in document.descendants(control_type="Table"):
            try:
                row_nodes = table.children(control_type="DataItem")
            except Exception:
                continue
            for row in row_nodes:
                try:
                    cells = row.children(control_type="DataItem")
                except Exception:
                    continue
                values = [self._node_text(cell, recursive=True) for cell in cells]
                if values:
                    rows.append(values)
        return texts, rows

    @staticmethod
    def _node_text(node: Any, *, recursive: bool = False) -> str:
        try:
            value = str(node.window_text() or "").strip()
        except Exception:
            value = ""
        if value or not recursive:
            return value
        try:
            parts = [
                str(item.window_text() or "").strip()
                for item in node.descendants(control_type="Text")
            ]
        except Exception:
            return ""
        return " ".join(part for part in parts if part)

    @staticmethod
    def _node_value(node: Any) -> str:
        for getter in (
            lambda: node.get_value(),
            lambda: node.legacy_properties().get("Value", ""),
        ):
            try:
                value = str(getter() or "")
            except Exception:
                continue
            if value:
                return value
        return ""

    def _dashboard_url(self, room_id: str) -> str:
        base = str(
            self.settings.get(
                "url_base", "https://shop.tiktok.com/workbench/live/overview"
            )
        )
        return f"{base}?{urlencode({'room_id': room_id, 'region': 'vn'})}"

    def _browser_executable(self) -> Path:
        raw = str(self.settings.get("browser_executable_path", "")).strip()
        if not raw:
            raw = r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
        executable = Path(os.path.expandvars(raw)).resolve()
        if not executable.is_file():
            raise DashboardAccessError(f"Không tìm thấy Microsoft Edge: {executable}")
        return executable
