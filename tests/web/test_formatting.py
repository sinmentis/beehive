from datetime import datetime, timedelta, timezone

from beehive.localization import localizer_for
from beehive.web.formatting import fetch_stats_label, freshness_exact_time, freshness_label, host_local_time_label, next_fetch_countdown, relative_time

EN = localizer_for("en")
ZH = localizer_for("zh-CN")


def test_relative_time_under_a_minute_says_just_now():
    now = datetime.now(timezone.utc).isoformat()
    assert relative_time(now, EN) == "Just now"


def test_relative_time_under_an_hour_shows_minutes():
    ts = (datetime.now(timezone.utc) - timedelta(minutes=25)).isoformat()
    assert relative_time(ts, EN) == "25 minutes ago"


def test_relative_time_under_a_day_shows_hours():
    ts = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    assert relative_time(ts, EN) == "5 hours ago"


def test_relative_time_at_and_past_a_day_shows_days():
    ts = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    assert relative_time(ts, EN) == "1 day ago"
    ts = (datetime.now(timezone.utc) - timedelta(days=3, hours=2)).isoformat()
    assert relative_time(ts, EN) == "3 days ago"


def test_relative_time_handles_naive_iso_strings_as_utc():
    naive_now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    assert relative_time(naive_now, EN) == "Just now"


def test_relative_time_uses_the_selected_locale():
    ts = (datetime.now(timezone.utc) - timedelta(minutes=25)).isoformat()
    assert relative_time(ts, ZH) == "25 分钟前"


def test_host_local_time_label_formats_in_pacific_auckland():
    # 2026-07-09T00:00:00 UTC is 2026-07-09T12:00 in Pacific/Auckland (NZST, UTC+12 in July)
    assert host_local_time_label("2026-07-09T00:00:00+00:00") == "2026-07-09 12:00"


def test_freshness_label_says_never_fetched_when_no_sources_have_fetched():
    assert freshness_label([{"last_fetch_at": None}], EN) == "Not fetched yet"


def test_freshness_label_uses_the_most_recent_fetch_across_sources():
    now_iso = datetime.now(timezone.utc).isoformat()
    old_iso = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    label = freshness_label([{"last_fetch_at": old_iso}, {"last_fetch_at": now_iso}], EN)
    assert label == "Fetched Just now"


def test_freshness_label_uses_the_selected_locale():
    now_iso = datetime.now(timezone.utc).isoformat()
    label = freshness_label([{"last_fetch_at": now_iso}], ZH)
    assert label == "刚刚抓取"


def _fetch_source(last_fetch_at=None):
    return {"last_fetch_at": last_fetch_at}


def test_next_fetch_countdown_just_before_a_boundary():
    now = datetime(2026, 7, 9, 3, 45, tzinfo=timezone.utc)
    assert next_fetch_countdown([_fetch_source()], 24, now, EN) == "Fetching in 15 minutes"


def test_next_fetch_countdown_just_after_a_boundary():
    now = datetime(2026, 7, 9, 4, 5, tzinfo=timezone.utc)
    assert next_fetch_countdown([_fetch_source()], 24, now, EN) == "Fetching in 2 hours"


def test_next_fetch_countdown_wraps_to_tomorrow_after_the_last_slot():
    now = datetime(2026, 7, 9, 10, 30, tzinfo=timezone.utc)
    assert next_fetch_countdown([_fetch_source()], 24, now, EN) == "Fetching in 2 hours"


def test_next_fetch_countdown_never_goes_negative_right_at_a_boundary():
    now = datetime(2026, 7, 9, 4, 0, 0, 500000, tzinfo=timezone.utc)
    assert next_fetch_countdown([_fetch_source()], 24, now, EN) == "Fetching in 0 minutes"


def test_next_fetch_countdown_uses_the_channels_interval():
    now = datetime(2026, 7, 13, 9, 59, tzinfo=timezone.utc)
    source = _fetch_source("2026-07-13T07:00:00+00:00")
    assert next_fetch_countdown([source], 24, now, EN) == "Fetching in 21 hours"


def test_next_fetch_countdown_is_empty_without_sources():
    now = datetime(2026, 7, 13, 9, 59, tzinfo=timezone.utc)
    assert next_fetch_countdown([], 24, now, EN) == ""


def test_next_fetch_countdown_uses_the_selected_locale():
    now = datetime(2026, 7, 9, 3, 45, tzinfo=timezone.utc)
    assert next_fetch_countdown([_fetch_source()], 24, now, ZH) == "15 分钟后抓取"


def test_freshness_exact_time_is_empty_when_never_fetched():
    assert freshness_exact_time([{"last_fetch_at": None}]) == ""


def test_freshness_exact_time_matches_the_most_recent_fetch_across_sources():
    old_iso = "2026-07-09T02:00:00+00:00"
    newest_iso = "2026-07-09T03:30:00+00:00"
    label = freshness_exact_time([{"last_fetch_at": old_iso}, {"last_fetch_at": newest_iso}])
    assert label == host_local_time_label(newest_iso)


def test_fetch_stats_label_sums_across_a_channels_sources():
    sources = [
        {"last_fetch_raw_count": 30, "last_fetch_new_count": 10},
        {"last_fetch_raw_count": 20, "last_fetch_new_count": 10},
    ]
    label = fetch_stats_label(sources, EN)
    assert "50" in label
    assert "20" in label
    assert "40%" in label


def test_fetch_stats_label_handles_never_fetched():
    assert fetch_stats_label([{"last_fetch_raw_count": None, "last_fetch_new_count": None}], EN) == ""
