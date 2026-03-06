from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

from src.utils.logger import setup_logger

logger = setup_logger("backtest_data")

CACHE_DIR = Path("data/backtest_cache")


class DataLoader:
    """Downloads historical 1m bars via yfinance with CSV caching."""

    def __init__(self, cache_dir: str | Path = CACHE_DIR) -> None:
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

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
        for sym in all_symbols:
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
        ticker = yf.Ticker(symbol)

        if start_date and end_date:
            # Date range mode
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
            df = ticker.history(start=start_date, end=end.strftime("%Y-%m-%d"), interval=interval)
        elif days <= 5:
            # yfinance 1m limit: max 7 days
            df = ticker.history(period="7d", interval="1m")
        else:
            # >5 days: downgrade to 5m (Yahoo limits 5m data to last 60 calendar days)
            calendar_days = days + 5
            max_5m_days = 59  # Yahoo hard limit ~60d, use 59 for safety
            if calendar_days > max_5m_days:
                logger.warning(
                    "Capping %dd request to %dd (Yahoo 5m data limit) for %s",
                    calendar_days, max_5m_days, symbol,
                )
                calendar_days = max_5m_days
            logger.warning("days=%d > 5, downgrading to 5m interval for %s", days, symbol)
            df = ticker.history(period=f"{calendar_days}d", interval="5m")

        if df.empty:
            logger.warning("No data downloaded for %s", symbol)
            return df

        # Ensure timezone
        if df.index.tz is None:
            df.index = df.index.tz_localize("America/New_York")
        else:
            df.index = df.index.tz_convert("America/New_York")

        # Filter to regular trading hours (09:30-16:00 ET)
        df = df.between_time("09:30", "15:59")

        # If no date range specified, take last N trading days
        if not start_date and not end_date:
            trading_days = sorted(df.index.date)
            unique_days = list(dict.fromkeys(trading_days))
            if len(unique_days) > days:
                cutoff_date = unique_days[-days]
                df = df[df.index.date >= cutoff_date]

        logger.info("Downloaded %s: %d bars (%s interval)", symbol, len(df),
                     "1m" if days <= 5 else "5m")
        return df

    @staticmethod
    def _cache_key(
        symbol: str, days: int, start_date: str | None, end_date: str | None
    ) -> str:
        if start_date and end_date:
            return f"{symbol}_{start_date}_{end_date}"
        return f"{symbol}_{days}d_{datetime.now().strftime('%Y%m%d')}"
