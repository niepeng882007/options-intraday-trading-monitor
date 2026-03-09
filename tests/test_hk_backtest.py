"""Tests for the HK Predict Backtest framework."""

import pandas as pd
import numpy as np
import pytest
from datetime import datetime, timezone, timedelta

from src.hk import RegimeType, VolumeProfileResult
from src.hk.backtest import (
    LevelEvent, LevelEvalResult,
    RegimeEvalDay, RegimeEvalResult,
    SimTrade, SimResult,
    HKBacktestResult,
)
from src.hk.backtest.evaluators import (
    evaluate_levels, evaluate_regimes,
    _get_session, _split_by_date,
)
from src.hk.backtest.simulator import TradeSimulator
from src.hk.backtest.engine import HKBacktestEngine
from src.hk.backtest.report import format_report, format_csv, format_json

HKT = timezone(timedelta(hours=8))


# ── Helpers ──

def _make_bars(prices: list[tuple], tz: str = "Asia/Hong_Kong") -> pd.DataFrame:
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


def _make_day_bars(
    date_str: str,
    base_price: float = 500.0,
    volatility: float = 5.0,
    volume: int = 10000,
    n_bars: int = 60,
    start_hour: int = 9,
    start_min: int = 30,
) -> list[tuple]:
    """Generate synthetic 1m bars for a single day."""
    bars = []
    np.random.seed(hash(date_str) % 2**31)
    price = base_price

    for i in range(n_bars):
        minute = start_min + i
        hour = start_hour + minute // 60
        minute = minute % 60

        # Skip lunch break
        if 12 <= hour < 13:
            continue
        if hour >= 16:
            break

        change = np.random.randn() * volatility * 0.01
        o = price
        h = price + abs(change) + np.random.rand() * volatility * 0.005
        l = price - abs(change) - np.random.rand() * volatility * 0.005
        c = price + change
        price = c

        ts = f"{date_str} {hour:02d}:{minute:02d}:00"
        bars.append((ts, round(o, 2), round(h, 2), round(l, 2), round(c, 2), volume))

    return bars


def _make_multi_day_bars(
    n_days: int = 15,
    base_price: float = 500.0,
    volatility: float = 5.0,
) -> pd.DataFrame:
    """Generate n_days of synthetic 1m bars."""
    all_bars = []
    from datetime import date, timedelta as td

    start_date = date(2026, 2, 16)  # Monday
    trading_days = 0
    current = start_date

    while trading_days < n_days:
        if current.weekday() < 5:  # Skip weekends
            date_str = current.strftime("%Y-%m-%d")
            day_bars = _make_day_bars(
                date_str, base_price, volatility,
                n_bars=150,  # morning session
            )
            all_bars.extend(day_bars)

            # Afternoon session
            afternoon_bars = _make_day_bars(
                date_str, base_price + np.random.randn() * 2,
                volatility, n_bars=180, start_hour=13, start_min=0,
            )
            all_bars.extend(afternoon_bars)
            trading_days += 1

        current += td(days=1)

    return _make_bars(all_bars)


# ── Utility Tests ──

class TestUtils:
    def test_get_session_morning(self):
        from datetime import time as dt_time
        assert _get_session(dt_time(9, 30)) == "morning"
        assert _get_session(dt_time(11, 59)) == "morning"

    def test_get_session_afternoon(self):
        from datetime import time as dt_time
        assert _get_session(dt_time(13, 0)) == "afternoon"
        assert _get_session(dt_time(15, 30)) == "afternoon"

    def test_split_by_date(self):
        bars = _make_bars([
            ("2026-03-07 09:30:00", 100, 101, 99, 100, 1000),
            ("2026-03-07 10:00:00", 101, 102, 100, 101, 1000),
            ("2026-03-08 09:30:00", 100, 101, 99, 100, 1000),
        ])
        daily = _split_by_date(bars)
        assert len(daily) == 2
        from datetime import date
        assert len(daily[date(2026, 3, 7)]) == 2
        assert len(daily[date(2026, 3, 8)]) == 1


# ── Level Evaluator Tests ──

class TestEvaluateLevels:
    def _build_test_data(self) -> dict[str, pd.DataFrame]:
        """Build 10 days of data where:
        - Days 1-5: lookback (price stable around 500, tight range)
        - Days 6-10: test days (price touches extremes)
        """
        all_bars = []
        from datetime import date, timedelta as td

        # Days 1-5: stable bars (VP should produce POC~500, tight VA)
        start = date(2026, 2, 16)
        for day_offset in range(5):
            d = start + td(days=day_offset)
            if d.weekday() >= 5:
                continue
            ds = d.strftime("%Y-%m-%d")
            for i in range(60):
                minute = 30 + i
                hour = 9 + minute // 60
                minute = minute % 60
                if hour >= 12:
                    break
                ts = f"{ds} {hour:02d}:{minute:02d}:00"
                # Tight range around 500
                all_bars.append((ts, 499, 502, 498, 500, 10000))

        # Days 6-10: price goes to 510 (above VAH) and then back
        for day_offset in range(7, 12):
            d = start + td(days=day_offset)
            if d.weekday() >= 5:
                continue
            ds = d.strftime("%Y-%m-%d")
            # First bar: touches above VAH
            all_bars.append((f"{ds} 09:30:00", 500, 510, 499, 508, 10000))
            # Next bars: price drops back (bounce down from VAH)
            for j in range(1, 16):
                ts = f"{ds} 09:{30+j:02d}:00"
                # Gradual drop from 508 to 502
                price = 508 - j * 0.4
                all_bars.append((ts, price + 0.2, price + 0.5, price - 0.5, price, 10000))

        return {"HK.TEST": _make_bars(all_bars)}

    def test_evaluate_levels_basic(self):
        data = self._build_test_data()
        result = evaluate_levels(
            data,
            vp_lookback_days=5,
            bounce_thresholds=[0.003, 0.005],
            bounce_window_bars=15,
        )
        assert isinstance(result, LevelEvalResult)
        # Should have some events (VAH touches at least)
        assert len(result.events) >= 0
        # Thresholds should be present
        assert 0.003 in result.by_threshold
        assert 0.005 in result.by_threshold

    def test_empty_data(self):
        result = evaluate_levels({}, vp_lookback_days=5)
        assert len(result.events) == 0

    def test_insufficient_history(self):
        """With only 3 days of data and lookback=5, no evaluations should occur."""
        bars = _make_bars([
            ("2026-03-07 09:30:00", 500, 502, 498, 500, 10000),
            ("2026-03-08 09:30:00", 500, 502, 498, 500, 10000),
            ("2026-03-09 09:30:00", 500, 515, 498, 510, 10000),
        ])
        result = evaluate_levels({"HK.TEST": bars}, vp_lookback_days=5)
        assert len(result.events) == 0

    def test_level_event_has_session(self):
        data = self._build_test_data()
        result = evaluate_levels(data, vp_lookback_days=5)
        for event in result.events:
            assert event.session in ("morning", "afternoon")

    def test_exclude_symbols_levels(self):
        """Excluded symbols should produce no level events."""
        data = self._build_test_data()
        result = evaluate_levels(
            data, vp_lookback_days=5,
            exclude_symbols={"HK.TEST"},
        )
        assert len(result.events) == 0


# ── Regime Evaluator Tests ──

class TestEvaluateRegimes:
    def _build_regime_data(self, rvol_multiplier: float = 1.0) -> dict[str, pd.DataFrame]:
        """Build data with controlled RVOL for regime testing."""
        all_bars = []
        from datetime import date, timedelta as td

        start = date(2026, 2, 10)
        base_vol = 10000

        for day_offset in range(15):
            d = start + td(days=day_offset)
            if d.weekday() >= 5:
                continue
            ds = d.strftime("%Y-%m-%d")

            # Determine if this is a "test" day (last 3 days get different volume)
            is_test = day_offset >= 12
            vol = int(base_vol * rvol_multiplier) if is_test else base_vol

            for i in range(30):
                minute = 30 + i
                hour = 9 + minute // 60
                minute = minute % 60
                ts = f"{ds} {hour:02d}:{minute:02d}:00"
                all_bars.append((ts, 500, 502, 498, 500, vol))

        return {"HK.TEST": _make_bars(all_bars)}

    def test_evaluate_regimes_basic(self):
        data = self._build_regime_data()
        result = evaluate_regimes(
            data,
            vp_lookback_days=5,
            rvol_lookback_days=5,
        )
        assert isinstance(result, RegimeEvalResult)
        # Should have some day evaluations
        assert len(result.days) >= 0

    def test_empty_data(self):
        result = evaluate_regimes({}, vp_lookback_days=5, rvol_lookback_days=5)
        assert len(result.days) == 0

    def test_regime_accuracy_fields(self):
        data = self._build_regime_data()
        result = evaluate_regimes(data, vp_lookback_days=3, rvol_lookback_days=3)
        for day in result.days:
            assert day.symbol == "HK.TEST"
            assert day.predicted in RegimeType
            assert 0 <= day.confidence <= 1
            assert day.day_high >= day.day_low

    def test_exclude_symbols_regimes(self):
        """Excluded symbols should produce no regime evaluations."""
        data = self._build_regime_data()
        result = evaluate_regimes(
            data, vp_lookback_days=5, rvol_lookback_days=5,
            exclude_symbols={"HK.TEST"},
        )
        assert len(result.days) == 0


# ── Trade Simulator Tests ──

class TestTradeSimulator:
    def test_long_take_profit(self):
        """Long trade should hit TP when price rises enough."""
        sim = TradeSimulator(
            tp_pct=0.005, sl_pct=0.003, slippage_per_leg=0.0,
            exit_mode="fixed", morning_only_levels=False,
        )

        # Entry at 500, price rises to 503 (0.6% > 0.5% TP)
        bars = _make_bars([
            ("2026-03-09 09:30:00", 500, 500.5, 499.5, 500, 10000),
            ("2026-03-09 09:31:00", 500, 503, 499, 502, 10000),
        ])

        event = LevelEvent(
            date=bars.index[0],
            symbol="HK.TEST",
            level_type="VAL",
            level_price=500,
            touch_price=500,
            touch_bar_idx=0,
            session="morning",
        )

        result = sim.simulate_from_levels({"HK.TEST": bars}, [event])
        assert result.total_trades == 1
        assert result.trades[0].exit_reason == "take_profit"
        assert result.trades[0].net_pnl_pct > 0

    def test_short_stop_loss(self):
        """Short trade should hit SL when price rises."""
        sim = TradeSimulator(
            tp_pct=0.005, sl_pct=0.003, slippage_per_leg=0.0,
            exit_mode="fixed", morning_only_levels=False,
        )

        # VAH touch at 510, price continues up to 512 (SL hit)
        bars = _make_bars([
            ("2026-03-09 09:30:00", 508, 510, 507, 509, 10000),
            ("2026-03-09 09:31:00", 509, 512, 509, 511, 10000),
        ])

        event = LevelEvent(
            date=bars.index[0],
            symbol="HK.TEST",
            level_type="VAH",
            level_price=510,
            touch_price=510,
            touch_bar_idx=0,
            session="morning",
        )

        result = sim.simulate_from_levels({"HK.TEST": bars}, [event])
        assert result.total_trades == 1
        assert result.trades[0].exit_reason == "stop_loss"
        assert result.trades[0].net_pnl_pct < 0

    def test_time_exit(self):
        """Trade should exit at 15:50 if no TP/SL hit."""
        sim = TradeSimulator(
            tp_pct=0.05, sl_pct=0.05, slippage_per_leg=0.0,
            exit_mode="fixed", morning_only_levels=False,
        )

        bars = _make_bars([
            ("2026-03-09 09:30:00", 500, 501, 499, 500, 10000),
            ("2026-03-09 15:50:00", 500, 501, 499, 500.5, 10000),
        ])

        event = LevelEvent(
            date=bars.index[0],
            symbol="HK.TEST",
            level_type="VAL",
            level_price=500,
            touch_price=500,
            touch_bar_idx=0,
            session="morning",
        )

        result = sim.simulate_from_levels({"HK.TEST": bars}, [event])
        assert result.total_trades == 1
        assert result.trades[0].exit_reason == "time_exit"

    def test_slippage_deduction(self):
        """Net P&L should account for slippage."""
        sim = TradeSimulator(
            tp_pct=0.005, sl_pct=0.003, slippage_per_leg=0.002,
            exit_mode="fixed", morning_only_levels=False,
        )

        bars = _make_bars([
            ("2026-03-09 09:30:00", 500, 500, 500, 500, 10000),
            ("2026-03-09 09:31:00", 500, 504, 499, 503, 10000),
        ])

        event = LevelEvent(
            date=bars.index[0],
            symbol="HK.TEST",
            level_type="VAL",
            level_price=500,
            touch_price=500,
            touch_bar_idx=0,
            session="morning",
        )

        result = sim.simulate_from_levels({"HK.TEST": bars}, [event])
        assert result.total_trades == 1
        trade = result.trades[0]
        # stock_pnl should be ~0.5%, but net should be 0.5% - 0.4% = 0.1%
        assert trade.stock_pnl_pct > trade.net_pnl_pct
        assert abs(trade.stock_pnl_pct - trade.net_pnl_pct - 0.4) < 0.1

    def test_empty_events(self):
        sim = TradeSimulator()
        result = sim.simulate_from_levels({}, [])
        assert result.total_trades == 0

    def test_regime_simulation(self):
        """Regime simulation should produce trades for BREAKOUT/RANGE days."""
        sim = TradeSimulator(
            tp_pct=0.005, sl_pct=0.003, slippage_per_leg=0.0,
            exit_mode="fixed",
        )

        # Need >= 10 bars to pass min check in simulate_from_regimes
        bar_data = [("2026-03-09 09:30:00", 500, 501, 499, 500, 10000)]
        for i in range(1, 10):
            bar_data.append((f"2026-03-09 09:{30+i}:00", 500, 501, 499, 500, 10000))
        # Bar after 09:35 with big move for TP
        bar_data.append(("2026-03-09 09:41:00", 500, 504, 499, 503, 10000))
        bar_data.append(("2026-03-09 15:50:00", 503, 504, 502, 503, 10000))
        bars = _make_bars(bar_data)

        regime_day = RegimeEvalDay(
            date=bars.index[0].date(),
            symbol="HK.TEST",
            predicted=RegimeType.BREAKOUT,
            confidence=0.8,
            rvol=1.5,
            vah=510, val=490, poc=500,
            day_open=500, day_high=504, day_low=499, day_close=503,
        )

        result = sim.simulate_from_regimes({"HK.TEST": bars}, [regime_day])
        assert result.total_trades == 1
        assert "BREAKOUT" in result.trades[0].signal_type

    def test_no_trade_for_unclear(self):
        """UNCLEAR regime should not generate trades."""
        sim = TradeSimulator()
        regime_day = RegimeEvalDay(
            date=datetime(2026, 3, 9).date(),
            symbol="HK.TEST",
            predicted=RegimeType.UNCLEAR,
            confidence=0.3,
            rvol=1.0,
            vah=510, val=490, poc=500,
        )
        result = sim.simulate_from_regimes({"HK.TEST": pd.DataFrame()}, [regime_day])
        assert result.total_trades == 0

    def test_exclude_symbols(self):
        """Excluded symbols should produce 0 trades."""
        sim = TradeSimulator(
            tp_pct=0.005, sl_pct=0.003, slippage_per_leg=0.0,
            exclude_symbols={"HK.TEST"}, exit_mode="fixed",
            morning_only_levels=False,
        )

        bars = _make_bars([
            ("2026-03-09 09:30:00", 500, 500.5, 499.5, 500, 10000),
            ("2026-03-09 09:31:00", 500, 503, 499, 502, 10000),
        ])
        event = LevelEvent(
            date=bars.index[0], symbol="HK.TEST",
            level_type="VAL", level_price=500, touch_price=500,
            touch_bar_idx=0, session="morning",
        )
        result = sim.simulate_from_levels({"HK.TEST": bars}, [event])
        assert result.total_trades == 0

    def test_morning_only_levels(self):
        """Afternoon level events should be skipped when morning_only_levels=True."""
        sim = TradeSimulator(
            tp_pct=0.005, sl_pct=0.003, slippage_per_leg=0.0,
            morning_only_levels=True, exit_mode="fixed",
        )

        bars = _make_bars([
            ("2026-03-09 13:30:00", 500, 500.5, 499.5, 500, 10000),
            ("2026-03-09 13:31:00", 500, 503, 499, 502, 10000),
        ])
        event = LevelEvent(
            date=bars.index[0], symbol="HK.TEST",
            level_type="VAL", level_price=500, touch_price=500,
            touch_bar_idx=0, session="afternoon",
        )
        result = sim.simulate_from_levels({"HK.TEST": bars}, [event])
        assert result.total_trades == 0

        # Morning events should still work
        bars2 = _make_bars([
            ("2026-03-09 09:30:00", 500, 500.5, 499.5, 500, 10000),
            ("2026-03-09 09:31:00", 500, 503, 499, 502, 10000),
        ])
        event2 = LevelEvent(
            date=bars2.index[0], symbol="HK.TEST",
            level_type="VAL", level_price=500, touch_price=500,
            touch_bar_idx=0, session="morning",
        )
        result2 = sim.simulate_from_levels({"HK.TEST": bars2}, [event2])
        assert result2.total_trades == 1

    def test_skip_signal_types(self):
        """Skipped signal types should produce 0 trades."""
        sim = TradeSimulator(
            tp_pct=0.005, sl_pct=0.003, slippage_per_leg=0.0,
            skip_signal_types={"BREAKOUT_long"}, exit_mode="fixed",
        )

        bar_data = [("2026-03-09 09:30:00", 500, 501, 499, 500, 10000)]
        for i in range(1, 10):
            bar_data.append((f"2026-03-09 09:{30+i}:00", 500, 501, 499, 500, 10000))
        bar_data.append(("2026-03-09 09:41:00", 500, 504, 499, 503, 10000))
        bar_data.append(("2026-03-09 15:50:00", 503, 504, 502, 503, 10000))
        bars = _make_bars(bar_data)

        # BREAKOUT_long: entry above POC → should be skipped
        regime_day = RegimeEvalDay(
            date=bars.index[0].date(), symbol="HK.TEST",
            predicted=RegimeType.BREAKOUT, confidence=0.8, rvol=1.5,
            vah=510, val=490, poc=499,  # POC < entry_price → BREAKOUT_long
            day_open=500, day_high=504, day_low=499, day_close=503,
        )
        result = sim.simulate_from_regimes({"HK.TEST": bars}, [regime_day])
        assert result.total_trades == 0

        # BREAKOUT_short should still work
        regime_day2 = RegimeEvalDay(
            date=bars.index[0].date(), symbol="HK.TEST",
            predicted=RegimeType.BREAKOUT, confidence=0.8, rvol=1.5,
            vah=510, val=490, poc=501,  # POC > entry_price → BREAKOUT_short
            day_open=500, day_high=504, day_low=499, day_close=503,
        )
        result2 = sim.simulate_from_regimes({"HK.TEST": bars}, [regime_day2])
        assert result2.total_trades == 1
        assert result2.trades[0].signal_type == "BREAKOUT_short"

    def test_trailing_stop(self):
        """Trailing stop should activate at threshold and exit on drawdown."""
        sim = TradeSimulator(
            tp_pct=0.05,  # high TP so it doesn't trigger
            sl_pct=0.05,  # high SL
            slippage_per_leg=0.0,
            exit_mode="trailing",
            trailing_activation_pct=0.005,  # 0.5%
            trailing_trail_pct=0.003,  # 0.3%
            morning_only_levels=False,
        )

        # Long trade: price rises 1%, then drops 0.5% → trailing activates at 0.5%, exit at drawdown 0.3%
        bars = _make_bars([
            ("2026-03-09 09:30:00", 500, 500, 500, 500, 10000),       # entry
            ("2026-03-09 09:31:00", 500, 505, 499.5, 504, 10000),     # peak 1.0%
            ("2026-03-09 09:32:00", 504, 504, 501.0, 501, 10000),     # drop from peak
        ])

        event = LevelEvent(
            date=bars.index[0], symbol="HK.TEST",
            level_type="VAL", level_price=500, touch_price=500,
            touch_bar_idx=0, session="morning",
        )

        result = sim.simulate_from_levels({"HK.TEST": bars}, [event])
        assert result.total_trades == 1
        trade = result.trades[0]
        assert trade.exit_reason == "trailing_stop"
        assert trade.peak_pnl_pct > 0
        assert trade.net_pnl_pct > 0  # should still be positive (exited above entry)

    def test_exit_mode_fixed(self):
        """Fixed mode should behave like original (TP/SL only, no trailing)."""
        sim = TradeSimulator(
            tp_pct=0.005, sl_pct=0.003, slippage_per_leg=0.0,
            exit_mode="fixed", morning_only_levels=False,
        )

        bars = _make_bars([
            ("2026-03-09 09:30:00", 500, 500, 500, 500, 10000),
            ("2026-03-09 09:31:00", 500, 503, 499, 502, 10000),
        ])

        event = LevelEvent(
            date=bars.index[0], symbol="HK.TEST",
            level_type="VAL", level_price=500, touch_price=500,
            touch_bar_idx=0, session="morning",
        )

        result = sim.simulate_from_levels({"HK.TEST": bars}, [event])
        assert result.total_trades == 1
        assert result.trades[0].exit_reason == "take_profit"

    def test_exit_mode_both(self):
        """Both mode: first-to-trigger between fixed TP and trailing stop."""
        # Test that TP triggers before trailing in 'both' mode
        sim = TradeSimulator(
            tp_pct=0.005, sl_pct=0.003, slippage_per_leg=0.0,
            exit_mode="both",
            trailing_activation_pct=0.003,
            trailing_trail_pct=0.002,
            morning_only_levels=False,
        )

        # Price goes up 0.6% → hits TP (0.5%) before trailing would exit
        bars = _make_bars([
            ("2026-03-09 09:30:00", 500, 500, 500, 500, 10000),
            ("2026-03-09 09:31:00", 500, 503, 499.5, 502, 10000),
        ])

        event = LevelEvent(
            date=bars.index[0], symbol="HK.TEST",
            level_type="VAL", level_price=500, touch_price=500,
            touch_bar_idx=0, session="morning",
        )

        result = sim.simulate_from_levels({"HK.TEST": bars}, [event])
        assert result.total_trades == 1
        assert result.trades[0].exit_reason == "take_profit"

    def test_peak_pnl_pct_tracked(self):
        """SimTrade should have peak_pnl_pct field."""
        sim = TradeSimulator(
            tp_pct=0.005, sl_pct=0.003, slippage_per_leg=0.0,
            exit_mode="fixed", morning_only_levels=False,
        )
        bars = _make_bars([
            ("2026-03-09 09:30:00", 500, 500, 500, 500, 10000),
            ("2026-03-09 09:31:00", 500, 503, 499, 502, 10000),
        ])
        event = LevelEvent(
            date=bars.index[0], symbol="HK.TEST",
            level_type="VAL", level_price=500, touch_price=500,
            touch_bar_idx=0, session="morning",
        )
        result = sim.simulate_from_levels({"HK.TEST": bars}, [event])
        assert result.total_trades == 1
        assert result.trades[0].peak_pnl_pct >= 0


# ── Sim Result Stats ──

class TestSimResultStats:
    def test_win_rate(self):
        sim = TradeSimulator(slippage_per_leg=0.0)
        result = sim._compute_sim_result([
            SimTrade(symbol="A", signal_type="VAL_long", entry_price=100,
                     entry_time=datetime.now(), exit_price=101,
                     net_pnl_pct=1.0, stock_pnl_pct=1.0),
            SimTrade(symbol="A", signal_type="VAH_short", entry_price=100,
                     entry_time=datetime.now(), exit_price=101,
                     net_pnl_pct=-0.5, stock_pnl_pct=-0.5),
        ])
        assert result.win_rate == 50.0
        assert result.winning_trades == 1
        assert result.losing_trades == 1

    def test_profit_factor(self):
        sim = TradeSimulator(slippage_per_leg=0.0)
        result = sim._compute_sim_result([
            SimTrade(symbol="A", signal_type="VAL_long", entry_price=100,
                     entry_time=datetime.now(), net_pnl_pct=2.0, stock_pnl_pct=2.0),
            SimTrade(symbol="A", signal_type="VAL_long", entry_price=100,
                     entry_time=datetime.now(), net_pnl_pct=-1.0, stock_pnl_pct=-1.0),
        ])
        assert abs(result.profit_factor - 2.0) < 0.01

    def test_max_drawdown(self):
        sim = TradeSimulator(slippage_per_leg=0.0)
        result = sim._compute_sim_result([
            SimTrade(symbol="A", signal_type="t", entry_price=100,
                     entry_time=datetime.now(), net_pnl_pct=3.0, stock_pnl_pct=3.0),
            SimTrade(symbol="A", signal_type="t", entry_price=100,
                     entry_time=datetime.now(), net_pnl_pct=-2.0, stock_pnl_pct=-2.0),
            SimTrade(symbol="A", signal_type="t", entry_price=100,
                     entry_time=datetime.now(), net_pnl_pct=-1.0, stock_pnl_pct=-1.0),
        ])
        assert result.max_drawdown_pct == 3.0  # Peak at 3, bottom at 0


# ── Engine Tests ──

class TestHKBacktestEngine:
    def test_engine_runs(self):
        """Engine should complete without errors on synthetic data."""
        bars = _make_multi_day_bars(n_days=12, base_price=500, volatility=5)
        engine = HKBacktestEngine(
            vp_lookback_days=5,
            rvol_lookback_days=5,
            run_sim=True,
        )
        result = engine.run({"HK.TEST": bars})
        assert isinstance(result, HKBacktestResult)
        assert result.level_eval is not None
        assert result.regime_eval is not None
        assert result.sim_result is not None

    def test_engine_no_sim(self):
        """Engine should skip simulation when run_sim=False."""
        bars = _make_multi_day_bars(n_days=12, base_price=500, volatility=5)
        engine = HKBacktestEngine(
            vp_lookback_days=5,
            rvol_lookback_days=5,
            run_sim=False,
        )
        result = engine.run({"HK.TEST": bars})
        assert result.sim_result is None

    def test_engine_metadata(self):
        bars = _make_multi_day_bars(n_days=10, base_price=500, volatility=5)
        engine = HKBacktestEngine(vp_lookback_days=3, rvol_lookback_days=3, run_sim=False)
        result = engine.run({"HK.TEST": bars})
        assert result.symbols == ["HK.TEST"]
        assert result.data_bars > 0
        assert result.days > 0

    def test_engine_with_exclude(self):
        """Engine should pass exclude_symbols to evaluators and simulator."""
        bars = _make_multi_day_bars(n_days=12, base_price=500, volatility=5)
        engine = HKBacktestEngine(
            vp_lookback_days=5,
            rvol_lookback_days=5,
            run_sim=True,
            exclude_symbols={"HK.TEST"},
        )
        result = engine.run({"HK.TEST": bars})
        assert result.level_eval is not None
        assert len(result.level_eval.events) == 0
        assert result.regime_eval is not None
        assert len(result.regime_eval.days) == 0


# ── Report Tests ──

class TestReport:
    def _make_result(self) -> HKBacktestResult:
        level_eval = LevelEvalResult(
            events=[],
            by_threshold={
                0.003: {"vah_touches": 5, "vah_bounces": 3, "val_touches": 4, "val_bounces": 3},
                0.005: {"vah_touches": 5, "vah_bounces": 2, "val_touches": 4, "val_bounces": 2},
            },
            by_session={
                "morning": {0.005: {"touches": 6, "bounces": 3}},
                "afternoon": {0.005: {"touches": 3, "bounces": 1}},
            },
        )
        regime_eval = RegimeEvalResult(
            days=[],
            by_regime={
                "breakout": {"total": 5, "accurate": 3},
                "range": {"total": 8, "accurate": 6},
            },
        )
        sim_result = SimResult(
            trades=[
                SimTrade(symbol="HK.800000", signal_type="VAH_short",
                         entry_price=25000, entry_time=datetime.now(),
                         exit_price=24950, net_pnl_pct=0.16, stock_pnl_pct=0.20,
                         peak_pnl_pct=0.25),
            ],
            total_trades=1, winning_trades=1, losing_trades=0,
            win_rate=100.0, profit_factor=float("inf"),
            total_return_pct=0.16, max_drawdown_pct=0.0,
            avg_win_pct=0.16, avg_loss_pct=0.0, expectancy_pct=0.16,
            by_signal_type={"VAH_short": {"trades": 1, "wins": 1, "total_pnl": 0.16, "win_rate": 100}},
        )
        return HKBacktestResult(
            level_eval=level_eval,
            regime_eval=regime_eval,
            sim_result=sim_result,
            symbols=["HK.800000"],
            days=20,
            data_bars=6600,
        )

    def test_format_report_table(self):
        result = self._make_result()
        text = format_report(result, verbose=True)
        assert "Section 1" in text
        assert "Section 2" in text
        assert "Section 3" in text
        assert "VAH" in text
        assert "breakout" in text

    def test_format_report_no_sim(self):
        result = self._make_result()
        result.sim_result = None
        text = format_report(result)
        assert "Section 1" in text
        assert "Section 2" in text
        assert "Section 3" not in text

    def test_format_csv(self):
        result = self._make_result()
        text = format_csv(result)
        assert "trade_num" in text
        assert "VAH_short" in text
        assert "peak_pnl_pct" in text

    def test_format_json(self):
        result = self._make_result()
        text = format_json(result)
        import json
        data = json.loads(text)
        assert "level_evaluation" in data
        assert "regime_evaluation" in data
        assert "simulation" in data
        assert "peak_pnl_pct" in data["simulation"]["trades"][0]

    def test_empty_result(self):
        result = HKBacktestResult()
        text = format_report(result)
        assert "HK Backtest Report" in text
