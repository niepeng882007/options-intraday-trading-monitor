from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

import math

import pandas as pd
import yfinance as yf

from src.collector.base import BaseCollector, OptionQuote, StockQuote
from src.utils.logger import setup_logger

logger = setup_logger("yahoo_collector")

BACKOFF_BASE_SECONDS = 1
BACKOFF_MAX_SECONDS = 60
MAX_RETRIES = 3
_thread_pool = ThreadPoolExecutor(max_workers=4)


def _safe_int(value, default: int = 0) -> int:
    if value is None:
        return default
    try:
        fval = float(value)
        if math.isnan(fval) or math.isinf(fval):
            return default
        return int(fval)
    except (ValueError, TypeError):
        return default


class YahooCollector(BaseCollector):
    """Data collector backed by yfinance (Yahoo Finance).

    All yfinance calls are blocking I/O — they are offloaded to a thread
    pool so the async event loop stays responsive.
    """

    def __init__(self) -> None:
        self._request_timestamps: list[float] = []

    # ── helpers ──

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
                    "yfinance request failed (attempt %d/%d): %s",
                    attempt + 1,
                    retries,
                    exc,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_SECONDS)
        raise RuntimeError(
            f"yfinance request failed after {retries} retries"
        ) from last_exc

    # ── Stock quotes ──

    @staticmethod
    def _fetch_stock_quote(symbol: str) -> StockQuote:
        ticker = yf.Ticker(symbol)
        info = ticker.fast_info
        return StockQuote(
            symbol=symbol,
            price=float(info.last_price),
            bid=float(getattr(info, "bid", 0.0) or 0.0),
            ask=float(getattr(info, "ask", 0.0) or 0.0),
            volume=int(info.last_volume or 0),
            timestamp=time.time(),
        )

    async def get_stock_quote(self, symbol: str) -> StockQuote:
        quote: StockQuote = await self._retry(self._fetch_stock_quote, symbol)
        logger.debug("Quote %s: $%.2f", symbol, quote.price)
        return quote

    # ── Option chains ──

    @staticmethod
    def _fetch_option_chain(symbol: str, expiration: str | None) -> list[OptionQuote]:
        ticker = yf.Ticker(symbol)
        available_expirations = ticker.options
        if not available_expirations:
            logger.warning("No options available for %s", symbol)
            return []

        target_expiration = expiration or available_expirations[0]
        chain = ticker.option_chain(target_expiration)

        results: list[OptionQuote] = []
        now = time.time()

        for option_type, df in [("call", chain.calls), ("put", chain.puts)]:
            for _, row in df.iterrows():
                results.append(
                    OptionQuote(
                        contract_symbol=row["contractSymbol"],
                        underlying=symbol,
                        strike=float(row["strike"]),
                        option_type=option_type,
                        expiration=target_expiration,
                        bid=float(row.get("bid", 0) or 0),
                        ask=float(row.get("ask", 0) or 0),
                        last=float(row.get("lastPrice", 0) or 0),
                    volume=_safe_int(row.get("volume", 0)),
                    open_interest=_safe_int(row.get("openInterest", 0)),
                        implied_volatility=float(
                            row.get("impliedVolatility", 0) or 0
                        ),
                        delta=None,
                        gamma=None,
                        theta=None,
                        vega=None,
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

    @staticmethod
    def _fetch_history(symbol: str, interval: str, period: str) -> pd.DataFrame:
        ticker = yf.Ticker(symbol)
        return ticker.history(period=period, interval=interval)

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
