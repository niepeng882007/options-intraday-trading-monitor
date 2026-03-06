from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable

import pandas as pd
from futu import (
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

    def _create_ctx(self) -> OpenQuoteContext:
        return OpenQuoteContext(host=self._host, port=self._port)

    def _ensure_connected(self) -> OpenQuoteContext:
        """Return the context, reconnecting if needed. Called from sync threads."""
        if self._ctx is None:
            self._ctx = self._create_ctx()
            logger.info("Reconnected to FutuOpenD")
        return self._ctx

    # ── Helpers ──

    def _run_sync(self, fn, *args):
        loop = asyncio.get_running_loop()
        return loop.run_in_executor(_thread_pool, fn, *args)

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
        )

    async def get_stock_quote(self, symbol: str) -> StockQuote:
        quote: StockQuote = await self._retry(self._fetch_stock_quote, symbol)
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
                    open_interest=int(row.get("option_area_type", 0) or 0),
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

        ret, data, _ = ctx.request_history_kline(
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

        # Rename columns to match expected format
        df = data.rename(columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
            "time_key": "Datetime",
        })

        # Set DatetimeIndex
        df["Datetime"] = pd.to_datetime(df["Datetime"])
        df = df.set_index("Datetime")
        df.index = df.index.tz_localize("America/New_York") if df.index.tz is None else df.index

        # Keep only OHLCV columns
        df = df[["Open", "High", "Low", "Close", "Volume"]]
        return df

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
                    )
                    if self._loop is not None:
                        asyncio.run_coroutine_threadsafe(callback(quote), self._loop)
                return ret, data

        ctx.set_handler(_QuoteHandler())
        ret, data = ctx.subscribe(futu_codes, [SubType.QUOTE])
        if ret != RET_OK:
            raise RuntimeError(f"subscribe failed: {data}")

        self._subscription_count += len(futu_codes)
        logger.info(
            "Subscribed to %d symbols (%d/%d quota used)",
            len(futu_codes),
            self._subscription_count,
            self._subscription_quota,
        )
