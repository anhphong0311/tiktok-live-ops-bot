from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from .config import ConfigError, load_config, resolve_path, save_config
from .lock import SingleInstance
from .logging_setup import configure_logging
from .models import LiveObservation, LiveState
from .service import NotificationService
from .state import StateStore
from .tiktok import TikTokChecker
from .zalo import ZaloAutomation, ZaloAutomationError, setup_zalo


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TikTok-to-Zalo LIVE notifier")
    parser.add_argument("--config", default="config.yaml", help="Path to YAML config")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Monitor TikTok LIVE")
    run.add_argument("--once", action="store_true", help="Check exactly once")
    check = commands.add_parser("check-live", help="Query TikTok once; never opens Zalo")
    check.add_argument("--json", action="store_true")
    preview = commands.add_parser("preview", help="Fill message in confirmed group; never send")
    preview.add_argument("--message", help="Custom message; default uses configured template")
    setup = commands.add_parser("setup-zalo", help="Calibrate and confirm a Zalo group")
    setup.add_argument("--group-name", required=True)
    enable = commands.add_parser("enable-send", help="Explicitly enable real sends")
    enable.add_argument("--confirm", help="Required confirmation phrase")
    commands.add_parser("disable-send", help="Return to safe dry-run mode")
    threshold = commands.add_parser(
        "set-viewer-threshold", help="Enable low-viewer alerts and set the daily minimum"
    )
    threshold.add_argument("minimum", type=int, help="Minimum acceptable live viewers")
    commands.add_parser("disable-viewer-alert", help="Disable low-viewer alerts")
    commands.add_parser("status", help="Show monitor and notification state")
    resolve = commands.add_parser("resolve", help="Resolve an uncertain send manually")
    resolve.add_argument("--notification-id", type=int, required=True)
    resolve.add_argument("--outcome", choices=("sent", "not-sent"), required=True)
    simulate = commands.add_parser("simulate", help="Exercise state logic without TikTok network")
    simulate.add_argument("state", choices=[state.value for state in LiveState])
    simulate.add_argument("--room-id")
    simulate.add_argument("--viewers", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        log_value = config["runtime"]["log_path"]
        configure_logging(
            resolve_path(config, log_value),
            str(config["runtime"].get("log_level", "INFO")),
        )
        store = StateStore(resolve_path(config, config["runtime"]["database_path"]))
        service = NotificationService(config, store)

        if args.command == "check-live":
            observation = asyncio.run(
                TikTokChecker(
                    config["tiktok"]["username"],
                    float(config["tiktok"].get("request_timeout_seconds", 20)),
                ).check()
            )
            data = {
                "state": observation.state.value,
                "room_id": observation.room_id,
                "viewer_count": observation.viewer_count,
                "checked_at": observation.checked_at.isoformat(),
                "detail": observation.detail,
            }
            print(json.dumps(data, ensure_ascii=False) if args.json else data)
            return 2 if observation.state is LiveState.UNKNOWN else 0

        if args.command == "setup-zalo":
            setup_zalo(config, args.group_name.strip())
            return 0

        if args.command == "enable-send":
            _enable_send(config, args.confirm)
            return 0

        if args.command == "disable-send":
            config["zalo"]["send_enabled"] = False
            config["zalo"]["dry_run"] = True
            save_config(config)
            print("Đã tắt gửi thật và bật dry-run.")
            return 0

        if args.command == "set-viewer-threshold":
            if args.minimum < 1:
                raise ConfigError("Ngưỡng mắt phải từ 1 trở lên")
            viewer_alert = config.setdefault("viewer_alert", {})
            viewer_alert["enabled"] = True
            viewer_alert["minimum_viewers"] = args.minimum
            viewer_alert.setdefault("confirmations", 2)
            viewer_alert.setdefault("recovery_margin", 10)
            viewer_alert.setdefault("reminder_minutes", 15)
            save_config(config)
            print(
                f"Đã bật cảnh báo khi số mắt dưới {args.minimum}. "
                "Bot đang chạy sẽ tự nạp ngưỡng mới."
            )
            return 0

        if args.command == "disable-viewer-alert":
            config.setdefault("viewer_alert", {})["enabled"] = False
            save_config(config)
            print("Đã tắt cảnh báo số mắt thấp.")
            return 0

        if args.command == "preview":
            message = args.message or service.format_message(datetime.now(UTC))
            result = ZaloAutomation(config).deliver(message, real_send=False)
            print(f"Đã điền preview, CHƯA nhấn gửi. Ảnh: {result.screenshot_path}")
            return 0

        if args.command == "status":
            _print_status(config, store)
            return 0

        if args.command == "resolve":
            store.resolve_uncertain(args.notification_id, args.outcome)
            print("Đã ghi nhận kết quả thủ công.")
            return 0

        if args.command == "simulate":
            observation = LiveObservation(
                LiveState(args.state),
                datetime.now(UTC),
                args.room_id,
                "CLI simulation",
                args.viewers,
            )
            asyncio.run(service.run_once(observation))
            _print_status(config, store)
            return 0

        if args.command == "run":
            lock_path = resolve_path(config, "data/bot.lock")
            with SingleInstance(lock_path):
                if args.once:
                    store.recover_inflight()
                    asyncio.run(service.run_once())
                else:
                    asyncio.run(service.run_forever())
            return 0
        return 1
    except (ConfigError, ZaloAutomationError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Stopped.")
        return 130


def _enable_send(config: dict, provided: str | None) -> None:
    zalo = config["zalo"]
    if not zalo.get("group_confirmed") or not zalo.get("group_name"):
        raise ConfigError("Phải chạy setup-zalo và xác nhận nhóm trước")
    required = f"BAT GUI THAT: {zalo['group_name']}"
    answer = provided if provided is not None else input(f"Nhập '{required}': ").strip()
    if answer != required:
        raise ConfigError("Câu xác nhận không khớp; gửi thật vẫn bị tắt")
    zalo["send_enabled"] = True
    zalo["dry_run"] = False
    save_config(config)
    print(f"Đã BẬT gửi thật cho nhóm: {zalo['group_name']}")


def _print_status(config: dict, store: StateStore) -> None:
    safe_zalo = {
        "group_name": config["zalo"].get("group_name"),
        "group_confirmed": config["zalo"].get("group_confirmed"),
        "send_enabled": config["zalo"].get("send_enabled"),
        "dry_run": config["zalo"].get("dry_run"),
    }
    print("Zalo:", json.dumps(safe_zalo, ensure_ascii=False))
    print(
        "Viewer alert:",
        json.dumps(config.get("viewer_alert", {}), ensure_ascii=False),
    )
    print("Monitor:", json.dumps(store.get_monitor_state(), ensure_ascii=False))
    print("Notifications:")
    for row in store.list_notifications():
        print(json.dumps(row, ensure_ascii=False))


if __name__ == "__main__":
    raise SystemExit(main())

