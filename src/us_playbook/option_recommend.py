"""US Option Recommendation Engine — direction, strike, expiry selection.

Adapted from src/hk/option_recommend.py for US market:
- DTE filtering: skip 0DTE, prefer 2-7 DTE (weekly)
- Direction: GAP_AND_GO/TREND_DAY → directional, FADE_CHOP → mean reversion
- Chase risk: ET timezone, tighter afternoon thresholds
- Spread: FADE_CHOP regime → vertical spreads
- Greeks: graceful degradation when LV1 returns None
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from src.collector.base import OptionQuote
from src.common.option_utils import (
    assess_chase_risk,
    calculate_spread_metrics as _calculate_spread_metrics,
    classify_moneyness,
    is_positive_ev as _is_positive_ev,
    option_leg_from_row as _option_leg_from_row,
    recommend_single_leg,
    recommend_spread,
)
from src.common.types import (
    ChaseRiskResult,
    FilterResult,
    GammaWallResult,
    OptionLeg,
    OptionRecommendation,
    SpreadMetrics,
    VolumeProfileResult,
)
from src.us_playbook import USRegimeResult, USRegimeType
from src.utils.logger import setup_logger

logger = setup_logger("us_option_rec")

ET = ZoneInfo("America/New_York")

MIN_OI = 100
MAX_SPREAD_PCT = 0.05


# ── OptionQuote → DataFrame conversion ──


def option_quotes_to_df(options: list[OptionQuote]) -> pd.DataFrame:
    """Convert list of OptionQuote to DataFrame matching HK chain_df format."""
    if not options:
        return pd.DataFrame()
    rows = []
    for o in options:
        rows.append({
            "code": o.contract_symbol,
            "option_type": o.option_type.upper(),
            "strike_price": o.strike,
            "strike_time": o.expiration,
            "open_interest": o.open_interest or 0,
            "implied_volatility": o.implied_volatility or 0.0,
            "delta": o.delta,
            "gamma": o.gamma,
            "theta": o.theta,
            "vega": o.vega,
            "last_price": o.last or 0.0,
            "snap_volume": o.volume or 0,
            "bid_price": o.bid or 0.0,
            "ask_price": o.ask or 0.0,
        })
    return pd.DataFrame(rows)


# ── Expiry selection ──


def select_expiry(
    expiry_dates: list[str],
    today: date | None = None,
    dte_min: int = 1,
    dte_preferred_max: int = 7,
) -> str | None:
    """Select best expiry from available date strings.

    Rules:
    - Filter out DTE <= 0 (0DTE)
    - Prefer nearest DTE in [dte_min, dte_preferred_max] range
    - Fallback to nearest DTE > 0
    """
    if today is None:
        today = date.today()

    candidates = []
    for exp_str in expiry_dates:
        if not exp_str:
            continue
        try:
            exp_date = datetime.strptime(str(exp_str)[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        dte = (exp_date - today).days
        if dte < dte_min:
            continue
        candidates.append((dte, exp_str[:10]))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])

    # Prefer within preferred range
    for dte, exp_str in candidates:
        if dte <= dte_preferred_max:
            return exp_str

    # Fallback to nearest
    return candidates[0][1]


# ── Fade momentum ──


def _compute_fade_momentum(
    today_bars: pd.DataFrame,
    lookback: int = 8,
    threshold: float = 0.03,
) -> int:
    """Short-term momentum via linear regression slope.

    Returns: 1 (uptrend), -1 (downtrend), 0 (neutral / insufficient data).
    Slope is normalized to percent-per-bar; *threshold* is the cutoff (default 0.03%).
    """
    if today_bars is None or today_bars.empty:
        return 0
    if len(today_bars) < max(lookback // 2, 2):
        return 0

    closes = today_bars["Close"].iloc[-lookback:].values.astype(float)
    if len(closes) < max(lookback // 2, 2):
        return 0

    mean_price = float(np.mean(closes))
    if mean_price <= 0:
        return 0

    x = np.arange(len(closes), dtype=float)
    slope = float(np.polyfit(x, closes, 1)[0])
    slope_pct = slope / mean_price * 100  # percent per bar

    if slope_pct > threshold:
        return 1
    if slope_pct < -threshold:
        return -1
    return 0


# ── Local trend (medium-term for structural veto) ──


def compute_local_trend(
    today_bars: pd.DataFrame,
    lookback: int = 30,
    threshold: float = 0.02,
) -> int:
    """Medium-term trend via linear regression slope (longer lookback than fade momentum).

    Returns: 1 (uptrend), -1 (downtrend), 0 (neutral / insufficient data).
    Public function — reused by L1 screen in main.py.
    """
    return _compute_fade_momentum(today_bars, lookback=lookback, threshold=threshold)


# ── Direction decision ──


def _decide_direction(
    regime: USRegimeResult,
    vp: VolumeProfileResult,
    momentum: int = 0,
) -> str:
    """Decide bullish / bearish / neutral based on US regime + price position.

    For FADE_CHOP, uses VA three-zone logic with momentum confirmation:
    - Edge zone (position_ratio >= 0.70 or <= 0.30): gives direction;
      momentum=0 passes, opposing momentum → neutral.
    - Transition zone (0.30 < ratio < 0.70): requires momentum confirmation;
      momentum=0 → neutral.
    """
    price = regime.price

    if regime.regime in (USRegimeType.GAP_AND_GO, USRegimeType.TREND_DAY):
        if price > vp.vah:
            return "bullish"
        if price < vp.val:
            return "bearish"
        return "bullish" if price > vp.poc else "bearish"

    if regime.regime == USRegimeType.FADE_CHOP:
        va_range = vp.vah - vp.val
        if va_range <= 0:
            return "neutral"

        position_ratio = (price - vp.val) / va_range
        position_ratio = max(0.0, min(1.0, position_ratio))

        if position_ratio >= 0.70:
            # Edge zone — base direction is bearish (mean reversion from VAH)
            if momentum == 1:
                return "neutral"  # momentum opposes
            return "bearish"
        elif position_ratio <= 0.30:
            # Edge zone — base direction is bullish (mean reversion from VAL)
            if momentum == -1:
                return "neutral"  # momentum opposes
            return "bullish"
        else:
            # Transition zone — require momentum confirmation
            if position_ratio >= 0.50:
                # Slightly above mid → base bearish, need momentum -1 to confirm
                if momentum == -1:
                    return "bearish"
            else:
                # Slightly below mid → base bullish, need momentum +1 to confirm
                if momentum == 1:
                    return "bullish"
            return "neutral"

    # UNCLEAR: use lean hint if available (P0-3)
    if regime.regime == USRegimeType.UNCLEAR and hasattr(regime, "lean") and regime.lean != "neutral":
        return regime.lean
    return "neutral"


# ── Fade entry staleness ──


def _check_fade_entry_staleness(
    price: float,
    vp: VolumeProfileResult,
    direction: str,
    stale_moderate: float = 0.35,
    stale_high: float = 0.55,
) -> tuple[str, float]:
    """Check how far price has penetrated into the VA from the entry edge.

    For FADE_CHOP mean-reversion trades:
    - Bullish: entry edge = VAL, penetration = (price - VAL) / (VAH - VAL)
    - Bearish: entry edge = VAH, penetration = (VAH - price) / (VAH - VAL)

    Returns (level, penetration) where level is "none" / "moderate" / "high".
    """
    if direction not in ("bullish", "bearish"):
        return "none", 0.0

    va_range = vp.vah - vp.val
    if va_range <= 0:
        return "none", 0.0

    if direction == "bullish":
        penetration = (price - vp.val) / va_range
    else:
        penetration = (vp.vah - price) / va_range

    penetration = max(0.0, min(1.0, penetration))

    if penetration >= stale_high:
        return "high", penetration
    if penetration >= stale_moderate:
        return "moderate", penetration
    return "none", penetration


# ── Wait decision ──


def should_wait(
    regime: USRegimeResult,
    filters: FilterResult,
    vp: VolumeProfileResult,
    chain_available: bool,
    expiry_available: bool,
) -> tuple[bool, list[str], list[str]]:
    """Determine if we should wait instead of trade."""
    reasons: list[str] = []
    conditions: list[str] = []

    if not filters.tradeable:
        # Soft blocks can be overridden by confident FADE_CHOP
        soft_reasons = {"inside_day_rvol", "opex_combo"}
        soft_only = (
            filters.block_reasons
            and all(r in soft_reasons for r in filters.block_reasons)
        )
        if soft_only and regime.regime == USRegimeType.FADE_CHOP and regime.confidence >= 0.7:
            # FADE_CHOP 覆盖软阻断：低波动震荡正是均值回归的理想条件
            # 修正 filters 状态，使风险区显示与推荐一致（🟡 而非 🔴）
            filters.tradeable = True
            filters.risk_level = "elevated"
            filters.block_reasons = [r for r in filters.block_reasons if r not in soft_reasons]
        else:
            reasons.append("过滤器标记为不宜交易")
            for w in filters.warnings:
                reasons.append(f"  - {w}")

    if regime.regime == USRegimeType.UNCLEAR and regime.confidence < 0.4:
        reasons.append(f"Regime UNCLEAR, 置信度仅 {regime.confidence:.0%}")
        conditions.append("等待 Regime 明确后再入场")

    # RVOL absolute floor — skip for FADE_CHOP (low vol is expected in chop)
    if regime.rvol < 0.5 and regime.regime != USRegimeType.FADE_CHOP:
        reasons.append(f"RVOL {regime.rvol:.2f} 极低, 量能不足")
        conditions.append("RVOL 回升至 0.8 以上")

    if not chain_available or not expiry_available:
        reasons.append("无可用期权链或到期日")
        conditions.append("检查标的是否有期权合约")

    # Price too close to POC — no edge
    if vp.poc > 0 and regime.price > 0:
        dist_to_poc = abs(regime.price - vp.poc) / vp.poc
        if dist_to_poc < 0.003 and regime.regime != USRegimeType.GAP_AND_GO:
            reasons.append(f"价格距 POC 仅 {dist_to_poc:.1%}, 无方向性优势")
            conditions.append(f"价格突破 VAH {vp.vah:,.2f} 或跌破 VAL {vp.val:,.2f}")

    return bool(reasons), reasons, conditions


def _check_liquidity(chain_df: pd.DataFrame, price: float, min_oi: int = MIN_OI) -> str | None:
    if chain_df.empty:
        return "期权链为空, 无法评估流动性"
    if "open_interest" in chain_df.columns:
        has_oi = (chain_df["open_interest"] > 0).any()
        if not has_oi:
            return "期权链快照数据不可用, 无法评估 OI/流动性, 建议下单前核实"
        max_oi = chain_df["open_interest"].max()
        if max_oi < min_oi:
            return f"最大 OI 仅 {max_oi}, 流动性极差"
    return None


# ── Main entry point ──


def recommend(
    regime: USRegimeResult,
    vp: VolumeProfileResult,
    filters: FilterResult,
    chain_df: pd.DataFrame | None = None,
    expiry_dates: list[str] | None = None,
    gamma_wall: GammaWallResult | None = None,
    vwap: float = 0.0,
    chase_risk_cfg: dict | None = None,
    option_cfg: dict | None = None,
    today_bars: pd.DataFrame | None = None,
) -> OptionRecommendation:
    """Generate US option recommendation."""
    price = regime.price
    has_chain = chain_df is not None and not chain_df.empty
    has_expiry = bool(expiry_dates)
    cfg = option_cfg or {}

    # Compute intraday momentum for FADE_CHOP direction decision
    momentum = 0
    if regime.regime == USRegimeType.FADE_CHOP and today_bars is not None and not today_bars.empty:
        cr_cfg = chase_risk_cfg or {}
        momentum = _compute_fade_momentum(
            today_bars,
            lookback=cr_cfg.get("fade_momentum_lookback", 8),
            threshold=cr_cfg.get("fade_momentum_threshold", 0.03),
        )

    # P2-1: VA width minimum check (before direction decision)
    if regime.regime == USRegimeType.FADE_CHOP:
        cr_cfg = chase_risk_cfg or {}
        va_width_pct = (vp.vah - vp.val) / regime.price * 100 if regime.price > 0 else 0
        min_va_width = cr_cfg.get("min_va_width_pct", 0.80)
        if va_width_pct < min_va_width:
            return OptionRecommendation(
                action="wait", direction="neutral",
                structural_veto=True,
                rationale=f"VA 区间过窄 ({va_width_pct:.2f}%)，均值回归空间不足",
                risk_note=f"VA 宽度过滤: {va_width_pct:.2f}% < {min_va_width}%",
                wait_conditions=["等待 VA 区间扩展或选择其他标的"],
            )

    direction = _decide_direction(regime, vp, momentum=momentum)

    # P0-1: Local trend veto for FADE_CHOP — structural rejection
    if regime.regime == USRegimeType.FADE_CHOP and direction != "neutral":
        cr_cfg = chase_risk_cfg or {}
        local_trend = compute_local_trend(
            today_bars if today_bars is not None else pd.DataFrame(),
            lookback=cr_cfg.get("local_trend_lookback", 30),
            threshold=cr_cfg.get("local_trend_threshold", 0.02),
        )
        if (direction == "bullish" and local_trend == -1) or \
           (direction == "bearish" and local_trend == 1):
            trend_label = "下跌" if local_trend == -1 else "上涨"
            return OptionRecommendation(
                action="wait", direction=direction,
                structural_veto=True,
                rationale=f"日内中期趋势偏{trend_label}, 与均值回归方向矛盾",
                risk_note=f"日内趋势过滤: 30bar linreg 趋势{trend_label}, 不宜逆势入场",
                wait_conditions=[
                    "等待趋势减弱或反转后再评估",
                    f"或等待价格确认{trend_label}趋势后切换策略",
                ],
            )

    # Check wait conditions (excluding chain/expiry — handled separately)
    wait, reasons, conditions = should_wait(
        regime, filters, vp,
        chain_available=True,
        expiry_available=True,
    )
    if wait:
        return OptionRecommendation(
            action="wait",
            direction="neutral",
            rationale="观望",
            risk_note="\n".join(reasons),
            wait_conditions=conditions,
        )

    if direction == "neutral":
        # Momentum conflict explanation for FADE_CHOP
        if regime.regime == USRegimeType.FADE_CHOP and momentum != 0:
            momentum_label = "上涨" if momentum == 1 else "下跌"
            return OptionRecommendation(
                action="wait",
                direction="neutral",
                rationale=f"震荡日但日内动量偏{momentum_label}, 方向矛盾, 建议观望",
                risk_note=f"FADE_CHOP 均值回归方向与短期动量({momentum_label})冲突, 入场胜率下降",
                wait_conditions=[
                    "等待动量衰减后价格回到 VA 边缘再入场",
                    f"或等待价格确认{momentum_label}趋势后切换到 TREND_DAY 策略",
                ],
            )
        return OptionRecommendation(
            action="wait",
            direction="neutral",
            rationale="方向不明确, 建议观望",
            risk_note="Regime 未给出明确方向",
            wait_conditions=["等待价格突破关键位后再入场"],
        )

    # Fade entry staleness check — only for FADE_CHOP mean-reversion
    fade_stale_level = "none"
    fade_penetration = 0.0
    if regime.regime == USRegimeType.FADE_CHOP:
        cr_cfg = chase_risk_cfg or {}
        fade_stale_level, fade_penetration = _check_fade_entry_staleness(
            price, vp, direction,
            stale_moderate=cr_cfg.get("fade_entry_stale_moderate", 0.35),
            stale_high=cr_cfg.get("fade_entry_stale_high", 0.55),
        )
        if fade_stale_level == "high":
            edge = f"VAL {vp.val:,.2f}" if direction == "bullish" else f"VAH {vp.vah:,.2f}"
            return OptionRecommendation(
                action="wait",
                direction=direction,
                rationale=f"VA 渗透 {fade_penetration:.0%}, 入场窗口已过",
                risk_note=f"价格已深入 VA 中部, 均值回归优势消失",
                wait_conditions=[f"等待回落至 {edge} 附近再入场"],
            )

    # Fade moderate → prefer ATM
    if fade_stale_level == "moderate":
        prefer_atm = True
    else:
        prefer_atm = False

    # Chase risk check (ET timezone, P2-2: proportional time decay)
    chase_risk = ChaseRiskResult()
    if direction != "neutral" and vwap > 0:
        cr_cfg = chase_risk_cfg or {}
        now_et = datetime.now(ET)
        is_afternoon = now_et.hour >= 12
        # Calculate minutes to close (16:00 ET)
        close_time = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
        minutes_to_close = max(0, int((close_time - now_et).total_seconds() / 60))
        chase_risk = assess_chase_risk(
            price=price,
            vwap=vwap,
            vp=vp,
            direction=direction,
            is_afternoon=is_afternoon,
            vwap_moderate_pct=cr_cfg.get("vwap_moderate_pct", 1.5),
            vwap_high_pct=cr_cfg.get("vwap_high_pct", 2.5),
            va_moderate_pct=cr_cfg.get("va_moderate_pct", 2.0),
            va_high_pct=cr_cfg.get("va_high_pct", 3.0),
            afternoon_tighten_pct=cr_cfg.get("afternoon_tighten_pct", 0.3),
            minutes_to_close=minutes_to_close,
        )
        if chase_risk.level == "high":
            dir_hint = "看多" if direction == "bullish" else "看空"
            risk_parts = [f"方向偏{dir_hint}, 但价格已过度延伸"]
            risk_parts.extend(chase_risk.reasons)
            return OptionRecommendation(
                action="wait",
                direction=direction,
                rationale=f"方向偏{dir_hint}, 但追高风险过大",
                risk_note="\n".join(risk_parts),
                wait_conditions=[
                    f"等待回调至 VWAP {chase_risk.pullback_target:,.2f} 附近再入场",
                ],
            )
        if chase_risk.level == "moderate":
            prefer_atm = True

    # P1-1: FADE_CHOP option parameter override (longer DTE, prefer ATM, wider delta)
    dte_min = cfg.get("dte_min", 1)
    dte_preferred_max = cfg.get("dte_preferred_max", 7)
    delta_min = cfg.get("delta_min", 0.30)
    delta_max = cfg.get("delta_max", 0.50)
    if regime.regime == USRegimeType.FADE_CHOP:
        rr_cfg = cfg.get("range_reversal", {})
        if rr_cfg:
            dte_min = rr_cfg.get("dte_min", dte_min)
            dte_preferred_max = rr_cfg.get("dte_preferred_max", dte_preferred_max)
            if rr_cfg.get("prefer_atm", False):
                prefer_atm = True
            delta_min = rr_cfg.get("delta_min", delta_min)
            delta_max = rr_cfg.get("delta_max", delta_max)

    # Expiry selection
    expiry = select_expiry(
        expiry_dates or [],
        dte_min=dte_min,
        dte_preferred_max=dte_preferred_max,
    )
    dte = 0
    if expiry:
        try:
            exp_date = datetime.strptime(expiry, "%Y-%m-%d").date()
            dte = (exp_date - date.today()).days
        except (ValueError, TypeError):
            pass

    # No expiry or no chain → wait with direction hint
    if not expiry or not has_chain:
        wait_reasons = []
        wait_conds = []
        if not expiry:
            wait_reasons.append("无可用非末日到期日")
            wait_conds.append("等待新的期权合约上市")
        if not has_chain:
            wait_reasons.append("期权链数据不可用")
            wait_conds.append("检查标的是否有期权合约")
        dir_hint = "看多" if direction == "bullish" else "看空"
        return OptionRecommendation(
            action="wait",
            direction=direction,
            rationale=f"方向偏{dir_hint}, 但缺少可交易的期权合约",
            risk_note="; ".join(wait_reasons),
            wait_conditions=wait_conds,
            liquidity_warning="该标的期权链不可用或流动性不足",
        )

    # Liquidity check
    min_oi = cfg.get("min_oi", MIN_OI)
    liq_warn = _check_liquidity(chain_df, price, min_oi=min_oi)

    # Check delta availability for annotation
    has_greeks = (
        "delta" in chain_df.columns
        and chain_df["delta"].notna().any()
        and (chain_df["delta"].abs() > 0).any()
    )

    # Try spread for FADE_CHOP regime (skip if DTE <= 3)
    if regime.regime == USRegimeType.FADE_CHOP and dte > 3:
        spread_legs = recommend_spread(direction, chain_df, price, expiry, min_oi=min_oi)
        if spread_legs:
            spread_action = "bull_put_spread" if direction == "bullish" else "bear_call_spread"
            metrics = _calculate_spread_metrics(spread_legs, spread_action)
            if metrics and _is_positive_ev(metrics):
                return OptionRecommendation(
                    action=spread_action,
                    direction=direction,
                    expiry=expiry,
                    legs=spread_legs,
                    moneyness=spread_legs[0].moneyness,
                    rationale=_build_rationale(regime, vp, direction, spread=True, has_greeks=has_greeks, fade_penetration=fade_penetration),
                    risk_note=_build_risk_note(regime, vp, direction, chase_risk=chase_risk, dte=dte, fade_stale_level=fade_stale_level),
                    liquidity_warning=liq_warn,
                    spread_metrics=metrics,
                    dte=dte,
                )

    # Single leg (delta_min/delta_max already set above, with FADE_CHOP override)
    leg = recommend_single_leg(
        direction, chain_df, price, expiry,
        prefer_atm=prefer_atm,
        min_oi=min_oi,
        delta_min=delta_min,
        delta_max=delta_max,
    )
    if leg:
        action = "call" if direction == "bullish" else "put"
        return OptionRecommendation(
            action=action,
            direction=direction,
            expiry=expiry,
            legs=[leg],
            moneyness=leg.moneyness,
            rationale=_build_rationale(regime, vp, direction, has_greeks=has_greeks, fade_penetration=fade_penetration),
            risk_note=_build_risk_note(regime, vp, direction, chase_risk=chase_risk, dte=dte, fade_stale_level=fade_stale_level),
            liquidity_warning=liq_warn,
            dte=dte,
        )

    # No suitable strike
    dir_hint = "看多" if direction == "bullish" else "看空"
    return OptionRecommendation(
        action="wait",
        direction=direction,
        rationale=f"方向偏{dir_hint}, 但未找到合适的行权价",
        risk_note="期权链中无满足 OI/delta 条件的合约",
        wait_conditions=["等待期权链流动性改善后重新评估"],
        liquidity_warning=liq_warn or "期权链流动性不足",
    )


def _build_rationale(
    regime: USRegimeResult,
    vp: VolumeProfileResult,
    direction: str,
    spread: bool = False,
    has_greeks: bool = True,
    fade_penetration: float = 0.0,
) -> str:
    parts = []
    regime_names = {
        USRegimeType.GAP_AND_GO: "缺口追击",
        USRegimeType.TREND_DAY: "趋势日",
        USRegimeType.FADE_CHOP: "震荡日",
        USRegimeType.UNCLEAR: "不明确",
    }
    parts.append(f"Regime: {regime_names.get(regime.regime, '未知')}")

    if regime.regime in (USRegimeType.GAP_AND_GO, USRegimeType.TREND_DAY):
        if direction == "bullish":
            parts.append(f"价格 {regime.price:,.2f} 突破 VAH {vp.vah:,.2f}")
        else:
            parts.append(f"价格 {regime.price:,.2f} 跌破 VAL {vp.val:,.2f}")
        parts.append(f"RVOL {regime.rvol:.2f} 量能配合")
    elif regime.regime == USRegimeType.FADE_CHOP:
        edge_label = f"VAL {vp.val:,.2f}" if direction == "bullish" else f"VAH {vp.vah:,.2f}"
        action_label = "低吸机会" if direction == "bullish" else "高抛机会"
        if fade_penetration >= 0.35:
            parts.append(f"已在 VA 中部 (渗透 {fade_penetration:.0%}), 入场优势减弱")
        elif fade_penetration >= 0.20:
            parts.append(f"距 {edge_label} 偏远 (VA 渗透 {fade_penetration:.0%})")
        else:
            parts.append(f"价格 {regime.price:,.2f} 靠近 {edge_label}, {action_label}")
        if spread:
            parts.append("震荡市适合使用价差策略, 利用时间价值衰减")

    if not has_greeks:
        parts.append("Delta 不可用, 按 ATM 估算")

    return "; ".join(parts)


def _build_risk_note(
    regime: USRegimeResult,
    vp: VolumeProfileResult,
    direction: str,
    chase_risk: ChaseRiskResult | None = None,
    dte: int = 0,
    fade_stale_level: str = "none",
) -> str:
    parts = []
    if regime.regime in (USRegimeType.GAP_AND_GO, USRegimeType.TREND_DAY):
        parts.append("防守线: VWAP, 跌破建议止损")
        parts.append("失效条件: RVOL 回落至 1.0 以下")
    elif regime.regime == USRegimeType.FADE_CHOP:
        parts.append("失效条件: 带量突破 VA 边界")

    if dte > 0:
        if dte <= 3:
            parts.append(f"仅剩 {dte} DTE, Gamma 风险极高")
            parts.append("⚠️ 期权亏损达 40% 即止损，不等标的到止损位")
        elif dte <= 5:
            parts.append(f"仅剩 {dte} DTE, Theta 衰减加速")

    if fade_stale_level == "moderate":
        parts.append("⚠️ 入场区已消耗: 仓位减半, 仅用 ATM")

    if chase_risk and chase_risk.level == "moderate":
        chase_parts = []
        if chase_risk.vwap_dev_pct > 0:
            chase_parts.append(f"VWAP 偏离 {chase_risk.vwap_dev_pct:.1f}%")
        if chase_risk.va_dist_pct > 0:
            chase_parts.append(f"VA 边界距离 {chase_risk.va_dist_pct:.1f}%")
        parts.append("⚠️ 追高警告: " + ", ".join(chase_parts))
        parts.append("建议 ATM 而非 OTM，降低 Theta 风险")

    return "; ".join(parts)
