import os
import tempfile
import time

import pytest
import yaml

from src.indicator.engine import IndicatorResult
from src.strategy.loader import StrategyConfig, StrategyLoader, load_strategy_file
from src.strategy.matcher import RuleMatcher, Signal
from src.strategy.state import StrategyState, StrategyStateManager


SAMPLE_STRATEGY = {
    "strategy_id": "test-rsi-bounce",
    "name": "Test RSI Bounce",
    "enabled": True,
    "watchlist": {
        "underlyings": ["AAPL"],
        "option_filter": {"type": "call", "max_dte": 7},
    },
    "entry_conditions": {
        "operator": "AND",
        "rules": [
            {
                "indicator": "RSI",
                "params": {"period": 14},
                "field": "value",
                "comparator": "crosses_above",
                "threshold": 30,
                "timeframe": "5m",
            },
            {
                "indicator": "MACD",
                "params": {"fast": 12, "slow": 26, "signal": 9},
                "field": "histogram",
                "comparator": "turns_positive",
                "timeframe": "5m",
            },
        ],
    },
    "exit_conditions": {
        "operator": "OR",
        "rules": [
            {"type": "take_profit_pct", "threshold": 0.50},
            {"type": "stop_loss_pct", "threshold": -0.20},
            {"type": "time_exit", "minutes_before_close": 15},
        ],
    },
    "notification": {"cooldown_seconds": 120, "priority": "high"},
}


class TestStrategyConfig:
    def test_basic_properties(self):
        config = StrategyConfig(SAMPLE_STRATEGY)
        assert config.strategy_id == "test-rsi-bounce"
        assert config.name == "Test RSI Bounce"
        assert config.enabled is True
        assert "AAPL" in config.underlyings
        assert config.cooldown_seconds == 120
        assert config.priority == "high"


class TestStrategyLoader:
    def test_load_from_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(SAMPLE_STRATEGY, f)
            f.flush()
            config = load_strategy_file(f.name)
        os.unlink(f.name)

        assert config is not None
        assert config.strategy_id == "test-rsi-bounce"

    def test_load_all_from_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            for i in range(3):
                strat = SAMPLE_STRATEGY.copy()
                strat["strategy_id"] = f"test-{i}"
                strat["name"] = f"Test Strategy {i}"
                with open(os.path.join(tmpdir, f"strat_{i}.yaml"), "w") as f:
                    yaml.dump(strat, f)

            loader = StrategyLoader(tmpdir)
            loader.load_all()
            assert len(loader.strategies) == 3
            assert len(loader.get_active()) == 3

    def test_enable_disable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "test.yaml"), "w") as f:
                yaml.dump(SAMPLE_STRATEGY, f)

            loader = StrategyLoader(tmpdir)
            loader.load_all()
            assert loader.set_enabled("test-rsi-bounce", False)
            assert len(loader.get_active()) == 0
            assert loader.set_enabled("test-rsi-bounce", True)
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
        self.strategy = StrategyConfig(SAMPLE_STRATEGY)

    def _make_indicators(self, rsi: float, macd_hist: float) -> dict:
        return {
            "5m": IndicatorResult(
                symbol="AAPL",
                timeframe="5m",
                timestamp=time.time(),
                rsi=rsi,
                macd_histogram=macd_hist,
            ),
        }

    def test_crosses_above_triggers(self):
        # First tick: RSI below 30
        ind1 = self._make_indicators(rsi=28.0, macd_hist=-0.1)
        result1 = self.matcher.evaluate_entry(self.strategy, "AAPL", ind1)
        assert result1 is None

        # Second tick: RSI crosses above 30, MACD turns positive
        ind2 = self._make_indicators(rsi=32.0, macd_hist=0.2)
        result2 = self.matcher.evaluate_entry(self.strategy, "AAPL", ind2)
        assert result2 is not None
        assert result2.signal_type == "entry"

    def test_no_trigger_when_already_above(self):
        # Both ticks above threshold — no crossing
        ind1 = self._make_indicators(rsi=35.0, macd_hist=0.1)
        self.matcher.evaluate_entry(self.strategy, "AAPL", ind1)

        ind2 = self._make_indicators(rsi=40.0, macd_hist=0.2)
        result = self.matcher.evaluate_entry(self.strategy, "AAPL", ind2)
        assert result is None

    def test_exit_take_profit(self):
        signal = self.matcher.evaluate_exit(
            self.strategy, "AAPL",
            current_price=5.25,
            entry_price=3.50,
            minutes_to_close=120,
        )
        assert signal is not None
        assert "止盈" in signal.exit_reason

    def test_exit_stop_loss(self):
        signal = self.matcher.evaluate_exit(
            self.strategy, "AAPL",
            current_price=2.50,
            entry_price=3.50,
            minutes_to_close=120,
        )
        assert signal is not None
        assert "止损" in signal.exit_reason
        assert signal.priority == "high"

    def test_exit_time_based(self):
        signal = self.matcher.evaluate_exit(
            self.strategy, "AAPL",
            current_price=3.60,
            entry_price=3.50,
            minutes_to_close=10,
        )
        assert signal is not None
        assert "收盘前" in signal.exit_reason

    def test_no_exit_when_within_bounds(self):
        signal = self.matcher.evaluate_exit(
            self.strategy, "AAPL",
            current_price=3.80,
            entry_price=3.50,
            minutes_to_close=120,
        )
        assert signal is None


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
        state.triggered_at = time.time() - 400  # Simulate 400s ago

        timed_out = self.manager.check_timeouts()
        assert len(timed_out) == 1
        assert self.manager.get_state("test-strat", "AAPL").state == StrategyState.WATCHING

    def test_update_highest_price(self):
        signal_id = self.manager.trigger_entry("test-strat", "AAPL")
        self.manager.confirm_entry(signal_id, entry_price=3.50)
        self.manager.update_highest_price("test-strat", "AAPL", 4.00)
        state = self.manager.get_state("test-strat", "AAPL")
        assert state.position.highest_price == 4.00
