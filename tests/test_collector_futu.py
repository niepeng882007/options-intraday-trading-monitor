"""Unit tests for FutuCollector — all Futu SDK calls are mocked."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

pytestmark = pytest.mark.asyncio(loop_scope="function")

# ── Symbol mapping tests (no SDK dependency) ──

from src.collector.futu import to_futu, from_futu, _period_to_dates, INTERVAL_MAP


class TestSymbolMapping:
    def test_to_futu_plain(self):
        assert to_futu("AAPL") == "US.AAPL"

    def test_to_futu_already_prefixed(self):
        assert to_futu("US.AAPL") == "US.AAPL"

    def test_from_futu(self):
        assert from_futu("US.AAPL") == "AAPL"

    def test_from_futu_no_prefix(self):
        assert from_futu("AAPL") == "AAPL"

    def test_roundtrip(self):
        assert from_futu(to_futu("TSLA")) == "TSLA"


class TestIntervalMapping:
    def test_1m(self):
        from futu import KLType
        assert INTERVAL_MAP["1m"] == KLType.K_1M

    def test_5m(self):
        from futu import KLType
        assert INTERVAL_MAP["5m"] == KLType.K_5M

    def test_15m(self):
        from futu import KLType
        assert INTERVAL_MAP["15m"] == KLType.K_15M


class TestPeriodMapping:
    def test_1d_returns_today(self):
        start, end = _period_to_dates("1d")
        assert start == end

    def test_5d_returns_range(self):
        start, end = _period_to_dates("5d")
        assert start < end


# ── FutuCollector tests with mocked OpenQuoteContext ──


def _make_mock_ctx():
    """Create a mock OpenQuoteContext with sensible defaults."""
    ctx = MagicMock()
    return ctx


def _make_quote_df(code: str = "US.AAPL", last_price: float = 150.0):
    return pd.DataFrame([{
        "code": code,
        "last_price": last_price,
        "bid_price": last_price - 0.01,
        "ask_price": last_price + 0.01,
        "volume": 1_000_000,
    }])


def _make_kline_df(n: int = 30, base: float = 150.0):
    import numpy as np
    np.random.seed(42)
    dates = pd.date_range("2025-03-20 09:30", periods=n, freq="1min")
    close = base + np.cumsum(np.random.randn(n) * 0.3)
    return pd.DataFrame({
        "time_key": dates.strftime("%Y-%m-%d %H:%M:%S"),
        "open": close + 0.1,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": np.random.randint(1000, 50000, size=n),
    })


@pytest.fixture
def collector():
    """Create a FutuCollector with a mocked context (no real connection)."""
    with patch("src.collector.futu.OpenQuoteContext") as MockCtx:
        mock_ctx = _make_mock_ctx()
        MockCtx.return_value = mock_ctx

        from src.collector.futu import FutuCollector
        c = FutuCollector(host="127.0.0.1", port=11111)
        c._ctx = mock_ctx
        c._loop = asyncio.new_event_loop()
        yield c, mock_ctx
        c._loop.close()


class TestGetStockQuote:
    @pytest.mark.asyncio
    async def test_returns_stock_quote(self, collector):
        c, mock_ctx = collector
        from futu import RET_OK
        mock_ctx.get_stock_quote.return_value = (RET_OK, _make_quote_df("US.AAPL", 155.25))

        quote = await c.get_stock_quote("AAPL")

        assert quote.symbol == "AAPL"
        assert quote.price == 155.25
        assert quote.bid == pytest.approx(155.24)
        assert quote.ask == pytest.approx(155.26)
        assert quote.volume == 1_000_000
        mock_ctx.get_stock_quote.assert_called_once_with(["US.AAPL"])

    @pytest.mark.asyncio
    async def test_error_raises(self, collector):
        c, mock_ctx = collector
        from futu import RET_ERROR
        mock_ctx.get_stock_quote.return_value = (RET_ERROR, "connection lost")

        with pytest.raises(RuntimeError, match="failed after"):
            await c.get_stock_quote("AAPL")


class TestGetHistory:
    @pytest.mark.asyncio
    async def test_returns_ohlcv_dataframe(self, collector):
        c, mock_ctx = collector
        from futu import RET_OK
        kline_df = _make_kline_df(30)
        mock_ctx.request_history_kline.return_value = (RET_OK, kline_df, None)

        df = await c.get_history("AAPL", interval="1m", period="1d")

        assert not df.empty
        assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
        assert isinstance(df.index, pd.DatetimeIndex)
        assert len(df) == 30

    @pytest.mark.asyncio
    async def test_empty_kline(self, collector):
        c, mock_ctx = collector
        from futu import RET_OK
        mock_ctx.request_history_kline.return_value = (RET_OK, pd.DataFrame(), None)

        df = await c.get_history("AAPL", interval="1m", period="1d")
        assert df.empty

    @pytest.mark.asyncio
    async def test_interval_mapping(self, collector):
        c, mock_ctx = collector
        from futu import RET_OK, KLType
        mock_ctx.request_history_kline.return_value = (RET_OK, _make_kline_df(10), None)

        await c.get_history("AAPL", interval="5m", period="1d")

        call_kwargs = mock_ctx.request_history_kline.call_args
        assert call_kwargs.kwargs.get("ktype") == KLType.K_5M or call_kwargs[1].get("ktype") == KLType.K_5M


class TestGetOptionChain:
    @pytest.mark.asyncio
    async def test_returns_options(self, collector):
        c, mock_ctx = collector
        from futu import RET_OK
        chain_df = pd.DataFrame([{
            "code": "US.AAPL250321C00150000",
            "strike_price": 150.0,
            "strike_time": "2025-03-21",
            "option_type": "CALL",
            "option_area_type": 0,
        }])
        mock_ctx.get_option_chain.return_value = (RET_OK, chain_df)
        mock_ctx.get_market_snapshot.return_value = (RET_OK, pd.DataFrame([{
            "code": "US.AAPL250321C00150000",
            "last_price": 2.50,
            "bid_price": 2.45,
            "ask_price": 2.55,
            "volume": 5000,
            "option_implied_volatility": 0,
            "option_open_interest": 0,
            "option_delta": None,
            "option_gamma": None,
            "option_theta": None,
            "option_vega": None,
        }]))

        options = await c.get_option_chain("AAPL")

        assert len(options) == 1
        assert options[0].underlying == "AAPL"
        assert options[0].strike == 150.0
        assert options[0].option_type == "call"
        assert options[0].delta is None  # LV1 has no greeks

    @pytest.mark.asyncio
    async def test_empty_chain(self, collector):
        c, mock_ctx = collector
        from futu import RET_OK
        mock_ctx.get_option_chain.return_value = (RET_OK, pd.DataFrame())

        options = await c.get_option_chain("AAPL")
        assert options == []


class TestSubscribeQuotes:
    def test_subscribe_calls_ctx(self, collector):
        c, mock_ctx = collector
        from futu import RET_OK
        mock_ctx.subscribe.return_value = (RET_OK, None)

        callback = MagicMock()
        c.subscribe_quotes(["AAPL", "TSLA"], callback)

        mock_ctx.subscribe.assert_called_once()
        args = mock_ctx.subscribe.call_args[0]
        assert "US.AAPL" in args[0]
        assert "US.TSLA" in args[0]
        assert c._subscription_count == 4  # 2 symbols × 2 sub types (QUOTE+K_1M)

    def test_quota_warning(self, collector):
        c, mock_ctx = collector
        from futu import RET_OK
        mock_ctx.subscribe.return_value = (RET_OK, None)
        c._subscription_quota = 3
        c._subscription_count = 2

        callback = MagicMock()
        # Should only subscribe 1 symbol (quota allows 1 more)
        c.subscribe_quotes(["AAPL", "TSLA"], callback)

        args = mock_ctx.subscribe.call_args[0]
        assert len(args[0]) == 1
        assert c._subscription_count == 4  # 2 + 1 symbol × 2 sub types


# ── Watchdog tests ──


class TestSafeCloseCtx:
    def test_close_sets_none(self, collector):
        c, mock_ctx = collector
        assert c._ctx is not None
        c._safe_close_ctx()
        assert c._ctx is None
        mock_ctx.close.assert_called_once()

    def test_close_when_already_none(self, collector):
        c, _ = collector
        c._ctx = None
        c._safe_close_ctx()  # should not raise
        assert c._ctx is None

    def test_close_suppresses_exception(self, collector):
        c, mock_ctx = collector
        mock_ctx.close.side_effect = RuntimeError("boom")
        c._safe_close_ctx()  # should not raise
        assert c._ctx is None


class TestWatchdogLifecycle:
    @pytest.mark.asyncio
    async def test_start_stop(self, collector):
        c, mock_ctx = collector
        from futu import RET_OK
        mock_ctx.get_global_state.return_value = (RET_OK, {})

        await c.start_watchdog(interval=600)  # long interval, won't fire
        assert c._watchdog_task is not None
        assert not c._watchdog_task.done()

        await c.stop_watchdog()
        assert c._watchdog_task is None

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self, collector):
        c, _ = collector
        await c.start_watchdog(interval=600)
        task1 = c._watchdog_task
        await c.start_watchdog(interval=600)
        assert c._watchdog_task is task1  # same task, not replaced
        await c.stop_watchdog()

    @pytest.mark.asyncio
    async def test_stop_when_not_started(self, collector):
        c, _ = collector
        await c.stop_watchdog()  # should not raise


class TestWatchdogProbe:
    @pytest.mark.asyncio
    async def test_healthy_probe_updates_timestamp(self, collector):
        c, mock_ctx = collector
        from futu import RET_OK
        mock_ctx.get_global_state.return_value = (RET_OK, {})

        old_ts = c._last_ok_ts
        # Run one iteration manually via the internal loop
        await c.start_watchdog(interval=1)
        await asyncio.sleep(1.5)  # let one probe fire
        await c.stop_watchdog()

        assert c._healthy is True
        assert c._last_ok_ts > old_ts

    @pytest.mark.asyncio
    async def test_failed_probe_recycles_ctx(self, collector):
        c, mock_ctx = collector
        from futu import RET_ERROR
        mock_ctx.get_global_state.return_value = (RET_ERROR, "dead")

        await c.start_watchdog(interval=1)
        await asyncio.sleep(1.5)
        await c.stop_watchdog()

        assert c._healthy is False
        assert c._ctx is None  # recycled

    @pytest.mark.asyncio
    async def test_close_stops_watchdog(self, collector):
        c, mock_ctx = collector
        from futu import RET_OK
        mock_ctx.get_global_state.return_value = (RET_OK, {})
        mock_ctx.close.return_value = None

        await c.start_watchdog(interval=600)
        assert c._watchdog_task is not None
        await c.close()
        assert c._watchdog_task is None
        # close() shuts down the module-level _thread_pool; reset it for subsequent tests
        c._reset_thread_pool()


class TestRetryUpdatesLastOk:
    @pytest.mark.asyncio
    async def test_successful_retry_updates_ts(self, collector):
        c, mock_ctx = collector
        from futu import RET_OK
        mock_ctx.get_stock_quote.return_value = (RET_OK, _make_quote_df("US.SPY", 500.0))

        old_ts = c._last_ok_ts
        await c.get_stock_quote("SPY")
        assert c._last_ok_ts > old_ts


class TestRunSyncSafeClose:
    @pytest.mark.asyncio
    async def test_timeout_calls_safe_close(self, collector):
        c, mock_ctx = collector
        import src.collector.futu as futu_mod

        def blocking():
            import time as t
            t.sleep(5)

        original_timeout = futu_mod.CALL_TIMEOUT_SECONDS
        futu_mod.CALL_TIMEOUT_SECONDS = 0.1
        try:
            with pytest.raises(asyncio.TimeoutError):
                await c._run_sync(blocking)
            assert c._ctx is None
        finally:
            futu_mod.CALL_TIMEOUT_SECONDS = original_timeout
