from __future__ import annotations

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
        return RegimeResult(
            regime=RegimeType.BREAKOUT, confidence=confidence,
            rvol=rvol, price=price, vah=vp.vah, val=vp.val, poc=vp.poc,
            details=f"RVOL {rvol:.2f} > {breakout_rvol}, price {direction}",
        )

    # Style B: Range
    if rvol <= range_rvol and inside_value:
        base = min(1.0, (range_rvol - rvol) / 0.3 * 0.5 + 0.5)
        va_adj = _price_va_distance_factor(price, vp.vah, vp.val, "RANGE") * 0.1
        gw_adj = 0.05 if near_gamma_wall else 0.0
        confidence = min(1.0, max(0.0, base + va_adj + gw_adj))

        # Discount confidence when intraday range is wide relative to VA
        details = f"RVOL {rvol:.2f} < {range_rvol}, price in value area"
        va_range = vp.vah - vp.val
        if intraday_range > 0 and va_range > 0:
            range_ratio = intraday_range / va_range
            if range_ratio > 0.3:
                discount = min(0.3, (range_ratio - 0.3) * 0.5)
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
            return RegimeResult(
                regime=RegimeType.BREAKOUT, confidence=confidence,
                rvol=rvol, price=price, vah=vp.vah, val=vp.val, poc=vp.poc,
                details=", ".join(details_parts),
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
