from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from src.utils.logger import setup_logger

logger = setup_logger("backtest_data")

CACHE_DIR = Path("data/backtest_cache")


class DataLoader:
    """Downloads historical 1m bars with CSV caching.

    Supports two data sources:
    - "futu": Futu OpenAPI (default) — unlimited 1m history via FutuOpenD
    - "yahoo": yfinance fallback — 1m capped at 7 days, 5m at 60 days
    """

    def __init__(
        self,
        cache_dir: str | Path = CACHE_DIR,
        data_source: str = "futu",
        futu_host: str = "127.0.0.1",
        futu_port: int = 11111,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._data_source = data_source
        self._futu_host = futu_host
        self._futu_port = futu_port
        self._ctx = None

        if data_source == "futu":
            self._connect_futu()

    def _connect_futu(self) -> None:
        try:
            from futu import OpenQuoteContext, RET_OK

            self._ctx = OpenQuoteContext(host=self._futu_host, port=self._futu_port)
            # Verify connection is actually alive
            ret, data = self._ctx.get_global_state()
            if ret != RET_OK:
                self._ctx.close()
                self._ctx = None
                raise RuntimeError(f"get_global_state failed: {data}")
            logger.info(
                "Connected to FutuOpenD at %s:%d", self._futu_host, self._futu_port
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
                f"Make sure FutuOpenD is running, or use --data-source yahoo as fallback. "
                f"Error: {exc}"
            ) from exc

    def close(self) -> None:
        if self._ctx is not None:
            self._ctx.close()
            self._ctx = None
            logger.info("Disconnected from FutuOpenD")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def load(
        self,
        symbol: str,
        days: int = 5,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> pd.DataFrame:
        cache_key = self._cache_key(symbol, days, start_date, end_date)
        cache_path = self._cache_dir / f"{cache_key}.csv"

        if cache_path.exists():
            logger.info("Loading cached data: %s", cache_path.name)
            df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
            if df.index.tz is None:
                df.index = df.index.tz_localize("America/New_York")
            return df

        df = self._download(symbol, days, start_date, end_date)
        if not df.empty:
            df.to_csv(cache_path)
            logger.info("Cached %d bars to %s", len(df), cache_path.name)
        return df

    def load_all(
        self,
        symbols: list[str],
        strategies: list | None = None,
        **kwargs,
    ) -> dict[str, pd.DataFrame]:
        # Auto-detect if SPY is needed for market context filters
        need_spy = False
        if strategies:
            for s in strategies:
                if s.market_context_filters.get("max_spy_day_drop_pct") is not None:
                    need_spy = True
                    break

        all_symbols = list(set(symbols))
        if need_spy and "SPY" not in all_symbols:
            all_symbols.append("SPY")

        result = {}
        for i, sym in enumerate(all_symbols, 1):
            logger.info("[%d/%d] Loading %s ...", i, len(all_symbols), sym)
            df = self.load(sym, **kwargs)
            if df.empty:
                logger.warning("No data for %s, skipping", sym)
            else:
                result[sym] = df
                logger.info("Loaded %s: %d bars", sym, len(df))
        return result

    def _download(
        self,
        symbol: str,
        days: int = 5,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> pd.DataFrame:
        if self._data_source == "futu":
            return self._download_futu(symbol, days, start_date, end_date)
        return self._download_yfinance(symbol, days, start_date, end_date)

    # ── Futu data source ──

    def _fetch_kline(self, futu_symbol: str, start: str, end: str) -> pd.DataFrame:
        from futu import KLType, RET_OK

        frames = []
        page_req_key = None
        total_bars = 0
        page_num = 0
        while True:
            ret, data, next_page = self._ctx.request_history_kline(
                futu_symbol,
                start=start,
                end=end,
                ktype=KLType.K_1M,
                max_count=1000,
                page_req_key=page_req_key,
            )
            if ret != RET_OK:
                raise RuntimeError(f"request_history_kline failed: {data}")
            if data.empty:
                break
            frames.append(data)
            page_num += 1
            total_bars += len(data)
            logger.debug(
                "  page %d: %d bars (total %d)", page_num, len(data), total_bars
            )
            if next_page is None:
                break
            page_req_key = next_page
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    def _download_futu(
        self,
        symbol: str,
        days: int = 5,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> pd.DataFrame:
        from src.collector.futu import normalize_futu_kline, to_futu

        futu_symbol = to_futu(symbol)

        if start_date and end_date:
            s_date, e_date = start_date, end_date
        else:
            today = datetime.now().date()
            # Fetch extra days to account for weekends/holidays
            start = today - timedelta(days=int(days * 1.5 + 5))
            s_date = start.strftime("%Y-%m-%d")
            e_date = today.strftime("%Y-%m-%d")

        logger.info("Fetching %s from Futu: %s -> %s", symbol, s_date, e_date)
        raw = self._fetch_kline(futu_symbol, s_date, e_date)
        if raw.empty:
            logger.warning("No data downloaded for %s", symbol)
            return pd.DataFrame()

        df = normalize_futu_kline(raw)

        # Filter to regular trading hours
        df = df.between_time("09:30", "15:59")

        # If days mode, truncate to last N trading days
        if not start_date and not end_date:
            trading_days = sorted(df.index.date)
            unique_days = list(dict.fromkeys(trading_days))
            if len(unique_days) > days:
                cutoff_date = unique_days[-days]
                df = df[df.index.date >= cutoff_date]

        logger.info("Downloaded %s: %d bars (1m)", symbol, len(df))
        # Rate limit: first request per symbol counts toward 60/30s limit
        time.sleep(0.5)
        return df

    # ── yfinance fallback ──

    def _download_yfinance(
        self,
        symbol: str,
        days: int = 5,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> pd.DataFrame:
        import yfinance as yf

        ticker = yf.Ticker(symbol)

        if start_date and end_date:
            start = datetime.strptime(start_date, "%Y-%m-%d")
            end = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
            delta_days = (end - start).days
            if delta_days <= 7:
                interval = "1m"
            else:
                interval = "5m"
                logger.warning(
                    "Date range > 7 days, downgrading to 5m interval for %s", symbol
                )
            df = ticker.history(
                start=start_date, end=end.strftime("%Y-%m-%d"), interval=interval
            )
        elif days <= 5:
            df = ticker.history(period="7d", interval="1m")
        else:
            calendar_days = days + 5
            max_5m_days = 59
            if calendar_days > max_5m_days:
                logger.warning(
                    "Capping %dd request to %dd (Yahoo 5m data limit) for %s",
                    calendar_days,
                    max_5m_days,
                    symbol,
                )
                calendar_days = max_5m_days
            logger.warning(
                "days=%d > 5, downgrading to 5m interval for %s", days, symbol
            )
            df = ticker.history(period=f"{calendar_days}d", interval="5m")

        if df.empty:
            logger.warning("No data downloaded for %s", symbol)
            return df

        if df.index.tz is None:
            df.index = df.index.tz_localize("America/New_York")
        else:
            df.index = df.index.tz_convert("America/New_York")

        df = df.between_time("09:30", "15:59")

        if not start_date and not end_date:
            trading_days = sorted(df.index.date)
            unique_days = list(dict.fromkeys(trading_days))
            if len(unique_days) > days:
                cutoff_date = unique_days[-days]
                df = df[df.index.date >= cutoff_date]

        logger.info(
            "Downloaded %s: %d bars (%s interval)",
            symbol,
            len(df),
            "1m" if days <= 5 else "5m",
        )
        return df

    def _cache_key(
        self, symbol: str, days: int, start_date: str | None, end_date: str | None
    ) -> str:
        suffix = self._data_source
        if start_date and end_date:
            return f"{symbol}_{start_date}_{end_date}_{suffix}"
        return f"{symbol}_{days}d_{datetime.now().strftime('%Y%m%d')}_{suffix}"
