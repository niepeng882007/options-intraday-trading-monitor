import os
import tempfile
import time

import pytest
import yaml

from src.indicator.engine import IndicatorResult
from src.strategy.loader import StrategyConfig, StrategyLoader, load_strategy_file
from src.strategy.matcher import RuleMatcher, Signal
from src.strategy.state import StrategyState, StrategyStateManager


# ── Strategy 1: VWAP Low Volume Ambush ──

VWAP_AMBUSH_STRATEGY = {
    "strategy_id": "vwap-low-vol-ambush",
    "name": "VWAP 极度缩量埋伏",
    "enabled": True,
    "watchlist": {
        "underlyings": ["SPY"],
        "option_filter": {"type": "call", "max_dte": 0, "moneyness": "ATM"},
    },
    "entry_conditions": {
        "operator": "AND",
        "rules": [
            {
                "indicator": "PRICE",
                "field": "day_change_pct",
                "comparator": ">",
                "threshold": -0.15,
                "timeframe": "5m",
            },
            {
                "indicator": "PRICE",
                "field": "close",
                "comparator": ">",
                "reference_field": "ema_50",
                "timeframe": "5m",
            },
            {
                "indicator": "PRICE",
                "field": "abs_vwap_distance_pct",
                "comparator": "<",
                "threshold": 0.08,
                "timeframe": "5m",
            },
            {
                "indicator": "CANDLE",
                "field": "body_pct",
                "comparator": "<",
                "threshold": 0.05,
                "timeframe": "5m",
            },
            {
                "indicator": "PRICE",
                "field": "volume_ratio",
                "comparator": "<",
                "threshold": 0.5,
                "timeframe": "5m",
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
    "notification": {"cooldown_seconds": 180, "priority": "high"},
    "entry_quality_filters": {
        "min_score": 65,
        "prefer_low_volume": True,
        "max_distance_from_vwap_pct": 0.15,
    },
}

# ── Strategy 2: BB Squeeze ──

BB_SQUEEZE_STRATEGY = {
    "strategy_id": "bb-squeeze-ambush",
    "name": "布林带极限挤压",
    "enabled": True,
    "watchlist": {"underlyings": ["SPY"]},
    "entry_conditions": {
        "operator": "AND",
        "rules": [
            {
                "indicator": "PRICE",
                "field": "day_change_pct",
                "comparator": ">",
                "threshold": -0.3,
                "timeframe": "5m",
            },
            {
                "indicator": "BOLLINGER",
                "field": "width_percentile",
                "comparator": "<",
                "threshold": 10,
                "timeframe": "5m",
            },
            {
                "indicator": "PRICE",
                "field": "close",
                "comparator": ">",
                "reference_field": "vwap",
                "timeframe": "5m",
            },
            {
                "indicator": "RSI",
                "field": "value",
                "comparator": ">",
                "threshold": 48,
                "timeframe": "5m",
            },
        ],
    },
    "exit_conditions": {
        "operator": "OR",
        "rules": [
            {"type": "take_profit_pct", "threshold": 0.015},
            {"type": "trailing_stop", "activation_pct": 0.008, "trail_pct": 0.003},
            {"type": "stop_loss_pct", "threshold": -0.005},
        ],
    },
    "notification": {"cooldown_seconds": 300, "priority": "high"},
    "entry_quality_filters": {
        "min_score": 60,
        "max_bb_width_pct": 0.20,
        "prefer_above_ema200": True,
    },
}

# ── Strategy 3: Extreme Oversold Reversal ──

OVERSOLD_REVERSAL_STRATEGY = {
    "strategy_id": "extreme-oversold-reversal",
    "name": "极端超卖钝化反转",
    "enabled": True,
    "watchlist": {"underlyings": ["AAPL"]},
    "entry_conditions": {
        "operator": "AND",
        "rules": [
            {
                "indicator": "RSI",
                "field": "value",
                "comparator": "<",
                "threshold": 30,
                "timeframe": "15m",
            },
            {
                "indicator": "PRICE",
                "field": "vwap_distance_pct",
                "comparator": "<",
                "threshold": -1.2,
                "timeframe": "5m",
            },
            {
                "indicator": "PRICE",
                "field": "close",
                "comparator": ">",
                "reference_field": "prev_bar_high",
                "timeframe": "5m",
            },
            {
                "indicator": "PRICE",
                "field": "volume_spike",
                "comparator": ">",
                "threshold": 1.5,
                "timeframe": "5m",
            },
        ],
    },
    "exit_conditions": {
        "operator": "OR",
        "rules": [
            {"type": "take_profit_pct", "threshold": 0.008},
            {"type": "stop_loss_pct", "threshold": -0.003},
        ],
    },
    "notification": {"cooldown_seconds": 120, "priority": "high"},
    "entry_quality_filters": {
        "min_score": 50,
        "max_rsi_15m": 33,
        "min_vwap_deviation_pct": 1.0,
    },
}


class TestStrategyConfig:
    def test_basic_properties(self):
        config = StrategyConfig(VWAP_AMBUSH_STRATEGY)
        assert config.strategy_id == "vwap-low-vol-ambush"
        assert config.name == "VWAP 极度缩量埋伏"
        assert config.enabled is True
        assert "SPY" in config.underlyings
        assert config.cooldown_seconds == 180
        assert config.priority == "high"

    def test_bb_squeeze_properties(self):
        config = StrategyConfig(BB_SQUEEZE_STRATEGY)
        assert config.strategy_id == "bb-squeeze-ambush"
        assert config.entry_conditions["rules"][1]["indicator"] == "BOLLINGER"

    def test_oversold_reversal_properties(self):
        config = StrategyConfig(OVERSOLD_REVERSAL_STRATEGY)
        assert config.strategy_id == "extreme-oversold-reversal"
        assert config.entry_conditions["rules"][0]["timeframe"] == "15m"


class TestStrategyLoader:
    def test_load_from_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(VWAP_AMBUSH_STRATEGY, f)
            f.flush()
            config = load_strategy_file(f.name)
        os.unlink(f.name)

        assert config is not None
        assert config.strategy_id == "vwap-low-vol-ambush"

    def test_load_all_from_directory(self):
        strategies = [VWAP_AMBUSH_STRATEGY, BB_SQUEEZE_STRATEGY, OVERSOLD_REVERSAL_STRATEGY]
        with tempfile.TemporaryDirectory() as tmpdir:
            for i, strat in enumerate(strategies):
                with open(os.path.join(tmpdir, f"strat_{i}.yaml"), "w") as f:
                    yaml.dump(strat, f)

            loader = StrategyLoader(tmpdir)
            loader.load_all()
            assert len(loader.strategies) == 3
            assert len(loader.get_active()) == 3

    def test_enable_disable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "test.yaml"), "w") as f:
                yaml.dump(VWAP_AMBUSH_STRATEGY, f)

            loader = StrategyLoader(tmpdir)
            loader.load_all()
            assert loader.set_enabled("vwap-low-vol-ambush", False)
            assert len(loader.get_active()) == 0
            assert loader.set_enabled("vwap-low-vol-ambush", True)
            assert len(loader.get_active()) == 1

    def test_invalid_strategy_rejected(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump({"name": "missing fields"}, f)
            f.flush()
            config = load_strategy_file(f.name)
        os.unlink(f.name)
        assert config is None


class TestRuleMatcher:
    def setup_method(self):
        self.matcher = RuleMatcher()

    # ── Strategy 1: VWAP Low-Volume Ambush ──

    def test_vwap_ambush_triggers(self):
        strategy = StrategyConfig(VWAP_AMBUSH_STRATEGY)
        indicators = {
            "5m": IndicatorResult(
                symbol="SPY", timeframe="5m", timestamp=time.time(),
                close=450.0, ema_50=449.5, vwap=450.02,
                vwap_distance_pct=-0.004, abs_vwap_distance_pct=0.004,
                candle_body_pct=0.02, volume_ratio=0.3,
                day_change_pct=0.1,
            ),
        }
        signal = self.matcher.evaluate_entry(strategy, "SPY", indicators)
        assert signal is not None
        assert signal.signal_type == "entry"

    def test_vwap_ambush_fails_when_above_vwap_range(self):
        strategy = StrategyConfig(VWAP_AMBUSH_STRATEGY)
        indicators = {
            "5m": IndicatorResult(
                symbol="SPY", timeframe="5m", timestamp=time.time(),
                close=450.0, ema_50=449.5, vwap=448.0,
                vwap_distance_pct=0.45, abs_vwap_distance_pct=0.45,
                candle_body_pct=0.02, volume_ratio=0.3,
                day_change_pct=0.1,
            ),
        }
        signal = self.matcher.evaluate_entry(strategy, "SPY", indicators)
        assert signal is None

    def test_vwap_ambush_fails_below_ema50(self):
        strategy = StrategyConfig(VWAP_AMBUSH_STRATEGY)
        indicators = {
            "5m": IndicatorResult(
                symbol="SPY", timeframe="5m", timestamp=time.time(),
                close=448.0, ema_50=449.5, vwap=448.01,
                vwap_distance_pct=-0.002, abs_vwap_distance_pct=0.002,
                candle_body_pct=0.02, volume_ratio=0.3,
                day_change_pct=0.1,
            ),
        }
        signal = self.matcher.evaluate_entry(strategy, "SPY", indicators)
        assert signal is None

    def test_vwap_ambush_both_body_and_volume_needed(self):
        """AND logic: both small candle body AND low volume required."""
        strategy = StrategyConfig(VWAP_AMBUSH_STRATEGY)
        # Small body but high volume → should fail (AND)
        indicators = {
            "5m": IndicatorResult(
                symbol="SPY", timeframe="5m", timestamp=time.time(),
                close=450.0, ema_50=449.5, vwap=450.01,
                vwap_distance_pct=-0.002, abs_vwap_distance_pct=0.002,
                candle_body_pct=0.01, volume_ratio=1.5,
                day_change_pct=0.1,
            ),
        }
        signal = self.matcher.evaluate_entry(strategy, "SPY", indicators)
        assert signal is None  # Changed from OR to AND

    def test_vwap_ambush_both_conditions_pass(self):
        """AND logic: both body small AND volume low → triggers."""
        strategy = StrategyConfig(VWAP_AMBUSH_STRATEGY)
        indicators = {
            "5m": IndicatorResult(
                symbol="SPY", timeframe="5m", timestamp=time.time(),
                close=450.0, ema_50=449.5, vwap=450.01,
                vwap_distance_pct=-0.002, abs_vwap_distance_pct=0.002,
                candle_body_pct=0.01, volume_ratio=0.3,
                day_change_pct=0.1,
            ),
        }
        signal = self.matcher.evaluate_entry(strategy, "SPY", indicators)
        assert signal is not None

    # ── Strategy 2: BB Squeeze ──

    def test_bb_squeeze_triggers(self):
        strategy = StrategyConfig(BB_SQUEEZE_STRATEGY)
        indicators = {
            "5m": IndicatorResult(
                symbol="SPY", timeframe="5m", timestamp=time.time(),
                close=450.0, vwap=449.5,
                bb_width_percentile=5.0, rsi=55.0,
                day_change_pct=0.1,
            ),
        }
        signal = self.matcher.evaluate_entry(strategy, "SPY", indicators)
        assert signal is not None

    def test_bb_squeeze_fails_wide_bands(self):
        strategy = StrategyConfig(BB_SQUEEZE_STRATEGY)
        indicators = {
            "5m": IndicatorResult(
                symbol="SPY", timeframe="5m", timestamp=time.time(),
                close=450.0, vwap=449.5,
                bb_width_percentile=50.0, rsi=55.0,
                day_change_pct=0.1,
            ),
        }
        signal = self.matcher.evaluate_entry(strategy, "SPY", indicators)
        assert signal is None

    def test_bb_squeeze_fails_below_vwap(self):
        strategy = StrategyConfig(BB_SQUEEZE_STRATEGY)
        indicators = {
            "5m": IndicatorResult(
                symbol="SPY", timeframe="5m", timestamp=time.time(),
                close=440.0, vwap=445.0,
                bb_width_percentile=5.0, rsi=55.0,
                day_change_pct=0.1,
            ),
        }
        signal = self.matcher.evaluate_entry(strategy, "SPY", indicators)
        assert signal is None

    # ── Strategy 3: Extreme Oversold Reversal ──

    def test_oversold_reversal_triggers(self):
        strategy = StrategyConfig(OVERSOLD_REVERSAL_STRATEGY)
        indicators = {
            "15m": IndicatorResult(
                symbol="AAPL", timeframe="15m", timestamp=time.time(),
                rsi=20.0,
            ),
            "5m": IndicatorResult(
                symbol="AAPL", timeframe="5m", timestamp=time.time(),
                close=175.0, vwap=178.0,
                vwap_distance_pct=-1.69, prev_bar_high=174.5,
                volume_spike=2.0,
            ),
        }
        signal = self.matcher.evaluate_entry(strategy, "AAPL", indicators)
        assert signal is not None

    def test_oversold_reversal_fails_rsi_not_extreme(self):
        strategy = StrategyConfig(OVERSOLD_REVERSAL_STRATEGY)
        indicators = {
            "15m": IndicatorResult(
                symbol="AAPL", timeframe="15m", timestamp=time.time(),
                rsi=35.0,
            ),
            "5m": IndicatorResult(
                symbol="AAPL", timeframe="5m", timestamp=time.time(),
                close=175.0, vwap=178.0,
                vwap_distance_pct=-1.69, prev_bar_high=174.5,
                volume_spike=2.0,
            ),
        }
        signal = self.matcher.evaluate_entry(strategy, "AAPL", indicators)
        assert signal is None

    def test_oversold_reversal_fails_no_bar_confirmation(self):
        """close < prev_bar_high → K-line reversal not confirmed."""
        strategy = StrategyConfig(OVERSOLD_REVERSAL_STRATEGY)
        indicators = {
            "15m": IndicatorResult(
                symbol="AAPL", timeframe="15m", timestamp=time.time(),
                rsi=20.0,
            ),
            "5m": IndicatorResult(
                symbol="AAPL", timeframe="5m", timestamp=time.time(),
                close=174.0, vwap=178.0,
                vwap_distance_pct=-2.25, prev_bar_high=174.5,
                volume_spike=2.0,
            ),
        }
        signal = self.matcher.evaluate_entry(strategy, "AAPL", indicators)
        assert signal is None

    def test_oversold_reversal_fails_vwap_not_deviated(self):
        strategy = StrategyConfig(OVERSOLD_REVERSAL_STRATEGY)
        indicators = {
            "15m": IndicatorResult(
                symbol="AAPL", timeframe="15m", timestamp=time.time(),
                rsi=20.0,
            ),
            "5m": IndicatorResult(
                symbol="AAPL", timeframe="5m", timestamp=time.time(),
                close=177.5, vwap=178.0,
                vwap_distance_pct=-0.28, prev_bar_high=177.0,
                volume_spike=2.0,
            ),
        }
        signal = self.matcher.evaluate_entry(strategy, "AAPL", indicators)
        assert signal is None

    def test_oversold_reversal_fails_no_volume_spike(self):
        """No volume spike → reversal not confirmed."""
        strategy = StrategyConfig(OVERSOLD_REVERSAL_STRATEGY)
        indicators = {
            "15m": IndicatorResult(
                symbol="AAPL", timeframe="15m", timestamp=time.time(),
                rsi=20.0,
            ),
            "5m": IndicatorResult(
                symbol="AAPL", timeframe="5m", timestamp=time.time(),
                close=175.0, vwap=178.0,
                vwap_distance_pct=-1.69, prev_bar_high=174.5,
                volume_spike=0.8,
            ),
        }
        signal = self.matcher.evaluate_entry(strategy, "AAPL", indicators)
        assert signal is None

    # ── Reference field comparison ──

    def test_reference_field_comparison(self):
        """reference_field: close > ema_50 should use the indicator's ema_50 value."""
        strategy_data = {
            "strategy_id": "ref-test",
            "name": "Ref Test",
            "enabled": True,
            "watchlist": {"underlyings": ["TEST"]},
            "entry_conditions": {
                "operator": "AND",
                "rules": [
                    {
                        "indicator": "PRICE",
                        "field": "close",
                        "comparator": ">",
                        "reference_field": "ema_50",
                        "timeframe": "5m",
                    },
                ],
            },
        }
        strategy = StrategyConfig(strategy_data)
        indicators = {
            "5m": IndicatorResult(
                symbol="TEST", timeframe="5m", timestamp=time.time(),
                close=100.0, ema_50=99.0,
            ),
        }
        signal = self.matcher.evaluate_entry(strategy, "TEST", indicators)
        assert signal is not None

        indicators_below = {
            "5m": IndicatorResult(
                symbol="TEST", timeframe="5m", timestamp=time.time(),
                close=98.0, ema_50=99.0,
            ),
        }
        signal_below = self.matcher.evaluate_entry(strategy, "TEST", indicators_below)
        assert signal_below is None

    def test_reference_field_returns_na_when_missing(self):
        strategy_data = {
            "strategy_id": "ref-missing",
            "name": "Ref Missing",
            "enabled": True,
            "watchlist": {"underlyings": ["TEST"]},
            "entry_conditions": {
                "operator": "AND",
                "rules": [
                    {
                        "indicator": "PRICE",
                        "field": "close",
                        "comparator": ">",
                        "reference_field": "ema_200",
                        "timeframe": "5m",
                    },
                ],
            },
        }
        strategy = StrategyConfig(strategy_data)
        indicators = {
            "5m": IndicatorResult(
                symbol="TEST", timeframe="5m", timestamp=time.time(),
                close=100.0, ema_200=None,
            ),
        }
        signal = self.matcher.evaluate_entry(strategy, "TEST", indicators)
        assert signal is None

    # ── breaks_above / breaks_below comparators ──

    def test_breaks_above(self):
        strategy_data = {
            "strategy_id": "breaks-test",
            "name": "Breaks Test",
            "enabled": True,
            "watchlist": {"underlyings": ["TEST"]},
            "entry_conditions": {
                "operator": "AND",
                "rules": [
                    {
                        "indicator": "PRICE",
                        "field": "close",
                        "comparator": "breaks_above",
                        "threshold": 100.0,
                        "timeframe": "1m",
                    },
                ],
            },
        }
        strategy = StrategyConfig(strategy_data)
        # First call: set previous value below threshold
        ind_below = {
            "1m": IndicatorResult(
                symbol="TEST", timeframe="1m", timestamp=time.time(),
                close=99.5,
            ),
        }
        self.matcher.evaluate_entry(strategy, "TEST", ind_below)
        # Second call: current above threshold with margin
        ind_above = {
            "1m": IndicatorResult(
                symbol="TEST", timeframe="1m", timestamp=time.time(),
                close=100.02,
            ),
        }
        signal = self.matcher.evaluate_entry(strategy, "TEST", ind_above)
        assert signal is not None

    def test_breaks_above_fails_no_margin(self):
        strategy_data = {
            "strategy_id": "breaks-fail",
            "name": "Breaks Fail",
            "enabled": True,
            "watchlist": {"underlyings": ["TEST"]},
            "entry_conditions": {
                "operator": "AND",
                "rules": [
                    {
                        "indicator": "PRICE",
                        "field": "close",
                        "comparator": "breaks_above",
                        "threshold": 100.0,
                        "timeframe": "1m",
                    },
                ],
            },
        }
        strategy = StrategyConfig(strategy_data)
        ind_below = {
            "1m": IndicatorResult(
                symbol="TEST", timeframe="1m", timestamp=time.time(),
                close=99.5,
            ),
        }
        self.matcher.evaluate_entry(strategy, "TEST", ind_below)
        # Exactly at threshold — should fail (no margin)
        ind_exact = {
            "1m": IndicatorResult(
                symbol="TEST", timeframe="1m", timestamp=time.time(),
                close=100.005,
            ),
        }
        signal = self.matcher.evaluate_entry(strategy, "TEST", ind_exact)
        assert signal is None

    def test_breaks_below(self):
        strategy_data = {
            "strategy_id": "breaks-below-test",
            "name": "Breaks Below",
            "enabled": True,
            "watchlist": {"underlyings": ["TEST"]},
            "entry_conditions": {
                "operator": "AND",
                "rules": [
                    {
                        "indicator": "PRICE",
                        "field": "close",
                        "comparator": "breaks_below",
                        "threshold": 100.0,
                        "timeframe": "1m",
                    },
                ],
            },
        }
        strategy = StrategyConfig(strategy_data)
        ind_above = {
            "1m": IndicatorResult(
                symbol="TEST", timeframe="1m", timestamp=time.time(),
                close=100.5,
            ),
        }
        self.matcher.evaluate_entry(strategy, "TEST", ind_above)
        ind_below = {
            "1m": IndicatorResult(
                symbol="TEST", timeframe="1m", timestamp=time.time(),
                close=99.98,
            ),
        }
        signal = self.matcher.evaluate_entry(strategy, "TEST", ind_below)
        assert signal is not None

    # ── crosses_above + reference_field fix ──

    def test_crosses_above_reference_field(self):
        """EMA9 crosses_above EMA21: should compare prev_ema9 vs prev_ema21."""
        strategy_data = {
            "strategy_id": "cross-ref-test",
            "name": "Cross Ref",
            "enabled": True,
            "watchlist": {"underlyings": ["TEST"]},
            "entry_conditions": {
                "operator": "AND",
                "rules": [
                    {
                        "indicator": "EMA",
                        "field": "ema_9",
                        "comparator": "crosses_above",
                        "reference_field": "ema_21",
                        "timeframe": "5m",
                    },
                ],
            },
        }
        strategy = StrategyConfig(strategy_data)
        # T-1: ema_9 < ema_21
        ind_t1 = {
            "5m": IndicatorResult(
                symbol="TEST", timeframe="5m", timestamp=time.time(),
                ema_9=49.0, ema_21=50.0,
            ),
        }
        signal_t1 = self.matcher.evaluate_entry(strategy, "TEST", ind_t1)
        assert signal_t1 is None

        # T: ema_9 > ema_21 (golden cross)
        ind_t2 = {
            "5m": IndicatorResult(
                symbol="TEST", timeframe="5m", timestamp=time.time(),
                ema_9=51.0, ema_21=50.5,
            ),
        }
        signal_t2 = self.matcher.evaluate_entry(strategy, "TEST", ind_t2)
        assert signal_t2 is not None

    def test_crosses_above_reference_field_no_cross(self):
        """Both above: no cross should be detected."""
        strategy_data = {
            "strategy_id": "cross-ref-no",
            "name": "Cross Ref No",
            "enabled": True,
            "watchlist": {"underlyings": ["TEST"]},
            "entry_conditions": {
                "operator": "AND",
                "rules": [
                    {
                        "indicator": "EMA",
                        "field": "ema_9",
                        "comparator": "crosses_above",
                        "reference_field": "ema_21",
                        "timeframe": "5m",
                    },
                ],
            },
        }
        strategy = StrategyConfig(strategy_data)
        ind_t1 = {
            "5m": IndicatorResult(
                symbol="TEST", timeframe="5m", timestamp=time.time(),
                ema_9=51.0, ema_21=50.0,
            ),
        }
        self.matcher.evaluate_entry(strategy, "TEST", ind_t1)

        ind_t2 = {
            "5m": IndicatorResult(
                symbol="TEST", timeframe="5m", timestamp=time.time(),
                ema_9=52.0, ema_21=50.5,
            ),
        }
        signal = self.matcher.evaluate_entry(strategy, "TEST", ind_t2)
        assert signal is None  # Already above, no cross

    # ── Exit conditions ──

    def test_exit_take_profit(self):
        """Stock price up 0.6% > 0.5% TP threshold → take profit."""
        strategy = StrategyConfig(VWAP_AMBUSH_STRATEGY)
        signal = self.matcher.evaluate_exit(
            strategy, "SPY",
            current_price=451.70,
            entry_price=449.0,
            minutes_to_close=120,
        )
        assert signal is not None
        assert "止盈" in signal.exit_reason

    def test_exit_stop_loss(self):
        """Stock price down 0.4% > 0.3% SL threshold → stop loss."""
        strategy = StrategyConfig(VWAP_AMBUSH_STRATEGY)
        signal = self.matcher.evaluate_exit(
            strategy, "SPY",
            current_price=447.65,
            entry_price=449.0,
            minutes_to_close=120,
        )
        assert signal is not None
        assert "止损" in signal.exit_reason
        assert signal.priority == "high"

    def test_exit_time_based(self):
        strategy = StrategyConfig(VWAP_AMBUSH_STRATEGY)
        signal = self.matcher.evaluate_exit(
            strategy, "SPY",
            current_price=449.5,
            entry_price=449.0,
            minutes_to_close=10,
        )
        assert signal is not None
        assert "收盘前" in signal.exit_reason

    def test_no_exit_when_within_bounds(self):
        """Stock price up 0.1% — within TP/SL bounds, no exit."""
        strategy = StrategyConfig(VWAP_AMBUSH_STRATEGY)
        signal = self.matcher.evaluate_exit(
            strategy, "SPY",
            current_price=449.5,
            entry_price=449.0,
            minutes_to_close=120,
        )
        assert signal is None

    # ── Trailing stop ──

    def test_trailing_stop_triggers(self):
        strategy_data = {
            "strategy_id": "trail-test",
            "name": "Trail Test",
            "enabled": True,
            "watchlist": {"underlyings": ["TEST"]},
            "entry_conditions": {"operator": "AND", "rules": []},
            "exit_conditions": {
                "operator": "OR",
                "rules": [
                    {
                        "type": "trailing_stop",
                        "activation_pct": 0.008,
                        "trail_pct": 0.003,
                    },
                ],
            },
        }
        strategy = StrategyConfig(strategy_data)
        # Entry at 100.0, peak at 101.0 (+1.0% > 0.8% activation),
        # current at 100.6 (0.4% drawdown from peak > 0.3% trail)
        signal = self.matcher.evaluate_exit(
            strategy, "TEST",
            current_price=100.6,
            entry_price=100.0,
            minutes_to_close=120,
            highest_price=101.0,
        )
        assert signal is not None
        assert "追踪止盈" in signal.exit_reason

    def test_trailing_stop_not_activated(self):
        strategy_data = {
            "strategy_id": "trail-test2",
            "name": "Trail Test 2",
            "enabled": True,
            "watchlist": {"underlyings": ["TEST"]},
            "entry_conditions": {"operator": "AND", "rules": []},
            "exit_conditions": {
                "operator": "OR",
                "rules": [
                    {
                        "type": "trailing_stop",
                        "activation_pct": 0.008,
                        "trail_pct": 0.003,
                    },
                ],
            },
        }
        strategy = StrategyConfig(strategy_data)
        # Entry at 100.0, peak at 100.5 (+0.5% < 0.8% activation), no trigger
        signal = self.matcher.evaluate_exit(
            strategy, "TEST",
            current_price=100.3,
            entry_price=100.0,
            minutes_to_close=120,
            highest_price=100.5,
        )
        assert signal is None

    # ── Entry quality (left-side ambush) ──

    def test_vwap_ambush_quality_high(self):
        strategy = StrategyConfig(VWAP_AMBUSH_STRATEGY)
        indicators = {
            "5m": IndicatorResult(
                symbol="SPY", timeframe="5m", timestamp=time.time(),
                close=450.0, vwap=450.02,
                vwap_distance_pct=-0.004, abs_vwap_distance_pct=0.004,
                volume_ratio=0.3, candle_body_pct=0.01,
            ),
        }
        quality = self.matcher.evaluate_entry_quality(strategy, indicators)
        assert quality.score >= 80
        assert quality.grade in ("A", "B")

    def test_bb_squeeze_quality_high(self):
        strategy = StrategyConfig(BB_SQUEEZE_STRATEGY)
        indicators = {
            "5m": IndicatorResult(
                symbol="SPY", timeframe="5m", timestamp=time.time(),
                close=450.0, ema_200=445.0, vwap=449.5,
                bb_width_pct=0.10,
            ),
        }
        quality = self.matcher.evaluate_entry_quality(strategy, indicators)
        assert quality.score >= 70

    def test_right_side_quality_high_volume(self):
        """Right-side strategy: high volume should be rewarded."""
        strategy_data = {
            "strategy_id": "right-side-test",
            "name": "Right Side Test",
            "enabled": True,
            "watchlist": {"underlyings": ["SPY"]},
            "entry_conditions": {"operator": "AND", "rules": []},
            "entry_quality_filters": {
                "min_score": 55,
                "prefer_high_volume": True,
                "min_volume_spike": 1.5,
            },
        }
        strategy = StrategyConfig(strategy_data)
        indicators = {
            "5m": IndicatorResult(
                symbol="SPY", timeframe="5m", timestamp=time.time(),
                close=450.0, volume_ratio=2.0, volume_spike=2.5,
            ),
        }
        quality = self.matcher.evaluate_entry_quality(strategy, indicators)
        assert quality.score >= 100  # High volume bonus
        assert any("放量" in r for r in quality.reasons)

    def test_right_side_quality_low_volume_penalized(self):
        """Right-side strategy: low volume should be penalized."""
        strategy_data = {
            "strategy_id": "right-side-low",
            "name": "Right Side Low",
            "enabled": True,
            "watchlist": {"underlyings": ["SPY"]},
            "entry_conditions": {"operator": "AND", "rules": []},
            "entry_quality_filters": {
                "min_score": 55,
                "prefer_high_volume": True,
                "min_volume_spike": 1.5,
            },
        }
        strategy = StrategyConfig(strategy_data)
        indicators = {
            "5m": IndicatorResult(
                symbol="SPY", timeframe="5m", timestamp=time.time(),
                close=450.0, volume_ratio=0.5, volume_spike=0.5,
            ),
        }
        quality = self.matcher.evaluate_entry_quality(strategy, indicators)
        assert quality.score < 100
        assert any("不宜" in r for r in quality.reasons)

    def test_oversold_reversal_quality_high(self):
        strategy = StrategyConfig(OVERSOLD_REVERSAL_STRATEGY)
        indicators = {
            "15m": IndicatorResult(
                symbol="AAPL", timeframe="15m", timestamp=time.time(),
                rsi=18.0,
            ),
            "5m": IndicatorResult(
                symbol="AAPL", timeframe="5m", timestamp=time.time(),
                vwap_distance_pct=-2.0, abs_vwap_distance_pct=2.0,
            ),
        }
        quality = self.matcher.evaluate_entry_quality(strategy, indicators)
        assert quality.score >= 70

    # ── P1.1: N-bar confirmation ──

    def test_confirm_bars_consecutive_pass(self):
        """confirm_bars: 2 — passes only after 2 consecutive evaluations."""
        strategy_data = {
            "strategy_id": "confirm-test",
            "name": "Confirm Test",
            "enabled": True,
            "watchlist": {"underlyings": ["TEST"]},
            "entry_conditions": {
                "operator": "AND",
                "rules": [
                    {
                        "indicator": "RSI",
                        "field": "value",
                        "comparator": ">",
                        "threshold": 50,
                        "timeframe": "5m",
                        "confirm_bars": 2,
                    },
                ],
            },
        }
        strategy = StrategyConfig(strategy_data)
        ind = {"5m": IndicatorResult(symbol="TEST", timeframe="5m", timestamp=time.time(), rsi=55.0)}
        # First pass: counter=1/2, should NOT trigger
        signal1 = self.matcher.evaluate_entry(strategy, "TEST", ind)
        assert signal1 is None
        # Second pass: counter=2/2, should trigger
        signal2 = self.matcher.evaluate_entry(strategy, "TEST", ind)
        assert signal2 is not None

    def test_confirm_bars_interrupted_resets(self):
        """confirm_bars: 2 — failing mid-sequence resets counter."""
        strategy_data = {
            "strategy_id": "confirm-reset",
            "name": "Confirm Reset",
            "enabled": True,
            "watchlist": {"underlyings": ["TEST"]},
            "entry_conditions": {
                "operator": "AND",
                "rules": [
                    {
                        "indicator": "RSI",
                        "field": "value",
                        "comparator": ">",
                        "threshold": 50,
                        "timeframe": "5m",
                        "confirm_bars": 2,
                    },
                ],
            },
        }
        strategy = StrategyConfig(strategy_data)
        ind_pass = {"5m": IndicatorResult(symbol="TEST", timeframe="5m", timestamp=time.time(), rsi=55.0)}
        ind_fail = {"5m": IndicatorResult(symbol="TEST", timeframe="5m", timestamp=time.time(), rsi=45.0)}
        # Pass, then fail, then pass — should not trigger
        self.matcher.evaluate_entry(strategy, "TEST", ind_pass)
        self.matcher.evaluate_entry(strategy, "TEST", ind_fail)
        signal = self.matcher.evaluate_entry(strategy, "TEST", ind_pass)
        assert signal is None
        # Now second consecutive pass should trigger
        signal2 = self.matcher.evaluate_entry(strategy, "TEST", ind_pass)
        assert signal2 is not None

    def test_confirm_bars_default_behavior(self):
        """No confirm_bars: default behavior (single pass triggers)."""
        strategy_data = {
            "strategy_id": "confirm-default",
            "name": "Confirm Default",
            "enabled": True,
            "watchlist": {"underlyings": ["TEST"]},
            "entry_conditions": {
                "operator": "AND",
                "rules": [
                    {
                        "indicator": "RSI",
                        "field": "value",
                        "comparator": ">",
                        "threshold": 50,
                        "timeframe": "5m",
                    },
                ],
            },
        }
        strategy = StrategyConfig(strategy_data)
        ind = {"5m": IndicatorResult(symbol="TEST", timeframe="5m", timestamp=time.time(), rsi=55.0)}
        signal = self.matcher.evaluate_entry(strategy, "TEST", ind)
        assert signal is not None

    # ── P1.2: min_magnitude ──

    def test_turns_positive_with_min_magnitude(self):
        """turns_positive with min_magnitude: tiny positive value should not trigger."""
        matcher = RuleMatcher()
        # previous <= 0, current = 0.005 < min_magnitude 0.01 → False
        assert matcher._compare("turns_positive", 0.005, 0, -0.1, min_magnitude=0.01) is False
        # previous <= 0, current = 0.02 > min_magnitude 0.01 → True
        assert matcher._compare("turns_positive", 0.02, 0, -0.1, min_magnitude=0.01) is True

    def test_turns_negative_with_min_magnitude(self):
        """turns_negative with min_magnitude: tiny negative value should not trigger."""
        matcher = RuleMatcher()
        # previous >= 0, current = -0.005 > -min_magnitude -0.01 → False
        assert matcher._compare("turns_negative", -0.005, 0, 0.1, min_magnitude=0.01) is False
        # previous >= 0, current = -0.02 < -min_magnitude -0.01 → True
        assert matcher._compare("turns_negative", -0.02, 0, 0.1, min_magnitude=0.01) is True

    # ── P1.3: prev_values persistence ──

    def test_prev_values_export_import(self):
        """prev_values can be exported and imported to restore state."""
        matcher1 = RuleMatcher()
        strategy_data = {
            "strategy_id": "persist-test",
            "name": "Persist",
            "enabled": True,
            "watchlist": {"underlyings": ["TEST"]},
            "entry_conditions": {
                "operator": "AND",
                "rules": [
                    {
                        "indicator": "RSI",
                        "field": "value",
                        "comparator": "crosses_above",
                        "threshold": 30,
                        "timeframe": "5m",
                    },
                ],
            },
        }
        strategy = StrategyConfig(strategy_data)
        # Set prev value
        ind_below = {"5m": IndicatorResult(symbol="TEST", timeframe="5m", timestamp=time.time(), rsi=25.0)}
        matcher1.evaluate_entry(strategy, "TEST", ind_below)
        exported = matcher1.export_prev_values()
        assert len(exported) > 0

        # Import into new matcher
        matcher2 = RuleMatcher()
        matcher2.import_prev_values(exported)
        # Now crosses_above should work (prev=25, current=35)
        ind_above = {"5m": IndicatorResult(symbol="TEST", timeframe="5m", timestamp=time.time(), rsi=35.0)}
        signal = matcher2.evaluate_entry(strategy, "TEST", ind_above)
        assert signal is not None

    # ── P2.2: Market context filter ──

    def test_market_context_filters_config(self):
        """market_context_filters field should be available on StrategyConfig."""
        strategy_data = {
            "strategy_id": "mcf-test",
            "name": "MCF Test",
            "enabled": True,
            "watchlist": {"underlyings": ["SPY"]},
            "entry_conditions": {"operator": "AND", "rules": []},
            "market_context_filters": {
                "max_spy_day_drop_pct": -1.0,
                "max_adx": 30,
            },
        }
        config = StrategyConfig(strategy_data)
        assert config.market_context_filters["max_spy_day_drop_pct"] == -1.0
        assert config.market_context_filters["max_adx"] == 30

    # ── P3.1: Score cap at 100 ──

    def test_quality_score_capped_at_100(self):
        """Quality score should be capped at 100 even with many bonuses."""
        strategy_data = {
            "strategy_id": "cap-test",
            "name": "Cap Test",
            "enabled": True,
            "watchlist": {"underlyings": ["SPY"]},
            "entry_conditions": {"operator": "AND", "rules": []},
            "entry_quality_filters": {
                "min_score": 50,
                "prefer_high_volume": True,
                "min_volume_spike": 1.5,
                "prefer_above_ema200": True,
                "max_bb_width_pct": 0.50,
            },
        }
        strategy = StrategyConfig(strategy_data)
        # Very high volume + above EMA200 + tight BB = lots of bonuses
        indicators = {
            "5m": IndicatorResult(
                symbol="SPY", timeframe="5m", timestamp=time.time(),
                close=450.0, ema_200=440.0, volume_spike=5.0,
                bb_width_pct=0.10,
            ),
        }
        quality = self.matcher.evaluate_entry_quality(strategy, indicators)
        assert quality.score <= 100


class TestStrategyStateManager:
    def setup_method(self):
        self.manager = StrategyStateManager()

    def test_initial_state_is_watching(self):
        state = self.manager.get_state("test-strat", "AAPL")
        assert state.state == StrategyState.WATCHING

    def test_entry_trigger_flow(self):
        signal_id = self.manager.trigger_entry("test-strat", "AAPL")
        assert signal_id is not None

        state = self.manager.get_state("test-strat", "AAPL")
        assert state.state == StrategyState.ENTRY_TRIGGERED

    def test_confirm_entry(self):
        signal_id = self.manager.trigger_entry("test-strat", "AAPL")
        assert signal_id is not None

        result = self.manager.confirm_entry(signal_id, entry_price=3.50)
        assert result is True

        state = self.manager.get_state("test-strat", "AAPL")
        assert state.state == StrategyState.HOLDING
        assert state.position.entry_price == 3.50

    def test_skip_entry(self):
        signal_id = self.manager.trigger_entry("test-strat", "AAPL")
        assert signal_id is not None

        result = self.manager.skip_entry(signal_id)
        assert result is True

        state = self.manager.get_state("test-strat", "AAPL")
        assert state.state == StrategyState.WATCHING

    def test_exit_flow(self):
        signal_id = self.manager.trigger_entry("test-strat", "AAPL")
        self.manager.confirm_entry(signal_id, entry_price=3.50)
        assert self.manager.trigger_exit("test-strat", "AAPL") is True

        state = self.manager.get_state("test-strat", "AAPL")
        assert state.state == StrategyState.EXIT_TRIGGERED

        assert self.manager.confirm_exit("test-strat", "AAPL") is True
        state = self.manager.get_state("test-strat", "AAPL")
        assert state.state == StrategyState.WATCHING

    def test_cannot_trigger_entry_twice(self):
        self.manager.trigger_entry("test-strat", "AAPL")
        second = self.manager.trigger_entry("test-strat", "AAPL")
        assert second is None

    def test_export_import(self):
        self.manager.trigger_entry("test-strat", "AAPL")
        data = self.manager.export_all()
        assert len(data) == 1

        new_manager = StrategyStateManager()
        new_manager.import_all(data)
        state = new_manager.get_state("test-strat", "AAPL")
        assert state.state == StrategyState.ENTRY_TRIGGERED

    def test_timeout(self):
        signal_id = self.manager.trigger_entry("test-strat", "AAPL")
        state = self.manager.get_state("test-strat", "AAPL")
        state.triggered_at = time.time() - 400

        timed_out = self.manager.check_timeouts()
        assert len(timed_out) == 1
        assert self.manager.get_state("test-strat", "AAPL").state == StrategyState.WATCHING

    def test_update_highest_price(self):
        signal_id = self.manager.trigger_entry("test-strat", "AAPL")
        self.manager.confirm_entry(signal_id, entry_price=3.50)
        self.manager.update_highest_price("test-strat", "AAPL", 4.00)
        state = self.manager.get_state("test-strat", "AAPL")
        assert state.position.highest_price == 4.00
