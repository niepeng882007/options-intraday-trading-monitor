from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pandas as pd
from futu import (
    IndexOptionType,
    KLType,
    OpenQuoteContext,
    RET_OK,
    SubType,
)

from src.utils.logger import setup_logger

logger = setup_logger("hk_collector")
HKT = timezone(timedelta(hours=8))


class HKCollector:
    """Synchronous HK market data collector via Futu OpenAPI."""

    def __init__(self, host: str = "127.0.0.1", port: int = 11111):
        self._host = host
        self._port = port
        self._ctx: OpenQuoteContext | None = None

    def connect(self) -> None:
        self._ctx = OpenQuoteContext(host=self._host, port=self._port)
        logger.info("HK collector connected to FutuOpenD %s:%d", self._host, self._port)

    def close(self) -> None:
        if self._ctx:
            self._ctx.close()
            self._ctx = None

    def _ensure_ctx(self) -> OpenQuoteContext:
        if self._ctx is None:
            self.connect()
        return self._ctx

    def get_global_state(self) -> dict:
        ctx = self._ensure_ctx()
        ret, data = ctx.get_global_state()
        if ret != RET_OK:
            raise RuntimeError(f"get_global_state failed: {data}")
        return data

    def get_quote(self, symbol: str) -> dict:
        """Get stock/index quote via get_market_snapshot (has bid/ask unlike get_stock_quote for HK)."""
        ctx = self._ensure_ctx()
        # Subscribe first
        ret, msg = ctx.subscribe([symbol], [SubType.QUOTE])
        if ret != RET_OK:
            raise RuntimeError(f"subscribe failed for {symbol}: {msg}")

        ret, data = ctx.get_market_snapshot([symbol])
        if ret != RET_OK:
            raise RuntimeError(f"get_market_snapshot failed: {data}")

        row = data.iloc[0]
        return {
            "symbol": symbol,
            "last_price": float(row.get("last_price", 0)),
            "open_price": float(row.get("open_price", 0)),
            "high_price": float(row.get("high_price", 0)),
            "low_price": float(row.get("low_price", 0)),
            "prev_close": float(row.get("prev_close_price", 0)),
            "volume": int(row.get("volume", 0) or 0),
            "turnover": float(row.get("turnover", 0) or 0),
            "bid_price": float(row.get("bid_price", 0) or 0),
            "ask_price": float(row.get("ask_price", 0) or 0),
            "amplitude": float(row.get("amplitude", 0) or 0),
            "turnover_rate": float(row.get("turnover_rate", 0) or 0),
            "timestamp": time.time(),
        }

    def get_history_kline(self, symbol: str, days: int = 5) -> pd.DataFrame:
        """Get 1-minute historical K-lines for given number of trading days.

        Returns DataFrame with DatetimeIndex (Asia/Hong_Kong), columns: Open, High, Low, Close, Volume.
        331 bars/day for HK (150 morning + 1 close + 180 afternoon).
        """
        ctx = self._ensure_ctx()
        today = datetime.now(HKT).date()
        start = (today - timedelta(days=days + 3)).strftime("%Y-%m-%d")  # Extra buffer for weekends/holidays
        end = today.strftime("%Y-%m-%d")

        # Futu returns OLDEST max_count bars when start+end given
        # Need room for (days) historical + today + weekend/holiday buffer
        max_count = min((days + 3) * 340, 5000)

        ret, data, _ = ctx.request_history_kline(
            symbol, ktype=KLType.K_1M, start=start, end=end, max_count=max_count,
        )
        if ret != RET_OK:
            raise RuntimeError(f"request_history_kline failed: {data}")

        if data.empty:
            return pd.DataFrame()

        df = data.rename(columns={
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
        return df[["Open", "High", "Low", "Close", "Volume"]]

    def get_option_chain_with_oi(
        self,
        symbol: str,
        index_option_type: str | None = None,
        strike_time: str | None = None,
    ) -> pd.DataFrame:
        """Get option chain with real OI from snapshot.

        Args:
            symbol: underlying symbol
            index_option_type: NORMAL or MINI (index options only); None for stock options
            strike_time: optional expiry date filter (e.g. "2026-03-18") to reduce API calls

        Returns DataFrame with columns: code, option_type, strike_price, strike_time,
        open_interest, implied_volatility, delta, gamma, theta, vega, last_price, volume.
        Falls back to chain structure without OI/Greeks if snapshot fails.
        """
        ctx = self._ensure_ctx()

        # Get chain structure — only pass index_option_type for index options
        kwargs: dict = {}
        if index_option_type:
            idx_type = getattr(IndexOptionType, index_option_type, IndexOptionType.NORMAL)
            kwargs["index_option_type"] = idx_type
        if strike_time:
            kwargs["start"] = strike_time
            kwargs["end"] = strike_time
        ret, chain = ctx.get_option_chain(symbol, **kwargs)
        if ret != RET_OK:
            logger.warning("get_option_chain failed for %s: %s", symbol, chain)
            return pd.DataFrame()
        if chain is None or chain.empty:
            logger.warning("get_option_chain returned empty for %s (kwargs=%s)", symbol, kwargs)
            return pd.DataFrame()

        logger.debug("get_option_chain for %s: %d codes", symbol, len(chain))

        # Get real OI + Greeks from snapshot (batch in groups of 200)
        all_codes = chain["code"].tolist()
        snapshot_rows = []

        for i in range(0, len(all_codes), 200):
            batch = all_codes[i : i + 200]
            ret2, snap = ctx.get_market_snapshot(batch)
            if ret2 == RET_OK and not snap.empty:
                for _, row in snap.iterrows():
                    snapshot_rows.append({
                        "code": row["code"],
                        "open_interest": int(row.get("option_open_interest", 0) or 0),
                        "implied_volatility": float(row.get("option_implied_volatility", 0) or 0),
                        "delta": float(row.get("option_delta", 0) or 0),
                        "gamma": float(row.get("option_gamma", 0) or 0),
                        "theta": float(row.get("option_theta", 0) or 0),
                        "vega": float(row.get("option_vega", 0) or 0),
                        "last_price": float(row.get("last_price", 0) or 0),
                        "snap_volume": int(row.get("volume", 0) or 0),
                    })
            else:
                logger.warning("get_market_snapshot failed for %s option batch %d: ret=%s", symbol, i, ret2)

        base_cols = ["code", "option_type", "strike_price", "strike_time"]

        if not snapshot_rows:
            # Fallback: return chain structure without OI/Greeks
            logger.warning(
                "No snapshot data for %s options (%d codes) — returning chain structure only",
                symbol, len(all_codes),
            )
            result = chain[base_cols].copy()
            for col in ["open_interest", "implied_volatility", "delta", "gamma", "theta", "vega", "last_price", "snap_volume"]:
                result[col] = 0
            return result

        snap_df = pd.DataFrame(snapshot_rows)

        # Merge chain info with snapshot
        merged = chain[base_cols].merge(snap_df, on="code", how="left")
        return merged

    def get_option_expiration_dates(self, symbol: str, index_option_type: str | None = None) -> list[dict]:
        """Get available option expiration dates.

        Args:
            symbol: underlying symbol
            index_option_type: NORMAL or MINI (index options only); None for stock options
        """
        ctx = self._ensure_ctx()
        kwargs: dict = {}
        if index_option_type:
            idx_type = getattr(IndexOptionType, index_option_type, IndexOptionType.NORMAL)
            kwargs["index_option_type"] = idx_type
        ret, data = ctx.get_option_expiration_date(symbol, **kwargs)
        if ret != RET_OK:
            logger.warning("get_option_expiration_date failed for %s: %s", symbol, data)
            return []
        if data is None or data.empty:
            logger.warning("get_option_expiration_date returned empty for %s", symbol)
            return []
        return data.to_dict("records")

    def get_order_book(self, symbol: str, num: int = 10) -> dict:
        """Get LV2 order book.

        Returns dict with keys: code, Ask, Bid
        Ask/Bid are lists of tuples: (price, volume, order_num, detail_dict)
        """
        ctx = self._ensure_ctx()
        ret, msg = ctx.subscribe([symbol], [SubType.ORDER_BOOK])
        if ret != RET_OK:
            raise RuntimeError(f"subscribe ORDER_BOOK failed: {msg}")

        ret, data = ctx.get_order_book(symbol, num=num)
        if ret != RET_OK:
            raise RuntimeError(f"get_order_book failed: {data}")
        return data

    def get_kline_quota(self) -> tuple[int, int]:
        """Return (used, remaining) K-line quota."""
        ctx = self._ensure_ctx()
        ret, data = ctx.get_history_kl_quota(get_detail=False)
        if ret != RET_OK:
            return 0, 0
        # data is tuple: (used, remaining, detail_list)
        if isinstance(data, tuple):
            return data[0], data[1]
        return 0, 0

    def get_subscription_info(self) -> dict:
        """Get current subscription usage."""
        ctx = self._ensure_ctx()
        ret, data = ctx.query_subscription(is_all_conn=True)
        if ret != RET_OK:
            return {}
        return data if isinstance(data, dict) else {}
