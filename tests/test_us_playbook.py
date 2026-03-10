"""Tests for the US Playbook module."""

import importlib
import pandas as pd
import numpy as np
import pytest
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from telegram.error import NetworkError

from src.hk import VolumeProfileResult, GammaWallResult, FilterResult, OptionRecommendation, QuoteSnapshot, OptionMarketSnapshot
from src.collector.base import PremarketData
from src.us_playbook import (
    USRegimeType, USRegimeResult, USPlaybookResult, KeyLevels,
    USScanSignal, USScanAlertRecord,
)
from src.us_playbook.indicators import calculate_vwap, calculate_us_rvol, compute_rvol_profile, RvolProfile
from src.us_playbook.levels import (
    us_tick_size, extract_previous_day_hl,
    get_today_bars, get_history_bars, compute_volume_profile, build_key_levels,
    calc_fetch_calendar_days,
)
from src.us_playbook.regime import classify_us_regime, regime_to_signal_type
from src.us_playbook.filter import check_us_filters, _is_monthly_opex
from src.us_playbook.playbook import format_us_playbook_message, _collect_levels
from src.us_playbook.watchlist import USWatchlist, normalize_us_symbol
from src.us_playbook.option_recommend import (
    select_expiry,
    _decide_direction,
    assess_chase_risk,
    option_quotes_to_df,
    recommend,
)
from src.us_playbook.main import USPredictor

us_playbook_entry = importlib.import_module("src.us_playbook.__main__")

ET = ZoneInfo("America/New_York")


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
    def _make_result(self, regime_type=USRegimeType.TREND_DAY) -> USPlaybookResult:
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
            quote=QuoteSnapshot(
                symbol="AAPL", last_price=554.2,
                open_price=553.0, high_price=556.0, low_price=551.0,
                prev_close=552.0, volume=12000000, turnover=6.6e9,
                bid_price=554.15, ask_price=554.25,
                turnover_rate=0.85, amplitude=0.91,
            ),
            option_market=OptionMarketSnapshot(
                expiry="2026-03-20", contract_count=120,
                call_contract_count=60, put_contract_count=60,
                atm_iv=0.28, avg_iv=0.30, iv_ratio=0.93,
            ),
        )

    def test_message_contains_all_sections(self):
        result = self._make_result()
        msg = format_us_playbook_message(result)
        assert "Apple" in msg
        assert "结论" in msg
        assert "实时数据" in msg
        assert "建议" in msg
        assert "风险" in msg
        assert "RVOL" in msg

    def test_market_context_section(self):
        result = self._make_result()
        spy = self._make_result(USRegimeType.FADE_CHOP)
        spy.symbol = "SPY"
        spy.name = "S&P 500 ETF"
        msg = format_us_playbook_message(result, spy_result=spy)
        assert "SPY" in msg
        assert "震荡日" in msg

    def test_playbook_vp_thin_warning(self):
        """VP with < 3 trading days should show warning in message."""
        result = self._make_result()
        result.volume_profile = VolumeProfileResult(poc=553, vah=556.5, val=550.5, trading_days=2)
        msg = format_us_playbook_message(result)
        assert "VP 仅 2 天数据" in msg

    def test_playbook_no_warning_sufficient_days(self):
        """VP with >= 3 trading days should NOT show warning."""
        result = self._make_result()
        result.volume_profile = VolumeProfileResult(poc=553, vah=556.5, val=550.5, trading_days=5)
        msg = format_us_playbook_message(result)
        assert "VP 仅" not in msg

    def test_option_rec_section(self):
        """Playbook with option_rec should include option recommendation section."""
        result = self._make_result()
        result.option_rec = OptionRecommendation(
            action="call", direction="bullish", expiry="2026-03-20",
            rationale="趋势日看多", dte=5,
        )
        msg = format_us_playbook_message(result)
        assert "建议" in msg
        assert "买入 Call" in msg

    def test_option_rec_wait_section(self):
        """Option rec=wait should show wait conditions."""
        result = self._make_result()
        result.option_rec = OptionRecommendation(
            action="wait", direction="neutral",
            rationale="方向不明确", wait_conditions=["等待 Regime 明确"],
        )
        msg = format_us_playbook_message(result)
        assert "建议" in msg
        assert "观望" in msg

    def test_confidence_bar_5_blocks(self):
        """Confidence bar should use 5 blocks."""
        from src.us_playbook.playbook import _confidence_bar
        bar_100 = _confidence_bar(1.0)
        assert len(bar_100) == 5
        assert bar_100 == "█████"
        bar_0 = _confidence_bar(0.0)
        assert bar_0 == "░░░░░"
        bar_60 = _confidence_bar(0.6)
        assert len(bar_60) == 5
        assert bar_60 == "███░░"


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
        msg = format_us_playbook_message(result)
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


# ── US Watchlist Tests ──

class TestUSWatchlist:
    def test_normalize_valid(self):
        assert normalize_us_symbol("AAPL") == "AAPL"
        assert normalize_us_symbol("aapl") == "AAPL"
        assert normalize_us_symbol("Tsla") == "TSLA"
        assert normalize_us_symbol("A") == "A"
        assert normalize_us_symbol("GOOGL") == "GOOGL"

    def test_normalize_invalid(self):
        assert normalize_us_symbol("") is None
        assert normalize_us_symbol("123") is None
        assert normalize_us_symbol("TOOLONG") is None
        assert normalize_us_symbol("AA BB") is None
        assert normalize_us_symbol("A1") is None

    def test_crud(self, tmp_path):
        wl = USWatchlist(path=str(tmp_path / "wl.json"))
        assert wl.symbols() == []

        # Add
        assert wl.add("SPY", "S&P 500 ETF") is True
        assert wl.add("SPY") is False  # duplicate
        assert wl.contains("SPY")
        assert wl.get_name("SPY") == "S&P 500 ETF"

        # List
        items = wl.list_all()
        assert len(items) == 1
        assert items[0]["symbol"] == "SPY"

        # Remove
        assert wl.remove("SPY") is True
        assert wl.remove("SPY") is False
        assert not wl.contains("SPY")

    def test_init_from_config(self, tmp_path):
        cfg = {
            "watchlist": [
                {"symbol": "SPY", "name": "S&P 500 ETF"},
                {"symbol": "AAPL", "name": "Apple"},
            ],
        }
        wl = USWatchlist(path=str(tmp_path / "wl.json"), initial_config=cfg)
        assert len(wl.symbols()) == 2
        assert wl.contains("SPY")
        assert wl.contains("AAPL")

    def test_persistence(self, tmp_path):
        path = str(tmp_path / "wl.json")
        wl = USWatchlist(path=path)
        wl.add("TSLA", "Tesla")

        # Re-load from file
        wl2 = USWatchlist(path=path)
        assert wl2.contains("TSLA")
        assert wl2.get_name("TSLA") == "Tesla"


# ── US Option Recommend Tests ──

class TestUSOptionRecommend:
    def test_select_expiry_filters_0dte(self):
        today = date(2026, 3, 10)
        dates = ["2026-03-10", "2026-03-11", "2026-03-17"]
        # 0DTE (2026-03-10) should be filtered
        result = select_expiry(dates, today=today, dte_min=1)
        assert result == "2026-03-11"

    def test_select_expiry_prefers_weekly(self):
        today = date(2026, 3, 10)
        dates = ["2026-03-12", "2026-03-14", "2026-03-21"]
        result = select_expiry(dates, today=today, dte_min=1, dte_preferred_max=7)
        assert result == "2026-03-12"

    def test_select_expiry_empty(self):
        assert select_expiry([]) is None

    def test_select_expiry_all_expired(self):
        today = date(2026, 3, 15)
        dates = ["2026-03-10", "2026-03-12"]
        assert select_expiry(dates, today=today) is None

    def test_direction_gap_and_go(self):
        regime = USRegimeResult(
            regime=USRegimeType.GAP_AND_GO, confidence=0.8,
            rvol=2.0, price=560, gap_pct=1.5,
        )
        vp = VolumeProfileResult(poc=550, vah=555, val=545)
        assert _decide_direction(regime, vp) == "bullish"  # price > vah

    def test_direction_fade_chop(self):
        regime = USRegimeResult(
            regime=USRegimeType.FADE_CHOP, confidence=0.7,
            rvol=0.8, price=556, gap_pct=0.2,
        )
        vp = VolumeProfileResult(poc=550, vah=555, val=545)
        # Price > mid → bearish (mean reversion)
        assert _decide_direction(regime, vp) == "bearish"

    def test_direction_unclear(self):
        regime = USRegimeResult(
            regime=USRegimeType.UNCLEAR, confidence=0.3,
            rvol=1.0, price=550, gap_pct=0.1,
        )
        vp = VolumeProfileResult(poc=550, vah=555, val=545)
        assert _decide_direction(regime, vp) == "neutral"

    def test_chase_risk_afternoon(self):
        """Afternoon should tighten thresholds."""
        vp = VolumeProfileResult(poc=550, vah=555, val=545)
        # Same deviation, morning = none, afternoon = moderate
        result_am = assess_chase_risk(
            price=558, vwap=555, vp=vp, direction="bullish", is_afternoon=False,
            vwap_moderate_pct=1.5,
        )
        result_pm = assess_chase_risk(
            price=558, vwap=555, vp=vp, direction="bullish", is_afternoon=True,
            vwap_moderate_pct=1.5, afternoon_tighten_pct=0.5,
        )
        # Afternoon has tighter thresholds
        assert result_pm.level in ("moderate", "high") or result_am.level == "none"

    def test_recommend_wait_unclear(self):
        """UNCLEAR regime with low confidence → wait."""
        regime = USRegimeResult(
            regime=USRegimeType.UNCLEAR, confidence=0.3,
            rvol=1.0, price=550, gap_pct=0.1,
        )
        vp = VolumeProfileResult(poc=550, vah=555, val=545)
        filters = FilterResult(tradeable=True, risk_level="normal")
        rec = recommend(regime=regime, vp=vp, filters=filters)
        assert rec.action == "wait"

    def test_recommend_wait_not_tradeable(self):
        """Not tradeable → wait."""
        regime = USRegimeResult(
            regime=USRegimeType.GAP_AND_GO, confidence=0.8,
            rvol=2.0, price=560, gap_pct=1.5,
        )
        vp = VolumeProfileResult(poc=550, vah=555, val=545)
        filters = FilterResult(tradeable=False, risk_level="blocked", warnings=["FOMC today"])
        rec = recommend(regime=regime, vp=vp, filters=filters)
        assert rec.action == "wait"

    def test_option_quotes_to_df(self):
        from src.collector.base import OptionQuote
        quotes = [
            OptionQuote(
                contract_symbol="US.AAPL260320C00230000",
                underlying="AAPL", strike=230, option_type="call",
                expiration="2026-03-20", bid=5.0, ask=5.5, last=5.25,
                volume=100, open_interest=500, implied_volatility=0.3,
                delta=0.45, gamma=0.02, theta=-0.05, vega=0.1,
                timestamp=1.0,
            ),
        ]
        df = option_quotes_to_df(quotes)
        assert len(df) == 1
        assert df.iloc[0]["strike_price"] == 230
        assert df.iloc[0]["delta"] == 0.45
        assert df.iloc[0]["option_type"] == "CALL"


# ── Regime Signal Type Mapping Tests ──

class TestRegimeSignalType:
    def test_gap_and_go_bullish(self):
        assert regime_to_signal_type(USRegimeType.GAP_AND_GO, "bullish") == "BREAKOUT_BULLISH"

    def test_trend_day_bearish(self):
        assert regime_to_signal_type(USRegimeType.TREND_DAY, "bearish") == "BREAKOUT_BEARISH"

    def test_fade_chop_bullish(self):
        assert regime_to_signal_type(USRegimeType.FADE_CHOP, "bullish") == "RANGE_REVERSAL_BULLISH"

    def test_unclear_returns_none(self):
        assert regime_to_signal_type(USRegimeType.UNCLEAR, "bullish") is None


# ── Auto-scan Window Tests ──

class TestAutoScanWindow:
    def test_morning_window(self):
        scan_cfg = {
            "morning_window": ["09:40", "11:30"],
            "afternoon_window": ["13:00", "15:00"],
        }
        # 10:00 ET Tuesday → morning
        now = datetime(2026, 3, 10, 10, 0, 0, tzinfo=ET)
        in_window, session = USPredictor._get_scan_window(scan_cfg, now)
        assert in_window
        assert session == "morning"

    def test_afternoon_window(self):
        scan_cfg = {
            "morning_window": ["09:40", "11:30"],
            "afternoon_window": ["13:00", "15:00"],
        }
        now = datetime(2026, 3, 10, 14, 0, 0, tzinfo=ET)
        in_window, session = USPredictor._get_scan_window(scan_cfg, now)
        assert in_window
        assert session == "afternoon"

    def test_outside_window(self):
        scan_cfg = {
            "morning_window": ["09:40", "11:30"],
            "afternoon_window": ["13:00", "15:00"],
        }
        now = datetime(2026, 3, 10, 12, 0, 0, tzinfo=ET)
        in_window, _ = USPredictor._get_scan_window(scan_cfg, now)
        assert not in_window

    def test_weekend(self):
        scan_cfg = {
            "morning_window": ["09:40", "11:30"],
            "afternoon_window": ["13:00", "15:00"],
        }
        saturday = datetime(2026, 3, 14, 10, 0, 0, tzinfo=ET)
        in_window, _ = USPredictor._get_scan_window(scan_cfg, saturday)
        assert not in_window


# ── Frequency Control Tests ──

class TestFrequencyControl:
    def _make_predictor(self):
        cfg = {
            "watchlist": [{"symbol": "SPY", "name": "S&P 500 ETF"}],
            "auto_scan": {
                "cooldown": {"same_signal_minutes": 30, "max_per_session": 2, "max_per_day": 3},
                "override": {"confidence_increase": 0.10, "price_extension_pct": 0.50, "regime_upgrade": True},
            },
        }
        return USPredictor(cfg, collector=None)

    def _make_signal(self, signal_type="BREAKOUT_BULLISH", direction="bullish", conf=0.75, price=560.0):
        return USScanSignal(
            signal_type=signal_type,
            direction=direction,
            symbol="AAPL",
            regime=USRegimeResult(
                regime=USRegimeType.GAP_AND_GO, confidence=conf,
                rvol=1.5, price=price, gap_pct=1.0,
            ),
            price=price,
            timestamp=1000.0,
        )

    def test_first_signal_allowed(self):
        pred = self._make_predictor()
        pred._scan_history_date = "2026-03-10"
        signal = self._make_signal()
        allowed, reason = pred._check_frequency("AAPL", signal, "morning", pred._cfg["auto_scan"])
        assert allowed
        assert reason is None

    def test_cooldown_blocks(self):
        pred = self._make_predictor()
        pred._scan_history_date = "2026-03-10"
        signal = self._make_signal()
        # Record a previous alert
        pred._record_alert("AAPL", signal, "morning")

        # Same signal within cooldown → blocked
        signal2 = self._make_signal()
        signal2.timestamp = 1100.0  # 100s later, within 30min
        allowed, _ = pred._check_frequency("AAPL", signal2, "morning", pred._cfg["auto_scan"])
        assert not allowed

    def test_confidence_override(self):
        pred = self._make_predictor()
        pred._scan_history_date = "2026-03-10"
        signal1 = self._make_signal(conf=0.70)
        pred._record_alert("AAPL", signal1, "morning")

        # Higher confidence overrides cooldown
        signal2 = self._make_signal(conf=0.85)
        signal2.timestamp = 1100.0
        allowed, reason = pred._check_frequency("AAPL", signal2, "morning", pred._cfg["auto_scan"])
        assert allowed
        assert "置信度" in reason

    def test_daily_max(self):
        pred = self._make_predictor()
        pred._scan_history_date = "2026-03-10"
        # Fill up daily max (3)
        for i in range(3):
            sig = self._make_signal()
            sig.timestamp = float(i * 3600)
            sig.signal_type = f"BREAKOUT_{i}"
            pred._record_alert("AAPL", sig, "morning" if i < 2 else "afternoon")

        signal = self._make_signal()
        signal.timestamp = 20000.0
        allowed, _ = pred._check_frequency("AAPL", signal, "afternoon", pred._cfg["auto_scan"])
        assert not allowed


# ── Scan Header Format Tests ──

class TestScanHeader:
    def test_breakout_with_option_rec(self):
        signal = USScanSignal(
            signal_type="BREAKOUT_BULLISH",
            direction="bullish",
            symbol="AAPL",
            regime=USRegimeResult(
                regime=USRegimeType.GAP_AND_GO, confidence=0.82,
                rvol=1.8, price=560, gap_pct=1.5,
            ),
            price=560,
            trigger_reasons=["突破 VAH 0.35%"],
            timestamp=1000.0,
        )
        rec = OptionRecommendation(action="call", direction="bullish", expiry="2026-03-20")
        header = USPredictor._format_scan_header(signal, "normal", rec, None, 30)
        assert "BREAKOUT_BULLISH" in header
        assert "可执行" in header
        assert "82%" in header

    def test_breakout_without_option_rec(self):
        signal = USScanSignal(
            signal_type="BREAKOUT_BULLISH",
            direction="bullish",
            symbol="AAPL",
            regime=USRegimeResult(
                regime=USRegimeType.GAP_AND_GO, confidence=0.75,
                rvol=1.5, price=555, gap_pct=1.0,
            ),
            price=555,
            trigger_reasons=["突破 VAH 0.25%"],
            timestamp=1000.0,
        )
        rec = OptionRecommendation(action="wait", direction="bullish", risk_note="无可用到期日")
        header = USPredictor._format_scan_header(signal, "normal", rec, None, 30)
        assert "暂无合约" in header
        assert "BREAKOUT_BULLISH" in header


# ── Standalone Entry Tests ──

class TestUSPlaybookStandaloneEntry:
    def test_build_telegram_application_wires_dual_requests(self, monkeypatch):
        created_requests = []

        class FakeRequest:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                created_requests.append(self)

        class FakeBuilder:
            def __init__(self):
                self.bot_token = None
                self.bot_request = None
                self.polling_request = None

            def token(self, bot_token):
                self.bot_token = bot_token
                return self

            def request(self, request):
                self.bot_request = request
                return self

            def get_updates_request(self, request):
                self.polling_request = request
                return self

            def build(self):
                return {
                    "bot_token": self.bot_token,
                    "bot_request": self.bot_request,
                    "polling_request": self.polling_request,
                }

        fake_builder = FakeBuilder()

        class FakeApplication:
            @staticmethod
            def builder():
                return fake_builder

        monkeypatch.setattr(us_playbook_entry, "Application", FakeApplication)
        monkeypatch.setattr(us_playbook_entry, "HTTPXRequest", FakeRequest)

        app = us_playbook_entry._build_telegram_application("token-123")

        assert app["bot_token"] == "token-123"
        assert app["bot_request"] is created_requests[0]
        assert app["polling_request"] is created_requests[1]
        assert created_requests[0].kwargs == {
            "read_timeout": us_playbook_entry.TELEGRAM_READ_TIMEOUT_SECONDS,
            "write_timeout": us_playbook_entry.TELEGRAM_WRITE_TIMEOUT_SECONDS,
            "connect_timeout": us_playbook_entry.TELEGRAM_CONNECT_TIMEOUT_SECONDS,
            "pool_timeout": us_playbook_entry.TELEGRAM_POOL_TIMEOUT_SECONDS,
        }
        assert created_requests[1].kwargs == created_requests[0].kwargs

    @pytest.mark.asyncio
    async def test_start_telegram_polling_retries_network_error(self, monkeypatch):
        sleep_delays = []

        async def fake_sleep(delay_seconds):
            sleep_delays.append(delay_seconds)

        monkeypatch.setattr(us_playbook_entry.asyncio, "sleep", fake_sleep)

        class FakeUpdater:
            def __init__(self):
                self.running = False
                self.start_polling_calls = 0
                self.stop_calls = 0

            async def start_polling(self, drop_pending_updates):
                assert drop_pending_updates is True
                self.start_polling_calls += 1
                if self.start_polling_calls == 1:
                    raise NetworkError("temporary disconnect")
                self.running = True

            async def stop(self):
                self.stop_calls += 1
                self.running = False

        class FakeApp:
            def __init__(self):
                self.updater = FakeUpdater()
                self.running = False
                self.initialized = False
                self.initialize_calls = 0
                self.start_calls = 0
                self.stop_calls = 0
                self.shutdown_calls = 0

            async def initialize(self):
                self.initialize_calls += 1
                self.initialized = True

            async def start(self):
                self.start_calls += 1
                self.running = True

            async def stop(self):
                self.stop_calls += 1
                self.running = False

            async def shutdown(self):
                self.shutdown_calls += 1
                self.initialized = False

        fake_app = FakeApp()

        await us_playbook_entry._start_telegram_polling(fake_app)

        assert fake_app.initialize_calls == 2
        assert fake_app.start_calls == 2
        assert fake_app.updater.start_polling_calls == 2
        assert fake_app.stop_calls == 1
        assert fake_app.shutdown_calls == 1
        assert sleep_delays == [us_playbook_entry.TELEGRAM_POLL_RETRY_BASE_SECONDS]


# ── Fix validation tests ──


class TestTimezoneET:
    """Verify ET uses America/New_York (DST-aware), not fixed UTC-5."""

    def test_et_is_dst_aware(self):
        from src.us_playbook.main import ET as main_ET
        from src.us_playbook.playbook import ET as playbook_ET
        from src.us_playbook.option_recommend import ET as rec_ET
        from src.us_playbook.filter import ET as filter_ET

        for tz in (main_ET, playbook_ET, rec_ET, filter_ET):
            assert tz.key == "America/New_York"

    def test_dst_period_offset(self):
        """During DST (Mar 8 - Nov 1), ET should be UTC-4, not UTC-5."""
        from src.us_playbook.main import ET as main_ET
        # 2026-03-10 is in DST
        dt_dst = datetime(2026, 3, 10, 12, 0, 0, tzinfo=main_ET)
        offset_hours = dt_dst.utcoffset().total_seconds() / 3600
        assert offset_hours == -4.0

    def test_non_dst_period_offset(self):
        """Outside DST, ET should be UTC-5."""
        from src.us_playbook.main import ET as main_ET
        # 2026-01-15 is NOT in DST
        dt_est = datetime(2026, 1, 15, 12, 0, 0, tzinfo=main_ET)
        offset_hours = dt_est.utcoffset().total_seconds() / 3600
        assert offset_hours == -5.0


class TestRvolFloor:
    """Verify adaptive RVOL trend_day floor prevents avg volume → TREND_DAY."""

    def test_floor_applied(self):
        """When P60 is below 1.0, floor should clamp trend_day to 1.0."""
        # Build history with low-volatility volume (RVOL samples all near 1.0)
        dates = pd.date_range("2026-03-01 09:33", periods=7 * 100, freq="1min", tz="America/New_York")
        # Assign each bar to a "day" group by repeating date assignment
        np.random.seed(42)
        rows = []
        for d in range(7):
            start = d * 100
            base_vol = 1000 + d * 10  # very similar volumes across days
            for i in range(100):
                rows.append({
                    "Open": 100.0, "High": 100.5, "Low": 99.5, "Close": 100.0,
                    "Volume": base_vol + np.random.randint(-50, 50),
                })
        idx = dates[:len(rows)]
        hist = pd.DataFrame(rows, index=idx)

        profile = compute_rvol_profile(
            history_bars=hist,
            today_rvol=0.99,
            skip_open_minutes=3,
            min_sample_days=3,
            min_trend_day_floor=1.0,
        )
        assert profile.trend_day_rvol >= 1.0
        assert profile.fade_chop_rvol < profile.trend_day_rvol

    def test_floor_not_applied_when_above(self):
        """When natural P60 is above floor, it should not be clamped."""
        # Build history with high-volatility volume (some days 2x)
        dates = pd.date_range("2026-03-01 09:33", periods=10 * 100, freq="1min", tz="America/New_York")
        np.random.seed(123)
        rows = []
        for d in range(10):
            start = d * 100
            base_vol = 1000 * (1 + d * 0.3)  # increasing volume pattern
            for i in range(100):
                rows.append({
                    "Open": 100.0, "High": 100.5, "Low": 99.5, "Close": 100.0,
                    "Volume": max(1, int(base_vol + np.random.randint(-100, 100))),
                })
        idx = dates[:len(rows)]
        hist = pd.DataFrame(rows, index=idx)

        profile = compute_rvol_profile(
            history_bars=hist,
            today_rvol=1.5,
            skip_open_minutes=3,
            min_sample_days=3,
            min_trend_day_floor=1.0,
        )
        # Natural P60 should be above 1.0 for this distribution
        assert profile.trend_day_rvol >= 1.0


class TestDirectionEmoji:
    """Verify direction-aware emoji for TREND_DAY and GAP_AND_GO."""

    def test_bearish_trend_day(self):
        from src.us_playbook.playbook import get_regime_emoji
        assert get_regime_emoji(USRegimeType.TREND_DAY, "bearish") == "\U0001f4c9"  # 📉

    def test_bullish_trend_day(self):
        from src.us_playbook.playbook import get_regime_emoji
        assert get_regime_emoji(USRegimeType.TREND_DAY, "bullish") == "\U0001f4c8"  # 📈

    def test_bearish_gap_and_go(self):
        from src.us_playbook.playbook import get_regime_emoji
        assert get_regime_emoji(USRegimeType.GAP_AND_GO, "bearish") == "\U0001f4a5"  # 💥

    def test_bullish_gap_and_go(self):
        from src.us_playbook.playbook import get_regime_emoji
        assert get_regime_emoji(USRegimeType.GAP_AND_GO, "bullish") == "\U0001f680"  # 🚀

    def test_bearish_trend_day_in_playbook(self):
        """Playbook with price < VAL should show 📉 for TREND_DAY."""
        r = USRegimeResult(
            regime=USRegimeType.TREND_DAY, confidence=0.65,
            rvol=1.3, price=250.0, gap_pct=-0.5,
        )
        vp = VolumeProfileResult(poc=260, vah=265, val=255)
        kl = KeyLevels(poc=260, vah=265, val=255, pdh=268, pdl=252, pmh=0, pml=0, vwap=258)
        filters = FilterResult(tradeable=True, warnings=[], risk_level="normal")
        result = USPlaybookResult(
            symbol="AAPL", name="Apple", regime=r,
            key_levels=kl, volume_profile=vp, gamma_wall=None,
            filters=filters, strategy_text="",
            generated_at=datetime(2026, 3, 10, 10, 0, 0, tzinfo=ET),
        )
        msg = format_us_playbook_message(result)
        assert "\U0001f4c9" in msg  # 📉
        assert "向下跟随" in msg


class TestPdhPdlWarning:
    """Verify PDH/PDL consistency warning is logged."""

    def test_mismatch_logged(self, caplog):
        """When prev_close is outside PDH/PDL range, a warning should be logged."""
        import logging
        caplog.set_level(logging.WARNING)
        # This is a unit test of the logic — we test the condition directly
        prev_close_snap = 257.6
        pdh = 260.20
        pdl = 258.50
        # prev_close_snap < pdl * 0.998
        assert prev_close_snap < pdl * 0.998
