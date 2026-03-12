"""Core evaluators for US backtest: level bounce rates and regime accuracy.

All regime classification parameters strictly mirror src/us_playbook/main.py.
"""

from __future__ import annotations

from datetime import time as dt_time

import pandas as pd

from src.common.types import VolumeProfileResult
from src.us_playbook import USRegimeType
from src.us_playbook.indicators import calculate_us_rvol, compute_rvol_profile
from src.us_playbook.levels import compute_volume_profile
from src.us_playbook.regime import classify_us_regime
from src.us_playbook.backtest import (
    LevelEvent, LevelEvalResult,
    RegimeEvalDay, RegimeEvalResult,
)
from src.utils.logger import setup_logger

logger = setup_logger("us_backtest_eval")

# US regular hours — no lunch break
US_OPEN = dt_time(9, 30)


def _split_by_date(bars: pd.DataFrame) -> dict:
    """Split bars into per-date DataFrames."""
    result = {}
    for d in sorted(set(bars.index.date)):
        result[d] = bars[bars.index.date == d]
    return result


def _prev_day_hl(daily: dict, dates: list, idx: int) -> tuple[float, float]:
    """Explicit helper: get PDH/PDL from dates[idx-1] (D2 design decision)."""
    if idx < 1:
        return 0.0, 0.0
    prev_bars = daily[dates[idx - 1]]
    return float(prev_bars["High"].max()), float(prev_bars["Low"].min())


# ── Section 1: Level Evaluation ──


def evaluate_levels(
    bars_by_symbol: dict[str, pd.DataFrame],
    vp_lookback_days: int = 5,
    bounce_thresholds: list[float] | None = None,
    bounce_window_bars: int = 15,
    value_area_pct: float = 0.70,
    exclude_symbols: set[str] | None = None,
    recency_decay: float = 0.15,
) -> LevelEvalResult:
    """Evaluate VAH/VAL/PDH/PDL level bounce rates across historical data.

    For each trading day D:
    1. Calculate Volume Profile from D-lookback ~ D-1
    2. Get PDH/PDL from D-1
    3. Scan D's bars for first VAH/VAL/PDH/PDL touch
    4. Check if price reverses by each threshold within bounce_window_bars
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
            if i < vp_lookback_days:
                continue

            # VP from D-lookback ~ D-1
            lookback_dates = dates[i - vp_lookback_days:i]
            lookback_bars = pd.concat([daily[d] for d in lookback_dates])

            if lookback_bars.empty or len(lookback_bars) < 10:
                continue

            vp = compute_volume_profile(lookback_bars, value_area_pct=value_area_pct, recency_decay=recency_decay)
            if vp.poc == 0:
                continue

            # PDH/PDL from D-1
            pdh, pdl = _prev_day_hl(daily, dates, i)

            # Scan target day
            day_bars = daily[target_date]
            if day_bars.empty:
                continue

            vah_touched = False
            val_touched = False
            pdh_touched = False
            pdl_touched = False

            for bar_idx in range(len(day_bars)):
                row = day_bars.iloc[bar_idx]

                # VAH touch: High >= VAH
                if not vah_touched and row["High"] >= vp.vah:
                    vah_touched = True
                    event = _create_level_event(
                        symbol, day_bars.index[bar_idx], "VAH", vp.vah, row["High"],
                        bar_idx, day_bars, bounce_window_bars,
                        bounce_thresholds, direction="down",
                    )
                    all_events.append(event)

                # VAL touch: Low <= VAL
                if not val_touched and row["Low"] <= vp.val:
                    val_touched = True
                    event = _create_level_event(
                        symbol, day_bars.index[bar_idx], "VAL", vp.val, row["Low"],
                        bar_idx, day_bars, bounce_window_bars,
                        bounce_thresholds, direction="up",
                    )
                    all_events.append(event)

                # PDH touch: High >= PDH (rejection expected → down)
                if not pdh_touched and pdh > 0 and row["High"] >= pdh:
                    pdh_touched = True
                    event = _create_level_event(
                        symbol, day_bars.index[bar_idx], "PDH", pdh, row["High"],
                        bar_idx, day_bars, bounce_window_bars,
                        bounce_thresholds, direction="down",
                    )
                    all_events.append(event)

                # PDL touch: Low <= PDL (rejection expected → up)
                if not pdl_touched and pdl > 0 and row["Low"] <= pdl:
                    pdl_touched = True
                    event = _create_level_event(
                        symbol, day_bars.index[bar_idx], "PDL", pdl, row["Low"],
                        bar_idx, day_bars, bounce_window_bars,
                        bounce_thresholds, direction="up",
                    )
                    all_events.append(event)

                if vah_touched and val_touched and pdh_touched and pdl_touched:
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
) -> LevelEvent:
    """Create a LevelEvent by checking bounce within window bars."""
    bounce_results: dict[float, bool] = {}
    max_reversal = 0.0

    end_idx = min(bar_idx + window + 1, len(day_bars))
    future_bars = day_bars.iloc[bar_idx + 1:end_idx]

    for _, frow in future_bars.iterrows():
        if direction == "down":
            reversal = (touch_price - frow["Low"]) / touch_price
        else:
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
    )


def _aggregate_level_results(
    events: list[LevelEvent],
    thresholds: list[float],
) -> LevelEvalResult:
    """Aggregate individual level events into summary statistics."""
    level_keys = ["vah", "val", "pdh", "pdl"]
    by_threshold: dict[float, dict[str, int]] = {}
    by_symbol: dict[str, dict[float, dict[str, int]]] = {}

    for threshold in thresholds:
        entry: dict[str, int] = {}
        for lk in level_keys:
            entry[f"{lk}_t"] = 0
            entry[f"{lk}_b"] = 0
        by_threshold[threshold] = entry

    for event in events:
        lk = event.level_type.lower()  # "vah", "val", "pdh", "pdl"
        for threshold in thresholds:
            hit = event.bounce_results.get(threshold, False)

            # By threshold
            by_threshold[threshold][f"{lk}_t"] += 1
            if hit:
                by_threshold[threshold][f"{lk}_b"] += 1

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
        by_symbol=by_symbol,
    )


# ── Section 2: Regime Evaluation ──


def evaluate_regimes(
    bars_by_symbol: dict[str, pd.DataFrame],
    vp_lookback_days: int = 5,
    rvol_lookback_days: int = 10,
    skip_open_minutes: int = 3,
    eval_minutes: int = 8,
    value_area_pct: float = 0.70,
    regime_cfg: dict | None = None,
    exclude_symbols: set[str] | None = None,
    no_adaptive: bool = False,
    recency_decay: float = 0.15,
) -> RegimeEvalResult:
    """Evaluate regime classification accuracy across historical data.

    For each trading day D:
    1. Calculate VP from D-vp_lookback ~ D-1
    2. Calculate RVOL from 09:33-09:38 (D1c)
    3. Compute adaptive RVOL profile (D1d)
    4. PM estimate: pmh=max(open, prev_close), pml=min(open, prev_close) (D1a)
    5. classify_us_regime() with full parameters (D1)
    6. Check accuracy (D3: UNCLEAR not scored)

    SPY context (D1b): if 'SPY' is in bars_by_symbol, it is processed first
    and its regime is used as spy_regime for other symbols.
    """
    if regime_cfg is None:
        regime_cfg = {}

    _exclude = exclude_symbols or set()
    all_days: list[RegimeEvalDay] = []

    # Config extraction — mirror main.py
    gap_and_go_rvol = regime_cfg.get("gap_and_go_rvol", 1.5)
    trend_day_rvol = regime_cfg.get("trend_day_rvol", 1.2)
    fade_chop_rvol = regime_cfg.get("fade_chop_rvol", 1.0)
    min_vp_trading_days = regime_cfg.get("min_vp_trading_days", 3)
    adaptive_cfg = regime_cfg.get("adaptive", {})
    gap_significance_threshold = adaptive_cfg.get("gap_significance_threshold", 0.3)

    min_history = max(vp_lookback_days, rvol_lookback_days)

    # D1c: RVOL evaluation times
    skip_cutoff = dt_time(US_OPEN.hour, US_OPEN.minute + skip_open_minutes)  # 09:33
    eval_cutoff = dt_time(US_OPEN.hour, US_OPEN.minute + eval_minutes)       # 09:38

    # Determine symbol processing order (SPY first for D1b)
    symbols_list = list(bars_by_symbol.keys())
    if "SPY" in symbols_list:
        symbols_list.remove("SPY")
        symbols_list.insert(0, "SPY")

    # Cache SPY regime per date for D1b
    spy_regime_by_date: dict = {}

    for symbol in symbols_list:
        bars = bars_by_symbol[symbol]
        if symbol in _exclude:
            continue

        daily = _split_by_date(bars)
        dates = sorted(daily.keys())

        for i, target_date in enumerate(dates):
            if i < min_history:
                continue

            # VP from D-vp_lookback..D-1
            vp_dates = dates[max(0, i - vp_lookback_days):i]
            vp_bars = pd.concat([daily[d] for d in vp_dates])
            if vp_bars.empty or len(vp_bars) < 10:
                continue

            vp = compute_volume_profile(vp_bars, value_area_pct=value_area_pct, recency_decay=recency_decay)
            if vp.poc == 0:
                continue

            day_bars = daily[target_date]
            if day_bars.empty or len(day_bars) < 5:
                continue

            # prev_close: last bar of D-1
            prev_close = float(daily[dates[i - 1]].iloc[-1]["Close"])
            open_price = float(day_bars.iloc[0]["Open"])

            # D1a: PM fallback — gap_estimate
            pmh = max(open_price, prev_close)
            pml = min(open_price, prev_close)

            # D1c: RVOL — bars from skip_cutoff to eval_cutoff on day D
            today_rvol_bars = day_bars[
                (day_bars.index.time >= skip_cutoff) & (day_bars.index.time <= eval_cutoff)
            ]
            if today_rvol_bars.empty:
                today_rvol_bars = day_bars.iloc[:1]

            # History for RVOL: D-rvol_lookback..D-1
            rvol_dates = dates[max(0, i - rvol_lookback_days):i]
            rvol_hist_bars = pd.concat([daily[d] for d in rvol_dates])

            rvol = calculate_us_rvol(
                today_rvol_bars, rvol_hist_bars,
                skip_open_minutes=skip_open_minutes,
                lookback_days=rvol_lookback_days,
            )

            # D1d: Adaptive RVOL profile
            rvol_profile = None
            adaptive_thresholds = None
            if not no_adaptive and adaptive_cfg.get("enabled", True):
                # History for adaptive: D-rvol_lookback..D-1
                rvol_profile = compute_rvol_profile(
                    history_bars=rvol_hist_bars,
                    today_rvol=rvol,
                    skip_open_minutes=skip_open_minutes,
                    gap_and_go_pctl=adaptive_cfg.get("gap_and_go_percentile", 85),
                    trend_day_pctl=adaptive_cfg.get("trend_day_percentile", 60),
                    fade_chop_pctl=adaptive_cfg.get("fade_chop_percentile", 30),
                    fallback_gap_and_go=gap_and_go_rvol,
                    fallback_trend_day=trend_day_rvol,
                    fallback_fade_chop=fade_chop_rvol,
                    min_sample_days=adaptive_cfg.get("min_sample_days", 5),
                )
                if rvol_profile.sample_size >= 5:
                    adaptive_thresholds = {
                        "gap_and_go": round(rvol_profile.gap_and_go_rvol, 2),
                        "trend_day": round(rvol_profile.trend_day_rvol, 2),
                        "fade_chop": round(rvol_profile.fade_chop_rvol, 2),
                        "pctl_rank": round(rvol_profile.percentile_rank, 1),
                        "sample": rvol_profile.sample_size,
                    }

            # Eval price: use close of the eval_cutoff bar (or last early bar)
            eval_bars = day_bars[day_bars.index.time <= eval_cutoff]
            eval_price = float(eval_bars.iloc[-1]["Close"]) if not eval_bars.empty else open_price

            # D1b: SPY context
            spy_regime = None
            if symbol != "SPY" and target_date in spy_regime_by_date:
                spy_regime = spy_regime_by_date[target_date]

            # D1: classify_us_regime with full parameters
            regime = classify_us_regime(
                price=eval_price,
                prev_close=prev_close,
                rvol=rvol,
                pmh=pmh,
                pml=pml,
                vp=vp,
                gamma_wall=None,  # backtest: no option chain
                spy_regime=spy_regime,
                gap_and_go_rvol=gap_and_go_rvol,
                trend_day_rvol=trend_day_rvol,
                fade_chop_rvol=fade_chop_rvol,
                vp_trading_days=vp.trading_days,
                min_vp_trading_days=min_vp_trading_days,
                rvol_profile=rvol_profile,
                gap_significance_threshold=gap_significance_threshold,
                pm_source="gap_estimate",
                open_price=open_price,
            )

            # Cache SPY regime for other symbols (D1b)
            if symbol == "SPY":
                spy_regime_by_date[target_date] = regime.regime

            # Actual day statistics
            day_open = float(day_bars.iloc[0]["Open"])
            day_high = float(day_bars["High"].max())
            day_low = float(day_bars["Low"].min())
            day_close = float(day_bars.iloc[-1]["Close"])
            gap_pct = ((open_price - prev_close) / prev_close * 100) if prev_close > 0 else 0.0

            # D3: Check accuracy
            accurate, details, scorable = _check_regime_accuracy(
                regime.regime, day_high, day_low, day_close, vp, gap_pct,
            )

            eval_day = RegimeEvalDay(
                date=target_date,
                symbol=symbol,
                predicted=regime.regime,
                confidence=regime.confidence,
                rvol=rvol,
                vah=vp.vah,
                val=vp.val,
                poc=vp.poc,
                prev_close=prev_close,
                gap_pct=gap_pct,
                pmh=pmh,
                pml=pml,
                adaptive_thresholds=adaptive_thresholds,
                day_open=day_open,
                day_high=day_high,
                day_low=day_low,
                day_close=day_close,
                accurate=accurate,
                scorable=scorable,
                details=details,
            )
            all_days.append(eval_day)

    return _aggregate_regime_results(all_days)


def _check_regime_accuracy(
    regime: USRegimeType,
    day_high: float,
    day_low: float,
    day_close: float,
    vp: VolumeProfileResult,
    gap_pct: float,
) -> tuple[bool, str, bool]:
    """Check if regime prediction matches actual price action.

    Returns (accurate, details, scorable).
    UNCLEAR is not scorable (D3).
    """
    if regime == USRegimeType.GAP_AND_GO:
        # Accurate if gap direction confirmed by close position
        if gap_pct > 0 and day_close > vp.vah:
            return True, f"Gap up confirmed: Close {day_close:.2f} > VAH {vp.vah:.2f}", True
        if gap_pct < 0 and day_close < vp.val:
            return True, f"Gap down confirmed: Close {day_close:.2f} < VAL {vp.val:.2f}", True
        # Also count if close outside VA regardless of gap direction
        if day_close > vp.vah:
            return True, f"Close {day_close:.2f} above VAH {vp.vah:.2f}", True
        if day_close < vp.val:
            return True, f"Close {day_close:.2f} below VAL {vp.val:.2f}", True
        return False, f"Close {day_close:.2f} stayed in value area [{vp.val:.2f}-{vp.vah:.2f}]", True

    if regime == USRegimeType.TREND_DAY:
        # Accurate if close outside value area
        if day_close > vp.vah:
            return True, f"Close {day_close:.2f} above VAH {vp.vah:.2f}", True
        if day_close < vp.val:
            return True, f"Close {day_close:.2f} below VAL {vp.val:.2f}", True
        return False, f"Close {day_close:.2f} stayed in value area", True

    if regime == USRegimeType.FADE_CHOP:
        # Accurate if price stayed within value area
        if day_high < vp.vah and day_low > vp.val:
            return True, f"Range [{day_low:.2f}-{day_high:.2f}] within VA", True
        return False, f"Breached VA: H={day_high:.2f} VAH={vp.vah:.2f}, L={day_low:.2f} VAL={vp.val:.2f}", True

    # UNCLEAR: not scored (D3)
    return False, "N/A (UNCLEAR not scored)", False


def _aggregate_regime_results(days: list[RegimeEvalDay]) -> RegimeEvalResult:
    """Aggregate regime evaluations into summary statistics.

    D3: by_regime contains {total, scorable, accurate}.
    UNCLEAR days are counted in total but not in scorable.
    """
    by_regime: dict[str, dict[str, int]] = {}
    by_symbol: dict[str, dict[str, int]] = {}

    for day in days:
        regime_key = day.predicted.value

        if regime_key not in by_regime:
            by_regime[regime_key] = {"total": 0, "scorable": 0, "accurate": 0}
        by_regime[regime_key]["total"] += 1
        if day.scorable:
            by_regime[regime_key]["scorable"] += 1
        if day.accurate:
            by_regime[regime_key]["accurate"] += 1

        if day.symbol not in by_symbol:
            by_symbol[day.symbol] = {"total": 0, "scorable": 0, "accurate": 0}
        by_symbol[day.symbol]["total"] += 1
        if day.scorable:
            by_symbol[day.symbol]["scorable"] += 1
        if day.accurate:
            by_symbol[day.symbol]["accurate"] += 1

    return RegimeEvalResult(days=days, by_regime=by_regime, by_symbol=by_symbol)
