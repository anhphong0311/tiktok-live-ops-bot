from types import SimpleNamespace

from tiktok_zalo_bot.tiktok import TikTokChecker


def test_checker_pins_requests_to_vietnam():
    assert TikTokChecker.VIETNAM_WEB_PARAMS == {
        "app_language": "vi",
        "browser_language": "vi-VN",
        "tz_name": "Asia/Ho_Chi_Minh",
        "priority_region": "VN",
        "region": "VN",
        "webcast_language": "vi",
    }


def test_parse_viewer_count_from_live_room_stats():
    payload = {"liveRoom": {"liveRoomStats": {"userCount": 137}}}

    assert TikTokChecker._parse_viewer_count(payload) == 137


def test_parse_viewer_count_tolerates_missing_data():
    assert TikTokChecker._parse_viewer_count({}) is None


def test_parse_viewer_count_rejects_unreliable_zero():
    payload = {"liveRoom": {"liveRoomStats": {"userCount": 0}}}

    assert TikTokChecker._parse_viewer_count(payload) is None


def test_parse_stream_viewer_count_uses_current_total_not_cumulative_total_user():
    event = SimpleNamespace(total=137, total_user=1823)

    assert TikTokChecker._parse_stream_viewer_count(event) == 137


def test_parse_stream_viewer_count_supports_older_protocol_name():
    event = SimpleNamespace(total=0, viewer_count=91, total_user=500)

    assert TikTokChecker._parse_stream_viewer_count(event) == 91


def test_parse_stream_viewer_count_rejects_zero():
    event = SimpleNamespace(total=0, viewer_count=0, total_user=500)

    assert TikTokChecker._parse_stream_viewer_count(event) is None
