from __future__ import annotations

import ctypes
import copy
import logging
import os
import subprocess
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageStat

from .config import resolve_path, save_config
from .lock import TimedFileLock

logger = logging.getLogger(__name__)


def _win32() -> tuple[Any, Any]:
    """Return Win32 libraries with 64-bit-safe window/process signatures."""
    # Use private DLL wrapper objects. Mutating ctypes.windll.user32 function
    # signatures globally breaks PyGetWindow, which defines its own RECT type.
    user32 = ctypes.WinDLL("user32.dll", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.BOOL,
    ]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return user32, kernel32


class ZaloAutomationError(RuntimeError):
    def __init__(self, message: str, *, send_may_have_happened: bool = False):
        super().__init__(message)
        self.send_may_have_happened = send_may_have_happened


@dataclass(frozen=True)
class DeliveryResult:
    preview_only: bool
    screenshot_path: Path


class ZaloAutomation:
    """Zalo PC UI automation without credentials, cookies, or unofficial APIs."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.zalo = config["zalo"]
        self._enable_dpi_awareness()

    @staticmethod
    def _enable_dpi_awareness() -> None:
        """Match PyAutoGUI's physical-pixel coordinate system on scaled displays."""
        try:
            user32 = ctypes.WinDLL("user32.dll", use_last_error=True)
            user32.SetProcessDPIAware.argtypes = []
            user32.SetProcessDPIAware.restype = wintypes.BOOL
            user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            logger.warning("Could not enable DPI awareness", exc_info=True)

    def validate_ready(self, *, real_send: bool) -> None:
        if not self.zalo.get("group_confirmed", False):
            raise ZaloAutomationError("Nhóm Zalo chưa được xác nhận")
        if not str(self.zalo.get("group_name", "")).strip():
            raise ZaloAutomationError("Thiếu zalo.group_name")
        verify_mode = str(
            self.zalo.get("header_verify_mode", "image")
        ).strip().casefold()
        required = [
            "back_to_list_point",
            "search_point",
            "search_result_point",
            "message_point",
            "window_size",
        ]
        if verify_mode == "image":
            required.append("header_region")
        elif verify_mode != "accessibility":
            raise ZaloAutomationError(
                f"Không hỗ trợ header_verify_mode={verify_mode!r}"
            )
        for key in required:
            if not self.zalo.get(key):
                raise ZaloAutomationError(f"Chưa hiệu chuẩn Zalo: thiếu {key}")
        self._configured_executable()
        if verify_mode == "image":
            template = resolve_path(self.config, self.zalo["header_template_path"])
            if not template.exists():
                raise ZaloAutomationError(f"Thiếu ảnh xác minh header: {template}")
        if real_send:
            if not self.zalo.get("send_enabled", False):
                raise ZaloAutomationError("Gửi thật chưa được bật")
            if self.zalo.get("dry_run", True):
                raise ZaloAutomationError("dry_run vẫn đang bật")
            if verify_mode == "image":
                maximum = float(self.zalo.get("header_max_difference", 0.025))
                changed_maximum = float(
                    self.zalo.get("header_max_changed_ratio", 0.08)
                )
                if maximum > 0.04 or changed_maximum > 0.10:
                    raise ZaloAutomationError(
                        "Ngưỡng xác minh nhóm Zalo không an toàn; không gửi tin"
                    )

    def deliver(self, message: str, *, real_send: bool) -> DeliveryResult:
        lock_path = resolve_path(
            self.config,
            str(self.config.get("runtime", {}).get("zalo_lock_path", "data/zalo_ui.lock")),
        )
        timeout = float(
            self.config.get("runtime", {}).get("zalo_lock_timeout_seconds", 90)
        )
        with TimedFileLock(lock_path, timeout):
            return self._deliver_locked(message, real_send=real_send)

    def _deliver_locked(self, message: str, *, real_send: bool) -> DeliveryResult:
        self.validate_ready(real_send=real_send)
        pyautogui, pyperclip = self._ui_modules()
        pyautogui.FAILSAFE = True
        window = self._ensure_window()

        send_pressed = False
        try:
            self._activate(window)
            self._validate_window_size(window)
            self._select_group(window, pyautogui, pyperclip)
            self._verify_header(window, pyautogui)
            self._fill_message(window, message, pyautogui, pyperclip)
            screenshot_path = self._save_preview(window, pyautogui, "latest.png")
            if real_send:
                self._assert_foreground(window, "ngay trước khi nhấn Enter")
                self._save_preview(window, pyautogui, "latest_before_send.png")
                pyautogui.press("enter")
                send_pressed = True
                time.sleep(float(self.zalo.get("paste_wait_seconds", 0.8)))
                self._assert_foreground(window, "sau khi nhấn Enter")
                screenshot_path = self._save_preview(
                    window, pyautogui, "latest_after_send.png"
                )
            return DeliveryResult(
                preview_only=not real_send,
                screenshot_path=screenshot_path,
            )
        except ZaloAutomationError:
            raise
        except Exception as exc:
            raise ZaloAutomationError(
                f"Lỗi UI Zalo: {type(exc).__name__}: {exc}",
                send_may_have_happened=send_pressed,
            ) from exc

    def _select_group(self, window: Any, pyautogui: Any, pyperclip: Any) -> None:
        back_point = self._absolute_point(window, self.zalo["back_to_list_point"])
        search_point = self._absolute_point(window, self.zalo["search_point"])
        result_point = self._absolute_point(window, self.zalo["search_result_point"])
        self._assert_foreground(window, "trước khi trở về danh sách chat")
        # In Zalo's single-chat layout this point is the Back button. In the
        # normal layout it falls inside the search box, which is harmless.
        pyautogui.click(*back_point)
        time.sleep(float(self.zalo.get("navigation_wait_seconds", 1.0)))
        self._assert_foreground(window, "trước khi chọn ô tìm kiếm")
        pyautogui.click(*search_point)
        self._assert_foreground(window, "trước khi nhập tên nhóm")
        pyautogui.hotkey("ctrl", "a")
        pyautogui.press("backspace")
        pyperclip.copy(str(self.zalo["group_name"]))
        pyautogui.hotkey("ctrl", "v")
        time.sleep(float(self.zalo.get("search_wait_seconds", 2.0)))
        self._assert_foreground(window, "trước khi mở nhóm")
        # Zalo does not consistently open the first search result when Enter is
        # pressed while the search box still has focus. Click the calibrated
        # result row instead, then independently verify the exact header image.
        pyautogui.click(*result_point)
        time.sleep(float(self.zalo.get("search_wait_seconds", 2.0)))
        # Selecting a search result leaves Zalo's search overlay open. Escape
        # closes only that overlay and exposes the selected chat header.
        pyautogui.press("esc")
        time.sleep(float(self.zalo.get("navigation_wait_seconds", 1.0)))
        self._assert_foreground(window, "sau khi mở nhóm")

    def _verify_header(self, window: Any, pyautogui: Any) -> None:
        self._assert_foreground(window, "khi xác minh tên nhóm")
        if (
            str(self.zalo.get("header_verify_mode", "image")).strip().casefold()
            == "accessibility"
        ):
            self._verify_accessible_header(window)
            return
        region = self._absolute_region(window, self.zalo["header_region"])
        actual = pyautogui.screenshot(region=region).convert("L")
        template_path = resolve_path(self.config, self.zalo["header_template_path"])
        with Image.open(template_path) as source:
            template = source.convert("L")
        if actual.size != template.size:
            raise ZaloAutomationError(
                "Kích thước vùng tên nhóm không khớp ảnh mẫu; không gửi tin"
            )
        template_stddev = ImageStat.Stat(template).stddev[0]
        minimum_stddev = float(self.zalo.get("header_min_template_stddev", 10.0))
        if template_stddev < minimum_stddev:
            raise ZaloAutomationError(
                "Ảnh mẫu tên nhóm không chứa đủ chi tiết; không gửi tin"
            )
        difference = ImageChops.difference(actual, template)
        mean = ImageStat.Stat(difference).mean[0] / 255.0
        histogram = difference.histogram()
        pixel_threshold = int(
            self.zalo.get("header_pixel_difference_threshold", 20)
        )
        changed_pixels = sum(histogram[pixel_threshold + 1 :])
        changed_ratio = changed_pixels / (actual.width * actual.height)
        maximum = float(self.zalo.get("header_max_difference", 0.025))
        changed_maximum = float(self.zalo.get("header_max_changed_ratio", 0.08))
        logger.info(
            "Zalo header difference %.4f (max %.4f), changed ratio %.4f "
            "(max %.4f)",
            mean,
            maximum,
            changed_ratio,
            changed_maximum,
        )
        if mean > maximum or changed_ratio > changed_maximum:
            raise ZaloAutomationError(
                "Tên nhóm Zalo không khớp chính xác "
                f"'{self.zalo['group_name']}' "
                f"(difference={mean:.3f}, changed={changed_ratio:.3f}); "
                "không điền/gửi tin để tránh gửi nhầm nhóm"
            )

    def _verify_accessible_header(self, window: Any) -> None:
        """Verify the selected recipient from Zalo's exact message-input label."""
        try:
            from pywinauto import Desktop

            handle = int(getattr(window, "_hWnd", 0))
            target = Desktop(backend="uia").window(handle=handle)
            candidates: list[str] = []
            for control in target.descendants():
                try:
                    automation_id = str(control.element_info.automation_id or "")
                except Exception:
                    automation_id = ""
                if automation_id != "richInput":
                    continue
                values = [
                    getattr(control.element_info, "name", ""),
                    control.window_text(),
                ]
                try:
                    values.extend(
                        item.window_text()
                        for item in control.descendants(control_type="Text")
                    )
                except Exception:
                    pass
                for value in values:
                    normalized = " ".join(
                        str(value or "").replace("\xa0", " ").split()
                    )
                    if normalized:
                        candidates.append(normalized)
        except Exception as exc:
            raise ZaloAutomationError(
                "Không đọc được tên nhóm Zalo qua Windows Accessibility; "
                "không gửi tin"
            ) from exc

        expected = " ".join(str(self.zalo["group_name"]).split())
        exact_label = f"Nhập @, tin nhắn tới {expected}"
        if exact_label not in candidates:
            raise ZaloAutomationError(
                "Tên nhóm Zalo không khớp chính xác "
                f"'{expected}' qua Windows Accessibility "
                f"(đã đọc: {candidates or ['không có']}); "
                "không điền/gửi tin để tránh gửi nhầm nhóm"
            )
        logger.info("Verified exact Zalo recipient via accessibility: %s", expected)

    def _fill_message(
        self, window: Any, message: str, pyautogui: Any, pyperclip: Any
    ) -> None:
        point = self._absolute_point(window, self.zalo["message_point"])
        self._assert_foreground(window, "trước khi chọn ô nhập tin")
        pyautogui.click(*point)
        self._assert_foreground(window, "trước khi điền tin")
        pyautogui.hotkey("ctrl", "a")
        pyautogui.press("backspace")
        pyperclip.copy(message)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(float(self.zalo.get("paste_wait_seconds", 0.8)))

    def _save_preview(self, window: Any, pyautogui: Any, filename: str) -> Path:
        self._assert_foreground(window, "khi chụp bằng chứng")
        directory = Path(self.config["_base_dir"]) / "preview"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / filename
        left, top, right, bottom = self._window_rect(window)
        image = pyautogui.screenshot(region=(left, top, right - left, bottom - top))
        image.save(path)
        return path

    def _find_window(self) -> Any | None:
        import pygetwindow as gw

        title = str(self.zalo.get("window_title_exact", "Zalo"))
        candidates = [
            window
            for window in gw.getAllWindows()
            if (window.title or "").strip().casefold() == title.casefold()
            and window.width > 200
            and window.height > 200
            and self._is_zalo_handle(int(getattr(window, "_hWnd", 0)))
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.width * item.height)

    def _ensure_window(self) -> Any:
        window = self._find_window()
        if window is not None:
            return window

        executable = self._configured_executable()
        logger.info("Zalo window not found; launching %s", executable)
        try:
            subprocess.Popen(
                [str(executable)],
                cwd=str(executable.parent),
                close_fds=True,
            )
        except OSError as exc:
            raise ZaloAutomationError(f"Không thể mở Zalo PC: {exc}") from exc

        timeout = float(self.zalo.get("launch_timeout_seconds", 30))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(1.0)
            window = self._find_window()
            if window is not None:
                logger.info("Zalo PC window became available")
                return window
        raise ZaloAutomationError(
            f"Đã mở Zalo nhưng không thấy cửa sổ hợp lệ sau {timeout:.0f} giây; "
            "hãy kiểm tra Zalo đã đăng nhập và màn hình Windows đang mở khóa"
        )

    def _configured_executable(self) -> Path:
        raw = str(self.zalo.get("executable_path", "")).strip()
        if not raw:
            raise ZaloAutomationError("Thiếu zalo.executable_path")
        executable = Path(os.path.expandvars(raw)).resolve()
        if not executable.is_file():
            raise ZaloAutomationError(f"Không tìm thấy Zalo PC: {executable}")
        return executable

    def _is_zalo_handle(self, handle: int) -> bool:
        if not handle:
            return False
        actual = self._window_executable(handle)
        if actual is None or actual.name.casefold() != "zalo.exe":
            return False
        install_root = self._configured_executable().parent
        try:
            actual.relative_to(install_root)
        except ValueError:
            return False
        return True

    @staticmethod
    def _window_executable(handle: int) -> Path | None:
        user32, kernel32 = _win32()
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(wintypes.HWND(handle), ctypes.byref(process_id))
        if not process_id.value:
            return None
        process = kernel32.OpenProcess(0x1000, False, process_id.value)
        if not process:
            return None
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(
                process, 0, buffer, ctypes.byref(size)
            ):
                return None
            return Path(buffer.value).resolve()
        finally:
            kernel32.CloseHandle(process)

    def _activate(self, window: Any) -> None:
        handle = int(getattr(window, "_hWnd", 0))
        if not self._is_zalo_handle(handle):
            raise ZaloAutomationError("Cửa sổ mục tiêu không thuộc tiến trình Zalo PC")

        user32, kernel32 = _win32()
        timeout = float(self.zalo.get("foreground_timeout_seconds", 5))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            foreground = int(user32.GetForegroundWindow())
            current_thread = int(kernel32.GetCurrentThreadId())
            foreground_thread = (
                int(user32.GetWindowThreadProcessId(foreground, None))
                if foreground
                else 0
            )
            attached = bool(
                foreground_thread
                and foreground_thread != current_thread
                and user32.AttachThreadInput(current_thread, foreground_thread, True)
            )
            try:
                user32.ShowWindow(handle, 9)  # SW_RESTORE
                # Keep Zalo on the primary display before maximizing it. The
                # workstation uses monitors with different DPI scales; if the
                # window is maximized on the secondary display, all calibrated
                # pixel coordinates and the recipient-header template shift.
                user32.SetWindowPos(
                    handle,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0x0045,  # SWP_NOSIZE | SWP_NOZORDER | SWP_SHOWWINDOW
                )
                user32.ShowWindow(handle, 3)  # SW_MAXIMIZE
                user32.BringWindowToTop(handle)
                user32.SetForegroundWindow(handle)
            finally:
                if attached:
                    user32.AttachThreadInput(current_thread, foreground_thread, False)
            time.sleep(0.25)
            if int(user32.GetForegroundWindow()) == handle:
                logger.info("Activated verified Zalo window hwnd=%s", handle)
                return
        raise ZaloAutomationError(
            "Không thể đưa Zalo PC lên trước; không thao tác để tránh gửi nhầm ứng dụng"
        )

    def _assert_foreground(self, window: Any, context: str) -> None:
        handle = int(getattr(window, "_hWnd", 0))
        foreground = self._foreground_handle()
        if foreground != handle or not self._is_zalo_handle(handle):
            raise ZaloAutomationError(
                f"Zalo mất focus {context} (expected hwnd={handle}, actual={foreground}); "
                "đã dừng để tránh gửi nhầm"
            )

    @staticmethod
    def _foreground_handle() -> int:
        user32, _ = _win32()
        return int(user32.GetForegroundWindow())

    def _validate_window_size(self, window: Any) -> None:
        expected = self.zalo["window_size"]
        left, top, right, bottom = self._window_rect(window)
        width = right - left
        height = bottom - top
        width_delta = abs(width - int(expected["width"]))
        height_delta = abs(height - int(expected["height"]))
        if width_delta > 8 or height_delta > 8:
            raise ZaloAutomationError(
                "Kích thước cửa sổ Zalo đã thay đổi; hãy chạy setup-zalo lại "
                f"(hiện tại {width}x{height}, đã lưu "
                f"{expected['width']}x{expected['height']})"
            )

    def _window_rect(self, window: Any) -> tuple[int, int, int, int]:
        handle = int(getattr(window, "_hWnd", 0))
        rect = wintypes.RECT()
        user32, _ = _win32()
        if not handle or not user32.GetWindowRect(handle, ctypes.byref(rect)):
            raise ZaloAutomationError("Không đọc được vị trí cửa sổ Zalo")
        return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)

    def _absolute_point(self, window: Any, point: dict[str, int]) -> tuple[int, int]:
        left, top, _, _ = self._window_rect(window)
        return left + int(point["x"]), top + int(point["y"])

    def _absolute_region(
        self, window: Any, region: dict[str, int]
    ) -> tuple[int, int, int, int]:
        left, top, _, _ = self._window_rect(window)
        return (
            left + int(region["left"]),
            top + int(region["top"]),
            int(region["width"]),
            int(region["height"]),
        )

    @staticmethod
    def _ui_modules() -> tuple[Any, Any]:
        import pyautogui
        import pyperclip

        return pyautogui, pyperclip


def setup_zalo(
    config: dict[str, Any], group_name: str, *, target: str = "zalo"
) -> None:
    """Interactive, no-send calibration and explicit group confirmation."""
    if target == "zalo":
        target_config = config["zalo"]
        working = config
    elif target == "gmv_report.zalo":
        target_config = config["gmv_report"]["zalo"]
        working = copy.deepcopy(config)
        merged = copy.deepcopy(config["zalo"])
        merged.update(copy.deepcopy(target_config))
        working["zalo"] = merged
    else:
        raise ZaloAutomationError(f"Không hỗ trợ Zalo target: {target}")

    automation = ZaloAutomation(working)
    pyautogui, _ = automation._ui_modules()
    window = automation._ensure_window()
    automation._activate(window)
    phrase = f"XAC NHAN NHOM: {group_name}"
    print("Mở Zalo PC, chọn ĐÚNG nhóm cần nhận tin và giữ cửa sổ ở kích thước cố định.")
    answer = input(f"Nhập chính xác '{phrase}' để xác nhận: ").strip()
    if answer != phrase:
        raise ZaloAutomationError("Câu xác nhận không khớp; không lưu cấu hình")

    def capture_point(label: str) -> dict[str, int]:
        input(f"Nhấn Enter, sau đó trong 5 giây đưa chuột tới {label}...")
        for remaining in range(5, 0, -1):
            print(f"  {remaining}...", flush=True)
            time.sleep(1)
        position = pyautogui.position()
        return {"x": int(position.x - window.left), "y": int(position.y - window.top)}

    back_to_list_point = capture_point("nút QUAY LẠI DANH SÁCH CHAT")
    search_point = capture_point("ô TÌM KIẾM của Zalo")
    search_result_point = capture_point("DÒNG KẾT QUẢ ĐÚNG sau khi tìm tên nhóm")
    message_point = capture_point("ô NHẬP TIN NHẮN của nhóm")
    top_left = capture_point("GÓC TRÊN-TRÁI vùng chứa TÊN NHÓM")
    bottom_right = capture_point("GÓC DƯỚI-PHẢI vùng chứa TÊN NHÓM")
    header_region = {
        "left": top_left["x"],
        "top": top_left["y"],
        "width": bottom_right["x"] - top_left["x"],
        "height": bottom_right["y"] - top_left["y"],
    }
    if header_region["width"] < 30 or header_region["height"] < 15:
        raise ZaloAutomationError("Vùng header quá nhỏ hoặc chọn ngược góc")

    template_path = resolve_path(working, working["zalo"]["header_template_path"])
    template_path.parent.mkdir(parents=True, exist_ok=True)
    absolute_region = automation._absolute_region(window, header_region)
    pyautogui.screenshot(region=absolute_region).save(template_path)

    target_config.update(
        {
            "group_name": group_name,
            "group_confirmed": True,
            "send_enabled": False,
            "dry_run": True,
            "back_to_list_point": back_to_list_point,
            "search_point": search_point,
            "search_result_point": search_result_point,
            "message_point": message_point,
            "header_region": header_region,
            "header_max_difference": 0.025,
            "header_max_changed_ratio": 0.08,
            "header_pixel_difference_threshold": 20,
            "header_min_template_stddev": 10.0,
            "window_size": {"width": int(window.width), "height": int(window.height)},
        }
    )
    save_config(config)
    print("Đã lưu xác nhận nhóm. Vẫn đang dry-run; chưa thể gửi thật.")

