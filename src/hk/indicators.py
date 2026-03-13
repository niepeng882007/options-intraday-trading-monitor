from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta, timezone

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


# Re-export shared VWAP for backward compatibility
from src.common.indicators import calculate_vwap  # noqa: F401, E402


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


@dataclass
class PulseEvent:
    """Represents a volume pulse (sudden surge) with price displacement."""
    peak_ratio: float           # max bar volume / expanding median
    displacement_pct: float     # price move % during pulse context window
    surge_bar_count: int        # number of bars exceeding threshold
    direction: str              # "bullish" / "bearish"


def detect_volume_pulse(
    today_bars: pd.DataFrame,
    multiplier: float = 2.5,
    context_bars: int = 2,
    min_surge_bars: int = 1,
) -> PulseEvent | None:
    """Detect a volume pulse event in today's bars.

    Uses expanding window median as baseline. Scans for bars where
    volume >= median * multiplier, then calculates price displacement
    over the surge + context window.

    Returns PulseEvent if detected, None otherwise.
    """
    if today_bars.empty or len(today_bars) < 10:
        return None

    volumes = today_bars["Volume"].values
    closes = today_bars["Close"].values

    # Expanding median baseline (cumulative median up to each bar)
    surge_indices: list[int] = []
    peak_ratio = 0.0
    for i in range(5, len(volumes)):
        median_so_far = float(np.median(volumes[:i]))
        if median_so_far <= 0:
            continue
        ratio = volumes[i] / median_so_far
        if ratio >= multiplier:
            surge_indices.append(i)
            peak_ratio = max(peak_ratio, ratio)

    if len(surge_indices) < min_surge_bars:
        return None

    # Context window: from (first_surge - context_bars) to (last_surge + context_bars)
    ctx_start = max(0, surge_indices[0] - context_bars)
    ctx_end = min(len(closes) - 1, surge_indices[-1] + context_bars)

    price_start = float(closes[ctx_start])
    price_end = float(closes[ctx_end])
    if price_start <= 0:
        return None

    displacement_pct = abs(price_end - price_start) / price_start * 100
    direction = "bullish" if price_end > price_start else "bearish"

    return PulseEvent(
        peak_ratio=peak_ratio,
        displacement_pct=displacement_pct,
        surge_bar_count=len(surge_indices),
        direction=direction,
    )


def calculate_peak_session_rvol(
    today_bars: pd.DataFrame,
    history_bars: pd.DataFrame,
    lookback_days: int = 10,
    min_session_bars: int = 15,
) -> float:
    """Calculate peak session RVOL = max(morning_rvol, afternoon_rvol).

    Filters out history days with < 150 bars (anomalous/partial).
    Sessions with < min_session_bars today bars are excluded from max.
    Falls back to 1.0 when data is insufficient.
    """
    if today_bars.empty or history_bars.empty:
        return 1.0

    # Filter anomalous history days (< 150 bars)
    hist_dates = (
        history_bars.index.date
        if hasattr(history_bars.index, "date")
        else pd.to_datetime(history_bars.index).date
    )
    unique_dates = sorted(set(hist_dates))
    valid_dates: list = []
    for d in unique_dates:
        day_data = (
            history_bars[history_bars.index.date == d]
            if hasattr(history_bars.index, "date")
            else history_bars[pd.to_datetime(history_bars.index).date == d]
        )
        if len(day_data) >= 150:
            valid_dates.append(d)

    if not valid_dates:
        return 1.0

    # Rebuild filtered history
    valid_set = set(valid_dates)
    if hasattr(history_bars.index, "date"):
        mask = [d in valid_set for d in history_bars.index.date]
    else:
        mask = [d in valid_set for d in pd.to_datetime(history_bars.index).date]
    filtered_hist = history_bars[mask]

    # Count today's session bars
    def _count_session_bars(df: pd.DataFrame, session: str) -> int:
        times = df.index.time if hasattr(df.index, "time") else pd.to_datetime(df.index).time
        if session == "morning":
            return sum(1 for t in times if MORNING_OPEN <= t <= MORNING_CLOSE)
        return sum(1 for t in times if AFTERNOON_OPEN <= t <= AFTERNOON_CLOSE)

    valid_rvols: list[float] = []

    for sess in ("morning", "afternoon"):
        if _count_session_bars(today_bars, sess) < min_session_bars:
            continue
        sess_rvol = calculate_rvol(
            today_bars, filtered_hist,
            lookback_days=lookback_days,
            session=sess,
        )
        valid_rvols.append(sess_rvol)

    return max(valid_rvols) if valid_rvols else 1.0


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


def get_history_bars(bars: pd.DataFrame, max_trading_days: int = 0) -> pd.DataFrame:
    """Extract historical (non-today) bars from a multi-day DataFrame.

    If max_trading_days > 0, keep only the most recent N trading days.
    """
    if bars.empty:
        return bars
    today = (
        bars.index[-1].date()
        if hasattr(bars.index[-1], "date")
        else pd.to_datetime(bars.index[-1]).date()
    )
    if hasattr(bars.index, "date"):
        history = bars[bars.index.date != today]
    else:
        history = bars[pd.to_datetime(bars.index).date != today]

    if max_trading_days > 0 and not history.empty:
        trading_dates = sorted(set(
            history.index.date if hasattr(history.index, "date")
            else pd.to_datetime(history.index).date
        ))
        if len(trading_dates) > max_trading_days:
            cutoff = trading_dates[-max_trading_days]
            if hasattr(history.index, "date"):
                history = history[history.index.date >= cutoff]
            else:
                history = history[pd.to_datetime(history.index).date >= cutoff]

    return history


# ── HK session constants ──

HKT = timezone(timedelta(hours=8))
_TOTAL_SESSION_MINUTES = 330  # 09:30-12:00 (150) + 13:00-16:00 (180)


def calculate_initial_balance(
    today_bars: pd.DataFrame,
    window_minutes: int = 30,
) -> tuple[float, float]:
    """Calculate Initial Balance (IBH/IBL) from first N minutes of trading.

    Returns (ibh, ibl). Returns (0.0, 0.0) if insufficient data.
    """
    if today_bars.empty:
        return 0.0, 0.0

    open_time = dt_time(9, 30)
    end_minute = 30 + window_minutes  # minutes past 9:00
    end_time = dt_time(9 + end_minute // 60, end_minute % 60)

    times = (
        today_bars.index.time
        if hasattr(today_bars.index, "time")
        else pd.to_datetime(today_bars.index).time
    )
    mask = [(open_time <= t <= end_time) for t in times]
    ib_bars = today_bars[mask]

    if ib_bars.empty:
        return 0.0, 0.0

    ibh = float(ib_bars["High"].max())
    ibl = float(ib_bars["Low"].min())
    return ibh, ibl


def minutes_to_close_hk(now: datetime | None = None) -> int:
    """Calculate remaining trading minutes in HK session, excluding lunch break.

    09:30-12:00 (150 min) + 13:00-16:00 (180 min) = 330 total.
    """
    if now is None:
        now = datetime.now(HKT)

    h, m = now.hour, now.minute
    current = h * 60 + m

    morning_open = 9 * 60 + 30   # 09:30
    morning_close = 12 * 60       # 12:00
    afternoon_open = 13 * 60      # 13:00
    afternoon_close = 16 * 60     # 16:00

    if current < morning_open:
        # Before market open — full day remaining
        return _TOTAL_SESSION_MINUTES
    elif current <= morning_close:
        # During morning session
        morning_left = morning_close - current
        return morning_left + 180  # + full afternoon
    elif current < afternoon_open:
        # Lunch break — only afternoon remaining
        return 180
    elif current <= afternoon_close:
        # During afternoon session
        return afternoon_close - current
    else:
        # After market close
        return 0


def calculate_avg_daily_range(
    hist_bars: pd.DataFrame,
    lookback_days: int = 10,
) -> float:
    """Calculate average daily range percentage over past N trading days.

    Per day: (High.max() - Low.min()) / Close.last(), then take mean.
    Used for PlanContext.avg_daily_range_pct to estimate reachable range.
    """
    if hist_bars.empty:
        return 0.0

    dates = (
        hist_bars.index.date
        if hasattr(hist_bars.index, "date")
        else pd.to_datetime(hist_bars.index).date
    )
    unique_dates = sorted(set(dates))[-lookback_days:]

    ranges: list[float] = []
    for d in unique_dates:
        if hasattr(hist_bars.index, "date"):
            day_data = hist_bars[hist_bars.index.date == d]
        else:
            day_data = hist_bars[pd.to_datetime(hist_bars.index).date == d]
        if day_data.empty:
            continue
        day_high = float(day_data["High"].max())
        day_low = float(day_data["Low"].min())
        day_close = float(day_data["Close"].iloc[-1])
        if day_close > 0:
            ranges.append((day_high - day_low) / day_close * 100)

    return float(np.mean(ranges)) if ranges else 0.0


def build_hk_key_levels(
    vp, vwap: float, pdh: float, pdl: float, pdc: float,
    day_open: float, ibh: float, ibl: float, gamma_wall=None,
):
    """Assemble HKKeyLevels from individual components."""
    from src.hk import HKKeyLevels
    return HKKeyLevels(
        poc=vp.poc,
        vah=vp.vah,
        val=vp.val,
        pdh=pdh,
        pdl=pdl,
        pdc=pdc,
        ibh=ibh,
        ibl=ibl,
        day_open=day_open,
        vwap=vwap,
        gamma_call_wall=gamma_wall.call_wall_strike if gamma_wall else 0.0,
        gamma_put_wall=gamma_wall.put_wall_strike if gamma_wall else 0.0,
        gamma_max_pain=gamma_wall.max_pain if gamma_wall else 0.0,
    )


def hk_key_levels_to_dict(kl) -> dict[str, float]:
    """Convert HKKeyLevels to dict[str, float], filtering zero values."""
    mapping = {
        "POC": kl.poc,
        "VAH": kl.vah,
        "VAL": kl.val,
        "PDH": kl.pdh,
        "PDL": kl.pdl,
        "PDC": kl.pdc,
        "IBH": kl.ibh,
        "IBL": kl.ibl,
        "Open": kl.day_open,
        "VWAP": kl.vwap,
        "Call Wall": kl.gamma_call_wall,
        "Put Wall": kl.gamma_put_wall,
        "Max Pain": kl.gamma_max_pain,
    }
    return {k: v for k, v in mapping.items() if v > 0}
