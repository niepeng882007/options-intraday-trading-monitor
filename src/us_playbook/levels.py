from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src.common.indicators import calculate_vwap_series
from src.common.types import GammaWallResult, VolumeProfileResult
from src.common.volume_profile import calculate_volume_profile
from src.us_playbook import KeyLevels
from src.utils.logger import setup_logger

logger = setup_logger("us_levels")


# ── Intraday (wide VA) levels ──


@dataclass
class IntradayLevels:
    """Effective intraday trigger levels when 5-day VA is too wide for ADR."""
    is_wide_va: bool
    va_adr_ratio: float              # VA width / ADR ratio
    effective_upper: float           # short entry reference (intraday VAH or VWAP+σ)
    effective_lower: float           # long entry reference (intraday VAL or VWAP-σ)
    effective_mid: float             # TP/pivot (intraday POC or VWAP)
    source: str                      # "intraday_vp" | "vwap_bands"

    @property
    def source_label(self) -> str:
        return "日内发展中 VP" if self.source == "intraday_vp" else "VWAP ± 1σ"


def detect_wide_va(
    vp: VolumeProfileResult,
    avg_daily_range_pct: float,
    threshold: float = 1.8,
) -> tuple[bool, float]:
    """Detect if 5-day VA is too wide relative to ADR.

    Returns (is_wide, ratio).
    """
    if vp.vah <= vp.val:
        return False, 0.0
    if avg_daily_range_pct <= 0:
        return False, 0.0
    midpoint = (vp.vah + vp.val) / 2
    if midpoint <= 0:
        return False, 0.0
    va_width_pct = (vp.vah - vp.val) / midpoint * 100
    ratio = va_width_pct / avg_daily_range_pct
    return ratio > threshold, ratio


def compute_intraday_vp(
    today_bars: pd.DataFrame,
    value_area_pct: float = 0.70,
    min_bars: int = 120,
) -> VolumeProfileResult | None:
    """Compute developing VP from today's bars only.

    Returns None if < min_bars (need ~2h of data).
    """
    if today_bars.empty or len(today_bars) < min_bars:
        return None
    avg_price = today_bars["Close"].mean()
    tick = us_tick_size(avg_price) / 2  # finer granularity for intraday
    return calculate_volume_profile(
        today_bars, value_area_pct=value_area_pct, tick_size=tick,
    )


def compute_vwap_bands(
    today_bars: pd.DataFrame,
    num_std: float = 1.0,
    min_bars: int = 15,
    min_band_width_pct: float = 0.002,
) -> tuple[float, float, float] | None:
    """Compute VWAP ± N*σ bands from today's bars.

    Returns (vwap, upper, lower) or None if insufficient data or bands too narrow.
    """
    if today_bars.empty or len(today_bars) < min_bars:
        return None
    vwap_series = calculate_vwap_series(today_bars)
    if vwap_series.empty:
        return None
    vwap = float(vwap_series.iloc[-1])
    if vwap <= 0:
        return None

    typical_price = (today_bars["High"] + today_bars["Low"] + today_bars["Close"]) / 3
    deviation = typical_price - vwap_series
    std = float(deviation.std())
    if std <= 0:
        return None

    upper = vwap + std * num_std
    lower = vwap - std * num_std

    # Min width gate
    if (upper - lower) / vwap < min_band_width_pct:
        return None

    return vwap, upper, lower


def build_intraday_levels(
    today_bars: pd.DataFrame,
    vp_5d: VolumeProfileResult,
    avg_daily_range_pct: float,
    vwap: float,
    cfg: dict,
) -> IntradayLevels | None:
    """Build intraday levels when 5-day VA is wide relative to ADR.

    Returns None if VA is not wide or insufficient intraday data.
    """
    threshold = cfg.get("threshold", 1.8)
    is_wide, ratio = detect_wide_va(vp_5d, avg_daily_range_pct, threshold)
    if not is_wide:
        return None

    # Priority 1: intraday VP (need ≥120 bars)
    ivp_min_bars = cfg.get("intraday_vp_min_bars", 120)
    ivp_va_pct = cfg.get("intraday_vp_value_area_pct", 0.70)
    ivp = compute_intraday_vp(today_bars, value_area_pct=ivp_va_pct, min_bars=ivp_min_bars)
    if ivp is not None:
        return IntradayLevels(
            is_wide_va=True,
            va_adr_ratio=ratio,
            effective_upper=ivp.vah,
            effective_lower=ivp.val,
            effective_mid=ivp.poc,
            source="intraday_vp",
        )

    # Priority 2: VWAP bands (need ≥15 bars)
    vwap_min_bars = cfg.get("vwap_bands_min_bars", 15)
    vwap_std = cfg.get("vwap_band_std", 1.0)
    min_bw = cfg.get("min_band_width_pct", 0.002)
    bands = compute_vwap_bands(today_bars, num_std=vwap_std, min_bars=vwap_min_bars, min_band_width_pct=min_bw)
    if bands is not None:
        vwap_val, upper, lower = bands
        return IntradayLevels(
            is_wide_va=True,
            va_adr_ratio=ratio,
            effective_upper=upper,
            effective_lower=lower,
            effective_mid=vwap,  # use kl.vwap for consistency
            source="vwap_bands",
        )

    return None


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

    pdh = float(prev_bars["High"].max())
    pdl = float(prev_bars["Low"].min())
    logger.debug(
        "extract_previous_day_hl: using date %s (of %d dates), PDH=%.2f, PDL=%.2f",
        prev_date, len(dates), pdh, pdl,
    )
    return pdh, pdl


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
    recency_decay: float = 0.0,
) -> VolumeProfileResult:
    """Compute VP using shared HK function with US tick_size."""
    if history_bars.empty:
        return VolumeProfileResult(poc=0, vah=0, val=0)

    avg_price = history_bars["Close"].mean()
    tick = us_tick_size(avg_price)
    result = calculate_volume_profile(
        history_bars, value_area_pct=value_area_pct, tick_size=tick,
        recency_decay=recency_decay,
    )

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
