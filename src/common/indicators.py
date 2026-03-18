"""Shared indicator functions — used by both HK and US modules."""

from __future__ import annotations

import numpy as np
import pandas as pd


def calculate_vwap(bars: pd.DataFrame) -> float:
    """Calculate VWAP for today's bars.

    VWAP = cumsum(typical_price * volume) / cumsum(volume)
    Continuous across breaks (lunch/halt) since bars simply don't exist during gaps.
    """
    if bars.empty:
        return 0.0

    typical_price = (bars["High"] + bars["Low"] + bars["Close"]) / 3
    cum_vol = bars["Volume"].cumsum()
    cum_tp_vol = (typical_price * bars["Volume"]).cumsum()

    if cum_vol.iloc[-1] == 0:
        return 0.0

    return float(cum_tp_vol.iloc[-1] / cum_vol.iloc[-1])


def calculate_vwap_series(bars: pd.DataFrame) -> pd.Series:
    """Return cumulative VWAP as a time series (same index as *bars*)."""
    if bars.empty:
        return pd.Series(dtype=float)

    typical_price = (bars["High"] + bars["Low"] + bars["Close"]) / 3
    cum_vol = bars["Volume"].cumsum()
    cum_tp_vol = (typical_price * bars["Volume"]).cumsum()

    vwap_s = cum_tp_vol / cum_vol.replace(0, np.nan)
    return vwap_s


def calculate_vwap_slope(bars: pd.DataFrame, lookback: int = 15) -> float:
    """VWAP slope over the last *lookback* bars (%/bar via linear regression).

    Returns 0.0 when insufficient data or VWAP is zero.
    """
    vwap_s = calculate_vwap_series(bars)
    if vwap_s.empty or len(vwap_s) < 2:
        return 0.0

    tail = vwap_s.dropna().iloc[-lookback:]
    if len(tail) < 2:
        return 0.0

    y = tail.values
    x = np.arange(len(y), dtype=float)
    slope = float(np.polyfit(x, y, 1)[0])  # absolute slope per bar

    mean_vwap = float(np.mean(y))
    if mean_vwap == 0:
        return 0.0

    return slope / mean_vwap  # normalise to %/bar


def calculate_vwap_hold_duration(
    bars: pd.DataFrame,
    vwap_series: pd.Series | None = None,
) -> tuple[int, str]:
    """Count consecutive bars on the same side of VWAP from the tail.

    Returns ``(consecutive_bars, side)`` where side is "bullish" / "bearish" / "neutral".
    Reuses *vwap_series* if provided; otherwise computes it.
    """
    if bars is None or bars.empty:
        return 0, "neutral"

    if vwap_series is None:
        vwap_series = calculate_vwap_series(bars)
    if vwap_series.empty or vwap_series.isna().all():
        return 0, "neutral"

    closes = bars["Close"].values
    vwaps = vwap_series.values
    n = len(closes)
    if n == 0:
        return 0, "neutral"

    # Determine side of last bar
    last_close = closes[-1]
    last_vwap = vwaps[-1]
    if np.isnan(last_vwap) or last_close == last_vwap:
        return 0, "neutral"

    side = "bullish" if last_close > last_vwap else "bearish"
    count = 0
    for i in range(n - 1, -1, -1):
        v = vwaps[i]
        if np.isnan(v):
            break
        if side == "bullish" and closes[i] > v:
            count += 1
        elif side == "bearish" and closes[i] < v:
            count += 1
        else:
            break

    return count, side
