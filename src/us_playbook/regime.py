from __future__ import annotations

from src.hk import GammaWallResult, VolumeProfileResult
from src.us_playbook import USRegimeResult, USRegimeType
from src.utils.logger import setup_logger

logger = setup_logger("us_regime")


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
) -> USRegimeResult:
    """Classify US intraday regime into 4 styles.

    Styles:
        GAP_AND_GO: Gap + high RVOL + price beyond PM range
        TREND_DAY: Moderate RVOL + directional, no big gap
        FADE_CHOP: Low RVOL + range-bound
        UNCLEAR: Mixed signals
    """
    if vp.poc == 0:
        return USRegimeResult(
            regime=USRegimeType.UNCLEAR, confidence=0.0,
            rvol=rvol, price=price, gap_pct=0.0,
            details="No volume profile data",
        )

    # Gap calculation
    gap_pct = ((price - prev_close) / prev_close * 100) if prev_close > 0 else 0.0

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

    # ── GAP_AND_GO ──
    if rvol >= gap_and_go_rvol and pm_breakout:
        direction = "above PMH" if above_pm else "below PML"
        confidence = min(1.0, (rvol - gap_and_go_rvol) / 0.5 * 0.3 + 0.6)
        # SPY context adjustment
        if spy_regime == USRegimeType.FADE_CHOP:
            confidence = max(0.1, confidence - 0.2)
        elif spy_regime == USRegimeType.GAP_AND_GO:
            confidence = min(1.0, confidence + 0.1)
        return USRegimeResult(
            regime=USRegimeType.GAP_AND_GO, confidence=confidence,
            rvol=rvol, price=price, gap_pct=gap_pct,
            spy_regime=spy_regime,
            details=f"RVOL {rvol:.2f} >= {gap_and_go_rvol}, price {direction}, gap {gap_pct:+.2f}%",
        )

    # ── TREND_DAY ──
    if rvol >= trend_day_rvol and abs(gap_pct) < 0.5 and outside_va:
        direction = "above VAH" if price > vp.vah else "below VAL"
        confidence = min(1.0, (rvol - trend_day_rvol) / 0.5 * 0.3 + 0.5)
        if spy_regime == USRegimeType.FADE_CHOP:
            confidence = max(0.1, confidence - 0.15)
        return USRegimeResult(
            regime=USRegimeType.TREND_DAY, confidence=confidence,
            rvol=rvol, price=price, gap_pct=gap_pct,
            spy_regime=spy_regime,
            details=f"RVOL {rvol:.2f} >= {trend_day_rvol}, small gap {gap_pct:+.2f}%, price {direction}",
        )

    # ── FADE_CHOP ──
    if rvol < fade_chop_rvol and (inside_va or near_gamma):
        reason = "in value area" if inside_va else "near Gamma wall"
        confidence = min(1.0, (fade_chop_rvol - rvol) / 0.3 * 0.3 + 0.5)
        return USRegimeResult(
            regime=USRegimeType.FADE_CHOP, confidence=confidence,
            rvol=rvol, price=price, gap_pct=gap_pct,
            spy_regime=spy_regime,
            details=f"RVOL {rvol:.2f} < {fade_chop_rvol}, price {reason}",
        )

    # ── UNCLEAR ──
    parts = []
    if trend_day_rvol > rvol >= fade_chop_rvol:
        parts.append(f"RVOL {rvol:.2f} in neutral zone")
    if inside_va and rvol >= trend_day_rvol:
        parts.append("High volume but price in value area")
    if outside_va and rvol < fade_chop_rvol:
        parts.append("Price outside VA but low volume")

    return USRegimeResult(
        regime=USRegimeType.UNCLEAR, confidence=0.3,
        rvol=rvol, price=price, gap_pct=gap_pct,
        spy_regime=spy_regime,
        details="; ".join(parts) if parts else "Mixed signals",
    )
