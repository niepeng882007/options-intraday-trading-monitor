"""Volume Profile calculation — shared across HK and US modules."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.common.types import VolumeProfileResult
from src.utils.logger import setup_logger

logger = setup_logger("volume_profile")


def calculate_volume_profile(
    bars: pd.DataFrame,
    value_area_pct: float = 0.70,
    tick_size: float | None = None,
    recency_decay: float = 0.0,
) -> VolumeProfileResult:
    """Calculate Volume Profile from 1m bars.

    Args:
        bars: DataFrame with columns Open, High, Low, Close, Volume
        value_area_pct: fraction of total volume for value area (default 0.70)
        tick_size: price binning granularity. Auto-detected if None:
            - price > 1000: tick_size = 50 (for HSI ~25000)
            - price > 100: tick_size = 1.0
            - price > 10: tick_size = 0.5
            - else: tick_size = 0.1
        recency_decay: exponential decay factor per day (0 = no decay).
            E.g. 0.3 means each older day's volume is weighted by 0.7^(days_ago).
            Reduces influence of stale price levels after trend breaks.

    Returns:
        VolumeProfileResult with poc, vah, val, volume_by_price
    """
    if bars.empty:
        return VolumeProfileResult(poc=0, vah=0, val=0)

    # Auto-detect tick size based on price range
    avg_price = bars["Close"].mean()
    if tick_size is None:
        if avg_price > 1000:
            tick_size = 50.0
        elif avg_price > 100:
            tick_size = 1.0
        elif avg_price > 10:
            tick_size = 0.5
        else:
            tick_size = 0.1

    # Precompute per-day recency weights
    day_weights: dict | None = None
    if recency_decay > 0 and hasattr(bars.index, "date"):
        trading_dates = sorted(set(bars.index.date))
        latest = trading_dates[-1]
        day_weights = {}
        for d in trading_dates:
            days_ago = (latest - d).days
            day_weights[d] = (1.0 - recency_decay) ** days_ago

    # Distribute each bar's volume across its price range
    volume_by_price: dict[float, float] = {}

    for idx, row in bars.iterrows():
        h, l, v = row["High"], row["Low"], row["Volume"]
        if v <= 0 or pd.isna(v):
            continue

        # Apply recency weight
        if day_weights is not None:
            bar_date = idx.date() if hasattr(idx, "date") else idx
            v = v * day_weights.get(bar_date, 1.0)

        # Discretize high-low range into price bins
        low_bin = np.floor(l / tick_size) * tick_size
        high_bin = np.ceil(h / tick_size) * tick_size
        bins = np.arange(low_bin, high_bin + tick_size / 2, tick_size)

        if len(bins) == 0:
            # Fallback to close price
            price_bin = round(row["Close"] / tick_size) * tick_size
            volume_by_price[price_bin] = volume_by_price.get(price_bin, 0) + v
        else:
            # Distribute volume evenly across bins
            vol_per_bin = v / len(bins)
            for price_bin in bins:
                price_bin = round(price_bin, 4)  # avoid float artifacts
                volume_by_price[price_bin] = volume_by_price.get(price_bin, 0) + vol_per_bin

    if not volume_by_price:
        return VolumeProfileResult(poc=0, vah=0, val=0)

    # POC: price level with max volume
    poc = max(volume_by_price, key=volume_by_price.get)  # type: ignore[arg-type]
    total_volume = sum(volume_by_price.values())
    target_volume = total_volume * value_area_pct

    # Value Area: expand from POC up/down
    sorted_prices = sorted(volume_by_price.keys())
    poc_idx = sorted_prices.index(poc)

    accumulated = volume_by_price[poc]
    low_idx = poc_idx
    high_idx = poc_idx

    while accumulated < target_volume and (low_idx > 0 or high_idx < len(sorted_prices) - 1):
        # Check volume above and below
        vol_above = (
            volume_by_price.get(sorted_prices[high_idx + 1], 0)
            if high_idx < len(sorted_prices) - 1
            else 0
        )
        vol_below = (
            volume_by_price.get(sorted_prices[low_idx - 1], 0)
            if low_idx > 0
            else 0
        )

        if vol_above >= vol_below and high_idx < len(sorted_prices) - 1:
            high_idx += 1
            accumulated += volume_by_price[sorted_prices[high_idx]]
        elif low_idx > 0:
            low_idx -= 1
            accumulated += volume_by_price[sorted_prices[low_idx]]
        else:
            high_idx += 1
            accumulated += volume_by_price[sorted_prices[high_idx]]

    vah = sorted_prices[high_idx]
    val = sorted_prices[low_idx]

    logger.info(
        "Volume Profile: POC=%.2f, VAH=%.2f, VAL=%.2f (tick=%.2f, %d price levels)",
        poc, vah, val, tick_size, len(volume_by_price),
    )

    return VolumeProfileResult(
        poc=poc,
        vah=vah,
        val=val,
        volume_by_price=volume_by_price,
        total_volume=total_volume,
    )
