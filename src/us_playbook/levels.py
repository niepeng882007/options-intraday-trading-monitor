from __future__ import annotations

import pandas as pd

from src.hk import GammaWallResult, VolumeProfileResult
from src.hk.volume_profile import calculate_volume_profile
from src.us_playbook import KeyLevels
from src.utils.logger import setup_logger

logger = setup_logger("us_levels")


def us_tick_size(avg_price: float) -> float:
    """US-specific VP price bucket granularity."""
    if avg_price > 400:
        return 0.50    # SPY ~$550
    if avg_price > 100:
        return 0.25    # AAPL ~$230, NVDA ~$140
    if avg_price > 20:
        return 0.10    # AMD ~$110
    return 0.05


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

    return float(prev_bars["High"].max()), float(prev_bars["Low"].min())


def get_today_bars(bars: pd.DataFrame) -> pd.DataFrame:
    """Extract today's bars (America/New_York timezone)."""
    if bars.empty:
        return bars
    today = bars.index[-1].date()
    return bars[bars.index.date == today]


def get_history_bars(bars: pd.DataFrame, max_trading_days: int = 0) -> pd.DataFrame:
    """Extract non-today bars. If max_trading_days > 0, keep only most recent N trading days."""
    if bars.empty:
        return bars
    today = bars.index[-1].date()
    history = bars[bars.index.date != today]
    if max_trading_days > 0 and not history.empty:
        trading_dates = sorted(set(history.index.date))
        if len(trading_dates) > max_trading_days:
            cutoff = trading_dates[-max_trading_days]
            history = history[history.index.date >= cutoff]
    return history


def compute_volume_profile(
    history_bars: pd.DataFrame,
    value_area_pct: float = 0.70,
) -> VolumeProfileResult:
    """Compute VP using shared HK function with US tick_size."""
    if history_bars.empty:
        return VolumeProfileResult(poc=0, vah=0, val=0)

    avg_price = history_bars["Close"].mean()
    tick = us_tick_size(avg_price)
    result = calculate_volume_profile(history_bars, value_area_pct=value_area_pct, tick_size=tick)

    # Populate trading_days from actual bar data
    if not history_bars.empty:
        result.trading_days = len(set(history_bars.index.date))

    return result


def calc_fetch_calendar_days(vp_trading_days: int, rvol_lookback_days: int) -> int:
    """Calculate calendar days to fetch from Futu to cover both VP and RVOL needs.

    Uses generous buffer (target * 2 + 2) to handle weekends + holidays.
    """
    target = max(vp_trading_days, rvol_lookback_days)
    return target * 2 + 2


def build_key_levels(
    vp: VolumeProfileResult,
    pdh: float,
    pdl: float,
    pmh: float,
    pml: float,
    vwap: float,
    gamma: GammaWallResult | None = None,
    pm_source: str = "futu",
) -> KeyLevels:
    """Assemble all key levels into a single object."""
    kl = KeyLevels(
        poc=vp.poc,
        vah=vp.vah,
        val=vp.val,
        pdh=pdh,
        pdl=pdl,
        pmh=pmh,
        pml=pml,
        vwap=vwap,
        pm_source=pm_source,
    )
    if gamma:
        kl.gamma_call_wall = gamma.call_wall_strike
        kl.gamma_put_wall = gamma.put_wall_strike
        kl.gamma_max_pain = gamma.max_pain
    return kl
