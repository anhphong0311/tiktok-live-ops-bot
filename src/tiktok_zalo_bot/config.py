from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    pass


REQUIRED_SECTIONS = ("tiktok", "notification", "zalo", "runtime")


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path).resolve()
    if not config_path.exists():
        raise ConfigError(f"Không tìm thấy file cấu hình: {config_path}")
    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    for section in REQUIRED_SECTIONS:
        if not isinstance(data.get(section), dict):
            raise ConfigError(f"Thiếu section '{section}' trong {config_path}")
    interval = int(data["tiktok"].get("poll_interval_seconds", 60))
    if interval < 15:
        raise ConfigError("poll_interval_seconds phải >= 15 giây")
    username = str(data["tiktok"].get("username", "")).strip().lstrip("@")
    if not username:
        raise ConfigError("tiktok.username không được để trống")
    data["tiktok"]["username"] = username
    viewer_alert = data.get("viewer_alert")
    if viewer_alert is not None:
        if not isinstance(viewer_alert, dict):
            raise ConfigError("viewer_alert phải là một section cấu hình")
        minimum = int(viewer_alert.get("minimum_viewers", 0))
        confirmations = int(viewer_alert.get("confirmations", 2))
        recovery_margin = int(viewer_alert.get("recovery_margin", 10))
        reminder_minutes = int(viewer_alert.get("reminder_minutes", 15))
        if minimum < 0:
            raise ConfigError("viewer_alert.minimum_viewers phải >= 0")
        if confirmations < 1:
            raise ConfigError("viewer_alert.confirmations phải >= 1")
        if recovery_margin < 0:
            raise ConfigError("viewer_alert.recovery_margin phải >= 0")
        if reminder_minutes < 0:
            raise ConfigError("viewer_alert.reminder_minutes phải >= 0")
    gmv_report = data.get("gmv_report")
    if gmv_report is not None:
        if not isinstance(gmv_report, dict):
            raise ConfigError("gmv_report phải là một section cấu hình")
        interval_minutes = int(gmv_report.get("interval_minutes", 60))
        product_interval = int(gmv_report.get("product_interval_minutes", 5))
        poll_seconds = int(gmv_report.get("poll_interval_seconds", 60))
        error_reminder = int(gmv_report.get("error_reminder_minutes", 30))
        retry_minutes = int(gmv_report.get("dashboard_retry_minutes", 5))
        if interval_minutes < 1:
            raise ConfigError("gmv_report.interval_minutes phải >= 1")
        if product_interval < 1:
            raise ConfigError("gmv_report.product_interval_minutes phải >= 1")
        if poll_seconds < 15:
            raise ConfigError("gmv_report.poll_interval_seconds phải >= 15")
        if error_reminder < 1:
            raise ConfigError("gmv_report.error_reminder_minutes phải >= 1")
        if retry_minutes < 1:
            raise ConfigError("gmv_report.dashboard_retry_minutes phải >= 1")
        dashboard = gmv_report.get("dashboard")
        if not isinstance(dashboard, dict):
            raise ConfigError("Thiếu section gmv_report.dashboard")
        target = gmv_report.get("zalo")
        if not isinstance(target, dict):
            raise ConfigError("Thiếu section gmv_report.zalo")
    data["_config_path"] = str(config_path)
    data["_base_dir"] = str(config_path.parent)
    return data


def resolve_path(config: dict[str, Any], value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = Path(config["_base_dir"]) / path
    return path.resolve()


def save_config(config: dict[str, Any]) -> None:
    path = Path(config["_config_path"])
    clean = copy.deepcopy(config)
    clean.pop("_config_path", None)
    clean.pop("_base_dir", None)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
        yaml.safe_dump(clean, handle, allow_unicode=True, sort_keys=False)
    temp_path.replace(path)

