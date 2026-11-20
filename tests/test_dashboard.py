from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tiktok_zalo_bot.dashboard import (
    DashboardAccessError,
    DashboardSnapshot,
    parse_compact_number,
    parse_dashboard_times,
    parse_product_rows,
    validate_dashboard_freshness,
)


def test_parse_compact_ads_cost() -> None:
    assert parse_compact_number("162.78K") == 162_780
    assert parse_compact_number("2.7M") == 2_700_000


def test_parse_product_rows_keeps_only_positive_gmv_and_sorts_descending() -> None:
    rows = [
        ["1 Product A ID: 1735947371752293944", "Pinned", "3", "424.724₫", "3", "0"],
        ["2 Product B ID: 1735947048521664056", "", "1", "155.504₫", "1", "0"],
        ["3 Product C ID: 1735947036359755320", "", "0", "0₫", "0", "0"],
        ["4 Product D ID: 1736545813363197882", "", "1", "715.122₫", "1", "0"],
    ]

    products = parse_product_rows(rows)

    assert [item.product_id for item in products] == [
        "1736545813363197882",
        "1735947371752293944",
        "1735947048521664056",
    ]
    assert products[0].attributed_gmv_vnd == 715_122


def test_parse_dashboard_time_range() -> None:
    now = datetime(2026, 11, 20, 6, tzinfo=UTC)
    started, dashboard_time = parse_dashboard_times(
        ["Nov 20, 09:31:04 - Nov 20, 12:53:27 UTC+07:00"], now=now
    )

    assert started == datetime(2026, 11, 20, 2, 31, 4, tzinfo=UTC)
    assert dashboard_time == datetime(2026, 11, 20, 5, 53, 27, tzinfo=UTC)


def test_fresh_dashboard_snapshot_is_accepted() -> None:
    captured = datetime(2026, 11, 20, 8, 30, tzinfo=UTC)
    value = DashboardSnapshot(
        room_id="room",
        captured_at=captured,
        attributed_gmv_vnd=14_087_634,
        ads_cost_vnd=201_610,
        products=(),
        dashboard_time=captured - timedelta(minutes=4),
    )

    validate_dashboard_freshness(value, max_age_minutes=10)


def test_stale_dashboard_snapshot_is_rejected() -> None:
    captured = datetime(2026, 11, 20, 8, 48, tzinfo=UTC)
    value = DashboardSnapshot(
        room_id="room",
        captured_at=captured,
        attributed_gmv_vnd=9_617_153,
        ads_cost_vnd=166_650,
        products=(),
        dashboard_time=datetime(2026, 11, 20, 6, 25, 49, tzinfo=UTC),
    )

    with pytest.raises(DashboardAccessError, match="dữ liệu cũ"):
        validate_dashboard_freshness(value, max_age_minutes=10)
