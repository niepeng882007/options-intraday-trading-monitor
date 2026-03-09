from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.backtest.engine import BacktestEngine
from src.backtest.trade_tracker import BacktestResult, Trade, TradeTracker
from src.indicator.engine import IndicatorResult
from src.strategy.loader import StrategyConfig, load_strategy_file
from src.strategy.matcher import RuleMatcher

ET = timezone(timedelta(hours=-5))


def _make_bars(
    n: int = 50,
    base_price: float = 100.0,
    start: str = "2025-03-20 09:30",
    trend: float = 0.0,
) -> pd.DataFrame:
    """Generate synthetic 1-minute OHLCV bars for testing."""
    np.random.seed(42)
    dates = pd.date_range(start, periods=n, freq="1min", tz="America/New_York")
    noise = np.random.randn(n) * 0.3
    drift = np.arange(n) * trend
    close = base_price + np.cumsum(noise) + drift
    high = close + np.abs(np.random.randn(n) * 0.2)
    low = close - np.abs(np.random.randn(n) * 0.2)
    opn = close + np.random.randn(n) * 0.05
    volume = np.random.randint(5000, 50000, size=n)
    return pd.DataFrame(
        {"Open": opn, "High": high, "Low": low, "Close": close, "Volume": volume},
        index=dates,
    )


def _make_multi_day_bars(
    days: int = 2,
    bars_per_day: int = 300,
    base_price: float = 500.0,
) -> pd.DataFrame:
    """Generate multi-day 1m bars."""
    frames = []
    price = base_price
    for d in range(days):
        day_date = datetime(2025, 3, 20 + d)
        start = f"{day_date.strftime('%Y-%m-%d')} 09:30"
        df = _make_bars(n=bars_per_day, base_price=price, start=start)
        frames.append(df)
        price = float(df["Close"].iloc[-1])
    return pd.concat(frames)


SIMPLE_CALL_STRATEGY = {
    "strategy_id": "test-call",
    "name": "Test Call Strategy",
    "enabled": True,
    "watchlist": {
        "underlyings": ["SPY"],
        "option_filter": {"type": "call"},
    },
    "entry_conditions": {
        "operator": "AND",
        "rules": [
            {
                "indicator": "RSI",
                "field": "value",
                "comparator": "<",
                "threshold": 45,
                "timeframe": "1m",
            },
        ],
    },
    "exit_conditions": {
        "operator": "OR",
        "rules": [
            {"type": "take_profit_pct", "threshold": 0.005},
            {"type": "stop_loss_pct", "threshold": -0.003},
            {"type": "time_exit", "minutes_before_close": 15},
        ],
    },
}

SIMPLE_PUT_STRATEGY = {
    "strategy_id": "test-put",
    "name": "Test Put Strategy",
    "enabled": True,
    "watchlist": {
        "underlyings": ["SPY"],
        "option_filter": {"type": "put"},
    },
    "entry_conditions": {
        "operator": "AND",
        "rules": [
            {
                "indicator": "RSI",
                "field": "value",
                "comparator": ">",
                "threshold": 55,
                "timeframe": "1m",
            },
        ],
    },
    "exit_conditions": {
        "operator": "OR",
        "rules": [
            {"type": "take_profit_pct", "threshold": 0.005},
            {"type": "stop_loss_pct", "threshold": -0.003},
        ],
    },
}


class TestTradeTracker:
    def test_call_trade_pnl(self):
        tracker = TradeTracker()
        t0 = datetime(2025, 3, 20, 10, 0, tzinfo=ET)
        t1 = datetime(2025, 3, 20, 10, 30, tzinfo=ET)

        tracker.open_trade("s1", "Strategy 1", "SPY", "call", 500.0, t0, 85, "A")
        trade = tracker.close_trade("s1", "SPY", 505.0, t1, "止盈")

        assert trade is not None
        assert trade.stock_pnl_pct == pytest.approx(1.0, abs=0.01)
        assert trade.direction_pnl_pct == pytest.approx(1.0, abs=0.01)  # call: same as stock
        assert trade.holding_minutes == 30.0

    def test_put_trade_pnl(self):
        tracker = TradeTracker()
        t0 = datetime(2025, 3, 20, 10, 0, tzinfo=ET)
        t1 = datetime(2025, 3, 20, 10, 30, tzinfo=ET)

        tracker.open_trade("s1", "Strategy 1", "SPY", "put", 500.0, t0)
        trade = tracker.close_trade("s1", "SPY", 495.0, t1, "止盈")

        assert trade is not None
        assert trade.stock_pnl_pct == pytest.approx(-1.0, abs=0.01)  # stock dropped
        assert trade.direction_pnl_pct == pytest.approx(1.0, abs=0.01)  # put profits from drop

    def test_win_rate_and_profit_factor(self):
        tracker = TradeTracker()
        t0 = datetime(2025, 3, 20, 10, 0, tzinfo=ET)

        # 2 winners, 1 loser
        tracker.open_trade("s1", "S1", "SPY", "call", 100.0, t0)
        tracker.close_trade("s1", "SPY", 102.0, t0 + timedelta(minutes=10), "TP")

        tracker.open_trade("s1", "S1", "SPY", "call", 100.0, t0 + timedelta(minutes=15))
        tracker.close_trade("s1", "SPY", 101.5, t0 + timedelta(minutes=25), "TP")

        tracker.open_trade("s1", "S1", "SPY", "call", 100.0, t0 + timedelta(minutes=30))
        tracker.close_trade("s1", "SPY", 99.0, t0 + timedelta(minutes=40), "SL")

        result = tracker.compute_results()
        assert result.total_trades == 3
        assert result.winning_trades == 2
        assert result.losing_trades == 1
        assert result.win_rate == pytest.approx(66.67, abs=0.1)
        assert result.profit_factor > 1.0

    def test_max_drawdown(self):
        tracker = TradeTracker()
        t0 = datetime(2025, 3, 20, 10, 0, tzinfo=ET)

        # Win +2%, then lose -3%, then win +1%
        tracker.open_trade("s1", "S1", "SPY", "call", 100.0, t0)
        tracker.close_trade("s1", "SPY", 102.0, t0 + timedelta(minutes=10), "TP")

        tracker.open_trade("s1", "S1", "SPY", "call", 100.0, t0 + timedelta(minutes=15))
        tracker.close_trade("s1", "SPY", 97.0, t0 + timedelta(minutes=25), "SL")

        tracker.open_trade("s1", "S1", "SPY", "call", 100.0, t0 + timedelta(minutes=30))
        tracker.close_trade("s1", "SPY", 101.0, t0 + timedelta(minutes=40), "TP")

        result = tracker.compute_results()
        # Peak after first trade: +2%, then drops to +2% - 3% = -1%, drawdown = 3%
        assert result.max_drawdown_pct == pytest.approx(3.0, abs=0.1)

    def test_force_close(self):
        tracker = TradeTracker()
        t0 = datetime(2025, 3, 20, 10, 0, tzinfo=ET)
        t_close = datetime(2025, 3, 20, 15, 45, tzinfo=ET)

        tracker.open_trade("s1", "S1", "SPY", "call", 500.0, t0)
        tracker.open_trade("s2", "S2", "AAPL", "put", 200.0, t0)

        closed = tracker.force_close_all(
            {"SPY": 502.0, "AAPL": 198.0}, t_close, "日终强平"
        )
        assert len(closed) == 2
        assert tracker.get_open_trade("s1", "SPY") is None
        assert tracker.get_open_trade("s2", "AAPL") is None

        result = tracker.compute_results()
        assert result.total_trades == 2

    def test_by_strategy_breakdown(self):
        tracker = TradeTracker()
        t0 = datetime(2025, 3, 20, 10, 0, tzinfo=ET)

        tracker.open_trade("s1", "S1", "SPY", "call", 100.0, t0)
        tracker.close_trade("s1", "SPY", 101.0, t0 + timedelta(minutes=10), "TP")

        tracker.open_trade("s2", "S2", "SPY", "put", 100.0, t0 + timedelta(minutes=15))
        tracker.close_trade("s2", "SPY", 99.5, t0 + timedelta(minutes=25), "TP")

        result = tracker.compute_results()
        assert "s1" in result.by_strategy
        assert "s2" in result.by_strategy
        assert result.by_strategy["s1"]["trades"] == 1
        assert result.by_strategy["s2"]["trades"] == 1


class TestBacktestEngine:
    def test_warmup_then_signal(self):
        """Verify engine doesn't trade during warmup period."""
        strategy = StrategyConfig(SIMPLE_CALL_STRATEGY)
        # Only 100 bars — all should be warmup (need 260)
        bars = _make_bars(100, base_price=500.0)
        engine = BacktestEngine([strategy], ["SPY"])
        result = engine.run({"SPY": bars})
        assert result.total_trades == 0

    def test_exit_stop_loss(self):
        """Verify stop loss triggers."""
        # Build strategy with tight stop loss
        strat_data = {
            **SIMPLE_CALL_STRATEGY,
            "entry_conditions": {
                "operator": "AND",
                "rules": [
                    # RSI < 100 to always trigger
                    {
                        "indicator": "RSI",
                        "field": "value",
                        "comparator": "<",
                        "threshold": 100,
                        "timeframe": "1m",
                    },
                ],
            },
            "exit_conditions": {
                "operator": "OR",
                "rules": [
                    {"type": "stop_loss_pct", "threshold": -0.001},
                ],
            },
        }
        strategy = StrategyConfig(strat_data)
        # Create downtrending bars to trigger stop loss
        bars = _make_bars(300, base_price=500.0, trend=-0.05)
        engine = BacktestEngine([strategy], ["SPY"])
        result = engine.run({"SPY": bars})
        # Should have at least 1 trade that exited via stop loss
        if result.total_trades > 0:
            has_sl = any("止损" in t.exit_reason for t in result.trades)
            has_force = any("日终强平" in t.exit_reason for t in result.trades)
            assert has_sl or has_force

    def test_trading_window_respected(self):
        """Strategy with narrow trading window should only trade in that window."""
        strat_data = {
            **SIMPLE_CALL_STRATEGY,
            "trading_window": {
                "start": "14:00",
                "end": "15:00",
                "timezone": "US/Eastern",
            },
            "entry_conditions": {
                "operator": "AND",
                "rules": [
                    {
                        "indicator": "RSI",
                        "field": "value",
                        "comparator": "<",
                        "threshold": 100,
                        "timeframe": "1m",
                    },
                ],
            },
        }
        strategy = StrategyConfig(strat_data)
        bars = _make_bars(300, base_price=500.0)
        engine = BacktestEngine([strategy], ["SPY"])
        result = engine.run({"SPY": bars})
        # All entries should be within 14:00-15:00 window
        for trade in result.trades:
            if trade.exit_reason != "日终强平":
                assert 14 <= trade.entry_time.hour <= 15

    def test_state_resets_between_days(self):
        """State manager should reset between trading days."""
        strategy = StrategyConfig(SIMPLE_CALL_STRATEGY)
        bars = _make_multi_day_bars(days=2, bars_per_day=300, base_price=500.0)
        engine = BacktestEngine([strategy], ["SPY"])
        result = engine.run({"SPY": bars})
        # Just verify it runs without error; exact trade count depends on data
        assert isinstance(result, BacktestResult)

    def test_put_direction_pnl(self):
        """Put trades should have inverted P&L direction."""
        strategy = StrategyConfig(SIMPLE_PUT_STRATEGY)
        bars = _make_bars(300, base_price=500.0, trend=-0.02)
        engine = BacktestEngine([strategy], ["SPY"])
        result = engine.run({"SPY": bars})
        for trade in result.trades:
            if trade.direction == "put":
                # direction_pnl should be opposite of stock_pnl
                assert trade.direction_pnl_pct == pytest.approx(
                    -trade.stock_pnl_pct, abs=0.001
                )

    def test_empty_data(self):
        """Engine should handle empty data gracefully."""
        strategy = StrategyConfig(SIMPLE_CALL_STRATEGY)
        engine = BacktestEngine([strategy], ["SPY"])
        result = engine.run({})
        assert result.total_trades == 0

    def test_multiple_strategies(self):
        """Engine should handle multiple strategies simultaneously."""
        s1 = StrategyConfig(SIMPLE_CALL_STRATEGY)
        s2 = StrategyConfig(SIMPLE_PUT_STRATEGY)
        bars = _make_bars(300, base_price=500.0)
        engine = BacktestEngine([s1, s2], ["SPY"])
        result = engine.run({"SPY": bars})
        assert isinstance(result, BacktestResult)


# ── Fix B: New tests for intra-bar, PUT exit, trailing stop, cooldown ──

ALWAYS_ENTRY_CALL = {
    "strategy_id": "test-always-call",
    "name": "Always Entry Call",
    "enabled": True,
    "watchlist": {
        "underlyings": ["SPY"],
        "option_filter": {"type": "call"},
    },
    "entry_conditions": {
        "operator": "AND",
        "rules": [
            {
                "indicator": "RSI",
                "field": "value",
                "comparator": "<",
                "threshold": 100,
                "timeframe": "1m",
            },
        ],
    },
    "exit_conditions": {
        "operator": "OR",
        "rules": [
            {"type": "take_profit_pct", "threshold": 0.005},
            {"type": "stop_loss_pct", "threshold": -0.003},
        ],
    },
}


class TestIntraBarEntry:
    def test_intra_bar_entry_triggers(self):
        """Intra-bar simulation should produce trades via partial-candle indicators."""
        # Use a strategy that always enters (RSI < 100)
        strategy = StrategyConfig(ALWAYS_ENTRY_CALL)
        bars = _make_bars(300, base_price=500.0)
        engine = BacktestEngine([strategy], ["SPY"], midday_no_trade=False)
        result = engine.run({"SPY": bars})
        # With intra-bar simulation + always-true entry, we expect trades
        assert result.total_trades > 0


class TestPutExitDirection:
    def test_put_take_profit_on_price_drop(self):
        """PUT: stock price drop should trigger take-profit (positive pnl)."""
        strategy = StrategyConfig(SIMPLE_PUT_STRATEGY)
        matcher = RuleMatcher()
        # Entry at $500, stock drops to $495 → +1% for PUT
        signal = matcher.evaluate_exit(
            strategy, "SPY",
            current_price=495.0,
            entry_price=500.0,
            minutes_to_close=120,
            direction="put",
        )
        assert signal is not None
        assert "止盈" in signal.exit_reason

    def test_put_stop_loss_on_price_rise(self):
        """PUT: stock price rise should trigger stop-loss (negative pnl)."""
        strategy = StrategyConfig(SIMPLE_PUT_STRATEGY)
        matcher = RuleMatcher()
        # Entry at $500, stock rises to $502 → -0.4% for PUT (exceeds -0.3% SL)
        signal = matcher.evaluate_exit(
            strategy, "SPY",
            current_price=502.0,
            entry_price=500.0,
            minutes_to_close=120,
            direction="put",
        )
        assert signal is not None
        assert "止损" in signal.exit_reason

    def test_call_take_profit_on_price_rise(self):
        """CALL: stock price rise should trigger take-profit (sanity check)."""
        strategy = StrategyConfig(SIMPLE_CALL_STRATEGY)
        matcher = RuleMatcher()
        signal = matcher.evaluate_exit(
            strategy, "SPY",
            current_price=505.0,
            entry_price=500.0,
            minutes_to_close=120,
            direction="call",
        )
        assert signal is not None
        assert "止盈" in signal.exit_reason


class TestPutTrailingStop:
    def _make_trailing_put_strategy(self):
        return StrategyConfig({
            "strategy_id": "test-trailing-put",
            "name": "Trailing Put",
            "enabled": True,
            "watchlist": {
                "underlyings": ["SPY"],
                "option_filter": {"type": "put"},
            },
            "entry_conditions": {
                "operator": "AND",
                "rules": [
                    {"indicator": "RSI", "field": "value", "comparator": ">",
                     "threshold": 50, "timeframe": "1m"},
                ],
            },
            "exit_conditions": {
                "operator": "OR",
                "rules": [
                    {"type": "trailing_stop", "activation_pct": 0.01,
                     "trail_pct": 0.005},
                ],
            },
        })

    def test_put_trailing_stop_triggers(self):
        """PUT trailing stop: price drops (profit), then bounces → should trigger."""
        strategy = self._make_trailing_put_strategy()
        matcher = RuleMatcher()
        # Entry $500, lowest $490 (PUT profit 2%), current $495 (bounce from low)
        # peak_pnl = (500-490)/500 = 2% >= 1% activation
        # drawdown_from_peak = (495-490)/490 ≈ 1.02% >= 0.5% trail
        signal = matcher.evaluate_exit(
            strategy, "SPY",
            current_price=495.0,
            entry_price=500.0,
            minutes_to_close=120,
            lowest_price=490.0,
            direction="put",
        )
        assert signal is not None
        assert "追踪止盈" in signal.exit_reason

    def test_put_trailing_stop_not_activated(self):
        """PUT trailing stop: price hasn't dropped enough to activate."""
        strategy = self._make_trailing_put_strategy()
        matcher = RuleMatcher()
        # Entry $500, lowest $499 (PUT profit 0.2%), current $499.5
        # peak_pnl = (500-499)/500 = 0.2% < 1% activation → no trigger
        signal = matcher.evaluate_exit(
            strategy, "SPY",
            current_price=499.5,
            entry_price=500.0,
            minutes_to_close=120,
            lowest_price=499.0,
            direction="put",
        )
        assert signal is None


class TestCooldownPreventsReentry:
    def test_cooldown_prevents_reentry(self):
        """Cooldown should prevent same (strategy, symbol) from re-entering too soon."""
        strat_data = {
            **ALWAYS_ENTRY_CALL,
            "strategy_id": "test-cooldown",
            "notification": {"cooldown_seconds": 600},
            "exit_conditions": {
                "operator": "OR",
                "rules": [
                    # Very tight stop loss to force quick exit
                    {"type": "stop_loss_pct", "threshold": -0.0001},
                ],
            },
        }
        strategy = StrategyConfig(strat_data)
        bars = _make_bars(300, base_price=500.0)
        engine = BacktestEngine([strategy], ["SPY"], midday_no_trade=False)
        result = engine.run({"SPY": bars})

        if result.total_trades >= 2:
            # Check that entries are at least 600 seconds (10 min) apart
            entries = sorted(t.entry_time for t in result.trades)
            for i in range(1, len(entries)):
                gap = (entries[i] - entries[i - 1]).total_seconds()
                assert gap >= 600, (
                    f"Trade {i} re-entered after only {gap}s, cooldown is 600s"
                )


class TestMinMatch:
    def test_3_of_4_passes(self):
        """MIN_MATCH with 3/4 required: 3 passing rules should trigger."""
        matcher = RuleMatcher()
        strat_data = {
            "strategy_id": "test-min-match",
            "name": "Min Match Test",
            "enabled": True,
            "watchlist": {"underlyings": ["SPY"], "option_filter": {"type": "call"}},
            "entry_conditions": {
                "operator": "MIN_MATCH",
                "min_count": 3,
                "rules": [
                    {"indicator": "RSI", "field": "value", "comparator": "<",
                     "threshold": 50, "timeframe": "1m"},    # likely passes
                    {"indicator": "RSI", "field": "value", "comparator": "<",
                     "threshold": 80, "timeframe": "1m"},    # passes
                    {"indicator": "RSI", "field": "value", "comparator": "<",
                     "threshold": 90, "timeframe": "1m"},    # passes
                    {"indicator": "RSI", "field": "value", "comparator": "<",
                     "threshold": 5, "timeframe": "1m"},     # fails (RSI unlikely < 5)
                ],
            },
        }
        strategy = StrategyConfig(strat_data)
        ind = IndicatorResult(symbol="SPY", timeframe="1m", timestamp=0.0, rsi=40.0)
        indicators = {"1m": ind, "5m": None, "15m": None}
        signal = matcher.evaluate_entry(strategy, "SPY", indicators)
        assert signal is not None

    def test_2_of_4_fails(self):
        """MIN_MATCH with 3/4 required: only 2 passing should not trigger."""
        matcher = RuleMatcher()
        strat_data = {
            "strategy_id": "test-min-match-fail",
            "name": "Min Match Fail",
            "enabled": True,
            "watchlist": {"underlyings": ["SPY"], "option_filter": {"type": "call"}},
            "entry_conditions": {
                "operator": "MIN_MATCH",
                "min_count": 3,
                "rules": [
                    {"indicator": "RSI", "field": "value", "comparator": "<",
                     "threshold": 80, "timeframe": "1m"},    # passes
                    {"indicator": "RSI", "field": "value", "comparator": "<",
                     "threshold": 90, "timeframe": "1m"},    # passes
                    {"indicator": "RSI", "field": "value", "comparator": "<",
                     "threshold": 5, "timeframe": "1m"},     # fails
                    {"indicator": "RSI", "field": "value", "comparator": "<",
                     "threshold": 3, "timeframe": "1m"},     # fails
                ],
            },
        }
        strategy = StrategyConfig(strat_data)
        ind = IndicatorResult(symbol="SPY", timeframe="1m", timestamp=0.0, rsi=40.0)
        indicators = {"1m": ind, "5m": None, "15m": None}
        signal = matcher.evaluate_entry(strategy, "SPY", indicators)
        assert signal is None


class TestIndicatorExit:
    def test_bb_middle_target_call(self):
        """indicator_target exit: call hits BB middle → take profit."""
        strat_data = {
            "strategy_id": "test-ind-exit",
            "name": "Indicator Exit Test",
            "enabled": True,
            "watchlist": {"underlyings": ["SPY"], "option_filter": {"type": "call"}},
            "entry_conditions": {"operator": "AND", "rules": []},
            "exit_conditions": {
                "operator": "OR",
                "rules": [
                    {"type": "indicator_target", "indicator": "BOLLINGER",
                     "field": "middle", "timeframe": "15m"},
                ],
            },
        }
        strategy = StrategyConfig(strat_data)
        matcher = RuleMatcher()
        ind_15m = IndicatorResult(
            symbol="SPY", timeframe="15m", timestamp=0.0,
            bb_middle=505.0,
        )
        # Price above BB middle → should trigger for call
        signal = matcher.evaluate_exit(
            strategy, "SPY",
            current_price=506.0, entry_price=500.0,
            minutes_to_close=120, direction="call",
            indicators_by_tf={"1m": None, "5m": None, "15m": ind_15m},
        )
        assert signal is not None
        assert "指标止盈" in signal.exit_reason

    def test_bb_middle_target_put(self):
        """indicator_target exit: put price below BB middle → take profit."""
        strat_data = {
            "strategy_id": "test-ind-exit-put",
            "name": "Indicator Exit Put Test",
            "enabled": True,
            "watchlist": {"underlyings": ["SPY"], "option_filter": {"type": "put"}},
            "entry_conditions": {"operator": "AND", "rules": []},
            "exit_conditions": {
                "operator": "OR",
                "rules": [
                    {"type": "indicator_target", "indicator": "BOLLINGER",
                     "field": "middle", "timeframe": "15m"},
                ],
            },
        }
        strategy = StrategyConfig(strat_data)
        matcher = RuleMatcher()
        ind_15m = IndicatorResult(
            symbol="SPY", timeframe="15m", timestamp=0.0,
            bb_middle=505.0,
        )
        # Price below BB middle → should trigger for put
        signal = matcher.evaluate_exit(
            strategy, "SPY",
            current_price=504.0, entry_price=510.0,
            minutes_to_close=120, direction="put",
            indicators_by_tf={"1m": None, "5m": None, "15m": ind_15m},
        )
        assert signal is not None
        assert "指标止盈" in signal.exit_reason

    def test_indicator_target_no_trigger(self):
        """indicator_target exit: price not yet at target → no signal."""
        strat_data = {
            "strategy_id": "test-ind-exit-no",
            "name": "Indicator Exit No Trigger",
            "enabled": True,
            "watchlist": {"underlyings": ["SPY"], "option_filter": {"type": "call"}},
            "entry_conditions": {"operator": "AND", "rules": []},
            "exit_conditions": {
                "operator": "OR",
                "rules": [
                    {"type": "indicator_target", "indicator": "BOLLINGER",
                     "field": "middle", "timeframe": "15m"},
                ],
            },
        }
        strategy = StrategyConfig(strat_data)
        matcher = RuleMatcher()
        ind_15m = IndicatorResult(
            symbol="SPY", timeframe="15m", timestamp=0.0,
            bb_middle=510.0,
        )
        # Price below BB middle → should not trigger for call
        signal = matcher.evaluate_exit(
            strategy, "SPY",
            current_price=505.0, entry_price=500.0,
            minutes_to_close=120, direction="call",
            indicators_by_tf={"1m": None, "5m": None, "15m": ind_15m},
        )
        assert signal is None


class TestMinAdxFilter:
    """Test min_adx market context filter."""

    def _make_strategy_with_min_adx(self, min_adx: float):
        return StrategyConfig({
            **ALWAYS_ENTRY_CALL,
            "strategy_id": "test-min-adx",
            "market_context_filters": {"min_adx": min_adx},
        })

    def test_min_adx_filter_blocks_entry(self):
        """ADX=20 + min_adx=25 → entry blocked."""
        strategy = self._make_strategy_with_min_adx(25)
        engine = BacktestEngine([strategy], ["SPY"], midday_no_trade=False)
        # Manually test _check_market_context
        ind = IndicatorResult(symbol="SPY", timeframe="5m", timestamp=0.0, adx=20.0)
        indicators = {"1m": None, "5m": ind, "15m": None}
        result = engine._check_market_context(strategy, "SPY", indicators)
        assert result is False

    def test_min_adx_filter_allows_entry(self):
        """ADX=30 + min_adx=25 → entry allowed."""
        strategy = self._make_strategy_with_min_adx(25)
        engine = BacktestEngine([strategy], ["SPY"], midday_no_trade=False)
        ind = IndicatorResult(symbol="SPY", timeframe="5m", timestamp=0.0, adx=30.0)
        indicators = {"1m": None, "5m": ind, "15m": None}
        result = engine._check_market_context(strategy, "SPY", indicators)
        assert result is True


class TestBBPiercing:
    def test_yaml_loads_call(self):
        """Verify call YAML loads without errors."""
        config = load_strategy_file("config/strategies/bb_piercing_reversion_call.yaml")
        assert config is not None
        assert config.strategy_id == "bb-piercing-reversion-call"
        assert config.option_filter.get("type") == "call"
        assert config.enabled is True

    def test_yaml_loads_put(self):
        """Verify put YAML loads without errors."""
        config = load_strategy_file("config/strategies/bb_piercing_reversion_put.yaml")
        assert config is not None
        assert config.strategy_id == "bb-piercing-reversion-put"
        assert config.option_filter.get("type") == "put"
        assert config.enabled is True
