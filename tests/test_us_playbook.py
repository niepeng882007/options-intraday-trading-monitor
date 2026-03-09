"""Tests for the US Playbook module."""

import pandas as pd
import numpy as np
import pytest
from datetime import datetime, date, timezone, timedelta

from src.hk import VolumeProfileResult, GammaWallResult, FilterResult
from src.us_playbook import (
    USRegimeType, USRegimeResult, USPlaybookResult, KeyLevels,
)
from src.us_playbook.indicators import calculate_vwap, calculate_us_rvol
from src.us_playbook.levels import (
    us_tick_size, extract_previous_day_hl,
    get_today_bars, get_history_bars, compute_volume_profile, build_key_levels,
)
from src.us_playbook.regime import classify_us_regime
from src.us_playbook.filter import check_us_filters, _is_monthly_opex
from src.us_playbook.playbook import format_us_playbook_message

ET = timezone(timedelta(hours=-5))


# ── Helpers ──

def _make_bars(prices: list[tuple], tz: str = "America/New_York") -> pd.DataFrame:
    """Create a DataFrame of 1m bars.

    prices: list of (datetime_str, open, high, low, close, volume)
    """
    rows = []
    for ts, o, h, l, c, v in prices:
        rows.append({"Open": o, "High": h, "Low": l, "Close": c, "Volume": v})
    idx = pd.DatetimeIndex(
        [pd.Timestamp(p[0], tz=tz) for p in prices], name="Datetime"
    )
    return pd.DataFrame(rows, index=idx)


# ── VWAP Tests ──

class TestVWAP:
    def test_basic_vwap(self):
        bars = _make_bars([
            ("2026-03-09 09:30:00", 100, 102, 99, 101, 10000),
            ("2026-03-09 09:31:00", 101, 103, 100, 102, 20000),
        ])
        vwap = calculate_vwap(bars)
        assert vwap > 0
        # VWAP should be between lowest low and highest high
        assert 99 <= vwap <= 103

    def test_empty_bars(self):
        assert calculate_vwap(pd.DataFrame()) == 0.0

    def test_volume_weighted(self):
        """Higher volume bar should pull VWAP toward it."""
        bars = _make_bars([
            ("2026-03-09 09:30:00", 100, 100, 100, 100, 1),
            ("2026-03-09 09:31:00", 200, 200, 200, 200, 1000),
        ])
        vwap = calculate_vwap(bars)
        # VWAP should be much closer to 200 than to 100
        assert vwap > 150


# ── RVOL Tests ──

class TestRVOL:
    def test_normal_rvol(self):
        """Same volume as history → RVOL ≈ 1.0."""
        today = _make_bars([
            ("2026-03-09 09:33:00", 100, 101, 99, 100, 10000),
            ("2026-03-09 09:34:00", 100, 101, 99, 100, 10000),
        ])
        history = _make_bars([
            ("2026-03-06 09:33:00", 100, 101, 99, 100, 10000),
            ("2026-03-06 09:34:00", 100, 101, 99, 100, 10000),
            ("2026-03-05 09:33:00", 100, 101, 99, 100, 10000),
            ("2026-03-05 09:34:00", 100, 101, 99, 100, 10000),
        ])
        rvol = calculate_us_rvol(today, history, skip_open_minutes=3)
        assert 0.8 <= rvol <= 1.2

    def test_high_rvol(self):
        """Double volume → RVOL ≈ 2.0."""
        today = _make_bars([
            ("2026-03-09 09:33:00", 100, 101, 99, 100, 20000),
            ("2026-03-09 09:34:00", 100, 101, 99, 100, 20000),
        ])
        history = _make_bars([
            ("2026-03-06 09:33:00", 100, 101, 99, 100, 10000),
            ("2026-03-06 09:34:00", 100, 101, 99, 100, 10000),
        ])
        rvol = calculate_us_rvol(today, history, skip_open_minutes=3)
        assert rvol >= 1.8

    def test_no_history(self):
        """No history → neutral RVOL (1.0)."""
        today = _make_bars([
            ("2026-03-09 09:33:00", 100, 101, 99, 100, 10000),
        ])
        rvol = calculate_us_rvol(today, pd.DataFrame(), skip_open_minutes=3)
        assert rvol == 1.0

    def test_rvol_skip_open_minutes(self):
        """Bars within the skip zone (09:30-09:32) should be excluded."""
        today = _make_bars([
            ("2026-03-09 09:30:00", 100, 101, 99, 100, 999999),  # skip zone
            ("2026-03-09 09:31:00", 100, 101, 99, 100, 999999),  # skip zone
            ("2026-03-09 09:32:00", 100, 101, 99, 100, 999999),  # skip zone
            ("2026-03-09 09:33:00", 100, 101, 99, 100, 10000),   # counted
            ("2026-03-09 09:34:00", 100, 101, 99, 100, 10000),   # counted
        ])
        history = _make_bars([
            ("2026-03-06 09:30:00", 100, 101, 99, 100, 999999),
            ("2026-03-06 09:31:00", 100, 101, 99, 100, 999999),
            ("2026-03-06 09:32:00", 100, 101, 99, 100, 999999),
            ("2026-03-06 09:33:00", 100, 101, 99, 100, 10000),
            ("2026-03-06 09:34:00", 100, 101, 99, 100, 10000),
        ])
        rvol = calculate_us_rvol(today, history, skip_open_minutes=3)
        # Only 09:33-09:34 bars count: same volume → RVOL ≈ 1.0
        assert 0.9 <= rvol <= 1.1

    def test_rvol_expanding_window(self):
        """At 10:15, more bars are used than at 09:45."""
        # Simulate bars from 09:33 to 10:14 (42 minutes of data)
        today_prices = [
            (f"2026-03-09 09:{m:02d}:00", 100, 101, 99, 100, 10000)
            for m in range(33, 60)
        ] + [
            (f"2026-03-09 10:{m:02d}:00", 100, 101, 99, 100, 10000)
            for m in range(0, 15)
        ]
        today = _make_bars(today_prices)

        history_prices = [
            (f"2026-03-06 09:{m:02d}:00", 100, 101, 99, 100, 10000)
            for m in range(33, 60)
        ] + [
            (f"2026-03-06 10:{m:02d}:00", 100, 101, 99, 100, 10000)
            for m in range(0, 15)
        ]
        history = _make_bars(history_prices)

        rvol = calculate_us_rvol(today, history, skip_open_minutes=3)
        # 42 bars of same volume → RVOL ≈ 1.0
        assert 0.9 <= rvol <= 1.1

    def test_rvol_all_bars_in_skip_zone(self):
        """If all today bars are in skip zone → return 1.0."""
        today = _make_bars([
            ("2026-03-09 09:30:00", 100, 101, 99, 100, 50000),
            ("2026-03-09 09:31:00", 100, 101, 99, 100, 50000),
        ])
        history = _make_bars([
            ("2026-03-06 09:33:00", 100, 101, 99, 100, 10000),
        ])
        rvol = calculate_us_rvol(today, history, skip_open_minutes=3)
        assert rvol == 1.0


# ── Key Levels Tests ──

class TestKeyLevels:
    def test_pdh_pdl_extraction(self):
        bars = _make_bars([
            # Day 1
            ("2026-03-06 09:30:00", 100, 105, 95, 102, 10000),
            ("2026-03-06 09:31:00", 102, 108, 96, 104, 10000),
            # Day 2 (today)
            ("2026-03-09 09:30:00", 104, 110, 98, 106, 10000),
        ])
        pdh, pdl = extract_previous_day_hl(bars)
        assert pdh == 108.0
        assert pdl == 95.0

    def test_us_tick_size(self):
        assert us_tick_size(550) == 0.50   # SPY
        assert us_tick_size(230) == 0.25   # AAPL
        assert us_tick_size(50) == 0.10    # Mid-cap
        assert us_tick_size(10) == 0.05    # Low-priced

    def test_volume_profile_integration(self):
        """VP should return valid POC/VAH/VAL."""
        bars = _make_bars([
            ("2026-03-06 09:30:00", 550, 552, 548, 551, 100000),
            ("2026-03-06 09:31:00", 551, 553, 549, 550, 100000),
            ("2026-03-06 09:32:00", 550, 551, 549, 550, 100000),
        ])
        vp = compute_volume_profile(bars)
        assert vp.poc > 0
        assert vp.vah >= vp.poc >= vp.val

    def test_get_today_history_bars(self):
        bars = _make_bars([
            ("2026-03-06 09:30:00", 100, 105, 95, 102, 10000),
            ("2026-03-09 09:30:00", 104, 110, 98, 106, 10000),
        ])
        today = get_today_bars(bars)
        history = get_history_bars(bars)
        assert len(today) == 1
        assert len(history) == 1
        assert today.index[0].date() == date(2026, 3, 9)
        assert history.index[0].date() == date(2026, 3, 6)

    def test_build_key_levels(self):
        vp = VolumeProfileResult(poc=550, vah=555, val=545)
        gw = GammaWallResult(
            call_wall_strike=560, put_wall_strike=540, max_pain=550,
        )
        kl = build_key_levels(vp, 558, 542, 555, 548, 552.5, gw)
        assert kl.poc == 550
        assert kl.pdh == 558
        assert kl.gamma_call_wall == 560
        assert kl.gamma_put_wall == 540
        assert kl.gamma_max_pain == 550


# ── Regime Tests ──

class TestUSRegime:
    def _vp(self, poc=550, vah=555, val=545):
        return VolumeProfileResult(poc=poc, vah=vah, val=val)

    def test_gap_and_go(self):
        result = classify_us_regime(
            price=560, prev_close=550, rvol=2.5,
            pmh=555, pml=548, vp=self._vp(),
        )
        assert result.regime == USRegimeType.GAP_AND_GO
        assert result.confidence > 0.5

    def test_trend_day(self):
        result = classify_us_regime(
            price=557, prev_close=556.5, rvol=1.3,
            pmh=556, pml=554, vp=self._vp(),
        )
        assert result.regime == USRegimeType.TREND_DAY

    def test_fade_chop(self):
        result = classify_us_regime(
            price=550, prev_close=551, rvol=0.8,
            pmh=555, pml=548, vp=self._vp(),
        )
        assert result.regime == USRegimeType.FADE_CHOP

    def test_unclear(self):
        result = classify_us_regime(
            price=550, prev_close=549.5, rvol=1.1,
            pmh=555, pml=548, vp=self._vp(),
        )
        assert result.regime == USRegimeType.UNCLEAR

    def test_spy_context_reduces_confidence(self):
        """SPY FADE_CHOP should reduce GAP_AND_GO confidence."""
        result_no_spy = classify_us_regime(
            price=560, prev_close=550, rvol=2.5,
            pmh=555, pml=548, vp=self._vp(),
        )
        result_with_spy = classify_us_regime(
            price=560, prev_close=550, rvol=2.5,
            pmh=555, pml=548, vp=self._vp(),
            spy_regime=USRegimeType.FADE_CHOP,
        )
        assert result_with_spy.confidence < result_no_spy.confidence

    def test_gap_and_go_unified_threshold(self):
        """Unified RVOL threshold — no more preliminary hack."""
        result = classify_us_regime(
            price=560, prev_close=550, rvol=1.8,
            pmh=555, pml=548, vp=self._vp(),
            gap_and_go_rvol=1.5,
        )
        assert result.regime == USRegimeType.GAP_AND_GO


# ── Filter Tests ──

class TestUSFilters:
    def test_fomc_day_blocked(self):
        result = check_us_filters(
            rvol=1.2, prev_high=100, prev_low=95,
            current_high=102, current_low=96,
            calendar_path="config/us_calendar.yaml",
            today=date(2026, 1, 28),  # FOMC
        )
        assert not result.tradeable
        assert result.risk_level == "blocked"

    def test_monthly_opex_elevated(self):
        result = check_us_filters(
            rvol=1.2, prev_high=100, prev_low=95,
            current_high=102, current_low=96,
            calendar_path="nonexistent.yaml",  # skip calendar
            today=date(2026, 1, 16),  # 3rd Friday of Jan 2026
        )
        assert result.tradeable
        assert result.risk_level == "elevated"

    def test_opex_plus_inside_day_low_rvol_blocked(self):
        result = check_us_filters(
            rvol=0.6, prev_high=100, prev_low=95,
            current_high=99, current_low=96,  # inside day
            calendar_path="nonexistent.yaml",
            today=date(2026, 1, 16),  # OpEx + Inside Day + low RVOL
        )
        assert not result.tradeable
        assert result.risk_level == "blocked"

    def test_inside_day_low_rvol_blocked(self):
        result = check_us_filters(
            rvol=0.6, prev_high=100, prev_low=95,
            current_high=99, current_low=96,  # inside day
            calendar_path="nonexistent.yaml",
            today=date(2026, 3, 10),  # normal day
        )
        assert not result.tradeable
        assert result.risk_level == "blocked"

    def test_normal_day(self):
        result = check_us_filters(
            rvol=1.2, prev_high=100, prev_low=95,
            current_high=105, current_low=94,
            calendar_path="nonexistent.yaml",
            today=date(2026, 3, 10),
        )
        assert result.tradeable
        assert result.risk_level == "normal"

    def test_is_monthly_opex(self):
        # 3rd Friday of January 2026 = Jan 16
        assert _is_monthly_opex(date(2026, 1, 16))
        # Not a Friday
        assert not _is_monthly_opex(date(2026, 1, 15))
        # Friday but not 3rd week
        assert not _is_monthly_opex(date(2026, 1, 9))
        assert not _is_monthly_opex(date(2026, 1, 23))


# ── Playbook Format Tests ──

class TestPlaybookFormat:
    def _make_result(self, regime_type=USRegimeType.TREND_DAY, update_type="morning") -> USPlaybookResult:
        return USPlaybookResult(
            symbol="AAPL",
            name="Apple",
            regime=USRegimeResult(
                regime=regime_type, confidence=0.72,
                rvol=1.35, price=554.2, gap_pct=0.42,
            ),
            key_levels=KeyLevels(
                poc=553.0, vah=556.5, val=550.5,
                pdh=558.3, pdl=548.7, pmh=555.0, pml=549.0,
                vwap=554.2,
                gamma_call_wall=562.0, gamma_put_wall=545.0, gamma_max_pain=550.0,
            ),
            volume_profile=VolumeProfileResult(poc=553, vah=556.5, val=550.5),
            gamma_wall=GammaWallResult(
                call_wall_strike=562, put_wall_strike=545, max_pain=550,
            ),
            filters=FilterResult(tradeable=True, risk_level="normal"),
            generated_at=datetime(2026, 3, 9, 9, 45, 0, tzinfo=ET),
        )

    def test_message_contains_all_sections(self):
        result = self._make_result()
        msg = format_us_playbook_message(result, "morning")
        assert "Apple" in msg
        assert "Playbook" in msg
        assert "关键点位" in msg
        assert "交易建议" in msg
        assert "风险过滤" in msg
        assert "RVOL" in msg

    def test_preliminary_label(self):
        result = self._make_result()
        msg = format_us_playbook_message(result, "morning")
        assert "初步" in msg

    def test_confirmed_label(self):
        result = self._make_result()
        msg = format_us_playbook_message(result, "confirm")
        assert "确认" in msg

    def test_market_context_section(self):
        result = self._make_result()
        spy = self._make_result(USRegimeType.FADE_CHOP)
        spy.symbol = "SPY"
        spy.name = "S&P 500 ETF"
        msg = format_us_playbook_message(result, "morning", spy_result=spy)
        assert "SPY" in msg
        assert "大盘环境" in msg
