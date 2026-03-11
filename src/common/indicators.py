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
