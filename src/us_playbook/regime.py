from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.common.indicators import calculate_vwap_hold_duration, calculate_vwap_series, calculate_vwap_slope
from src.common.types import GammaWallResult, VolumeProfileResult
from src.us_playbook import RegimeFamily, USRegimeResult, USRegimeType
from src.us_playbook.indicators import RvolProfile
from src.utils.logger import setup_logger

logger = setup_logger("us_regime")


def _check_trend_exhaustion(
    today_bars: pd.DataFrame,
    rvol_profile: RvolProfile | None,
    elapsed_ratio: float,
) -> tuple[bool, str]:
    """Check if today's range has consumed most of the average daily range.

    Returns (exhausted, detail_msg).

    ``elapsed_ratio`` is fraction of trading session elapsed (0.0 = open, 1.0 = close).
    Threshold is tighter early (0.85) and relaxes toward close (0.70).
    """
    if rvol_profile is None or rvol_profile.avg_daily_range_pct <= 0:
        return False, ""
    if today_bars is None or today_bars.empty:
        return False, ""

    today_high = float(today_bars["High"].max())
    today_low = float(today_bars["Low"].min())
    if today_low <= 0:
        return False, ""

    consumed_pct = (today_high - today_low) / today_low * 100
    exhaustion_ratio = consumed_pct / rvol_profile.avg_daily_range_pct
    threshold = 0.70 + 0.15 * (1 - elapsed_ratio)  # early 0.85, late 0.70
    if exhaustion_ratio > threshold:
        detail = f"Trend exhausted: consumed {consumed_pct:.1f}% of {rvol_profile.avg_daily_range_pct:.1f}% ADR ({exhaustion_ratio:.0%})"
        return True, detail
    return False, ""


def _vwap_hold_ratio(today_bars: pd.DataFrame) -> tuple[float, str]:
    """Return (ratio, side) — fraction of bars on the dominant VWAP side.

    ``side`` is "bullish" if majority closes > VWAP, "bearish" if <, else "neutral".
    Returns ``(0.0, "neutral")`` on empty / NaN data.
    """
    if today_bars is None or today_bars.empty:
        return 0.0, "neutral"

    vwap_s = calculate_vwap_series(today_bars)
    if vwap_s.empty or vwap_s.isna().all():
        return 0.0, "neutral"

    closes = today_bars["Close"].values
    vwaps = vwap_s.values
    valid = ~np.isnan(vwaps)
    if valid.sum() == 0:
        return 0.0, "neutral"

    above = np.sum(closes[valid] > vwaps[valid])
    below = np.sum(closes[valid] < vwaps[valid])
    total = int(valid.sum())

    if above >= below:
        return above / total, "bullish"
    return below / total, "bearish"


# ── Price Structure Detection ──


@dataclass
class StructureResult:
    """Result of price structure detection."""
    direction: str  # "bullish" | "bearish"
    strength: float
    layer: int  # 1 or 2
    confidence: float


def detect_price_structure(
    today_bars: pd.DataFrame,
    *,
    window: int = 15,
    min_windows: int = 3,
    consistency: float = 0.67,
    fast_min_bars: int = 20,
    fast_side_pct: float = 0.80,
    fast_r2_min: float = 0.70,
    flat_threshold: float = 0.0005,  # DEPRECATED — kept for config compat, not used as gate
) -> StructureResult | None:
    """Detect directional price structure via two layers.

    Layer 1 (fast): Close R² entry gate + VWAP side cross-validation (≥20 bars).
    Layer 2 (swing): Rolling window HH/HL or LH/LL (≥ window * min_windows bars).

    Returns the highest-layer result, or None if no structure detected.
    """
    if today_bars is None or today_bars.empty:
        return None

    n_bars = len(today_bars)
    l2_min_bars = window * min_windows

    l2_result: StructureResult | None = None
    l1_result: StructureResult | None = None

    # ── Layer 2: Rolling window peaks/troughs ──
    if n_bars >= l2_min_bars:
        n_full_windows = n_bars // window
        if n_full_windows >= min_windows:
            highs = []
            lows = []
            for i in range(n_full_windows):
                chunk = today_bars.iloc[i * window : (i + 1) * window]
                highs.append(float(chunk["High"].max()))
                lows.append(float(chunk["Low"].min()))

            n = len(highs) - 1  # number of adjacent pairs
            if n >= 2:
                hh_count = sum(1 for i in range(n) if highs[i + 1] > highs[i])
                hl_count = sum(1 for i in range(n) if lows[i + 1] > lows[i])
                lh_count = sum(1 for i in range(n) if highs[i + 1] < highs[i])
                ll_count = sum(1 for i in range(n) if lows[i + 1] < lows[i])

                # RC2: Primary signal is hard gate; secondary only affects strength/confidence
                bearish_primary = lh_count / n >= consistency  # LH is hard gate for bearish
                bullish_primary = hh_count / n >= consistency  # HH is hard gate for bullish

                # VWAP slope cross-check for L2
                vwap_slope = calculate_vwap_slope(today_bars, lookback=min(n_bars, 30))

                if bearish_primary and not bullish_primary and vwap_slope < 0:
                    strength = (lh_count + ll_count) / (2 * n)
                    conf = 0.45 + min(0.20, strength * 0.25)
                    if ll_count / n < consistency:
                        conf = max(0.40, conf - 0.05)  # secondary shortfall penalty
                    l2_result = StructureResult("bearish", strength, 2, conf)
                elif bullish_primary and not bearish_primary and vwap_slope > 0:
                    strength = (hh_count + hl_count) / (2 * n)
                    conf = 0.45 + min(0.20, strength * 0.25)
                    if hl_count / n < consistency:
                        conf = max(0.40, conf - 0.05)  # secondary shortfall penalty
                    l2_result = StructureResult("bullish", strength, 2, conf)

    # ── Layer 1: Close R² entry gate + VWAP side cross-validation ──
    if n_bars >= fast_min_bars:
        tail = today_bars.iloc[-fast_min_bars:]
        closes = tail["Close"].values.astype(float)
        x = np.arange(len(closes), dtype=float)

        if np.std(closes) > 0:
            slope_c, intercept = np.polyfit(x, closes, 1)
            y_pred = slope_c * x + intercept
            ss_res = np.sum((closes - y_pred) ** 2)
            ss_tot = np.sum((closes - np.mean(closes)) ** 2)
            r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
        else:
            slope_c = 0.0
            r2 = 0.0

        if r2 >= fast_r2_min:
            direction = "bearish" if slope_c < 0 else "bullish"
            conf = 0.40 + min(0.10, (r2 - fast_r2_min) * 0.33)

            # VWAP side cross-validation (soft — penalty, not rejection)
            vwap_series = calculate_vwap_series(today_bars)
            vwap_tail = vwap_series.iloc[-fast_min_bars:]
            vwaps = vwap_tail.values
            if direction == "bearish":
                side_count = np.sum(closes < vwaps)
            else:
                side_count = np.sum(closes > vwaps)
            side_pct = side_count / fast_min_bars
            if side_pct < fast_side_pct:
                conf = max(0.35, conf - 0.05)  # cross-validation shortfall

            # Hold Duration bonus
            hold_ratio, hold_side = _vwap_hold_ratio(today_bars)
            if hold_ratio >= 0.75 and hold_side == direction:
                conf = min(0.65, conf + 0.05)

            l1_result = StructureResult(direction, r2, 1, conf)

    # Prefer L2 over L1
    if l2_result is not None:
        return l2_result
    return l1_result


def _detect_v_reversal(
    today_bars: pd.DataFrame,
    open_price: float,
    prev_close: float,
    rvol: float,
    cfg: dict | None = None,
) -> tuple[bool, str, float]:
    """Detect V-shaped intraday reversal.

    Conditions:
    1. Price moved > min_initial_move_pct from open in one direction
    2. Price has reversed to the other side of open
    3. Reversal area has elevated RVOL (local volume > avg)

    Returns (detected, direction, confidence).
    Direction is the POST-reversal direction ("bullish" if reversed up).
    """
    if today_bars is None or today_bars.empty or len(today_bars) < 20:
        return False, "neutral", 0.0

    _cfg = cfg or {}
    min_move_pct = _cfg.get("min_initial_move_pct", 0.5)
    confirmation_bars = _cfg.get("confirmation_bars", 5)

    if open_price <= 0:
        return False, "neutral", 0.0

    high = float(today_bars["High"].max())
    low = float(today_bars["Low"].min())
    current = float(today_bars.iloc[-1]["Close"])

    up_move = (high - open_price) / open_price * 100
    down_move = (open_price - low) / open_price * 100

    # Check for bearish-then-bullish reversal (V bottom)
    if down_move >= min_move_pct and current > open_price:
        # Confirm: last N bars trending up
        tail = today_bars.iloc[-confirmation_bars:]
        if float(tail.iloc[-1]["Close"]) > float(tail.iloc[0]["Open"]):
            # Check volume in reversal area
            avg_vol = float(today_bars["Volume"].mean())
            tail_vol = float(tail["Volume"].mean())
            vol_ok = avg_vol <= 0 or tail_vol >= avg_vol
            if vol_ok:
                confidence = min(0.70, 0.45 + down_move / 100 * 5)
                return True, "bullish", confidence

    # Check for bullish-then-bearish reversal (inverted V)
    if up_move >= min_move_pct and current < open_price:
        tail = today_bars.iloc[-confirmation_bars:]
        if float(tail.iloc[-1]["Close"]) < float(tail.iloc[0]["Open"]):
            avg_vol = float(today_bars["Volume"].mean())
            tail_vol = float(tail["Volume"].mean())
            vol_ok = avg_vol <= 0 or tail_vol >= avg_vol
            if vol_ok:
                confidence = min(0.70, 0.45 + up_move / 100 * 5)
                return True, "bearish", confidence

    return False, "neutral", 0.0


def _detect_gap_fill(
    today_bars: pd.DataFrame,
    gap_pct: float,
    open_price: float,
    prev_close: float,
    rvol: float,
    cfg: dict | None = None,
) -> tuple[bool, float, float]:
    """Detect gap fill — gap has been substantially retraced.

    Returns (detected, fill_pct, confidence).
    """
    if today_bars is None or today_bars.empty:
        return False, 0.0, 0.0

    _cfg = cfg or {}
    min_gap_pct = _cfg.get("min_gap_pct", 0.2)
    fill_threshold_pct = _cfg.get("fill_threshold_pct", 50)

    if abs(gap_pct) < min_gap_pct or open_price <= 0 or prev_close <= 0:
        return False, 0.0, 0.0

    current = float(today_bars.iloc[-1]["Close"])
    gap_size = open_price - prev_close  # positive = gap up

    if abs(gap_size) < 0.0001:
        return False, 0.0, 0.0

    # Fill percentage: how much of the gap has been retraced
    if gap_size > 0:
        # Gap up: fill = how far price has dropped from open toward prev_close
        fill = (open_price - current) / gap_size * 100
    else:
        # Gap down: fill = how far price has risen from open toward prev_close
        fill = (current - open_price) / abs(gap_size) * 100

    fill = max(0.0, min(100.0, fill))

    if fill >= fill_threshold_pct:
        confidence = min(0.70, 0.40 + fill / 100 * 0.3)
        return True, fill, confidence

    return False, fill, 0.0


def detect_regime_transition(
    original: USRegimeResult,
    current_rvol: float,
    current_price: float,
    vp: VolumeProfileResult,
    spy_regime: USRegimeType | None = None,
    prev_close: float = 0.0,
    pmh: float = 0.0,
    pml: float = 0.0,
    gap_and_go_rvol: float = 1.5,
    trend_day_rvol: float = 1.2,
    fade_chop_rvol: float = 1.0,
    rvol_profile=None,
    gap_significance_threshold: float = 0.3,
    pm_source: str = "futu",
    open_price: float = 0.0,
    today_bars: pd.DataFrame | None = None,
    structure_trend_cfg: dict | None = None,
    vwap: float = 0.0,
) -> tuple[bool, USRegimeResult | None]:
    """Detect if regime has transitioned from original classification.

    Returns (transitioned, new_regime) — only returns True for meaningful
    upgrades (UNCLEAR/RANGE → TREND_STRONG/GAP_GO).
    """
    # Strong trend regimes: only check for V_REVERSAL and GAP_FILL transitions
    if original.regime.family == RegimeFamily.TREND:
        # Check V_REVERSAL and GAP_FILL below, but skip re-classification
        if today_bars is None:
            return False, None
        # V_REVERSAL from trending
        detected, v_dir, v_conf = _detect_v_reversal(
            today_bars, open_price, prev_close, current_rvol,
        )
        if detected and v_conf >= 0.50:
            return True, USRegimeResult(
                regime=USRegimeType.V_REVERSAL, confidence=v_conf,
                rvol=current_rvol, price=current_price,
                gap_pct=original.gap_pct,
                spy_regime=spy_regime,
                details=f"V-reversal {v_dir}: from {original.regime.value}",
                lean=v_dir,
                reversal_confirmed=True,
            )
        # GAP_FILL from GAP_GO
        if original.regime == USRegimeType.GAP_GO:
            detected, fill_pct, fill_conf = _detect_gap_fill(
                today_bars, original.gap_pct, open_price, prev_close, current_rvol,
            )
            if detected and fill_conf >= 0.40:
                fill_dir = "bearish" if original.gap_pct > 0 else "bullish"
                return True, USRegimeResult(
                    regime=USRegimeType.GAP_FILL, confidence=fill_conf,
                    rvol=current_rvol, price=current_price,
                    gap_pct=original.gap_pct,
                    spy_regime=spy_regime,
                    details=f"Gap fill {fill_pct:.0f}%: gap was {original.gap_pct:+.2f}%",
                    lean=fill_dir,
                    gap_fill_pct=fill_pct,
                )
        return False, None

    new_regime = classify_us_regime(
        price=current_price,
        prev_close=prev_close,
        rvol=current_rvol,
        pmh=pmh,
        pml=pml,
        vp=vp,
        spy_regime=spy_regime,
        gap_and_go_rvol=gap_and_go_rvol,
        trend_day_rvol=trend_day_rvol,
        fade_chop_rvol=fade_chop_rvol,
        rvol_profile=rvol_profile,
        gap_significance_threshold=gap_significance_threshold,
        pm_source=pm_source,
        open_price=open_price,
        today_bars=today_bars,
        structure_trend_cfg=structure_trend_cfg,
        vwap=vwap,
    )

    # Only signal meaningful upgrades
    upgrades = {
        USRegimeType.UNCLEAR: (USRegimeType.TREND_STRONG, USRegimeType.GAP_GO),
        USRegimeType.RANGE: (USRegimeType.TREND_STRONG, USRegimeType.GAP_GO),
    }
    valid_targets = upgrades.get(original.regime, ())
    if new_regime.regime in valid_targets and new_regime.confidence >= 0.60:
        new_regime.details = f"Regime transition: {original.regime.value} → {new_regime.regime.value}; {new_regime.details}"
        return True, new_regime

    return False, None


def regime_to_signal_type(regime: USRegimeType, direction: str) -> str | None:
    """Map US regime + direction to auto-scan signal type.

    Returns signal type string or None for UNCLEAR/NARROW_GRIND.
    """
    if regime.family == RegimeFamily.TREND:
        return f"BREAKOUT_{direction.upper()}"
    if regime.family == RegimeFamily.FADE and regime != USRegimeType.NARROW_GRIND:
        return f"RANGE_REVERSAL_{direction.upper()}"
    if regime == USRegimeType.V_REVERSAL:
        return f"REVERSAL_{direction.upper()}"
    if regime == USRegimeType.GAP_FILL:
        return f"RANGE_REVERSAL_{direction.upper()}"
    return None


def classify_us_regime(
    price: float,
    prev_close: float,
    rvol: float,
    pmh: float,
    pml: float,
    vp: VolumeProfileResult,
    gamma_wall: GammaWallResult | None = None,
    spy_regime: USRegimeType | None = None,
    gap_and_go_rvol: float = 1.5,
    trend_day_rvol: float = 1.2,
    fade_chop_rvol: float = 1.0,
    vp_trading_days: int = 0,
    min_vp_trading_days: int = 3,
    rvol_profile: RvolProfile | None = None,
    gap_significance_threshold: float = 0.3,
    pm_source: str = "futu",
    open_price: float = 0.0,
    today_bars: pd.DataFrame | None = None,
    structure_trend_cfg: dict | None = None,
    vwap: float = 0.0,
    vwap_trend_cfg: dict | None = None,
    narrow_grind_rvol: float = 0.5,
    narrow_grind_adr_ratio: float = 0.5,
    trend_strong_rvol: float = 0.0,  # 0=use gap_and_go_rvol
) -> USRegimeResult:
    """Classify US intraday regime.

    Styles:
        GAP_GO: Gap + high RVOL + price beyond PM range
        TREND_STRONG: Moderate RVOL + directional, no big gap
        RANGE: Low RVOL + range-bound
        UNCLEAR: Mixed signals

    If ``rvol_profile`` is provided with sufficient sample size (>= 5),
    adaptive thresholds override the static parameters.
    """
    if vp.poc == 0:
        return USRegimeResult(
            regime=USRegimeType.UNCLEAR, confidence=0.0,
            rvol=rvol, price=price, gap_pct=0.0,
            details="No volume profile data",
        )

    # Apply adaptive thresholds if available
    adaptive_info: dict | None = None
    if rvol_profile and rvol_profile.sample_size >= 5:
        gap_and_go_rvol = rvol_profile.gap_and_go_rvol
        trend_day_rvol = rvol_profile.trend_day_rvol
        fade_chop_rvol = rvol_profile.fade_chop_rvol
        adaptive_info = {
            "gap_and_go": round(gap_and_go_rvol, 2),
            "trend_day": round(trend_day_rvol, 2),
            "fade_chop": round(fade_chop_rvol, 2),
            "pctl_rank": round(rvol_profile.percentile_rank, 1),
            "sample": rvol_profile.sample_size,
        }
        threshold_label = "adaptive"
    else:
        threshold_label = "static"

    # Gap calculation — use open_price (fixed at open) rather than current price
    gap_ref = open_price if open_price > 0 else price  # fallback for callers that don't pass open_price
    gap_pct = ((gap_ref - prev_close) / prev_close * 100) if prev_close > 0 else 0.0

    # Normalized gap: use daily range if available from adaptive profile
    if rvol_profile and rvol_profile.avg_daily_range_pct > 0:
        normalized_gap = abs(gap_pct) / rvol_profile.avg_daily_range_pct
        small_gap = normalized_gap < gap_significance_threshold
    else:
        small_gap = abs(gap_pct) < 0.5  # fallback static

    # VWAP-confirmed gap relaxation for TREND_STRONG
    _vwap_confirms = (price < vp.val and vwap > 0 and price < vwap) or \
                     (price > vp.vah and vwap > 0 and price > vwap)
    if rvol_profile and rvol_profile.avg_daily_range_pct > 0:
        _relaxed_gap = normalized_gap < 0.5
    else:
        _relaxed_gap = abs(gap_pct) < 0.8
    _gap_ok = small_gap or (_vwap_confirms and _relaxed_gap)

    # Position relative to VP value area
    inside_va = vp.val <= price <= vp.vah
    outside_va = not inside_va

    # Position relative to pre-market range
    above_pm = price > pmh if pmh > 0 else False
    below_pm = price < pml if pml > 0 else False
    pm_breakout = above_pm or below_pm

    # Near gamma wall check
    near_gamma = False
    if gamma_wall:
        if gamma_wall.call_wall_strike > 0:
            near_gamma = abs(price - gamma_wall.call_wall_strike) / price < 0.01
        if not near_gamma and gamma_wall.put_wall_strike > 0:
            near_gamma = abs(price - gamma_wall.put_wall_strike) / price < 0.01

    # ── Phase 2: VWAP hold duration (computed once, used by TREND split) ──
    _vwap_cfg = vwap_trend_cfg or {}
    _hold_min_threshold = _vwap_cfg.get("hold_minutes_trend_bias", 60)
    if today_bars is not None and not today_bars.empty:
        _vwap_hold_bars, _vwap_hold_side = calculate_vwap_hold_duration(today_bars)
        _vwap_hold_min = _vwap_hold_bars  # 1 bar = 1 minute for 1m bars
    else:
        _vwap_hold_bars, _vwap_hold_side = 0, "neutral"
        _vwap_hold_min = 0

    # Phase 2: effective RVOL threshold for TREND_STRONG
    _trend_strong_threshold = trend_strong_rvol if trend_strong_rvol > 0 else gap_and_go_rvol

    result: USRegimeResult | None = None

    # Large gap signal: gap occupies >= 80% of ADR → treat as gap-driven even if PM absorbed
    large_gap_signal = False
    if rvol_profile and rvol_profile.avg_daily_range_pct > 0:
        large_gap_signal = normalized_gap >= 0.8

    # ── GAP_GO ──
    if rvol >= gap_and_go_rvol and (pm_breakout or large_gap_signal):
        if pm_breakout:
            direction = "above PMH" if above_pm else "below PML"
        else:
            direction = "gap up" if gap_pct > 0 else "gap down"
        confidence = min(1.0, (rvol - gap_and_go_rvol) / 0.5 * 0.3 + 0.6)
        # Gap-driven (not PM breakout) → penalty -0.10
        if not pm_breakout:
            confidence = max(0.1, confidence - 0.10)
        # SPY context adjustment (P1-3: asymmetric — boost stronger, penalty milder)
        if spy_regime == USRegimeType.RANGE:
            confidence = max(0.1, confidence - 0.15)
        elif spy_regime in (USRegimeType.GAP_GO, USRegimeType.TREND_STRONG):
            confidence = min(1.0, confidence + 0.15)
        result = USRegimeResult(
            regime=USRegimeType.GAP_GO, confidence=confidence,
            rvol=rvol, price=price, gap_pct=gap_pct,
            spy_regime=spy_regime,
            adaptive_thresholds=adaptive_info,
            details=f"RVOL {rvol:.2f} >= {gap_and_go_rvol:.2f} ({threshold_label}), {direction}, gap {gap_pct:+.2f}%",
        )

    # Elapsed ratio for exhaustion check (US session = 390 1m bars)
    _elapsed_ratio = min(1.0, len(today_bars) / 390) if today_bars is not None and not today_bars.empty else 0.0

    # ── TREND_STRONG ──
    if result is None and rvol >= trend_day_rvol and _gap_ok and outside_va:
        direction = "above VAH" if price > vp.vah else "below VAL"
        confidence = min(1.0, (rvol - trend_day_rvol) / 0.5 * 0.3 + 0.5)
        # P1-3: SPY context — milder penalty + new boost branch
        if spy_regime == USRegimeType.RANGE:
            confidence = max(0.1, confidence - 0.12)
        elif spy_regime in (USRegimeType.TREND_STRONG, USRegimeType.GAP_GO):
            confidence = min(1.0, confidence + 0.10)
        # P0-1: Trend exhaustion — downgrade to UNCLEAR if range consumed
        _trend_dir = "bullish" if price > vp.vah else "bearish"
        _exhausted, _exhaust_detail = _check_trend_exhaustion(today_bars, rvol_profile, _elapsed_ratio)
        if _exhausted:
            result = USRegimeResult(
                regime=USRegimeType.UNCLEAR, confidence=0.30,
                rvol=rvol, price=price, gap_pct=gap_pct,
                spy_regime=spy_regime,
                adaptive_thresholds=adaptive_info,
                details=_exhaust_detail,
                lean=_trend_dir,
            )
        else:
            # Relaxed gap penalty: VWAP-confirmed but gap not truly small
            if not small_gap:
                confidence = max(0.1, confidence - 0.10)
            _gap_note = ", VWAP-confirmed gap relaxation" if not small_gap else ""
            # Phase 2: TREND_STRONG vs TREND_WEAK split
            if rvol >= _trend_strong_threshold or _vwap_hold_min >= _hold_min_threshold:
                _rvol_regime = USRegimeType.TREND_STRONG
            else:
                _rvol_regime = USRegimeType.TREND_WEAK
            result = USRegimeResult(
                regime=_rvol_regime, confidence=confidence,
                rvol=rvol, price=price, gap_pct=gap_pct,
                spy_regime=spy_regime,
                adaptive_thresholds=adaptive_info,
                details=f"RVOL {rvol:.2f} >= {trend_day_rvol:.2f} ({threshold_label}), small gap {gap_pct:+.2f}%, price {direction}{_gap_note}",
            )

    # ── TREND_WEAK (Structure-based) ──
    _st_cfg = structure_trend_cfg or {}
    if result is None and _st_cfg.get("enabled", False) and today_bars is not None:
        structure = detect_price_structure(
            today_bars,
            window=_st_cfg.get("window", 15),
            min_windows=_st_cfg.get("min_windows", 3),
            consistency=_st_cfg.get("consistency", 0.67),
            fast_min_bars=_st_cfg.get("fast_min_bars", 20),
            fast_side_pct=_st_cfg.get("fast_side_pct", 0.80),
            fast_r2_min=_st_cfg.get("fast_r2_min", 0.70),
        )
        if structure is not None:
            confidence = structure.confidence
            # SPY context adjustment (same as RVOL-based TREND)
            if spy_regime == USRegimeType.RANGE:
                confidence = max(0.1, confidence - 0.12)
            elif spy_regime in (USRegimeType.TREND_STRONG, USRegimeType.GAP_GO):
                confidence = min(1.0, confidence + 0.10)
            # P0-1: Trend exhaustion — downgrade to UNCLEAR if range consumed
            _exhausted, _exhaust_detail = _check_trend_exhaustion(today_bars, rvol_profile, _elapsed_ratio)
            if _exhausted:
                result = USRegimeResult(
                    regime=USRegimeType.UNCLEAR, confidence=0.30,
                    rvol=rvol, price=price, gap_pct=gap_pct,
                    spy_regime=spy_regime,
                    adaptive_thresholds=adaptive_info,
                    details=_exhaust_detail,
                    lean=structure.direction,
                )
            else:
                layer_label = f"L{structure.layer}"
                result = USRegimeResult(
                    regime=USRegimeType.TREND_WEAK, confidence=confidence,
                    rvol=rvol, price=price, gap_pct=gap_pct,
                    spy_regime=spy_regime,
                    adaptive_thresholds=adaptive_info,
                    details=(
                        f"Structure {layer_label} {structure.direction}: "
                        f"strength {structure.strength:.2f}, "
                        f"RVOL {rvol:.2f} (below {trend_day_rvol:.2f})"
                    ),
                    lean=structure.direction,
                )

    # ── TREND_WEAK (Persistence — inside VA but still trending) ──
    _enough_bars = today_bars is not None and len(today_bars) >= 30
    if (result is None and rvol >= trend_day_rvol and small_gap
            and inside_va and open_price > 0 and _enough_bars):
        intraday_return = (price - open_price) / open_price
        _trend_lean = "bearish" if intraday_return < 0 else "bullish"
        # V-shape guard: intraday_return direction must agree with price vs VWAP
        vwap_agrees = (vwap <= 0
            or (_trend_lean == "bearish" and price < vwap)
            or (_trend_lean == "bullish" and price > vwap))
        # RC3: Dynamic persistence threshold based on ADR
        if rvol_profile and rvol_profile.avg_daily_range_pct > 0:
            _adr = rvol_profile.avg_daily_range_pct
            if not (0.1 < _adr < 20):
                logger.warning("ADR %.2f%% outside sane range, fallback 0.01", _adr)
                _persist_threshold = 0.01
            else:
                _persist_threshold = max(0.005, 0.4 * _adr / 100)
        else:
            _persist_threshold = 0.01
        if abs(intraday_return) >= _persist_threshold and vwap_agrees:
            base_confidence = min(1.0, (rvol - trend_day_rvol) / 0.5 * 0.3 + 0.5)
            confidence = max(0.1, base_confidence - 0.15)
            if spy_regime == USRegimeType.RANGE:
                confidence = max(0.1, confidence - 0.12)
            elif spy_regime in (USRegimeType.TREND_STRONG, USRegimeType.GAP_GO):
                confidence = min(1.0, confidence + 0.10)
            direction = "above VAH" if price > vp.vah else "below VAL" if price < vp.val else "in VA"
            result = USRegimeResult(
                regime=USRegimeType.TREND_WEAK, confidence=confidence,
                rvol=rvol, price=price, gap_pct=gap_pct,
                spy_regime=spy_regime,
                adaptive_thresholds=adaptive_info,
                details=(
                    f"Trend persistence: {intraday_return:+.2%} since open, "
                    f"RVOL {rvol:.2f} >= {trend_day_rvol:.2f} ({threshold_label}), price {direction}"
                ),
                lean=_trend_lean,
            )

    # ── RANGE ──
    # P0-2: Directional trap check — low RVOL + strong unidirectional move
    # should NOT be classified as RANGE; route to UNCLEAR instead.
    _directional_trap = False
    if (
        result is None
        and rvol < fade_chop_rvol
        and today_bars is not None
        and len(today_bars) >= 15
    ):
        _open_bar_price = float(today_bars.iloc[0]["Close"])
        if _open_bar_price > 0:
            _intraday_move = abs(price - _open_bar_price) / _open_bar_price
            if _intraday_move > 0.015:  # >1.5% unidirectional since open
                _directional_trap = True
                _trap_lean = "bearish" if price < _open_bar_price else "bullish"
                result = USRegimeResult(
                    regime=USRegimeType.UNCLEAR, confidence=0.30,
                    rvol=rvol, price=price, gap_pct=gap_pct,
                    spy_regime=spy_regime,
                    adaptive_thresholds=adaptive_info,
                    details=(
                        f"Directional trap: RVOL {rvol:.2f} < {fade_chop_rvol:.2f} "
                        f"but {_intraday_move:.1%} move since open"
                    ),
                    lean=_trap_lean,
                )

    # ── NARROW_GRIND ──
    if result is None and rvol < narrow_grind_rvol:
        _narrow_range = False
        if rvol_profile and rvol_profile.avg_daily_range_pct > 0 and today_bars is not None and not today_bars.empty:
            today_high = float(today_bars["High"].max())
            today_low = float(today_bars["Low"].min())
            if today_low > 0:
                today_range_pct = (today_high - today_low) / today_low * 100
                if today_range_pct < rvol_profile.avg_daily_range_pct * narrow_grind_adr_ratio:
                    _narrow_range = True
        if _narrow_range:
            confidence = min(1.0, (narrow_grind_rvol - rvol) / 0.3 * 0.2 + 0.5)
            result = USRegimeResult(
                regime=USRegimeType.NARROW_GRIND, confidence=confidence,
                rvol=rvol, price=price, gap_pct=gap_pct,
                spy_regime=spy_regime,
                adaptive_thresholds=adaptive_info,
                details=f"RVOL {rvol:.2f} < {narrow_grind_rvol:.2f}, range {today_range_pct:.2f}% < {rvol_profile.avg_daily_range_pct * narrow_grind_adr_ratio:.2f}% (ADR*{narrow_grind_adr_ratio})",
            )

    if result is None and rvol < fade_chop_rvol and (inside_va or near_gamma):
        reason = "in value area" if inside_va else "near Gamma wall"
        confidence = min(1.0, (fade_chop_rvol - rvol) / 0.3 * 0.3 + 0.5)
        result = USRegimeResult(
            regime=USRegimeType.RANGE, confidence=confidence,
            rvol=rvol, price=price, gap_pct=gap_pct,
            spy_regime=spy_regime,
            adaptive_thresholds=adaptive_info,
            details=f"RVOL {rvol:.2f} < {fade_chop_rvol:.2f} ({threshold_label}), price {reason}",
        )

    # ── UNCLEAR (P0-3: sub-type differentiation) ──
    if result is None:
        parts = []
        lean = "neutral"
        confidence = 0.30  # default

        if inside_va and rvol >= trend_day_rvol:
            # Sub-type 2: high volume but price in VA — potential breakout buildup
            parts.append("High volume but price in value area")
            confidence = 0.40
            lean = "bullish" if price > vp.poc else "bearish"
        elif outside_va and rvol < fade_chop_rvol:
            # Sub-type 3: price outside VA but low volume — VWAP cross-validation
            parts.append("Price outside VA but low volume")
            confidence = 0.25
            # RC4: VWAP cross-validation for sub-type 3 lean
            if price > vp.vah:
                if vwap > 0 and price <= vwap:
                    lean = "bearish"
                else:
                    lean = "neutral"
            elif price < vp.val:
                if vwap > 0 and price < vwap:
                    lean = "bearish"
                elif vwap > 0 and price >= vwap:
                    lean = "bullish"
                else:
                    lean = "neutral"
            else:
                lean = "neutral"
            # Hold ratio contradiction check
            if lean != "neutral":
                _hold_ratio, _hold_side = _vwap_hold_ratio(today_bars)
                if _hold_ratio > 0.70 and _hold_side != lean:
                    lean = "neutral"
        elif trend_day_rvol > rvol >= fade_chop_rvol:
            # Sub-type 1: RVOL in neutral zone — could go either way
            parts.append(f"RVOL {rvol:.2f} in neutral zone")
            confidence = 0.30
        else:
            parts.append("Mixed signals")
            # Derive lean from price position when no sub-type matched
            if outside_va and vwap > 0:
                if price < vp.val and price < vwap:
                    lean = "bearish"
                elif price > vp.vah and price > vwap:
                    lean = "bullish"

        # VWAP + intraday return double-confirmation override
        _enough_bars_uc = today_bars is not None and len(today_bars) >= 30
        if open_price > 0 and vwap > 0 and _enough_bars_uc:
            intraday_ret = (price - open_price) / open_price
            _ret_lean = "bearish" if intraday_ret < 0 else "bullish"
            _vwap_lean = "bearish" if price < vwap else "bullish"
            if abs(intraday_ret) >= 0.004 and _ret_lean == _vwap_lean:
                lean = _ret_lean

        result = USRegimeResult(
            regime=USRegimeType.UNCLEAR, confidence=confidence,
            rvol=rvol, price=price, gap_pct=gap_pct,
            spy_regime=spy_regime,
            adaptive_thresholds=adaptive_info,
            details="; ".join(parts) if parts else "Mixed signals",
            lean=lean,
        )

    # ── Phase 2: store computed fields on final result ──
    if today_bars is not None and not today_bars.empty:
        result.vwap_slope = calculate_vwap_slope(today_bars, lookback=_vwap_cfg.get("slope_lookback_bars", 30))
        result.vwap_hold_minutes = _vwap_hold_min
    result.classified_at = time.time()

    # ── VP staleness: RANGE VA unreachable penalty ──
    if result.regime == USRegimeType.RANGE and price > 0 and vp.vah > 0 and vp.val > 0:
        _va_dist_pct = min(abs(price - vp.vah), abs(price - vp.val)) / price * 100
        if _va_dist_pct > 5.0:
            result.confidence = max(0.1, result.confidence - 0.15)
            _stale_note = f"VA unreachable ({_va_dist_pct:.1f}%)"
            result.details = f"{result.details}; {_stale_note}" if result.details else _stale_note

    # ── VP thin data penalty ──
    if 0 < vp_trading_days < min_vp_trading_days:
        result.confidence = max(0.0, result.confidence - 0.15)
        thin_note = f"VP thin ({vp_trading_days}d)"
        result.details = f"{result.details}; {thin_note}" if result.details else thin_note

    # ── PM estimated penalty ──
    if pm_source == "gap_estimate" and result.regime == USRegimeType.GAP_GO:
        result.confidence = max(0.1, result.confidence - 0.15)
        pm_note = "PM estimated (gap range)"
        result.details = f"{result.details}; {pm_note}" if result.details else pm_note

    return result


# ── Index Consistency ──

_DIRECTIONAL_REGIMES = {USRegimeType.GAP_GO, USRegimeType.TREND_STRONG, USRegimeType.TREND_WEAK, USRegimeType.V_REVERSAL}


def check_index_consistency(
    spy_regime: USRegimeType, spy_direction: str,
    qqq_regime: USRegimeType, qqq_direction: str,
) -> tuple[bool, str]:
    """Check if SPY and QQQ have conflicting directional regimes.

    Returns (conflict, detail_msg).
    Conflict only when both are in directional regimes with non-neutral
    opposite directions.
    """
    if spy_regime not in _DIRECTIONAL_REGIMES or qqq_regime not in _DIRECTIONAL_REGIMES:
        return False, ""
    if spy_direction == "neutral" or qqq_direction == "neutral":
        return False, ""
    if spy_direction == qqq_direction:
        return False, ""
    return True, (
        f"SPY {spy_regime.value} {spy_direction} vs QQQ {qqq_regime.value} {qqq_direction}"
    )


def downgrade_to_unclear(original: USRegimeResult, reason: str) -> USRegimeResult:
    """Create a downgraded UNCLEAR copy of a regime result."""
    return USRegimeResult(
        regime=USRegimeType.UNCLEAR,
        confidence=0.25,
        rvol=original.rvol,
        price=original.price,
        gap_pct=original.gap_pct,
        spy_regime=original.spy_regime,
        adaptive_thresholds=original.adaptive_thresholds,
        details=f"Downgraded from {original.regime.value}: {reason}; original: {original.details}",
        lean="neutral",
    )
