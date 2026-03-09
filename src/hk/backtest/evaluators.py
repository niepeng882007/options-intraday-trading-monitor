"""Core evaluators for HK backtest: level bounce rates and regime accuracy."""

from __future__ import annotations

from datetime import time as dt_time

import pandas as pd

from src.hk import RegimeType, VolumeProfileResult
from src.hk.volume_profile import calculate_volume_profile
from src.hk.indicators import calculate_rvol
from src.hk.regime import classify_regime
from src.hk.backtest import (
    LevelEvent, LevelEvalResult,
    RegimeEvalDay, RegimeEvalResult,
)
from src.utils.logger import setup_logger

logger = setup_logger("hk_backtest_eval")

MORNING_START = dt_time(9, 30)
MORNING_END = dt_time(12, 0)
AFTERNOON_START = dt_time(13, 0)


def _get_session(t: dt_time) -> str:
    """Classify time into morning or afternoon session."""
    if MORNING_START <= t <= MORNING_END:
        return "morning"
    return "afternoon"


def _split_by_date(bars: pd.DataFrame) -> dict:
    """Split bars into per-date DataFrames."""
    result = {}
    for d in sorted(set(bars.index.date)):
        result[d] = bars[bars.index.date == d]
    return result


def evaluate_levels(
    bars_by_symbol: dict[str, pd.DataFrame],
    vp_lookback_days: int = 5,
    bounce_thresholds: list[float] | None = None,
    bounce_window_bars: int = 15,
    value_area_pct: float = 0.70,
    exclude_symbols: set[str] | None = None,
) -> LevelEvalResult:
    """Evaluate VAH/VAL level bounce rates across historical data.

    For each trading day D:
    1. Calculate Volume Profile from D-lookback ~ D-1
    2. Scan D's bars for first VAH/VAL touch (per direction)
    3. Check if price reverses by each threshold within bounce_window_bars

    Args:
        bars_by_symbol: Symbol -> 1m bars DataFrame
        vp_lookback_days: Days of history for VP calculation
        bounce_thresholds: Reversal thresholds to test (default [0.3%, 0.5%, 0.7%, 1.0%])
        bounce_window_bars: Number of bars to check for bounce after touch
        value_area_pct: Value area percentage for VP

    Returns:
        LevelEvalResult with per-threshold bounce rates
    """
    if bounce_thresholds is None:
        bounce_thresholds = [0.003, 0.005, 0.007, 0.010]

    _exclude = exclude_symbols or set()
    all_events: list[LevelEvent] = []

    for symbol, bars in bars_by_symbol.items():
        if symbol in _exclude:
            continue
        daily = _split_by_date(bars)
        dates = sorted(daily.keys())

        for i, target_date in enumerate(dates):
            # Need at least vp_lookback_days of history
            if i < vp_lookback_days:
                continue

            # Collect lookback bars (D-lookback ~ D-1)
            lookback_dates = dates[i - vp_lookback_days:i]
            lookback_bars = pd.concat([daily[d] for d in lookback_dates])

            if lookback_bars.empty or len(lookback_bars) < 10:
                continue

            # Calculate VP from history
            vp = calculate_volume_profile(lookback_bars, value_area_pct=value_area_pct)
            if vp.poc == 0:
                continue

            # Scan target day for VAH/VAL touches
            day_bars = daily[target_date]
            if day_bars.empty:
                continue

            vah_touched = False
            val_touched = False

            for bar_idx in range(len(day_bars)):
                row = day_bars.iloc[bar_idx]
                bar_time = day_bars.index[bar_idx]
                t = bar_time.time() if hasattr(bar_time, "time") else pd.Timestamp(bar_time).time()

                # VAH touch: High >= VAH
                if not vah_touched and row["High"] >= vp.vah:
                    vah_touched = True
                    event = _create_level_event(
                        symbol, bar_time, "VAH", vp.vah, row["High"],
                        bar_idx, day_bars, bounce_window_bars,
                        bounce_thresholds, direction="down", session=_get_session(t),
                    )
                    all_events.append(event)

                # VAL touch: Low <= VAL
                if not val_touched and row["Low"] <= vp.val:
                    val_touched = True
                    event = _create_level_event(
                        symbol, bar_time, "VAL", vp.val, row["Low"],
                        bar_idx, day_bars, bounce_window_bars,
                        bounce_thresholds, direction="up", session=_get_session(t),
                    )
                    all_events.append(event)

                if vah_touched and val_touched:
                    break

    return _aggregate_level_results(all_events, bounce_thresholds)


def _create_level_event(
    symbol: str,
    touch_time,
    level_type: str,
    level_price: float,
    touch_price: float,
    bar_idx: int,
    day_bars: pd.DataFrame,
    window: int,
    thresholds: list[float],
    direction: str,
    session: str,
) -> LevelEvent:
    """Create a LevelEvent by checking bounce within window bars."""
    bounce_results: dict[float, bool] = {}
    max_reversal = 0.0

    # Look forward from touch bar
    end_idx = min(bar_idx + window + 1, len(day_bars))
    future_bars = day_bars.iloc[bar_idx + 1:end_idx]

    for _, frow in future_bars.iterrows():
        if direction == "down":
            # VAH touch → check downward reversal from touch price
            reversal = (touch_price - frow["Low"]) / touch_price
        else:
            # VAL touch → check upward reversal from touch price
            reversal = (frow["High"] - touch_price) / touch_price

        max_reversal = max(max_reversal, reversal)

    for threshold in thresholds:
        bounce_results[threshold] = max_reversal >= threshold

    return LevelEvent(
        date=touch_time,
        symbol=symbol,
        level_type=level_type,
        level_price=level_price,
        touch_price=touch_price,
        touch_bar_idx=bar_idx,
        bounce_results=bounce_results,
        max_reversal_pct=max_reversal * 100,
        session=session,
    )


def _aggregate_level_results(
    events: list[LevelEvent],
    thresholds: list[float],
) -> LevelEvalResult:
    """Aggregate individual level events into summary statistics."""
    by_threshold: dict[float, dict[str, int]] = {}
    by_session: dict[str, dict[float, dict[str, int]]] = {}
    by_symbol: dict[str, dict[float, dict[str, int]]] = {}

    for threshold in thresholds:
        by_threshold[threshold] = {
            "vah_touches": 0, "vah_bounces": 0,
            "val_touches": 0, "val_bounces": 0,
        }

    for event in events:
        for threshold in thresholds:
            hit = event.bounce_results.get(threshold, False)

            # By threshold
            entry = by_threshold[threshold]
            if event.level_type == "VAH":
                entry["vah_touches"] += 1
                if hit:
                    entry["vah_bounces"] += 1
            else:
                entry["val_touches"] += 1
                if hit:
                    entry["val_bounces"] += 1

            # By session
            sess = event.session
            if sess not in by_session:
                by_session[sess] = {}
            if threshold not in by_session[sess]:
                by_session[sess][threshold] = {"touches": 0, "bounces": 0}
            by_session[sess][threshold]["touches"] += 1
            if hit:
                by_session[sess][threshold]["bounces"] += 1

            # By symbol
            sym = event.symbol
            if sym not in by_symbol:
                by_symbol[sym] = {}
            if threshold not in by_symbol[sym]:
                by_symbol[sym][threshold] = {"touches": 0, "bounces": 0}
            by_symbol[sym][threshold]["touches"] += 1
            if hit:
                by_symbol[sym][threshold]["bounces"] += 1

    return LevelEvalResult(
        events=events,
        by_threshold=by_threshold,
        by_session=by_session,
        by_symbol=by_symbol,
    )


def evaluate_regimes(
    bars_by_symbol: dict[str, pd.DataFrame],
    vp_lookback_days: int = 5,
    rvol_lookback_days: int = 10,
    morning_rvol_minutes: int = 5,
    breakout_rvol: float = 1.2,
    range_rvol: float = 0.8,
    exclude_symbols: set[str] | None = None,
) -> RegimeEvalResult:
    """Evaluate regime classification accuracy across historical data.

    For each trading day D:
    1. Calculate VP from D-vp_lookback ~ D-1
    2. Calculate early-morning RVOL from 09:30-09:35 (or configurable)
    3. Classify regime
    4. Check actual day's price action against prediction

    Accuracy criteria:
    - BREAKOUT: close > VAH or close < VAL
    - RANGE: high < VAH and low > VAL
    - WHIPSAW/UNCLEAR: always marked as N/A (no clear accuracy metric)

    Args:
        bars_by_symbol: Symbol -> 1m bars DataFrame
        vp_lookback_days: Days of history for VP calculation
        rvol_lookback_days: Days of history for RVOL calculation
        morning_rvol_minutes: Minutes from open to calculate RVOL (default 5)
        breakout_rvol: RVOL threshold for breakout classification
        range_rvol: RVOL threshold for range classification

    Returns:
        RegimeEvalResult with per-regime accuracy
    """
    _exclude = exclude_symbols or set()
    all_days: list[RegimeEvalDay] = []

    for symbol, bars in bars_by_symbol.items():
        if symbol in _exclude:
            continue
        daily = _split_by_date(bars)
        dates = sorted(daily.keys())

        # Need enough history for both VP and RVOL lookback
        min_history = max(vp_lookback_days, rvol_lookback_days)

        for i, target_date in enumerate(dates):
            if i < min_history:
                continue

            # VP from last vp_lookback_days
            vp_dates = dates[i - vp_lookback_days:i]
            vp_bars = pd.concat([daily[d] for d in vp_dates])
            if vp_bars.empty or len(vp_bars) < 10:
                continue

            vp = calculate_volume_profile(vp_bars)
            if vp.poc == 0:
                continue

            # RVOL from early morning bars
            day_bars = daily[target_date]
            if day_bars.empty:
                continue

            morning_cutoff = dt_time(9, 30 + morning_rvol_minutes)
            early_bars = day_bars[day_bars.index.time <= morning_cutoff]
            if early_bars.empty:
                # Fallback: use first bar
                early_bars = day_bars.iloc[:1]

            # History for RVOL comparison
            rvol_dates = dates[max(0, i - rvol_lookback_days):i]
            rvol_hist_bars = pd.concat([daily[d] for d in rvol_dates])

            rvol = calculate_rvol(
                early_bars, rvol_hist_bars,
                lookback_days=rvol_lookback_days,
            )

            # Opening price for regime classification
            opening_price = float(day_bars.iloc[0]["Open"])

            # Classify regime
            regime_result = classify_regime(
                price=opening_price,
                rvol=rvol,
                vp=vp,
                breakout_rvol=breakout_rvol,
                range_rvol=range_rvol,
            )

            # Actual day statistics
            day_open = float(day_bars.iloc[0]["Open"])
            day_high = float(day_bars["High"].max())
            day_low = float(day_bars["Low"].min())
            day_close = float(day_bars.iloc[-1]["Close"])

            # Check accuracy
            accurate, details = _check_regime_accuracy(
                regime_result.regime, day_high, day_low, day_close, vp,
            )

            eval_day = RegimeEvalDay(
                date=target_date,
                symbol=symbol,
                predicted=regime_result.regime,
                confidence=regime_result.confidence,
                rvol=rvol,
                vah=vp.vah,
                val=vp.val,
                poc=vp.poc,
                day_open=day_open,
                day_high=day_high,
                day_low=day_low,
                day_close=day_close,
                accurate=accurate,
                details=details,
            )
            all_days.append(eval_day)

    return _aggregate_regime_results(all_days)


def _check_regime_accuracy(
    regime: RegimeType,
    day_high: float,
    day_low: float,
    day_close: float,
    vp: VolumeProfileResult,
) -> tuple[bool, str]:
    """Check if regime prediction matches actual price action."""
    if regime == RegimeType.BREAKOUT:
        # Accurate if close finished outside value area
        if day_close > vp.vah:
            return True, f"Close {day_close:.0f} above VAH {vp.vah:.0f}"
        if day_close < vp.val:
            return True, f"Close {day_close:.0f} below VAL {vp.val:.0f}"
        return False, f"Close {day_close:.0f} stayed in value area"

    if regime == RegimeType.RANGE:
        # Accurate if high stayed below VAH and low stayed above VAL
        if day_high < vp.vah and day_low > vp.val:
            return True, f"Range [{day_low:.0f}-{day_high:.0f}] within VA"
        return False, f"Breached VA: high={day_high:.0f} VAH={vp.vah:.0f}, low={day_low:.0f} VAL={vp.val:.0f}"

    # WHIPSAW and UNCLEAR — mark as N/A (not scored)
    return False, "N/A (no accuracy metric for this regime type)"


def _aggregate_regime_results(days: list[RegimeEvalDay]) -> RegimeEvalResult:
    """Aggregate regime evaluations into summary statistics."""
    by_regime: dict[str, dict[str, int]] = {}
    by_symbol: dict[str, dict[str, int]] = {}

    for day in days:
        regime_key = day.predicted.value

        if regime_key not in by_regime:
            by_regime[regime_key] = {"total": 0, "accurate": 0}
        by_regime[regime_key]["total"] += 1
        if day.accurate:
            by_regime[regime_key]["accurate"] += 1

        if day.symbol not in by_symbol:
            by_symbol[day.symbol] = {"total": 0, "accurate": 0}
        by_symbol[day.symbol]["total"] += 1
        if day.accurate:
            by_symbol[day.symbol]["accurate"] += 1

    return RegimeEvalResult(days=days, by_regime=by_regime, by_symbol=by_symbol)
