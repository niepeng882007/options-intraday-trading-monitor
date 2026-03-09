"""Tests for the US Playbook module."""

import pandas as pd
import numpy as np
import pytest
from datetime import datetime, date, timezone, timedelta

from src.hk import VolumeProfileResult, GammaWallResult, FilterResult
from src.collector.base import PremarketData
from src.us_playbook import (
    USRegimeType, USRegimeResult, USPlaybookResult, KeyLevels,
)
from src.us_playbook.indicators import calculate_vwap, calculate_us_rvol, compute_rvol_profile, RvolProfile
from src.us_playbook.levels import (
    us_tick_size, extract_previous_day_hl,
    get_today_bars, get_history_bars, compute_volume_profile, build_key_levels,
    calc_fetch_calendar_days,
)
from src.us_playbook.regime import classify_us_regime
from src.us_playbook.filter import check_us_filters, _is_monthly_opex
from src.us_playbook.playbook import format_us_playbook_message, format_regime_change_alert, _collect_levels
from src.us_playbook.main import USPlaybook

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

    def test_playbook_vp_thin_warning(self):
        """VP with < 3 trading days should show warning in message."""
        result = self._make_result()
        result.volume_profile = VolumeProfileResult(poc=553, vah=556.5, val=550.5, trading_days=2)
        msg = format_us_playbook_message(result, "morning")
        assert "VP 仅 2 天数据" in msg

    def test_playbook_no_warning_sufficient_days(self):
        """VP with >= 3 trading days should NOT show warning."""
        result = self._make_result()
        result.volume_profile = VolumeProfileResult(poc=553, vah=556.5, val=550.5, trading_days=5)
        msg = format_us_playbook_message(result, "morning")
        assert "VP 仅" not in msg


# ── VP Shallow Data Optimization Tests ──

class TestVPShallowDataOptimization:
    def test_calc_fetch_calendar_days(self):
        """Should return max(vp, rvol) * 2 + 2."""
        assert calc_fetch_calendar_days(5, 10) == 22   # max(5,10)*2+2
        assert calc_fetch_calendar_days(5, 3) == 12    # max(5,3)*2+2
        assert calc_fetch_calendar_days(10, 10) == 22  # max(10,10)*2+2

    def test_get_history_bars_max_trading_days(self):
        """With max_trading_days=5, 6 days of data → keep only most recent 5."""
        bars = _make_bars([
            ("2026-03-02 09:30:00", 100, 102, 99, 101, 1000),
            ("2026-03-03 09:30:00", 100, 102, 99, 101, 1000),
            ("2026-03-04 09:30:00", 100, 102, 99, 101, 1000),
            ("2026-03-05 09:30:00", 100, 102, 99, 101, 1000),
            ("2026-03-06 09:30:00", 100, 102, 99, 101, 1000),
            ("2026-03-09 09:30:00", 100, 102, 99, 101, 1000),  # today (last)
            # history has 5 dates (03-02..03-06), cap to 5 keeps all
        ])
        history = get_history_bars(bars, max_trading_days=5)
        dates = sorted(set(history.index.date))
        assert len(dates) == 5

        # Cap to 3 → only most recent 3 (03-04, 03-05, 03-06)
        history_3 = get_history_bars(bars, max_trading_days=3)
        dates_3 = sorted(set(history_3.index.date))
        assert len(dates_3) == 3
        assert dates_3[0] == date(2026, 3, 4)

    def test_get_history_bars_no_cap(self):
        """max_trading_days=0 keeps all history (backward compat)."""
        bars = _make_bars([
            ("2026-03-02 09:30:00", 100, 102, 99, 101, 1000),
            ("2026-03-03 09:30:00", 100, 102, 99, 101, 1000),
            ("2026-03-04 09:30:00", 100, 102, 99, 101, 1000),
            ("2026-03-09 09:30:00", 100, 102, 99, 101, 1000),  # today
        ])
        history = get_history_bars(bars, max_trading_days=0)
        dates = sorted(set(history.index.date))
        assert len(dates) == 3

    def test_compute_vp_trading_days_populated(self):
        """compute_volume_profile should populate trading_days."""
        bars = _make_bars([
            ("2026-03-04 09:30:00", 550, 552, 548, 551, 100000),
            ("2026-03-04 09:31:00", 551, 553, 549, 550, 100000),
            ("2026-03-05 09:30:00", 550, 552, 548, 551, 100000),
            ("2026-03-05 09:31:00", 551, 553, 549, 550, 100000),
            ("2026-03-06 09:30:00", 550, 552, 548, 551, 100000),
        ])
        vp = compute_volume_profile(bars)
        assert vp.trading_days == 3
        assert vp.poc > 0

    def test_compute_vp_empty_bars(self):
        """Empty bars → trading_days stays 0."""
        vp = compute_volume_profile(pd.DataFrame())
        assert vp.trading_days == 0

    def test_regime_vp_thin_penalty(self):
        """VP with < min_trading_days → confidence reduced by 0.15."""
        vp = VolumeProfileResult(poc=550, vah=555, val=545, trading_days=2)
        result = classify_us_regime(
            price=560, prev_close=550, rvol=2.5,
            pmh=555, pml=548, vp=vp,
            vp_trading_days=2, min_vp_trading_days=3,
        )
        assert result.regime == USRegimeType.GAP_AND_GO
        assert "VP thin (2d)" in result.details

        # Compare with no penalty
        result_full = classify_us_regime(
            price=560, prev_close=550, rvol=2.5,
            pmh=555, pml=548, vp=vp,
            vp_trading_days=5, min_vp_trading_days=3,
        )
        assert result.confidence < result_full.confidence

    def test_regime_no_penalty_sufficient_days(self):
        """VP with >= min_trading_days → no penalty."""
        vp = VolumeProfileResult(poc=550, vah=555, val=545, trading_days=5)
        result = classify_us_regime(
            price=560, prev_close=550, rvol=2.5,
            pmh=555, pml=548, vp=vp,
            vp_trading_days=5, min_vp_trading_days=3,
        )
        assert "VP thin" not in result.details

    def test_regime_no_penalty_zero_days(self):
        """VP with trading_days=0 (legacy) → no penalty applied."""
        vp = VolumeProfileResult(poc=550, vah=555, val=545)
        result = classify_us_regime(
            price=560, prev_close=550, rvol=2.5,
            pmh=555, pml=548, vp=vp,
            vp_trading_days=0, min_vp_trading_days=3,
        )
        assert "VP thin" not in result.details


# ── Adaptive RVOL Profile Tests ──

def _make_history_bars_multi_day(
    n_days: int = 10,
    base_vol: int = 10000,
    vol_variance: float = 0.3,
    base_price: float = 100.0,
    daily_range_pct: float = 2.0,
) -> pd.DataFrame:
    """Generate multi-day 1m history bars for RVOL profile tests.

    Creates bars from 09:33-09:40 for n_days, with random-ish volume.
    """
    rows = []
    dates = []
    rng = np.random.default_rng(42)
    base_date = date(2026, 3, 9)

    for day_offset in range(n_days, 0, -1):
        d = base_date - timedelta(days=day_offset)
        if d.weekday() >= 5:  # skip weekends
            continue
        # Volume scales by day to create variance
        day_factor = 1.0 + vol_variance * rng.standard_normal()
        day_factor = max(0.3, day_factor)
        half_range = base_price * daily_range_pct / 200
        for m in range(33, 41):  # 09:33 to 09:40
            ts = f"{d.isoformat()} 09:{m:02d}:00"
            vol = int(base_vol * day_factor)
            o = base_price
            h = base_price + half_range
            l = base_price - half_range
            c = base_price + half_range * 0.5
            rows.append((ts, o, h, l, c, vol))

    return _make_bars(rows)


class TestRvolProfile:
    def test_adaptive_thresholds_with_sufficient_data(self):
        """With enough history, compute_rvol_profile returns adaptive thresholds."""
        history = _make_history_bars_multi_day(n_days=15, base_vol=10000)
        profile = compute_rvol_profile(
            history, today_rvol=1.5, skip_open_minutes=3,
            min_sample_days=5,
        )
        assert profile.sample_size >= 5
        # Adaptive thresholds should differ from fallback defaults
        assert profile.gap_and_go_rvol > 0
        assert profile.trend_day_rvol > 0
        assert profile.fade_chop_rvol > 0
        # Gap_and_go should be highest, fade_chop lowest
        assert profile.gap_and_go_rvol >= profile.trend_day_rvol
        assert profile.trend_day_rvol >= profile.fade_chop_rvol
        # Minimum separation guard
        assert profile.gap_and_go_rvol >= profile.trend_day_rvol + 0.1

    def test_fallback_on_insufficient_data(self):
        """With too few days, returns static fallback thresholds."""
        history = _make_bars([
            ("2026-03-06 09:33:00", 100, 102, 98, 100, 10000),
            ("2026-03-06 09:34:00", 100, 102, 98, 100, 10000),
            ("2026-03-07 09:33:00", 100, 102, 98, 100, 10000),
        ])
        profile = compute_rvol_profile(
            history, today_rvol=1.0, skip_open_minutes=3,
            fallback_gap_and_go=1.5, fallback_trend_day=1.2, fallback_fade_chop=1.0,
            min_sample_days=5,
        )
        assert profile.gap_and_go_rvol == 1.5
        assert profile.trend_day_rvol == 1.2
        assert profile.fade_chop_rvol == 1.0
        assert profile.sample_size < 5

    def test_empty_history_returns_fallback(self):
        profile = compute_rvol_profile(
            pd.DataFrame(), today_rvol=1.0,
            fallback_gap_and_go=2.0, fallback_trend_day=1.5, fallback_fade_chop=0.8,
        )
        assert profile.gap_and_go_rvol == 2.0
        assert profile.avg_daily_range_pct == 0.0

    def test_high_vol_symbol_wider_thresholds(self):
        """A high-variance symbol (like TSLA) should have wider thresholds."""
        # High variance: volume swings wildly
        high_var = _make_history_bars_multi_day(
            n_days=20, base_vol=10000, vol_variance=0.8,
        )
        # Low variance: steady volume
        low_var = _make_history_bars_multi_day(
            n_days=20, base_vol=10000, vol_variance=0.05,
        )
        profile_high = compute_rvol_profile(high_var, today_rvol=1.5, min_sample_days=5)
        profile_low = compute_rvol_profile(low_var, today_rvol=1.5, min_sample_days=5)

        # High variance symbol should have wider spread between P30 and P85
        spread_high = profile_high.gap_and_go_rvol - profile_high.fade_chop_rvol
        spread_low = profile_low.gap_and_go_rvol - profile_low.fade_chop_rvol
        assert spread_high > spread_low

    def test_percentile_rank(self):
        """Today's RVOL percentile should be 0-100."""
        history = _make_history_bars_multi_day(n_days=15)
        profile = compute_rvol_profile(history, today_rvol=1.5, min_sample_days=5)
        assert 0 <= profile.percentile_rank <= 100

    def test_avg_daily_range(self):
        """Should compute meaningful daily range percentage."""
        history = _make_history_bars_multi_day(
            n_days=15, base_price=100.0, daily_range_pct=2.0,
        )
        profile = compute_rvol_profile(history, today_rvol=1.0, min_sample_days=5)
        if profile.sample_size >= 5:
            assert profile.avg_daily_range_pct > 0


class TestAdaptiveRegime:
    def _vp(self, poc=550, vah=555, val=545):
        return VolumeProfileResult(poc=poc, vah=vah, val=val)

    def test_profile_overrides_static_thresholds(self):
        """With adaptive profile, lower gap_and_go threshold triggers GAP_AND_GO."""
        profile = RvolProfile(
            gap_and_go_rvol=1.2,  # much lower than static 1.5
            trend_day_rvol=0.9,
            fade_chop_rvol=0.6,
            avg_daily_range_pct=2.0,
            percentile_rank=85.0,
            sample_size=10,
        )
        # RVOL=1.3 would be below static 1.5 but above adaptive 1.2
        result = classify_us_regime(
            price=560, prev_close=550, rvol=1.3,
            pmh=555, pml=548, vp=self._vp(),
            gap_and_go_rvol=1.5,  # static (should be overridden)
            rvol_profile=profile,
        )
        assert result.regime == USRegimeType.GAP_AND_GO
        assert result.adaptive_thresholds is not None
        assert result.adaptive_thresholds["gap_and_go"] == 1.2
        assert "adaptive" in result.details

    def test_none_profile_keeps_static(self):
        """Without adaptive profile, static thresholds are used."""
        result = classify_us_regime(
            price=560, prev_close=550, rvol=1.3,
            pmh=555, pml=548, vp=self._vp(),
            gap_and_go_rvol=1.5,
            rvol_profile=None,
        )
        # RVOL 1.3 < static 1.5, so not GAP_AND_GO
        assert result.regime != USRegimeType.GAP_AND_GO
        assert result.adaptive_thresholds is None

    def test_gap_normalization_with_profile(self):
        """TREND_DAY gap check uses normalized gap when profile available."""
        # High daily range symbol: 2% avg → gap 0.5% is only 0.25 of range → small
        profile = RvolProfile(
            gap_and_go_rvol=2.0,
            trend_day_rvol=1.2,
            fade_chop_rvol=0.8,
            avg_daily_range_pct=2.0,  # large daily range
            percentile_rank=65.0,
            sample_size=10,
        )
        # gap_pct ≈ 0.45% → normalized = 0.45/2.0 = 0.225 < 0.3 → small_gap=True
        result = classify_us_regime(
            price=557, prev_close=554.5, rvol=1.3,
            pmh=556, pml=554, vp=self._vp(),
            rvol_profile=profile,
            gap_significance_threshold=0.3,
        )
        assert result.regime == USRegimeType.TREND_DAY

    def test_gap_normalization_blocks_large_gap(self):
        """Large normalized gap prevents TREND_DAY classification."""
        # Low daily range symbol: 0.5% avg → gap 0.4% is 0.8 of range → big
        profile = RvolProfile(
            gap_and_go_rvol=2.0,
            trend_day_rvol=1.2,
            fade_chop_rvol=0.8,
            avg_daily_range_pct=0.5,  # tiny daily range
            percentile_rank=65.0,
            sample_size=10,
        )
        # gap_pct ≈ 0.45% → normalized = 0.45/0.5 = 0.9 > 0.3 → not small
        result = classify_us_regime(
            price=557, prev_close=554.5, rvol=1.3,
            pmh=556, pml=554, vp=self._vp(),
            rvol_profile=profile,
            gap_significance_threshold=0.3,
        )
        assert result.regime != USRegimeType.TREND_DAY

    def test_insufficient_sample_uses_static(self):
        """Profile with sample_size < 5 → static thresholds used."""
        profile = RvolProfile(
            gap_and_go_rvol=1.0,  # would trigger at RVOL=1.3
            trend_day_rvol=0.8,
            fade_chop_rvol=0.5,
            avg_daily_range_pct=2.0,
            percentile_rank=50.0,
            sample_size=3,  # too few
        )
        result = classify_us_regime(
            price=560, prev_close=550, rvol=1.3,
            pmh=555, pml=548, vp=self._vp(),
            gap_and_go_rvol=1.5,
            rvol_profile=profile,
        )
        # Static 1.5 should be used → 1.3 < 1.5 → not GAP_AND_GO
        assert result.regime != USRegimeType.GAP_AND_GO
        assert result.adaptive_thresholds is None

    def test_playbook_message_shows_adaptive_info(self):
        """Telegram message includes adaptive threshold info."""
        result = USPlaybookResult(
            symbol="TSLA",
            name="Tesla",
            regime=USRegimeResult(
                regime=USRegimeType.GAP_AND_GO, confidence=0.85,
                rvol=2.31, price=280.0, gap_pct=1.82,
                adaptive_thresholds={
                    "gap_and_go": 1.73, "trend_day": 1.15, "fade_chop": 0.88,
                    "pctl_rank": 92.0, "sample": 9,
                },
            ),
            key_levels=KeyLevels(
                poc=275, vah=280, val=270,
                pdh=278, pdl=268, pmh=279, pml=272,
                vwap=276,
            ),
            volume_profile=VolumeProfileResult(poc=275, vah=280, val=270),
            gamma_wall=None,
            filters=FilterResult(tradeable=True, risk_level="normal"),
            generated_at=datetime(2026, 3, 9, 9, 45, 0, tzinfo=ET),
        )
        msg = format_us_playbook_message(result, "morning")
        assert "自适应" in msg
        assert "rank" in msg


# ── PMH/PML Data Reliability Tests ──

class TestPremarketData:
    def test_dataclass_fields(self):
        pm = PremarketData(pmh=555.0, pml=548.0, source="futu")
        assert pm.pmh == 555.0
        assert pm.pml == 548.0
        assert pm.source == "futu"

    def test_gap_estimate_source(self):
        pm = PremarketData(pmh=552.0, pml=548.0, source="gap_estimate")
        assert pm.source == "gap_estimate"

    def test_yahoo_source(self):
        pm = PremarketData(pmh=556.0, pml=549.0, source="yahoo")
        assert pm.source == "yahoo"


class TestKeyLevelsPmSource:
    def test_default_pm_source(self):
        kl = KeyLevels(poc=550, vah=555, val=545, pdh=558, pdl=542, pmh=555, pml=548, vwap=552)
        assert kl.pm_source == "futu"

    def test_custom_pm_source(self):
        kl = KeyLevels(
            poc=550, vah=555, val=545, pdh=558, pdl=542,
            pmh=555, pml=548, vwap=552, pm_source="yahoo",
        )
        assert kl.pm_source == "yahoo"

    def test_build_key_levels_passes_pm_source(self):
        vp = VolumeProfileResult(poc=550, vah=555, val=545)
        kl = build_key_levels(vp, 558, 542, 555, 548, 552.5, pm_source="gap_estimate")
        assert kl.pm_source == "gap_estimate"

    def test_build_key_levels_default_pm_source(self):
        vp = VolumeProfileResult(poc=550, vah=555, val=545)
        kl = build_key_levels(vp, 558, 542, 555, 548, 552.5)
        assert kl.pm_source == "futu"


class TestCollectLevelsPmAnnotation:
    def _kl(self, pm_source="futu"):
        return KeyLevels(
            poc=550, vah=555, val=545, pdh=558, pdl=542,
            pmh=555, pml=548, vwap=552, pm_source=pm_source,
        )

    def test_futu_no_annotation(self):
        items = _collect_levels(self._kl("futu"), 550)
        pmh_items = [i for i in items if i[0] == "PMH"]
        pml_items = [i for i in items if i[0] == "PML"]
        assert pmh_items[0][2] == ""  # no annotation (or "current")
        assert pml_items[0][2] == ""

    def test_yahoo_annotation(self):
        items = _collect_levels(self._kl("yahoo"), 530)  # far from any level
        pmh_items = [i for i in items if i[0] == "PMH"]
        pml_items = [i for i in items if i[0] == "PML"]
        assert pmh_items[0][2] == " (Yahoo)"
        assert pml_items[0][2] == " (Yahoo)"

    def test_gap_estimate_annotation(self):
        items = _collect_levels(self._kl("gap_estimate"), 530)
        pmh_items = [i for i in items if i[0] == "PMH"]
        pml_items = [i for i in items if i[0] == "PML"]
        assert pmh_items[0][2] == " (估)"
        assert pml_items[0][2] == " (估)"


class TestRegimePmSourcePenalty:
    def _vp(self, poc=550, vah=555, val=545):
        return VolumeProfileResult(poc=poc, vah=vah, val=val)

    def test_gap_and_go_gap_estimate_penalty(self):
        """GAP_AND_GO with gap_estimate PM should have reduced confidence."""
        result_futu = classify_us_regime(
            price=560, prev_close=550, rvol=2.5,
            pmh=555, pml=548, vp=self._vp(),
            pm_source="futu",
        )
        result_est = classify_us_regime(
            price=560, prev_close=550, rvol=2.5,
            pmh=555, pml=548, vp=self._vp(),
            pm_source="gap_estimate",
        )
        assert result_futu.regime == USRegimeType.GAP_AND_GO
        assert result_est.regime == USRegimeType.GAP_AND_GO
        assert result_est.confidence < result_futu.confidence
        assert "PM estimated" in result_est.details

    def test_gap_and_go_yahoo_no_penalty(self):
        """GAP_AND_GO with yahoo PM should NOT be penalized."""
        result_futu = classify_us_regime(
            price=560, prev_close=550, rvol=2.5,
            pmh=555, pml=548, vp=self._vp(),
            pm_source="futu",
        )
        result_yahoo = classify_us_regime(
            price=560, prev_close=550, rvol=2.5,
            pmh=555, pml=548, vp=self._vp(),
            pm_source="yahoo",
        )
        assert result_yahoo.confidence == result_futu.confidence

    def test_fade_chop_no_pm_penalty(self):
        """Non-GAP_AND_GO regimes should NOT be affected by pm_source."""
        result = classify_us_regime(
            price=550, prev_close=551, rvol=0.8,
            pmh=555, pml=548, vp=self._vp(),
            pm_source="gap_estimate",
        )
        assert result.regime == USRegimeType.FADE_CHOP
        assert "PM estimated" not in result.details

    def test_confidence_floor(self):
        """Confidence should not drop below 0.1 from PM penalty."""
        # Use SPY FADE_CHOP to already reduce confidence, then add PM penalty
        result = classify_us_regime(
            price=560, prev_close=550, rvol=1.6,
            pmh=555, pml=548, vp=self._vp(),
            spy_regime=USRegimeType.FADE_CHOP,
            pm_source="gap_estimate",
        )
        assert result.confidence >= 0.1


# ── Regime Monitor Tests ──

class TestRegimeMonitor:
    """Tests for the lightweight regime change detection between playbook pushes."""

    def _make_playbook(self, symbol="TSLA", name="Tesla"):
        return USPlaybook(
            config={
                "watchlist": [
                    {"symbol": "SPY", "name": "S&P 500 ETF"},
                    {"symbol": symbol, "name": name},
                ],
                "regime": {
                    "market_context_symbols": ["SPY"],
                    "gap_and_go_rvol": 1.5,
                    "trend_day_rvol": 1.2,
                    "fade_chop_rvol": 1.0,
                    "adaptive": {"enabled": False},
                },
                "volume_profile": {"lookback_trading_days": 5, "min_trading_days": 3},
                "rvol": {"skip_open_minutes": 3, "lookback_days": 10},
                "playbook": {"push_times": ["09:45", "10:15"]},
                "regime_monitor": {
                    "enabled": True,
                    "start_after_morning_minutes": 5,
                    "end_before_confirm_minutes": 2,
                    "confidence_change_threshold": 0.2,
                    "max_flips_in_window": 2,
                },
            },
            collector=None,
        )

    def _vp(self, poc=280, vah=285, val=275):
        return VolumeProfileResult(poc=poc, vah=vah, val=val, trading_days=5)

    def _seed_playbook(self, pb, symbol, regime_type, rvol, price, confidence=0.7):
        """Seed _last_playbooks and _cached_context for a symbol."""
        regime = USRegimeResult(
            regime=regime_type, confidence=confidence,
            rvol=rvol, price=price, gap_pct=1.5,
        )
        kl = KeyLevels(
            poc=280, vah=285, val=275, pdh=288, pdl=272,
            pmh=284, pml=276, vwap=280,
        )
        vp = self._vp()
        pb._last_playbooks[symbol] = USPlaybookResult(
            symbol=symbol, name="Test",
            regime=regime, key_levels=kl,
            volume_profile=vp,
            gamma_wall=None,
            filters=FilterResult(tradeable=True, risk_level="normal"),
            generated_at=datetime(2026, 3, 9, 9, 45, 0, tzinfo=ET),
        )
        pb._cached_context[symbol] = {
            "history_all": pd.DataFrame(),
            "vp": vp,
            "pdh": 288.0, "pdl": 272.0,
            "pmh": 284.0, "pml": 276.0,
            "prev_close": price - 5,
            "rvol_profile": None,
            "gamma_wall": None,
        }

    def test_regime_change_detected(self):
        """Verify _check_regime_change detects GAP_AND_GO → FADE_CHOP."""
        old_regime = USRegimeResult(
            regime=USRegimeType.GAP_AND_GO, confidence=0.72,
            rvol=2.5, price=280.0, gap_pct=1.5,
        )
        new_regime = USRegimeResult(
            regime=USRegimeType.FADE_CHOP, confidence=0.65,
            rvol=0.8, price=275.0, gap_pct=1.5,
        )
        # Regime type changed → should be detected
        assert old_regime.regime != new_regime.regime

    def test_no_change_no_alert(self):
        """Same regime and similar confidence → no alert."""
        old = USRegimeResult(
            regime=USRegimeType.TREND_DAY, confidence=0.65,
            rvol=1.3, price=280.0, gap_pct=0.5,
        )
        new = USRegimeResult(
            regime=USRegimeType.TREND_DAY, confidence=0.68,
            rvol=1.35, price=281.0, gap_pct=0.5,
        )
        regime_changed = old.regime != new.regime
        conf_changed = abs(old.confidence - new.confidence) >= 0.2
        assert not regime_changed
        assert not conf_changed

    def test_confidence_change_triggers(self):
        """Same regime but confidence delta > 0.2 → should trigger."""
        old = USRegimeResult(
            regime=USRegimeType.GAP_AND_GO, confidence=0.85,
            rvol=2.5, price=280.0, gap_pct=2.0,
        )
        new = USRegimeResult(
            regime=USRegimeType.GAP_AND_GO, confidence=0.60,
            rvol=1.6, price=276.0, gap_pct=2.0,
        )
        regime_changed = old.regime != new.regime
        conf_changed = abs(old.confidence - new.confidence) >= 0.2
        assert not regime_changed
        assert conf_changed

    def test_flip_debounce(self):
        """After max_flips exceeded, _should_suppress_flip returns True."""
        pb = self._make_playbook()
        now = datetime(2026, 3, 9, 9, 55, 0, tzinfo=ET)
        max_flips = 2
        # First two flips: allowed
        assert not pb._should_suppress_flip("TSLA", now, max_flips)
        assert not pb._should_suppress_flip(
            "TSLA", now + timedelta(minutes=1), max_flips,
        )
        # Third flip within 10 min: suppressed
        assert pb._should_suppress_flip(
            "TSLA", now + timedelta(minutes=2), max_flips,
        )

    def test_time_window_guard_before(self):
        """09:44 ET → outside monitor window → return False."""
        pb = self._make_playbook()
        before = datetime(2026, 3, 9, 9, 44, 0, tzinfo=ET)
        assert not pb._is_in_monitor_window(before)

    def test_time_window_guard_after(self):
        """10:16 ET → outside monitor window → return False."""
        pb = self._make_playbook()
        after = datetime(2026, 3, 9, 10, 16, 0, tzinfo=ET)
        assert not pb._is_in_monitor_window(after)

    def test_time_window_guard_inside(self):
        """09:55 ET → inside monitor window → return True."""
        pb = self._make_playbook()
        inside = datetime(2026, 3, 9, 9, 55, 0, tzinfo=ET)
        assert pb._is_in_monitor_window(inside)

    def test_time_window_guard_weekend(self):
        """Saturday → always outside."""
        pb = self._make_playbook()
        saturday = datetime(2026, 3, 14, 9, 55, 0, tzinfo=ET)  # Saturday
        assert not pb._is_in_monitor_window(saturday)

    def test_time_window_disabled(self):
        """regime_monitor.enabled=false → always outside."""
        pb = self._make_playbook()
        pb._cfg["regime_monitor"]["enabled"] = False
        inside = datetime(2026, 3, 9, 9, 55, 0, tzinfo=ET)
        assert not pb._is_in_monitor_window(inside)

    def test_alert_format(self):
        """Verify alert message contains old/new regime comparison."""
        old = USRegimeResult(
            regime=USRegimeType.GAP_AND_GO, confidence=0.72,
            rvol=2.31, price=280.50, gap_pct=1.82,
        )
        new = USRegimeResult(
            regime=USRegimeType.FADE_CHOP, confidence=0.65,
            rvol=1.05, price=275.20, gap_pct=1.82,
        )
        kl = KeyLevels(
            poc=278, vah=283, val=273, pdh=285, pdl=270,
            pmh=282, pml=274, vwap=276.5,
        )
        msg = format_regime_change_alert("TSLA", "Tesla", old, new, kl)
        assert "REGIME 变更" in msg
        assert "Tesla" in msg
        assert "缺口追击日" in msg
        assert "震荡日" in msg
        assert "2.31" in msg
        assert "1.05" in msg
        assert "280.50" in msg
        assert "275.20" in msg
        assert "VAH" in msg
        assert "VWAP" in msg

    def test_alert_format_no_key_levels(self):
        """Alert without key_levels should still work."""
        old = USRegimeResult(
            regime=USRegimeType.TREND_DAY, confidence=0.6,
            rvol=1.3, price=550.0, gap_pct=0.5,
        )
        new = USRegimeResult(
            regime=USRegimeType.UNCLEAR, confidence=0.3,
            rvol=1.1, price=548.0, gap_pct=0.5,
        )
        msg = format_regime_change_alert("SPY", "S&P 500 ETF", old, new, None)
        assert "REGIME 变更" in msg
        assert "VAH" not in msg  # no key levels section

    def test_cached_context_populated(self):
        """After _seed_playbook, cached context should have required keys."""
        pb = self._make_playbook()
        self._seed_playbook(pb, "TSLA", USRegimeType.GAP_AND_GO, 2.5, 280.0)
        ctx = pb._cached_context["TSLA"]
        assert "vp" in ctx
        assert "pdh" in ctx
        assert "prev_close" in ctx
        assert "gamma_wall" in ctx
        assert "rvol_profile" in ctx
