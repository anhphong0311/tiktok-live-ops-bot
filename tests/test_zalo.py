from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from tiktok_zalo_bot.zalo import ZaloAutomation, ZaloAutomationError


def make_config(tmp_path: Path) -> dict:
    executable = tmp_path / "Zalo" / "Zalo.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"test")
    return {
        "_base_dir": str(tmp_path),
        "zalo": {
            "executable_path": str(executable),
            "window_title_exact": "Zalo",
            "launch_timeout_seconds": 2,
        },
    }


def test_find_window_requires_exact_title_and_verified_process(tmp_path, monkeypatch):
    automation = ZaloAutomation(make_config(tmp_path))
    edge = SimpleNamespace(title="Zalo - Microsoft Edge", width=1900, height=1000, _hWnd=1)
    native = SimpleNamespace(title="Zalo", width=1900, height=1000, _hWnd=2)
    fake_module = SimpleNamespace(getAllWindows=lambda: [edge, native])
    monkeypatch.setitem(sys.modules, "pygetwindow", fake_module)
    monkeypatch.setattr(automation, "_is_zalo_handle", lambda handle: handle == 2)

    assert automation._find_window() is native


def test_zalo_process_must_be_inside_configured_install_root(tmp_path, monkeypatch):
    automation = ZaloAutomation(make_config(tmp_path))
    allowed = tmp_path / "Zalo" / "Zalo-26.10.11" / "Zalo.exe"
    monkeypatch.setattr(automation, "_window_executable", lambda handle: allowed)
    assert automation._is_zalo_handle(42)

    browser = tmp_path / "Edge" / "msedge.exe"
    monkeypatch.setattr(automation, "_window_executable", lambda handle: browser)
    assert not automation._is_zalo_handle(42)


def test_ensure_window_launches_zalo_and_waits_for_native_window(tmp_path, monkeypatch):
    automation = ZaloAutomation(make_config(tmp_path))
    native = SimpleNamespace(title="Zalo", width=1900, height=1000, _hWnd=2)
    results = iter([None, native])
    launched: list[tuple[list[str], str]] = []

    monkeypatch.setattr(automation, "_find_window", lambda: next(results))
    monkeypatch.setattr("tiktok_zalo_bot.zalo.time.sleep", lambda _: None)
    monkeypatch.setattr(
        "tiktok_zalo_bot.zalo.subprocess.Popen",
        lambda command, cwd, close_fds: launched.append((command, cwd)),
    )

    assert automation._ensure_window() is native
    assert launched == [
        ([str(automation._configured_executable())], str(automation._configured_executable().parent))
    ]


def test_foreground_mismatch_stops_before_input(tmp_path, monkeypatch):
    automation = ZaloAutomation(make_config(tmp_path))
    window = SimpleNamespace(_hWnd=42)
    monkeypatch.setattr(automation, "_foreground_handle", lambda: 99)
    monkeypatch.setattr(automation, "_is_zalo_handle", lambda handle: True)

    with pytest.raises(ZaloAutomationError, match="tránh gửi nhầm"):
        automation._assert_foreground(window, "test")


def test_select_group_clicks_calibrated_result_instead_of_pressing_enter(
    tmp_path, monkeypatch
):
    config = make_config(tmp_path)
    config["zalo"].update(
        {
            "group_name": "Chạy mắt",
            "back_to_list_point": {"x": 5, "y": 6},
            "search_point": {"x": 10, "y": 20},
            "search_result_point": {"x": 30, "y": 40},
            "search_wait_seconds": 0,
        }
    )
    automation = ZaloAutomation(config)
    window = object()
    actions: list[tuple] = []
    clipboard: list[str] = []
    pyautogui = SimpleNamespace(
        click=lambda *point: actions.append(("click", *point)),
        hotkey=lambda *keys: actions.append(("hotkey", *keys)),
        press=lambda key: actions.append(("press", key)),
    )
    pyperclip = SimpleNamespace(copy=clipboard.append)
    monkeypatch.setattr(
        automation,
        "_absolute_point",
        lambda _window, point: (point["x"], point["y"]),
    )
    monkeypatch.setattr(automation, "_assert_foreground", lambda *_: None)
    monkeypatch.setattr("tiktok_zalo_bot.zalo.time.sleep", lambda _: None)

    automation._select_group(window, pyautogui, pyperclip)

    assert clipboard == ["Chạy mắt"]
    assert ("click", 30, 40) in actions
    assert actions[-1] == ("press", "esc")
    assert ("press", "enter") not in actions


def _header_config(tmp_path: Path) -> tuple[dict, Image.Image]:
    config = make_config(tmp_path)
    template = Image.new("L", (160, 40), "white")
    ImageDraw.Draw(template).text((5, 5), "Chay mat", fill="black")
    template_path = tmp_path / "header.png"
    template.save(template_path)
    config["zalo"].update(
        {
            "group_name": "Chạy mắt",
            "header_region": {"left": 0, "top": 0, "width": 160, "height": 40},
            "header_template_path": str(template_path),
            "header_max_difference": 0.025,
            "header_max_changed_ratio": 0.08,
            "header_pixel_difference_threshold": 20,
            "header_min_template_stddev": 5.0,
        }
    )
    return config, template


def test_verify_header_accepts_exact_target_group(tmp_path, monkeypatch):
    config, template = _header_config(tmp_path)
    automation = ZaloAutomation(config)
    monkeypatch.setattr(automation, "_assert_foreground", lambda *_: None)
    monkeypatch.setattr(automation, "_absolute_region", lambda *_: (0, 0, 160, 40))
    pyautogui = SimpleNamespace(screenshot=lambda **_: template.copy())

    automation._verify_header(object(), pyautogui)


def test_verify_header_rejects_a_different_group_even_below_old_threshold(
    tmp_path, monkeypatch
):
    config, template = _header_config(tmp_path)
    wrong = template.copy()
    ImageDraw.Draw(wrong).rectangle((0, 0, 100, 25), fill="black")
    automation = ZaloAutomation(config)
    monkeypatch.setattr(automation, "_assert_foreground", lambda *_: None)
    monkeypatch.setattr(automation, "_absolute_region", lambda *_: (0, 0, 160, 40))
    pyautogui = SimpleNamespace(screenshot=lambda **_: wrong.copy())

    with pytest.raises(ZaloAutomationError, match="không khớp chính xác"):
        automation._verify_header(object(), pyautogui)
