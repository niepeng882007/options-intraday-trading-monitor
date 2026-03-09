from __future__ import annotations

from src.hk import GammaWallResult, VolumeProfileResult
from src.us_playbook import USRegimeResult, USRegimeType
from src.us_playbook.indicators import RvolProfile
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
    vp_trading_days: int = 0,
    min_vp_trading_days: int = 3,
    rvol_profile: RvolProfile | None = None,
    gap_significance_threshold: float = 0.3,
    pm_source: str = "futu",
) -> USRegimeResult:
    """Classify US intraday regime into 4 styles.

    Styles:
        GAP_AND_GO: Gap + high RVOL + price beyond PM range
        TREND_DAY: Moderate RVOL + directional, no big gap
        FADE_CHOP: Low RVOL + range-bound
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

    # Gap calculation
    gap_pct = ((price - prev_close) / prev_close * 100) if prev_close > 0 else 0.0

    # Normalized gap: use daily range if available from adaptive profile
    if rvol_profile and rvol_profile.avg_daily_range_pct > 0:
        normalized_gap = abs(gap_pct) / rvol_profile.avg_daily_range_pct
        small_gap = normalized_gap < gap_significance_threshold
    else:
        small_gap = abs(gap_pct) < 0.5  # fallback static

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

    result: USRegimeResult | None = None

    # ── GAP_AND_GO ──
    if rvol >= gap_and_go_rvol and pm_breakout:
        direction = "above PMH" if above_pm else "below PML"
        confidence = min(1.0, (rvol - gap_and_go_rvol) / 0.5 * 0.3 + 0.6)
        # SPY context adjustment
        if spy_regime == USRegimeType.FADE_CHOP:
            confidence = max(0.1, confidence - 0.2)
        elif spy_regime == USRegimeType.GAP_AND_GO:
            confidence = min(1.0, confidence + 0.1)
        result = USRegimeResult(
            regime=USRegimeType.GAP_AND_GO, confidence=confidence,
            rvol=rvol, price=price, gap_pct=gap_pct,
            spy_regime=spy_regime,
            adaptive_thresholds=adaptive_info,
            details=f"RVOL {rvol:.2f} >= {gap_and_go_rvol:.2f} ({threshold_label}), price {direction}, gap {gap_pct:+.2f}%",
        )

    # ── TREND_DAY ──
    if result is None and rvol >= trend_day_rvol and small_gap and outside_va:
        direction = "above VAH" if price > vp.vah else "below VAL"
        confidence = min(1.0, (rvol - trend_day_rvol) / 0.5 * 0.3 + 0.5)
        if spy_regime == USRegimeType.FADE_CHOP:
            confidence = max(0.1, confidence - 0.15)
        result = USRegimeResult(
            regime=USRegimeType.TREND_DAY, confidence=confidence,
            rvol=rvol, price=price, gap_pct=gap_pct,
            spy_regime=spy_regime,
            adaptive_thresholds=adaptive_info,
            details=f"RVOL {rvol:.2f} >= {trend_day_rvol:.2f} ({threshold_label}), small gap {gap_pct:+.2f}%, price {direction}",
        )

    # ── FADE_CHOP ──
    if result is None and rvol < fade_chop_rvol and (inside_va or near_gamma):
        reason = "in value area" if inside_va else "near Gamma wall"
        confidence = min(1.0, (fade_chop_rvol - rvol) / 0.3 * 0.3 + 0.5)
        result = USRegimeResult(
            regime=USRegimeType.FADE_CHOP, confidence=confidence,
            rvol=rvol, price=price, gap_pct=gap_pct,
            spy_regime=spy_regime,
            adaptive_thresholds=adaptive_info,
            details=f"RVOL {rvol:.2f} < {fade_chop_rvol:.2f} ({threshold_label}), price {reason}",
        )

    # ── UNCLEAR ──
    if result is None:
        parts = []
        if trend_day_rvol > rvol >= fade_chop_rvol:
            parts.append(f"RVOL {rvol:.2f} in neutral zone")
        if inside_va and rvol >= trend_day_rvol:
            parts.append("High volume but price in value area")
        if outside_va and rvol < fade_chop_rvol:
            parts.append("Price outside VA but low volume")
        result = USRegimeResult(
            regime=USRegimeType.UNCLEAR, confidence=0.3,
            rvol=rvol, price=price, gap_pct=gap_pct,
            spy_regime=spy_regime,
            adaptive_thresholds=adaptive_info,
            details="; ".join(parts) if parts else "Mixed signals",
        )

    # ── VP thin data penalty ──
    if 0 < vp_trading_days < min_vp_trading_days:
        result.confidence = max(0.0, result.confidence - 0.15)
        thin_note = f"VP thin ({vp_trading_days}d)"
        result.details = f"{result.details}; {thin_note}" if result.details else thin_note

    # ── PM estimated penalty ──
    if pm_source == "gap_estimate" and result.regime == USRegimeType.GAP_AND_GO:
        result.confidence = max(0.1, result.confidence - 0.15)
        pm_note = "PM estimated (gap range)"
        result.details = f"{result.details}; {pm_note}" if result.details else pm_note

    return result
