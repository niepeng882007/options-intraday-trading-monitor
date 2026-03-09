from __future__ import annotations

from datetime import time as dt_time

import numpy as np
import pandas as pd

from src.utils.logger import setup_logger

logger = setup_logger("us_indicators")

US_OPEN = dt_time(9, 30)
US_CLOSE = dt_time(16, 0)


def calculate_vwap(bars: pd.DataFrame) -> float:
    """Calculate VWAP for today's bars.

    VWAP = cumsum(typical_price * volume) / cumsum(volume)
    """
    if bars.empty:
        return 0.0

    typical = (bars["High"] + bars["Low"] + bars["Close"]) / 3
    cum_vol = bars["Volume"].cumsum()
    cum_tp_vol = (typical * bars["Volume"]).cumsum()

    if cum_vol.iloc[-1] == 0:
        return 0.0

    return float(cum_tp_vol.iloc[-1] / cum_vol.iloc[-1])


def calculate_us_rvol(
    today_bars: pd.DataFrame,
    history_bars: pd.DataFrame,
    window_minutes: int = 15,
    lookback_days: int = 10,
) -> float:
    """Calculate RVOL for the first N minutes of trading.

    Compares today's volume in the window (09:30 + window_minutes) to the
    average volume for the same window over the past lookback_days.

    Returns 1.0 (neutral) if insufficient data.
    """
    if today_bars.empty or history_bars.empty:
        return 1.0

    # Today: filter bars within window
    first_bar_time = today_bars.index[0]
    cutoff = first_bar_time + pd.Timedelta(minutes=window_minutes)
    today_window = today_bars[today_bars.index < cutoff]
    today_vol = today_window["Volume"].sum()

    if today_vol == 0:
        return 1.0

    # History: for each day, get same-window volume
    hist_dates = history_bars.index.date
    unique_dates = sorted(set(hist_dates))[-lookback_days:]

    daily_vols: list[float] = []
    for d in unique_dates:
        day_data = history_bars[history_bars.index.date == d]
        if day_data.empty:
            continue
        day_first = day_data.index[0]
        day_cutoff = day_first + pd.Timedelta(minutes=window_minutes)
        day_window = day_data[day_data.index < day_cutoff]
        if not day_window.empty:
            daily_vols.append(day_window["Volume"].sum())

    if not daily_vols:
        return 1.0

    avg_vol = np.mean(daily_vols)
    if avg_vol == 0:
        return 1.0

    rvol = today_vol / avg_vol
    logger.debug(
        "US RVOL (window=%dmin): today=%d, avg=%d, ratio=%.2f",
        window_minutes, today_vol, avg_vol, rvol,
    )
    return float(rvol)
