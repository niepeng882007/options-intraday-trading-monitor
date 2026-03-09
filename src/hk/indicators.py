from __future__ import annotations

from datetime import time as dt_time

import numpy as np
import pandas as pd

from src.utils.logger import setup_logger

logger = setup_logger("hk_indicators")

# HK trading sessions
MORNING_OPEN = dt_time(9, 30)
MORNING_CLOSE = dt_time(12, 0)
AFTERNOON_OPEN = dt_time(13, 0)
AFTERNOON_CLOSE = dt_time(16, 0)


def is_trading_time(t: dt_time) -> bool:
    """Check if time is within HK trading hours (excludes lunch break)."""
    return (MORNING_OPEN <= t <= MORNING_CLOSE) or (AFTERNOON_OPEN <= t <= AFTERNOON_CLOSE)


def calculate_vwap(bars: pd.DataFrame) -> float:
    """Calculate VWAP for today's bars, continuous across lunch break.

    VWAP = cumsum(typical_price * volume) / cumsum(volume)
    Lunch break (12:00-13:00) is simply skipped -- no special handling needed
    since there are no bars during lunch.

    Args:
        bars: Today's 1m bars with Open, High, Low, Close, Volume columns

    Returns:
        Current VWAP value, or 0.0 if no data
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
    """Calculate running VWAP series for charting."""
    if bars.empty:
        return pd.Series(dtype=float)

    typical_price = (bars["High"] + bars["Low"] + bars["Close"]) / 3
    cum_vol = bars["Volume"].cumsum()
    cum_tp_vol = (typical_price * bars["Volume"]).cumsum()

    return cum_tp_vol / cum_vol.replace(0, np.nan)


def calculate_rvol(
    today_bars: pd.DataFrame,
    history_bars: pd.DataFrame,
    lookback_days: int = 10,
    session: str = "full",
) -> float:
    """Calculate Relative Volume (RVOL).

    RVOL = today's volume up to current time / average volume for same period over past N days.

    Sessions:
        - "full": entire day (09:30-16:00, excluding lunch)
        - "morning": morning session only (09:30-12:00)
        - "afternoon": afternoon session only (13:00-16:00)

    Args:
        today_bars: Today's 1m bars so far
        history_bars: Historical 1m bars (past N trading days)
        lookback_days: Number of historical days to average
        session: which session to compare

    Returns:
        RVOL ratio (1.0 = average, >1.2 = above average, <0.8 = below average)
    """
    if today_bars.empty or history_bars.empty:
        return 1.0  # Default to neutral

    # Filter by session
    def filter_session(df: pd.DataFrame, sess: str) -> pd.DataFrame:
        if sess == "full":
            return df
        times = df.index.time if hasattr(df.index, "time") else pd.to_datetime(df.index).time
        if sess == "morning":
            mask = [(MORNING_OPEN <= t <= MORNING_CLOSE) for t in times]
        elif sess == "afternoon":
            mask = [(AFTERNOON_OPEN <= t <= AFTERNOON_CLOSE) for t in times]
        else:
            return df
        return df[mask]

    today_filtered = filter_session(today_bars, session)
    today_vol = today_filtered["Volume"].sum()

    if today_vol == 0:
        return 1.0

    # Get current time of day for fair comparison
    today_latest_time = today_filtered.index[-1]
    if hasattr(today_latest_time, "time"):
        cutoff_time = today_latest_time.time()
    else:
        cutoff_time = pd.to_datetime(today_latest_time).time()

    # Group history by date
    hist = filter_session(history_bars, session)
    if hist.empty:
        return 1.0

    hist_dates = (
        hist.index.date if hasattr(hist.index, "date") else pd.to_datetime(hist.index).date
    )
    unique_dates = sorted(set(hist_dates))[-lookback_days:]

    daily_vols: list[float] = []
    for d in unique_dates:
        day_data = (
            hist[hist.index.date == d]
            if hasattr(hist.index, "date")
            else hist[pd.to_datetime(hist.index).date == d]
        )
        # Only count bars up to cutoff_time for fair comparison
        day_times = (
            day_data.index.time
            if hasattr(day_data.index, "time")
            else pd.to_datetime(day_data.index).time
        )
        comparable = day_data[[t <= cutoff_time for t in day_times]]
        if not comparable.empty:
            daily_vols.append(comparable["Volume"].sum())

    if not daily_vols:
        return 1.0

    avg_vol = np.mean(daily_vols)
    if avg_vol == 0:
        return 1.0

    rvol = today_vol / avg_vol
    logger.debug(
        "RVOL (session=%s): today=%d, avg=%d, ratio=%.2f",
        session, today_vol, avg_vol, rvol,
    )
    return float(rvol)


def get_today_bars(bars: pd.DataFrame) -> pd.DataFrame:
    """Extract today's bars from a multi-day DataFrame."""
    if bars.empty:
        return bars
    today = (
        bars.index[-1].date()
        if hasattr(bars.index[-1], "date")
        else pd.to_datetime(bars.index[-1]).date()
    )
    if hasattr(bars.index, "date"):
        return bars[bars.index.date == today]
    return bars[pd.to_datetime(bars.index).date == today]


def get_history_bars(bars: pd.DataFrame) -> pd.DataFrame:
    """Extract historical (non-today) bars from a multi-day DataFrame."""
    if bars.empty:
        return bars
    today = (
        bars.index[-1].date()
        if hasattr(bars.index[-1], "date")
        else pd.to_datetime(bars.index[-1]).date()
    )
    if hasattr(bars.index, "date"):
        return bars[bars.index.date != today]
    return bars[pd.to_datetime(bars.index).date != today]
