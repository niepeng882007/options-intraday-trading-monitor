"""HK Option Recommendation Engine — direction, strike, expiry selection."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd

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
from src.hk import RegimeResult, RegimeType
from src.utils.logger import setup_logger

logger = setup_logger("hk_option_rec")

MIN_OI = 50
MAX_SPREAD_PCT = 0.05  # 5% of underlying price


# ── Expiry selection ──


def select_expiry(expiry_dates: list[dict], today: date | None = None) -> str | None:
    """Select best expiry from available dates.

    Rules:
    - Filter out DTE=0 (same day)
    - Prefer nearest DTE >= 7 (weekly)
    - Fallback to nearest monthly
    - None if nothing available
    """
    if today is None:
        today = date.today()

    candidates = []
    for row in expiry_dates:
        exp_str = row.get("strike_time", "")
        if not exp_str:
            continue
        try:
            exp_date = datetime.strptime(exp_str[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        dte = (exp_date - today).days
        if dte <= 0:
            continue
        candidates.append((exp_date, dte, exp_str[:10]))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[1])

    # Prefer weekly (DTE >= 1)
    for exp_date, dte, exp_str in candidates:
        if dte >= 1:
            return exp_str

    return None


# ── Direction decision ──


def _decide_direction(
    regime: RegimeResult,
    vp: VolumeProfileResult,
    vwap: float = 0.0,
) -> str:
    """Decide bullish / bearish / neutral based on regime + price position."""
    price = regime.price

    if regime.regime in (
        RegimeType.GAP_AND_GO, RegimeType.TREND_DAY, RegimeType.BREAKOUT,
    ):
        if price > vp.vah:
            if vwap > 0 and price < vwap:
                return "neutral"  # VWAP contradiction
            return "bullish"
        if price < vp.val:
            if vwap > 0 and price > vwap:
                return "neutral"  # VWAP contradiction
            return "bearish"
        return "bullish" if price > vp.poc else "bearish"

    if regime.regime in (RegimeType.FADE_CHOP, RegimeType.RANGE):
        # Mean reversion: near VAH → bearish, near VAL → bullish
        if vp.vah > vp.val:
            mid = (vp.vah + vp.val) / 2
            direction = "bearish" if price > mid else "bullish"
            # VWAP structural veto: if VWAP contradicts the mean-reversion direction,
            # the range thesis is unreliable (e.g. VWAP above VAH → uptrend, not range)
            if vwap > 0:
                if direction == "bearish" and vwap > vp.vah:
                    return "neutral"  # VWAP above VAH contradicts bearish range
                if direction == "bullish" and vwap < vp.val:
                    return "neutral"  # VWAP below VAL contradicts bullish range
            return direction
        return "neutral"

    return "neutral"  # WHIPSAW / UNCLEAR


# ── Wait decision ──


def should_wait(
    regime: RegimeResult,
    filters: FilterResult,
    vp: VolumeProfileResult,
    chain_available: bool,
    expiry_available: bool,
) -> tuple[bool, list[str], list[str]]:
    """Determine if we should wait instead of trade.

    Returns: (should_wait, reasons, wait_conditions)
    """
    reasons: list[str] = []
    conditions: list[str] = []

    if not filters.tradeable:
        reasons.append("过滤器标记为不宜交易")
        for w in filters.warnings:
            reasons.append(f"  - {w}")

    if regime.regime == RegimeType.UNCLEAR and regime.confidence < 0.4:
        reasons.append(f"Regime UNCLEAR, 置信度仅 {regime.confidence:.0%}")
        conditions.append("等待 Regime 明确为 GAP_AND_GO / TREND_DAY 或 FADE_CHOP")

    if regime.regime == RegimeType.WHIPSAW:
        reasons.append("高波洗盘日, 方向不明确")
        conditions.append("等待带量突破确认方向")

    if regime.rvol < 0.5 and regime.regime not in (RegimeType.FADE_CHOP, RegimeType.RANGE):
        reasons.append(f"RVOL {regime.rvol:.2f} 极低, 量能不足")
        conditions.append(f"RVOL 回升至 0.8 以上")

    if not chain_available or not expiry_available:
        reasons.append("无可用期权链或到期日")
        conditions.append("检查标的是否有期权合约")

    # Price too close to POC — no edge
    if vp.poc > 0 and regime.price > 0:
        dist_to_poc = abs(regime.price - vp.poc) / vp.poc
        if dist_to_poc < 0.003 and regime.regime not in (
            RegimeType.GAP_AND_GO, RegimeType.TREND_DAY, RegimeType.BREAKOUT,
            RegimeType.FADE_CHOP, RegimeType.RANGE,
        ):
            reasons.append(f"价格距 POC 仅 {dist_to_poc:.1%}, 无方向性优势")
            conditions.append(f"价格突破 VAH {vp.vah:,.2f} 或跌破 VAL {vp.val:,.2f}")

    return bool(reasons), reasons, conditions


# ── Liquidity check ──


def _has_snapshot_data(chain_df: pd.DataFrame) -> bool:
    """Check if chain has real snapshot data (OI/Greeks) or just structure."""
    if chain_df.empty or "open_interest" not in chain_df.columns:
        return False
    return (chain_df["open_interest"] > 0).any()


def _check_liquidity(chain_df: pd.DataFrame, price: float) -> str | None:
    """Return liquidity warning or None."""
    if chain_df.empty:
        return "期权链为空, 无法评估流动性"

    if not _has_snapshot_data(chain_df):
        return "期权链快照数据不可用, 无法评估 OI/流动性, 建议下单前核实"

    warnings = []
    if "open_interest" in chain_df.columns:
        max_oi = chain_df["open_interest"].max()
        if max_oi < MIN_OI:
            warnings.append(f"最大 OI 仅 {max_oi}, 流动性极差")

    if "last_price" in chain_df.columns and price > 0:
        # Check bid-ask spread via proxy (not always available)
        pass  # bid/ask not in chain_df; skip

    return "; ".join(warnings) if warnings else None


# ── Main entry point ──


def recommend(
    regime: RegimeResult,
    vp: VolumeProfileResult,
    filters: FilterResult,
    chain_df: pd.DataFrame | None = None,
    expiry_dates: list[dict] | None = None,
    gamma_wall: GammaWallResult | None = None,
    vwap: float = 0.0,
    chase_risk_cfg: dict | None = None,
    range_min_dte: int = 2,
) -> OptionRecommendation:
    """Generate option recommendation based on regime, levels, and chain data."""
    price = regime.price
    has_chain = chain_df is not None and not chain_df.empty
    has_expiry = bool(expiry_dates)

    # Direction first — needed for degraded recommendations
    direction = _decide_direction(regime, vp, vwap=vwap)

    # Check regime/filter-based wait conditions first.
    # Chain/expiry availability is handled explicitly below
    # to preserve direction hint in the wait recommendation.
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
        return OptionRecommendation(
            action="wait",
            direction="neutral",
            rationale="方向不明确, 建议观望",
            risk_note="Regime 未给出明确方向",
            wait_conditions=["等待价格突破关键位后再入场"],
        )

    # ── Chase risk check ──
    chase_risk = ChaseRiskResult()
    prefer_atm = False
    if direction != "neutral" and vwap > 0:
        cfg = chase_risk_cfg or {}
        now_hkt = datetime.now(timezone(timedelta(hours=8)))
        is_afternoon = now_hkt.hour >= 12
        chase_risk = assess_chase_risk(
            price=price,
            vwap=vwap,
            vp=vp,
            direction=direction,
            is_afternoon=is_afternoon,
            vwap_moderate_pct=cfg.get("vwap_moderate_pct", 2.0),
            vwap_high_pct=cfg.get("vwap_high_pct", 3.5),
            va_moderate_pct=cfg.get("va_moderate_pct", 2.5),
            va_high_pct=cfg.get("va_high_pct", 4.0),
            afternoon_tighten_pct=cfg.get("afternoon_tighten_pct", 0.5),
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

    # Expiry + DTE
    expiry = select_expiry(expiry_dates or [])
    dte = 0
    if expiry:
        try:
            exp_date = datetime.strptime(expiry, "%Y-%m-%d").date()
            dte = (exp_date - date.today()).days
        except (ValueError, TypeError):
            pass

    # FADE_CHOP/RANGE + short DTE guard: mean-reversion needs time to play out,
    # 1 DTE options have too much gamma risk for this strategy
    if regime.regime in (RegimeType.FADE_CHOP, RegimeType.RANGE) and 0 < dte < range_min_dte:
        dir_hint = "看多" if direction == "bullish" else "看空"
        return OptionRecommendation(
            action="wait",
            direction=direction,
            rationale=f"方向偏{dir_hint}, 但 DTE={dte} 过短不适合 FADE_CHOP 策略",
            risk_note=f"FADE_CHOP 需要时间回归均值, {dte} DTE Gamma 风险过高",
            wait_conditions=[f"等待 DTE >= {range_min_dte} 的合约"],
        )

    # No expiry or no chain → wait (data issue, not market-based)
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
            wait_category="data",
        )

    # Liquidity check
    liq_warn = _check_liquidity(chain_df, price)

    # Try spread for FADE_CHOP/RANGE regime (skip if DTE <= 3 — gamma risk too high for spreads)
    if regime.regime in (RegimeType.FADE_CHOP, RegimeType.RANGE) and dte > 3:
        spread_legs = recommend_spread(direction, chain_df, price, expiry)
        if spread_legs:
            spread_action = "bull_put_spread" if direction == "bullish" else "bear_call_spread"
            metrics = _calculate_spread_metrics(spread_legs, spread_action)
            # P1: Check EV before recommending spread
            if metrics and _is_positive_ev(metrics):
                return OptionRecommendation(
                    action=spread_action,
                    direction=direction,
                    expiry=expiry,
                    legs=spread_legs,
                    moneyness=spread_legs[0].moneyness,
                    rationale=_build_rationale(regime, vp, direction, spread=True),
                    risk_note=_build_risk_note(regime, vp, direction, chase_risk=chase_risk, dte=dte),
                    liquidity_warning=liq_warn,
                    spread_metrics=metrics,
                    dte=dte,
                )
            else:
                logger.info(
                    "Spread rejected for %s: R:R=%.3f, falling through to single leg",
                    regime.price,
                    metrics.risk_reward_ratio if metrics else 0.0,
                )

    # Single leg
    leg = recommend_single_leg(direction, chain_df, price, expiry, prefer_atm=prefer_atm)
    if leg:
        action = "call" if direction == "bullish" else "put"
        return OptionRecommendation(
            action=action,
            direction=direction,
            expiry=expiry,
            legs=[leg],
            moneyness=leg.moneyness,
            rationale=_build_rationale(regime, vp, direction),
            risk_note=_build_risk_note(regime, vp, direction, chase_risk=chase_risk, dte=dte),
            liquidity_warning=liq_warn,
            dte=dte,
        )

    # No suitable strike found → wait
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
    regime: RegimeResult,
    vp: VolumeProfileResult,
    direction: str,
    spread: bool = False,
) -> str:
    """Build human-readable rationale."""
    parts = []

    regime_names = {
        RegimeType.GAP_AND_GO: "缺口追击",
        RegimeType.TREND_DAY: "趋势日",
        RegimeType.FADE_CHOP: "震荡回归",
        RegimeType.WHIPSAW: "高波洗盘",
        RegimeType.UNCLEAR: "不明确",
        # Deprecated — backward compat
        RegimeType.BREAKOUT: "突破",
        RegimeType.RANGE: "震荡",
    }
    parts.append(f"Regime: {regime_names.get(regime.regime, '未知')}")

    if regime.regime in (
        RegimeType.GAP_AND_GO, RegimeType.TREND_DAY, RegimeType.BREAKOUT,
    ):
        if direction == "bullish":
            parts.append(f"价格 {regime.price:,.2f} 突破 VAH {vp.vah:,.2f}")
        else:
            parts.append(f"价格 {regime.price:,.2f} 跌破 VAL {vp.val:,.2f}")
        if "Momentum" in (regime.details or ""):
            parts.append(f"RVOL {regime.rvol:.2f} 偏低但价格动量确认")
        else:
            parts.append(f"RVOL {regime.rvol:.2f} 量能配合")

    elif regime.regime in (RegimeType.FADE_CHOP, RegimeType.RANGE):
        if direction == "bullish":
            parts.append(f"价格 {regime.price:,.2f} 靠近 VAL {vp.val:,.2f}, 低吸机会")
        else:
            parts.append(f"价格 {regime.price:,.2f} 靠近 VAH {vp.vah:,.2f}, 高抛机会")
        if spread:
            parts.append("震荡市适合使用价差策略, 利用时间价值衰减")

    return "; ".join(parts)


def _build_risk_note(
    regime: RegimeResult,
    vp: VolumeProfileResult,
    direction: str,
    chase_risk: ChaseRiskResult | None = None,
    dte: int = 0,
) -> str:
    """Build risk note / defense line."""
    parts = []
    if regime.regime in (
        RegimeType.GAP_AND_GO, RegimeType.TREND_DAY, RegimeType.BREAKOUT,
    ):
        parts.append("防守线: VWAP, 跌破建议止损")
        if "Momentum" in (regime.details or ""):
            parts.append("失效条件: 量能持续萎缩且价格回落至 VA 内")
        else:
            parts.append("失效条件: RVOL 回落至 1.0 以下")
    elif regime.regime in (RegimeType.FADE_CHOP, RegimeType.RANGE):
        if direction == "bullish":
            parts.append(f"止损: 跌破 VAL {vp.val:,.2f}")
        else:
            parts.append(f"止损: 突破 VAH {vp.vah:,.2f}")
        parts.append("失效条件: 带量突破 VA 边界转为 TREND_DAY")

    # DTE risk warnings
    if dte > 0:
        if dte <= 3:
            parts.append(f"仅剩 {dte} DTE, Gamma 风险极高, 价格小幅波动可能导致大幅亏损")
        elif dte <= 5:
            parts.append(f"仅剩 {dte} DTE, Theta 衰减加速, 持仓过夜风险偏高")

    # Chase risk warnings
    if chase_risk and chase_risk.level == "moderate":
        chase_parts = []
        if chase_risk.vwap_dev_pct > 0:
            chase_parts.append(f"VWAP 偏离 {chase_risk.vwap_dev_pct:.1f}%")
        if chase_risk.va_dist_pct > 0:
            chase_parts.append(f"VA 边界距离 {chase_risk.va_dist_pct:.1f}%")
        parts.append("⚠️ 追高警告: " + ", ".join(chase_parts))
        parts.append("建议 ATM 而非 OTM，降低 Theta 风险")
        for r in chase_risk.reasons:
            if "午后" in r:
                parts.append("午后信号可靠性较低，建议缩小仓位")
                break

    return "; ".join(parts)
