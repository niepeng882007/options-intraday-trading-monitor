from __future__ import annotations

from src.hk import RegimeType, RegimeResult, VolumeProfileResult, GammaWallResult
from src.utils.logger import setup_logger

logger = setup_logger("hk_regime")


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

    # Check if price is near gamma wall
    near_gamma_wall = False
    if gamma_wall and gamma_wall.call_wall_strike > 0:
        call_dist = abs(price - gamma_wall.call_wall_strike) / price
        put_dist = abs(price - gamma_wall.put_wall_strike) / price
        near_gamma_wall = call_dist < 0.01 or put_dist < 0.01

    # Style A: Breakout
    if rvol >= breakout_rvol and outside_value:
        direction = "above VAH" if price > vp.vah else "below VAL"
        confidence = min(1.0, (rvol - breakout_rvol) / 0.5 * 0.5 + 0.5)
        return RegimeResult(
            regime=RegimeType.BREAKOUT, confidence=confidence,
            rvol=rvol, price=price, vah=vp.vah, val=vp.val, poc=vp.poc,
            details=f"RVOL {rvol:.2f} > {breakout_rvol}, price {direction}",
        )

    # Style C: Whipsaw (check before B — IV spike overrides range)
    if iv_spiking and near_gamma_wall:
        confidence = 0.6
        return RegimeResult(
            regime=RegimeType.WHIPSAW, confidence=confidence,
            rvol=rvol, price=price, vah=vp.vah, val=vp.val, poc=vp.poc,
            details=f"IV spike ({atm_iv:.1f}% vs avg {avg_iv:.1f}%), price near Gamma wall",
        )

    # Style B: Range
    if rvol <= range_rvol and inside_value:
        confidence = min(1.0, (range_rvol - rvol) / 0.3 * 0.5 + 0.5)
        return RegimeResult(
            regime=RegimeType.RANGE, confidence=confidence,
            rvol=rvol, price=price, vah=vp.vah, val=vp.val, poc=vp.poc,
            details=f"RVOL {rvol:.2f} < {range_rvol}, price in value area",
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
