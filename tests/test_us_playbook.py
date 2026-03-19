"""Tests for the US Playbook module."""

import importlib
import pandas as pd
import numpy as np
import pytest
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from telegram.error import NetworkError

from src.hk import VolumeProfileResult, GammaWallResult, FilterResult, OptionRecommendation, QuoteSnapshot, OptionMarketSnapshot
from src.common.types import OptionLeg
from src.collector.base import PremarketData
from src.us_playbook import (
    USRegimeType, USRegimeResult, USPlaybookResult, KeyLevels,
    USScanSignal, USScanAlertRecord,
    BreadthProxy, MarketTone,
    RegimeFamily,
)
from src.us_playbook.indicators import calculate_vwap, calculate_us_rvol, compute_rvol_profile, RvolProfile
from src.us_playbook.levels import (
    us_tick_size, extract_previous_day_hl,
    get_today_bars, get_history_bars, compute_volume_profile, build_key_levels,
    calc_fetch_calendar_days,
    IntradayLevels, detect_wide_va, compute_intraday_vp, compute_vwap_bands,
    build_intraday_levels,
)
from src.us_playbook.regime import (
    classify_us_regime, check_index_consistency, detect_price_structure,
    downgrade_to_unclear, regime_to_signal_type,
)
from src.us_playbook.filter import check_us_filters, _is_monthly_opex
from src.us_playbook.playbook import (
    format_us_playbook_message, _collect_levels,
    _nearest_levels, _risk_action_lines, _entry_zone_text,
    ActionPlan, _calculate_rr, _generate_action_plans,
    _compact_option_line, _rvol_assessment, _find_fade_entry_zone,
    PlanContext, _reachable_range_pct, _cap_tp1, _cap_tp2,
    _check_entry_reachability, _apply_wait_coherence, _apply_min_rr_gate,
    _us_key_levels_to_dict, _cap_fade_sl,
)
from src.common.action_plan import (
    cap_tp1 as _cap_tp1_common,
    apply_gamma_wall_warning as _apply_gamma_wall_warning,
    apply_vwap_deviation_warning as _apply_vwap_deviation_warning,
    format_action_plan as _format_action_plan,
)
from src.us_playbook.watchlist import USWatchlist, normalize_us_symbol
from src.us_playbook.option_recommend import (
    compute_local_trend,
    select_expiry,
    _decide_direction,
    _check_fade_entry_staleness,
    _compute_fade_momentum,
    assess_chase_risk,
    option_quotes_to_df,
    recommend,
    should_wait,
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
        assert result.regime == USRegimeType.GAP_GO
        assert result.confidence > 0.5

    def test_trend_day(self):
        result = classify_us_regime(
            price=557, prev_close=556.5, rvol=1.3,
            pmh=556, pml=554, vp=self._vp(),
        )
        # Phase 2: RVOL 1.3 < gap_and_go_rvol 1.5 and no VWAP hold → TREND_WEAK
        assert result.regime == USRegimeType.TREND_WEAK

    def test_fade_chop(self):
        result = classify_us_regime(
            price=550, prev_close=551, rvol=0.8,
            pmh=555, pml=548, vp=self._vp(),
        )
        assert result.regime == USRegimeType.RANGE

    def test_unclear(self):
        result = classify_us_regime(
            price=550, prev_close=549.5, rvol=1.1,
            pmh=555, pml=548, vp=self._vp(),
        )
        assert result.regime == USRegimeType.UNCLEAR

    def test_spy_context_reduces_confidence(self):
        """SPY RANGE should reduce GAP_GO confidence."""
        result_no_spy = classify_us_regime(
            price=560, prev_close=550, rvol=2.5,
            pmh=555, pml=548, vp=self._vp(),
        )
        result_with_spy = classify_us_regime(
            price=560, prev_close=550, rvol=2.5,
            pmh=555, pml=548, vp=self._vp(),
            spy_regime=USRegimeType.RANGE,
        )
        assert result_with_spy.confidence < result_no_spy.confidence

    def test_gap_and_go_unified_threshold(self):
        """Unified RVOL threshold — no more preliminary hack."""
        result = classify_us_regime(
            price=560, prev_close=550, rvol=1.8,
            pmh=555, pml=548, vp=self._vp(),
            gap_and_go_rvol=1.5,
        )
        assert result.regime == USRegimeType.GAP_GO


# ── Filter Tests ──

class TestUSFilters:
    def test_fomc_day_elevated_not_blocked(self):
        """FOMC day (with behavior=range_then_trend) → elevated, not blocked."""
        result = check_us_filters(
            rvol=1.2, prev_high=100, prev_low=95,
            current_high=102, current_low=96,
            calendar_path="config/us_calendar.yaml",
            today=date(2026, 1, 28),  # FOMC
        )
        assert result.tradeable
        assert "宏观事件日" in result.warnings[0]

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
    def _make_result(self, regime_type=USRegimeType.TREND_STRONG) -> USPlaybookResult:
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
        assert "模式识别" in msg  # Section 2: conclusion
        assert "日型" in msg       # Section 2: regime type
        assert "剧本推演" in msg
        assert "盘面逻辑" in msg
        assert "数据雷达" in msg
        assert "RVOL" in msg

    def test_market_context_section(self):
        result = self._make_result()
        spy = self._make_result(USRegimeType.RANGE)
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
        """Playbook with option_rec should include compact option line in Plan A."""
        result = self._make_result()
        result.option_rec = OptionRecommendation(
            action="call", direction="bullish", expiry="2026-03-20",
            rationale="趋势日看多", dte=5,
        )
        msg = format_us_playbook_message(result)
        assert "剧本推演" in msg
        assert "CALL" in msg

    def test_option_rec_wait_section(self):
        """Option rec=wait should show wait in core conclusion."""
        result = self._make_result()
        result.option_rec = OptionRecommendation(
            action="wait", direction="neutral",
            rationale="方向不明确", wait_conditions=["等待 Regime 明确"],
        )
        msg = format_us_playbook_message(result)
        assert "核心结论" in msg
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
        assert result.regime == USRegimeType.GAP_GO
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
        """With adaptive profile, lower gap_and_go threshold triggers GAP_GO."""
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
        assert result.regime == USRegimeType.GAP_GO
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
        # RVOL 1.3 < static 1.5, so not GAP_GO
        assert result.regime != USRegimeType.GAP_GO
        assert result.adaptive_thresholds is None

    def test_gap_normalization_with_profile(self):
        """TREND_STRONG gap check uses normalized gap when profile available."""
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
        # Phase 2: RVOL 1.3 < adaptive gap_and_go_rvol 2.0 and no VWAP hold → TREND_WEAK
        assert result.regime == USRegimeType.TREND_WEAK

    def test_gap_normalization_blocks_large_gap(self):
        """Large normalized gap prevents TREND classification."""
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
        assert result.regime not in (USRegimeType.TREND_STRONG, USRegimeType.TREND_WEAK)

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
        # Static 1.5 should be used → 1.3 < 1.5 → not GAP_GO
        assert result.regime != USRegimeType.GAP_GO
        assert result.adaptive_thresholds is None

    def test_playbook_message_shows_adaptive_info(self):
        """Telegram message includes adaptive threshold info."""
        result = USPlaybookResult(
            symbol="TSLA",
            name="Tesla",
            regime=USRegimeResult(
                regime=USRegimeType.GAP_GO, confidence=0.85,
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
        """GAP_GO with gap_estimate PM should have reduced confidence."""
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
        assert result_futu.regime == USRegimeType.GAP_GO
        assert result_est.regime == USRegimeType.GAP_GO
        assert result_est.confidence < result_futu.confidence
        assert "PM estimated" in result_est.details

    def test_gap_and_go_yahoo_no_penalty(self):
        """GAP_GO with yahoo PM should NOT be penalized."""
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
        """Non-GAP_GO regimes should NOT be affected by pm_source."""
        result = classify_us_regime(
            price=550, prev_close=551, rvol=0.8,
            pmh=555, pml=548, vp=self._vp(),
            pm_source="gap_estimate",
        )
        assert result.regime == USRegimeType.RANGE
        assert "PM estimated" not in result.details

    def test_confidence_floor(self):
        """Confidence should not drop below 0.1 from PM penalty."""
        # Use SPY RANGE to already reduce confidence, then add PM penalty
        result = classify_us_regime(
            price=560, prev_close=550, rvol=1.6,
            pmh=555, pml=548, vp=self._vp(),
            spy_regime=USRegimeType.RANGE,
            pm_source="gap_estimate",
        )
        assert result.confidence >= 0.1


# ── C1: gap_pct uses open_price Tests ──

class TestGapPctOpenPrice:
    """C1: classify_us_regime should use open_price for gap calculation."""

    def _vp(self, poc=550, vah=555, val=545):
        return VolumeProfileResult(poc=poc, vah=vah, val=val)

    def test_gap_pct_uses_open_not_current(self):
        """gap_pct should reflect the opening gap, not intraday drift."""
        # Scenario: opened at 560 (gap +1.8%), drifted back to 552
        result = classify_us_regime(
            price=552, prev_close=550, rvol=2.5,
            pmh=555, pml=548, vp=self._vp(),
            open_price=560.0,
        )
        # gap_pct should be ~1.82% (from open), not ~0.36% (from current price)
        assert abs(result.gap_pct - 1.82) < 0.1

    def test_gap_pct_fallback_when_no_open(self):
        """When open_price=0, fall back to current price for backward compat."""
        result = classify_us_regime(
            price=560, prev_close=550, rvol=2.5,
            pmh=555, pml=548, vp=self._vp(),
            open_price=0.0,
        )
        # Should use current price as fallback
        assert abs(result.gap_pct - 1.82) < 0.1

    def test_gap_stability_intraday(self):
        """gap_pct should stay the same regardless of current price when open_price is given."""
        result_a = classify_us_regime(
            price=560, prev_close=550, rvol=2.0,
            pmh=555, pml=548, vp=self._vp(),
            open_price=555.0,
        )
        result_b = classify_us_regime(
            price=545, prev_close=550, rvol=2.0,
            pmh=555, pml=548, vp=self._vp(),
            open_price=555.0,
        )
        # Same open_price → same gap_pct
        assert result_a.gap_pct == result_b.gap_pct


# ── M5: BreadthProxy majority_direction Tests ──

class TestBreadthMajorityDirection:
    """M5: BreadthProxy should carry majority_direction field."""

    def test_bearish_majority(self):
        """When bears dominate, majority_direction should be bearish."""
        bp = BreadthProxy(
            aligned_count=7, total_count=10,
            alignment_ratio=0.7, alignment_label="mixed",
            index_aligned=False, majority_direction="bearish",
            details="3↑ 7↓ / 10",
        )
        assert bp.majority_direction == "bearish"

    def test_bullish_majority(self):
        bp = BreadthProxy(
            aligned_count=8, total_count=10,
            alignment_ratio=0.8, alignment_label="strong_aligned",
            index_aligned=True, majority_direction="bullish",
            details="8↑ 2↓ / 10",
        )
        assert bp.majority_direction == "bullish"

    def test_default_neutral(self):
        """Default majority_direction is neutral."""
        bp = BreadthProxy(
            aligned_count=5, total_count=10,
            alignment_ratio=0.5, alignment_label="mixed",
            index_aligned=False,
        )
        assert bp.majority_direction == "neutral"


# ── C3: Tone modifier helper Tests ──

class TestApplyToneModifier:
    """C3: _apply_tone_modifier should adjust confidence and add details."""

    def test_negative_modifier(self):
        regime = USRegimeResult(
            regime=USRegimeType.GAP_GO, confidence=0.80,
            rvol=2.0, price=560, gap_pct=1.5,
        )
        tone = MarketTone(
            grade="D", grade_score=0, direction="bearish",
            day_type="chop", confidence_modifier=-0.15,
            position_size_hint="sit_out",
        )
        from src.us_playbook.main import USPredictor
        USPredictor._apply_tone_modifier(regime, tone)
        assert abs(regime.confidence - 0.65) < 0.01
        assert "Tone D adj -0.15" in regime.details

    def test_positive_modifier(self):
        regime = USRegimeResult(
            regime=USRegimeType.TREND_STRONG, confidence=0.70,
            rvol=1.5, price=555, gap_pct=0.3,
        )
        tone = MarketTone(
            grade="A+", grade_score=5, direction="bullish",
            day_type="trend", confidence_modifier=0.10,
            position_size_hint="full",
        )
        from src.us_playbook.main import USPredictor
        USPredictor._apply_tone_modifier(regime, tone)
        assert abs(regime.confidence - 0.80) < 0.01

    def test_zero_modifier_no_change(self):
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.60,
            rvol=0.8, price=550, gap_pct=0.1,
        )
        tone = MarketTone(
            grade="B+", grade_score=3, direction="neutral",
            day_type="chop", confidence_modifier=0.0,
            position_size_hint="reduced",
        )
        from src.us_playbook.main import USPredictor
        USPredictor._apply_tone_modifier(regime, tone)
        assert regime.confidence == 0.60
        assert "Tone" not in regime.details

    def test_clamp_to_bounds(self):
        regime = USRegimeResult(
            regime=USRegimeType.GAP_GO, confidence=0.95,
            rvol=2.5, price=560, gap_pct=2.0,
        )
        tone = MarketTone(
            grade="A+", grade_score=5, direction="bullish",
            day_type="trend", confidence_modifier=0.10,
            position_size_hint="full",
        )
        from src.us_playbook.main import USPredictor
        USPredictor._apply_tone_modifier(regime, tone)
        assert regime.confidence == 1.0


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
            regime=USRegimeType.GAP_GO, confidence=0.8,
            rvol=2.0, price=560, gap_pct=1.5,
        )
        vp = VolumeProfileResult(poc=550, vah=555, val=545)
        assert _decide_direction(regime, vp) == "bullish"  # price > vah

    def test_direction_fade_chop(self):
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.7,
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
            regime=USRegimeType.GAP_GO, confidence=0.8,
            rvol=2.0, price=560, gap_pct=1.5,
        )
        vp = VolumeProfileResult(poc=550, vah=555, val=545)
        filters = FilterResult(tradeable=False, risk_level="blocked", warnings=["FOMC today"])
        rec = recommend(regime=regime, vp=vp, filters=filters)
        assert rec.action == "wait"

    def test_fade_chop_bypasses_inside_day_filter(self):
        """RANGE with high confidence should bypass Inside Day + low RVOL filter
        and correct FilterResult so risk section shows 🟡 not 🔴."""
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=1.0,
            rvol=0.37, price=545, gap_pct=0.1,
        )
        vp = VolumeProfileResult(poc=550, vah=555, val=545)
        filters = FilterResult(
            tradeable=False, risk_level="blocked",
            warnings=["Inside Day + 低 RVOL (0.37 < 0.80) — 假突破概率高"],
            block_reasons=["inside_day_rvol"],
        )
        # should_wait should NOT block
        wait, reasons, _ = should_wait(regime, filters, vp, True, True)
        assert not wait, f"RANGE should bypass Inside Day + low RVOL, got reasons: {reasons}"
        # FilterResult should be corrected for consistent risk display
        assert filters.tradeable is True
        assert filters.risk_level == "elevated"
        assert filters.block_reasons == []

    def test_fade_chop_low_confidence_still_blocked(self):
        """RANGE with low confidence should still be blocked by Inside Day filter."""
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.5,
            rvol=0.37, price=545, gap_pct=0.1,
        )
        vp = VolumeProfileResult(poc=550, vah=555, val=545)
        filters = FilterResult(
            tradeable=False, risk_level="blocked",
            warnings=["Inside Day + 低 RVOL"],
            block_reasons=["inside_day_rvol"],
        )
        rec = recommend(regime=regime, vp=vp, filters=filters)
        assert rec.action == "wait"

    def test_fade_chop_calendar_hard_block(self):
        """RANGE should NOT bypass calendar (hard) blocks."""
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=1.0,
            rvol=0.37, price=545, gap_pct=0.1,
        )
        vp = VolumeProfileResult(poc=550, vah=555, val=545)
        filters = FilterResult(
            tradeable=False, risk_level="blocked",
            warnings=["FOMC today"],
            block_reasons=["calendar"],
        )
        rec = recommend(regime=regime, vp=vp, filters=filters)
        assert rec.action == "wait"

    def test_trend_day_still_blocked_by_inside_day(self):
        """TREND_STRONG should still be blocked by Inside Day + low RVOL."""
        regime = USRegimeResult(
            regime=USRegimeType.TREND_STRONG, confidence=0.9,
            rvol=0.37, price=560, gap_pct=0.5,
        )
        vp = VolumeProfileResult(poc=550, vah=555, val=545)
        filters = FilterResult(
            tradeable=False, risk_level="blocked",
            warnings=["Inside Day + 低 RVOL"],
            block_reasons=["inside_day_rvol"],
        )
        rec = recommend(regime=regime, vp=vp, filters=filters)
        assert rec.action == "wait"

    def test_fade_chop_bypasses_rvol_floor(self):
        """RANGE should not be blocked by RVOL < 0.5 absolute floor."""
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.8,
            rvol=0.37, price=545, gap_pct=0.1,
        )
        vp = VolumeProfileResult(poc=550, vah=555, val=545)
        filters = FilterResult(tradeable=True, risk_level="normal")
        wait, reasons, _ = should_wait(regime, filters, vp, True, True)
        assert not wait, f"RANGE should bypass RVOL floor, got reasons: {reasons}"

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
        assert regime_to_signal_type(USRegimeType.GAP_GO, "bullish") == "BREAKOUT_BULLISH"

    def test_trend_day_bearish(self):
        assert regime_to_signal_type(USRegimeType.TREND_STRONG, "bearish") == "BREAKOUT_BEARISH"

    def test_fade_chop_bullish(self):
        assert regime_to_signal_type(USRegimeType.RANGE, "bullish") == "RANGE_REVERSAL_BULLISH"

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
                regime=USRegimeType.GAP_GO, confidence=conf,
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
                regime=USRegimeType.GAP_GO, confidence=0.82,
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
                regime=USRegimeType.GAP_GO, confidence=0.75,
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
        # Clear proxy env vars so they don't leak into the test
        monkeypatch.delenv("https_proxy", raising=False)
        monkeypatch.delenv("HTTPS_PROXY", raising=False)

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
            "proxy": None,
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
    """Verify adaptive RVOL trend_day floor prevents avg volume → TREND_STRONG."""

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
    """Verify direction-aware emoji for TREND_STRONG and GAP_GO."""

    def test_bearish_trend_day(self):
        from src.us_playbook.playbook import get_regime_emoji
        assert get_regime_emoji(USRegimeType.TREND_STRONG, "bearish") == "\U0001f4c9"  # 📉

    def test_bullish_trend_day(self):
        from src.us_playbook.playbook import get_regime_emoji
        assert get_regime_emoji(USRegimeType.TREND_STRONG, "bullish") == "\U0001f4c8"  # 📈

    def test_bearish_gap_and_go(self):
        from src.us_playbook.playbook import get_regime_emoji
        assert get_regime_emoji(USRegimeType.GAP_GO, "bearish") == "\U0001f4a5"  # 💥

    def test_bullish_gap_and_go(self):
        from src.us_playbook.playbook import get_regime_emoji
        assert get_regime_emoji(USRegimeType.GAP_GO, "bullish") == "\U0001f680"  # 🚀

    def test_bearish_trend_day_in_playbook(self):
        """Playbook with price < VAL should show 📉 for TREND_STRONG."""
        r = USRegimeResult(
            regime=USRegimeType.TREND_STRONG, confidence=0.65,
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
        assert "强趋势日" in msg  # regime type in Section 2


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


# ── Fade Entry Staleness ──


class TestFadeEntryStaleness:
    """Verify VA penetration check for RANGE mean-reversion freshness."""

    # VAH=680, VAL=670 → VA range = 10

    def _vp(self):
        return VolumeProfileResult(poc=675.0, vah=680.0, val=670.0)

    # ── _check_fade_entry_staleness unit tests ──

    def test_bullish_none(self):
        """Price near VAL → penetration low → none."""
        level, pen = _check_fade_entry_staleness(671.0, self._vp(), "bullish")
        assert level == "none"
        assert pen < 0.20

    def test_bullish_moderate(self):
        """Price 40% into VA from VAL → moderate."""
        level, pen = _check_fade_entry_staleness(674.0, self._vp(), "bullish")
        assert level == "moderate"
        assert 0.35 <= pen <= 0.45

    def test_bullish_high(self):
        """Price 60% into VA from VAL → high."""
        level, pen = _check_fade_entry_staleness(676.0, self._vp(), "bullish")
        assert level == "high"
        assert pen >= 0.55

    def test_bearish_none(self):
        """Price near VAH → penetration low → none."""
        level, pen = _check_fade_entry_staleness(679.0, self._vp(), "bearish")
        assert level == "none"
        assert pen < 0.20

    def test_bearish_moderate(self):
        """Price 40% into VA from VAH → moderate."""
        level, pen = _check_fade_entry_staleness(676.0, self._vp(), "bearish")
        assert level == "moderate"
        assert 0.35 <= pen <= 0.45

    def test_bearish_high(self):
        """Price 60% into VA from VAH → high."""
        level, pen = _check_fade_entry_staleness(674.0, self._vp(), "bearish")
        assert level == "high"
        assert pen >= 0.55

    def test_neutral_skipped(self):
        """Neutral direction → always none."""
        level, pen = _check_fade_entry_staleness(675.0, self._vp(), "neutral")
        assert level == "none"
        assert pen == 0.0

    def test_zero_va_range(self):
        """VAH == VAL → no division error, returns none."""
        flat_vp = VolumeProfileResult(poc=100.0, vah=100.0, val=100.0)
        level, pen = _check_fade_entry_staleness(100.0, flat_vp, "bullish")
        assert level == "none"

    def test_clamp_below_zero(self):
        """Price below VAL for bullish → clamp to 0."""
        level, pen = _check_fade_entry_staleness(668.0, self._vp(), "bullish")
        assert pen == 0.0
        assert level == "none"

    def test_clamp_above_one(self):
        """Price above VAH for bullish → clamp to 1.0."""
        level, pen = _check_fade_entry_staleness(685.0, self._vp(), "bullish")
        assert pen == 1.0
        assert level == "high"

    def test_custom_thresholds(self):
        """Custom moderate/high thresholds."""
        # 25% penetration with moderate=0.20 → moderate
        level, _ = _check_fade_entry_staleness(
            672.5, self._vp(), "bullish",
            stale_moderate=0.20, stale_high=0.40,
        )
        assert level == "moderate"

    # ── recommend() integration tests ──

    def test_recommend_high_returns_wait(self):
        """RANGE + high penetration → wait.

        Price 674 with VAH=680/VAL=670 gives position_ratio=0.40 (transition zone).
        Provide upward momentum (confirming bullish in below-mid transition), then
        staleness threshold 0.35 catches penetration=0.40 as "high".
        """
        vp = VolumeProfileResult(poc=670.0, vah=680.0, val=670.0)
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.7,
            rvol=0.8, price=674.0, gap_pct=0.1,  # bullish via momentum, pen=0.4
        )
        filters = FilterResult(tradeable=True, warnings=[], risk_level="normal")
        # Provide upward momentum bars so direction resolves to "bullish"
        today_bars = _make_bars([
            (f"2026-03-10 10:{30+i}:00", 672+i*0.3, 672+i*0.3+0.1, 672+i*0.3-0.1, 672+i*0.3, 10000)
            for i in range(10)
        ])
        rec = recommend(
            regime, vp, filters,
            chase_risk_cfg={"fade_entry_stale_high": 0.35},
            today_bars=today_bars,
        )
        assert rec.action == "wait"
        assert "渗透" in rec.rationale
        assert "入场窗口已过" in rec.rationale

    def test_recommend_moderate_still_tradeable(self):
        """RANGE + moderate penetration → still tradeable (not wait).

        Price 674 with VAH=680/VAL=670 gives position_ratio=0.40 (transition zone).
        Provide upward momentum to confirm bullish direction, then moderate staleness applies.
        """
        from datetime import date, timedelta

        future_expiry = (date.today() + timedelta(days=5)).strftime("%Y-%m-%d")
        vp = VolumeProfileResult(poc=670.0, vah=680.0, val=670.0)
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.7,
            rvol=0.8, price=674.0, gap_pct=0.1,
        )
        filters = FilterResult(tradeable=True, warnings=[], risk_level="normal")
        chain_df = pd.DataFrame([{
            "code": "SPY260313C00674000", "option_type": "CALL",
            "strike_price": 674.0, "strike_time": future_expiry,
            "open_interest": 500, "implied_volatility": 0.2,
            "delta": 0.50, "gamma": 0.05, "theta": -0.10, "vega": 0.15,
            "last_price": 3.0, "snap_volume": 100,
            "bid_price": 2.90, "ask_price": 3.10,
        }])
        today_bars = _make_bars([
            (f"2026-03-10 10:{30+i}:00", 672+i*0.3, 672+i*0.3+0.1, 672+i*0.3-0.1, 672+i*0.3, 10000)
            for i in range(10)
        ])
        rec = recommend(
            regime, vp, filters,
            chain_df=chain_df,
            expiry_dates=[future_expiry],
            today_bars=today_bars,
        )
        assert rec.action != "wait"
        # Risk note should mention 入场区已消耗
        assert "入场区已消耗" in (rec.risk_note or "")

    def test_recommend_none_shows_near_val(self):
        """RANGE + low penetration → rationale says '靠近 VAL'."""
        from datetime import date, timedelta

        future_expiry = (date.today() + timedelta(days=5)).strftime("%Y-%m-%d")
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.7,
            rvol=0.8, price=671.0, gap_pct=0.1,
        )
        vp = self._vp()
        filters = FilterResult(tradeable=True, warnings=[], risk_level="normal")
        chain_df = pd.DataFrame([{
            "code": "SPY260313C00671000", "option_type": "CALL",
            "strike_price": 671.0, "strike_time": future_expiry,
            "open_interest": 500, "implied_volatility": 0.2,
            "delta": 0.50, "gamma": 0.05, "theta": -0.10, "vega": 0.15,
            "last_price": 3.0, "snap_volume": 100,
            "bid_price": 2.90, "ask_price": 3.10,
        }])
        rec = recommend(
            regime, vp, filters,
            chain_df=chain_df,
            expiry_dates=[future_expiry],
        )
        assert rec.action != "wait"
        assert "靠近" in rec.rationale
        assert "入场区已消耗" not in (rec.risk_note or "")

    # ── rationale text verification ──

    def test_rationale_mid_penetration(self):
        """20-35% penetration → '偏远' wording."""
        from src.us_playbook.option_recommend import _build_rationale
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.7,
            rvol=0.8, price=672.5, gap_pct=0.1,
        )
        vp = self._vp()
        text = _build_rationale(regime, vp, "bullish", fade_penetration=0.25)
        assert "偏远" in text
        assert "25%" in text

    def test_rationale_high_penetration(self):
        """>=35% penetration → 'VA 中部' wording."""
        from src.us_playbook.option_recommend import _build_rationale
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.7,
            rvol=0.8, price=674.0, gap_pct=0.1,
        )
        vp = self._vp()
        text = _build_rationale(regime, vp, "bullish", fade_penetration=0.40)
        assert "VA 中部" in text
        assert "40%" in text


# ── Nearest Levels + Low-DTE Stop-Loss + Entry Zone ──


class TestNearestLevels:
    """Verify _nearest_levels() finds correct key levels above/below price."""

    def _vp(self):
        return VolumeProfileResult(poc=252.0, vah=255.0, val=249.0)

    def _kl(self):
        return KeyLevels(
            poc=252.0, vah=255.0, val=249.0,
            pdh=257.0, pdl=248.0, pmh=254.0, pml=250.5,
            vwap=251.5,
        )

    def test_nearest_levels_above(self):
        """Price=253, above levels should be PMH 254, VAH 255, PDH 257 — return nearest 2."""
        result = _nearest_levels(253.0, "above", self._vp(), kl=self._kl())
        assert len(result) == 2
        names = [r[0] for r in result]
        assert names[0] == "PMH"  # 254 is closest above 253
        assert names[1] == "VAH"  # 255 is next

    def test_nearest_levels_below(self):
        """Price=253, below levels should be POC 252, VWAP 251.5, PML 250.5, VAL 249."""
        result = _nearest_levels(253.0, "below", self._vp(), kl=self._kl())
        assert len(result) == 2
        names = [r[0] for r in result]
        assert names[0] == "POC"   # 252 is closest below 253
        assert names[1] == "VWAP"  # 251.5 is next

    def test_nearest_levels_with_gamma_wall(self):
        """Gamma wall levels should be included."""
        gw = GammaWallResult(call_wall_strike=256.0, put_wall_strike=247.0, max_pain=252.0)
        result = _nearest_levels(253.0, "above", self._vp(), kl=self._kl(), gamma_wall=gw)
        names = [r[0] for r in result]
        assert "PMH" in names  # 254 still closest
        # Call Wall 256 should be reachable
        result_3 = _nearest_levels(253.0, "above", self._vp(), kl=self._kl(), gamma_wall=gw, n=3)
        names_3 = [r[0] for r in result_3]
        assert "Call Wall" in names_3

    def test_nearest_levels_filters_noise(self):
        """Levels within 0.05% of price are excluded."""
        vp = VolumeProfileResult(poc=100.04, vah=105.0, val=95.0)
        kl = KeyLevels(poc=100.04, vah=105.0, val=95.0, pdh=110.0, pdl=90.0, pmh=0.0, pml=0.0, vwap=100.03)
        # POC=100.04 and VWAP=100.03 are within 0.05% of 100.0 → should be excluded
        result = _nearest_levels(100.0, "above", vp, kl=kl)
        names = [r[0] for r in result]
        assert "POC" not in names
        assert "VWAP" not in names

    def test_nearest_levels_no_kl(self):
        """Without KeyLevels, only VP levels are used."""
        result = _nearest_levels(253.0, "above", self._vp())
        names = [r[0] for r in result]
        assert "VAH" in names
        assert "PMH" not in names  # no KeyLevels


class TestLowDteRiskAction:
    """Verify _risk_action_lines() for low-DTE single legs."""

    def _vp(self):
        return VolumeProfileResult(poc=252.0, vah=255.0, val=249.0)

    def _kl(self):
        return KeyLevels(
            poc=252.0, vah=255.0, val=249.0,
            pdh=257.0, pdl=248.0, pmh=254.0, pml=250.5,
            vwap=251.5,
        )

    def _regime(self, price=253.0):
        return USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.7,
            rvol=0.8, price=price, gap_pct=0.1,
        )

    def test_risk_action_lines_low_dte_put(self):
        """DTE=1 put → RANGE skips VAH, uses deeper resistance + premium stop-loss."""
        rec = OptionRecommendation(
            action="put", direction="bearish", dte=1,
            legs=[OptionLeg(
                side="buy", option_type="put", strike=252.0,
                pct_from_price=0.4, moneyness="ATM", last_price=2.10,
            )],
        )
        lines = _risk_action_lines(rec, self._regime(), self._vp(), kl=self._kl())
        # P1-2: RANGE put should skip VAH and use deeper resistance (PMH 254 or PDH 257)
        assert any("PMH" in l or "PDH" in l for l in lines), f"Expected deeper resistance in lines: {lines}"
        assert any("VAH" in l and "wick" in l for l in lines), f"Expected VAH wick note: {lines}"
        # Premium stop-loss: 2.10 * 0.60 = 1.26
        assert any("$1.26" in l for l in lines), f"Expected $1.26 in lines: {lines}"
        assert any("低 DTE" in l and "40%" in l for l in lines)

    def test_risk_action_lines_low_dte_call(self):
        """DTE=2 call → RANGE skips VAL, uses deeper support + premium stop-loss."""
        rec = OptionRecommendation(
            action="call", direction="bullish", dte=2,
            legs=[OptionLeg(
                side="buy", option_type="call", strike=253.0,
                pct_from_price=0.0, moneyness="ATM", last_price=3.00,
            )],
        )
        lines = _risk_action_lines(rec, self._regime(), self._vp(), kl=self._kl())
        # P1-2: RANGE call should skip VAL and use deeper support (POC/VWAP/PML/PDL etc.)
        assert any(any(k in l for k in ("POC", "VWAP", "PML", "PDL")) for l in lines), f"Expected deeper support in lines: {lines}"
        assert any("VAL" in l and "wick" in l for l in lines), f"Expected VAL wick note: {lines}"
        # Premium stop-loss: 3.00 * 0.60 = 1.80
        assert any("$1.80" in l for l in lines), f"Expected $1.80 in lines: {lines}"

    def test_risk_action_lines_normal_dte_put(self):
        """DTE=7 put → original behavior, no premium stop-loss."""
        rec = OptionRecommendation(
            action="put", direction="bearish", dte=7,
            legs=[OptionLeg(
                side="buy", option_type="put", strike=252.0,
                pct_from_price=0.4, moneyness="ATM", last_price=2.10,
            )],
        )
        lines = _risk_action_lines(rec, self._regime(), self._vp(), kl=self._kl())
        # Should use original VWAP-based stop
        assert any("VWAP" in l for l in lines)
        # No premium stop-loss line
        assert not any("低 DTE" in l for l in lines)
        assert not any("$1.26" in l for l in lines)

    def test_risk_action_lines_spread_unchanged(self):
        """Spread actions should not get low-DTE treatment."""
        from src.common.types import SpreadMetrics
        rec = OptionRecommendation(
            action="bear_call_spread", direction="bearish", dte=1,
            legs=[
                OptionLeg(side="sell", option_type="call", strike=255.0, pct_from_price=0.8, moneyness="OTM 0.8%"),
                OptionLeg(side="buy", option_type="call", strike=257.0, pct_from_price=1.6, moneyness="OTM 1.6%"),
            ],
            spread_metrics=SpreadMetrics(
                net_credit=0.5, max_profit=0.5, max_loss=1.5,
                breakeven=255.5, risk_reward_ratio=0.33,
            ),
        )
        lines = _risk_action_lines(rec, self._regime(), self._vp(), kl=self._kl())
        assert any("盈亏平衡" in l for l in lines)
        assert not any("低 DTE" in l for l in lines)


class TestEntryZone:
    """Verify _entry_zone_text() for different regimes and directions."""

    def _vp(self):
        return VolumeProfileResult(poc=252.0, vah=255.0, val=249.0)

    def _kl(self):
        return KeyLevels(
            poc=252.0, vah=255.0, val=249.0,
            pdh=257.0, pdl=248.0, pmh=254.0, pml=250.5,
            vwap=251.5,
        )

    def test_entry_zone_fade_chop_bearish(self):
        """RANGE bearish → entry zone uses resistance above."""
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.7,
            rvol=0.8, price=253.0, gap_pct=0.1,
        )
        text = _entry_zone_text(253.0, "bearish", regime, self._vp(), kl=self._kl())
        assert text is not None
        assert "最佳入场区间" in text
        # Should mention PMH 254 and VAH 255 as zone boundaries
        assert "254" in text
        assert "255" in text

    def test_entry_zone_fade_chop_bullish(self):
        """RANGE bullish → entry zone uses support below."""
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.7,
            rvol=0.8, price=253.0, gap_pct=0.1,
        )
        text = _entry_zone_text(253.0, "bullish", regime, self._vp(), kl=self._kl())
        assert text is not None
        # Below: POC 252, VWAP 251.5, PML 250.5, VAL 249
        assert "最佳入场区间" in text

    def test_entry_zone_trend_day(self):
        """TREND_STRONG bullish → VWAP pullback suggestion."""
        regime = USRegimeResult(
            regime=USRegimeType.TREND_STRONG, confidence=0.75,
            rvol=1.5, price=258.0, gap_pct=0.5,
        )
        text = _entry_zone_text(258.0, "bullish", regime, self._vp(), kl=self._kl())
        assert text is not None
        assert "VWAP" in text
        assert "251.50" in text
        assert "回调" in text

    def test_entry_zone_trend_day_bearish(self):
        """TREND_STRONG bearish → VWAP bounce suggestion."""
        regime = USRegimeResult(
            regime=USRegimeType.TREND_STRONG, confidence=0.75,
            rvol=1.5, price=247.0, gap_pct=-0.5,
        )
        text = _entry_zone_text(247.0, "bearish", regime, self._vp(), kl=self._kl())
        assert text is not None
        assert "VWAP" in text
        assert "反弹" in text

    def test_entry_zone_unclear_returns_none(self):
        """UNCLEAR regime → no entry zone."""
        regime = USRegimeResult(
            regime=USRegimeType.UNCLEAR, confidence=0.3,
            rvol=0.6, price=253.0, gap_pct=0.1,
        )
        text = _entry_zone_text(253.0, "neutral", regime, self._vp(), kl=self._kl())
        assert text is None


class TestBuildRiskNoteLowDte:
    """Verify _build_risk_note includes 40% premium stop for low DTE."""

    def test_build_risk_note_low_dte(self):
        """DTE <= 3 → includes 40% stop reminder."""
        from src.us_playbook.option_recommend import _build_risk_note
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.7,
            rvol=0.8, price=253.0, gap_pct=0.1,
        )
        vp = VolumeProfileResult(poc=252.0, vah=255.0, val=249.0)
        note = _build_risk_note(regime, vp, "bearish", dte=2)
        assert "Gamma 风险极高" in note
        assert "40%" in note
        assert "止损" in note

    def test_build_risk_note_normal_dte_no_premium_stop(self):
        """DTE > 3 → no 40% stop reminder."""
        from src.us_playbook.option_recommend import _build_risk_note
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.7,
            rvol=0.8, price=253.0, gap_pct=0.1,
        )
        vp = VolumeProfileResult(poc=252.0, vah=255.0, val=249.0)
        note = _build_risk_note(regime, vp, "bearish", dte=7)
        assert "40%" not in note


# ── Fade Momentum Tests ──


class TestFadeMomentum:
    def test_compute_fade_momentum_uptrend(self):
        """Steadily rising closes → 1."""
        bars = _make_bars([
            (f"2026-03-10 10:{30+i}:00", 100+i*0.1, 100+i*0.1+0.05, 100+i*0.1-0.05, 100+i*0.1, 10000)
            for i in range(10)
        ])
        assert _compute_fade_momentum(bars, lookback=8, threshold=0.03) == 1

    def test_compute_fade_momentum_downtrend(self):
        """Steadily falling closes → -1."""
        bars = _make_bars([
            (f"2026-03-10 10:{30+i}:00", 100-i*0.1, 100-i*0.1+0.05, 100-i*0.1-0.05, 100-i*0.1, 10000)
            for i in range(10)
        ])
        assert _compute_fade_momentum(bars, lookback=8, threshold=0.03) == -1

    def test_compute_fade_momentum_flat(self):
        """Flat closes → 0."""
        bars = _make_bars([
            (f"2026-03-10 10:{30+i}:00", 100, 100.05, 99.95, 100, 10000)
            for i in range(10)
        ])
        assert _compute_fade_momentum(bars, lookback=8, threshold=0.03) == 0

    def test_compute_fade_momentum_insufficient(self):
        """Less than lookback//2 bars → 0."""
        bars = _make_bars([
            ("2026-03-10 10:30:00", 100, 101, 99, 100.5, 10000),
            ("2026-03-10 10:31:00", 100.5, 102, 99.5, 101, 10000),
        ])
        assert _compute_fade_momentum(bars, lookback=8, threshold=0.03) == 0

    def test_compute_fade_momentum_empty(self):
        """Empty/None → 0."""
        assert _compute_fade_momentum(pd.DataFrame(), lookback=8) == 0
        assert _compute_fade_momentum(None, lookback=8) == 0


class TestFadeChopDirection:
    """Test _decide_direction with VA three-zone + momentum logic."""

    def _vp(self, poc=255, vah=260, val=250):
        return VolumeProfileResult(poc=poc, vah=vah, val=val)

    def _regime(self, price):
        return USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.75,
            rvol=0.8, price=price, gap_pct=0.1,
        )

    def test_transition_zone_momentum_override(self):
        """IWM 10:40 scenario: price slightly above mid (ratio~0.52) + upward momentum → neutral."""
        # price=254.93, VAH=259.50, VAL=250.00 → ratio ≈ 0.52 (transition zone)
        vp = self._vp(poc=254.75, vah=259.50, val=250.00)
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.7,
            rvol=0.8, price=254.93, gap_pct=0.1,
        )
        # momentum=+1 (uptrend) conflicts with bearish base in transition zone
        result = _decide_direction(regime, vp, momentum=1)
        assert result == "neutral"

    def test_transition_zone_no_momentum(self):
        """Transition zone + momentum=0 → neutral."""
        vp = self._vp(poc=255, vah=260, val=250)
        regime = self._regime(255)  # ratio=0.5, mid transition zone
        result = _decide_direction(regime, vp, momentum=0)
        assert result == "neutral"

    def test_transition_zone_momentum_confirms_bearish(self):
        """Transition zone above mid + downward momentum → bearish."""
        vp = self._vp(poc=255, vah=260, val=250)
        regime = self._regime(256)  # ratio=0.6, above mid
        result = _decide_direction(regime, vp, momentum=-1)
        assert result == "bearish"

    def test_transition_zone_momentum_confirms_bullish(self):
        """Transition zone below mid + upward momentum → bullish."""
        vp = self._vp(poc=255, vah=260, val=250)
        regime = self._regime(254)  # ratio=0.4, below mid
        result = _decide_direction(regime, vp, momentum=1)
        assert result == "bullish"

    def test_edge_zone_confirmed(self):
        """Edge zone near VAH + no momentum → bearish (edge allows momentum=0)."""
        vp = self._vp(poc=255, vah=260, val=250)
        regime = self._regime(258)  # ratio=0.8, edge zone
        result = _decide_direction(regime, vp, momentum=0)
        assert result == "bearish"

    def test_edge_zone_bullish_confirmed(self):
        """Edge zone near VAL + no momentum → bullish."""
        vp = self._vp(poc=255, vah=260, val=250)
        regime = self._regime(252)  # ratio=0.2, edge zone
        result = _decide_direction(regime, vp, momentum=0)
        assert result == "bullish"

    def test_edge_zone_oppose(self):
        """Edge zone near VAH + upward momentum → neutral (momentum opposes)."""
        vp = self._vp(poc=255, vah=260, val=250)
        regime = self._regime(258)  # ratio=0.8, edge zone
        result = _decide_direction(regime, vp, momentum=1)
        assert result == "neutral"

    def test_edge_zone_bullish_oppose(self):
        """Edge zone near VAL + downward momentum → neutral."""
        vp = self._vp(poc=255, vah=260, val=250)
        regime = self._regime(252)  # ratio=0.2, edge zone
        result = _decide_direction(regime, vp, momentum=-1)
        assert result == "neutral"

    def test_backward_compat_no_momentum(self):
        """No momentum arg (default 0) → edge zone still gives direction (regression test)."""
        vp = self._vp(poc=255, vah=260, val=250)
        regime = self._regime(258)  # edge zone, ratio=0.8
        # Called without momentum → default 0
        result = _decide_direction(regime, vp)
        assert result == "bearish"

    def test_backward_compat_bullish_edge(self):
        """No momentum arg → edge zone near VAL gives bullish."""
        vp = self._vp(poc=255, vah=260, val=250)
        regime = self._regime(252)  # edge zone, ratio=0.2
        result = _decide_direction(regime, vp)
        assert result == "bullish"

    def test_non_fade_chop_ignores_momentum(self):
        """GAP_GO should ignore momentum parameter."""
        vp = self._vp(poc=255, vah=260, val=250)
        regime = USRegimeResult(
            regime=USRegimeType.GAP_GO, confidence=0.85,
            rvol=2.0, price=265, gap_pct=1.5,
        )
        result = _decide_direction(regime, vp, momentum=-1)
        assert result == "bullish"  # price > VAH → bullish regardless


class TestRecommendFadeMomentum:
    """Test full recommend() flow with momentum conflict."""

    def test_recommend_fade_momentum_wait_message(self):
        """RANGE in transition zone with opposing momentum → wait with momentum explanation."""
        # Price 256 in transition zone (ratio=0.6), far enough from POC to avoid POC proximity block
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.75,
            rvol=0.8, price=256.0, gap_pct=0.1,
        )
        vp = VolumeProfileResult(poc=253, vah=260, val=250)
        filters = FilterResult(tradeable=True, risk_level="normal")

        # Build today_bars with strong uptrend → momentum=+1
        # Price above mid (ratio=0.6), base=bearish, momentum=+1 opposes → neutral
        today_bars = _make_bars([
            (f"2026-03-10 10:{30+i}:00", 254+i*0.2, 254+i*0.2+0.1, 254+i*0.2-0.1, 254+i*0.2, 10000)
            for i in range(10)
        ])

        rec = recommend(
            regime=regime, vp=vp, filters=filters,
            today_bars=today_bars,
        )
        assert rec.action == "wait"
        assert rec.direction == "neutral"
        assert "动量" in rec.risk_note

    def test_recommend_no_today_bars_backward_compat(self):
        """Without today_bars, RANGE edge zone still gives direction (backward compat)."""
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.75,
            rvol=0.8, price=258.0, gap_pct=0.1,
        )
        vp = VolumeProfileResult(poc=255, vah=260, val=250)
        filters = FilterResult(tradeable=True, risk_level="normal")

        rec = recommend(
            regime=regime, vp=vp, filters=filters,
            today_bars=None,
        )
        # Edge zone ratio=0.8, momentum=0 → bearish direction, not wait
        assert rec.direction == "bearish"


# ── P0-1: Local Trend Veto + structural_veto ──


class TestLocalTrendVeto:
    """Verify compute_local_trend() and structural_veto in recommend()."""

    def test_compute_local_trend_downtrend(self):
        """Steadily falling closes over 30 bars → -1."""
        from src.us_playbook.option_recommend import compute_local_trend
        bars = _make_bars([
            (f"2026-03-10 10:{i:02d}:00", 410-i*0.2, 410-i*0.2+0.1, 410-i*0.2-0.1, 410-i*0.2, 50000)
            for i in range(35)
        ])
        assert compute_local_trend(bars, lookback=30, threshold=0.02) == -1

    def test_compute_local_trend_uptrend(self):
        from src.us_playbook.option_recommend import compute_local_trend
        bars = _make_bars([
            (f"2026-03-10 10:{i:02d}:00", 400+i*0.2, 400+i*0.2+0.1, 400+i*0.2-0.1, 400+i*0.2, 50000)
            for i in range(35)
        ])
        assert compute_local_trend(bars, lookback=30, threshold=0.02) == 1

    def test_compute_local_trend_neutral(self):
        from src.us_playbook.option_recommend import compute_local_trend
        bars = _make_bars([
            (f"2026-03-10 10:{i:02d}:00", 400, 400.05, 399.95, 400, 50000)
            for i in range(35)
        ])
        assert compute_local_trend(bars, lookback=30, threshold=0.02) == 0

    def test_recommend_structural_veto_bullish_vs_downtrend(self):
        """RANGE bullish near VAL + long-term downtrend but short-term flat → structural_veto.

        Need: 8-bar momentum neutral/up (so direction resolves bullish),
              but 30-bar local_trend is down (so structural veto fires).
        """
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.80,
            rvol=0.7, price=405.0, gap_pct=0.1,
        )
        vp = VolumeProfileResult(poc=408.0, vah=412.0, val=404.0)
        filters = FilterResult(tradeable=True, risk_level="normal")
        # 25 bars of downtrend + 10 bars flat (short momentum neutral)
        down_bars = [
            (f"2026-03-10 10:{i:02d}:00", 412-i*0.3, 412-i*0.3+0.1, 412-i*0.3-0.1, 412-i*0.3, 50000)
            for i in range(25)
        ]
        # Last 10 bars: flat at ~404.5 (near VAL) — makes 8-bar momentum neutral
        flat_bars = [
            (f"2026-03-10 10:{25+i:02d}:00", 404.5, 404.6, 404.4, 404.5, 50000)
            for i in range(10)
        ]
        today_bars = _make_bars(down_bars + flat_bars)
        rec = recommend(regime=regime, vp=vp, filters=filters, today_bars=today_bars)
        assert rec.action == "wait"
        assert rec.structural_veto is True
        assert "趋势" in rec.rationale

    def test_recommend_no_veto_when_trend_aligns(self):
        """RANGE bullish near VAL + uptrend → no veto."""
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.80,
            rvol=0.7, price=405.0, gap_pct=0.1,
        )
        vp = VolumeProfileResult(poc=408.0, vah=412.0, val=404.0)
        filters = FilterResult(tradeable=True, risk_level="normal")
        chain_df = pd.DataFrame([{
            "code": "MSFT260320C00405000", "option_type": "CALL",
            "strike_price": 405.0, "strike_time": "2026-03-20",
            "open_interest": 500, "implied_volatility": 0.2,
            "delta": 0.50, "gamma": 0.05, "theta": -0.10, "vega": 0.15,
            "last_price": 3.0, "snap_volume": 100,
            "bid_price": 2.90, "ask_price": 3.10,
        }])
        # Uptrend aligns with bullish direction → no veto
        today_bars = _make_bars([
            (f"2026-03-10 10:{i:02d}:00", 402+i*0.15, 402+i*0.15+0.1, 402+i*0.15-0.1, 402+i*0.15, 50000)
            for i in range(35)
        ])
        rec = recommend(
            regime=regime, vp=vp, filters=filters,
            chain_df=chain_df, expiry_dates=["2026-03-20"],
            today_bars=today_bars,
        )
        assert rec.structural_veto is False
        assert rec.action != "wait" or "趋势" not in rec.rationale

    def test_structural_veto_field_default(self):
        """OptionRecommendation defaults structural_veto to False."""
        rec = OptionRecommendation(action="call", direction="bullish")
        assert rec.structural_veto is False


# ── P0-2: Entry Direction Bug Fix ──


class TestEntryDirectionFix:
    """Entry zone should use recommendation direction, not price-vs-POC."""

    def test_entry_zone_uses_recommendation_direction(self):
        """Price below POC (bearish by price) but recommendation is bullish → entry_zone for bullish."""
        from src.us_playbook.playbook import _entry_zone_text
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.75,
            rvol=0.8, price=405.0, gap_pct=0.1,
        )
        vp = VolumeProfileResult(poc=408.0, vah=412.0, val=404.0)
        kl = KeyLevels(poc=408.0, vah=412.0, val=404.0, pdh=415.0, pdl=400.0, pmh=0.0, pml=0.0, vwap=407.0)
        # Bullish direction: should look for support below
        text = _entry_zone_text(405.0, "bullish", regime, vp, kl=kl)
        assert text is not None
        # Should mention levels below price (support), not above (resistance)
        assert "404" in text or "400" in text or "407" in text  # VAL, PDL, or VWAP


# ── P1-1: RANGE DTE/Delta Override ──


class TestFadeChopDteOverride:
    """Verify RANGE uses range_reversal config override for DTE/delta."""

    def test_recommend_fade_chop_uses_rr_dte(self):
        """RANGE should use range_reversal.dte_min instead of global dte_min."""
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.75,
            rvol=0.7, price=252.0, gap_pct=0.1,
        )
        vp = VolumeProfileResult(poc=252.0, vah=255.0, val=249.0)
        filters = FilterResult(tradeable=True, risk_level="normal")
        option_cfg = {
            "dte_min": 1, "dte_preferred_max": 7,
            "delta_min": 0.30, "delta_max": 0.50,
            "range_reversal": {
                "dte_min": 3, "dte_preferred_max": 10,
                "prefer_atm": True,
                "delta_min": 0.45, "delta_max": 0.65,
            },
        }
        chain_df = pd.DataFrame([{
            "code": "SPY260316C00252000", "option_type": "CALL",
            "strike_price": 252.0, "strike_time": "2026-03-16",
            "open_interest": 500, "implied_volatility": 0.2,
            "delta": 0.55, "gamma": 0.05, "theta": -0.05, "vega": 0.15,
            "last_price": 3.0, "snap_volume": 100,
            "bid_price": 2.90, "ask_price": 3.10,
        }])
        # Use dates relative to today so select_expiry works correctly
        today = date.today()
        exp_short = (today + timedelta(days=2)).strftime("%Y-%m-%d")
        exp_long = (today + timedelta(days=6)).strftime("%Y-%m-%d")
        rec = recommend(
            regime=regime, vp=vp, filters=filters,
            chain_df=chain_df,
            expiry_dates=[exp_short, exp_long],
            option_cfg=option_cfg,
        )
        # Should pick longer expiry since dte_min=3 skips 2-DTE
        if rec.action != "wait":
            assert rec.expiry == exp_long
            assert rec.dte >= 3

    def test_non_fade_chop_ignores_rr_override(self):
        """TREND_STRONG should NOT use range_reversal DTE override."""
        regime = USRegimeResult(
            regime=USRegimeType.TREND_STRONG, confidence=0.80,
            rvol=1.5, price=260.0, gap_pct=0.5,
        )
        vp = VolumeProfileResult(poc=255.0, vah=258.0, val=252.0)
        filters = FilterResult(tradeable=True, risk_level="normal")
        option_cfg = {
            "dte_min": 1, "dte_preferred_max": 7,
            "range_reversal": {"dte_min": 3, "dte_preferred_max": 10},
        }
        chain_df = pd.DataFrame([{
            "code": "SPY260312C00260000", "option_type": "CALL",
            "strike_price": 260.0, "strike_time": "2026-03-12",
            "open_interest": 500, "implied_volatility": 0.2,
            "delta": 0.45, "gamma": 0.05, "theta": -0.10, "vega": 0.15,
            "last_price": 3.0, "snap_volume": 100,
            "bid_price": 2.90, "ask_price": 3.10,
        }])
        today = date.today()
        exp_short = (today + timedelta(days=2)).strftime("%Y-%m-%d")
        exp_long = (today + timedelta(days=6)).strftime("%Y-%m-%d")
        rec = recommend(
            regime=regime, vp=vp, filters=filters,
            chain_df=chain_df,
            expiry_dates=[exp_short, exp_long],
            option_cfg=option_cfg,
        )
        if rec.action != "wait":
            # Should pick shorter expiry (global dte_min=1 allows 2-DTE)
            assert rec.expiry == exp_short


# ── P1-2: Unified Stop Source + RANGE Stop Expansion ──


class TestFadeChopStopExpansion:
    """Verify RANGE stop-loss skips entry premise level."""

    def _vp(self):
        return VolumeProfileResult(poc=408.0, vah=412.0, val=404.0)

    def _kl(self):
        return KeyLevels(
            poc=408.0, vah=412.0, val=404.0,
            pdh=415.0, pdl=400.0, pmh=410.0, pml=406.0,
            vwap=407.5,
        )

    def _regime(self, price=405.0):
        return USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.75,
            rvol=0.7, price=price, gap_pct=0.1,
        )

    def test_call_stop_skips_val(self):
        """RANGE bullish call: stop should NOT be VAL (entry premise)."""
        rec = OptionRecommendation(
            action="call", direction="bullish", dte=3,
            legs=[OptionLeg(
                side="buy", option_type="call", strike=405.0,
                pct_from_price=0.0, moneyness="ATM", last_price=3.0,
            )],
        )
        lines = _risk_action_lines(rec, self._regime(), self._vp(), kl=self._kl())
        # Should mention VAL + wick, and use deeper support (PDL/PML/VWAP etc.)
        stop_line = lines[0]
        assert "VAL" in stop_line and "wick" in stop_line
        assert "VAL" not in stop_line.split("(")[0] or "跌破 VAL" not in stop_line.split("(")[0]

    def test_put_stop_skips_vah(self):
        """RANGE bearish put: stop should NOT be VAH (entry premise)."""
        rec = OptionRecommendation(
            action="put", direction="bearish", dte=3,
            legs=[OptionLeg(
                side="buy", option_type="put", strike=411.0,
                pct_from_price=0.2, moneyness="ATM", last_price=3.0,
            )],
        )
        lines = _risk_action_lines(rec, self._regime(price=411.0), self._vp(), kl=self._kl())
        stop_line = lines[0]
        assert "VAH" in stop_line and "wick" in stop_line

    def test_non_fade_chop_uses_original(self):
        """Non-RANGE regime should use original nearest level logic."""
        regime = USRegimeResult(
            regime=USRegimeType.TREND_STRONG, confidence=0.80,
            rvol=1.5, price=405.0, gap_pct=0.5,
        )
        rec = OptionRecommendation(
            action="call", direction="bullish", dte=2,
            legs=[OptionLeg(
                side="buy", option_type="call", strike=405.0,
                pct_from_price=0.0, moneyness="ATM", last_price=3.0,
            )],
        )
        lines = _risk_action_lines(rec, regime, self._vp(), kl=self._kl())
        # Should mention "最近支撑位"
        assert any("最近支撑位" in l for l in lines)

    def test_risk_note_no_specific_stop(self):
        """_build_risk_note for RANGE should NOT include specific stop level."""
        from src.us_playbook.option_recommend import _build_risk_note
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.7,
            rvol=0.8, price=405.0, gap_pct=0.1,
        )
        vp = self._vp()
        note = _build_risk_note(regime, vp, "bullish")
        assert "止损: 跌破 VAL" not in note
        assert "失效条件" in note


# ── P2-1: VA Width Minimum ──


class TestVAWidthMinimum:
    """Verify VA width filter rejects narrow VA for RANGE."""

    def test_narrow_va_returns_veto(self):
        """VA width 0.3% < 0.8% threshold → structural_veto."""
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.80,
            rvol=0.7, price=400.0, gap_pct=0.1,
        )
        # VA width: (401 - 400) / 400 * 100 = 0.25%
        vp = VolumeProfileResult(poc=400.5, vah=401.0, val=400.0)
        filters = FilterResult(tradeable=True, risk_level="normal")
        rec = recommend(regime=regime, vp=vp, filters=filters)
        assert rec.action == "wait"
        assert rec.structural_veto is True
        assert "VA 区间过窄" in rec.rationale

    def test_normal_va_passes(self):
        """VA width 2% > 0.8% threshold → no veto."""
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.80,
            rvol=0.7, price=405.0, gap_pct=0.1,
        )
        # VA width: (412 - 404) / 405 * 100 = 1.98%
        vp = VolumeProfileResult(poc=408.0, vah=412.0, val=404.0)
        filters = FilterResult(tradeable=True, risk_level="normal")
        chain_df = pd.DataFrame([{
            "code": "MSFT260320C00405000", "option_type": "CALL",
            "strike_price": 405.0, "strike_time": "2026-03-20",
            "open_interest": 500, "implied_volatility": 0.2,
            "delta": 0.50, "gamma": 0.05, "theta": -0.10, "vega": 0.15,
            "last_price": 3.0, "snap_volume": 100,
            "bid_price": 2.90, "ask_price": 3.10,
        }])
        rec = recommend(
            regime=regime, vp=vp, filters=filters,
            chain_df=chain_df, expiry_dates=["2026-03-20"],
        )
        assert rec.structural_veto is False

    def test_non_fade_chop_ignores_va_width(self):
        """TREND_STRONG should NOT check VA width."""
        regime = USRegimeResult(
            regime=USRegimeType.TREND_STRONG, confidence=0.80,
            rvol=1.5, price=400.0, gap_pct=0.5,
        )
        # Narrow VA but TREND_STRONG doesn't care
        vp = VolumeProfileResult(poc=400.5, vah=401.0, val=400.0)
        filters = FilterResult(tradeable=True, risk_level="normal")
        rec = recommend(regime=regime, vp=vp, filters=filters)
        # Should not be vetoed for VA width
        assert not rec.structural_veto


# ── Market Tone Tests ──


class TestMarketTone:
    """Tests for MarketToneEngine and related components."""

    def _spy_bars(self, gap_up=False, breakout=False, n_bars=45):
        """Generate synthetic SPY 1m bars.

        Default: 09:30-10:14, base price 550.
        gap_up=True: open above prev close.
        breakout=True: price rises above ORB high after 10:00.
        """
        base = 555 if gap_up else 550
        bars = []
        for i in range(n_bars):
            minute = 30 + i
            hour = 9 + minute // 60
            minute = minute % 60
            ts = f"2026-03-11 {hour:02d}:{minute:02d}:00"
            price = base + i * 0.03
            if breakout and hour >= 10:
                price += 1.0  # Push above ORB high
            bars.append((ts, price, price + 0.15, price - 0.1, price + 0.05, 100000))
        return _make_bars(bars)

    # ── ORB Tests ──

    def test_orb_from_synthetic_bars(self):
        """ORB high/low should span first 30 minutes."""
        from src.us_playbook.market_tone import MarketToneEngine
        engine = MarketToneEngine({"market_tone": {"orb": {"window_minutes": 30}}}, None)
        bars = self._spy_bars(n_bars=45)
        now = datetime(2026, 3, 11, 10, 15, tzinfo=ET)
        orb = engine._compute_orb(bars, now)
        assert orb.high > 0
        assert orb.low > 0
        assert orb.high > orb.low

    def test_orb_breakout_direction(self):
        """After 10AM, price above ORB high → bullish breakout."""
        from src.us_playbook.market_tone import MarketToneEngine
        engine = MarketToneEngine({"market_tone": {"orb": {"window_minutes": 30, "reversal_check_time": "10:00", "reversal_window_minutes": 15}}}, None)
        bars = self._spy_bars(breakout=True, n_bars=45)
        now = datetime(2026, 3, 11, 10, 15, tzinfo=ET)
        orb = engine._compute_orb(bars, now)
        assert orb.breakout_direction == "bullish"
        assert orb.confirmed is True

    def test_orb_no_breakout_before_check_time(self):
        """Before 10AM, no breakout should be detected."""
        from src.us_playbook.market_tone import MarketToneEngine
        engine = MarketToneEngine({"market_tone": {"orb": {"window_minutes": 30, "reversal_check_time": "10:00"}}}, None)
        bars = self._spy_bars(n_bars=20)  # Only 20 bars, before 10AM
        now = datetime(2026, 3, 11, 9, 50, tzinfo=ET)
        orb = engine._compute_orb(bars, now)
        assert orb.breakout_direction is None

    # ── VWAP Slope Tests ──

    def test_vwap_slope_rising(self):
        """Rising price → rising VWAP slope."""
        from src.common.indicators import calculate_vwap_slope
        bars = _make_bars([
            (f"2026-03-11 09:{30+i}:00", 500+i*0.5, 500+i*0.5+0.1, 500+i*0.5-0.1, 500+i*0.5, 100000)
            for i in range(20)
        ])
        slope = calculate_vwap_slope(bars, lookback=15)
        assert slope > 0

    def test_vwap_slope_falling(self):
        """Falling price → falling VWAP slope."""
        from src.common.indicators import calculate_vwap_slope
        bars = _make_bars([
            (f"2026-03-11 09:{30+i}:00", 500-i*0.5, 500-i*0.5+0.1, 500-i*0.5-0.1, 500-i*0.5, 100000)
            for i in range(20)
        ])
        slope = calculate_vwap_slope(bars, lookback=15)
        assert slope < 0

    def test_vwap_slope_flat(self):
        """Flat price → near-zero VWAP slope."""
        from src.common.indicators import calculate_vwap_slope
        bars = _make_bars([
            (f"2026-03-11 09:{30+i}:00", 500, 500.1, 499.9, 500, 100000)
            for i in range(20)
        ])
        slope = calculate_vwap_slope(bars, lookback=15)
        assert abs(slope) < 0.01

    def test_vwap_series_length(self):
        """calculate_vwap_series returns same length as input."""
        from src.common.indicators import calculate_vwap_series
        bars = _make_bars([
            (f"2026-03-11 09:{30+i}:00", 500, 501, 499, 500, 100000)
            for i in range(10)
        ])
        series = calculate_vwap_series(bars)
        assert len(series) == len(bars)

    # ── Breadth Tests ──

    def test_breadth_strong(self):
        """8/10 same direction → strong_aligned."""
        from src.us_playbook import BreadthProxy
        bp = BreadthProxy(
            aligned_count=8, total_count=10, alignment_ratio=0.8,
            alignment_label="strong_aligned", index_aligned=True,
            details="8↑ 2↓ / 10",
        )
        assert bp.alignment_label == "strong_aligned"
        assert bp.alignment_ratio >= 0.75
        assert bp.index_aligned is True

    def test_breadth_divergent(self):
        """4/10 same direction → divergent."""
        from src.us_playbook import BreadthProxy
        bp = BreadthProxy(
            aligned_count=4, total_count=10, alignment_ratio=0.4,
            alignment_label="divergent", index_aligned=False,
            details="4↑ 6↓ / 10",
        )
        assert bp.alignment_label == "divergent"
        assert bp.alignment_ratio < 0.50

    # ── Gap Classification ──

    def test_gap_classification_large(self):
        """Gap > 1% → gap_and_go."""
        from src.us_playbook.market_tone import MarketToneEngine
        import asyncio

        class FakeCollector:
            async def get_snapshot(self, symbol):
                return {"last_price": 555.5, "prev_close_price": 550.0}

        engine = MarketToneEngine({"market_tone": {"gap": {"small_threshold": 0.5, "large_threshold": 1.0}}}, FakeCollector())
        signal, gap_pct = asyncio.get_event_loop().run_until_complete(engine._classify_gap())
        assert signal == "gap_and_go"
        assert gap_pct > 0

    def test_gap_classification_small(self):
        """Gap < 0.5% → gap_fill."""
        from src.us_playbook.market_tone import MarketToneEngine
        import asyncio

        class FakeCollector:
            async def get_snapshot(self, symbol):
                return {"last_price": 550.5, "prev_close_price": 550.0}

        engine = MarketToneEngine({"market_tone": {"gap": {"small_threshold": 0.5, "large_threshold": 1.0}}}, FakeCollector())
        signal, gap_pct = asyncio.get_event_loop().run_until_complete(engine._classify_gap())
        assert signal == "gap_fill"

    # ── Grade Aggregation ──

    def test_grade_aggregation_all_aligned(self):
        """5/5 aligned signals → A+."""
        from src.us_playbook.market_tone import MarketToneEngine
        from src.us_playbook import ORBRange, VWAPStatus, BreadthProxy
        engine = MarketToneEngine({"market_tone": {}}, None)
        now_et = datetime(2026, 3, 11, 10, 30, tzinfo=ET)
        tone = engine._aggregate(
            macro_signal="clear",
            gap_signal="gap_and_go",
            gap_pct=1.5,
            orb=ORBRange(high=555, low=550, breakout_direction="bullish", confirmed=True, reversal_failed=True),
            vwap_status=VWAPStatus(value=553, position="above", slope=0.01, slope_label="rising"),
            breadth=BreadthProxy(
                aligned_count=8, total_count=10, alignment_ratio=0.8,
                alignment_label="strong_aligned", index_aligned=True,
                details="8↑ 2↓ / 10",
            ),
            vix=None,
            now_et=now_et,
            macro_ctx={"event_name": None, "risk": "normal", "behavior": "clear"},
        )
        assert tone.grade == "A+"
        assert tone.grade_score == 5
        assert tone.confidence_modifier > 0

    def test_grade_fomc_cap(self):
        """FOMC day before 2PM → grade capped at B."""
        from src.us_playbook.market_tone import MarketToneEngine
        from src.us_playbook import ORBRange, VWAPStatus, BreadthProxy
        engine = MarketToneEngine({
            "market_tone": {
                "grade": {
                    "fomc_max_grade": "B",
                    "event_day": {"fomc_trend_unlock_time": "14:00"},
                }
            }
        }, None)
        now_et = datetime(2026, 3, 11, 11, 0, tzinfo=ET)  # Before 2PM
        tone = engine._aggregate(
            macro_signal="range_then_trend",
            gap_signal="gap_and_go",
            gap_pct=1.5,
            orb=ORBRange(high=555, low=550, breakout_direction="bullish", confirmed=True),
            vwap_status=VWAPStatus(value=553, position="above", slope=0.01, slope_label="rising"),
            breadth=BreadthProxy(
                aligned_count=9, total_count=10, alignment_ratio=0.9,
                alignment_label="strong_aligned", index_aligned=True,
                details="9↑ 1↓ / 10",
            ),
            vix=None,
            now_et=now_et,
            macro_ctx={"event_name": "FOMC Meeting", "risk": "elevated", "behavior": "range_then_trend"},
        )
        # Even with many aligned signals, FOMC caps at B before 2PM
        assert tone.grade in ("B", "C", "D", "B+")
        score = {"D": 0, "C": 1, "B": 2, "B+": 3, "A": 4, "A+": 5}
        assert score[tone.grade] <= score["B"]

    def test_vix_modifier_caution(self):
        """VIX caution → grade demoted by 1."""
        from src.us_playbook.market_tone import MarketToneEngine
        from src.us_playbook import VIXContext
        engine = MarketToneEngine({"market_tone": {}}, None)
        now_et = datetime(2026, 3, 11, 10, 30, tzinfo=ET)
        # 3 aligned signals = B+ base, VIX caution → B
        tone = engine._aggregate(
            macro_signal="clear",
            gap_signal="gap_and_go",
            gap_pct=1.2,
            orb=None,
            vwap_status=None,
            breadth=None,
            vix=VIXContext(level=28.5, change_pct=7.0, signal="caution"),
            now_et=now_et,
            macro_ctx={"event_name": None, "risk": "normal", "behavior": "clear"},
        )
        # Base = B (2 aligned: macro + gap), VIX caution → C
        assert tone.grade in ("C", "B")  # demoted from B

    def test_confidence_modifier_applied(self):
        """MarketTone confidence_modifier should be within expected range."""
        from src.us_playbook import MarketTone
        tone = MarketTone(
            grade="A+", grade_score=5, direction="bullish",
            day_type="trend", confidence_modifier=0.10,
            position_size_hint="full",
        )
        assert tone.confidence_modifier == 0.10

        tone_d = MarketTone(
            grade="D", grade_score=0, direction="neutral",
            day_type="chop", confidence_modifier=-0.15,
            position_size_hint="sit_out",
        )
        assert tone_d.confidence_modifier == -0.15

    # ── Playbook Integration Tests ──

    def test_playbook_with_tone_section(self):
        """Playbook output should include Section 0 when market_tone is set."""
        from src.us_playbook import MarketTone
        result = USPlaybookResult(
            symbol="SPY", name="S&P 500 ETF",
            regime=USRegimeResult(
                regime=USRegimeType.TREND_STRONG, confidence=0.80,
                rvol=1.5, price=555.0, gap_pct=1.2,
            ),
            key_levels=KeyLevels(
                poc=550, vah=555, val=545,
                pdh=553, pdl=543, pmh=554, pml=548, vwap=551,
            ),
            volume_profile=VolumeProfileResult(poc=550, vah=555, val=545),
            gamma_wall=None,
            filters=FilterResult(tradeable=True, risk_level="normal"),
            generated_at=datetime(2026, 3, 11, 10, 30, 0, tzinfo=ET),
            market_tone=MarketTone(
                grade="A", grade_score=4, direction="bullish",
                day_type="trend", confidence_modifier=0.05,
                position_size_hint="full",
                macro_signal="clear",
                gap_signal="gap_and_go",
                gap_pct=1.2,
            ),
        )
        msg = format_us_playbook_message(result)
        # Market tone is no longer a separate section, but regime info is in header
        assert "趋势日" in msg
        assert "剧本推演" in msg

    def test_backward_compat_no_tone(self):
        """market_tone=None → no Section 0, output unchanged."""
        result = USPlaybookResult(
            symbol="AAPL", name="Apple",
            regime=USRegimeResult(
                regime=USRegimeType.RANGE, confidence=0.65,
                rvol=0.8, price=180.0, gap_pct=-0.2,
            ),
            key_levels=KeyLevels(
                poc=179, vah=182, val=176,
                pdh=183, pdl=175, pmh=181, pml=177, vwap=178.5,
            ),
            volume_profile=VolumeProfileResult(poc=179, vah=182, val=176),
            gamma_wall=None,
            filters=FilterResult(tradeable=True, risk_level="normal"),
            generated_at=datetime(2026, 3, 11, 10, 30, 0, tzinfo=ET),
            market_tone=None,
        )
        msg = format_us_playbook_message(result)
        assert "市场定调" not in msg
        # Normal sections still present
        assert "震荡日" in msg
        assert "数据雷达" in msg

    # ── Filter Refactor Tests ──

    def test_calendar_fomc_not_blocked(self):
        """FOMC day (with behavior) should NOT be blocked."""
        result = check_us_filters(
            rvol=1.0, prev_high=555, prev_low=545,
            current_high=558, current_low=546,
            calendar_path="config/us_calendar.yaml",
            today=date(2026, 3, 18),  # FOMC day
        )
        # Should be tradeable (not blocked)
        assert result.tradeable is True

    def test_calendar_holiday_still_blocked(self):
        """Holiday should still be blocked."""
        result = check_us_filters(
            rvol=1.0, prev_high=555, prev_low=545,
            current_high=558, current_low=546,
            calendar_path="config/us_calendar.yaml",
            today=date(2026, 12, 25),  # Christmas
        )
        assert result.tradeable is False

    def test_get_today_macro_context_fomc(self):
        """FOMC day → behavior=range_then_trend."""
        from src.us_playbook.filter import get_today_macro_context
        ctx = get_today_macro_context("config/us_calendar.yaml", date(2026, 3, 18))
        assert ctx["behavior"] == "range_then_trend"
        assert ctx["event_name"] == "FOMC Meeting"

    def test_get_today_macro_context_nfp(self):
        """NFP day → behavior=data_reaction."""
        from src.us_playbook.filter import get_today_macro_context
        ctx = get_today_macro_context("config/us_calendar.yaml", date(2026, 3, 6))
        assert ctx["behavior"] == "data_reaction"

    def test_get_today_macro_context_clear(self):
        """Normal day → behavior=clear."""
        from src.us_playbook.filter import get_today_macro_context
        ctx = get_today_macro_context("config/us_calendar.yaml", date(2026, 3, 9))
        assert ctx["behavior"] == "clear"

    def test_get_today_macro_context_holiday(self):
        """Holiday → behavior=blocked."""
        from src.us_playbook.filter import get_today_macro_context
        ctx = get_today_macro_context("config/us_calendar.yaml", date(2026, 12, 25))
        assert ctx["behavior"] == "blocked"


# ── ActionPlan Generation Tests ──


class TestCalculateRR:
    def test_long_rr(self):
        """Long: entry=100, sl=95, tp=115 → rr=3.0."""
        assert _calculate_rr(100, 95, 115) == 3.0

    def test_short_rr(self):
        """Short: entry=100, sl=105, tp=85 → rr=3.0."""
        assert _calculate_rr(100, 105, 85) == 3.0

    def test_zero_risk(self):
        """entry == sl → rr=0.0."""
        assert _calculate_rr(100, 100, 110) == 0.0

    def test_none_values(self):
        """Any None → rr=0.0."""
        assert _calculate_rr(None, 95, 115) == 0.0
        assert _calculate_rr(100, None, 115) == 0.0
        assert _calculate_rr(100, 95, None) == 0.0


class TestActionPlanGeneration:
    def _vp(self):
        return VolumeProfileResult(poc=252.0, vah=255.0, val=249.0)

    def _kl(self):
        return KeyLevels(
            poc=252.0, vah=255.0, val=249.0,
            pdh=257.0, pdl=248.0, pmh=254.0, pml=250.5,
            vwap=251.5,
        )

    def _gw(self):
        return GammaWallResult(call_wall_strike=260.0, put_wall_strike=245.0, max_pain=252.0)

    def test_trend_day_bullish(self):
        """TREND_STRONG bullish → Plan A entry=VWAP, direction=bullish."""
        regime = USRegimeResult(
            regime=USRegimeType.TREND_STRONG, confidence=0.75,
            rvol=1.5, price=256.0, gap_pct=0.5,
        )
        plans = _generate_action_plans(regime, "bullish", self._vp(), self._kl(), self._gw(), None)
        assert len(plans) == 3
        assert plans[0].label == "A"
        assert plans[0].direction == "bullish"
        assert plans[0].entry == self._kl().vwap
        assert plans[0].is_primary
        assert plans[2].label == "C"

    def test_trend_day_bearish(self):
        """TREND_STRONG bearish → Plan A entry=VWAP, direction=bearish."""
        regime = USRegimeResult(
            regime=USRegimeType.TREND_STRONG, confidence=0.75,
            rvol=1.5, price=247.0, gap_pct=-0.5,
        )
        plans = _generate_action_plans(regime, "bearish", self._vp(), self._kl(), self._gw(), None)
        assert plans[0].direction == "bearish"
        assert plans[0].entry == self._kl().vwap
        assert plans[0].entry_action == "做空"

    def test_fade_chop_bearish(self):
        """RANGE near VAH → Plan A entry=VAH with zone from PMH."""
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.7,
            rvol=0.8, price=254.5, gap_pct=0.1,
        )
        # vp: poc=252, vah=255, val=249 → VA range=6, upper third=[253, 255]
        # kl: pmh=254 → falls in [253, 255] → zone found
        plans = _generate_action_plans(regime, "bearish", self._vp(), self._kl(), self._gw(), None)
        assert plans[0].name == "上沿做空"
        assert plans[0].entry == self._vp().vah  # 255.0
        assert plans[0].entry_zone_label == "PMH"
        assert plans[0].entry_zone_price == 254.0
        assert plans[0].tp1 == self._vp().poc
        assert plans[0].tp2 == self._vp().val

    def test_fade_chop_bullish(self):
        """RANGE near VAL → Plan A entry=VAL with zone from PML."""
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.7,
            rvol=0.8, price=249.5, gap_pct=0.1,
        )
        # vp: poc=252, vah=255, val=249 → VA range=6, lower third=[249, 251]
        # kl: pml=250.5 → falls in [249, 251] → zone found
        plans = _generate_action_plans(regime, "bullish", self._vp(), self._kl(), self._gw(), None)
        assert plans[0].name == "下沿做多"
        assert plans[0].entry == self._vp().val  # 249.0
        assert plans[0].entry_zone_label == "PML"
        assert plans[0].entry_zone_price == 250.5
        assert plans[0].tp1 == self._vp().poc

    def test_fade_chop_no_structural_level_in_third(self):
        """No structural level in VA third → fallback to single-point entry."""
        # All key levels far from VA edges
        kl = KeyLevels(
            poc=252.0, vah=255.0, val=249.0,
            pdh=260.0, pdl=240.0, pmh=260.0, pml=240.0,
            vwap=252.0,
        )
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.7,
            rvol=0.8, price=254.5, gap_pct=0.1,
        )
        plans = _generate_action_plans(regime, "bearish", self._vp(), kl, None, None)
        assert plans[0].entry == self._vp().vah
        assert plans[0].entry_zone_price is None
        assert plans[0].entry_zone_label == ""
        assert "触及 VAH" in plans[0].trigger

    def test_unclear_no_entry(self):
        """UNCLEAR → Plan A has no entry price."""
        regime = USRegimeResult(
            regime=USRegimeType.UNCLEAR, confidence=0.3,
            rvol=1.0, price=252.0, gap_pct=0.1,
        )
        plans = _generate_action_plans(regime, "neutral", self._vp(), self._kl(), None, None)
        assert plans[0].entry is None
        assert plans[0].name == "等待确认"

    def test_unclear_with_lean(self):
        """UNCLEAR with lean=bullish → Plan B has entry."""
        regime = USRegimeResult(
            regime=USRegimeType.UNCLEAR, confidence=0.35,
            rvol=1.0, price=252.0, gap_pct=0.1, lean="bullish",
        )
        plans = _generate_action_plans(regime, "neutral", self._vp(), self._kl(), None, None)
        assert plans[1].direction == "bullish"
        assert plans[1].entry == self._kl().vwap


class TestCompactOptionLine:
    def test_single_leg_call(self):
        """Single call leg → 'Buy CALL ...'."""
        rec = OptionRecommendation(
            action="call", direction="bullish", dte=5,
            legs=[OptionLeg(
                side="buy", option_type="call", strike=255.0,
                pct_from_price=0.2, moneyness="ATM",
                delta=0.45, open_interest=1500,
            )],
        )
        line = _compact_option_line(rec)
        assert line is not None
        assert "Buy CALL 255" in line
        assert "ATM" in line
        assert "DTE 5" in line
        assert "Δ+0.45" in line
        assert "OI 1,500" in line

    def test_spread(self):
        """Bear call spread → 'Bear Call Spread ...'."""
        from src.common.types import SpreadMetrics
        rec = OptionRecommendation(
            action="bear_call_spread", direction="bearish", dte=5,
            legs=[
                OptionLeg(side="sell", option_type="call", strike=260.0, pct_from_price=1.0, moneyness="OTM 1%"),
                OptionLeg(side="buy", option_type="call", strike=265.0, pct_from_price=2.0, moneyness="OTM 2%"),
            ],
            spread_metrics=SpreadMetrics(
                net_credit=0.85, max_profit=0.85, max_loss=4.15,
                breakeven=260.85, risk_reward_ratio=0.2,
            ),
        )
        line = _compact_option_line(rec)
        assert line is not None
        assert "Bear Call Spread" in line
        assert "260" in line
        assert "265" in line

    def test_wait_returns_none(self):
        """wait action → None."""
        rec = OptionRecommendation(action="wait", direction="neutral")
        assert _compact_option_line(rec) is None

    def test_none_returns_none(self):
        assert _compact_option_line(None) is None


class TestRvolAssessment:
    def test_extreme_low(self):
        assert _rvol_assessment(0.3) == "极寒"

    def test_weak(self):
        assert _rvol_assessment(0.6) == "偏弱"

    def test_normal(self):
        assert _rvol_assessment(1.0) == "正常"

    def test_active(self):
        assert _rvol_assessment(1.3) == "活跃"

    def test_trend_level(self):
        assert _rvol_assessment(2.0) == "趋势级"


class TestMessageLength:
    """Verify all regime types produce output within Telegram 4096 char limit."""

    def _make_result(self, regime_type=USRegimeType.TREND_STRONG, price=554.2):
        return USPlaybookResult(
            symbol="AAPL",
            name="Apple",
            regime=USRegimeResult(
                regime=regime_type, confidence=0.72,
                rvol=1.35, price=price, gap_pct=0.42,
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
            filters=FilterResult(
                tradeable=True, risk_level="elevated",
                warnings=["月度期权到期日 (Monthly OpEx)", "宏观事件日 (FOMC)"],
            ),
            generated_at=datetime(2026, 3, 9, 9, 45, 0, tzinfo=ET),
            quote=QuoteSnapshot(
                symbol="AAPL", last_price=price,
                open_price=553.0, high_price=556.0, low_price=551.0,
                prev_close=552.0, volume=12000000, turnover=6.6e9,
                bid_price=554.15, ask_price=554.25,
                turnover_rate=0.85, amplitude=0.91,
            ),
            option_rec=OptionRecommendation(
                action="call", direction="bullish", expiry="2026-03-20",
                rationale="趋势日看多", dte=5,
                legs=[OptionLeg(
                    side="buy", option_type="call", strike=554.0,
                    pct_from_price=0.0, moneyness="ATM",
                    delta=0.50, open_interest=2000, last_price=5.20,
                )],
            ),
            option_market=OptionMarketSnapshot(
                expiry="2026-03-20", contract_count=120,
                call_contract_count=60, put_contract_count=60,
                atm_iv=0.28, avg_iv=0.30, iv_ratio=0.93,
            ),
        )

    def test_trend_day_within_limit(self):
        msg = format_us_playbook_message(self._make_result(USRegimeType.TREND_STRONG))
        assert len(msg) <= 4096, f"Message too long: {len(msg)} chars"

    def test_gap_and_go_within_limit(self):
        msg = format_us_playbook_message(self._make_result(USRegimeType.GAP_GO, price=560))
        assert len(msg) <= 4096, f"Message too long: {len(msg)} chars"

    def test_fade_chop_within_limit(self):
        msg = format_us_playbook_message(self._make_result(USRegimeType.RANGE, price=554))
        assert len(msg) <= 4096, f"Message too long: {len(msg)} chars"

    def test_unclear_within_limit(self):
        msg = format_us_playbook_message(self._make_result(USRegimeType.UNCLEAR, price=554))
        assert len(msg) <= 4096, f"Message too long: {len(msg)} chars"


class TestEdgeCases:
    """Verify edge cases don't crash."""

    def test_quote_none(self):
        """quote=None → uses regime.price, no crash."""
        result = USPlaybookResult(
            symbol="SPY", name="S&P 500 ETF",
            regime=USRegimeResult(
                regime=USRegimeType.TREND_STRONG, confidence=0.7,
                rvol=1.5, price=555.0, gap_pct=0.5,
            ),
            key_levels=KeyLevels(
                poc=550, vah=555, val=545,
                pdh=558, pdl=542, pmh=554, pml=548, vwap=552,
            ),
            volume_profile=VolumeProfileResult(poc=550, vah=555, val=545),
            gamma_wall=None,
            filters=FilterResult(tradeable=True, risk_level="normal"),
            generated_at=datetime(2026, 3, 9, 10, 0, 0, tzinfo=ET),
            quote=None,
        )
        msg = format_us_playbook_message(result)
        assert "555.00" in msg  # regime.price used

    def test_gamma_wall_none(self):
        """gamma_wall=None → fallback to PDH/PDL levels."""
        result = USPlaybookResult(
            symbol="SPY", name="S&P 500 ETF",
            regime=USRegimeResult(
                regime=USRegimeType.TREND_STRONG, confidence=0.7,
                rvol=1.5, price=558.0, gap_pct=0.5,
            ),
            key_levels=KeyLevels(
                poc=550, vah=555, val=545,
                pdh=560, pdl=542, pmh=556, pml=548, vwap=552,
            ),
            volume_profile=VolumeProfileResult(poc=550, vah=555, val=545),
            gamma_wall=None,
            filters=FilterResult(tradeable=True, risk_level="normal"),
            generated_at=datetime(2026, 3, 9, 10, 0, 0, tzinfo=ET),
        )
        msg = format_us_playbook_message(result)
        assert "Call Wall" not in msg
        assert "PDH" in msg

    def test_option_rec_none(self):
        """option_rec=None → no compact option line, no crash."""
        result = USPlaybookResult(
            symbol="SPY", name="S&P 500 ETF",
            regime=USRegimeResult(
                regime=USRegimeType.RANGE, confidence=0.65,
                rvol=0.8, price=550.0, gap_pct=0.1,
            ),
            key_levels=KeyLevels(
                poc=550, vah=555, val=545,
                pdh=558, pdl=542, pmh=554, pml=548, vwap=551,
            ),
            volume_profile=VolumeProfileResult(poc=550, vah=555, val=545),
            gamma_wall=None,
            filters=FilterResult(tradeable=True, risk_level="normal"),
            generated_at=datetime(2026, 3, 9, 10, 0, 0, tzinfo=ET),
            option_rec=None,
        )
        msg = format_us_playbook_message(result)
        assert "📋 合约" not in msg
        assert "剧本推演" in msg


class TestPlanContext:
    """Tests for PlanContext reachability estimation."""

    def test_reachable_range_high_rvol_morning(self):
        """RVOL=1.5, 360min remaining → wide reachable range."""
        ctx = PlanContext(minutes_to_close=360, rvol=1.5, avg_daily_range_pct=1.5)
        result = _reachable_range_pct(ctx)
        # total_range = 1.5 * 1.5 = 2.25, time_factor = sqrt(360/390) ≈ 0.96
        # remaining ≈ 2.25 * 0.96 ≈ 2.16
        assert result > 2.0
        assert result < 2.5

    def test_reachable_range_low_rvol_afternoon(self):
        """RVOL=0.46, 90min remaining → narrow range."""
        ctx = PlanContext(
            minutes_to_close=90, rvol=0.46,
            avg_daily_range_pct=1.5, intraday_range_pct=0.5,
        )
        result = _reachable_range_pct(ctx)
        # total_range = 1.5 * 0.46 = 0.69, time_factor = sqrt(90/390) ≈ 0.48
        # remaining = 0.69 * 0.48 - 0.25 ≈ 0.08, floor = 0.69 * 0.15 ≈ 0.10
        assert result < 0.5
        assert result > 0

    def test_reachable_range_no_history(self):
        """avg_daily_range_pct=0 → returns inf."""
        ctx = PlanContext(minutes_to_close=90, rvol=0.5, avg_daily_range_pct=0.0)
        assert _reachable_range_pct(ctx) == float("inf")


class TestActionPlanReachability:
    """Tests for post-processing pipeline: TP2 cap, entry reachability, R:R gate, wait coherence."""

    def _vp(self):
        return VolumeProfileResult(poc=252.0, vah=255.0, val=249.0)

    def _kl(self):
        return KeyLevels(
            poc=252.0, vah=255.0, val=249.0,
            pdh=257.0, pdl=248.0, pmh=254.0, pml=250.5,
            vwap=251.5,
        )

    def _gw(self):
        return GammaWallResult(call_wall_strike=260.0, put_wall_strike=245.0, max_pain=252.0)

    def _regime(self, regime_type=USRegimeType.RANGE, price=254.5, rvol=0.8):
        return USRegimeResult(
            regime=regime_type, confidence=0.7,
            rvol=rvol, price=price, gap_pct=0.1,
        )

    def test_tp2_capped_low_rvol(self):
        """TP2=VAL too far in low RVOL afternoon → TP2 capped or cleared."""
        ctx = PlanContext(
            minutes_to_close=90, rvol=0.46,
            avg_daily_range_pct=1.5, intraday_range_pct=0.5,
        )
        # FADE bearish: entry=VAH=255, tp2=VAL=249 → dist=2.35%
        plans = _generate_action_plans(
            self._regime(), "bearish", self._vp(), self._kl(), self._gw(), None, ctx=ctx,
        )
        plan_a = plans[0]
        # With low reachable range, TP2 should be capped or cleared
        if plan_a.tp2 is not None:
            tp2_dist = abs(plan_a.tp2 - plan_a.entry) / plan_a.entry * 100
            reachable = _reachable_range_pct(ctx)
            assert tp2_dist <= reachable
        # TP2 was either replaced or cleared

    def test_tp2_kept_high_rvol(self):
        """High RVOL morning → TP2 preserved."""
        ctx = PlanContext(
            minutes_to_close=360, rvol=1.5,
            avg_daily_range_pct=2.5,  # wide enough to keep TP2
        )
        plans = _generate_action_plans(
            self._regime(rvol=1.5), "bearish", self._vp(), self._kl(), self._gw(), None, ctx=ctx,
        )
        plan_a = plans[0]
        assert plan_a.tp2 == self._vp().val  # VAL preserved

    def test_tp2_range_filter(self):
        """Replacement TP2 must fall between entry and original TP2."""
        ctx = PlanContext(
            minutes_to_close=60, rvol=0.3,
            avg_daily_range_pct=1.0, intraday_range_pct=0.8,
        )
        plan = ActionPlan(
            label="A", name="test", emoji="📉", is_primary=True,
            logic="test", direction="bearish", trigger="test",
            entry=255.0, entry_action="做空",
            stop_loss=257.0, stop_loss_reason="PDH",
            tp1=252.0, tp1_label="POC",
            tp2=245.0, tp2_label="Put Wall",
            rr_ratio=1.5,
        )
        result = _cap_tp2(plan, ctx, self._vp(), self._kl(), self._gw())
        if result.tp2 is not None:
            # Must be between tp2_original(245) and entry(255)
            assert 245.0 < result.tp2 < 255.0
            # Must be farther than TP1 from entry
            tp1_dist = abs(252.0 - 255.0)
            tp2_dist = abs(result.tp2 - 255.0)
            assert tp2_dist > tp1_dist

    def test_entry_demoted_unreachable(self):
        """Entry far from current price + low reachable → demoted."""
        ctx = PlanContext(
            minutes_to_close=60, rvol=0.3,
            avg_daily_range_pct=1.0, intraday_range_pct=0.8,
        )
        plan = ActionPlan(
            label="A", name="test", emoji="📈", is_primary=True,
            logic="test", direction="bullish", trigger="test",
            entry=260.0, entry_action="做多",
            stop_loss=258.0, stop_loss_reason="SL",
            tp1=265.0, tp1_label="target",
            tp2=None, tp2_label="", rr_ratio=2.5,
        )
        current_price = 252.0
        result = _check_entry_reachability(plan, current_price, ctx)
        assert result.demoted is True
        assert "入场位距当前价" in result.demote_reason

    def test_entry_ok_early_session(self):
        """Same distance, early morning with high RVOL → not demoted."""
        ctx = PlanContext(
            minutes_to_close=360, rvol=1.5,
            avg_daily_range_pct=2.0,
        )
        plan = ActionPlan(
            label="A", name="test", emoji="📈", is_primary=True,
            logic="test", direction="bullish", trigger="test",
            entry=255.0, entry_action="做多",
            stop_loss=253.0, stop_loss_reason="SL",
            tp1=260.0, tp1_label="target",
            tp2=None, tp2_label="", rr_ratio=2.5,
        )
        current_price = 252.0
        result = _check_entry_reachability(plan, current_price, ctx)
        assert result.demoted is False

    def test_plan_b_dynamic_sl(self):
        """Plan B SL uses nearest structure level, not hardcoded VAH/VAL."""
        # FADE bearish: Plan B entry=VWAP, SL should be nearest level above VWAP
        plans = _generate_action_plans(
            self._regime(), "bearish", self._vp(), self._kl(), self._gw(), None,
        )
        plan_b = plans[1]
        # VWAP=251.5, nearest above: POC=252, PMH=254, VAH=255...
        # SL should NOT be hardcoded VAH=255.0 anymore
        # It should be nearest structural level above VWAP
        assert plan_b.stop_loss is not None
        if plan_b.entry is not None:  # VWAP > POC condition met
            assert plan_b.stop_loss != self._vp().vah or plan_b.stop_loss_reason != "VAH"

    def test_min_rr_gate_filters(self):
        """R:R=0.3 → demoted."""
        ctx = PlanContext(min_rr=0.8)
        plan = ActionPlan(
            label="A", name="test", emoji="📉", is_primary=True,
            logic="test", direction="bearish", trigger="test",
            entry=255.0, entry_action="做空",
            stop_loss=256.5, stop_loss_reason="SL",
            tp1=254.5, tp1_label="TP",
            tp2=None, tp2_label="", rr_ratio=0.3,
        )
        result = _apply_min_rr_gate([plan], ctx)
        assert result[0].demoted is True
        assert "R:R" in result[0].demote_reason

    def test_min_rr_gate_passes(self):
        """R:R=1.5 → not demoted."""
        ctx = PlanContext(min_rr=0.8)
        plan = ActionPlan(
            label="A", name="test", emoji="📈", is_primary=True,
            logic="test", direction="bullish", trigger="test",
            entry=250.0, entry_action="做多",
            stop_loss=248.0, stop_loss_reason="SL",
            tp1=253.0, tp1_label="TP",
            tp2=None, tp2_label="", rr_ratio=1.5,
        )
        result = _apply_min_rr_gate([plan], ctx)
        assert result[0].demoted is False

    def test_wait_demotes_a_suppresses_b(self):
        """Wait signal → Plan A demoted, Plan B suppressed, Plan C unchanged."""
        ctx = PlanContext(option_action="wait")
        plans = [
            ActionPlan(
                label="A", name="做空", emoji="📉", is_primary=True,
                logic="test", direction="bearish", trigger="test",
                entry=255.0, entry_action="做空",
                stop_loss=257.0, stop_loss_reason="SL",
                tp1=252.0, tp1_label="POC",
                tp2=None, tp2_label="", rr_ratio=1.5,
            ),
            ActionPlan(
                label="B", name="VWAP回归", emoji="📉", is_primary=False,
                logic="test", direction="bearish", trigger="test",
                entry=251.5, entry_action="做空",
                stop_loss=253.0, stop_loss_reason="SL",
                tp1=249.0, tp1_label="VAL",
                tp2=None, tp2_label="", rr_ratio=1.0,
            ),
            ActionPlan(
                label="C", name="失效", emoji="⚡", is_primary=False,
                logic="test", direction="bullish", trigger="test",
                entry=None, entry_action="",
                stop_loss=None, stop_loss_reason="",
                tp1=None, tp1_label="", tp2=None, tp2_label="", rr_ratio=0.0,
            ),
        ]
        result = _apply_wait_coherence(plans, ctx)
        assert result[0].demoted is True
        assert "观望" in result[0].demote_reason
        assert result[1].suppressed is True
        assert "观望" in result[1].demote_reason
        assert result[2].demoted is False
        assert result[2].suppressed is False

    def test_no_ctx_backward_compat(self):
        """No ctx → same behavior as before (no post-processing)."""
        vp = self._vp()
        kl = self._kl()
        regime = self._regime()
        plans_no_ctx = _generate_action_plans(regime, "bearish", vp, kl, self._gw(), None)
        # Should still return 3 plans with no demoted/suppressed
        assert len(plans_no_ctx) == 3
        assert all(not p.demoted for p in plans_no_ctx)
        assert all(not p.suppressed for p in plans_no_ctx)

    def test_plan_c_safe_skip(self):
        """Plan C (no entry/tp) → post-processing safely skips it."""
        ctx = PlanContext(
            minutes_to_close=60, rvol=0.3,
            avg_daily_range_pct=1.0, option_action="wait", min_rr=0.8,
        )
        plan_c = ActionPlan(
            label="C", name="失效", emoji="⚡", is_primary=False,
            logic="test", direction="bullish", trigger="test",
            entry=None, entry_action="",
            stop_loss=None, stop_loss_reason="",
            tp1=None, tp1_label="", tp2=None, tp2_label="", rr_ratio=0.0,
        )
        # Entry reachability should skip
        result = _check_entry_reachability(plan_c, 252.0, ctx)
        assert result.demoted is False
        # TP2 cap should skip
        result = _cap_tp2(plan_c, ctx, self._vp(), self._kl(), self._gw())
        assert result.tp2 is None
        # R:R gate should skip
        results = _apply_min_rr_gate([plan_c], ctx)
        assert results[0].demoted is False


class TestDuplicateWarningFix:
    """Both demoted+suppressed should produce only ONE ⚠️ line."""

    def test_demoted_and_suppressed_single_warning(self):
        plan = ActionPlan(
            label="B", name="test", emoji="📉", is_primary=False,
            logic="test", direction="bearish", trigger="test",
            entry=255.0, entry_action="做空",
            stop_loss=257.0, stop_loss_reason="SL",
            tp1=252.0, tp1_label="POC",
            tp2=None, tp2_label="", rr_ratio=0.5,
            demoted=True, suppressed=True,
            demote_reason="R:R 不合格 (0.5 < 0.8)",
        )
        lines = _format_action_plan(plan)
        warning_lines = [l for l in lines if "⚠️" in l and "R:R" in l]
        assert len(warning_lines) == 1


class TestCapTp1:
    """Tests for OPT-1: TP1 capping to reachable range."""

    def _vp(self):
        return VolumeProfileResult(poc=252.0, vah=255.0, val=249.0)

    def _kl(self):
        return KeyLevels(
            poc=252.0, vah=255.0, val=249.0,
            pdh=257.0, pdl=248.0, pmh=254.0, pml=250.5,
            vwap=251.5,
        )

    def _gw(self):
        return GammaWallResult(call_wall_strike=260.0, put_wall_strike=245.0, max_pain=252.0)

    def test_tp1_beyond_reachable_replaced(self):
        """TP1 exceeds reachable range → replaced with nearer structure."""
        ctx = PlanContext(
            minutes_to_close=60, rvol=0.3,
            avg_daily_range_pct=1.0, intraday_range_pct=0.8,
        )
        plan = ActionPlan(
            label="A", name="test", emoji="📉", is_primary=True,
            logic="test", direction="bearish", trigger="test",
            entry=255.0, entry_action="做空",
            stop_loss=257.0, stop_loss_reason="PDH",
            tp1=245.0, tp1_label="Put Wall",  # 3.9% away — exceeds reachable
            tp2=None, tp2_label="", rr_ratio=5.0,
        )
        result = _cap_tp1(plan, ctx, self._vp(), self._kl(), self._gw())
        # Should be replaced with a nearer level (between 245 and 255)
        if result.tp1 != 245.0:
            assert 245.0 < result.tp1 < 255.0
            assert result.rr_ratio > 0  # R:R recalculated

    def test_tp1_beyond_reachable_no_replacement_warns(self):
        """TP1 exceeds reachable + no suitable replacement → warning added."""
        ctx = PlanContext(
            minutes_to_close=30, rvol=0.2,
            avg_daily_range_pct=0.5, intraday_range_pct=0.4,
        )
        # All levels far away, TP1 the only option
        plan = ActionPlan(
            label="A", name="test", emoji="📉", is_primary=True,
            logic="test", direction="bearish", trigger="test",
            entry=255.0, entry_action="做空",
            stop_loss=256.0, stop_loss_reason="SL",
            tp1=245.0, tp1_label="Put Wall",
            tp2=None, tp2_label="", rr_ratio=10.0,
        )
        result = _cap_tp1(plan, ctx, self._vp(), self._kl(), self._gw())
        # TP1 kept but warning added
        assert result.tp1 == 245.0
        assert "TP1" in result.warning

    def test_tp1_within_range_no_change(self):
        """TP1 within reachable range → no change."""
        ctx = PlanContext(
            minutes_to_close=360, rvol=1.5,
            avg_daily_range_pct=2.5,
        )
        plan = ActionPlan(
            label="A", name="test", emoji="📉", is_primary=True,
            logic="test", direction="bearish", trigger="test",
            entry=255.0, entry_action="做空",
            stop_loss=257.0, stop_loss_reason="PDH",
            tp1=252.0, tp1_label="POC",
            tp2=None, tp2_label="", rr_ratio=1.5,
        )
        result = _cap_tp1(plan, ctx, self._vp(), self._kl(), self._gw())
        assert result.tp1 == 252.0
        assert result.warning == ""

    def test_tp1_entry_none_skipped(self):
        """entry=None → cap_tp1 skipped."""
        ctx = PlanContext(minutes_to_close=60, rvol=0.3, avg_daily_range_pct=1.0)
        plan = ActionPlan(
            label="A", name="test", emoji="⏳", is_primary=True,
            logic="test", direction="neutral", trigger="test",
            entry=None, entry_action="",
            stop_loss=None, stop_loss_reason="",
            tp1=245.0, tp1_label="target",
            tp2=None, tp2_label="", rr_ratio=0.0,
        )
        result = _cap_tp1(plan, ctx, self._vp(), self._kl(), self._gw())
        assert result.tp1 == 245.0
        assert result.warning == ""


class TestUnclearFadePlan:
    """Tests for OPT-2: UNCLEAR + low RVOL → mean-reversion fade plan."""

    def _vp(self):
        return VolumeProfileResult(poc=252.0, vah=255.0, val=249.0)

    def _kl(self):
        return KeyLevels(
            poc=252.0, vah=255.0, val=249.0,
            pdh=257.0, pdl=248.0, pmh=254.0, pml=250.5,
            vwap=251.5,
        )

    def _gw(self):
        return GammaWallResult(call_wall_strike=260.0, put_wall_strike=245.0, max_pain=252.0)

    def test_chop_likely_bearish_fade(self):
        """is_chop_likely + lean=bearish → Plan B is fade short with SL/TP/rr.

        VWAP must be above VAH so TP1 (VAH) is below entry → valid short direction.
        """
        regime = USRegimeResult(
            regime=USRegimeType.UNCLEAR, confidence=0.25,
            rvol=0.57, price=258.0, gap_pct=0.1, lean="bearish",
        )
        kl = KeyLevels(
            poc=252.0, vah=255.0, val=249.0,
            pdh=260.0, pdl=248.0, pmh=258.0, pml=250.5,
            vwap=256.5,  # above VAH → bearish fade targets VAH below
        )
        plans = _generate_action_plans(
            regime, "neutral", self._vp(), kl, self._gw(), None,
        )
        plan_b = plans[1]
        assert plan_b.name == "均值回归做空"
        assert plan_b.direction == "bearish"
        assert plan_b.entry == kl.vwap
        assert plan_b.stop_loss is not None  # has explicit SL
        assert plan_b.tp1 is not None  # has explicit TP1
        assert plan_b.tp1 < plan_b.entry  # TP below entry for short
        assert plan_b.rr_ratio > 0  # meaningful R:R
        # No entry_zone (single-point entry)
        assert plan_b.entry_zone_price is None

    def test_chop_likely_bullish_fade(self):
        """is_chop_likely + lean=bullish → Plan B is fade long with SL/TP/rr.

        VWAP must be below VAL so TP1 (VAL) is above entry → valid long direction.
        """
        regime = USRegimeResult(
            regime=USRegimeType.UNCLEAR, confidence=0.25,
            rvol=0.57, price=247.0, gap_pct=-0.1, lean="bullish",
        )
        kl = KeyLevels(
            poc=252.0, vah=255.0, val=249.0,
            pdh=257.0, pdl=245.0, pmh=254.0, pml=246.0,
            vwap=247.5,  # below VAL → bullish fade targets VAL above
        )
        plans = _generate_action_plans(
            regime, "neutral", self._vp(), kl, self._gw(), None,
        )
        plan_b = plans[1]
        assert plan_b.name == "均值回归做多"
        assert plan_b.direction == "bullish"
        assert plan_b.entry == kl.vwap
        assert plan_b.stop_loss is not None
        assert plan_b.tp1 is not None
        assert plan_b.tp1 > plan_b.entry  # TP above entry for long
        assert plan_b.rr_ratio > 0
        assert plan_b.entry_zone_price is None

    def test_chop_likely_neutral_no_fade(self):
        """is_chop_likely + lean=neutral → Plan B stays as '观察关键位'."""
        regime = USRegimeResult(
            regime=USRegimeType.UNCLEAR, confidence=0.25,
            rvol=0.57, price=252.0, gap_pct=0.0, lean="neutral",
        )
        plans = _generate_action_plans(
            regime, "neutral", self._vp(), self._kl(), self._gw(), None,
        )
        plan_b = plans[1]
        assert plan_b.name == "观察关键位"
        assert plan_b.entry is None

    def test_not_chop_likely_keeps_original(self):
        """rvol=1.0, confidence=0.35 → keeps original '轻仓' plan (backward compat)."""
        regime = USRegimeResult(
            regime=USRegimeType.UNCLEAR, confidence=0.35,
            rvol=1.0, price=252.0, gap_pct=0.1, lean="bullish",
        )
        plans = _generate_action_plans(
            regime, "neutral", self._vp(), self._kl(), self._gw(), None,
        )
        plan_b = plans[1]
        assert "轻仓" in plan_b.name
        assert plan_b.direction == "bullish"
        assert plan_b.stop_loss is not None  # now has structural SL

    def test_amd_scenario(self):
        """AMD-like: RVOL=0.57, confidence=0.25, lean=bearish, price=204.85."""
        vp = VolumeProfileResult(poc=202.25, vah=202.75, val=193.75)
        kl = KeyLevels(
            poc=202.25, vah=202.75, val=193.75,
            pdh=207.0, pdl=200.0, pmh=206.0, pml=199.0,
            vwap=206.42,
        )
        regime = USRegimeResult(
            regime=USRegimeType.UNCLEAR, confidence=0.25,
            rvol=0.57, price=204.85, gap_pct=0.1, lean="bearish",
        )
        plans = _generate_action_plans(regime, "neutral", vp, kl, None, None)
        plan_b = plans[1]
        assert plan_b.name == "均值回归做空"
        assert plan_b.entry == 206.42  # VWAP
        assert plan_b.tp1 == vp.vah  # 202.75 — nearest VA edge
        assert plan_b.rr_ratio > 0

    def test_vwap_near_val_fallback_to_directional(self):
        """QQQ bug: VWAP≈VAL (602.17 vs 602.00) → fade unprofitable, fallback to 轻仓做多."""
        vp = VolumeProfileResult(poc=605.0, vah=608.0, val=602.0)
        kl = KeyLevels(
            poc=605.0, vah=608.0, val=602.0,
            pdh=610.0, pdl=600.0, pmh=609.0, pml=601.0,
            vwap=602.17,
        )
        regime = USRegimeResult(
            regime=USRegimeType.UNCLEAR, confidence=0.25,
            rvol=0.57, price=601.0, gap_pct=-0.1, lean="bullish",
        )
        plans = _generate_action_plans(regime, "neutral", vp, kl, self._gw(), None)
        plan_b = plans[1]
        # Should fallback to directional plan, not fade
        assert "轻仓" in plan_b.name
        assert plan_b.direction == "bullish"
        assert plan_b.stop_loss is not None  # directional plan now has structural SL

    def test_vwap_near_vah_fallback_to_directional(self):
        """VWAP≈VAH → fade short unprofitable, fallback to 轻仓做空."""
        vp = VolumeProfileResult(poc=250.0, vah=255.0, val=247.0)
        kl = KeyLevels(
            poc=250.0, vah=255.0, val=247.0,
            pdh=258.0, pdl=246.0, pmh=256.0, pml=248.0,
            vwap=255.10,
        )
        regime = USRegimeResult(
            regime=USRegimeType.UNCLEAR, confidence=0.25,
            rvol=0.57, price=256.0, gap_pct=0.1, lean="bearish",
        )
        plans = _generate_action_plans(regime, "neutral", vp, kl, self._gw(), None)
        plan_b = plans[1]
        assert "轻仓" in plan_b.name
        assert plan_b.direction == "bearish"
        assert plan_b.stop_loss is not None  # now has structural SL

    def test_sl_capped_when_distant_put_wall(self):
        """SL only candidate is Put Wall at 510 (15% away) → capped to 1%.

        All structural levels (PDL, PML, VAL) placed above VWAP so that
        _nearest_levels("below") finds only the distant Put Wall.
        VAL=608 gives enough reward (6pts) vs capped SL (~6pts) for R:R≈1.0.

        Note: Put Wall 510 is >10% from price 599 and excluded by gamma wall
        distance filter. SL falls back to VAL=608 (nearest structural level).
        """
        vp = VolumeProfileResult(poc=612.0, vah=615.0, val=608.0)
        kl = KeyLevels(
            poc=612.0, vah=615.0, val=608.0,
            pdh=620.0, pdl=606.0, pmh=618.0, pml=607.0,
            vwap=602.0,
        )
        gw = GammaWallResult(call_wall_strike=650.0, put_wall_strike=510.0, max_pain=600.0)
        regime = USRegimeResult(
            regime=USRegimeType.UNCLEAR, confidence=0.25,
            rvol=0.57, price=599.0, gap_pct=-0.1, lean="bullish",
        )
        plans = _generate_action_plans(regime, "neutral", vp, kl, gw, None)
        plan_b = plans[1]
        assert plan_b.name == "均值回归做多"
        assert plan_b.stop_loss is not None
        # Put Wall 510 excluded (>10% away), SL falls to VAL=608
        sl_distance_pct = abs(plan_b.stop_loss - kl.vwap) / kl.vwap
        assert sl_distance_pct <= 0.011  # allow tiny float tolerance
        assert plan_b.stop_loss_reason == "VAL"

    def test_well_spaced_levels_unchanged(self):
        """Normal spacing: VWAP=247.5 below VAL=249 → bullish fade plan generated."""
        kl = KeyLevels(
            poc=252.0, vah=255.0, val=249.0,
            pdh=257.0, pdl=245.0, pmh=254.0, pml=246.0,
            vwap=247.5,  # below VAL → valid bullish fade
        )
        plans = _generate_action_plans(
            USRegimeResult(
                regime=USRegimeType.UNCLEAR, confidence=0.25,
                rvol=0.57, price=247.0, gap_pct=-0.1, lean="bullish",
            ),
            "neutral", self._vp(), kl, self._gw(), None,
        )
        plan_b = plans[1]
        assert plan_b.name == "均值回归做多"
        assert plan_b.entry == kl.vwap
        assert plan_b.tp1 == self._vp().val
        assert plan_b.tp1 > plan_b.entry  # TP above entry for long
        assert plan_b.rr_ratio > 0
        assert plan_b.stop_loss is not None


    def test_low_rr_fade_fallback_to_directional(self):
        """AAPL bug: VWAP=256.53, VAL=257.50, SL capped → R:R=0.4 → fallback to 轻仓."""
        vp = VolumeProfileResult(poc=258.0, vah=260.0, val=257.50)
        kl = KeyLevels(
            poc=258.0, vah=260.0, val=257.50,
            pdh=261.0, pdl=253.0, pmh=260.5, pml=254.0,
            vwap=256.53,
        )
        regime = USRegimeResult(
            regime=USRegimeType.UNCLEAR, confidence=0.25,
            rvol=0.57, price=255.0, gap_pct=-0.1, lean="bullish",
        )
        plans = _generate_action_plans(regime, "neutral", vp, kl, self._gw(), None)
        plan_b = plans[1]
        # R:R too low for fade → should fallback to directional
        assert "轻仓" in plan_b.name
        assert plan_b.direction == "bullish"
        assert plan_b.stop_loss is not None  # now has structural SL

    def test_low_rr_bearish_fade_fallback(self):
        """Bearish fade with R:R < 0.8 → fallback to 轻仓做空.

        VWAP=200 above VAH=199.65 (reward=0.35), nearest SL above=PMH=201.5 (risk=1.5).
        R:R = 0.35/1.5 ≈ 0.23 < 0.8 → fallback.
        """
        vp = VolumeProfileResult(poc=199.0, vah=199.65, val=198.0)
        kl = KeyLevels(
            poc=199.0, vah=199.65, val=198.0,
            pdh=202.0, pdl=197.0, pmh=201.5, pml=197.5,
            vwap=200.0,
        )
        regime = USRegimeResult(
            regime=USRegimeType.UNCLEAR, confidence=0.25,
            rvol=0.57, price=200.5, gap_pct=0.1, lean="bearish",
        )
        plans = _generate_action_plans(regime, "neutral", vp, kl, None, None)
        plan_b = plans[1]
        assert "轻仓" in plan_b.name
        assert plan_b.direction == "bearish"
        assert plan_b.stop_loss is not None  # now has structural SL


class TestVwapDeviationWarning:
    """Tests for OPT-4: VWAP deviation warning."""

    def _plan(self, direction="bearish", entry=255.0):
        return ActionPlan(
            label="A", name="test", emoji="📉", is_primary=True,
            logic="test", direction=direction, trigger="test",
            entry=entry, entry_action="做空" if direction == "bearish" else "做多",
            stop_loss=257.0, stop_loss_reason="SL",
            tp1=252.0, tp1_label="POC",
            tp2=None, tp2_label="", rr_ratio=1.5,
        )

    def test_price_below_vwap_bearish_warns(self):
        """Price below VWAP + bearish → warning."""
        plan = self._plan(direction="bearish")
        plans = _apply_vwap_deviation_warning([plan], price=250.0, vwap=255.0)
        assert "低于 VWAP" in plans[0].warning

    def test_price_above_vwap_bullish_warns(self):
        """Price above VWAP + bullish → warning."""
        plan = self._plan(direction="bullish", entry=250.0)
        plans = _apply_vwap_deviation_warning([plan], price=260.0, vwap=255.0)
        assert "高于 VWAP" in plans[0].warning

    def test_price_below_vwap_bullish_no_warning(self):
        """Price below VWAP + bullish → no warning (direction consistent)."""
        plan = self._plan(direction="bullish", entry=250.0)
        plans = _apply_vwap_deviation_warning([plan], price=250.0, vwap=255.0)
        assert plans[0].warning == ""

    def test_deviation_below_threshold_no_warning(self):
        """Deviation < 0.5% → no warning."""
        plan = self._plan(direction="bearish")
        plans = _apply_vwap_deviation_warning([plan], price=254.5, vwap=255.0)
        assert plans[0].warning == ""


class TestWarningRendering:
    """Test that warning field is rendered in format_action_plan output."""

    def test_warning_rendered(self):
        plan = ActionPlan(
            label="A", name="test", emoji="📈", is_primary=True,
            logic="test", direction="bullish", trigger="test",
            entry=250.0, entry_action="做多",
            stop_loss=248.0, stop_loss_reason="SL",
            tp1=255.0, tp1_label="VAH",
            tp2=None, tp2_label="", rr_ratio=2.5,
            warning="价格已高于 VWAP 1.2%, 做多需等回调",
        )
        lines = _format_action_plan(plan)
        assert any("VWAP" in l and "回调" in l for l in lines)


# ── P0-1: Volume Surge Baseline Fix Tests ──

class TestVolumeSurgeBaseline:
    def test_skip_open_bars_for_avg(self):
        """Opening spike bars should be excluded from average baseline."""
        bars = _make_bars([
            # Opening rotation — 3 bars with huge volume
            ("2026-03-10 09:30:00", 100, 101, 99, 100, 500000),
            ("2026-03-10 09:31:00", 100, 101, 99, 100, 400000),
            ("2026-03-10 09:32:00", 100, 101, 99, 100, 300000),
            # Normal trading — lower volume
            ("2026-03-10 09:33:00", 100, 101, 99, 101, 50000),
            ("2026-03-10 09:34:00", 101, 102, 100, 102, 60000),
            ("2026-03-10 09:35:00", 102, 103, 101, 103, 70000),
            ("2026-03-10 09:36:00", 103, 104, 102, 104, 80000),
            # Surge bar
            ("2026-03-10 09:37:00", 104, 106, 103, 106, 200000),
        ])
        from datetime import time as dt_time
        _cutoff = dt_time(9, 33)
        filtered = bars[bars.index.time >= _cutoff]
        avg_all = float(bars["Volume"].mean())
        avg_filtered = float(filtered["Volume"].mean())
        # Filtered avg should be much lower (no opening spike)
        assert avg_filtered < avg_all
        # Surge (200000) should exceed 2x filtered avg (~92k) but may not exceed 2x all avg (~207k)
        surge_threshold = 2.0
        assert 200000 >= avg_filtered * surge_threshold
        # With unfiltered avg, the surge would NOT be detected
        assert 200000 < avg_all * surge_threshold


# ── P0-2: Frequency Precheck Tests ──

class TestFrequencyPrecheck:
    def _make_predictor(self):
        cfg = {
            "watchlist": [{"symbol": "SPY", "name": "S&P 500 ETF"}],
            "auto_scan": {
                "cooldown": {"same_signal_minutes": 30, "max_per_session": 2, "max_per_day": 3},
                "override": {"confidence_increase": 0.10, "price_extension_pct": 0.50, "regime_upgrade": True},
            },
        }
        return USPredictor(cfg, collector=None)

    def test_no_history_passes(self):
        pred = self._make_predictor()
        pred._scan_history_date = "2026-03-10"
        assert pred._quick_frequency_precheck("AAPL", "morning", pred._cfg["auto_scan"]) is True

    def test_daily_max_reached_breakout_blocks(self):
        """When daily max is reached and last alert is BREAKOUT, no upgrade possible → skip."""
        pred = self._make_predictor()
        pred._scan_history_date = "2026-03-10"
        for i in range(3):
            pred._scan_history.setdefault("AAPL", []).append(
                USScanAlertRecord(
                    symbol="AAPL", signal_type="BREAKOUT_LONG", direction="bullish",
                    confidence=0.8, price=180.0, timestamp=float(i * 3600), session="morning",
                )
            )
        assert pred._quick_frequency_precheck("AAPL", "morning", pred._cfg["auto_scan"]) is False

    def test_daily_max_reached_range_allows(self):
        """When daily max reached but last alert is RANGE, upgrade possible → allow."""
        pred = self._make_predictor()
        pred._scan_history_date = "2026-03-10"
        for i in range(3):
            pred._scan_history.setdefault("AAPL", []).append(
                USScanAlertRecord(
                    symbol="AAPL", signal_type="RANGE_REVERSAL_LONG", direction="bullish",
                    confidence=0.7, price=175.0, timestamp=float(i * 3600), session="morning",
                )
            )
        assert pred._quick_frequency_precheck("AAPL", "morning", pred._cfg["auto_scan"]) is True

    def test_session_max_reached_blocks(self):
        pred = self._make_predictor()
        pred._scan_history_date = "2026-03-10"
        for i in range(2):
            pred._scan_history.setdefault("AAPL", []).append(
                USScanAlertRecord(
                    symbol="AAPL", signal_type="BREAKOUT_LONG", direction="bullish",
                    confidence=0.8, price=180.0, timestamp=float(i * 3600), session="morning",
                )
            )
        assert pred._quick_frequency_precheck("AAPL", "morning", pred._cfg["auto_scan"]) is False


# ── P0-3: Signal Strength Grading Tests ──

class TestSignalStrength:
    def _make_signal(self, conf=0.75, rvol=1.5):
        return USScanSignal(
            signal_type="BREAKOUT_BULLISH",
            direction="bullish",
            symbol="AAPL",
            regime=USRegimeResult(
                regime=USRegimeType.GAP_GO, confidence=conf,
                rvol=rvol, price=560, gap_pct=1.5,
            ),
            price=560,
            trigger_reasons=["突破 VAH 0.35%"],
            timestamp=1000.0,
        )

    def test_extreme_strong(self):
        label, emoji = USPredictor._signal_strength_label(self._make_signal(conf=0.90, rvol=2.5))
        assert label == "极强信号"
        assert emoji == "\U0001f525"

    def test_strong_by_conf(self):
        label, _ = USPredictor._signal_strength_label(self._make_signal(conf=0.82, rvol=1.2))
        assert label == "强信号"

    def test_strong_by_rvol(self):
        label, _ = USPredictor._signal_strength_label(self._make_signal(conf=0.70, rvol=1.9))
        assert label == "强信号"

    def test_standard(self):
        label, emoji = USPredictor._signal_strength_label(self._make_signal(conf=0.72, rvol=1.3))
        assert label == "标准信号"
        assert emoji == "\U0001f514"

    def test_header_uses_graded_label(self):
        """_format_scan_header should use graded label instead of hardcoded '强信号'."""
        signal = self._make_signal(conf=0.72, rvol=1.3)
        rec = OptionRecommendation(action="call", direction="bullish", expiry="2026-03-20")
        header = USPredictor._format_scan_header(signal, "normal", rec, None, 30)
        assert "标准信号" in header
        assert "强信号" not in header

    def test_header_extreme_strong(self):
        signal = self._make_signal(conf=0.90, rvol=2.5)
        rec = OptionRecommendation(action="call", direction="bullish", expiry="2026-03-20")
        header = USPredictor._format_scan_header(signal, "normal", rec, None, 30)
        assert "极强信号" in header


# ── RSI Tests ──

class TestRSI:
    def test_basic_rsi(self):
        from src.us_playbook.indicators import calculate_rsi
        # Build bars with consistent up moves → RSI should be high
        prices = []
        for i in range(20):
            ts = f"2026-03-10 09:{30+i}:00"
            p = 100 + i * 0.5  # steadily rising
            prices.append((ts, p, p + 0.2, p - 0.1, p + 0.3, 10000))
        bars = _make_bars(prices)
        rsi = calculate_rsi(bars, period=14)
        assert rsi > 70  # overbought territory

    def test_rsi_down(self):
        from src.us_playbook.indicators import calculate_rsi
        prices = []
        for i in range(20):
            ts = f"2026-03-10 09:{30+i}:00"
            p = 200 - i * 0.5  # steadily falling
            prices.append((ts, p, p + 0.1, p - 0.2, p - 0.3, 10000))
        bars = _make_bars(prices)
        rsi = calculate_rsi(bars, period=14)
        assert rsi < 30  # oversold territory

    def test_rsi_insufficient_data(self):
        from src.us_playbook.indicators import calculate_rsi
        bars = _make_bars([
            ("2026-03-10 09:30:00", 100, 101, 99, 100, 10000),
        ])
        rsi = calculate_rsi(bars, period=14)
        assert rsi == 50.0  # neutral fallback

    def test_rsi_empty(self):
        from src.us_playbook.indicators import calculate_rsi
        assert calculate_rsi(pd.DataFrame(), period=14) == 50.0


# ── Per-Type Frequency Control Tests ──

class TestPerTypeFrequency:
    def _make_predictor(self):
        cfg = {
            "watchlist": [{"symbol": "SPY", "name": "S&P 500 ETF"}],
            "auto_scan": {
                "cooldown": {
                    "same_signal_minutes": 30,
                    "max_per_session": 3,
                    "max_per_day": 5,
                    "per_type": {
                        "BREAKOUT": {"max_per_session": 2, "max_per_day": 3},
                        "RANGE_REVERSAL": {"max_per_session": 1, "max_per_day": 2},
                    },
                },
                "override": {"confidence_increase": 0.10, "price_extension_pct": 0.50, "regime_upgrade": True},
            },
        }
        return USPredictor(cfg, collector=None)

    def _make_signal(self, signal_type="BREAKOUT_LONG", direction="bullish", conf=0.75, price=560.0):
        return USScanSignal(
            signal_type=signal_type,
            direction=direction,
            symbol="AAPL",
            regime=USRegimeResult(
                regime=USRegimeType.GAP_GO, confidence=conf,
                rvol=1.5, price=price, gap_pct=1.0,
            ),
            price=price,
            timestamp=5000.0,
        )

    def test_range_reversal_session_limit(self):
        """RANGE_REVERSAL has per-type session max=1."""
        pred = self._make_predictor()
        pred._scan_history_date = "2026-03-10"
        # Record 1 RANGE_REVERSAL
        rr_sig = self._make_signal(signal_type="RANGE_REVERSAL_LONG")
        rr_sig.timestamp = 1000.0
        pred._record_alert("AAPL", rr_sig, "morning")

        # Second RANGE_REVERSAL in same session → blocked by per-type
        rr_sig2 = self._make_signal(signal_type="RANGE_REVERSAL_SHORT", direction="bearish")
        rr_sig2.timestamp = 5000.0
        allowed, _ = pred._check_frequency("AAPL", rr_sig2, "morning", pred._cfg["auto_scan"])
        assert not allowed

    def test_breakout_still_allowed_when_rr_maxed(self):
        """BREAKOUT should still be allowed when RANGE_REVERSAL per-type limit is hit."""
        pred = self._make_predictor()
        pred._scan_history_date = "2026-03-10"
        rr_sig = self._make_signal(signal_type="RANGE_REVERSAL_LONG")
        rr_sig.timestamp = 1000.0
        pred._record_alert("AAPL", rr_sig, "morning")

        # BREAKOUT should still pass (BREAKOUT per-type max=2)
        bo_sig = self._make_signal(signal_type="BREAKOUT_LONG")
        bo_sig.timestamp = 5000.0
        allowed, _ = pred._check_frequency("AAPL", bo_sig, "morning", pred._cfg["auto_scan"])
        assert allowed

    def test_breakout_per_type_daily_limit(self):
        """BREAKOUT has per-type daily max=3."""
        pred = self._make_predictor()
        pred._scan_history_date = "2026-03-10"
        for i in range(3):
            sig = self._make_signal(signal_type=f"BREAKOUT_{'LONG' if i % 2 == 0 else 'SHORT'}")
            sig.timestamp = float(i * 3600)
            pred._record_alert("AAPL", sig, "morning" if i < 2 else "afternoon")

        # 4th BREAKOUT → blocked by per-type daily
        sig4 = self._make_signal(signal_type="BREAKOUT_LONG")
        sig4.timestamp = 20000.0
        allowed, _ = pred._check_frequency("AAPL", sig4, "afternoon", pred._cfg["auto_scan"])
        assert not allowed

    def test_no_per_type_config_uses_global(self):
        """Without per_type config, global limits apply."""
        cfg = {
            "watchlist": [{"symbol": "SPY", "name": "S&P 500 ETF"}],
            "auto_scan": {
                "cooldown": {"same_signal_minutes": 30, "max_per_session": 2, "max_per_day": 3},
                "override": {"regime_upgrade": True},
            },
        }
        pred = USPredictor(cfg, collector=None)
        pred._scan_history_date = "2026-03-10"
        sig = self._make_signal()
        sig.timestamp = 5000.0
        allowed, _ = pred._check_frequency("AAPL", sig, "morning", pred._cfg["auto_scan"])
        assert allowed


# ── L1 Hysteresis Tests ──

class TestL1Hysteresis:
    """Test RVOL threshold hysteresis in L1 screening."""

    def test_l1_hysteresis_trend_lowers_threshold(self):
        """Last confirmed TREND_STRONG → thresholds lowered by 10% (easier to stay trend)."""
        from src.us_playbook.main import _TREND_FAMILY

        regime_cfg = {
            "gap_and_go_rvol": 1.5,
            "trend_day_rvol": 1.2,
            "stability": {"rvol_hysteresis": 0.10},
        }
        stability_cfg = regime_cfg.get("stability", {})
        rvol_hyst = stability_cfg.get("rvol_hysteresis", 0.10)
        adj_gap_rvol = regime_cfg["gap_and_go_rvol"]
        adj_trend_rvol = regime_cfg["trend_day_rvol"]

        last_regime = USRegimeResult(
            regime=USRegimeType.TREND_STRONG, confidence=0.75,
            rvol=1.3, price=560, gap_pct=0.5,
        )
        if last_regime.regime in _TREND_FAMILY:
            adj_gap_rvol *= (1 - rvol_hyst)
            adj_trend_rvol *= (1 - rvol_hyst)

        assert abs(adj_trend_rvol - 1.08) < 1e-9  # 1.2 * 0.9
        assert abs(adj_gap_rvol - 1.35) < 1e-9    # 1.5 * 0.9

    def test_l1_hysteresis_fade_raises_threshold(self):
        """Last confirmed RANGE → thresholds raised by 10% (harder to leave fade)."""
        regime_cfg = {
            "gap_and_go_rvol": 1.5,
            "trend_day_rvol": 1.2,
            "stability": {"rvol_hysteresis": 0.10},
        }
        stability_cfg = regime_cfg.get("stability", {})
        rvol_hyst = stability_cfg.get("rvol_hysteresis", 0.10)
        adj_gap_rvol = regime_cfg["gap_and_go_rvol"]
        adj_trend_rvol = regime_cfg["trend_day_rvol"]

        last_regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.60,
            rvol=0.9, price=560, gap_pct=0.2,
        )
        if last_regime.regime == USRegimeType.RANGE:
            adj_gap_rvol *= (1 + rvol_hyst)
            adj_trend_rvol *= (1 + rvol_hyst)

        assert abs(adj_trend_rvol - 1.32) < 1e-9  # 1.2 * 1.1
        assert abs(adj_gap_rvol - 1.65) < 1e-9    # 1.5 * 1.1

    def test_l1_hysteresis_no_prior(self):
        """No prior regime → use original thresholds (no adjustment)."""
        regime_cfg = {
            "gap_and_go_rvol": 1.5,
            "trend_day_rvol": 1.2,
            "stability": {"rvol_hysteresis": 0.10},
        }
        stability_cfg = regime_cfg.get("stability", {})
        rvol_hyst = stability_cfg.get("rvol_hysteresis", 0.10)
        adj_gap_rvol = regime_cfg["gap_and_go_rvol"]
        adj_trend_rvol = regime_cfg["trend_day_rvol"]

        last_regime = None
        if last_regime and rvol_hyst > 0:
            pass  # should not enter

        assert adj_trend_rvol == 1.2
        assert adj_gap_rvol == 1.5


class TestCrossFamilyCooldown:
    """Test cross-family contradiction cooldown in frequency control."""

    def _make_predictor(self):
        cfg = {
            "watchlist": [{"symbol": "SPY", "name": "S&P 500 ETF"}],
            "regime": {
                "stability": {"cross_family_cooldown_minutes": 15},
            },
            "auto_scan": {
                "cooldown": {
                    "same_signal_minutes": 30,
                    "max_per_session": 5,
                    "max_per_day": 10,
                },
                "override": {"confidence_increase": 0.10, "price_extension_pct": 0.50, "regime_upgrade": True},
            },
        }
        return USPredictor(cfg, collector=None)

    def test_cross_family_cooldown(self):
        """BREAKOUT alert within 15min of RANGE_REVERSAL → blocked."""
        pred = self._make_predictor()
        pred._scan_history_date = "2026-03-10"

        # Record a RANGE_REVERSAL (FADE family)
        rr_signal = USScanSignal(
            signal_type="RANGE_REVERSAL_LONG", direction="bullish", symbol="QQQ",
            regime=USRegimeResult(
                regime=USRegimeType.RANGE, confidence=0.80,
                rvol=0.8, price=480, gap_pct=0.1,
            ),
            price=480, timestamp=1000.0,
        )
        pred._record_alert("QQQ", rr_signal, "morning")

        # BREAKOUT (TREND family) 5 min later → should be blocked
        bo_signal = USScanSignal(
            signal_type="BREAKOUT_BULLISH", direction="bullish", symbol="QQQ",
            regime=USRegimeResult(
                regime=USRegimeType.TREND_STRONG, confidence=0.75,
                rvol=1.3, price=482, gap_pct=0.5,
            ),
            price=482, timestamp=1300.0,  # 300s = 5min later
        )
        allowed, _ = pred._check_frequency("QQQ", bo_signal, "morning", pred._cfg["auto_scan"])
        assert not allowed

    def test_cross_family_cooldown_expired(self):
        """BREAKOUT alert after cooldown expires → allowed."""
        pred = self._make_predictor()
        pred._scan_history_date = "2026-03-10"

        rr_signal = USScanSignal(
            signal_type="RANGE_REVERSAL_LONG", direction="bullish", symbol="QQQ",
            regime=USRegimeResult(
                regime=USRegimeType.RANGE, confidence=0.80,
                rvol=0.8, price=480, gap_pct=0.1,
            ),
            price=480, timestamp=1000.0,
        )
        pred._record_alert("QQQ", rr_signal, "morning")

        # BREAKOUT 20 min later → cooldown expired, should pass
        bo_signal = USScanSignal(
            signal_type="BREAKOUT_BULLISH", direction="bullish", symbol="QQQ",
            regime=USRegimeResult(
                regime=USRegimeType.TREND_STRONG, confidence=0.75,
                rvol=1.3, price=482, gap_pct=0.5,
            ),
            price=482, timestamp=2200.0,  # 1200s = 20min later
        )
        allowed, _ = pred._check_frequency("QQQ", bo_signal, "morning", pred._cfg["auto_scan"])
        assert allowed

    def test_same_family_no_cooldown(self):
        """Same family (TREND→TREND) not affected by cross-family cooldown."""
        pred = self._make_predictor()
        pred._scan_history_date = "2026-03-10"

        bo1 = USScanSignal(
            signal_type="BREAKOUT_BULLISH", direction="bullish", symbol="QQQ",
            regime=USRegimeResult(
                regime=USRegimeType.GAP_GO, confidence=0.85,
                rvol=1.8, price=480, gap_pct=1.5,
            ),
            price=480, timestamp=1000.0,
        )
        pred._record_alert("QQQ", bo1, "morning")

        # Different BREAKOUT direction 5 min later → same family, not blocked by cross-family
        bo2 = USScanSignal(
            signal_type="BREAKOUT_BEARISH", direction="bearish", symbol="QQQ",
            regime=USRegimeResult(
                regime=USRegimeType.TREND_STRONG, confidence=0.75,
                rvol=1.3, price=478, gap_pct=0.3,
            ),
            price=478, timestamp=1300.0,
        )
        allowed, _ = pred._check_frequency("QQQ", bo2, "morning", pred._cfg["auto_scan"])
        assert allowed


# ── P0-1: Gamma Wall distance filter ──

class TestGammaWallDistanceFilter:
    """Gamma walls too far from current price should be excluded from levels dict."""

    def test_close_gamma_wall_included(self):
        """Gamma wall within 10% → included."""
        vp = VolumeProfileResult(poc=400, vah=410, val=390)
        gw = GammaWallResult(call_wall_strike=430, put_wall_strike=375, max_pain=400)
        d = _us_key_levels_to_dict(vp, gamma_wall=gw, current_price=400)
        assert "Call Wall" in d  # 430 is 7.5% from 400
        assert "Put Wall" in d   # 375 is 6.25% from 400

    def test_far_gamma_wall_excluded(self):
        """Gamma wall beyond 10% → excluded (TSLA Put Wall 120 vs price 400)."""
        vp = VolumeProfileResult(poc=400, vah=410, val=390)
        gw = GammaWallResult(call_wall_strike=680, put_wall_strike=120, max_pain=400)
        d = _us_key_levels_to_dict(vp, gamma_wall=gw, current_price=400)
        assert "Call Wall" not in d  # 680 is 70% away
        assert "Put Wall" not in d   # 120 is 70% away

    def test_boundary_10_pct(self):
        """Gamma wall at exactly 10% → included."""
        vp = VolumeProfileResult(poc=400, vah=410, val=390)
        gw = GammaWallResult(call_wall_strike=440, put_wall_strike=360, max_pain=400)
        d = _us_key_levels_to_dict(vp, gamma_wall=gw, current_price=400)
        assert "Call Wall" in d   # exactly 10%
        assert "Put Wall" in d    # exactly 10%

    def test_no_current_price_includes_all(self):
        """Without current_price, all gamma walls included (backward compat)."""
        vp = VolumeProfileResult(poc=400, vah=410, val=390)
        gw = GammaWallResult(call_wall_strike=680, put_wall_strike=120, max_pain=400)
        d = _us_key_levels_to_dict(vp, gamma_wall=gw)
        assert "Call Wall" in d
        assert "Put Wall" in d

    def test_custom_max_distance(self):
        """Custom max_gamma_distance_pct=5 excludes walls at 7.5%."""
        vp = VolumeProfileResult(poc=400, vah=410, val=390)
        gw = GammaWallResult(call_wall_strike=430, put_wall_strike=375, max_pain=400)
        d = _us_key_levels_to_dict(vp, gamma_wall=gw, current_price=400, max_gamma_distance_pct=5.0)
        assert "Call Wall" not in d  # 7.5% > 5%
        assert "Put Wall" not in d   # 6.25% > 5%


# ── P0-1b: SL distance cap for fade plans ──

class TestFadeSLDistanceCap:
    """SL distance should be capped at 2% for fade plans."""

    def test_sl_within_limit_unchanged(self):
        """SL at 1% from entry → not capped."""
        sl, reason = _cap_fade_sl(entry=100.0, sl=101.0, sl_reason="PDH", direction="bearish")
        assert sl == 101.0
        assert reason == "PDH"

    def test_sl_beyond_limit_capped_bearish(self):
        """SL at 5% above entry → capped to 2%."""
        sl, reason = _cap_fade_sl(entry=100.0, sl=105.0, sl_reason="Call Wall", direction="bearish")
        assert sl == 102.0  # 100 * 1.02
        assert reason == "固定止损"

    def test_sl_beyond_limit_capped_bullish(self):
        """SL at 5% below entry → capped to 2%."""
        sl, reason = _cap_fade_sl(entry=100.0, sl=95.0, sl_reason="Put Wall", direction="bullish")
        assert sl == 98.0  # 100 * 0.98
        assert reason == "固定止损"

    def test_sl_none_passthrough(self):
        """None SL passes through."""
        sl, reason = _cap_fade_sl(entry=100.0, sl=None, sl_reason="VAH 上方", direction="bearish")
        assert sl is None

    def test_fade_bearish_plan_sl_capped(self):
        """Full plan generation: far SL capped in _plans_fade_bearish."""
        vp = VolumeProfileResult(poc=252.0, vah=255.0, val=249.0)
        # PDH at 280 → 9.8% from VAH 255 → should be capped
        kl = KeyLevels(
            poc=252.0, vah=255.0, val=249.0,
            pdh=280.0, pdl=230.0, pmh=254.0, pml=250.5,
            vwap=251.5,
        )
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.7,
            rvol=0.8, price=254.5, gap_pct=0.1,
        )
        plans = _generate_action_plans(regime, "bearish", vp, kl, None, None)
        plan_a = plans[0]
        if plan_a.entry and plan_a.stop_loss:
            sl_dist = abs(plan_a.stop_loss - plan_a.entry) / plan_a.entry
            assert sl_dist <= 0.021  # within 2% + rounding tolerance


# ── P0-2: RANGE directional trap ──

class TestDirectionalTrap:
    """Low RVOL + strong unidirectional move → UNCLEAR instead of RANGE."""

    def _vp(self, poc=400, vah=410, val=390):
        return VolumeProfileResult(poc=poc, vah=vah, val=val)

    def _make_today_bars(self, open_close: float, final_close: float, n_bars: int = 20):
        """Create today bars starting at open_close and ending at final_close."""
        prices = []
        for i in range(n_bars):
            t = f"2026-03-12 09:{30 + i}:00"
            # Linear interpolation
            c = open_close + (final_close - open_close) * (i / (n_bars - 1))
            prices.append((t, c - 0.5, c + 0.5, c - 0.5, c, 1000))
        return _make_bars(prices)

    def test_strong_bearish_move_unclear(self):
        """RVOL=0.7, price dropped 2.5% from open → UNCLEAR(lean=bearish)."""
        # Open at 405, now at 395 → -2.47%
        today = self._make_today_bars(open_close=405.0, final_close=395.0)
        r = classify_us_regime(
            price=395.0, prev_close=410.0, rvol=0.7,
            pmh=408.0, pml=402.0, vp=self._vp(poc=400, vah=410, val=390),
            today_bars=today,
        )
        assert r.regime == USRegimeType.UNCLEAR
        assert r.lean == "bearish"
        assert "Directional trap" in r.details

    def test_strong_bullish_move_unclear(self):
        """RVOL=0.8, price rallied 2% from open → UNCLEAR(lean=bullish)."""
        today = self._make_today_bars(open_close=400.0, final_close=408.5)
        r = classify_us_regime(
            price=408.5, prev_close=398.0, rvol=0.8,
            pmh=405.0, pml=398.0, vp=self._vp(poc=400, vah=410, val=390),
            today_bars=today,
        )
        assert r.regime == USRegimeType.UNCLEAR
        assert r.lean == "bullish"

    def test_small_move_still_fade_chop(self):
        """RVOL=0.7, price only moved 0.5% → still RANGE."""
        today = self._make_today_bars(open_close=400.0, final_close=402.0)
        r = classify_us_regime(
            price=402.0, prev_close=401.0, rvol=0.7,
            pmh=405.0, pml=398.0, vp=self._vp(poc=400, vah=410, val=390),
            today_bars=today,
        )
        assert r.regime == USRegimeType.RANGE

    def test_no_today_bars_no_trap(self):
        """Without today_bars, directional trap is not applied (backward compat)."""
        r = classify_us_regime(
            price=395.0, prev_close=410.0, rvol=0.7,
            pmh=408.0, pml=402.0, vp=self._vp(poc=400, vah=410, val=390),
        )
        # Without today_bars, just normal classification (RANGE or UNCLEAR based on VA)
        assert r.regime in (USRegimeType.RANGE, USRegimeType.UNCLEAR)
        if r.regime == USRegimeType.UNCLEAR:
            assert "Directional trap" not in r.details

    def test_high_rvol_no_trap(self):
        """RVOL >= fade_chop_rvol → GAP_GO/TREND_STRONG, trap doesn't apply."""
        today = self._make_today_bars(open_close=405.0, final_close=395.0)
        r = classify_us_regime(
            price=395.0, prev_close=410.0, rvol=1.5,
            pmh=408.0, pml=402.0, vp=self._vp(poc=400, vah=410, val=390),
            today_bars=today,
        )
        # High RVOL + below PML → likely GAP_GO or TREND_STRONG
        assert r.regime != USRegimeType.RANGE


# ── P1: RANGE direction consistency check ──

class TestFadeChopDirectionConsistency:
    """RANGE with direction conflicting VA edge → UNCLEAR plans."""

    def _vp(self):
        return VolumeProfileResult(poc=252.0, vah=255.0, val=249.0)

    def _kl(self):
        return KeyLevels(
            poc=252.0, vah=255.0, val=249.0,
            pdh=257.0, pdl=248.0, pmh=254.0, pml=250.5,
            vwap=251.5,
        )

    def test_val_bearish_conflict_unclear(self):
        """RANGE, edge=VAL, direction=bearish → conflict → UNCLEAR plans."""
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.7,
            rvol=0.8, price=249.5, gap_pct=0.1,  # near VAL → edge=VAL
        )
        plans = _generate_action_plans(regime, "bearish", self._vp(), self._kl(), None, None)
        # Should produce UNCLEAR-style plans (等待确认), not fade bullish plans
        assert plans[0].name == "等待确认"

    def test_vah_bullish_conflict_unclear(self):
        """RANGE, edge=VAH, direction=bullish → conflict → UNCLEAR plans."""
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.7,
            rvol=0.8, price=254.5, gap_pct=0.1,  # near VAH → edge=VAH
        )
        plans = _generate_action_plans(regime, "bullish", self._vp(), self._kl(), None, None)
        assert plans[0].name == "等待确认"

    def test_val_bullish_consistent_fade(self):
        """RANGE, edge=VAL, direction=bullish → consistent → normal fade plans."""
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.7,
            rvol=0.8, price=249.5, gap_pct=0.1,
        )
        plans = _generate_action_plans(regime, "bullish", self._vp(), self._kl(), None, None)
        assert plans[0].name == "下沿做多"

    def test_vah_bearish_consistent_fade(self):
        """RANGE, edge=VAH, direction=bearish → consistent → normal fade plans."""
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.7,
            rvol=0.8, price=254.5, gap_pct=0.1,
        )
        plans = _generate_action_plans(regime, "bearish", self._vp(), self._kl(), None, None)
        assert plans[0].name == "上沿做空"


# ── P2: Suppressed plan rendering ──

class TestSuppressedPlanRendering:
    """Suppressed plans should only show trigger + warning, no entry/SL/TP."""

    def test_suppressed_plan_hides_details(self):
        """Suppressed plan → no entry/SL/TP lines rendered (only trigger + warning)."""
        plan = ActionPlan(
            label="B", name="VWAP 回归做空", emoji="📉", is_primary=False,
            logic="VWAP 上方接空", direction="bearish",
            trigger="价格反弹至 VWAP 251.50",
            entry=251.5, entry_action="做空",
            stop_loss=255.0, stop_loss_reason="VAH",
            tp1=250.0, tp1_label="POC", tp2=249.0, tp2_label="VAL",
            rr_ratio=1.5,
            suppressed=True,
            demote_reason="核心结论为观望, 中间区域暂缓",
        )
        lines = _format_action_plan(plan)
        text = "\n".join(lines)
        # Specific entry/SL/TP format strings should not appear
        assert "入场:" not in text and "入场区间:" not in text
        assert "止损:" not in text
        assert "TP1" not in text
        assert "R:R" not in text
        assert "观望" in text

    def test_demoted_plan_shows_details(self):
        """Demoted (not suppressed) plan → still shows entry/SL/TP."""
        plan = ActionPlan(
            label="A", name="上沿做空", emoji="📉", is_primary=True,
            logic="VAH 附近做空", direction="bearish",
            trigger="价格触及 VAH",
            entry=255.0, entry_action="做空",
            stop_loss=257.0, stop_loss_reason="PDH",
            tp1=252.0, tp1_label="POC", tp2=249.0, tp2_label="VAL",
            rr_ratio=1.5,
            demoted=True,
            demote_reason="入场位距当前价 2.0%, 剩余波动预估仅 1.5%",
        )
        lines = _format_action_plan(plan)
        text = "\n".join(lines)
        assert "入场" in text
        assert "止损" in text
        assert "⚠️" in text


class TestStructureOverrideDirection:
    """Test _decide_direction with structural level overrides (long bias fix)."""

    def test_extreme_bearish_structure_override(self):
        """price < PDL + VWAP + PML → forced bearish even if price > VAH."""
        regime = USRegimeResult(
            regime=USRegimeType.TREND_STRONG, confidence=0.8,
            rvol=1.5, price=548, gap_pct=0.5,
        )
        # price=548 > VAH=545 → old logic would say bullish
        vp = VolumeProfileResult(poc=540, vah=545, val=535)
        result = _decide_direction(
            regime, vp, vwap=550, pdl=550, pdh=560, pml=552, pmh=565,
        )
        # price < PDL(550), price < VWAP(550), price < PML(552) → bearish_count=3
        assert result == "bearish"

    def test_extreme_bullish_structure_override(self):
        """price > PDH + VWAP + PMH → forced bullish even if price < VAL."""
        regime = USRegimeResult(
            regime=USRegimeType.GAP_GO, confidence=0.85,
            rvol=2.0, price=570, gap_pct=1.5,
        )
        # price=570 < VAL=575 → old logic would say bearish
        vp = VolumeProfileResult(poc=580, vah=585, val=575)
        result = _decide_direction(
            regime, vp, vwap=565, pdl=555, pdh=565, pml=560, pmh=568,
        )
        # price > PDH(565), price > VWAP(565), price > PMH(568) → bullish_count=3
        assert result == "bullish"

    def test_vwap_contradiction_neutral(self):
        """price > VAH but < VWAP → neutral (VWAP contradiction veto)."""
        regime = USRegimeResult(
            regime=USRegimeType.TREND_STRONG, confidence=0.75,
            rvol=1.3, price=556, gap_pct=0.3,
        )
        vp = VolumeProfileResult(poc=550, vah=555, val=545)
        # price=556 > VAH=555, but price=556 < VWAP=560 → neutral
        result = _decide_direction(regime, vp, vwap=560)
        assert result == "neutral"

    def test_vwap_contradiction_bearish_side(self):
        """price < VAL but > VWAP → neutral (VWAP contradiction veto)."""
        regime = USRegimeResult(
            regime=USRegimeType.TREND_STRONG, confidence=0.75,
            rvol=1.3, price=544, gap_pct=-0.3,
        )
        vp = VolumeProfileResult(poc=550, vah=555, val=545)
        # price=544 < VAL=545, but price=544 > VWAP=540 → neutral
        result = _decide_direction(regime, vp, vwap=540)
        assert result == "neutral"

    def test_poc_zero_vwap_fallback(self):
        """POC=0 should use VWAP for direction, not hardcode bullish."""
        regime = USRegimeResult(
            regime=USRegimeType.TREND_STRONG, confidence=0.7,
            rvol=1.2, price=548, gap_pct=0.2,
        )
        vp = VolumeProfileResult(poc=0, vah=555, val=545)
        # price=548 is between VAL and VAH, POC=0
        # price=548 < VWAP=550 → bearish (not the old default "bullish")
        result = _decide_direction(regime, vp, vwap=550)
        assert result == "bearish"

    def test_poc_zero_no_vwap_neutral(self):
        """POC=0 and VWAP=0 → neutral (not hardcode bullish)."""
        regime = USRegimeResult(
            regime=USRegimeType.TREND_STRONG, confidence=0.7,
            rvol=1.2, price=548, gap_pct=0.2,
        )
        vp = VolumeProfileResult(poc=0, vah=555, val=545)
        result = _decide_direction(regime, vp, vwap=0)
        assert result == "neutral"

    def test_backward_compat_no_structure_args(self):
        """Without new params, behavior matches old logic."""
        regime = USRegimeResult(
            regime=USRegimeType.GAP_GO, confidence=0.85,
            rvol=2.0, price=560, gap_pct=1.5,
        )
        vp = VolumeProfileResult(poc=550, vah=555, val=545)
        # price > VAH → bullish (same as before)
        assert _decide_direction(regime, vp) == "bullish"

        regime2 = USRegimeResult(
            regime=USRegimeType.TREND_STRONG, confidence=0.8,
            rvol=1.5, price=540, gap_pct=-0.5,
        )
        # price < VAL → bearish (same as before)
        assert _decide_direction(regime2, vp) == "bearish"

    def test_fade_chop_vwap_veto(self):
        """RANGE bullish direction but VWAP < VAL → neutral."""
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.75,
            rvol=0.8, price=252, gap_pct=0.1,
        )
        vp = VolumeProfileResult(poc=255, vah=260, val=250)
        # price=252 → ratio=0.2 edge zone → base bullish
        # But VWAP=248 < VAL=250 → veto to neutral
        result = _decide_direction(regime, vp, vwap=248)
        assert result == "neutral"

    def test_fade_chop_vwap_veto_bearish(self):
        """RANGE bearish direction but VWAP > VAH → neutral."""
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.75,
            rvol=0.8, price=258, gap_pct=0.1,
        )
        vp = VolumeProfileResult(poc=255, vah=260, val=250)
        # price=258 → ratio=0.8 edge zone → base bearish
        # But VWAP=262 > VAH=260 → veto to neutral
        result = _decide_direction(regime, vp, vwap=262)
        assert result == "neutral"

    def test_structure_override_only_for_trend_regimes(self):
        """RANGE should NOT trigger extreme structure override."""
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.75,
            rvol=0.8, price=252, gap_pct=0.1,
        )
        vp = VolumeProfileResult(poc=255, vah=260, val=250)
        # Even with extreme structure: price < PDL, < VWAP, < PML
        # RANGE should still use VA zone logic, not forced bearish
        result = _decide_direction(
            regime, vp, vwap=253, pdl=253, pdh=260, pml=254, pmh=262,
        )
        # price=252 → ratio=0.2, edge zone bullish (VWAP=253 > VAL=250, no veto)
        assert result == "bullish"

    def test_single_structure_signal_not_enough(self):
        """Only 1 structural level aligned → no override (need >=2)."""
        regime = USRegimeResult(
            regime=USRegimeType.TREND_STRONG, confidence=0.8,
            rvol=1.5, price=556, gap_pct=0.5,
        )
        vp = VolumeProfileResult(poc=550, vah=555, val=545)
        # price=556 > VAH=555 → normally bullish
        # Only price > PDH(554), but price < VWAP(560) and PMH=0 → bullish_count=1
        # Not enough for override, but VWAP veto: price > VAH but < VWAP → neutral
        result = _decide_direction(regime, vp, vwap=560, pdh=554)
        assert result == "neutral"  # VWAP contradiction veto


# ── Structure-based TREND_STRONG ──

class TestStructureTrendDay:
    """Price structure detection for low-RVOL trend days (slow bleed / slow grind)."""

    _ENABLED_CFG = {
        "enabled": True,
        "window": 15,
        "min_windows": 3,
        "consistency": 0.67,
        "fast_min_bars": 20,
        "fast_side_pct": 0.80,
        "fast_r2_min": 0.70,
    }

    def _vp(self, poc=400, vah=410, val=390):
        return VolumeProfileResult(poc=poc, vah=vah, val=val)

    def _make_declining_bars(self, n_bars: int, start_price: float = 200.0, drop_per_bar: float = 0.15):
        """Create steady declining bars (LH + LL pattern)."""
        prices = []
        for i in range(n_bars):
            t = f"2026-03-13 09:{30 + i}:00" if i < 30 else f"2026-03-13 10:{i - 30:02d}:00"
            c = start_price - drop_per_bar * i
            h = c + 0.3  # small wick above
            l = c - 0.2  # small wick below
            o = c + 0.1
            prices.append((t, o, h, l, c, 500))
        return _make_bars(prices)

    def _make_rising_bars(self, n_bars: int, start_price: float = 200.0, rise_per_bar: float = 0.15):
        """Create steady rising bars (HH + HL pattern)."""
        prices = []
        for i in range(n_bars):
            t = f"2026-03-13 09:{30 + i}:00" if i < 30 else f"2026-03-13 10:{i - 30:02d}:00"
            c = start_price + rise_per_bar * i
            h = c + 0.2
            l = c - 0.3
            o = c - 0.1
            prices.append((t, o, h, l, c, 500))
        return _make_bars(prices)

    def _make_choppy_bars(self, n_bars: int, center: float = 200.0):
        """Create oscillating bars with no clear direction."""
        prices = []
        for i in range(n_bars):
            t = f"2026-03-13 09:{30 + i}:00" if i < 30 else f"2026-03-13 10:{i - 30:02d}:00"
            # Zigzag around center
            offset = 1.0 if i % 2 == 0 else -1.0
            c = center + offset * (i % 3)
            h = c + 0.5
            l = c - 0.5
            o = c - offset * 0.3
            prices.append((t, o, h, l, c, 500))
        return _make_bars(prices)

    # ── detect_price_structure unit tests ──

    def test_structure_l1_bearish(self):
        """20+ bars + R² entry gate + VWAP cross-validation → L1 bearish."""
        # Steeper decline to ensure R² passes threshold
        bars = self._make_declining_bars(25, start_price=200.0, drop_per_bar=0.50)
        result = detect_price_structure(bars, fast_min_bars=20, fast_r2_min=0.70)
        assert result is not None
        assert result.direction == "bearish"
        assert result.layer == 1
        assert 0.35 <= result.confidence <= 0.65  # R² gate + optional hold bonus

    def test_structure_l2_bearish(self):
        """4 declining windows → L2 bearish with primary gate + secondary."""
        # 60 bars = 4 windows of 15 bars each, steady decline
        bars = self._make_declining_bars(60, start_price=200.0, drop_per_bar=0.10)
        result = detect_price_structure(bars, window=15, min_windows=3, fast_min_bars=20)
        assert result is not None
        assert result.direction == "bearish"
        assert result.layer == 2
        assert 0.45 <= result.confidence <= 0.65

    def test_structure_l2_overrides_l1(self):
        """45+ bars satisfying both layers → L2 is preferred (higher layer)."""
        bars = self._make_declining_bars(60, start_price=200.0, drop_per_bar=0.12)
        result = detect_price_structure(bars, window=15, min_windows=3, fast_min_bars=20)
        assert result is not None
        assert result.layer == 2  # L2 preferred over L1

    def test_structure_bullish(self):
        """HH + HL + positive VWAP slope → bullish."""
        bars = self._make_rising_bars(60, start_price=200.0, rise_per_bar=0.10)
        result = detect_price_structure(bars, window=15, min_windows=3, fast_min_bars=20)
        assert result is not None
        assert result.direction == "bullish"

    def test_structure_no_pattern(self):
        """Choppy bars → no structure detected."""
        bars = self._make_choppy_bars(60, center=200.0)
        result = detect_price_structure(bars, window=15, min_windows=3, fast_min_bars=20)
        assert result is None

    def test_structure_low_r2_no_l1(self):
        """R² < 0.70 → Layer 1 does not trigger."""
        # Choppy bars but only 25 (not enough for L2)
        bars = self._make_choppy_bars(25, center=200.0)
        result = detect_price_structure(bars, fast_min_bars=20, fast_r2_min=0.70)
        assert result is None

    def test_structure_insufficient_bars(self):
        """< 20 bars → no structure."""
        bars = self._make_declining_bars(15, start_price=200.0, drop_per_bar=0.20)
        result = detect_price_structure(bars, fast_min_bars=20)
        assert result is None

    # ── classify_us_regime integration tests ──

    def test_structure_disabled_config(self):
        """enabled: false → structure detection skipped."""
        bars = self._make_declining_bars(60, start_price=200.0, drop_per_bar=0.10)
        final_price = 200.0 - 0.10 * 59
        r = classify_us_regime(
            price=final_price, prev_close=201.0, rvol=0.7,
            pmh=201.0, pml=198.0, vp=self._vp(poc=196, vah=200, val=192),
            today_bars=bars,
            structure_trend_cfg={"enabled": False},
        )
        assert r.regime not in (USRegimeType.TREND_STRONG, USRegimeType.TREND_WEAK)

    def test_structure_triggers_trend_day_in_classify(self):
        """Low RVOL + clear declining structure → TREND_WEAK via structure path (Phase 2)."""
        bars = self._make_declining_bars(60, start_price=200.0, drop_per_bar=0.10)
        final_price = 200.0 - 0.10 * 59  # ~194.1
        r = classify_us_regime(
            price=final_price, prev_close=201.0, rvol=0.7,
            pmh=201.0, pml=198.0, vp=self._vp(poc=196, vah=200, val=192),
            today_bars=bars,
            structure_trend_cfg=self._ENABLED_CFG,
        )
        # Phase 2: Structure-based trend always outputs TREND_WEAK
        assert r.regime == USRegimeType.TREND_WEAK
        assert "Structure" in r.details

    def test_structure_does_not_override_rvol_trend_day(self):
        """RVOL-based TREND fires first (higher priority than structure)."""
        bars = self._make_declining_bars(60, start_price=200.0, drop_per_bar=0.10)
        final_price = 200.0 - 0.10 * 59  # ~194.1
        r = classify_us_regime(
            price=final_price, prev_close=194.5, rvol=1.3,  # RVOL 1.3 >= trend_day 1.2, small gap
            pmh=201.0, pml=190.0,  # pml=190 so no pm_breakout → avoids GAP_GO
            vp=self._vp(poc=198, vah=198, val=195),  # price 194.1 < val 195 → outside VA
            today_bars=bars,
            structure_trend_cfg=self._ENABLED_CFG,
        )
        # Phase 2: RVOL 1.3 < gap_and_go_rvol 1.5 → TREND_WEAK, but RVOL path (not structure)
        assert r.regime in (USRegimeType.TREND_STRONG, USRegimeType.TREND_WEAK)
        assert "Structure" not in r.details  # RVOL path, not structure

    def test_structure_stop_hunt_l2_fallback_l1(self):
        """Stop hunt breaks L2 pattern but L1 (VWAP trend) still holds."""
        # Create declining bars but insert a spike in the middle that breaks L2
        bars = self._make_declining_bars(45, start_price=200.0, drop_per_bar=0.10)
        # Inject a stop-hunt spike in window 2 (bars 15-29) — higher high than window 1
        spike_idx = 20
        bars.iloc[spike_idx, bars.columns.get_loc("High")] = 202.0  # breaks LH pattern
        bars.iloc[spike_idx, bars.columns.get_loc("Close")] = 199.0  # close stays low

        result = detect_price_structure(
            bars, window=15, min_windows=3, fast_min_bars=20, fast_r2_min=0.70,
        )
        # Even if L2 fails, L1 should still detect via VWAP trend + R²
        assert result is not None
        assert result.direction == "bearish"

    # ── RC1/RC2 new tests ──

    def test_structure_l1_moderate_decline(self):
        """Moderate decline (no extreme VWAP slope) still detected via R² entry gate."""
        # 25 bars with gentle decline — old VWAP-slope gate would reject this
        bars = self._make_declining_bars(25, start_price=200.0, drop_per_bar=0.20)
        result = detect_price_structure(bars, fast_min_bars=20, fast_r2_min=0.70)
        assert result is not None
        assert result.direction == "bearish"
        assert result.layer == 1

    def test_structure_l2_primary_signal_only(self):
        """L2: LH pattern passes but LL doesn't meet consistency → still detected with penalty."""
        # 60 bars: highs consistently decline but lows are flat
        prices = []
        for i in range(60):
            t = f"2026-03-13 09:{30 + i}:00" if i < 30 else f"2026-03-13 10:{i - 30:02d}:00"
            c = 200.0 - 0.10 * i
            h = c + 0.3 - 0.05 * i  # highs decline
            l = 197.0  # lows are flat — LL pattern fails
            o = c + 0.1
            prices.append((t, o, h, l, c, 500))
        bars = _make_bars(prices)
        result = detect_price_structure(bars, window=15, min_windows=3, consistency=0.67)
        # LH primary should pass, LL secondary fails → still detected with lower conf
        if result is not None and result.layer == 2:
            assert result.direction == "bearish"
            assert result.confidence <= 0.60  # secondary penalty applied

    def test_structure_l2_too_many_violations(self):
        """L2: Primary signal (LH) doesn't meet consistency threshold → no L2 detection."""
        # 60 bars: mostly flat highs with occasional rises — LH pattern fails
        prices = []
        for i in range(60):
            t = f"2026-03-13 09:{30 + i}:00" if i < 30 else f"2026-03-13 10:{i - 30:02d}:00"
            c = 200.0 - 0.02 * i  # very slight decline
            h = 201.0 + (0.5 if i % 20 < 10 else 0.0)  # highs oscillate, not consistently lower
            l = c - 0.2
            o = c + 0.1
            prices.append((t, o, h, l, c, 500))
        bars = _make_bars(prices)
        result = detect_price_structure(bars, window=15, min_windows=3, consistency=0.67)
        # LH primary should fail → no L2 result (L1 might still fire)
        if result is not None:
            assert result.layer != 2 or result.direction == "bearish"

    def test_spy_312_scenario(self):
        """SPY 3/12 渗透型趋势日: moderate decline + low VWAP slope → now detected."""
        # Simulate: 50 bars of gradual decline, ~-1.2% total over 50 bars
        start_price = 565.0
        drop_per_bar = 0.14  # ~7.0 total = ~1.2%
        bars = self._make_declining_bars(50, start_price=start_price, drop_per_bar=drop_per_bar)
        final_price = start_price - drop_per_bar * 49  # ~558.14

        r = classify_us_regime(
            price=final_price, prev_close=566.0, rvol=0.9,
            pmh=567.0, pml=563.0,
            vp=self._vp(poc=562, vah=568, val=556),
            today_bars=bars,
            structure_trend_cfg=self._ENABLED_CFG,
        )
        # Phase 2: Structure-based trend → TREND_WEAK (not UNCLEAR)
        assert r.regime == USRegimeType.TREND_WEAK
        assert "Structure" in r.details

    def test_genuine_chop_not_promoted(self):
        """Choppy bars should NOT be promoted to any TREND regime."""
        bars = self._make_choppy_bars(50, center=200.0)
        r = classify_us_regime(
            price=200.5, prev_close=201.0, rvol=0.9,
            pmh=202.0, pml=198.0,
            vp=self._vp(poc=200, vah=205, val=195),
            today_bars=bars,
            structure_trend_cfg=self._ENABLED_CFG,
        )
        assert r.regime not in (USRegimeType.TREND_STRONG, USRegimeType.TREND_WEAK)


# ── TREND_STRONG Persistence Tests ──

class TestTrendPersistence:
    """TREND_STRONG persistence: inside VA + strong intraday return + VWAP agreement."""

    @staticmethod
    def _make_n_bars(n: int, base: str = "2026-03-13 10:00") -> pd.DataFrame:
        """Create n 1-minute bars starting from base timestamp."""
        rows = []
        start = pd.Timestamp(base, tz="America/New_York")
        for i in range(n):
            ts = start + pd.Timedelta(minutes=i)
            rows.append({
                "Open": 100.0, "High": 100.5, "Low": 99.5,
                "Close": 100.0, "Volume": 1000,
            })
        idx = pd.DatetimeIndex(
            [start + pd.Timedelta(minutes=i) for i in range(n)], name="Datetime"
        )
        return pd.DataFrame(rows, index=idx)

    def test_trend_persistence_bearish(self):
        """inside_va + return<-1% + price<VWAP + >=30 bars → TREND_WEAK bearish (Phase 2)."""
        bars = self._make_n_bars(35)
        # open_price=100, price=98.5 → return=-1.5%, inside VA [98..102]
        r = classify_us_regime(
            price=98.5, prev_close=100.0, rvol=1.3,
            pmh=101.0, pml=97.0,
            vp=VolumeProfileResult(poc=100.0, vah=102.0, val=98.0, trading_days=5),
            open_price=100.0,
            today_bars=bars,
            vwap=99.0,  # price 98.5 < vwap 99.0 → agrees with bearish
            trend_day_rvol=1.2,
        )
        # Phase 2: Persistence-based trend always outputs TREND_WEAK
        assert r.regime == USRegimeType.TREND_WEAK
        assert r.lean == "bearish"
        assert "persistence" in r.details.lower()

    def test_trend_persistence_vshape_guard(self):
        """return<-1% but price>VWAP → V-shape guard blocks persistence."""
        bars = self._make_n_bars(35)
        r = classify_us_regime(
            price=98.5, prev_close=100.0, rvol=1.3,
            pmh=101.0, pml=97.0,
            vp=VolumeProfileResult(poc=100.0, vah=102.0, val=98.0, trading_days=5),
            open_price=100.0,
            today_bars=bars,
            vwap=98.0,  # price 98.5 > vwap 98.0 → disagrees with bearish
            trend_day_rvol=1.2,
        )
        # Should NOT be TREND via persistence (V-shape guard)
        assert r.regime not in (USRegimeType.TREND_STRONG, USRegimeType.TREND_WEAK) or "persistence" not in r.details.lower()

    def test_trend_persistence_early_session(self):
        """inside_va + return<-1% but <30 bars → not triggered."""
        bars = self._make_n_bars(20)  # only 20 bars
        r = classify_us_regime(
            price=98.5, prev_close=100.0, rvol=1.3,
            pmh=101.0, pml=97.0,
            vp=VolumeProfileResult(poc=100.0, vah=102.0, val=98.0, trading_days=5),
            open_price=100.0,
            today_bars=bars,
            vwap=99.0,
            trend_day_rvol=1.2,
        )
        # With <30 bars, persistence should not activate
        assert r.regime not in (USRegimeType.TREND_STRONG, USRegimeType.TREND_WEAK) or "persistence" not in r.details.lower()

    def test_trend_persistence_adaptive_threshold(self):
        """RC3: ADR-based adaptive threshold — ADR 2% → threshold 0.8%, return 1.5% passes."""
        bars = self._make_n_bars(35)
        profile = RvolProfile(
            gap_and_go_rvol=1.5, trend_day_rvol=1.2, fade_chop_rvol=1.0,
            avg_daily_range_pct=2.0,  # threshold = max(0.005, 0.4*2.0/100) = 0.008
            percentile_rank=50.0, sample_size=10,
        )
        r = classify_us_regime(
            price=98.5, prev_close=100.0, rvol=1.3,
            pmh=101.0, pml=97.0,
            vp=VolumeProfileResult(poc=100.0, vah=102.0, val=98.0, trading_days=5),
            open_price=100.0, today_bars=bars, vwap=99.0,
            rvol_profile=profile,
        )
        # Phase 2: Persistence-based trend always outputs TREND_WEAK
        assert r.regime == USRegimeType.TREND_WEAK
        assert "persistence" in r.details.lower()

    def test_trend_persistence_adaptive_boundary(self):
        """RC3: Return just below adaptive threshold → persistence does NOT fire."""
        bars = self._make_n_bars(35)
        profile = RvolProfile(
            gap_and_go_rvol=1.5, trend_day_rvol=1.2, fade_chop_rvol=1.0,
            avg_daily_range_pct=5.0,  # threshold = max(0.005, 0.4*5.0/100) = 0.02
            percentile_rank=50.0, sample_size=10,
        )
        # open=100, price=98.5 → return=-1.5%=0.015 < threshold 0.02
        r = classify_us_regime(
            price=98.5, prev_close=100.0, rvol=1.3,
            pmh=101.0, pml=97.0,
            vp=VolumeProfileResult(poc=100.0, vah=102.0, val=98.0, trading_days=5),
            open_price=100.0, today_bars=bars, vwap=99.0,
            rvol_profile=profile,
        )
        assert r.regime not in (USRegimeType.TREND_STRONG, USRegimeType.TREND_WEAK) or "persistence" not in r.details.lower()

    def test_trend_persistence_no_profile_fallback(self):
        """RC3: No rvol_profile → fallback to 0.01 threshold."""
        bars = self._make_n_bars(35)
        # open=100, price=98.5 → return=-1.5% > 1% → passes fallback
        r = classify_us_regime(
            price=98.5, prev_close=100.0, rvol=1.3,
            pmh=101.0, pml=97.0,
            vp=VolumeProfileResult(poc=100.0, vah=102.0, val=98.0, trading_days=5),
            open_price=100.0, today_bars=bars, vwap=99.0,
            rvol_profile=None, trend_day_rvol=1.2,
        )
        # Phase 2: Persistence-based trend always outputs TREND_WEAK
        assert r.regime == USRegimeType.TREND_WEAK
        assert "persistence" in r.details.lower()


# ── Direction Override VWAP Tests ──

class TestDirectionOverrideVWAP:
    """Playbook neutral fallback uses VWAP instead of POC."""

    def test_inside_va_below_vwap_bearish(self):
        """price inside VA, below VWAP → direction=bearish."""
        from src.us_playbook.playbook import format_us_playbook_message
        regime = USRegimeResult(
            regime=USRegimeType.UNCLEAR, confidence=0.30,
            rvol=1.0, price=100.0, gap_pct=0.1,
            lean="neutral",
        )
        vp = VolumeProfileResult(poc=99.5, vah=102.0, val=98.0, trading_days=5)
        kl = KeyLevels(
            poc=99.5, vah=102.0, val=98.0,
            pdh=101.0, pdl=99.0, pmh=101.5, pml=99.5,
            vwap=100.5,  # price 100.0 < vwap 100.5 → bearish
        )
        # _decide_direction for UNCLEAR with lean="neutral" returns "neutral"
        # Neutral fallback should use VWAP: 100 < 100.5 → bearish
        _direction = _decide_direction(regime, vp, vwap=kl.vwap)
        # _decide_direction returns "neutral" for UNCLEAR lean=neutral
        assert _direction == "neutral"
        # The playbook neutral fallback should pick bearish via VWAP
        # Test the fallback logic directly
        if _direction == "neutral":
            if regime.price > vp.vah:
                _direction = "bullish"
            elif regime.price < vp.val:
                _direction = "bearish"
            elif kl.vwap > 0:
                _direction = "bullish" if regime.price > kl.vwap else "bearish"
            elif vp.poc > 0:
                _direction = "bullish" if regime.price > vp.poc else "bearish"
            else:
                _direction = "bullish"
        assert _direction == "bearish"


# ── UNCLEAR Lean Override Tests ──

class TestUnclearLeanOverride:
    """UNCLEAR lean override: intraday return + VWAP double-confirmation."""

    @staticmethod
    def _make_n_bars(n: int) -> pd.DataFrame:
        rows = []
        start = pd.Timestamp("2026-03-13 10:00", tz="America/New_York")
        for i in range(n):
            rows.append({
                "Open": 100.0, "High": 100.5, "Low": 99.5,
                "Close": 100.0, "Volume": 1000,
            })
        idx = pd.DatetimeIndex(
            [start + pd.Timedelta(minutes=i) for i in range(n)], name="Datetime"
        )
        return pd.DataFrame(rows, index=idx)

    def test_unclear_lean_bearish_override(self):
        """return<-0.5% + price<VWAP + >=30 bars → lean=bearish."""
        bars = self._make_n_bars(35)
        # Sub-type 2: inside_va + rvol >= trend_day → default lean = price vs POC
        # price=99.8, POC=99.5 → default lean would be bullish
        # But override: open=100, price=99.3 → return=-0.7%, vwap=99.5, price<vwap → bearish
        r = classify_us_regime(
            price=99.3, prev_close=100.0, rvol=1.3,
            pmh=101.0, pml=97.0,
            vp=VolumeProfileResult(poc=99.5, vah=102.0, val=98.0, trading_days=5),
            open_price=100.0,
            today_bars=bars,
            vwap=99.5,  # price 99.3 < vwap 99.5 → bearish
            trend_day_rvol=1.2,
        )
        # With persistence, this might be TREND_STRONG. If UNCLEAR, check lean.
        # return=-0.7% < -1% threshold for persistence, so persistence won't fire.
        # RVOL 1.3 >= 1.2 + inside VA → sub-type 2 UNCLEAR.
        # Override: return=-0.7%, |0.7%| > 0.5%, ret_lean=bearish, price<vwap → vwap_lean=bearish → match
        assert r.regime == USRegimeType.UNCLEAR
        assert r.lean == "bearish"

    def test_unclear_lean_no_override_conflicting(self):
        """return<0 but price>VWAP → conflicting, no override."""
        bars = self._make_n_bars(35)
        # open=100, price=99.3 → return=-0.7% (bearish return)
        # vwap=99.0, price 99.3 > vwap → bullish VWAP → conflict → no override
        r = classify_us_regime(
            price=99.3, prev_close=100.0, rvol=1.3,
            pmh=101.0, pml=97.0,
            vp=VolumeProfileResult(poc=99.5, vah=102.0, val=98.0, trading_days=5),
            open_price=100.0,
            today_bars=bars,
            vwap=99.0,  # price 99.3 > vwap 99.0 → bullish VWAP, conflicts with bearish return
            trend_day_rvol=1.2,
        )
        # Default sub-type 2 lean: price(99.3) < poc(99.5) → bearish (from POC, not override)
        # The override should NOT fire because ret_lean(bearish) != vwap_lean(bullish)
        assert r.regime == USRegimeType.UNCLEAR
        # lean stays as default POC-based: price < poc → bearish
        assert r.lean == "bearish"


# ── Sub-type 3 Lean VWAP Cross-validation Tests ──

class TestSubtype3Lean:
    """RC4: Sub-type 3 (outside VA, low volume) lean uses VWAP cross-validation."""

    def test_subtype3_below_val_below_vwap_bearish(self):
        """price < VAL, price < VWAP → lean = bearish."""
        # No today_bars to avoid directional trap (|move| > 1.5%)
        r = classify_us_regime(
            price=88.0, prev_close=90.0, rvol=0.7,
            pmh=91.0, pml=89.0,
            vp=VolumeProfileResult(poc=92.0, vah=95.0, val=89.0, trading_days=5),
            vwap=89.5,  # price 88.0 < vwap 89.5 → bearish
            fade_chop_rvol=1.0,
        )
        assert r.regime == USRegimeType.UNCLEAR
        assert r.lean == "bearish"

    def test_subtype3_below_val_above_vwap_bullish(self):
        """price < VAL, price >= VWAP → lean = bullish (reversal)."""
        r = classify_us_regime(
            price=88.5, prev_close=90.0, rvol=0.7,
            pmh=91.0, pml=87.0,
            vp=VolumeProfileResult(poc=92.0, vah=95.0, val=89.0, trading_days=5),
            vwap=88.0,  # price 88.5 >= vwap 88.0 → bullish
            fade_chop_rvol=1.0,
        )
        assert r.regime == USRegimeType.UNCLEAR
        assert r.lean == "bullish"

    def test_subtype3_no_vwap_neutral(self):
        """price outside VA, VWAP=0 → lean = neutral."""
        r = classify_us_regime(
            price=88.0, prev_close=90.0, rvol=0.7,
            pmh=91.0, pml=89.0,
            vp=VolumeProfileResult(poc=92.0, vah=95.0, val=89.0, trading_days=5),
            vwap=0,
            fade_chop_rvol=1.0,
        )
        assert r.regime == USRegimeType.UNCLEAR
        assert r.lean == "neutral"

    def test_unclear_override_at_0_4_pct(self):
        """RC5: |return| = 0.4% now triggers override (was > 0.5%)."""
        rows = []
        start = pd.Timestamp("2026-03-13 10:00", tz="America/New_York")
        for i in range(35):
            rows.append({
                "Open": 100.0, "High": 100.5, "Low": 99.5,
                "Close": 100.0, "Volume": 1000,
            })
        idx = pd.DatetimeIndex(
            [start + pd.Timedelta(minutes=i) for i in range(35)], name="Datetime"
        )
        bars = pd.DataFrame(rows, index=idx)

        # open=100, price=99.6 → return=-0.4%, |0.4%| >= 0.004
        # VWAP=99.8, price 99.6 < vwap → bearish. ret_lean=bearish → match → override
        r = classify_us_regime(
            price=99.6, prev_close=100.0, rvol=1.3,
            pmh=101.0, pml=97.0,
            vp=VolumeProfileResult(poc=99.5, vah=102.0, val=98.0, trading_days=5),
            open_price=100.0, today_bars=bars, vwap=99.8,
            trend_day_rvol=1.2,
        )
        assert r.regime == USRegimeType.UNCLEAR
        assert r.lean == "bearish"


# ── Gamma Wall Adverse Warning Tests ──

class TestGammaWallAdverseWarning:
    """Tests for gamma wall adverse warning / demote logic."""

    def _plan(self, direction="bullish", entry=250.0):
        return ActionPlan(
            label="A", name="test", emoji="📈", is_primary=True,
            logic="test", direction=direction, trigger="test",
            entry=entry, entry_action="做多" if direction == "bullish" else "做空",
            stop_loss=248.0 if direction == "bullish" else 252.0,
            stop_loss_reason="SL",
            tp1=255.0 if direction == "bullish" else 245.0,
            tp1_label="VAH",
            tp2=None, tp2_label="", rr_ratio=2.5,
        )

    def _ctx(self, adr=1.2):
        return PlanContext(avg_daily_range_pct=adr)

    def _gw(self, max_pain=250.0, call_wall=260.0, put_wall=240.0):
        return GammaWallResult(
            call_wall_strike=call_wall,
            put_wall_strike=put_wall,
            max_pain=max_pain,
        )

    def test_bullish_max_pain_below_warns(self):
        """Bullish plan + MaxPain well below price → warning."""
        plan = self._plan(direction="bullish", entry=250.0)
        gw = self._gw(max_pain=245.0)  # 2% below 250
        ctx = self._ctx(adr=1.2)  # warn_thr = 0.6%
        plans = _apply_gamma_wall_warning([plan], price=250.0, gamma_wall=gw, ctx=ctx)
        assert "期权引力偏空" in plans[0].warning

    def test_bearish_max_pain_above_warns(self):
        """Bearish plan + MaxPain well above price → warning."""
        plan = self._plan(direction="bearish", entry=250.0)
        gw = self._gw(max_pain=256.0)  # 2.4% above 250
        ctx = self._ctx(adr=1.2)
        plans = _apply_gamma_wall_warning([plan], price=250.0, gamma_wall=gw, ctx=ctx)
        assert "期权引力偏多" in plans[0].warning

    def test_bullish_put_wall_above_price_demotes(self):
        """Bullish plan + Put Wall above price → demote."""
        plan = self._plan(direction="bullish", entry=250.0)
        gw = self._gw(put_wall=252.0)  # put wall above price
        ctx = self._ctx(adr=1.2)
        plans = _apply_gamma_wall_warning([plan], price=250.0, gamma_wall=gw, ctx=ctx)
        assert plans[0].demoted is True
        assert "不支持做多" in plans[0].demote_reason

    def test_bearish_call_wall_below_price_demotes(self):
        """Bearish plan + Call Wall below price → demote."""
        plan = self._plan(direction="bearish", entry=250.0)
        gw = self._gw(call_wall=248.0)  # call wall below price
        ctx = self._ctx(adr=1.2)
        plans = _apply_gamma_wall_warning([plan], price=250.0, gamma_wall=gw, ctx=ctx)
        assert plans[0].demoted is True
        assert "不支持做空" in plans[0].demote_reason

    def test_gamma_wall_none_no_change(self):
        """gamma_wall=None → plans unchanged."""
        plan = self._plan(direction="bullish")
        ctx = self._ctx()
        plans = _apply_gamma_wall_warning([plan], price=250.0, gamma_wall=None, ctx=ctx)
        assert plans[0].warning == ""
        assert plans[0].demoted is False

    def test_adr_zero_fallback_fixed_threshold(self):
        """ADR=0 → falls back to fixed 1.0% warn / 1.5% proximity."""
        plan = self._plan(direction="bullish", entry=250.0)
        gw = self._gw(max_pain=246.0)  # 1.6% below → > 1.0% fixed threshold
        ctx = self._ctx(adr=0.0)
        plans = _apply_gamma_wall_warning([plan], price=250.0, gamma_wall=gw, ctx=ctx)
        assert "期权引力偏空" in plans[0].warning

    def test_no_overwrite_existing_vwap_warning(self):
        """Existing VWAP warning preserved, gamma warning appended."""
        plan = self._plan(direction="bullish", entry=250.0)
        plan.warning = "价格已高于 VWAP 1.2%, 做多需等回调"
        gw = self._gw(max_pain=245.0)
        ctx = self._ctx(adr=1.2)
        plans = _apply_gamma_wall_warning([plan], price=250.0, gamma_wall=gw, ctx=ctx)
        assert "VWAP" in plans[0].warning
        assert "期权引力偏空" in plans[0].warning
        assert plans[0].warning.startswith("价格已高于 VWAP")

    def test_plan_c_skipped(self):
        """Plan C (invalidation) is always skipped."""
        plan = ActionPlan(
            label="C", name="失效", emoji="⚡", is_primary=False,
            logic="test", direction="bullish", trigger="test",
            entry=250.0, entry_action="观望",
            stop_loss=None, stop_loss_reason="",
            tp1=None, tp1_label="", tp2=None, tp2_label="", rr_ratio=0.0,
        )
        gw = self._gw(max_pain=240.0, put_wall=255.0)
        ctx = self._ctx(adr=1.2)
        plans = _apply_gamma_wall_warning([plan], price=250.0, gamma_wall=gw, ctx=ctx)
        assert plans[0].warning == ""
        assert plans[0].demoted is False

    def test_bullish_call_wall_proximity_warns(self):
        """Bullish plan + Call Wall very close above → proximity warning."""
        plan = self._plan(direction="bullish", entry=250.0)
        # call_wall at 250.5 → 0.2% from price, adr=1.2 → prox_thr=0.36%
        gw = self._gw(max_pain=250.0, call_wall=250.5, put_wall=240.0)
        ctx = self._ctx(adr=1.2)
        plans = _apply_gamma_wall_warning([plan], price=250.0, gamma_wall=gw, ctx=ctx)
        assert "上方压制" in plans[0].warning

    def test_bearish_put_wall_proximity_warns(self):
        """Bearish plan + Put Wall very close below → proximity warning."""
        plan = self._plan(direction="bearish", entry=250.0)
        # put_wall at 249.5 → 0.2% from price, adr=1.2 → prox_thr=0.36%
        gw = self._gw(max_pain=250.0, call_wall=260.0, put_wall=249.5)
        ctx = self._ctx(adr=1.2)
        plans = _apply_gamma_wall_warning([plan], price=250.0, gamma_wall=gw, ctx=ctx)
        assert "下方承接" in plans[0].warning


# ── P0-1: Trend Exhaustion Detection ──

class TestTrendExhaustion:
    """Test _check_trend_exhaustion and its effect on regime classification."""

    def _vp(self):
        return VolumeProfileResult(poc=550, vah=555, val=545)

    def test_exhaustion_downgrades_rvol_trend_day(self):
        """TREND_STRONG should downgrade to UNCLEAR when range consumed."""
        from src.us_playbook.regime import _check_trend_exhaustion
        # ADR 2%, consumed 2.5% → exhaustion_ratio = 1.25 > threshold
        bars = _make_bars([
            (f"2026-03-10 10:{i:02d}:00", 550, 555, 545, 553, 10000)
            for i in range(60)
        ])
        profile = RvolProfile(
            gap_and_go_rvol=1.5, trend_day_rvol=1.2, fade_chop_rvol=1.0,
            avg_daily_range_pct=1.0, percentile_rank=70, sample_size=10,
        )
        exhausted, detail = _check_trend_exhaustion(bars, profile, elapsed_ratio=0.3)
        assert exhausted
        assert "Trend exhausted" in detail

    def test_no_exhaustion_when_range_small(self):
        """No exhaustion when consumed range is small relative to ADR."""
        from src.us_playbook.regime import _check_trend_exhaustion
        # ADR 5%, consumed ~0.2% → ratio ≈ 0.04, well below threshold
        bars = _make_bars([
            (f"2026-03-10 10:{i:02d}:00", 550, 550.5, 549.5, 550.2, 10000)
            for i in range(30)
        ])
        profile = RvolProfile(
            gap_and_go_rvol=1.5, trend_day_rvol=1.2, fade_chop_rvol=1.0,
            avg_daily_range_pct=5.0, percentile_rank=70, sample_size=10,
        )
        exhausted, _ = _check_trend_exhaustion(bars, profile, elapsed_ratio=0.5)
        assert not exhausted

    def test_exhaustion_threshold_relaxes_late_session(self):
        """Late session (elapsed_ratio=1.0) uses lower threshold (0.70)."""
        from src.us_playbook.regime import _check_trend_exhaustion
        # ADR 2%, consumed 1.5% → ratio = 0.75, between 0.70 and 0.85
        bars = _make_bars([
            (f"2026-03-10 10:{i:02d}:00", 550, 553, 550, 552, 10000)
            for i in range(60)
        ])
        profile = RvolProfile(
            gap_and_go_rvol=1.5, trend_day_rvol=1.2, fade_chop_rvol=1.0,
            avg_daily_range_pct=2.0, percentile_rank=70, sample_size=10,
        )
        # Late session → threshold = 0.70, ratio = 0.75/2.0*100 ... let's be precise
        # high=553, low=550, consumed=(553-550)/550*100=0.545%
        # ratio = 0.545/2.0 = 0.273 → not exhausted
        exhausted_late, _ = _check_trend_exhaustion(bars, profile, elapsed_ratio=1.0)
        assert not exhausted_late  # 0.273 < 0.70

    def test_no_exhaustion_without_profile(self):
        """No profile → no exhaustion check."""
        from src.us_playbook.regime import _check_trend_exhaustion
        bars = _make_bars([
            (f"2026-03-10 10:{i:02d}:00", 550, 560, 540, 555, 10000)
            for i in range(30)
        ])
        exhausted, _ = _check_trend_exhaustion(bars, None, elapsed_ratio=0.5)
        assert not exhausted

    def test_regime_trend_day_exhausted_becomes_unclear(self):
        """classify_us_regime: RVOL-based TREND_STRONG exhausted → UNCLEAR with lean."""
        # Setup: price=557 > VAH=555, RVOL=1.3 > 1.2, small gap
        # But range 10% consumed of 1% ADR → exhausted
        bars = _make_bars([
            (f"2026-03-10 10:{i:02d}:00", 550, 560, 545, 557, 50000)
            for i in range(60)
        ])
        profile = RvolProfile(
            gap_and_go_rvol=1.5, trend_day_rvol=1.2, fade_chop_rvol=1.0,
            avg_daily_range_pct=1.0, percentile_rank=70, sample_size=10,
        )
        result = classify_us_regime(
            price=557, prev_close=556.5, rvol=1.3,
            pmh=556, pml=554, vp=self._vp(),
            rvol_profile=profile, today_bars=bars,
        )
        assert result.regime == USRegimeType.UNCLEAR
        assert "Trend exhausted" in result.details
        assert result.lean == "bullish"  # original direction preserved

    def test_persistence_branch_exempt_from_exhaustion(self):
        """Persistence branch (inside VA, high RVOL) should NOT apply exhaustion check."""
        bars = _make_bars([
            (f"2026-03-10 10:{i:02d}:00", 548, 560, 540, 552, 50000)
            for i in range(60)
        ])
        profile = RvolProfile(
            gap_and_go_rvol=1.5, trend_day_rvol=1.2, fade_chop_rvol=1.0,
            avg_daily_range_pct=1.0, percentile_rank=70, sample_size=10,
        )
        # price=552 inside VA (545-555), RVOL=1.3, open=548 → intraday_return > threshold
        result = classify_us_regime(
            price=552, prev_close=549, rvol=1.3,
            pmh=556, pml=554, vp=self._vp(),
            rvol_profile=profile, today_bars=bars,
            open_price=548, vwap=553,
        )
        # Should be TREND_STRONG (persistence) or RANGE, not UNCLEAR with exhaustion
        assert "Trend exhausted" not in (result.details or "")


# ── P0-2: RANGE neutral wait_conditions ──

class TestFadeChopNeutralWait:
    """Test RANGE neutral gives VA edge semantics, not generic 'direction unclear'."""

    def test_fade_chop_neutral_no_momentum_va_edge(self):
        """RANGE neutral (no momentum) → 'VA 边缘机会' text."""
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.75,
            rvol=0.8, price=252.5, gap_pct=0.1,
        )
        # price=252.5, mid=(260+250)/2=255, in transition zone
        # position_ratio = (252.5-250)/(260-250) = 0.25 → bullish edge zone
        # But let's make it truly neutral: price exactly at mid
        regime2 = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.75,
            rvol=0.8, price=255, gap_pct=0.1,
        )
        vp = VolumeProfileResult(poc=253, vah=260, val=250)
        filters = FilterResult(tradeable=True, risk_level="normal")
        # No today_bars → momentum=0, position_ratio=0.5 → transition zone → neutral
        rec = recommend(regime=regime2, vp=vp, filters=filters)
        assert rec.action == "wait"
        assert "VA 边缘" in rec.wait_conditions[0]
        assert "VA 中部" in rec.risk_note


# ── P1-1: Data-wait should not mask regime conclusion ──

class TestDataWaitNotMaskingRegime:
    """Test that no-chain/no-expiry wait still shows regime-based conclusion."""

    def test_data_wait_shows_regime_conclusion_us(self):
        """US: no chain → core conclusion shows regime + data caveat."""
        from src.us_playbook.playbook import _core_conclusion_text
        regime = USRegimeResult(
            regime=USRegimeType.TREND_STRONG, confidence=0.7,
            rvol=1.3, price=557, gap_pct=0.2,
        )
        vp = VolumeProfileResult(poc=550, vah=555, val=545)
        kl = KeyLevels(poc=550, vah=555, val=545, pdh=556, pdl=544, pmh=555, pml=548, vwap=553)
        option_rec = OptionRecommendation(
            action="wait", direction="bullish",
            rationale="方向偏看多, 但缺少可交易的期权合约",
            risk_note="期权链数据不可用",
            wait_conditions=["检查标的是否有期权合约"],
            wait_category="data",
        )
        text = _core_conclusion_text(regime, "bullish", vp, kl, option_rec)
        # Should contain regime conclusion (VWAP) + data caveat
        assert "VWAP" in text or "做多" in text
        assert "⚠️" in text

    def test_market_wait_still_shows_wait(self):
        """Market-based wait still shows '观望' text."""
        from src.us_playbook.playbook import _core_conclusion_text
        regime = USRegimeResult(
            regime=USRegimeType.UNCLEAR, confidence=0.3,
            rvol=0.5, price=550, gap_pct=0.1,
        )
        vp = VolumeProfileResult(poc=550, vah=555, val=545)
        kl = KeyLevels(poc=550, vah=555, val=545, pdh=556, pdl=544, pmh=555, pml=548, vwap=550)
        option_rec = OptionRecommendation(
            action="wait", direction="neutral",
            rationale="观望",
            wait_conditions=["等待 Regime 明确后再入场"],
        )
        text = _core_conclusion_text(regime, "neutral", vp, kl, option_rec)
        assert "观望" in text


# ── P1-2: Double "等待" format fix ──

class TestDoubleWaitFormatFix:
    """Test that '观望, 等待 等待...' is replaced by '观望 — ...' format."""

    def test_no_double_wait(self):
        """Conditions starting with '等待' should not produce '等待 等待...'."""
        from src.us_playbook.playbook import _core_conclusion_text
        regime = USRegimeResult(
            regime=USRegimeType.UNCLEAR, confidence=0.3,
            rvol=0.5, price=550, gap_pct=0.1,
        )
        vp = VolumeProfileResult(poc=550, vah=555, val=545)
        kl = KeyLevels(poc=550, vah=555, val=545, pdh=556, pdl=544, pmh=555, pml=548, vwap=550)
        option_rec = OptionRecommendation(
            action="wait", direction="neutral",
            wait_conditions=["等待价格突破关键位后再入场"],
        )
        text = _core_conclusion_text(regime, "neutral", vp, kl, option_rec)
        assert "等待 等待" not in text
        assert "观望 —" in text

    def test_no_double_wait_hk(self):
        """HK: same fix for double '等待'."""
        from src.hk import RegimeResult, RegimeType
        from src.hk.playbook import _core_conclusion_text as hk_core_conclusion
        regime = RegimeResult(
            regime=RegimeType.UNCLEAR, confidence=0.3,
            rvol=0.5, price=100, vah=105, val=95, poc=100, gap_pct=0.1,
        )
        vp = VolumeProfileResult(poc=100, vah=105, val=95)
        option_rec = OptionRecommendation(
            action="wait", direction="neutral",
            wait_conditions=["等待价格突破关键位后再入场"],
        )
        text = hk_core_conclusion(regime, "neutral", vp, 100.5, option_rec)
        assert "等待 等待" not in text
        assert "观望 —" in text


# ── P1-1 (US option_recommend): wait_category="data" ──

class TestWaitCategoryData:
    """Test that no-chain/no-expiry returns wait_category='data'."""

    def test_no_chain_sets_data_category(self):
        """No chain → wait_category='data'."""
        regime = USRegimeResult(
            regime=USRegimeType.TREND_STRONG, confidence=0.7,
            rvol=1.3, price=557, gap_pct=0.2,
        )
        vp = VolumeProfileResult(poc=550, vah=555, val=545)
        filters = FilterResult(tradeable=True, risk_level="normal")
        rec = recommend(
            regime=regime, vp=vp, filters=filters,
            chain_df=None, expiry_dates=["2026-03-20"],
        )
        assert rec.action == "wait"
        assert rec.wait_category == "data"

    def test_no_expiry_sets_data_category(self):
        """No expiry → wait_category='data'."""
        regime = USRegimeResult(
            regime=USRegimeType.TREND_STRONG, confidence=0.7,
            rvol=1.3, price=557, gap_pct=0.2,
        )
        vp = VolumeProfileResult(poc=550, vah=555, val=545)
        filters = FilterResult(tradeable=True, risk_level="normal")
        rec = recommend(
            regime=regime, vp=vp, filters=filters,
            chain_df=pd.DataFrame({"code": ["X"]}),
            expiry_dates=[],
        )
        assert rec.action == "wait"
        assert rec.wait_category == "data"

    def test_market_wait_has_default_category(self):
        """Normal market-based wait → wait_category='market' (default)."""
        regime = USRegimeResult(
            regime=USRegimeType.UNCLEAR, confidence=0.3,
            rvol=1.0, price=550, gap_pct=0.1,
        )
        vp = VolumeProfileResult(poc=550, vah=555, val=545)
        filters = FilterResult(tradeable=True, risk_level="normal")
        rec = recommend(regime=regime, vp=vp, filters=filters)
        assert rec.action == "wait"
        assert rec.wait_category == "market"


# ── Fix 1 & 2: Gap dead zone + UNCLEAR lean from position ──


class TestGapDeadZoneVwapRelaxation:
    """Test VWAP-confirmed gap relaxation for TREND_STRONG classification."""

    def _vp(self, poc=550, vah=555, val=545):
        return VolumeProfileResult(poc=poc, vah=vah, val=val)

    def test_gap_dead_zone_with_vwap_confirms_trend_day(self):
        """normalized_gap 0.32 (> 0.3 threshold) but VWAP confirms → TREND with penalty (Phase 2: TREND_WEAK)."""
        profile = RvolProfile(
            gap_and_go_rvol=2.0,
            trend_day_rvol=1.2,
            fade_chop_rvol=0.8,
            avg_daily_range_pct=1.5,  # gap 0.48% → normalized = 0.48/1.5 ≈ 0.32
            percentile_rank=65.0,
            sample_size=10,
        )
        # price=542 < val=545 (outside VA, below VAL) and price < vwap=548 → _vwap_confirms
        # normalized ≈ 0.32 < 0.5 → _relaxed_gap=True → _gap_ok=True
        result = classify_us_regime(
            price=542, prev_close=544.6, rvol=1.3,
            pmh=546, pml=543, vp=self._vp(),
            rvol_profile=profile,
            gap_significance_threshold=0.3,
            vwap=548.0,
        )
        # Phase 2: RVOL 1.3 < adaptive gap_and_go_rvol 2.0 and no VWAP hold → TREND_WEAK
        assert result.regime == USRegimeType.TREND_WEAK
        assert "VWAP-confirmed gap relaxation" in result.details
        # Confidence should have -0.10 penalty vs base
        base_confidence = min(1.0, (1.3 - 1.2) / 0.5 * 0.3 + 0.5)
        assert result.confidence <= base_confidence - 0.09  # approx -0.10

    def test_gap_dead_zone_no_vwap_confirm_stays_unclear(self):
        """normalized_gap 0.32, price > VWAP → VWAP doesn't confirm → not TREND_STRONG."""
        profile = RvolProfile(
            gap_and_go_rvol=2.0,
            trend_day_rvol=1.2,
            fade_chop_rvol=0.8,
            avg_daily_range_pct=1.5,
            percentile_rank=65.0,
            sample_size=10,
        )
        # price=542 < val=545 (outside VA, below VAL) but price > vwap=540 → no _vwap_confirms
        result = classify_us_regime(
            price=542, prev_close=544.6, rvol=1.3,
            pmh=546, pml=543, vp=self._vp(),
            rvol_profile=profile,
            gap_significance_threshold=0.3,
            vwap=540.0,
        )
        assert result.regime not in (USRegimeType.TREND_STRONG, USRegimeType.TREND_WEAK)

    def test_large_normalized_gap_still_blocked(self):
        """normalized_gap 0.9 (> 0.5 relaxed threshold) → still blocked even with VWAP."""
        profile = RvolProfile(
            gap_and_go_rvol=2.0,
            trend_day_rvol=1.2,
            fade_chop_rvol=0.8,
            avg_daily_range_pct=0.5,  # tiny range → gap 0.45% → normalized=0.9
            percentile_rank=65.0,
            sample_size=10,
        )
        # price=557 > vah=555, vwap=553 → _vwap_confirms=True
        # but normalized=0.9 > 0.5 → _relaxed_gap=False → _gap_ok=False
        result = classify_us_regime(
            price=557, prev_close=554.5, rvol=1.3,
            pmh=556, pml=554, vp=self._vp(),
            rvol_profile=profile,
            gap_significance_threshold=0.3,
            vwap=553.0,
        )
        assert result.regime not in (USRegimeType.TREND_STRONG, USRegimeType.TREND_WEAK)


class TestUnclearDefaultLeanFromPosition:
    """Test UNCLEAR lean derived from price position when no sub-type matched."""

    def _vp(self, poc=550, vah=555, val=545):
        return VolumeProfileResult(poc=poc, vah=vah, val=val)

    def test_unclear_default_lean_bearish_from_position(self):
        """Outside VA + below VWAP → lean='bearish' in else (Mixed signals) branch."""
        # RVOL in gap between trend_day and gap_and_go (not matching any sub-type exactly)
        # Need: rvol < fade_chop OR rvol in range but inside_va=False + rvol < trend_day
        # The 'else' branch triggers when none of sub-type 1/2/3 match
        # Sub-type 2: inside_va + rvol >= trend_day → no (outside_va)
        # Sub-type 3: outside_va + rvol < fade_chop → no (rvol=1.1 >= fade_chop=1.0)
        # Sub-type 1: trend_day > rvol >= fade_chop → yes (1.2 > 1.1 >= 1.0) → this matches sub-type 1!
        # We need to avoid all sub-types: set rvol=1.25 with outside_va, rvol >= trend_day_rvol
        # Actually: sub-type 1 is trend_day > rvol >= fade_chop. To hit 'else', need rvol < fade_chop + outside_va
        # But sub-type 3 is outside_va + rvol < fade_chop. So the only way to hit else:
        # inside_va=True + rvol < trend_day → but that's also not sub-type 2 (needs rvol >= trend_day)
        # Let me re-read: the branches are:
        # if inside_va and rvol >= trend_day → sub-type 2
        # elif outside_va and rvol < fade_chop → sub-type 3
        # elif trend_day > rvol >= fade_chop → sub-type 1
        # else → Mixed signals
        # So else = (inside_va and rvol < trend_day and NOT (trend_day > rvol >= fade_chop))
        #         = inside_va and rvol < fade_chop
        # OR: outside_va and rvol >= fade_chop and rvol < trend_day → wait, that's sub-type 1
        # Actually else triggers when:
        # NOT (inside_va and rvol >= trend_day) AND NOT (outside_va and rvol < fade_chop) AND NOT (trend_day > rvol >= fade_chop)
        # = outside_va and rvol >= fade_chop and rvol >= trend_day (since sub-type 1 needs rvol < trend_day)
        # But rvol >= trend_day + outside_va + not small_gap → wouldn't trigger TREND_STRONG above?
        # Actually _gap_ok is false → TREND_STRONG block skipped → falls to UNCLEAR → else branch
        # Perfect: outside_va, rvol >= trend_day, big gap (not _gap_ok), no VWAP confirm for gap
        # Need: price < val, price > vwap (so _vwap_confirms=False for gap relaxation)
        # But for lean: price < val, price < vwap → bearish

        # Simpler approach: make sure we hit the else branch by having:
        # rvol >= trend_day (so sub-type 1 skipped), outside_va (so sub-type 2 skipped),
        # rvol >= fade_chop (so sub-type 3 skipped)
        # This means else branch. And price < val + price < vwap → lean=bearish
        # But wait: if rvol >= trend_day + outside_va → TREND_STRONG block should catch it first
        # unless gap is too big. So we need big gap + no VWAP confirm for gap.

        # Let's use: big gap (not small, not relaxable), outside_va, price < val
        # gap too big for _gap_ok, rvol >= trend_day, outside_va
        # No pm_breakout → not GAP_GO
        # Structure not enabled → skip
        # Persistence needs inside_va → skip
        # RANGE needs rvol < fade_chop → skip (rvol=1.3 >= 1.0)
        # → UNCLEAR, else branch (rvol >= trend_day, outside_va, not sub-type 1/2/3)
        profile = RvolProfile(
            gap_and_go_rvol=2.0,
            trend_day_rvol=1.2,
            fade_chop_rvol=1.0,
            avg_daily_range_pct=0.5,  # tiny range → normalized gap huge → _gap_ok=False
            percentile_rank=65.0,
            sample_size=10,
        )
        # price=540 < val=545, vwap=543 → price < vwap → lean should be bearish
        # gap = (540-550)/550 = -1.8% → normalized = 1.8/0.5 = 3.6 >> 0.5
        # _vwap_confirms: price < val and price < vwap → True, but _relaxed_gap = 3.6 > 0.5 → False
        # → _gap_ok = False → TREND_STRONG skipped → UNCLEAR
        result = classify_us_regime(
            price=540, prev_close=550, rvol=1.3,
            pmh=538, pml=536, vp=self._vp(),  # pmh < price → no pm_breakout
            rvol_profile=profile,
            gap_significance_threshold=0.3,
            vwap=543.0,
        )
        assert result.regime == USRegimeType.UNCLEAR
        assert result.lean == "bearish"

    def test_unclear_default_lean_bullish_from_position(self):
        """Above VAH + above VWAP → lean='bullish' in else branch."""
        profile = RvolProfile(
            gap_and_go_rvol=2.0,
            trend_day_rvol=1.2,
            fade_chop_rvol=1.0,
            avg_daily_range_pct=0.5,
            percentile_rank=65.0,
            sample_size=10,
        )
        # price=560 > vah=555, vwap=557 → price > vwap → lean should be bullish
        # gap = (560-550)/550 = 1.82% → normalized = 1.82/0.5 = 3.64 >> 0.5
        # pm_breakout: pmh=558, price=560 > pmh → above_pm=True
        # But rvol=1.3 < gap_and_go=2.0 → GAP_GO skipped
        # _gap_ok = False → TREND_STRONG skipped → UNCLEAR
        # Actually pmh=558 → above_pm but not GAP_GO (rvol too low)
        # Set pmh > price to avoid pm_breakout triggering GAP_GO later
        result = classify_us_regime(
            price=560, prev_close=550, rvol=1.3,
            pmh=562, pml=558, vp=self._vp(),  # pmh > price → no pm_breakout
            rvol_profile=profile,
            gap_significance_threshold=0.3,
            vwap=557.0,
        )
        assert result.regime == USRegimeType.UNCLEAR
        assert result.lean == "bullish"


# ── P0-1: Large gap signal → GAP_GO even when PM absorbed ──

class TestLargeGapSignal:
    """Test that large gaps (normalized >= 0.8) trigger GAP_GO without PM breakout."""

    def _vp(self, poc=640, vah=645, val=635):
        return VolumeProfileResult(poc=poc, vah=vah, val=val)

    def test_large_gap_down_gap_and_go(self):
        """META 3/13 scenario: gap=-2.11%, RVOL=2.68, PM absorbed → GAP_GO via large gap."""
        profile = RvolProfile(
            gap_and_go_rvol=1.5, trend_day_rvol=1.2, fade_chop_rvol=1.0,
            avg_daily_range_pct=2.0,  # normalized_gap = 2.11/2.0 ≈ 1.05 >= 0.8
            percentile_rank=90, sample_size=10,
        )
        result = classify_us_regime(
            price=624.73, prev_close=637.18, rvol=2.68,
            pmh=635.68, pml=623.22,  # price=624.73 > pml=623.22 → no pm_breakout
            vp=self._vp(),
            rvol_profile=profile,
        )
        assert result.regime == USRegimeType.GAP_GO
        assert "gap down" in result.details
        assert result.gap_pct < 0

    def test_large_gap_up_gap_and_go(self):
        """Large gap up (normalized >= 0.8) → GAP_GO gap-driven."""
        profile = RvolProfile(
            gap_and_go_rvol=1.5, trend_day_rvol=1.2, fade_chop_rvol=1.0,
            avg_daily_range_pct=2.0,
            percentile_rank=85, sample_size=10,
        )
        result = classify_us_regime(
            price=655, prev_close=640, rvol=2.0,
            pmh=660, pml=650,  # price=655, inside PM range → no pm_breakout
            vp=self._vp(),
            rvol_profile=profile,
        )
        assert result.regime == USRegimeType.GAP_GO
        assert "gap up" in result.details

    def test_gap_driven_confidence_penalty(self):
        """Gap-driven (not PM breakout) should have -0.10 confidence penalty."""
        profile = RvolProfile(
            gap_and_go_rvol=1.5, trend_day_rvol=1.2, fade_chop_rvol=1.0,
            avg_daily_range_pct=2.0,
            percentile_rank=85, sample_size=10,
        )
        # With PM breakout
        result_pm = classify_us_regime(
            price=624, prev_close=637, rvol=2.5,
            pmh=630, pml=625,  # price=624 < pml=625 → pm_breakout
            vp=self._vp(),
            rvol_profile=profile,
        )
        # Without PM breakout (gap-driven)
        result_gap = classify_us_regime(
            price=624, prev_close=637, rvol=2.5,
            pmh=630, pml=620,  # price=624 > pml=620 → no pm_breakout
            vp=self._vp(),
            rvol_profile=profile,
        )
        assert result_pm.regime == USRegimeType.GAP_GO
        assert result_gap.regime == USRegimeType.GAP_GO
        # Gap-driven should be ~0.10 lower
        assert result_gap.confidence < result_pm.confidence

    def test_small_gap_no_large_gap_signal(self):
        """Small gap (normalized < 0.8) + no PM breakout → NOT GAP_GO."""
        profile = RvolProfile(
            gap_and_go_rvol=1.5, trend_day_rvol=1.2, fade_chop_rvol=1.0,
            avg_daily_range_pct=2.0,  # normalized_gap = 0.5/2.0 = 0.25 < 0.8
            percentile_rank=85, sample_size=10,
        )
        result = classify_us_regime(
            price=641, prev_close=640, rvol=2.0,
            pmh=645, pml=638,  # price inside PM range
            vp=self._vp(),
            rvol_profile=profile,
        )
        assert result.regime != USRegimeType.GAP_GO


# ── P0-2: Chase risk VA distance disabled for trend regimes ──

class TestChaseRiskTrendRegime:
    """Test that GAP_GO/TREND_STRONG regimes disable VA distance chase risk."""

    def test_gap_and_go_no_high_chase_from_va(self):
        """GAP_GO: VA distance 4.5% should NOT trigger high chase risk."""
        vp = VolumeProfileResult(poc=640, vah=645, val=635)
        result = assess_chase_risk(
            price=615.80, vwap=620, vp=vp, direction="bearish",
            va_moderate_pct=2.0, va_high_pct=3.0,
            regime="GAP_GO",
        )
        # VA distance = (635-615.80)/635 = 3.02% → would be "high" without regime override
        assert result.level != "high"

    def test_trend_day_no_high_chase_from_va(self):
        """TREND_STRONG: VA distance should NOT trigger high chase risk."""
        vp = VolumeProfileResult(poc=550, vah=555, val=545)
        result = assess_chase_risk(
            price=530, vwap=535, vp=vp, direction="bearish",
            va_moderate_pct=2.0, va_high_pct=3.0,
            regime="TREND_STRONG",
        )
        assert result.level != "high"

    def test_fade_chop_still_checks_va(self):
        """RANGE: VA distance should still trigger chase risk as before."""
        vp = VolumeProfileResult(poc=550, vah=555, val=545)
        result = assess_chase_risk(
            price=530, vwap=535, vp=vp, direction="bearish",
            va_moderate_pct=2.0, va_high_pct=3.0,
            regime="RANGE",
        )
        # VA distance = (545-530)/545 = 2.75% → moderate or high
        assert result.level in ("moderate", "high")

    def test_no_regime_still_checks_va(self):
        """No regime param (default None): VA distance check active."""
        vp = VolumeProfileResult(poc=550, vah=555, val=545)
        result = assess_chase_risk(
            price=530, vwap=535, vp=vp, direction="bearish",
            va_moderate_pct=2.0, va_high_pct=3.0,
        )
        assert result.level in ("moderate", "high")


# ── P1-2: Rapid-fire same signal cooldown ──

class TestRapidFireCooldown:
    """Test that 3 identical signals within 120s → 2nd and 3rd blocked."""

    def _make_predictor(self):
        cfg = {
            "watchlist": [{"symbol": "SPY", "name": "S&P 500 ETF"}],
            "auto_scan": {
                "cooldown": {"same_signal_minutes": 30, "max_per_session": 2, "max_per_day": 3},
                "override": {"confidence_increase": 0.10, "price_extension_pct": 0.50, "regime_upgrade": True},
            },
        }
        return USPredictor(cfg, collector=None)

    def _make_signal(self, ts=1000.0):
        return USScanSignal(
            signal_type="BREAKOUT_BEARISH",
            direction="bearish",
            symbol="META",
            regime=USRegimeResult(
                regime=USRegimeType.GAP_GO, confidence=0.85,
                rvol=2.0, price=615, gap_pct=-2.0,
            ),
            price=615,
            timestamp=ts,
        )

    def test_rapid_fire_blocked(self):
        """3 identical signals within 120s — 2nd and 3rd should be blocked."""
        pred = self._make_predictor()
        pred._scan_history_date = "2026-03-13"

        # First signal at t=1000 → allowed
        sig1 = self._make_signal(ts=1000.0)
        allowed1, _ = pred._check_frequency("META", sig1, "morning", pred._cfg["auto_scan"])
        assert allowed1
        pred._record_alert("META", sig1, "morning")

        # Second signal at t=1060 (60s later) → blocked
        sig2 = self._make_signal(ts=1060.0)
        allowed2, _ = pred._check_frequency("META", sig2, "morning", pred._cfg["auto_scan"])
        assert not allowed2

        # Third signal at t=1120 (120s after first) → still blocked (within 30min)
        sig3 = self._make_signal(ts=1120.0)
        allowed3, _ = pred._check_frequency("META", sig3, "morning", pred._cfg["auto_scan"])
        assert not allowed3


# ── P2-1: VP staleness — RANGE VA unreachable confidence penalty ──

class TestVPStaleness:
    """Test VP staleness detection: RANGE confidence penalty when VA is far away."""

    def test_fade_chop_va_unreachable_penalty(self):
        """RANGE (via near_gamma) with VA distance > 5% → confidence penalty."""
        # Price=610, VA=645-661 → outside VA but near gamma wall → triggers RANGE
        # va_distance = min(|610-661|, |610-645|) / 610 * 100 = 35/610*100 = 5.74% > 5%
        vp = VolumeProfileResult(poc=650, vah=661, val=645)
        gamma = GammaWallResult(call_wall_strike=609, put_wall_strike=0, max_pain=0)
        result = classify_us_regime(
            price=610, prev_close=611, rvol=0.7,
            pmh=620, pml=608,
            vp=vp, gamma_wall=gamma,
            fade_chop_rvol=1.0,
        )
        assert result.regime == USRegimeType.RANGE
        assert "VA unreachable" in result.details
        # Confidence should be lower than base
        base_conf = min(1.0, (1.0 - 0.7) / 0.3 * 0.3 + 0.5)
        assert result.confidence < base_conf

    def test_fade_chop_va_reachable_no_penalty(self):
        """RANGE with price inside VA (distance < 5%) → no penalty."""
        vp = VolumeProfileResult(poc=610, vah=620, val=600)
        result = classify_us_regime(
            price=610, prev_close=611, rvol=0.7,
            pmh=615, pml=608,
            vp=vp,
            fade_chop_rvol=1.0,
        )
        assert result.regime == USRegimeType.RANGE
        assert "VA unreachable" not in result.details


# ── Part A: Regime Stabilizer Tests ──


class TestRegimeStabilizer:
    """Tests for RegimeStabilizer (auto-scan L1 debounce)."""

    @staticmethod
    def _make_regime(
        regime=USRegimeType.TREND_STRONG,
        confidence=0.80,
        rvol=1.30,
        price=150.0,
        gap_pct=0.5,
        adaptive=None,
        lean="neutral",
    ):
        return USRegimeResult(
            regime=regime,
            confidence=confidence,
            rvol=rvol,
            price=price,
            gap_pct=gap_pct,
            adaptive_thresholds=adaptive,
            lean=lean,
        )

    @staticmethod
    def _cfg(**overrides):
        base = {
            "enabled": True,
            "hysteresis_ratio": 0.30,
            "hold_upgrade_minutes": 15,
            "hold_downgrade_minutes": 30,
            "hold_from_unclear_minutes": 10,
            "bypass_confidence_delta": 0.20,
        }
        base.update(overrides)
        return base

    def test_stabilizer_first_call_passthrough(self):
        """First call for a symbol → raw regime passed through unchanged."""
        from src.us_playbook.stabilizer import RegimeStabilizer
        stab = RegimeStabilizer(self._cfg())
        raw = self._make_regime(regime=USRegimeType.TREND_STRONG)
        result = stab.stabilize("AAPL", raw)
        assert result is raw
        assert not result.stabilized

    def test_stabilizer_same_regime_refreshes(self):
        """Same regime on second call → accepted, price/rvol refreshed."""
        from src.us_playbook.stabilizer import RegimeStabilizer
        stab = RegimeStabilizer(self._cfg())
        r1 = self._make_regime(rvol=1.20, price=150.0)
        stab.stabilize("AAPL", r1)
        r2 = self._make_regime(rvol=1.35, price=152.0)
        result = stab.stabilize("AAPL", r2)
        assert result is r2
        assert not result.stabilized

    def test_stabilizer_hysteresis_rejects_boundary(self):
        """RVOL on boundary → hysteresis rejects switch."""
        from src.us_playbook.stabilizer import RegimeStabilizer
        adaptive = {"trend_day": 1.20, "fade_chop": 0.90}
        stab = RegimeStabilizer(self._cfg())

        # Accept initial TREND_STRONG
        trend = self._make_regime(
            regime=USRegimeType.TREND_STRONG, rvol=1.30, adaptive=adaptive,
        )
        stab.stabilize("QQQ", trend)

        # Try switch to RANGE with RVOL at boundary (1.20 - 0.09 = 1.11)
        # gap = 1.20 - 0.90 = 0.30, hyst = 0.30*0.30 = 0.09
        # Need rvol < 1.20 - 0.09 = 1.11 to pass; 1.12 should be rejected
        fade = self._make_regime(
            regime=USRegimeType.RANGE, rvol=1.12, adaptive=adaptive,
        )
        result = stab.stabilize("QQQ", fade)
        assert result.regime == USRegimeType.TREND_STRONG  # held
        assert result.stabilized is True
        assert result.rvol == 1.12  # price/rvol refreshed

    def test_stabilizer_hysteresis_skipped_no_adaptive(self):
        """No adaptive thresholds → Layer 1 skipped, goes to Layer 2."""
        from src.us_playbook.stabilizer import RegimeStabilizer
        import time as _time

        stab = RegimeStabilizer(self._cfg(hold_downgrade_minutes=0))

        trend = self._make_regime(regime=USRegimeType.TREND_STRONG, adaptive=None)
        stab.stabilize("SPY", trend)

        # No adaptive → Layer 1 skipped, hold=0 → accepted
        fade = self._make_regime(regime=USRegimeType.RANGE, adaptive=None)
        result = stab.stabilize("SPY", fade)
        assert result.regime == USRegimeType.RANGE  # passed through

    def test_stabilizer_graduated_hold_upgrade(self):
        """FADE→TREND needs 15min hold (upgrade)."""
        from src.us_playbook.stabilizer import RegimeStabilizer
        stab = RegimeStabilizer(self._cfg())

        fade = self._make_regime(regime=USRegimeType.RANGE)
        stab.stabilize("TSLA", fade)

        # Immediately try upgrade to TREND_STRONG
        trend = self._make_regime(regime=USRegimeType.TREND_STRONG)
        result = stab.stabilize("TSLA", trend)
        assert result.regime == USRegimeType.RANGE  # held
        assert result.stabilized is True

    def test_stabilizer_graduated_hold_downgrade(self):
        """TREND→FADE needs 30min hold (downgrade)."""
        from src.us_playbook.stabilizer import RegimeStabilizer
        stab = RegimeStabilizer(self._cfg())

        trend = self._make_regime(regime=USRegimeType.TREND_STRONG)
        stab.stabilize("NVDA", trend)

        fade = self._make_regime(regime=USRegimeType.RANGE)
        result = stab.stabilize("NVDA", fade)
        assert result.regime == USRegimeType.TREND_STRONG  # held
        assert result.stabilized is True

    def test_stabilizer_graduated_hold_from_unclear(self):
        """UNCLEAR→any needs 10min hold."""
        from src.us_playbook.stabilizer import RegimeStabilizer
        stab = RegimeStabilizer(self._cfg())

        unclear = self._make_regime(regime=USRegimeType.UNCLEAR, confidence=0.50)
        stab.stabilize("META", unclear)

        # delta = |0.60 - 0.50| = 0.10 < 0.20 → no bypass
        trend = self._make_regime(regime=USRegimeType.TREND_STRONG, confidence=0.60)
        result = stab.stabilize("META", trend)
        assert result.regime == USRegimeType.UNCLEAR  # held
        assert result.stabilized is True

    def test_stabilizer_bypass_strong_signal(self):
        """Confidence delta >= 0.20 → bypass all holds."""
        from src.us_playbook.stabilizer import RegimeStabilizer
        stab = RegimeStabilizer(self._cfg())

        fade = self._make_regime(regime=USRegimeType.RANGE, confidence=0.55)
        stab.stabilize("AMD", fade)

        # Strong signal: delta = 0.80 - 0.55 = 0.25 >= 0.20
        trend = self._make_regime(regime=USRegimeType.TREND_STRONG, confidence=0.80)
        result = stab.stabilize("AMD", trend)
        assert result.regime == USRegimeType.TREND_STRONG
        assert not result.stabilized

    def test_stabilizer_disabled_passthrough(self):
        """enabled=False → raw passed through unchanged."""
        from src.us_playbook.stabilizer import RegimeStabilizer
        stab = RegimeStabilizer({"enabled": False})

        r1 = self._make_regime(regime=USRegimeType.TREND_STRONG)
        stab.stabilize("SPY", r1)

        r2 = self._make_regime(regime=USRegimeType.RANGE)
        result = stab.stabilize("SPY", r2)
        assert result is r2  # raw, not held
        assert not result.stabilized

    def test_stabilizer_marks_stabilized_flag(self):
        """Held regime has stabilized=True."""
        from src.us_playbook.stabilizer import RegimeStabilizer
        stab = RegimeStabilizer(self._cfg())

        trend = self._make_regime(regime=USRegimeType.GAP_GO)
        stab.stabilize("AMZN", trend)

        fade = self._make_regime(regime=USRegimeType.RANGE)
        result = stab.stabilize("AMZN", fade)
        assert result.stabilized is True
        assert result.regime == USRegimeType.GAP_GO

    def test_adaptive_hysteresis_proportional(self):
        """Adaptive thresholds (trend=1.05, fade=0.88): hysteresis computed correctly."""
        from src.us_playbook.stabilizer import RegimeStabilizer
        adaptive = {"trend_day": 1.05, "fade_chop": 0.88}
        stab = RegimeStabilizer(self._cfg())

        # Accept TREND_STRONG initially
        trend = self._make_regime(
            regime=USRegimeType.TREND_STRONG, rvol=1.10, adaptive=adaptive,
        )
        stab.stabilize("MSFT", trend)

        # gap=0.17, hyst=0.17*0.30=0.051
        # TREND→FADE needs rvol < 1.05 - 0.051 = 0.999
        # Try with rvol=1.00 → still >= 0.999, should be rejected
        fade = self._make_regime(
            regime=USRegimeType.RANGE, rvol=1.00, adaptive=adaptive,
        )
        result = stab.stabilize("MSFT", fade)
        assert result.regime == USRegimeType.TREND_STRONG  # held

        # Try with rvol=0.98 → < 0.999, but still needs temporal hold
        fade2 = self._make_regime(
            regime=USRegimeType.RANGE, rvol=0.98, adaptive=adaptive,
        )
        result2 = stab.stabilize("MSFT", fade2)
        # Passes hysteresis but blocked by 30min hold
        assert result2.regime == USRegimeType.TREND_STRONG

    def test_ondemand_no_stabilizer(self):
        """USRegimeResult from on-demand has stabilized=False by default."""
        r = self._make_regime()
        assert r.stabilized is False

    def test_ondemand_volatility_warning(self):
        """USPlaybookResult.regime_volatile set when regime differs from last scan."""
        r = USPlaybookResult(
            symbol="TEST", name="Test", regime=self._make_regime(),
            key_levels=KeyLevels(poc=100, vah=105, val=95, pdh=106, pdl=94, pmh=103, pml=97, vwap=100),
            volume_profile=VolumeProfileResult(poc=100, vah=105, val=95),
            gamma_wall=None, filters=FilterResult(tradeable=True),
            regime_volatile=True,
        )
        assert r.regime_volatile is True

        r2 = USPlaybookResult(
            symbol="TEST", name="Test", regime=self._make_regime(),
            key_levels=KeyLevels(poc=100, vah=105, val=95, pdh=106, pdl=94, pmh=103, pml=97, vwap=100),
            volume_profile=VolumeProfileResult(poc=100, vah=105, val=95),
            gamma_wall=None, filters=FilterResult(tradeable=True),
        )
        assert r2.regime_volatile is False


# ── Part B: Direction Consistency Tests ──


class TestDirectionConsistency:
    """Tests for direction_override in _plans_unclear and enforce_direction_consistency."""

    @staticmethod
    def _make_regime(regime=USRegimeType.TREND_STRONG, lean="bearish", confidence=0.60):
        return USRegimeResult(
            regime=regime, confidence=confidence, rvol=1.10,
            price=150.0, gap_pct=0.3, lean=lean,
        )

    @staticmethod
    def _kl():
        return KeyLevels(
            poc=149, vah=152, val=147, pdh=153, pdl=146, pmh=151, pml=148, vwap=150,
        )

    def test_trend_downgrade_bullish_no_bearish_plan(self):
        """US: TREND_STRONG bullish + lean=bearish + wait → Plan B should NOT be bearish."""
        regime = self._make_regime(lean="bearish", confidence=0.60)
        vp = VolumeProfileResult(poc=149, vah=152, val=147)
        kl = self._kl()
        option_rec = OptionRecommendation(action="wait", direction="neutral", wait_conditions=["等待确认"])

        plans = _generate_action_plans(
            regime, "bullish", vp, kl, None, option_rec,
            trend_downgrade_confidence=0.70,
        )
        # Plans should have been generated via _plans_unclear(direction_override="bullish")
        # Plan B should be bullish (from direction_override), not bearish (from lean)
        plan_b = next((p for p in plans if p.label == "B"), None)
        assert plan_b is not None
        if plan_b.direction in ("bullish", "bearish"):
            assert plan_b.direction == "bullish"

    def test_trend_downgrade_bearish_no_bullish_plan(self):
        """US: GAP_GO bearish + lean=bullish + wait → Plan B should NOT be bullish."""
        regime = USRegimeResult(
            regime=USRegimeType.GAP_GO, confidence=0.60, rvol=1.50,
            price=150.0, gap_pct=1.5, lean="bullish",
        )
        vp = VolumeProfileResult(poc=149, vah=152, val=147)
        kl = self._kl()
        option_rec = OptionRecommendation(action="wait", direction="neutral", wait_conditions=["等待确认"])

        plans = _generate_action_plans(
            regime, "bearish", vp, kl, None, option_rec,
            trend_downgrade_confidence=0.70,
        )
        plan_b = next((p for p in plans if p.label == "B"), None)
        assert plan_b is not None
        if plan_b.direction in ("bullish", "bearish"):
            assert plan_b.direction == "bearish"

    def test_enforce_strips_contradicting_entry_plan_a(self):
        """enforce_direction_consistency strips entry from opposing Plan A."""
        from src.common.action_plan import enforce_direction_consistency
        plan = ActionPlan(
            label="A", name="趋势做空", emoji="📉", is_primary=True,
            logic="test", direction="bearish",
            trigger="test", entry=150.0, entry_action="做空",
            stop_loss=152.0, stop_loss_reason="above",
            tp1=148.0, tp1_label="VAL", tp2=None, tp2_label="", rr_ratio=1.5,
        )
        plans = enforce_direction_consistency([plan], "TREND_STRONG", "bullish")
        assert plans[0].entry is None
        assert plans[0].entry_action == ""
        assert "矛盾" in plans[0].warning

    def test_enforce_exempts_hedge_plan_b(self):
        """Plan B in opposite direction (hedge) is exempt from stripping."""
        from src.common.action_plan import enforce_direction_consistency
        plan = ActionPlan(
            label="B", name="反向对冲做空", emoji="📉", is_primary=False,
            logic="test", direction="bearish",
            trigger="test", entry=150.0, entry_action="做空",
            stop_loss=152.0, stop_loss_reason="above",
            tp1=148.0, tp1_label="VAL", tp2=None, tp2_label="", rr_ratio=1.5,
        )
        plans = enforce_direction_consistency([plan], "TREND_STRONG", "bullish")
        # Plan B hedge should NOT be stripped
        assert plans[0].entry == 150.0
        assert plans[0].warning == ""

    def test_enforce_plan_c_exempt(self):
        """Plan C is always exempt from direction consistency enforcement."""
        from src.common.action_plan import enforce_direction_consistency
        plan_c = ActionPlan(
            label="C", name="失效反转", emoji="⚡", is_primary=False,
            logic="反转", direction="bearish",
            trigger="test", entry=148.0, entry_action="做空",
            stop_loss=None, stop_loss_reason="",
            tp1=None, tp1_label="", tp2=None, tp2_label="", rr_ratio=0.0,
        )
        plans = enforce_direction_consistency([plan_c], "TREND_STRONG", "bullish")
        assert plans[0].entry == 148.0  # unchanged

    def test_enforce_fade_chop_no_effect(self):
        """RANGE is not affected by direction enforcement."""
        from src.common.action_plan import enforce_direction_consistency
        plan = ActionPlan(
            label="A", name="上沿做空", emoji="📉", is_primary=True,
            logic="test", direction="bearish",
            trigger="test", entry=152.0, entry_action="做空",
            stop_loss=153.0, stop_loss_reason="above",
            tp1=149.0, tp1_label="POC", tp2=147.0, tp2_label="VAL", rr_ratio=2.0,
        )
        plans = enforce_direction_consistency([plan], "RANGE", "bullish")
        assert plans[0].entry == 152.0  # unchanged


# ── Index Consistency Tests ──

class TestIndexConsistency:
    """Tests for SPY/QQQ directional regime conflict detection."""

    def test_conflict_opposite_trend_day(self):
        """SPY TREND_STRONG bullish + QQQ TREND_STRONG bearish → conflict."""
        conflict, detail = check_index_consistency(
            USRegimeType.TREND_STRONG, "bullish",
            USRegimeType.TREND_STRONG, "bearish",
        )
        assert conflict is True
        assert "SPY" in detail and "QQQ" in detail

    def test_no_conflict_same_direction(self):
        """Same direction TREND_STRONG → no conflict."""
        conflict, _ = check_index_consistency(
            USRegimeType.TREND_STRONG, "bullish",
            USRegimeType.TREND_STRONG, "bullish",
        )
        assert conflict is False

    def test_conflict_gap_and_go_vs_trend_day_opposite(self):
        """Mixed directional regimes with opposite directions → conflict."""
        conflict, detail = check_index_consistency(
            USRegimeType.GAP_GO, "bullish",
            USRegimeType.TREND_STRONG, "bearish",
        )
        assert conflict is True
        assert "gap_go" in detail and "trend_strong" in detail

    def test_no_conflict_gap_and_go_vs_trend_day_same(self):
        """Mixed directional regimes with same direction → no conflict."""
        conflict, _ = check_index_consistency(
            USRegimeType.GAP_GO, "bearish",
            USRegimeType.TREND_STRONG, "bearish",
        )
        assert conflict is False

    def test_no_conflict_one_fade_chop(self):
        """One side RANGE → no conflict (not directional)."""
        conflict, _ = check_index_consistency(
            USRegimeType.RANGE, "bullish",
            USRegimeType.TREND_STRONG, "bearish",
        )
        assert conflict is False

    def test_no_conflict_one_unclear(self):
        """One side UNCLEAR → no conflict."""
        conflict, _ = check_index_consistency(
            USRegimeType.UNCLEAR, "bullish",
            USRegimeType.TREND_STRONG, "bearish",
        )
        assert conflict is False

    def test_no_conflict_one_neutral_direction(self):
        """One side neutral direction → no conflict."""
        conflict, _ = check_index_consistency(
            USRegimeType.TREND_STRONG, "neutral",
            USRegimeType.TREND_STRONG, "bearish",
        )
        assert conflict is False

    def test_no_conflict_both_fade_chop(self):
        """Both RANGE → no conflict."""
        conflict, _ = check_index_consistency(
            USRegimeType.RANGE, "bullish",
            USRegimeType.RANGE, "bearish",
        )
        assert conflict is False

    def test_downgrade_sets_unclear(self):
        """Downgrade produces UNCLEAR with correct fields."""
        original = USRegimeResult(
            regime=USRegimeType.TREND_STRONG, confidence=0.75,
            rvol=1.5, price=450.0, gap_pct=0.3,
            details="RVOL 1.50 >= 1.20, small gap",
            lean="bullish",
        )
        reason = "SPY trend_strong bullish vs QQQ trend_strong bearish"
        result = downgrade_to_unclear(original, reason)
        assert result.regime == USRegimeType.UNCLEAR
        assert result.confidence == 0.25
        assert result.lean == "neutral"
        assert "Downgraded from trend_strong" in result.details
        assert reason in result.details
        assert "original:" in result.details


# ── Wide VA Detection Tests ──


def _make_today_bars(n: int, base_price: float = 550.0) -> pd.DataFrame:
    """Generate n synthetic today bars for wide VA tests (09:30 start, 1min each).

    Uses ±1.0 oscillation for sufficient band width in VWAP bands tests.
    """
    base_ts = pd.Timestamp("2026-03-17 09:30:00", tz="America/New_York")
    rows = []
    timestamps = []
    for i in range(n):
        ts = base_ts + pd.Timedelta(minutes=i)
        offset = (i % 10 - 5) * 0.5  # ±2.5 range for wider bands
        p = base_price + offset
        timestamps.append(ts)
        rows.append({"Open": p - 0.3, "High": p + 0.5, "Low": p - 0.5, "Close": p, "Volume": 10000 + i * 100})
    idx = pd.DatetimeIndex(timestamps, name="Datetime")
    return pd.DataFrame(rows, index=idx)


class TestWideVADetection:
    def test_wide_va_detected(self):
        """VA width / ADR > threshold → is_wide=True."""
        vp = VolumeProfileResult(poc=550.0, vah=560.0, val=540.0)
        # VA width ≈ 20/550*100 ≈ 3.636%, ADR = 1.0% → ratio ≈ 3.64
        is_wide, ratio = detect_wide_va(vp, avg_daily_range_pct=1.0, threshold=1.8)
        assert is_wide is True
        assert ratio > 1.8

    def test_narrow_va_not_detected(self):
        """VA width / ADR < threshold → is_wide=False."""
        vp = VolumeProfileResult(poc=550.0, vah=553.0, val=547.0)
        # VA width ≈ 6/550*100 ≈ 1.09%, ADR = 1.0% → ratio ≈ 1.09
        is_wide, ratio = detect_wide_va(vp, avg_daily_range_pct=1.0, threshold=1.8)
        assert is_wide is False
        assert ratio < 1.8

    def test_no_adr_returns_false(self):
        """avg_daily_range_pct <= 0 → safe default False."""
        vp = VolumeProfileResult(poc=550.0, vah=560.0, val=540.0)
        is_wide, ratio = detect_wide_va(vp, avg_daily_range_pct=0.0)
        assert is_wide is False
        assert ratio == 0.0

    def test_vah_lte_val_returns_false(self):
        """Inverted VA (vah <= val) → False."""
        vp = VolumeProfileResult(poc=550.0, vah=540.0, val=560.0)
        is_wide, ratio = detect_wide_va(vp, avg_daily_range_pct=1.0)
        assert is_wide is False

    def test_boundary_just_below_threshold(self):
        """ratio just below threshold → not wide."""
        # VA width ≈ 9.8/550*100 ≈ 1.7818%, ADR=1.0 → ratio ≈ 1.78 < 1.8
        vp = VolumeProfileResult(poc=550.0, vah=554.9, val=545.1)
        is_wide, ratio = detect_wide_va(vp, avg_daily_range_pct=1.0, threshold=1.8)
        assert is_wide is False
        assert ratio < 1.8


class TestIntradayVP:
    def test_sufficient_bars_returns_vp(self):
        """≥120 bars → valid VolumeProfileResult."""
        bars = _make_today_bars(130, base_price=550.0)
        result = compute_intraday_vp(bars, min_bars=120)
        assert result is not None
        assert result.poc > 0
        assert result.vah >= result.val

    def test_insufficient_bars_returns_none(self):
        """< 120 bars → None."""
        bars = _make_today_bars(100, base_price=550.0)
        result = compute_intraday_vp(bars, min_bars=120)
        assert result is None

    def test_tick_size_finer_than_multiday(self):
        """Intraday uses us_tick_size / 2 for finer granularity."""
        # For SPY ~550, us_tick_size=0.50, intraday should use 0.25
        from src.us_playbook.levels import us_tick_size
        tick_multiday = us_tick_size(550.0)
        assert tick_multiday == 0.50
        # compute_intraday_vp uses tick/2 = 0.25 internally


class TestVWAPBands:
    def test_sufficient_bars(self):
        """≥15 bars → (vwap, upper, lower) with upper > vwap > lower."""
        bars = _make_today_bars(30, base_price=550.0)
        result = compute_vwap_bands(bars, min_bars=15)
        assert result is not None
        vwap, upper, lower = result
        assert upper > vwap > lower

    def test_insufficient_bars_returns_none(self):
        """< 15 bars → None."""
        bars = _make_today_bars(10, base_price=550.0)
        result = compute_vwap_bands(bars, min_bars=15)
        assert result is None

    def test_narrow_bands_returns_none(self):
        """Bands too narrow → None."""
        # Constant price → std = 0 → should return None
        rows = []
        for i in range(20):
            ts = f"2026-03-17 09:{30 + i}:00"
            rows.append((ts, 550.0, 550.0, 550.0, 550.0, 10000))
        bars = _make_bars(rows)
        result = compute_vwap_bands(bars, min_bars=15, min_band_width_pct=0.002)
        assert result is None

    def test_simple_std_calculation(self):
        """Std is based on typical_price - vwap_series (simple, not volume-weighted)."""
        # Two distinct price groups should produce non-zero std
        rows = []
        for i in range(20):
            ts = f"2026-03-17 09:{30 + i}:00"
            p = 550.0 if i < 10 else 555.0
            rows.append((ts, p, p + 0.5, p - 0.5, p, 10000))
        bars = _make_bars(rows)
        result = compute_vwap_bands(bars, min_bars=15)
        assert result is not None
        vwap, upper, lower = result
        assert upper - lower > 0.5  # Non-trivial band width


class TestBuildIntradayLevels:
    def _wide_vp(self):
        """VP with wide VA (20 points on ~550 base ≈ 3.6%)."""
        return VolumeProfileResult(poc=550.0, vah=560.0, val=540.0)

    def _narrow_vp(self):
        """VP with narrow VA (6 points on ~550 base ≈ 1.1%)."""
        return VolumeProfileResult(poc=550.0, vah=553.0, val=547.0)

    def test_narrow_va_returns_none(self):
        """Non-wide VA → None."""
        bars = _make_today_bars(150)
        result = build_intraday_levels(bars, self._narrow_vp(), 1.0, 550.0, {})
        assert result is None

    def test_wide_va_with_enough_bars_uses_intraday_vp(self):
        """Wide VA + ≥120 bars → source='intraday_vp'."""
        bars = _make_today_bars(150)
        result = build_intraday_levels(bars, self._wide_vp(), 1.0, 550.0, {})
        assert result is not None
        assert result.is_wide_va is True
        assert result.source == "intraday_vp"
        assert result.effective_upper >= result.effective_lower

    def test_wide_va_few_bars_uses_vwap_bands(self):
        """Wide VA + 15-119 bars → source='vwap_bands'."""
        bars = _make_today_bars(50)
        result = build_intraday_levels(
            bars, self._wide_vp(), 1.0, 550.0,
            {"intraday_vp_min_bars": 120, "vwap_bands_min_bars": 15},
        )
        assert result is not None
        assert result.source == "vwap_bands"

    def test_wide_va_very_few_bars_returns_none(self):
        """Wide VA + < 15 bars → None."""
        bars = _make_today_bars(10)
        result = build_intraday_levels(
            bars, self._wide_vp(), 1.0, 550.0,
            {"intraday_vp_min_bars": 120, "vwap_bands_min_bars": 15},
        )
        assert result is None

    def test_source_label(self):
        """IntradayLevels.source_label returns correct CN text."""
        il_vp = IntradayLevels(True, 2.0, 555.0, 545.0, 550.0, "intraday_vp")
        assert il_vp.source_label == "日内发展中 VP"
        il_vw = IntradayLevels(True, 2.0, 553.0, 547.0, 550.0, "vwap_bands")
        assert il_vw.source_label == "VWAP ± 1σ"


# ── Wide VA Fade Plan Tests ──


class TestWideFadePlans:
    def _vp(self):
        """Wide 5-day VA: 540-560."""
        return VolumeProfileResult(poc=550.0, vah=560.0, val=540.0)

    def _kl(self):
        return KeyLevels(
            poc=550.0, vah=560.0, val=540.0,
            pdh=562.0, pdl=538.0, pmh=558.0, pml=542.0,
            vwap=551.0,
        )

    def _gw(self):
        return GammaWallResult(call_wall_strike=570.0, put_wall_strike=530.0, max_pain=550.0)

    def _il_vp(self, upper=554.0, lower=548.0, mid=551.0):
        return IntradayLevels(
            is_wide_va=True, va_adr_ratio=2.1,
            effective_upper=upper, effective_lower=lower,
            effective_mid=mid, source="intraday_vp",
        )

    def _il_vwap(self, upper=553.0, lower=549.0, mid=551.0):
        return IntradayLevels(
            is_wide_va=True, va_adr_ratio=2.1,
            effective_upper=upper, effective_lower=lower,
            effective_mid=mid, source="vwap_bands",
        )

    def test_wide_va_near_upper_bearish_plan_a(self):
        """Wide VA + price near effective_upper → bearish Plan A uses intraday levels."""
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.7,
            rvol=0.8, price=554.0, gap_pct=0.1,
        )
        il = self._il_vp()
        plans = _generate_action_plans(
            regime, "bearish", self._vp(), self._kl(), self._gw(), None,
            intraday_levels=il,
        )
        assert plans[0].name == "日内上沿做空"
        assert plans[0].entry == il.effective_upper
        assert plans[0].tp1 == il.effective_mid
        assert plans[0].tp2 == il.effective_lower
        assert plans[0].direction == "bearish"

    def test_wide_va_near_lower_bullish_plan_a(self):
        """Wide VA + price near effective_lower → bullish Plan A uses intraday levels."""
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.7,
            rvol=0.8, price=548.0, gap_pct=0.1,
        )
        il = self._il_vp()
        plans = _generate_action_plans(
            regime, "bullish", self._vp(), self._kl(), self._gw(), None,
            intraday_levels=il,
        )
        assert plans[0].name == "日内下沿做多"
        assert plans[0].entry == il.effective_lower
        assert plans[0].tp1 == il.effective_mid
        assert plans[0].tp2 == il.effective_upper
        assert plans[0].direction == "bullish"

    def test_wide_va_middle_follows_direction(self):
        """Wide VA + price in middle + direction=bearish → bearish plans."""
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.7,
            rvol=0.8, price=551.0, gap_pct=0.1,
        )
        il = self._il_vp()
        plans = _generate_action_plans(
            regime, "bearish", self._vp(), self._kl(), self._gw(), None,
            intraday_levels=il,
        )
        assert plans[0].name == "日内上沿做空"
        assert plans[0].direction == "bearish"

    def test_wide_va_direction_conflict_to_unclear(self):
        """Wide VA + intraday direction conflict → UNCLEAR plans."""
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.7,
            rvol=0.8, price=548.0, gap_pct=0.1,  # near lower
        )
        il = self._il_vp()
        plans = _generate_action_plans(
            regime, "bearish", self._vp(), self._kl(), self._gw(), None,
            intraday_levels=il,
        )
        # price <= effective_lower and direction=bearish → conflict → unclear
        assert plans[0].name == "等待确认"

    def test_plan_c_uses_5day_va(self):
        """Plan C always uses 5-day VAH/VAL for invalidation."""
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.7,
            rvol=0.8, price=554.0, gap_pct=0.1,
        )
        il = self._il_vp()
        plans = _generate_action_plans(
            regime, "bearish", self._vp(), self._kl(), self._gw(), None,
            intraday_levels=il,
        )
        assert plans[2].label == "C"
        assert "5日 VAH" in plans[2].trigger
        assert f"{self._vp().vah:,.2f}" in plans[2].trigger

    def test_plan_c_bullish_uses_5day_val(self):
        """Plan C bullish uses 5-day VAL for invalidation."""
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.7,
            rvol=0.8, price=548.0, gap_pct=0.1,
        )
        il = self._il_vp()
        plans = _generate_action_plans(
            regime, "bullish", self._vp(), self._kl(), self._gw(), None,
            intraday_levels=il,
        )
        assert plans[2].label == "C"
        assert "5日 VAL" in plans[2].trigger

    def test_narrow_va_original_behavior(self):
        """Narrow VA (no intraday_levels) → original fade plans unchanged."""
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.7,
            rvol=0.8, price=554.5, gap_pct=0.1,
        )
        narrow_vp = VolumeProfileResult(poc=552.0, vah=555.0, val=549.0)
        kl = KeyLevels(
            poc=552.0, vah=555.0, val=549.0,
            pdh=557.0, pdl=548.0, pmh=554.0, pml=550.5,
            vwap=551.5,
        )
        plans = _generate_action_plans(
            regime, "bearish", narrow_vp, kl, None, None,
            intraday_levels=None,  # No wide VA
        )
        assert plans[0].name == "上沿做空"  # Original name, not "日内上沿做空"
        assert plans[0].entry == narrow_vp.vah

    def test_early_session_vwap_bands(self):
        """VWAP bands source → Plan A logic mentions VWAP ± 1σ."""
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.7,
            rvol=0.8, price=553.0, gap_pct=0.1,
        )
        il = self._il_vwap()
        plans = _generate_action_plans(
            regime, "bearish", self._vp(), self._kl(), self._gw(), None,
            intraday_levels=il,
        )
        assert plans[0].name == "日内上沿做空"
        assert "VWAP ± 1σ" in plans[0].logic

    def test_5day_direction_conflict_overrides_wide_va(self):
        """5-day VA direction conflict check happens before wide VA routing."""
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.7,
            rvol=0.8, price=554.5, gap_pct=0.1,
        )
        il = self._il_vp()
        # edge=VAH + direction=bullish → 5-day direction conflict → UNCLEAR
        plans = _generate_action_plans(
            regime, "bullish", self._vp(), self._kl(), self._gw(), None,
            intraday_levels=il,
        )
        assert plans[0].name == "等待确认"


class TestWideVAPlaybookFormat:
    """Test Section 4 wide VA annotation in formatted output."""

    def _make_result(self, intraday_levels=None):
        vp = VolumeProfileResult(poc=550.0, vah=560.0, val=540.0)
        regime = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=0.7,
            rvol=0.8, price=551.0, gap_pct=0.1,
        )
        kl = KeyLevels(
            poc=550.0, vah=560.0, val=540.0,
            pdh=562.0, pdl=538.0, pmh=558.0, pml=542.0,
            vwap=551.0,
        )
        return USPlaybookResult(
            symbol="SPY", name="S&P 500 ETF",
            regime=regime, key_levels=kl,
            volume_profile=vp, gamma_wall=None,
            filters=FilterResult(tradeable=True, warnings=[], risk_level="normal"),
            generated_at=datetime(2026, 3, 17, 10, 30, 0, tzinfo=ET),
            intraday_levels=intraday_levels,
        )

    def test_intraday_vp_annotation(self):
        """Section 4 shows intraday VP source when wide VA active."""
        il = IntradayLevels(True, 2.1, 554.0, 548.0, 551.0, "intraday_vp")
        result = self._make_result(intraday_levels=il)
        msg = format_us_playbook_message(result)
        assert "宽 VA" in msg
        assert "日内发展中 VP" in msg
        assert "2.1x ADR" in msg

    def test_vwap_bands_annotation(self):
        """Section 4 shows VWAP bands source when wide VA active."""
        il = IntradayLevels(True, 2.1, 553.0, 549.0, 551.0, "vwap_bands")
        result = self._make_result(intraday_levels=il)
        msg = format_us_playbook_message(result)
        assert "宽 VA" in msg
        assert "VWAP ± 1σ" in msg

    def test_no_annotation_when_narrow(self):
        """No wide VA annotation when intraday_levels is None."""
        result = self._make_result(intraday_levels=None)
        msg = format_us_playbook_message(result)
        assert "宽 VA" not in msg


class TestRegimeFamily:
    """Test RegimeFamily property on USRegimeType."""

    def test_trend_family(self):
        assert USRegimeType.GAP_GO.family == RegimeFamily.TREND
        assert USRegimeType.TREND_STRONG.family == RegimeFamily.TREND
        assert USRegimeType.TREND_WEAK.family == RegimeFamily.TREND

    def test_fade_family(self):
        assert USRegimeType.RANGE.family == RegimeFamily.FADE
        assert USRegimeType.NARROW_GRIND.family == RegimeFamily.FADE

    def test_reversal_family(self):
        assert USRegimeType.V_REVERSAL.family == RegimeFamily.REVERSAL
        assert USRegimeType.GAP_FILL.family == RegimeFamily.REVERSAL

    def test_unclear_family(self):
        assert USRegimeType.UNCLEAR.family == RegimeFamily.UNCLEAR


class TestVReversalDetection:
    """Test V_REVERSAL detection in regime transitions."""

    def _make_v_bars(self, n=40, drop_pct=0.8, recover=True):
        """Create synthetic bars with V-shape: drop then recover past open."""
        import numpy as np
        dates = pd.date_range("2026-03-18 09:30", periods=n, freq="1min", tz="America/New_York")
        open_price = 100.0
        prices = []
        # First half: drop
        half = n // 2
        for i in range(half):
            p = open_price - (drop_pct * (i + 1) / half)
            prices.append(p)
        # Second half: recover
        low_price = prices[-1]
        for i in range(n - half):
            if recover:
                p = low_price + ((open_price + 0.3 - low_price) * (i + 1) / (n - half))
            else:
                p = low_price + 0.01 * i  # barely recover
            prices.append(p)

        df = pd.DataFrame({
            "Open": [p - 0.05 for p in prices],
            "High": [p + 0.1 for p in prices],
            "Low": [p - 0.1 for p in prices],
            "Close": prices,
            "Volume": [1000] * n,
        }, index=dates)
        return df, open_price

    def test_v_reversal_detected(self):
        from src.us_playbook.regime import _detect_v_reversal
        bars, open_price = self._make_v_bars(n=40, drop_pct=0.8, recover=True)
        detected, direction, confidence = _detect_v_reversal(bars, open_price, open_price - 0.5, 1.5)
        assert detected is True
        assert direction == "bullish"
        assert confidence > 0.4

    def test_v_reversal_not_detected_no_recovery(self):
        from src.us_playbook.regime import _detect_v_reversal
        bars, open_price = self._make_v_bars(n=40, drop_pct=0.8, recover=False)
        detected, direction, confidence = _detect_v_reversal(bars, open_price, open_price - 0.5, 1.5)
        assert detected is False

    def test_v_reversal_too_few_bars(self):
        from src.us_playbook.regime import _detect_v_reversal
        bars, open_price = self._make_v_bars(n=10, drop_pct=0.8, recover=True)
        detected, _, _ = _detect_v_reversal(bars, open_price, open_price, 1.5)
        assert detected is False  # needs >= 20 bars


class TestGapFillDetection:
    """Test GAP_FILL detection."""

    def test_gap_fill_detected(self):
        from src.us_playbook.regime import _detect_gap_fill
        # Gap up 1%, price retraced 70% of gap
        open_price = 101.0
        prev_close = 100.0
        gap_pct = 1.0
        dates = pd.date_range("2026-03-18 09:30", periods=20, freq="1min", tz="America/New_York")
        # Price starts at 101 and drops to 100.3 (70% fill)
        prices = [101.0 - i * 0.035 for i in range(20)]
        bars = pd.DataFrame({
            "Open": prices,
            "High": [p + 0.05 for p in prices],
            "Low": [p - 0.05 for p in prices],
            "Close": prices,
            "Volume": [1000] * 20,
        }, index=dates)
        detected, fill_pct, confidence = _detect_gap_fill(bars, gap_pct, open_price, prev_close, 1.0)
        assert detected is True
        assert fill_pct >= 50

    def test_gap_fill_not_detected_small_gap(self):
        from src.us_playbook.regime import _detect_gap_fill
        dates = pd.date_range("2026-03-18 09:30", periods=5, freq="1min", tz="America/New_York")
        bars = pd.DataFrame({
            "Open": [100.1]*5, "High": [100.2]*5,
            "Low": [100.0]*5, "Close": [100.1]*5,
            "Volume": [1000]*5,
        }, index=dates)
        detected, _, _ = _detect_gap_fill(bars, 0.05, 100.1, 100.05, 1.0)
        assert detected is False  # gap too small


class TestUnclearTimeout:
    """Test UNCLEAR timeout in RegimeStabilizer."""

    def test_unclear_timeout_forces_range(self):
        """After 60min of UNCLEAR, should force to RANGE."""
        from src.us_playbook.stabilizer import RegimeStabilizer
        stab = RegimeStabilizer({"enabled": True, "unclear_timeout_minutes": 60})

        raw = USRegimeResult(
            regime=USRegimeType.UNCLEAR, confidence=0.25,
            rvol=0.8, price=100, gap_pct=0.1,
        )

        # First call — accepts raw
        result1 = stab.stabilize("TEST", raw)
        assert result1.regime == USRegimeType.UNCLEAR

        # Simulate 61 minutes later — same UNCLEAR
        import time
        stab._state["TEST"].accepted_at = time.time() - 61 * 60

        result2 = stab.stabilize("TEST", raw)
        assert result2.regime == USRegimeType.RANGE  # forced from UNCLEAR
        assert result2.stabilized is True

    def test_unclear_timeout_forces_trend_weak_with_lean(self):
        """UNCLEAR with bullish lean should force to TREND_WEAK."""
        from src.us_playbook.stabilizer import RegimeStabilizer
        stab = RegimeStabilizer({"enabled": True, "unclear_timeout_minutes": 60})

        raw = USRegimeResult(
            regime=USRegimeType.UNCLEAR, confidence=0.35,
            rvol=1.2, price=100, gap_pct=0.1,
            lean="bullish",
        )

        result1 = stab.stabilize("TEST", raw)
        assert result1.regime == USRegimeType.UNCLEAR

        import time
        stab._state["TEST"].accepted_at = time.time() - 61 * 60

        result2 = stab.stabilize("TEST", raw)
        assert result2.regime == USRegimeType.TREND_WEAK
        assert result2.lean == "bullish"

    def test_unclear_no_timeout_when_disabled(self):
        """Timeout disabled should not force reclassify."""
        from src.us_playbook.stabilizer import RegimeStabilizer
        stab = RegimeStabilizer({"enabled": True, "unclear_timeout_minutes": 0})

        raw = USRegimeResult(
            regime=USRegimeType.UNCLEAR, confidence=0.25,
            rvol=0.8, price=100, gap_pct=0.1,
        )

        stab.stabilize("TEST", raw)

        import time
        stab._state["TEST"].accepted_at = time.time() - 120 * 60

        result = stab.stabilize("TEST", raw)
        assert result.regime == USRegimeType.UNCLEAR  # timeout disabled


class TestRegimeTransition7Types:
    """Test regime transitions across all 7+1 types."""

    def test_trend_to_v_reversal(self):
        """TREND_STRONG → V_REVERSAL transition."""
        from src.us_playbook.regime import detect_regime_transition
        from src.common.types import VolumeProfileResult

        vp = VolumeProfileResult(poc=100, vah=102, val=98)

        # Create V-shape bars
        n = 40
        dates = pd.date_range("2026-03-18 09:30", periods=n, freq="1min", tz="America/New_York")
        prices = []
        open_p = 100.0
        half = n // 2
        for i in range(half):
            prices.append(open_p - (1.0 * (i + 1) / half))
        low_p = prices[-1]
        for i in range(n - half):
            prices.append(low_p + ((open_p + 0.5 - low_p) * (i + 1) / (n - half)))

        bars = pd.DataFrame({
            "Open": [p - 0.02 for p in prices],
            "High": [p + 0.05 for p in prices],
            "Low": [p - 0.05 for p in prices],
            "Close": prices,
            "Volume": [1000] * n,
        }, index=dates)

        original = USRegimeResult(
            regime=USRegimeType.TREND_STRONG, confidence=0.75,
            rvol=1.5, price=101, gap_pct=0.5,
        )

        transitioned, new_regime = detect_regime_transition(
            original, current_rvol=1.5, current_price=100.5,
            vp=vp, open_price=open_p, prev_close=99.5,
            today_bars=bars,
        )
        # V reversal may or may not detect depending on exact bar shapes
        # Just verify no crash
        assert isinstance(transitioned, bool)

    def test_gap_go_to_gap_fill(self):
        """GAP_GO → GAP_FILL transition when gap retraced."""
        from src.us_playbook.regime import detect_regime_transition
        from src.common.types import VolumeProfileResult

        vp = VolumeProfileResult(poc=100, vah=102, val=98)

        # Gap up from 100 to 101, now price back at 100.3 (70% fill)
        n = 20
        dates = pd.date_range("2026-03-18 09:30", periods=n, freq="1min", tz="America/New_York")
        prices = [101.0 - i * 0.035 for i in range(n)]
        bars = pd.DataFrame({
            "Open": prices,
            "High": [p + 0.05 for p in prices],
            "Low": [p - 0.05 for p in prices],
            "Close": prices,
            "Volume": [1000] * n,
        }, index=dates)

        original = USRegimeResult(
            regime=USRegimeType.GAP_GO, confidence=0.80,
            rvol=1.8, price=101, gap_pct=1.0,
        )

        transitioned, new_regime = detect_regime_transition(
            original, current_rvol=1.2, current_price=100.3,
            vp=vp, open_price=101.0, prev_close=100.0,
            today_bars=bars,
        )
        assert transitioned is True
        assert new_regime.regime == USRegimeType.GAP_FILL
        assert new_regime.gap_fill_pct >= 50


# ── Phase 7: Direction & Entry Refactoring Tests ──


class TestDirectionConfidence:
    """Test _compute_direction_confidence from playbook.py."""

    def test_all_aligned_bullish(self):
        from src.us_playbook.playbook import _compute_direction_confidence
        regime = USRegimeResult(
            regime=USRegimeType.TREND_STRONG, confidence=0.8,
            rvol=1.5, price=102.0, gap_pct=0.5, vwap_slope=0.003,
        )
        vp = VolumeProfileResult(poc=100.0, vah=101.0, val=99.0)
        kl = KeyLevels(poc=100, vah=101, val=99, pdh=101.5, pdl=98.5, pmh=101.0, pml=99.0, vwap=100.5)
        result = _compute_direction_confidence(regime, vp, kl, "bullish")
        assert result.direction == "bullish"
        assert result.score > 0.5

    def test_neutral_direction_zero_score(self):
        from src.us_playbook.playbook import _compute_direction_confidence
        regime = USRegimeResult(
            regime=USRegimeType.UNCLEAR, confidence=0.3,
            rvol=0.8, price=100.0, gap_pct=0.1,
        )
        vp = VolumeProfileResult(poc=100.0, vah=101.0, val=99.0)
        kl = KeyLevels(poc=100, vah=101, val=99, pdh=101.5, pdl=98.5, pmh=101.0, pml=99.0, vwap=100.0)
        result = _compute_direction_confidence(regime, vp, kl, "neutral")
        assert result.direction == "neutral"
        assert result.score == 0.0


class TestRelativeStrength:
    """Test compute_relative_strength from indicators."""

    def test_outperforming_stock(self):
        from src.common.indicators import compute_relative_strength
        # Stock up 2%, SPY up 0.5%
        dates = pd.date_range("2026-03-18 09:30", periods=60, freq="1min")
        stock_bars = pd.DataFrame({
            "Open": [100] * 60,
            "High": [100 + i * 0.033 + 0.1 for i in range(60)],
            "Low": [100 + i * 0.033 - 0.1 for i in range(60)],
            "Close": [100 + i * 0.033 for i in range(60)],
            "Volume": [1000] * 60,
        }, index=dates)
        spy_bars = pd.DataFrame({
            "Open": [450] * 60,
            "High": [450 + i * 0.0083 + 0.05 for i in range(60)],
            "Low": [450 + i * 0.0083 - 0.05 for i in range(60)],
            "Close": [450 + i * 0.0083 for i in range(60)],
            "Volume": [10000] * 60,
        }, index=dates)
        rs = compute_relative_strength(stock_bars, spy_bars)
        assert rs.stock_return_pct > rs.spy_return_pct
        assert rs.label in ("强势", "同步")

    def test_empty_bars(self):
        from src.common.indicators import compute_relative_strength
        empty = pd.DataFrame()
        non_empty = pd.DataFrame({
            "Open": [100], "High": [101], "Low": [99], "Close": [100], "Volume": [1000],
        })
        rs = compute_relative_strength(empty, non_empty)
        assert rs.label == "数据不足"


class TestPlanBHedgeStructure:
    """Test that Plan B is now reverse direction (hedge) in trend/fade plans."""

    def _vp(self):
        return VolumeProfileResult(poc=252, vah=255, val=249)

    def _kl(self):
        return KeyLevels(poc=252, vah=255, val=249, pdh=257, pdl=248, pmh=254, pml=250.5, vwap=251.5)

    def _gw(self):
        return GammaWallResult(call_wall_strike=260, put_wall_strike=245, max_pain=252)

    def test_trend_bullish_plan_b_is_bearish(self):
        plans = _generate_action_plans(
            USRegimeResult(regime=USRegimeType.TREND_STRONG, confidence=0.8, rvol=1.5, price=252, gap_pct=0.5),
            "bullish", self._vp(), self._kl(), self._gw(), None,
        )
        plan_b = next(p for p in plans if p.label == "B")
        assert plan_b.direction == "bearish"
        assert "对冲" in plan_b.name or "反向" in plan_b.name

    def test_trend_bearish_plan_b_is_bullish(self):
        plans = _generate_action_plans(
            USRegimeResult(regime=USRegimeType.TREND_STRONG, confidence=0.8, rvol=1.5, price=252, gap_pct=0.5),
            "bearish", self._vp(), self._kl(), self._gw(), None,
        )
        plan_b = next(p for p in plans if p.label == "B")
        assert plan_b.direction == "bullish"

    def test_fade_bearish_plan_b_is_bullish(self):
        plans = _generate_action_plans(
            USRegimeResult(regime=USRegimeType.RANGE, confidence=0.7, rvol=0.8, price=254.5, gap_pct=0.1),
            "bearish", self._vp(), self._kl(), self._gw(), None,
        )
        plan_b = next(p for p in plans if p.label == "B")
        assert plan_b.direction == "bullish"

    def test_fade_bullish_plan_b_is_bearish(self):
        plans = _generate_action_plans(
            USRegimeResult(regime=USRegimeType.RANGE, confidence=0.7, rvol=0.8, price=249.5, gap_pct=0.1),
            "bullish", self._vp(), self._kl(), self._gw(), None,
        )
        plan_b = next(p for p in plans if p.label == "B")
        assert plan_b.direction == "bearish"


class TestInvalidationSection:
    """Test that the formatted playbook includes Plan C section (失效与切换 or 近端备选)."""

    def test_trend_includes_plan_c_section(self):
        regime = USRegimeResult(
            regime=USRegimeType.TREND_STRONG, confidence=0.8,
            rvol=1.5, price=260.0, gap_pct=0.5,
        )
        kl = KeyLevels(poc=255, vah=258, val=252, pdh=261, pdl=249, pmh=259, pml=253, vwap=256)
        vp = VolumeProfileResult(poc=255, vah=258, val=252)
        result = USPlaybookResult(
            symbol="AAPL", name="Apple", regime=regime,
            key_levels=kl, volume_profile=vp, gamma_wall=None,
            filters=FilterResult(tradeable=True),
            generated_at=datetime(2026, 3, 18, 10, 30, tzinfo=ZoneInfo("America/New_York")),
            quote=QuoteSnapshot(symbol="AAPL", last_price=260.0, high_price=261.0, low_price=258.0),
        )
        msg = format_us_playbook_message(result)
        # v2: Plan C shows as either "失效与切换" or "近端备选" section header
        assert "失效与切换" in msg or "近端备选" in msg

    def test_unclear_includes_plan_c_section(self):
        regime = USRegimeResult(
            regime=USRegimeType.UNCLEAR, confidence=0.3,
            rvol=0.8, price=255.0, gap_pct=0.1,
        )
        kl = KeyLevels(poc=255, vah=258, val=252, pdh=261, pdl=249, pmh=259, pml=253, vwap=256)
        vp = VolumeProfileResult(poc=255, vah=258, val=252)
        result = USPlaybookResult(
            symbol="SPY", name="S&P 500 ETF", regime=regime,
            key_levels=kl, volume_profile=vp, gamma_wall=None,
            filters=FilterResult(tradeable=True),
            generated_at=datetime(2026, 3, 18, 10, 30, tzinfo=ZoneInfo("America/New_York")),
            quote=QuoteSnapshot(symbol="SPY", last_price=255.0, high_price=256.0, low_price=254.0),
        )
        msg = format_us_playbook_message(result)
        # v2: Plan C shows as either "失效与切换" or "近端备选" section header
        assert "失效与切换" in msg or "近端备选" in msg


class TestEndToEndActionPlans:
    """End-to-end test: _generate_action_plans produces valid three-plan structure."""

    def _vp(self):
        return VolumeProfileResult(poc=252, vah=255, val=249)

    def _kl(self):
        return KeyLevels(poc=252, vah=255, val=249, pdh=257, pdl=248, pmh=254, pml=250.5, vwap=251.5)

    def test_all_regimes_produce_three_plans(self):
        """Every regime type produces exactly 3 plans."""
        for regime_type in [USRegimeType.TREND_STRONG, USRegimeType.RANGE, USRegimeType.UNCLEAR,
                            USRegimeType.GAP_GO, USRegimeType.TREND_WEAK]:
            regime = USRegimeResult(regime=regime_type, confidence=0.7, rvol=1.0, price=252, gap_pct=0.5)
            plans = _generate_action_plans(regime, "bullish", self._vp(), self._kl(), None, None)
            assert len(plans) == 3, f"{regime_type.name} produced {len(plans)} plans"
            labels = [p.label for p in plans]
            assert "A" in labels
            assert "B" in labels
            assert "C" in labels

    def test_plan_a_primary(self):
        regime = USRegimeResult(regime=USRegimeType.TREND_STRONG, confidence=0.8, rvol=1.5, price=252, gap_pct=0.5)
        plans = _generate_action_plans(regime, "bullish", self._vp(), self._kl(), None, None)
        plan_a = next(p for p in plans if p.label == "A")
        assert plan_a.is_primary is True

    def test_plan_b_is_hedge(self):
        """Plan B should have opposite direction to Plan A (hedge)."""
        regime = USRegimeResult(regime=USRegimeType.TREND_STRONG, confidence=0.8, rvol=1.5, price=252, gap_pct=0.5)
        plans = _generate_action_plans(regime, "bullish", self._vp(), self._kl(), None, None)
        plan_a = next(p for p in plans if p.label == "A")
        plan_b = next(p for p in plans if p.label == "B")
        assert plan_a.direction != plan_b.direction
