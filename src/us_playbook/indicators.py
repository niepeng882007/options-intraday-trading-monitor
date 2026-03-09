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
    skip_open_minutes: int = 3,
    lookback_days: int = 10,
) -> float:
    """Calculate RVOL using expanding window with open-rotation skip.

    Skips the first ``skip_open_minutes`` after 09:30 (auction/rotation noise),
    then compares today's volume from skip_cutoff to the latest bar against the
    same time-of-day window in historical days.  This gives a fair apples-to-
    apples comparison regardless of when the function is called.

    Returns 1.0 (neutral) if insufficient data.
    """
    if today_bars.empty or history_bars.empty:
        return 1.0

    # skip_cutoff: first bar time we consider valid
    skip_cutoff = dt_time(US_OPEN.hour, US_OPEN.minute + skip_open_minutes)

    # Today: bars from skip_cutoff onward
    today_times = today_bars.index.time
    today_window = today_bars[today_times >= skip_cutoff]
    if today_window.empty:
        return 1.0

    today_vol = today_window["Volume"].sum()
    if today_vol == 0:
        return 1.0

    # cutoff_time: latest bar time in today's window (for symmetric history cut)
    cutoff_time = today_window.index[-1].time()

    # History: for each day, filter skip_cutoff <= bar.time <= cutoff_time
    hist_dates = history_bars.index.date
    unique_dates = sorted(set(hist_dates))[-lookback_days:]

    daily_vols: list[float] = []
    for d in unique_dates:
        day_data = history_bars[history_bars.index.date == d]
        if day_data.empty:
            continue
        day_times = day_data.index.time
        day_window = day_data[(day_times >= skip_cutoff) & (day_times <= cutoff_time)]
        if not day_window.empty:
            daily_vols.append(day_window["Volume"].sum())

    if not daily_vols:
        return 1.0

    avg_vol = np.mean(daily_vols)
    if avg_vol == 0:
        return 1.0

    rvol = today_vol / avg_vol
    logger.debug(
        "US RVOL (skip=%dmin, cutoff=%s): today=%d, avg=%d, ratio=%.2f",
        skip_open_minutes, cutoff_time, today_vol, avg_vol, rvol,
    )
    return float(rvol)
