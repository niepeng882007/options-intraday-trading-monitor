from __future__ import annotations

from dataclasses import dataclass
from datetime import time as dt_time

import numpy as np
import pandas as pd

from src.utils.logger import setup_logger

logger = setup_logger("us_indicators")

US_OPEN = dt_time(9, 30)
US_CLOSE = dt_time(16, 0)


@dataclass
class RvolProfile:
    """Per-symbol adaptive RVOL thresholds derived from historical distribution."""

    gap_and_go_rvol: float      # Percentile-based threshold (e.g. P85)
    trend_day_rvol: float       # Percentile-based threshold (e.g. P60)
    fade_chop_rvol: float       # Percentile-based threshold (e.g. P30)
    avg_daily_range_pct: float  # Average (H-L)/L % across history days
    percentile_rank: float      # Today's RVOL percentile in distribution (0-100)
    sample_size: int            # Number of historical days used


def compute_rvol_profile(
    history_bars: pd.DataFrame,
    today_rvol: float,
    skip_open_minutes: int = 3,
    gap_and_go_pctl: float = 85,
    trend_day_pctl: float = 60,
    fade_chop_pctl: float = 30,
    fallback_gap_and_go: float = 1.5,
    fallback_trend_day: float = 1.2,
    fallback_fade_chop: float = 1.0,
    min_sample_days: int = 5,
    min_trend_day_floor: float = 1.0,
) -> RvolProfile:
    """Compute per-symbol adaptive RVOL thresholds using percentile method.

    For each historical day Di (from day 2 onward), calculates RVOL using
    D1..D(i-1) as the baseline. Collects these RVOL samples, then derives
    percentile-based thresholds.

    Falls back to static thresholds if insufficient data.
    """
    if history_bars.empty:
        return _fallback_profile(
            today_rvol, fallback_gap_and_go, fallback_trend_day, fallback_fade_chop,
        )

    skip_cutoff = dt_time(US_OPEN.hour, US_OPEN.minute + skip_open_minutes)

    # Group bars by date
    hist_dates = sorted(set(history_bars.index.date))
    if len(hist_dates) < min_sample_days + 1:
        return _fallback_profile(
            today_rvol, fallback_gap_and_go, fallback_trend_day, fallback_fade_chop,
            sample_size=len(hist_dates),
        )

    # Collect per-day data: filtered volume and daily range
    daily_data: dict[object, dict] = {}
    for d in hist_dates:
        day_bars = history_bars[history_bars.index.date == d]
        if day_bars.empty:
            continue
        day_times = day_bars.index.time
        filtered = day_bars[day_times >= skip_cutoff]
        vol = float(filtered["Volume"].sum()) if not filtered.empty else 0.0
        high = float(day_bars["High"].max())
        low = float(day_bars["Low"].min())
        daily_range_pct = ((high - low) / low * 100) if low > 0 else 0.0
        daily_data[d] = {"volume": vol, "range_pct": daily_range_pct}

    sorted_dates = sorted(daily_data.keys())
    if len(sorted_dates) < min_sample_days + 1:
        return _fallback_profile(
            today_rvol, fallback_gap_and_go, fallback_trend_day, fallback_fade_chop,
            sample_size=len(sorted_dates),
        )

    # Calculate RVOL for each day using expanding prior-day average
    rvol_samples: list[float] = []
    daily_ranges: list[float] = []
    for i in range(1, len(sorted_dates)):
        current_vol = daily_data[sorted_dates[i]]["volume"]
        if current_vol == 0:
            continue
        prior_vols = [daily_data[sorted_dates[j]]["volume"] for j in range(i)]
        prior_vols = [v for v in prior_vols if v > 0]
        if not prior_vols:
            continue
        avg_prior = np.mean(prior_vols)
        rvol_samples.append(current_vol / avg_prior)
        daily_ranges.append(daily_data[sorted_dates[i]]["range_pct"])

    if len(rvol_samples) < min_sample_days:
        return _fallback_profile(
            today_rvol, fallback_gap_and_go, fallback_trend_day, fallback_fade_chop,
            sample_size=len(rvol_samples),
        )

    # Percentile-based thresholds
    arr = np.array(rvol_samples)
    gap_and_go = float(np.percentile(arr, gap_and_go_pctl))
    trend_day = float(np.percentile(arr, trend_day_pctl))
    fade_chop = float(np.percentile(arr, fade_chop_pctl))

    # Floor: trend_day must be at least min_trend_day_floor (prevents avg volume → TREND_DAY)
    if trend_day < min_trend_day_floor:
        trend_day = min_trend_day_floor
    # Keep fade_chop below trend_day with minimum separation
    if fade_chop >= trend_day - 0.1:
        fade_chop = trend_day - 0.1

    # Guard: ensure minimum separation between tiers
    if gap_and_go < trend_day + 0.1:
        gap_and_go = trend_day + 0.1

    # Today's percentile rank
    pctl_rank = float(np.searchsorted(np.sort(arr), today_rvol) / len(arr) * 100)

    avg_range = float(np.mean(daily_ranges)) if daily_ranges else 0.0

    logger.debug(
        "RVOL profile: samples=%d, P%.0f=%.2f, P%.0f=%.2f, P%.0f=%.2f, "
        "avg_range=%.2f%%, today_rank=%.1f%%",
        len(rvol_samples), gap_and_go_pctl, gap_and_go, trend_day_pctl, trend_day,
        fade_chop_pctl, fade_chop, avg_range, pctl_rank,
    )

    return RvolProfile(
        gap_and_go_rvol=gap_and_go,
        trend_day_rvol=trend_day,
        fade_chop_rvol=fade_chop,
        avg_daily_range_pct=avg_range,
        percentile_rank=pctl_rank,
        sample_size=len(rvol_samples),
    )


def _fallback_profile(
    today_rvol: float,
    gap_and_go: float,
    trend_day: float,
    fade_chop: float,
    sample_size: int = 0,
) -> RvolProfile:
    return RvolProfile(
        gap_and_go_rvol=gap_and_go,
        trend_day_rvol=trend_day,
        fade_chop_rvol=fade_chop,
        avg_daily_range_pct=0.0,
        percentile_rank=0.0,
        sample_size=sample_size,
    )


# Re-export shared VWAP for backward compatibility
from src.common.indicators import calculate_vwap  # noqa: F401, E402


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
