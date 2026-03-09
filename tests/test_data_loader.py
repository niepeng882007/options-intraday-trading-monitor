"""Unit tests for backtest DataLoader — all Futu SDK calls are mocked."""
from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from src.backtest.data_loader import DataLoader


# ── Helpers ──


def _make_kline_df(
    n: int = 30,
    base: float = 150.0,
    start: str = "2026-03-03 09:30",
) -> pd.DataFrame:
    np.random.seed(42)
    dates = pd.date_range(start, periods=n, freq="1min")
    close = base + np.cumsum(np.random.randn(n) * 0.3)
    return pd.DataFrame(
        {
            "time_key": dates.strftime("%Y-%m-%d %H:%M:%S"),
            "open": close + 0.1,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.random.randint(1000, 50000, size=n),
        }
    )


def _make_prepost_kline_df() -> pd.DataFrame:
    """K-line data spanning pre-market, regular hours, and after hours."""
    times = [
        "2026-03-03 08:00:00",  # pre-market
        "2026-03-03 09:29:00",  # pre-market
        "2026-03-03 09:30:00",  # regular
        "2026-03-03 12:00:00",  # regular
        "2026-03-03 15:59:00",  # regular (last valid)
        "2026-03-03 16:00:00",  # after hours
        "2026-03-03 17:30:00",  # after hours
    ]
    n = len(times)
    return pd.DataFrame(
        {
            "time_key": times,
            "open": [150.0] * n,
            "high": [151.0] * n,
            "low": [149.0] * n,
            "close": [150.5] * n,
            "volume": [10000] * n,
        }
    )


def _make_multiday_kline_df(num_days: int = 10) -> pd.DataFrame:
    """Generate K-line data spanning multiple trading days."""
    frames = []
    base_date = datetime(2026, 2, 20)
    day_count = 0
    cal_day = 0
    while day_count < num_days:
        d = base_date + pd.Timedelta(days=cal_day)
        cal_day += 1
        if d.weekday() >= 5:  # skip weekends
            continue
        day_count += 1
        day_str = d.strftime("%Y-%m-%d")
        times = pd.date_range(f"{day_str} 09:30", f"{day_str} 15:59", freq="1min")
        n = len(times)
        frames.append(
            pd.DataFrame(
                {
                    "time_key": times.strftime("%Y-%m-%d %H:%M:%S"),
                    "open": [150.0] * n,
                    "high": [151.0] * n,
                    "low": [149.0] * n,
                    "close": [150.5] * n,
                    "volume": [10000] * n,
                }
            )
        )
    return pd.concat(frames, ignore_index=True)


@pytest.fixture
def mock_futu_ctx():
    """Patch OpenQuoteContext so DataLoader can connect without FutuOpenD."""
    with patch("src.backtest.data_loader.DataLoader._connect_futu") as mock_connect:
        yield mock_connect


@pytest.fixture
def loader(mock_futu_ctx, tmp_path):
    """Create a DataLoader with mocked Futu connection."""
    dl = DataLoader(cache_dir=tmp_path, data_source="futu")
    dl._ctx = MagicMock()
    yield dl
    dl.close()


# ── Tests ──


class TestPageReqKeyPagination:
    def test_multi_page_merge(self, loader):
        """Multiple pages are correctly concatenated."""
        from futu import RET_OK

        page1 = _make_kline_df(n=10, start="2026-03-03 09:30")
        page2 = _make_kline_df(n=10, start="2026-03-03 09:40")

        loader._ctx.request_history_kline.side_effect = [
            (RET_OK, page1, "next_key"),
            (RET_OK, page2, None),
        ]

        result = loader._fetch_kline("US.AAPL", "2026-03-03", "2026-03-03")
        assert len(result) == 20
        assert loader._ctx.request_history_kline.call_count == 2

    def test_single_page(self, loader):
        """Single page with no next_page key."""
        from futu import RET_OK

        page = _make_kline_df(n=5)
        loader._ctx.request_history_kline.return_value = (RET_OK, page, None)

        result = loader._fetch_kline("US.AAPL", "2026-03-03", "2026-03-03")
        assert len(result) == 5
        assert loader._ctx.request_history_kline.call_count == 1

    def test_empty_result(self, loader):
        """Empty response returns empty DataFrame."""
        from futu import RET_OK

        loader._ctx.request_history_kline.return_value = (
            RET_OK,
            pd.DataFrame(),
            None,
        )

        result = loader._fetch_kline("US.AAPL", "2026-03-03", "2026-03-03")
        assert result.empty

    def test_api_error_raises(self, loader):
        """API error raises RuntimeError."""
        from futu import RET_ERROR

        loader._ctx.request_history_kline.return_value = (
            RET_ERROR,
            "rate limit exceeded",
            None,
        )

        with pytest.raises(RuntimeError, match="rate limit exceeded"):
            loader._fetch_kline("US.AAPL", "2026-03-03", "2026-03-03")


class TestCaching:
    def test_cache_hit(self, loader, tmp_path):
        """Cached file is loaded without calling Futu."""
        from src.collector.futu import normalize_futu_kline

        # Pre-populate cache
        raw = _make_kline_df(n=30)
        df = normalize_futu_kline(raw)
        df = df.between_time("09:30", "15:59")
        cache_key = loader._cache_key("AAPL", 5, None, None)
        df.to_csv(tmp_path / f"{cache_key}.csv")

        result = loader.load("AAPL", days=5)
        assert not result.empty
        assert len(result) == len(df)
        # Futu should not have been called
        loader._ctx.request_history_kline.assert_not_called()

    @patch("src.backtest.data_loader.time.sleep")
    def test_cache_miss_downloads(self, mock_sleep, loader, tmp_path):
        """Cache miss triggers Futu download and saves cache."""
        from futu import RET_OK

        raw = _make_kline_df(n=30)
        loader._ctx.request_history_kline.return_value = (RET_OK, raw, None)

        result = loader.load("AAPL", days=5)
        assert not result.empty
        loader._ctx.request_history_kline.assert_called_once()

        # Verify cache file was created
        cache_key = loader._cache_key("AAPL", 5, None, None)
        assert (tmp_path / f"{cache_key}.csv").exists()

    def test_cache_key_includes_data_source(self, loader):
        """Cache key includes data source suffix to avoid conflicts."""
        key = loader._cache_key("AAPL", 5, None, None)
        assert key.endswith("_futu")


class TestTradingHoursFilter:
    @patch("src.backtest.data_loader.time.sleep")
    def test_filters_prepost_market(self, mock_sleep, loader):
        """Pre-market and after-hours bars are filtered out."""
        from futu import RET_OK

        raw = _make_prepost_kline_df()
        loader._ctx.request_history_kline.return_value = (RET_OK, raw, None)

        result = loader.load("AAPL", days=5)
        # Only 09:30, 12:00, 15:59 should remain
        assert len(result) == 3
        times = result.index.time
        for t in times:
            assert t >= pd.Timestamp("09:30").time()
            assert t <= pd.Timestamp("15:59").time()


class TestDaysModeTruncation:
    @patch("src.backtest.data_loader.time.sleep")
    def test_days_truncation(self, mock_sleep, loader):
        """days=5 returns only the last 5 trading days."""
        from futu import RET_OK

        raw = _make_multiday_kline_df(num_days=10)
        loader._ctx.request_history_kline.return_value = (RET_OK, raw, None)

        result = loader.load("AAPL", days=5)
        unique_days = sorted(set(result.index.date))
        assert len(unique_days) == 5


class TestConnectionError:
    def test_futu_connection_error(self, tmp_path):
        """Friendly error when FutuOpenD is not running."""
        with patch(
            "src.backtest.data_loader.DataLoader._connect_futu",
            side_effect=ConnectionError("Failed to connect"),
        ):
            with pytest.raises(ConnectionError, match="Failed to connect"):
                DataLoader(cache_dir=tmp_path, data_source="futu")

    def test_yahoo_fallback_no_connection(self, tmp_path):
        """Yahoo data source does not attempt Futu connection."""
        # Should not raise even without FutuOpenD
        dl = DataLoader(cache_dir=tmp_path, data_source="yahoo")
        assert dl._ctx is None
        dl.close()


class TestContextManager:
    def test_context_manager_closes(self, mock_futu_ctx, tmp_path):
        with DataLoader(cache_dir=tmp_path, data_source="futu") as dl:
            dl._ctx = MagicMock()
            mock_ctx = dl._ctx
        # After exiting, ctx should be None
        assert dl._ctx is None
        mock_ctx.close.assert_called_once()
