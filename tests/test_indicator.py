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

    def test_5m_timeframe(self):
        bars = _make_bars(60)
        self.engine.update_bars("AAPL", bars)
        result = self.engine.calculate("AAPL", "5m")
        assert result is not None
        assert result.timeframe == "5m"

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

    def test_get_last(self):
        bars = _make_bars(50)
        self.engine.update_bars("AAPL", bars)
        self.engine.calculate("AAPL", "1m")
        last = self.engine.get_last("AAPL", "1m")
        assert last is not None
        assert last.symbol == "AAPL"

    def test_to_dict(self):
        bars = _make_bars(50)
        self.engine.update_bars("AAPL", bars)
        result = self.engine.calculate("AAPL", "1m")
        assert result is not None
        d = result.to_dict()
        assert "symbol" in d
        assert "rsi" in d
