"""Tests for the chart generation module."""

import io
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from src.common.chart import ChartData, generate_chart
from src.common.types import GammaWallResult, VolumeProfileResult


def _make_today_bars(n: int = 60, base_price: float = 100.0) -> pd.DataFrame:
    """Generate synthetic 1m bars for testing."""
    start = datetime(2026, 3, 10, 9, 30)
    times = [start + timedelta(minutes=i) for i in range(n)]
    rng = np.random.default_rng(42)

    prices = base_price + np.cumsum(rng.normal(0, 0.2, n))
    opens = prices
    closes = prices + rng.normal(0, 0.1, n)
    highs = np.maximum(opens, closes) + rng.uniform(0, 0.3, n)
    lows = np.minimum(opens, closes) - rng.uniform(0, 0.3, n)
    volumes = rng.integers(1000, 50000, n)

    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=pd.DatetimeIndex(times),
    )


def _make_vp(poc: float = 100.0, vah: float = 101.0, val: float = 99.0) -> VolumeProfileResult:
    volume_by_price = {
        99.0: 5000, 99.5: 8000, 100.0: 12000, 100.5: 9000, 101.0: 6000,
    }
    return VolumeProfileResult(
        poc=poc, vah=vah, val=val,
        volume_by_price=volume_by_price,
        total_volume=40000,
        trading_days=5,
    )


def _make_chart_data(**kwargs) -> ChartData:
    defaults = dict(
        symbol="TEST",
        today_bars=_make_today_bars(),
        volume_profile=_make_vp(),
        vwap=100.2,
        last_price=100.5,
        prev_close=99.8,
        regime_label="TREND_DAY 72%",
        key_levels={"POC": 100.0, "VAH": 101.0, "VAL": 99.0, "VWAP": 100.2},
    )
    defaults.update(kwargs)
    return ChartData(**defaults)


class TestGenerateChart:
    def test_basic_chart_returns_bytesio(self):
        data = _make_chart_data()
        result = generate_chart(data)
        assert result is not None
        assert isinstance(result, io.BytesIO)
        content = result.getvalue()
        assert len(content) > 0

    def test_png_magic_bytes(self):
        data = _make_chart_data()
        result = generate_chart(data)
        assert result is not None
        content = result.getvalue()
        # PNG magic bytes: 89 50 4E 47
        assert content[:4] == b"\x89PNG"

    def test_empty_bars_returns_none(self):
        data = _make_chart_data(today_bars=pd.DataFrame())
        result = generate_chart(data)
        assert result is None

    def test_too_few_bars_returns_none(self):
        bars = _make_today_bars(n=3)
        data = _make_chart_data(today_bars=bars)
        result = generate_chart(data)
        assert result is None

    def test_no_gamma_wall(self):
        data = _make_chart_data(gamma_wall=None)
        result = generate_chart(data)
        assert result is not None
        assert result.getvalue()[:4] == b"\x89PNG"

    def test_with_gamma_wall(self):
        gw = GammaWallResult(
            call_wall_strike=102.0,
            put_wall_strike=98.0,
            max_pain=100.0,
        )
        data = _make_chart_data(
            gamma_wall=gw,
            key_levels={
                "POC": 100.0, "VAH": 101.0, "VAL": 99.0, "VWAP": 100.2,
                "Gamma Call Wall": 102.0, "Gamma Put Wall": 98.0,
            },
        )
        result = generate_chart(data)
        assert result is not None
        assert result.getvalue()[:4] == b"\x89PNG"

    def test_with_pdh_pdl_pmh_pml(self):
        data = _make_chart_data(
            key_levels={
                "POC": 100.0, "VAH": 101.0, "VAL": 99.0, "VWAP": 100.2,
                "PDH": 101.5, "PDL": 98.5, "PMH": 102.0, "PML": 97.5,
            },
        )
        result = generate_chart(data)
        assert result is not None

    def test_zero_price_levels_ignored(self):
        """Levels with value 0 should be silently skipped."""
        data = _make_chart_data(
            key_levels={"POC": 100.0, "VAH": 0.0, "VAL": 99.0, "VWAP": 0.0},
        )
        result = generate_chart(data)
        assert result is not None

    def test_lowercase_columns(self):
        """Bars with lowercase column names should work."""
        bars = _make_today_bars()
        bars.columns = [c.lower() for c in bars.columns]
        data = _make_chart_data(today_bars=bars)
        result = generate_chart(data)
        assert result is not None
        assert result.getvalue()[:4] == b"\x89PNG"
