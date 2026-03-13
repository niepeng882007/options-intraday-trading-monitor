"""HK Regime classifier — 5-class system (GAP_AND_GO, TREND_DAY, FADE_CHOP, WHIPSAW, UNCLEAR)."""

from __future__ import annotations

import pandas as pd

from src.hk import RegimeType, RegimeResult, VolumeProfileResult, GammaWallResult
from src.utils.logger import setup_logger

logger = setup_logger("hk_regime")


# ── Helper functions ──


def _price_va_distance_factor(
    price: float, vah: float, val: float, regime: str,
) -> float:
    """Return 0~1 factor based on price distance from VA boundary.

    BREAKOUT/TREND: farther from VA edge → higher (deeper breakout = more conviction).
    RANGE/FADE: deeper inside VA center → higher.
    """
    va_range = vah - val
    if va_range <= 0:
        return 0.0

    if regime in ("BREAKOUT", "TREND"):
        if price > vah:
            dist = (price - vah) / va_range
        else:
            dist = (val - price) / va_range
        return min(1.0, max(0.0, dist))

    # RANGE/FADE: distance from nearest VA edge, normalized to half-range
    dist_from_edge = min(price - val, vah - price)
    return min(1.0, max(0.0, dist_from_edge / (va_range * 0.5)))


def _intraday_trend(
    today_bars: pd.DataFrame,
    min_bars: int = 10,
) -> tuple[str, float]:
    """Detect intraday trend direction and strength.

    Returns (direction, strength).
    direction: "rising" | "falling" | "flat"
    strength: 0.0-1.0
    """
    if today_bars is None or today_bars.empty or len(today_bars) < min_bars:
        return ("flat", 0.0)

    close = today_bars["Close"]

    # EMA-10 vs EMA-20 slope direction
    ema10 = close.ewm(span=10, adjust=False).mean()
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema_bullish = float(ema10.iloc[-1]) > float(ema20.iloc[-1])

    # Count lower-lows and higher-highs in recent N bars
    recent = today_bars.iloc[-min_bars:]
    lows = recent["Low"].values
    highs = recent["High"].values
    lower_lows = sum(1 for i in range(1, len(lows)) if lows[i] < lows[i - 1])
    higher_highs = sum(1 for i in range(1, len(highs)) if highs[i] > highs[i - 1])
    n = len(lows) - 1
    ll_ratio = lower_lows / n if n > 0 else 0.0
    hh_ratio = higher_highs / n if n > 0 else 0.0

    # Open → current direction
    open_price = float(close.iloc[0])
    current_price = float(close.iloc[-1])
    if open_price > 0:
        move_pct = (current_price - open_price) / open_price
    else:
        move_pct = 0.0

    # Composite scoring
    score = 0.0
    if ema_bullish:
        score += 0.3
    else:
        score -= 0.3
    score += (hh_ratio - ll_ratio) * 0.4
    if move_pct > 0.002:
        score += 0.3
    elif move_pct < -0.002:
        score -= 0.3

    if score > 0.15:
        return ("rising", min(1.0, score))
    elif score < -0.15:
        return ("falling", min(1.0, abs(score)))
    return ("flat", abs(score))


def _check_double_sweep(
    today_bars: pd.DataFrame, ibh: float, ibl: float,
) -> bool:
    """Check if today's bars swept both IBH and IBL (whipsaw indicator)."""
    if today_bars is None or today_bars.empty or ibh <= 0 or ibl <= 0:
        return False
    day_high = float(today_bars["High"].max())
    day_low = float(today_bars["Low"].min())
    return day_high > ibh and day_low < ibl


def _check_vwap_divergence(
    today_bars: pd.DataFrame, vwap: float,
) -> str:
    """Check VWAP slope direction from recent bars.

    Returns "rising" / "falling" / "flat".
    """
    if today_bars is None or today_bars.empty or len(today_bars) < 5 or vwap <= 0:
        return "flat"

    # Use recent 10 bars to detect VWAP slope via typical price trend
    recent = today_bars.iloc[-min(10, len(today_bars)):]
    tp = (recent["High"] + recent["Low"] + recent["Close"]) / 3
    cum_vol = recent["Volume"].cumsum()
    cum_tp_vol = (tp * recent["Volume"]).cumsum()
    running_vwap = cum_tp_vol / cum_vol.replace(0, float("nan"))
    running_vwap = running_vwap.dropna()

    if len(running_vwap) < 3:
        return "flat"

    slope = float(running_vwap.iloc[-1] - running_vwap.iloc[0])
    pct = abs(slope) / vwap * 100

    if pct < 0.05:
        return "flat"
    return "rising" if slope > 0 else "falling"


def _calculate_intraday_atr_pct(today_bars: pd.DataFrame) -> float:
    """Calculate intraday ATR as percentage of last close."""
    if today_bars is None or today_bars.empty:
        return 0.0
    day_high = float(today_bars["High"].max())
    day_low = float(today_bars["Low"].min())
    last_close = float(today_bars["Close"].iloc[-1])
    if last_close <= 0:
        return 0.0
    return (day_high - day_low) / last_close * 100


# ── Main classifier ──


def classify_regime(
    price: float,
    rvol: float,
    vp: VolumeProfileResult,
    gamma_wall: GammaWallResult | None = None,
    atm_iv: float = 0.0,
    avg_iv: float = 0.0,
    breakout_rvol: float = 1.2,
    range_rvol: float = 0.8,
    iv_spike_ratio: float = 1.3,
    intraday_range: float = 0.0,
    has_volume_surge: bool = False,
    momentum_min_dist_pct: float = 1.0,
    vwap: float = 0.0,
    open_price: float = 0.0,
    prev_close: float = 0.0,
    today_bars: pd.DataFrame | None = None,
    gap_warning_pct: float = 3.0,
    va_penetration_min_pct: float = 0.3,
    failed_breakout_pct: float = 0.5,
    range_discount_threshold: float = 0.3,
    range_discount_slope: float = 0.5,
    # New params for 5-class system
    ibh: float = 0.0,
    ibl: float = 0.0,
    pdc: float = 0.0,
    day_open: float = 0.0,
    gap_and_go_gap_pct: float = 1.0,
    gap_and_go_rvol: float = 1.2,
    trend_day_rvol: float = 0.0,    # 0 = fallback to breakout_rvol
    fade_chop_rvol: float = 0.0,    # 0 = fallback to range_rvol
    unclear_atr_pct: float = 0.5,
    unclear_vwap_proximity_pct: float = 0.5,
    # Pulse trend + directional trap params
    pulse_peak_ratio: float = 0.0,
    pulse_displacement_pct: float = 0.0,
    peak_rvol: float = 0.0,
    directional_trap_pct: float = 1.5,
    pulse_min_ratio: float = 2.5,
    pulse_min_displacement_pct: float = 1.0,
) -> RegimeResult:
    """Classify current market regime into 5 classes.

    Priority order:
    1. UNCLEAR early-exit (narrow range + low RVOL + near VWAP)
    2. GAP_AND_GO (significant gap + IB confirmation)
    3. WHIPSAW (double IB sweep or IV spike + gamma wall)
    4. TREND_DAY (IB breakout + RVOL + single direction)
    5. FADE_CHOP (inside VA/IB range + low RVOL)
    6. UNCLEAR fallback

    Returns RegimeResult with new regime types. Old breakout_rvol/range_rvol
    auto-map to trend_day_rvol/fade_chop_rvol for backward compatibility.
    """
    # Backward compat: map old params to new if new params are default
    _trend_rvol = trend_day_rvol if trend_day_rvol > 0 else breakout_rvol
    _fade_rvol = fade_chop_rvol if fade_chop_rvol > 0 else range_rvol

    # Use open_price with fallback to day_open param
    _open = open_price if open_price > 0 else day_open
    _pdc = prev_close if prev_close > 0 else pdc

    if vp.poc == 0:
        return RegimeResult(
            regime=RegimeType.UNCLEAR, confidence=0.0,
            rvol=rvol, price=price, vah=vp.vah, val=vp.val, poc=vp.poc,
            details="No volume profile data",
        )

    outside_value = price > vp.vah or price < vp.val
    inside_value = vp.val <= price <= vp.vah

    # Check IV spike
    iv_spiking = False
    if avg_iv > 0 and atm_iv > 0:
        iv_spiking = atm_iv > avg_iv * iv_spike_ratio

    # Check if price is near gamma wall
    near_gamma_wall = False
    if gamma_wall:
        if gamma_wall.call_wall_strike > 0:
            near_gamma_wall = abs(price - gamma_wall.call_wall_strike) / price < 0.01
        if not near_gamma_wall and gamma_wall.put_wall_strike > 0:
            near_gamma_wall = abs(price - gamma_wall.put_wall_strike) / price < 0.01

    # Gap calculation
    gap_pct = 0.0
    if _open > 0 and _pdc > 0:
        gap_pct = (_open - _pdc) / _pdc * 100

    # Direction inference
    direction = ""
    if price > vp.vah:
        direction = "bullish"
    elif price < vp.val:
        direction = "bearish"
    elif vp.poc > 0:
        direction = "bullish" if price > vp.poc else "bearish"

    # ── 1. UNCLEAR early-exit: narrow range + low RVOL + near VWAP ──
    _bars = today_bars if today_bars is not None else pd.DataFrame()
    atr_pct = _calculate_intraday_atr_pct(_bars)
    if (
        atr_pct < unclear_atr_pct
        and rvol < _fade_rvol * 0.7
        and vwap > 0
        and price > 0
        and abs(price - vwap) / price * 100 < unclear_vwap_proximity_pct
    ):
        return RegimeResult(
            regime=RegimeType.UNCLEAR, confidence=0.25,
            rvol=rvol, price=price, vah=vp.vah, val=vp.val, poc=vp.poc,
            details=f"窄幅缩量日: ATR {atr_pct:.2f}% < {unclear_atr_pct}%, RVOL {rvol:.2f}, near VWAP",
            gap_pct=gap_pct, direction=direction, lean="neutral",
        )

    # ── 2. GAP_AND_GO: significant gap + price outside IB / VA ──
    if abs(gap_pct) >= gap_and_go_gap_pct and _open > 0:
        gap_bullish = gap_pct > 0

        # Price must maintain outside IB or outside VA in gap direction
        ib_valid = ibh > 0 and ibl > 0
        ib_confirmed = False
        if ib_valid:
            ib_confirmed = (gap_bullish and price > ibh) or (not gap_bullish and price < ibl)
        va_confirmed = (gap_bullish and price > vp.vah) or (not gap_bullish and price < vp.val)

        # VWAP divergence confirms gap direction
        vwap_dir = _check_vwap_divergence(_bars, vwap)
        vwap_confirms = (
            (gap_bullish and vwap_dir == "rising")
            or (not gap_bullish and vwap_dir == "falling")
        )

        if (ib_confirmed or va_confirmed) and rvol >= gap_and_go_rvol * 0.8:
            base = min(1.0, (rvol - gap_and_go_rvol * 0.8) / 0.5 * 0.4 + 0.55)
            if vwap_confirms:
                base += 0.10
            if not ib_confirmed and not va_confirmed:
                base -= 0.15
            confidence = min(1.0, max(0.0, base))

            gap_dir = "bullish" if gap_bullish else "bearish"
            details = f"Gap {gap_pct:+.2f}%, RVOL {rvol:.2f}, price {'above IBH' if gap_bullish else 'below IBL'}"

            # Discount: gap fade (price retreated past open)
            if gap_bullish and price < _open:
                fade_pct = (_open - price) / _open * 100
                confidence -= min(0.15, fade_pct * 0.05)
                details += f", gap fade ({fade_pct:.1f}%)"
            elif not gap_bullish and price > _open:
                fade_pct = (price - _open) / _open * 100
                confidence -= min(0.15, fade_pct * 0.05)
                details += f", gap fade ({fade_pct:.1f}%)"

            # VWAP contradiction
            if vwap > 0:
                if gap_bullish and price < vwap:
                    confidence -= 0.20
                    details += ", VWAP contradiction"
                elif not gap_bullish and price > vwap:
                    confidence -= 0.20
                    details += ", VWAP contradiction"

            confidence = max(0.0, confidence)
            if confidence < 0.35:
                return RegimeResult(
                    regime=RegimeType.UNCLEAR, confidence=confidence,
                    rvol=rvol, price=price, vah=vp.vah, val=vp.val, poc=vp.poc,
                    details=details + " → downgraded to UNCLEAR",
                    gap_pct=gap_pct, direction=gap_dir, lean=gap_dir,
                )

            return RegimeResult(
                regime=RegimeType.GAP_AND_GO, confidence=confidence,
                rvol=rvol, price=price, vah=vp.vah, val=vp.val, poc=vp.poc,
                details=details,
                gap_pct=gap_pct, direction=gap_dir,
            )

    # ── 3. WHIPSAW: double IB sweep OR IV spike + gamma wall ──
    ib_valid = ibh > 0 and ibl > 0
    double_swept = _check_double_sweep(_bars, ibh, ibl) if ib_valid else False

    if double_swept:
        # Price swept both sides of IB → classic whipsaw
        confidence = 0.65
        return RegimeResult(
            regime=RegimeType.WHIPSAW, confidence=confidence,
            rvol=rvol, price=price, vah=vp.vah, val=vp.val, poc=vp.poc,
            details=f"Double IB sweep (IBH {ibh:.2f}, IBL {ibl:.2f}), RVOL {rvol:.2f}",
            gap_pct=gap_pct, direction=direction,
        )

    if iv_spiking and near_gamma_wall:
        # IV spike + near Gamma Wall (original WHIPSAW logic, fallback when IB not formed)
        confidence = 0.6
        return RegimeResult(
            regime=RegimeType.WHIPSAW, confidence=confidence,
            rvol=rvol, price=price, vah=vp.vah, val=vp.val, poc=vp.poc,
            details=f"IV spike ({atm_iv:.1f}% vs avg {avg_iv:.1f}%), price near Gamma wall",
            gap_pct=gap_pct, direction=direction,
        )

    # ── 4. TREND_DAY: IB breakout + RVOL + single direction ──
    ib_breakout = False
    if ib_valid:
        ib_breakout = price > ibh or price < ibl

    _effective_rvol = max(rvol, peak_rvol)
    if _effective_rvol >= _trend_rvol and (outside_value or ib_breakout):
        trend_direction = "above VAH" if price > vp.vah else "below VAL"
        if not outside_value and ib_breakout:
            trend_direction = "above IBH" if price > ibh else "below IBL"

        base = min(1.0, (_effective_rvol - _trend_rvol) / 0.5 * 0.5 + 0.5)
        va_adj = _price_va_distance_factor(price, vp.vah, vp.val, "TREND") * 0.1
        gw_adj = -0.05 if near_gamma_wall else 0.0
        confidence = min(1.0, max(0.0, base + va_adj + gw_adj))
        details = f"RVOL {rvol:.2f} >= {_trend_rvol}, price {trend_direction}"

        # Discount layers (same as old BREAKOUT)
        has_vwap_contradiction = False
        has_trend_contradiction = False

        if vwap > 0:
            if price > vp.vah and price < vwap:
                confidence -= 0.20
                details += ", VWAP contradiction (above VAH but below VWAP)"
                has_vwap_contradiction = True
            elif price < vp.val and price > vwap:
                confidence -= 0.20
                details += ", VWAP contradiction (below VAL but above VWAP)"
                has_vwap_contradiction = True

        va_range = vp.vah - vp.val
        if va_range > 0 and va_penetration_min_pct > 0:
            if price > vp.vah:
                penetration_pct = (price - vp.vah) / price * 100
            else:
                penetration_pct = (vp.val - price) / price * 100 if price < vp.val else 0
            if outside_value and penetration_pct < va_penetration_min_pct:
                confidence -= 0.15
                details += f", shallow penetration ({penetration_pct:.2f}%)"

        trend_dir, trend_strength = _intraday_trend(_bars)
        if trend_strength >= 0.5:
            if price > vp.vah and trend_dir == "falling":
                confidence -= 0.20
                details += ", trend contradiction (falling)"
                has_trend_contradiction = True
            elif price < vp.val and trend_dir == "rising":
                confidence -= 0.20
                details += ", trend contradiction (rising)"
                has_trend_contradiction = True

        # Gap fade
        if _pdc > 0 and _open > 0:
            local_gap = (_open - _pdc) / _pdc * 100
            if abs(local_gap) >= gap_warning_pct:
                if local_gap > 0 and price < _open:
                    fade_pct = (_open - price) / _open * 100
                    confidence -= min(0.15, fade_pct * 0.05)
                    details += f", gap fade (gap +{local_gap:.1f}%, faded {fade_pct:.1f}%)"
                elif local_gap < 0 and price > _open:
                    fade_pct = (price - _open) / _open * 100
                    confidence -= min(0.15, fade_pct * 0.05)
                    details += f", gap fade (gap {local_gap:.1f}%, faded {fade_pct:.1f}%)"

        confidence = max(0.0, confidence)

        # Failed breakout detection (from old RANGE logic — moved into TREND discount)
        if today_bars is not None and not today_bars.empty and vp.vah > 0 and vp.val > 0:
            today_high = float(today_bars["High"].max())
            today_low = float(today_bars["Low"].min())
            if price < vp.vah and today_high > vp.vah:
                breach_pct = (today_high - vp.vah) / vp.vah * 100
                if breach_pct >= failed_breakout_pct:
                    confidence -= 0.15
                    details += f", failed breakout above VAH ({breach_pct:.1f}%)"
            if price > vp.val and today_low < vp.val:
                breach_pct = (vp.val - today_low) / vp.val * 100
                if breach_pct >= failed_breakout_pct:
                    confidence -= 0.15
                    details += f", failed breakout below VAL ({breach_pct:.1f}%)"
            confidence = max(0.0, confidence)

        if confidence < 0.40 and (has_vwap_contradiction or has_trend_contradiction):
            return RegimeResult(
                regime=RegimeType.UNCLEAR, confidence=confidence,
                rvol=rvol, price=price, vah=vp.vah, val=vp.val, poc=vp.poc,
                details=details + " → downgraded to UNCLEAR",
                gap_pct=gap_pct, direction=direction,
            )

        td_direction = "bullish" if price > vp.poc else "bearish"
        return RegimeResult(
            regime=RegimeType.TREND_DAY, confidence=confidence,
            rvol=rvol, price=price, vah=vp.vah, val=vp.val, poc=vp.poc,
            details=details,
            gap_pct=gap_pct, direction=td_direction,
        )

    # ── 4b. Momentum Trend — outside VA, low RVOL but significant distance ──
    if outside_value and _effective_rvol < _trend_rvol:
        if price > vp.vah:
            va_dist_pct = (price - vp.vah) / price * 100
        else:
            va_dist_pct = (vp.val - price) / price * 100
        if va_dist_pct >= momentum_min_dist_pct:
            direction_str = "above VAH" if price > vp.vah else "below VAL"
            base = 0.40 + min(0.15, (va_dist_pct - momentum_min_dist_pct) / 3.0 * 0.15)
            rvol_adj = 0.0
            if _trend_rvol > _fade_rvol:
                rvol_adj = min(0.05, max(0.0, rvol - _fade_rvol) / (_trend_rvol - _fade_rvol) * 0.05)
            surge_adj = 0.10 if has_volume_surge else 0.0
            gw_adj = -0.05 if near_gamma_wall else 0.0
            confidence = min(0.65, max(0.40, base + rvol_adj + surge_adj + gw_adj))
            details_parts = [
                f"Momentum: price {va_dist_pct:.1f}% {direction_str}",
                f"RVOL {rvol:.2f} < {_trend_rvol}",
            ]
            if has_volume_surge:
                details_parts.append("volume surge detected")

            has_vwap_contradiction = False
            has_trend_contradiction = False
            if vwap > 0:
                if price > vp.vah and price < vwap:
                    confidence -= 0.20
                    details_parts.append("VWAP contradiction")
                    has_vwap_contradiction = True
                elif price < vp.val and price > vwap:
                    confidence -= 0.20
                    details_parts.append("VWAP contradiction")
                    has_vwap_contradiction = True

            trend_dir, trend_strength = _intraday_trend(_bars)
            if trend_strength >= 0.5:
                if price > vp.vah and trend_dir == "falling":
                    confidence -= 0.20
                    details_parts.append("trend contradiction (falling)")
                    has_trend_contradiction = True
                elif price < vp.val and trend_dir == "rising":
                    confidence -= 0.20
                    details_parts.append("trend contradiction (rising)")
                    has_trend_contradiction = True

            confidence = max(0.0, confidence)
            details_str = ", ".join(details_parts)

            if confidence < 0.40 and (has_vwap_contradiction or has_trend_contradiction):
                return RegimeResult(
                    regime=RegimeType.UNCLEAR, confidence=confidence,
                    rvol=rvol, price=price, vah=vp.vah, val=vp.val, poc=vp.poc,
                    details=details_str + " → downgraded to UNCLEAR",
                    gap_pct=gap_pct, direction=direction,
                )

            td_direction = "bullish" if price > vp.poc else "bearish"
            return RegimeResult(
                regime=RegimeType.TREND_DAY, confidence=confidence,
                rvol=rvol, price=price, vah=vp.vah, val=vp.val, poc=vp.poc,
                details=details_str,
                gap_pct=gap_pct, direction=td_direction,
            )

    # ── 4c. Pulse Trend — IB breakout + volume pulse despite low RVOL ──
    if (
        _effective_rvol < _trend_rvol
        and ib_breakout
        and pulse_peak_ratio >= pulse_min_ratio
        and pulse_displacement_pct >= pulse_min_displacement_pct
    ):
        pulse_dir = "bullish" if price > ibh else "bearish"
        surge_adj = min(0.05, (pulse_peak_ratio - pulse_min_ratio) / 5.0 * 0.05)
        ib_adj = 0.05 if outside_value else 0.0
        confidence = min(0.60, 0.45 + surge_adj + ib_adj)

        details = (
            f"Pulse Trend: peak {pulse_peak_ratio:.1f}x, displacement {pulse_displacement_pct:.2f}%, "
            f"RVOL {rvol:.2f}"
        )

        # VWAP contradiction penalty
        if vwap > 0:
            if pulse_dir == "bullish" and price < vwap:
                confidence -= 0.15
                details += ", VWAP contradiction"
            elif pulse_dir == "bearish" and price > vwap:
                confidence -= 0.15
                details += ", VWAP contradiction"

        confidence = max(0.0, confidence)
        return RegimeResult(
            regime=RegimeType.TREND_DAY, confidence=confidence,
            rvol=rvol, price=price, vah=vp.vah, val=vp.val, poc=vp.poc,
            details=details,
            gap_pct=gap_pct, direction=pulse_dir,
        )

    # ── 4d. Directional Trap — low RVOL but large price displacement from open ──
    if (
        _effective_rvol < _fade_rvol
        and _open > 0
        and abs(price - _open) / _open * 100 > directional_trap_pct
    ):
        trap_dir = "bullish" if price > _open else "bearish"
        return RegimeResult(
            regime=RegimeType.UNCLEAR, confidence=0.30,
            rvol=rvol, price=price, vah=vp.vah, val=vp.val, poc=vp.poc,
            details=f"Directional trap: price moved {abs(price - _open) / _open * 100:.1f}% from open, RVOL {rvol:.2f} < {_fade_rvol}",
            gap_pct=gap_pct, direction=trap_dir, lean=trap_dir,
        )

    # ── 5. FADE_CHOP: inside VA + low RVOL ──
    if rvol <= _fade_rvol and inside_value:
        base = min(1.0, (_fade_rvol - rvol) / 0.3 * 0.5 + 0.5)
        va_adj = _price_va_distance_factor(price, vp.vah, vp.val, "RANGE") * 0.1
        gw_adj = 0.05 if near_gamma_wall else 0.0
        confidence = min(1.0, max(0.0, base + va_adj + gw_adj))
        details = f"RVOL {rvol:.2f} < {_fade_rvol}, price in value area"

        # Failed breakout detection
        _fb_pct = failed_breakout_pct
        today_high = 0.0
        today_low = float("inf")
        if today_bars is not None and not today_bars.empty:
            today_high = float(today_bars["High"].max())
            today_low = float(today_bars["Low"].min())
        if vp.vah > 0 and today_high > vp.vah:
            breach_pct = (today_high - vp.vah) / vp.vah * 100
            if breach_pct >= _fb_pct:
                confidence -= 0.20
                details += f", failed breakout above VAH ({breach_pct:.1f}%)"
        if vp.val > 0 and today_low < float("inf") and today_low < vp.val:
            breach_pct = (vp.val - today_low) / vp.val * 100
            if breach_pct >= _fb_pct:
                confidence -= 0.20
                details += f", failed breakout below VAL ({breach_pct:.1f}%)"
        confidence = max(0.0, confidence)

        # Spike-and-fade markers
        if today_high > vp.vah and price < vp.vah:
            details += ", spike-and-fade above VAH"
        if today_low < float("inf") and today_low < vp.val and price > vp.val:
            details += ", spike-and-fade below VAL"

        # Discount for wide intraday range
        va_range = vp.vah - vp.val
        if intraday_range > 0 and va_range > 0:
            range_ratio = intraday_range / va_range
            if range_ratio > range_discount_threshold:
                discount = min(0.3, (range_ratio - range_discount_threshold) * range_discount_slope)
                confidence = max(0.0, confidence - discount)
                details += f" (振幅占VA {range_ratio:.0%}, 置信度折扣)"

        # Volume surge near VA edge
        if has_volume_surge and va_range > 0:
            dist_to_vah = abs(vp.vah - price) / va_range
            dist_to_val = abs(price - vp.val) / va_range
            near_edge = min(dist_to_vah, dist_to_val)
            if near_edge < 0.35:
                surge_discount = 0.15
                confidence = max(0.0, confidence - surge_discount)
                details += ", volume surge near VA edge"
                if confidence < 0.40:
                    return RegimeResult(
                        regime=RegimeType.UNCLEAR, confidence=confidence,
                        rvol=rvol, price=price, vah=vp.vah, val=vp.val, poc=vp.poc,
                        details=details + " → downgraded to UNCLEAR",
                        gap_pct=gap_pct, direction=direction,
                    )

        # Momentum discount: large unidirectional move toward VA edge
        if _open > 0 and va_range > 0:
            open_to_current_pct = abs(price - _open) / _open * 100
            dist_to_nearest_edge = min(abs(price - vp.vah), abs(price - vp.val))
            near_edge_ratio = dist_to_nearest_edge / va_range
            if open_to_current_pct > 1.2 and near_edge_ratio < 0.25:
                momentum_discount = min(0.20, (open_to_current_pct - 1.2) * 0.10)
                confidence = max(0.0, confidence - momentum_discount)
                details += f", momentum discount ({open_to_current_pct:.1f}% from open, near edge {near_edge_ratio:.2f})"
                if confidence < 0.35:
                    return RegimeResult(
                        regime=RegimeType.UNCLEAR, confidence=confidence,
                        rvol=rvol, price=price, vah=vp.vah, val=vp.val, poc=vp.poc,
                        details=details + " → downgraded to UNCLEAR",
                        gap_pct=gap_pct, direction=direction,
                    )

        fc_direction = "bearish" if price > vp.poc else "bullish"
        return RegimeResult(
            regime=RegimeType.FADE_CHOP, confidence=confidence,
            rvol=rvol, price=price, vah=vp.vah, val=vp.val, poc=vp.poc,
            details=details,
            gap_pct=gap_pct, direction=fc_direction,
        )

    # ── 6. UNCLEAR fallback ──
    parts = []
    lean = "neutral"
    if _trend_rvol > rvol > _fade_rvol:
        parts.append(f"RVOL {rvol:.2f} in neutral zone")
    if inside_value and rvol >= _trend_rvol:
        parts.append("High volume but price in value area")
        # Buildup sub-type: lean toward eventual breakout direction
        if vwap > 0:
            lean = "bullish" if price > vwap else "bearish"
    if outside_value and rvol <= _fade_rvol:
        parts.append("Price outside value but low volume")
        # False breakout sub-type: lean toward mean reversion
        lean = "bearish" if price > vp.vah else "bullish"

    return RegimeResult(
        regime=RegimeType.UNCLEAR, confidence=0.3,
        rvol=rvol, price=price, vah=vp.vah, val=vp.val, poc=vp.poc,
        details="; ".join(parts) if parts else "Mixed signals",
        gap_pct=gap_pct, direction=direction, lean=lean,
    )
