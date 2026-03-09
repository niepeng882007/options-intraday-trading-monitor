from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable

import pandas as pd
from futu import (
    CurKlineHandlerBase,
    KLType,
    OpenQuoteContext,
    RET_OK,
    StockQuoteHandlerBase,
    SubType,
)

from src.collector.base import BaseCollector, OptionQuote, StockQuote
from src.utils.logger import setup_logger

if TYPE_CHECKING:
    pass

logger = setup_logger("futu_collector")

BACKOFF_BASE_SECONDS = 1
BACKOFF_MAX_SECONDS = 60
MAX_RETRIES = 3
CALL_TIMEOUT_SECONDS = 30

# Futu protocol requires serial access — use a single-thread pool.
_thread_pool = ThreadPoolExecutor(max_workers=1)

ET = timezone(timedelta(hours=-5))

# ── Symbol mapping ──

def to_futu(symbol: str) -> str:
    """Convert plain symbol to Futu format: AAPL -> US.AAPL"""
    if "." in symbol and symbol.split(".")[0] == "US":
        return symbol
    return f"US.{symbol}"


def from_futu(futu_code: str) -> str:
    """Convert Futu format to plain symbol: US.AAPL -> AAPL"""
    if futu_code.startswith("US."):
        return futu_code[3:]
    return futu_code


# ── Interval / period mapping ──

INTERVAL_MAP = {
    "1m": KLType.K_1M,
    "5m": KLType.K_5M,
    "15m": KLType.K_15M,
}


def normalize_futu_kline(data: pd.DataFrame) -> pd.DataFrame:
    """Normalize Futu K-line DataFrame to standard OHLCV format.

    Shared by FutuCollector._fetch_history and backtest DataLoader.
    """
    df = data.rename(columns={
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
        "time_key": "Datetime",
    })
    df["Datetime"] = pd.to_datetime(df["Datetime"])
    df = df.set_index("Datetime")
    if df.index.tz is None:
        df.index = df.index.tz_localize("America/New_York")
    return df[["Open", "High", "Low", "Close", "Volume"]]


def _period_to_dates(period: str) -> tuple[str, str]:
    """Convert period string to (start, end) date strings for Futu API."""
    today = datetime.now(ET).date()
    end = today.strftime("%Y-%m-%d")
    if period == "1d":
        start = end
    elif period == "5d":
        start = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    else:
        days = int(period.rstrip("d"))
        start = (today - timedelta(days=days + 2)).strftime("%Y-%m-%d")
    return start, end


class FutuCollector(BaseCollector):
    """Data collector backed by Futu OpenAPI (FutuOpenD).

    All Futu SDK calls are blocking — they are offloaded to a single-thread
    pool so the async event loop stays responsive. The Futu protocol requires
    serial access, hence max_workers=1.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 11111,
        subscription_quota: int = 300,
    ) -> None:
        self._host = host
        self._port = port
        self._subscription_quota = subscription_quota
        self._subscription_count = 0
        self._ctx: OpenQuoteContext | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._quote_cache: dict[str, StockQuote] = {}

    # ── Lifecycle ──

    async def connect(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._ctx = await self._run_sync(self._create_ctx)
        logger.info("Connected to FutuOpenD at %s:%d", self._host, self._port)

    async def close(self) -> None:
        if self._ctx is not None:
            await self._run_sync(self._ctx.close)
            self._ctx = None
            logger.info("Disconnected from FutuOpenD")

    async def health_check(self) -> None:
        """Check if the Futu connection is alive."""
        await self._run_sync(self._check_connection)

    def _check_connection(self) -> dict:
        ctx = self._ensure_connected()
        ret, data = ctx.get_global_state()
        if ret != RET_OK:
            raise RuntimeError(f"Futu health check failed: {data}")
        return data

    async def get_connection_info(self) -> dict:
        """Return detailed Futu connection status."""
        info: dict = {
            "source": "futu",
            "host": self._host,
            "port": self._port,
            "connected": self._ctx is not None,
            "subscription_used": self._subscription_count,
            "subscription_quota": self._subscription_quota,
            "cached_quotes": len(self._quote_cache),
        }

        # Quote cache freshness
        if self._quote_cache:
            now = time.time()
            ages = {s: now - q.timestamp for s, q in self._quote_cache.items()}
            info["quote_ages"] = {s: round(a, 1) for s, a in sorted(ages.items())}

        # Try to get global state from FutuOpenD
        try:
            data = await self._run_sync(self._check_connection)
            info["server_ver"] = data.get("server_ver", "N/A")
            info["qot_logined"] = data.get("qot_logined", "N/A")
            info["trd_logined"] = data.get("trd_logined", "N/A")
            info["market_us"] = data.get("market_us", "N/A")
            info["global_state"] = "OK"
        except Exception as exc:
            info["global_state"] = f"ERROR: {exc}"

        return info

    def _create_ctx(self) -> OpenQuoteContext:
        return OpenQuoteContext(host=self._host, port=self._port)

    def _ensure_connected(self) -> OpenQuoteContext:
        """Return the context, reconnecting if needed. Called from sync threads."""
        if self._ctx is None:
            self._ctx = self._create_ctx()
            logger.info("Reconnected to FutuOpenD")
        return self._ctx

    def _reset_thread_pool(self) -> None:
        """Replace the thread pool — the old thread may still be blocked."""
        global _thread_pool
        _thread_pool = ThreadPoolExecutor(max_workers=1)
        logger.info("Thread pool reset")

    # ── Helpers ──

    async def _run_sync(self, fn, *args):
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(_thread_pool, fn, *args)
        try:
            return await asyncio.wait_for(fut, timeout=CALL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.error(
                "Futu call timed out after %ds, resetting thread pool",
                CALL_TIMEOUT_SECONDS,
            )
            self._ctx = None
            self._reset_thread_pool()
            raise

    async def _retry(self, fn, *args, retries: int = MAX_RETRIES):
        backoff = BACKOFF_BASE_SECONDS
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                return await self._run_sync(fn, *args)
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "Futu request failed (attempt %d/%d): %s",
                    attempt + 1,
                    retries,
                    exc,
                )
                # Reset connection on failure
                self._ctx = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_SECONDS)
        raise RuntimeError(
            f"Futu request failed after {retries} retries"
        ) from last_exc

    # ── Quote cache ──

    def _cache_quote(self, quote: StockQuote) -> None:
        self._quote_cache[quote.symbol] = quote

    def get_cached_quote(self, symbol: str, max_age: float = 60.0) -> StockQuote | None:
        quote = self._quote_cache.get(symbol)
        if quote is None:
            return None
        if time.time() - quote.timestamp > max_age:
            return None
        return quote

    # ── Stock quotes ──

    def _fetch_stock_quote(self, symbol: str) -> StockQuote:
        ctx = self._ensure_connected()
        futu_code = to_futu(symbol)
        ret, data = ctx.get_stock_quote([futu_code])
        if ret != RET_OK:
            raise RuntimeError(f"get_stock_quote failed: {data}")

        row = data.iloc[0]
        return StockQuote(
            symbol=symbol,
            price=float(row["last_price"]),
            bid=float(row.get("bid_price", 0) or 0),
            ask=float(row.get("ask_price", 0) or 0),
            volume=int(row.get("volume", 0) or 0),
            timestamp=time.time(),
            open_price=float(row["open_price"]) if row.get("open_price") else None,
            high_price=float(row["high_price"]) if row.get("high_price") else None,
            low_price=float(row["low_price"]) if row.get("low_price") else None,
            prev_close_price=float(row["prev_close_price"]) if row.get("prev_close_price") else None,
            change_pct=float(row["change_rate"]) if row.get("change_rate") is not None else None,
            turnover=float(row["turnover"]) if row.get("turnover") else None,
            turnover_rate=float(row["turnover_rate"]) if row.get("turnover_rate") is not None else None,
            amplitude=float(row["amplitude"]) if row.get("amplitude") is not None else None,
        )

    async def get_stock_quote(self, symbol: str) -> StockQuote:
        quote: StockQuote = await self._retry(self._fetch_stock_quote, symbol)
        self._cache_quote(quote)
        logger.debug("Quote %s: $%.2f", symbol, quote.price)
        return quote

    # ── Option chains ──

    def _fetch_option_chain(self, symbol: str, expiration: str | None) -> list[OptionQuote]:
        ctx = self._ensure_connected()
        futu_code = to_futu(symbol)

        ret, data = ctx.get_option_chain(futu_code)
        if ret != RET_OK:
            raise RuntimeError(f"get_option_chain failed: {data}")

        if data.empty:
            logger.warning("No options available for %s", symbol)
            return []

        # Filter by expiration
        if expiration:
            data = data[data["strike_time"].str.startswith(expiration)]
        else:
            # Pick nearest expiration
            dates = sorted(data["strike_time"].unique())
            if dates:
                data = data[data["strike_time"] == dates[0]]

        if data.empty:
            return []

        # Get option codes for quote lookup
        option_codes = data["code"].tolist()
        if not option_codes:
            return []

        # Fetch quotes for all option codes
        ret2, quote_data = ctx.get_stock_quote(option_codes)
        if ret2 != RET_OK:
            raise RuntimeError(f"get_stock_quote for options failed: {quote_data}")

        # Fetch option snapshots for Greeks/IV
        greeks_map: dict[str, dict] = {}
        try:
            ret3, snapshot_data = ctx.get_market_snapshot(option_codes)
            if ret3 == RET_OK and not snapshot_data.empty:
                greeks_map = {row["code"]: row for _, row in snapshot_data.iterrows()}
        except Exception:
            logger.debug("Failed to fetch option snapshots for Greeks/IV, continuing without")

        # Merge data
        quote_map = {row["code"]: row for _, row in quote_data.iterrows()}

        results: list[OptionQuote] = []
        now = time.time()

        for _, row in data.iterrows():
            code = row["code"]
            q = quote_map.get(code, {})
            g = greeks_map.get(code, {})
            option_type = "call" if row.get("option_type", "").upper() == "CALL" else "put"

            # Extract Greeks from snapshot data
            iv = float(g.get("option_implied_volatility", 0) or 0)
            delta = g.get("option_delta")
            gamma = g.get("option_gamma")
            theta = g.get("option_theta")
            vega = g.get("option_vega")

            results.append(
                OptionQuote(
                    contract_symbol=code,
                    underlying=symbol,
                    strike=float(row.get("strike_price", 0) or 0),
                    option_type=option_type,
                    expiration=str(row.get("strike_time", ""))[:10],
                    bid=float(q.get("bid_price", 0) or 0),
                    ask=float(q.get("ask_price", 0) or 0),
                    last=float(q.get("last_price", 0) or 0),
                    volume=int(q.get("volume", 0) or 0),
                    open_interest=int(g.get("option_open_interest", 0) or 0),
                    implied_volatility=iv / 100 if iv > 1 else iv,
                    delta=float(delta) if delta is not None else None,
                    gamma=float(gamma) if gamma is not None else None,
                    theta=float(theta) if theta is not None else None,
                    vega=float(vega) if vega is not None else None,
                    timestamp=now,
                )
            )
        return results

    async def get_option_chain(
        self,
        symbol: str,
        expiration: str | None = None,
    ) -> list[OptionQuote]:
        options: list[OptionQuote] = await self._retry(
            self._fetch_option_chain, symbol, expiration
        )
        logger.debug(
            "Option chain %s exp=%s: %d contracts",
            symbol,
            expiration or "nearest",
            len(options),
        )
        return options

    # ── Historical K-line data ──

    def _fetch_history(self, symbol: str, interval: str, period: str) -> pd.DataFrame:
        ctx = self._ensure_connected()
        futu_code = to_futu(symbol)
        kl_type = INTERVAL_MAP.get(interval, KLType.K_1M)
        start, end = _period_to_dates(period)

        ret, data, _page_req_key = ctx.request_history_kline(
            futu_code,
            start=start,
            end=end,
            ktype=kl_type,
            max_count=1000,
        )
        if ret != RET_OK:
            raise RuntimeError(f"request_history_kline failed: {data}")

        if data.empty:
            return pd.DataFrame()

        return normalize_futu_kline(data)

    async def get_history(
        self,
        symbol: str,
        interval: str = "1m",
        period: str = "1d",
    ) -> pd.DataFrame:
        df: pd.DataFrame = await self._retry(
            self._fetch_history, symbol, interval, period
        )
        logger.debug(
            "History %s (%s/%s): %d bars", symbol, interval, period, len(df)
        )
        return df

    # ── Extended history (for US Playbook) ──

    def _fetch_history_bars(self, symbol: str, days: int, interval: str = "1m") -> pd.DataFrame:
        """Fetch history K-lines by day count with sufficient max_count."""
        ctx = self._ensure_connected()
        futu_code = to_futu(symbol)
        kl_type = INTERVAL_MAP.get(interval, KLType.K_1M)

        today = datetime.now(ET).date()
        start = (today - timedelta(days=days + 3)).strftime("%Y-%m-%d")  # buffer for weekends
        end = today.strftime("%Y-%m-%d")
        max_count = min(days * 400 + 100, 5000)

        ret, data, _ = ctx.request_history_kline(
            futu_code, start=start, end=end, ktype=kl_type, max_count=max_count,
        )
        if ret != RET_OK:
            raise RuntimeError(f"request_history_kline failed: {data}")
        if data.empty:
            return pd.DataFrame()
        return normalize_futu_kline(data)

    async def get_history_bars(self, symbol: str, days: int = 5, interval: str = "1m") -> pd.DataFrame:
        df = await self._retry(self._fetch_history_bars, symbol, days, interval)
        logger.debug("History bars %s (%dd %s): %d bars", symbol, days, interval, len(df))
        return df

    def _fetch_snapshot(self, symbol: str) -> dict:
        """Get full market snapshot (no subscription needed)."""
        ctx = self._ensure_connected()
        futu_code = to_futu(symbol)
        ret, data = ctx.get_market_snapshot([futu_code])
        if ret != RET_OK:
            raise RuntimeError(f"get_market_snapshot failed: {data}")
        row = data.iloc[0]
        return {
            "last_price": float(row.get("last_price", 0) or 0),
            "open_price": float(row.get("open_price", 0) or 0),
            "high_price": float(row.get("high_price", 0) or 0),
            "low_price": float(row.get("low_price", 0) or 0),
            "prev_close_price": float(row.get("prev_close_price", 0) or 0),
            "volume": int(row.get("volume", 0) or 0),
            "turnover": float(row.get("turnover", 0) or 0),
        }

    async def get_snapshot(self, symbol: str) -> dict:
        return await self._retry(self._fetch_snapshot, symbol)

    def _fetch_premarket_hl(self, symbol: str) -> tuple[float, float]:
        """Get pre-market high/low via snapshot; fallback to gap range."""
        ctx = self._ensure_connected()
        futu_code = to_futu(symbol)
        ret, data = ctx.get_market_snapshot([futu_code])
        if ret != RET_OK:
            raise RuntimeError(f"get_market_snapshot failed: {data}")

        row = data.iloc[0]
        pmh = float(row.get("pre_high_price", 0) or 0)
        pml = float(row.get("pre_low_price", 0) or 0)
        if pmh > 0 and pml > 0:
            return pmh, pml

        # Fallback: gap range from open vs prev_close
        open_p = float(row.get("open_price", 0) or 0)
        prev_c = float(row.get("prev_close_price", 0) or 0)
        if open_p > 0 and prev_c > 0:
            return max(open_p, prev_c), min(open_p, prev_c)
        return open_p or prev_c, open_p or prev_c

    async def get_premarket_hl(self, symbol: str) -> tuple[float, float]:
        return await self._retry(self._fetch_premarket_hl, symbol)

    # ── Real-time push subscription ──

    def subscribe_quotes(self, symbols: list[str], callback: Callable) -> None:
        """Subscribe to real-time quote push for the given symbols."""
        ctx = self._ensure_connected()
        futu_codes = [to_futu(s) for s in symbols]

        needed = len(futu_codes)
        if self._subscription_count + needed > self._subscription_quota:
            logger.warning(
                "Subscription quota nearly exhausted: %d/%d used, need %d more. "
                "Falling back to polling for excess symbols.",
                self._subscription_count,
                self._subscription_quota,
                needed,
            )
            # Subscribe as many as quota allows
            available = self._subscription_quota - self._subscription_count
            if available <= 0:
                return
            futu_codes = futu_codes[:available]

        class _QuoteHandler(StockQuoteHandlerBase):
            def on_recv_rsp(self_, rsp_pb):
                ret, data = super(_QuoteHandler, self_).on_recv_rsp(rsp_pb)
                if ret != RET_OK:
                    return ret, data
                for _, row in data.iterrows():
                    quote = StockQuote(
                        symbol=from_futu(row["code"]),
                        price=float(row["last_price"]),
                        bid=float(row.get("bid_price", 0) or 0),
                        ask=float(row.get("ask_price", 0) or 0),
                        volume=int(row.get("volume", 0) or 0),
                        timestamp=time.time(),
                        open_price=float(row["open_price"]) if row.get("open_price") else None,
                        high_price=float(row["high_price"]) if row.get("high_price") else None,
                        low_price=float(row["low_price"]) if row.get("low_price") else None,
                        prev_close_price=float(row["prev_close_price"]) if row.get("prev_close_price") else None,
                        change_pct=float(row["change_rate"]) if row.get("change_rate") is not None else None,
                        turnover=float(row["turnover"]) if row.get("turnover") else None,
                        turnover_rate=float(row["turnover_rate"]) if row.get("turnover_rate") is not None else None,
                        amplitude=float(row["amplitude"]) if row.get("amplitude") is not None else None,
                    )
                    self._cache_quote(quote)
                    if self._loop is not None:
                        asyncio.run_coroutine_threadsafe(callback(quote), self._loop)
                return ret, data

        ctx.set_handler(_QuoteHandler())
        ret, data = ctx.subscribe(futu_codes, [SubType.QUOTE, SubType.K_1M])
        if ret != RET_OK:
            raise RuntimeError(f"subscribe failed: {data}")

        # Each symbol × 2 sub types counts toward quota
        self._subscription_count += len(futu_codes) * 2
        logger.info(
            "Subscribed to %d symbols (QUOTE+K_1M, %d/%d quota used)",
            len(futu_codes),
            self._subscription_count,
            self._subscription_quota,
        )

    def subscribe_kline(self, symbols: list[str], callback: Callable) -> None:
        """Register a handler for real-time 1-minute K-line push.

        Must be called after subscribe_quotes (which already subscribes K_1M).
        This only sets the handler callback — no additional subscription needed.
        """
        ctx = self._ensure_connected()

        collector_self = self

        class _KlineHandler(CurKlineHandlerBase):
            def on_recv_rsp(self_, rsp_pb):
                ret, data = super(_KlineHandler, self_).on_recv_rsp(rsp_pb)
                if ret != RET_OK:
                    return ret, data
                for _, row in data.iterrows():
                    try:
                        symbol = from_futu(row["code"])
                        kline_df = pd.DataFrame(
                            [{
                                "Open": float(row["open"]),
                                "High": float(row["high"]),
                                "Low": float(row["low"]),
                                "Close": float(row["close"]),
                                "Volume": int(row.get("volume", 0) or 0),
                            }],
                            index=pd.DatetimeIndex(
                                [pd.to_datetime(row["time_key"])],
                                name="Datetime",
                            ).tz_localize("America/New_York")
                            if pd.to_datetime(row["time_key"]).tzinfo is None
                            else pd.DatetimeIndex(
                                [pd.to_datetime(row["time_key"])],
                                name="Datetime",
                            ),
                        )
                        if collector_self._loop is not None:
                            asyncio.run_coroutine_threadsafe(
                                callback(symbol, kline_df), collector_self._loop
                            )
                    except Exception:
                        logger.exception("Error processing kline push for %s", row.get("code"))
                return ret, data

        ctx.set_handler(_KlineHandler())
        logger.info("K-line push handler registered for %d symbols", len(symbols))
