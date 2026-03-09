"""HK backtest data loader with CSV caching.

Fetches 1-minute bars from Futu OpenAPI for HK symbols,
with local CSV cache to avoid redundant API calls.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from src.utils.logger import setup_logger

logger = setup_logger("hk_backtest_data")

CACHE_DIR = Path("data/hk_backtest_cache")
HKT = timezone(timedelta(hours=8))


class HKDataLoader:
    """Downloads and caches HK 1-minute bars from Futu OpenAPI."""

    def __init__(
        self,
        cache_dir: str | Path = CACHE_DIR,
        futu_host: str = "127.0.0.1",
        futu_port: int = 11111,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._futu_host = futu_host
        self._futu_port = futu_port
        self._ctx = None
        self._connect_futu()

    def _connect_futu(self) -> None:
        try:
            from futu import OpenQuoteContext, RET_OK

            self._ctx = OpenQuoteContext(host=self._futu_host, port=self._futu_port)
            ret, data = self._ctx.get_global_state()
            if ret != RET_OK:
                self._ctx.close()
                self._ctx = None
                raise RuntimeError(f"get_global_state failed: {data}")
            logger.info(
                "HK backtest connected to FutuOpenD at %s:%d",
                self._futu_host, self._futu_port,
            )
        except Exception as exc:
            if self._ctx is not None:
                try:
                    self._ctx.close()
                except Exception:
                    pass
                self._ctx = None
            raise ConnectionError(
                f"Failed to connect to FutuOpenD at {self._futu_host}:{self._futu_port}. "
                f"Make sure FutuOpenD is running. Error: {exc}"
            ) from exc

    def close(self) -> None:
        if self._ctx is not None:
            self._ctx.close()
            self._ctx = None
            logger.info("HK backtest disconnected from FutuOpenD")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def load(self, symbol: str, days: int = 20) -> pd.DataFrame:
        """Load 1m bars for a HK symbol.

        Args:
            symbol: Futu symbol (e.g. "HK.800000", "HK.00700")
            days: Number of trading days to load

        Returns:
            DataFrame with DatetimeIndex (Asia/Hong_Kong), columns: Open, High, Low, Close, Volume
        """
        cache_key = self._cache_key(symbol, days)
        cache_path = self._cache_dir / f"{cache_key}.csv"

        if cache_path.exists():
            logger.info("Loading cached data: %s", cache_path.name)
            df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
            if df.index.tz is None:
                df.index = df.index.tz_localize("Asia/Hong_Kong")
            return df

        df = self._download(symbol, days)
        if not df.empty:
            df.to_csv(cache_path)
            logger.info("Cached %d bars to %s", len(df), cache_path.name)
        return df

    def load_all(self, symbols: list[str], days: int = 20) -> dict[str, pd.DataFrame]:
        """Load bars for multiple symbols."""
        result = {}
        for i, sym in enumerate(symbols, 1):
            logger.info("[%d/%d] Loading %s ...", i, len(symbols), sym)
            df = self.load(sym, days=days)
            if df.empty:
                logger.warning("No data for %s, skipping", sym)
            else:
                result[sym] = df
                logger.info("Loaded %s: %d bars", sym, len(df))
        return result

    def _download(self, symbol: str, days: int) -> pd.DataFrame:
        """Download 1m bars from Futu with pagination."""
        from futu import KLType, RET_OK

        if self._ctx is None:
            raise RuntimeError("Not connected to FutuOpenD")

        today = datetime.now(HKT).date()
        # Extra buffer for weekends/holidays
        start = (today - timedelta(days=int(days * 1.6 + 10))).strftime("%Y-%m-%d")
        end = today.strftime("%Y-%m-%d")

        logger.info("Fetching %s from Futu: %s -> %s", symbol, start, end)

        # Paginated fetch (max 1000 bars per page)
        frames = []
        page_req_key = None
        total_bars = 0

        while True:
            ret, data, next_page = self._ctx.request_history_kline(
                symbol,
                start=start,
                end=end,
                ktype=KLType.K_1M,
                max_count=1000,
                page_req_key=page_req_key,
            )
            if ret != RET_OK:
                raise RuntimeError(f"request_history_kline failed for {symbol}: {data}")
            if data.empty:
                break
            frames.append(data)
            total_bars += len(data)
            logger.debug("  page %d: %d bars (total %d)", len(frames), len(data), total_bars)
            if next_page is None:
                break
            page_req_key = next_page

        if not frames:
            logger.warning("No data downloaded for %s", symbol)
            return pd.DataFrame()

        raw = pd.concat(frames, ignore_index=True)

        # Normalize to standard format (same as HKCollector.get_history_kline)
        df = raw.rename(columns={
            "open": "Open", "high": "High", "low": "Low",
            "close": "Close", "volume": "Volume", "turnover": "Turnover",
            "time_key": "Datetime",
        })
        df["Datetime"] = pd.to_datetime(df["Datetime"])
        df = df.set_index("Datetime")
        if df.index.tz is None:
            df.index = df.index.tz_localize("Asia/Hong_Kong")

        # For indices (volume=0), use turnover as volume proxy
        if "Turnover" in df.columns and (df["Volume"] == 0).all():
            df["Volume"] = df["Turnover"]

        df = df[["Open", "High", "Low", "Close", "Volume"]]

        # Filter to HK trading hours (09:30-12:00 + 13:00-16:00)
        df = df[
            ((df.index.time >= pd.Timestamp("09:30").time()) &
             (df.index.time <= pd.Timestamp("12:00").time())) |
            ((df.index.time >= pd.Timestamp("13:00").time()) &
             (df.index.time <= pd.Timestamp("16:00").time()))
        ]

        # Truncate to last N trading days
        trading_days = sorted(set(df.index.date))
        if len(trading_days) > days:
            cutoff_date = trading_days[-days]
            df = df[df.index.date >= cutoff_date]

        logger.info("Downloaded %s: %d bars (1m), %d trading days", symbol, len(df), len(set(df.index.date)))
        time.sleep(0.5)  # Rate limit
        return df

    def _cache_key(self, symbol: str, days: int) -> str:
        safe_symbol = symbol.replace(".", "_")
        return f"{safe_symbol}_{days}d_{datetime.now().strftime('%Y%m%d')}"
