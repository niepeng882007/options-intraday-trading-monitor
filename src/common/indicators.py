"""Shared indicator functions — used by both HK and US modules."""

from __future__ import annotations

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
