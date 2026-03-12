from __future__ import annotations

import pandas as pd

from src.hk import RegimeType, RegimeResult, VolumeProfileResult, GammaWallResult
from src.utils.logger import setup_logger

logger = setup_logger("hk_regime")


def _price_va_distance_factor(
    price: float, vah: float, val: float, regime: str,
) -> float:
    """Return 0~1 factor based on price distance from VA boundary.

    BREAKOUT: farther from VA edge → higher (deeper breakout = more conviction).
    RANGE: deeper inside VA center → higher.
    """
    va_range = vah - val
    if va_range <= 0:
        return 0.0

    if regime == "BREAKOUT":
        if price > vah:
            dist = (price - vah) / va_range
        else:
            dist = (val - price) / va_range
        return min(1.0, max(0.0, dist))

    # RANGE: distance from nearest VA edge, normalized to half-range
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
) -> RegimeResult:
    """Classify current market regime.

    Returns RegimeResult with type, confidence and details.
    """
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

    # Check if price is near gamma wall (check each wall independently)
    near_gamma_wall = False
    if gamma_wall:
        if gamma_wall.call_wall_strike > 0:
            near_gamma_wall = abs(price - gamma_wall.call_wall_strike) / price < 0.01
        if not near_gamma_wall and gamma_wall.put_wall_strike > 0:
            near_gamma_wall = abs(price - gamma_wall.put_wall_strike) / price < 0.01

    # Style C: Whipsaw — highest priority (IV spike + near Gamma Wall is most dangerous)
    if iv_spiking and near_gamma_wall:
        confidence = 0.6
        return RegimeResult(
            regime=RegimeType.WHIPSAW, confidence=confidence,
            rvol=rvol, price=price, vah=vp.vah, val=vp.val, poc=vp.poc,
            details=f"IV spike ({atm_iv:.1f}% vs avg {avg_iv:.1f}%), price near Gamma wall",
        )

    # Style A: Breakout
    if rvol >= breakout_rvol and outside_value:
        direction = "above VAH" if price > vp.vah else "below VAL"
        base = min(1.0, (rvol - breakout_rvol) / 0.5 * 0.5 + 0.5)
        va_adj = _price_va_distance_factor(price, vp.vah, vp.val, "BREAKOUT") * 0.1
        gw_adj = -0.05 if near_gamma_wall else 0.0
        confidence = min(1.0, max(0.0, base + va_adj + gw_adj))
        details = f"RVOL {rvol:.2f} > {breakout_rvol}, price {direction}"

        # ── Breakout discount layers ──
        has_vwap_contradiction = False
        has_trend_contradiction = False

        # 3a. VWAP contradiction
        if vwap > 0:
            if price > vp.vah and price < vwap:
                confidence -= 0.20
                details += ", VWAP contradiction (above VAH but below VWAP)"
                has_vwap_contradiction = True
            elif price < vp.val and price > vwap:
                confidence -= 0.20
                details += ", VWAP contradiction (below VAL but above VWAP)"
                has_vwap_contradiction = True

        # 3b. Shallow VA penetration
        va_range = vp.vah - vp.val
        if va_range > 0 and va_penetration_min_pct > 0:
            if price > vp.vah:
                penetration_pct = (price - vp.vah) / price * 100
            else:
                penetration_pct = (vp.val - price) / price * 100
            if penetration_pct < va_penetration_min_pct:
                confidence -= 0.15
                details += f", shallow penetration ({penetration_pct:.2f}%)"

        # 3c. Intraday trend contradiction
        trend_dir, trend_strength = _intraday_trend(today_bars)
        if trend_strength >= 0.5:
            if price > vp.vah and trend_dir == "falling":
                confidence -= 0.20
                details += ", trend contradiction (falling)"
                has_trend_contradiction = True
            elif price < vp.val and trend_dir == "rising":
                confidence -= 0.20
                details += ", trend contradiction (rising)"
                has_trend_contradiction = True

        # 3d. Gap fade
        if prev_close > 0 and open_price > 0:
            gap_pct = (open_price - prev_close) / prev_close * 100
            if abs(gap_pct) >= gap_warning_pct:
                # Price faded back past open
                if gap_pct > 0 and price < open_price:
                    fade_pct = (open_price - price) / open_price * 100
                    confidence -= min(0.15, fade_pct * 0.05)
                    details += f", gap fade (gap +{gap_pct:.1f}%, faded {fade_pct:.1f}%)"
                elif gap_pct < 0 and price > open_price:
                    fade_pct = (price - open_price) / open_price * 100
                    confidence -= min(0.15, fade_pct * 0.05)
                    details += f", gap fade (gap {gap_pct:.1f}%, faded {fade_pct:.1f}%)"

        confidence = max(0.0, confidence)

        # 3e. Downgrade to UNCLEAR if too many contradictions
        if confidence < 0.40 and (has_vwap_contradiction or has_trend_contradiction):
            return RegimeResult(
                regime=RegimeType.UNCLEAR, confidence=confidence,
                rvol=rvol, price=price, vah=vp.vah, val=vp.val, poc=vp.poc,
                details=details + " → downgraded to UNCLEAR",
            )

        return RegimeResult(
            regime=RegimeType.BREAKOUT, confidence=confidence,
            rvol=rvol, price=price, vah=vp.vah, val=vp.val, poc=vp.poc,
            details=details,
        )

    # Style B: Range
    if rvol <= range_rvol and inside_value:
        base = min(1.0, (range_rvol - rvol) / 0.3 * 0.5 + 0.5)
        va_adj = _price_va_distance_factor(price, vp.vah, vp.val, "RANGE") * 0.1
        gw_adj = 0.05 if near_gamma_wall else 0.0
        confidence = min(1.0, max(0.0, base + va_adj + gw_adj))

        # Discount confidence when intraday range is wide relative to VA
        details = f"RVOL {rvol:.2f} < {range_rvol}, price in value area"

        # Failed breakout detection: today's price breached VA boundary
        # then retreated — the level has been tested and may not hold
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

        # Spike-and-fade marker (informational): today touched VAH/VAL but retreated
        # Price is back inside VA → level was tested, adds context to playbook
        if today_high > vp.vah and price < vp.vah:
            details += ", spike-and-fade above VAH"
        if today_low < float("inf") and today_low < vp.val and price > vp.val:
            details += ", spike-and-fade below VAL"

        va_range = vp.vah - vp.val
        if intraday_range > 0 and va_range > 0:
            range_ratio = intraday_range / va_range
            if range_ratio > range_discount_threshold:
                discount = min(0.3, (range_ratio - range_discount_threshold) * range_discount_slope)
                confidence = max(0.0, confidence - discount)
                details += f" (振幅占VA {range_ratio:.0%}, 置信度折扣)"

        # Volume surge during RANGE: price trending toward VA boundary
        # may signal impending breakout — downgrade confidence
        if has_volume_surge and va_range > 0:
            dist_to_vah = abs(vp.vah - price) / va_range
            dist_to_val = abs(price - vp.val) / va_range
            near_edge = min(dist_to_vah, dist_to_val)
            if near_edge < 0.35:
                # Price near VA edge with volume surge → potential breakout
                surge_discount = 0.15
                confidence = max(0.0, confidence - surge_discount)
                details += ", volume surge near VA edge"
                if confidence < 0.40:
                    return RegimeResult(
                        regime=RegimeType.UNCLEAR, confidence=confidence,
                        rvol=rvol, price=price, vah=vp.vah, val=vp.val, poc=vp.poc,
                        details=details + " → downgraded to UNCLEAR",
                    )

        return RegimeResult(
            regime=RegimeType.RANGE, confidence=confidence,
            rvol=rvol, price=price, vah=vp.vah, val=vp.val, poc=vp.poc,
            details=details,
        )

    # Style A2: Momentum Breakout — price significantly outside VA, low RVOL
    if outside_value and rvol < breakout_rvol:
        if price > vp.vah:
            va_dist_pct = (price - vp.vah) / price * 100
        else:
            va_dist_pct = (vp.val - price) / price * 100
        if va_dist_pct >= momentum_min_dist_pct:
            direction = "above VAH" if price > vp.vah else "below VAL"
            base = 0.40 + min(0.15, (va_dist_pct - momentum_min_dist_pct) / 3.0 * 0.15)
            rvol_adj = 0.0
            if breakout_rvol > range_rvol:
                rvol_adj = min(0.05, max(0.0, rvol - range_rvol) / (breakout_rvol - range_rvol) * 0.05)
            surge_adj = 0.10 if has_volume_surge else 0.0
            gw_adj = -0.05 if near_gamma_wall else 0.0
            confidence = min(0.65, max(0.40, base + rvol_adj + surge_adj + gw_adj))
            details_parts = [
                f"Momentum: price {va_dist_pct:.1f}% {direction}",
                f"RVOL {rvol:.2f} < {breakout_rvol}",
            ]
            if has_volume_surge:
                details_parts.append("volume surge detected")

            # ── Momentum breakout discount layers (VWAP + trend) ──
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

            trend_dir, trend_strength = _intraday_trend(today_bars)
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
            details = ", ".join(details_parts)

            if confidence < 0.40 and (has_vwap_contradiction or has_trend_contradiction):
                return RegimeResult(
                    regime=RegimeType.UNCLEAR, confidence=confidence,
                    rvol=rvol, price=price, vah=vp.vah, val=vp.val, poc=vp.poc,
                    details=details + " → downgraded to UNCLEAR",
                )

            return RegimeResult(
                regime=RegimeType.BREAKOUT, confidence=confidence,
                rvol=rvol, price=price, vah=vp.vah, val=vp.val, poc=vp.poc,
                details=details,
            )

    # Style D: Unclear
    parts = []
    if breakout_rvol > rvol > range_rvol:
        parts.append(f"RVOL {rvol:.2f} in neutral zone")
    if inside_value and rvol >= breakout_rvol:
        parts.append("High volume but price in value area")
    if outside_value and rvol <= range_rvol:
        parts.append("Price outside value but low volume")

    return RegimeResult(
        regime=RegimeType.UNCLEAR, confidence=0.3,
        rvol=rvol, price=price, vah=vp.vah, val=vp.val, poc=vp.poc,
        details="; ".join(parts) if parts else "Mixed signals",
    )
