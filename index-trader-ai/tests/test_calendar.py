"""测试经济日历。"""

import tempfile
from datetime import date
from pathlib import Path

from calendar_fetcher import get_today_events, _is_monthly_opex, is_market_holiday


class TestCalendarFetcher:
    def test_today_events(self, tmp_path):
        cal = tmp_path / "cal.yaml"
        cal.write_text(
            """
events:
  - {date: "2026-03-25", name: "CPI Release", risk_level: high, behavior: data_reaction}
  - {date: "2026-03-26", name: "FOMC Meeting", risk_level: high, behavior: range_then_trend}
""",
            encoding="utf-8",
        )
        events = get_today_events(str(cal), today=date(2026, 3, 25))
        assert len(events) == 1
        assert events[0].name == "CPI Release"
        assert events[0].time == "08:30"
        assert events[0].importance == "high"

    def test_empty_day(self, tmp_path):
        cal = tmp_path / "cal.yaml"
        cal.write_text("events: []", encoding="utf-8")
        events = get_today_events(str(cal), today=date(2026, 3, 25))
        assert events == []

    def test_missing_file(self):
        events = get_today_events("/nonexistent/path.yaml", today=date(2026, 3, 25))
        assert events == []

    def test_fomc_default_time(self, tmp_path):
        cal = tmp_path / "cal.yaml"
        cal.write_text(
            """
events:
  - {date: "2026-03-18", name: "FOMC Meeting", risk_level: high, behavior: range_then_trend}
""",
            encoding="utf-8",
        )
        events = get_today_events(str(cal), today=date(2026, 3, 18))
        assert events[0].time == "14:00"

    def test_holiday_time_is_all_day(self, tmp_path):
        cal = tmp_path / "cal.yaml"
        cal.write_text(
            """
events:
  - {date: "2026-12-25", name: "Christmas", risk_level: high}
""",
            encoding="utf-8",
        )
        events = get_today_events(str(cal), today=date(2026, 12, 25))
        assert events[0].time == "全天"


class TestMonthlyOpex:
    def test_third_friday_march_2026(self):
        # 2026-03-20 is the third Friday of March
        assert _is_monthly_opex(date(2026, 3, 20))

    def test_not_third_friday(self):
        assert not _is_monthly_opex(date(2026, 3, 25))

    def test_not_friday(self):
        assert not _is_monthly_opex(date(2026, 3, 18))  # Wednesday


class TestMarketHoliday:
    def test_holiday(self, tmp_path):
        cal = tmp_path / "cal.yaml"
        cal.write_text(
            """
market_holidays:
  - "2026-12-25"
events: []
""",
            encoding="utf-8",
        )
        assert is_market_holiday(str(cal), today=date(2026, 12, 25))

    def test_not_holiday(self, tmp_path):
        cal = tmp_path / "cal.yaml"
        cal.write_text("market_holidays: []\nevents: []", encoding="utf-8")
        assert not is_market_holiday(str(cal), today=date(2026, 3, 25))
