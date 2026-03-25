"""Shared level-extraction utilities — used by US Playbook and Index Trader."""

from __future__ import annotations

import pandas as pd

from src.utils.logger import setup_logger

logger = setup_logger("common_levels")


def extract_previous_day_hl(bars: pd.DataFrame) -> tuple[float, float]:
    """Extract previous day's regular hours High/Low from 1m bars.

    Returns (pdh, pdl). Falls back to (0.0, 0.0) if insufficient data.
    """
    if bars.empty:
        return 0.0, 0.0

    dates = sorted(set(bars.index.date))
    if len(dates) < 2:
        # Only one day of data — use it as "previous"
        if dates:
            day_bars = bars[bars.index.date == dates[0]]
            return float(day_bars["High"].max()), float(day_bars["Low"].min())
        return 0.0, 0.0

    prev_date = dates[-2]  # second-to-last date
    prev_bars = bars[bars.index.date == prev_date]
    if prev_bars.empty:
        return 0.0, 0.0

    pdh = float(prev_bars["High"].max())
    pdl = float(prev_bars["Low"].min())
    logger.debug(
        "extract_previous_day_hl: using date %s (of %d dates), PDH=%.2f, PDL=%.2f",
        prev_date, len(dates), pdh, pdl,
    )
    return pdh, pdl
