import numpy as np
import pandas as pd
import pytest

from src.indicator.engine import IndicatorEngine, IndicatorResult


def _make_bars(n: int = 50, base_price: float = 100.0) -> pd.DataFrame:
    """Generate synthetic 1-minute OHLCV bars for testing."""
    np.random.seed(42)
    dates = pd.date_range("2025-03-20 09:30", periods=n, freq="1min", tz="America/New_York")
    close = base_price + np.cumsum(np.random.randn(n) * 0.5)
    high = close + np.abs(np.random.randn(n) * 0.3)
    low = close - np.abs(np.random.randn(n) * 0.3)
    opn = close + np.random.randn(n) * 0.1
    volume = np.random.randint(1000, 50000, size=n)
    return pd.DataFrame(
        {"Open": opn, "High": high, "Low": low, "Close": close, "Volume": volume},
        index=dates,
    )


class TestIndicatorEngine:
    def setup_method(self):
        self.engine = IndicatorEngine()

    def test_update_bars_and_calculate(self):
        bars = _make_bars(50)
        self.engine.update_bars("AAPL", bars)
        result = self.engine.calculate("AAPL", "1m")
        assert result is not None
        assert result.symbol == "AAPL"
        assert result.timeframe == "1m"
        assert result.rsi is not None
        assert 0 <= result.rsi <= 100

    def test_macd_calculation(self):
        bars = _make_bars(60)
        self.engine.update_bars("AAPL", bars)
        result = self.engine.calculate("AAPL", "1m")
        assert result is not None
        assert result.macd_line is not None
        assert result.macd_signal is not None
        assert result.macd_histogram is not None

    def test_ema_calculation(self):
        bars = _make_bars(30)
        self.engine.update_bars("TSLA", bars)
        result = self.engine.calculate("TSLA", "1m")
        assert result is not None
        assert result.ema_9 is not None
        assert result.ema_21 is not None

    def test_ema_50_calculation(self):
        bars = _make_bars(60)
        self.engine.update_bars("AAPL", bars)
        result = self.engine.calculate("AAPL", "1m")
        assert result is not None
        assert result.ema_50 is not None

    def test_ema_200_requires_many_bars(self):
        bars = _make_bars(100)
        self.engine.update_bars("AAPL", bars)
        result = self.engine.calculate("AAPL", "1m")
        assert result is not None
        assert result.ema_200 is None

    def test_ema_200_with_sufficient_bars(self):
        bars = _make_bars(210)
        self.engine.update_bars("SPY", bars)
        result = self.engine.calculate("SPY", "1m")
        assert result is not None
        assert result.ema_200 is not None

    def test_atr_calculation(self):
        bars = _make_bars(30)
        self.engine.update_bars("SPY", bars)
        result = self.engine.calculate("SPY", "1m")
        assert result is not None
        assert result.atr is not None
        assert result.atr > 0

    def test_vwap_calculation(self):
        bars = _make_bars(30)
        self.engine.update_bars("QQQ", bars)
        result = self.engine.calculate("QQQ", "1m")
        assert result is not None
        assert result.vwap is not None

    def test_bollinger_bands_calculation(self):
        bars = _make_bars(30)
        self.engine.update_bars("AAPL", bars)
        result = self.engine.calculate("AAPL", "1m")
        assert result is not None
        assert result.bb_upper is not None
        assert result.bb_lower is not None
        assert result.bb_width_pct is not None
        assert result.bb_upper > result.bb_lower
        assert result.bb_width_pct > 0

    def test_bollinger_insufficient_data(self):
        bars = _make_bars(10)
        self.engine.update_bars("NVDA", bars)
        result = self.engine.calculate("NVDA", "1m")
        assert result is not None
        assert result.bb_upper is None

    def test_candle_metrics(self):
        bars = _make_bars(30)
        self.engine.update_bars("AAPL", bars)
        result = self.engine.calculate("AAPL", "1m")
        assert result is not None
        assert result.candle_body_pct is not None
        assert result.candle_body_pct >= 0
        assert result.candle_range_pct is not None
        assert result.candle_range_pct >= 0
        assert result.open is not None
        assert result.high is not None
        assert result.low is not None

    def test_prev_bar_high(self):
        bars = _make_bars(30)
        self.engine.update_bars("AAPL", bars)
        result = self.engine.calculate("AAPL", "1m")
        assert result is not None
        assert result.prev_bar_high is not None
        expected_prev_high = float(bars["High"].iloc[-2])
        assert abs(result.prev_bar_high - expected_prev_high) < 1e-6

    def test_vwap_distance_metrics(self):
        bars = _make_bars(30)
        self.engine.update_bars("AAPL", bars)
        result = self.engine.calculate("AAPL", "1m")
        assert result is not None
        assert result.vwap_distance_pct is not None
        assert result.abs_vwap_distance_pct is not None
        assert result.abs_vwap_distance_pct >= 0
        assert result.abs_vwap_distance_pct == abs(result.vwap_distance_pct)

    def test_5m_timeframe(self):
        bars = _make_bars(60)
        self.engine.update_bars("AAPL", bars)
        result = self.engine.calculate("AAPL", "5m")
        assert result is not None
        assert result.timeframe == "5m"

    def test_15m_timeframe(self):
        bars = _make_bars(60)
        self.engine.update_bars("AAPL", bars)
        result = self.engine.calculate("AAPL", "15m")
        assert result is not None
        assert result.timeframe == "15m"

    def test_15m_bars_aggregated(self):
        bars = _make_bars(60)
        self.engine.update_bars("AAPL", bars)
        sym = self.engine._data["AAPL"]
        assert not sym.bars_15m.empty
        assert len(sym.bars_15m) <= len(sym.bars_1m)

    def test_insufficient_data_returns_none(self):
        bars = _make_bars(3)
        self.engine.update_bars("NVDA", bars)
        result = self.engine.calculate("NVDA", "1m")
        assert result is not None
        assert result.rsi is None
        assert result.macd_line is None

    def test_calculate_all(self):
        bars = _make_bars(60)
        self.engine.update_bars("AAPL", bars)
        results = self.engine.calculate_all("AAPL")
        assert "1m" in results
        assert "5m" in results
        assert "15m" in results

    def test_get_last(self):
        bars = _make_bars(50)
        self.engine.update_bars("AAPL", bars)
        self.engine.calculate("AAPL", "1m")
        last = self.engine.get_last("AAPL", "1m")
        assert last is not None
        assert last.symbol == "AAPL"

    def test_get_last_15m(self):
        bars = _make_bars(60)
        self.engine.update_bars("AAPL", bars)
        self.engine.calculate("AAPL", "15m")
        last = self.engine.get_last("AAPL", "15m")
        assert last is not None
        assert last.timeframe == "15m"

    def test_update_live_price(self):
        bars = _make_bars(60)
        self.engine.update_bars("AAPL", bars)
        results = self.engine.update_live_price("AAPL", 105.0, 1234567890.0)
        assert "1m" in results
        assert "5m" in results
        assert "15m" in results

    def test_volume_spike(self):
        bars = _make_bars(30)
        self.engine.update_bars("AAPL", bars)
        result = self.engine.calculate("AAPL", "1m")
        assert result is not None
        assert result.volume_spike is not None
        assert result.volume_spike > 0

    def test_volume_spike_insufficient_data(self):
        bars = _make_bars(3)
        self.engine.update_bars("NVDA", bars)
        result = self.engine.calculate("NVDA", "1m")
        assert result is not None
        assert result.volume_spike is None

    def test_bb_width_percentile(self):
        bars = _make_bars(50)
        self.engine.update_bars("AAPL", bars)
        # Calculate multiple times to build BBW history
        for _ in range(5):
            result = self.engine.calculate("AAPL", "1m")
        assert result is not None
        assert result.bb_width_percentile is not None
        assert 0 <= result.bb_width_percentile <= 100

    def test_bb_width_percentile_insufficient_history(self):
        bars = _make_bars(25)
        self.engine.update_bars("NVDA", bars)
        result = self.engine.calculate("NVDA", "1m")
        assert result is not None
        # Only 1 BBW value in history, need >= 5 for percentile
        assert result.bb_width_percentile is None

    def test_adx_calculation(self):
        bars = _make_bars(50)
        self.engine.update_bars("AAPL", bars)
        result = self.engine.calculate("AAPL", "1m")
        assert result is not None
        assert result.adx is not None
        assert result.adx > 0

    def test_adx_insufficient_data(self):
        bars = _make_bars(10)
        self.engine.update_bars("NVDA", bars)
        result = self.engine.calculate("NVDA", "1m")
        assert result is not None
        assert result.adx is None

    def test_to_dict(self):
        bars = _make_bars(50)
        self.engine.update_bars("AAPL", bars)
        result = self.engine.calculate("AAPL", "1m")
        assert result is not None
        d = result.to_dict()
        assert "symbol" in d
        assert "rsi" in d
