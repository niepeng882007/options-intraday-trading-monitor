"""Shared ActionPlan types and utility functions for US and HK playbooks."""

from __future__ import annotations

import html
import math
from dataclasses import dataclass

from src.common.formatting import format_strike as _format_strike
from src.common.types import OptionRecommendation
from src.utils.logger import setup_logger

logger = setup_logger("common_action_plan")

_esc = html.escape

# ATR-based stop trigger probabilities (to be calibrated with backtest data)
_STOP_PROB_TIGHT = 0.80    # stop < 0.5x ATR
_STOP_PROB_NARROW = 0.65   # stop < 1.0x ATR
_STOP_PROB_DEFAULT = 0.40  # stop >= 1.0x ATR

# Target reach probabilities based on remaining volatility
_TP_PROB_DEFAULT = 0.50    # tp <= remaining_vol
_TP_PROB_STRETCH = 0.25    # tp > remaining_vol
_TP_PROB_EXTREME = 0.10    # tp > 1.5x remaining_vol


@dataclass
class ActionPlan:
    label: str           # "A" / "B" / "C"
    name: str            # e.g., "趋势回调做多"
    emoji: str           # "📈" / "📉" / "⚡"
    is_primary: bool
    logic: str           # 一句话逻辑
    direction: str       # "bullish" / "bearish"
    trigger: str         # 触发条件
    entry: float | None
    entry_action: str    # "做多" / "做空"
    stop_loss: float | None
    stop_loss_reason: str
    tp1: float | None
    tp1_label: str       # "VAH" / "POC" etc.
    tp2: float | None
    tp2_label: str
    rr_ratio: float
    option_line: str | None = None  # 压缩合约行
    entry_zone_price: float | None = None   # 区间另一端 (结构位价格)
    entry_zone_label: str = ""              # 结构位名称 (如 "PMH")
    demoted: bool = False          # 因可达性/R:R 不合格
    demote_reason: str = ""
    suppressed: bool = False       # 因 wait 信号被压制
    warning: str = ""              # 附加警告 (如 VWAP 偏离)
    reachability_tag: str = ""     # "" / "远端" / "⛔不可达"
    is_near_entry: bool = False    # True = 近端备选 (距当前价 < 0.3%)
    effective_rr: float = 0.0          # 概率加权后的有效 R:R
    stop_atr_multiple: float = 0.0     # 止损占 5min ATR 的倍数
    stop_floor_applied: bool = False    # True = 止损被自动扩大


@dataclass
class PlanContext:
    """Runtime context for plan reachability and coherence checks."""
    minutes_to_close: int = 390
    total_session_minutes: int = 390  # US=390, HK=330
    rvol: float = 1.0
    avg_daily_range_pct: float = 0.0   # 0 = 无历史数据，不限制
    intraday_range_pct: float = 0.0
    option_action: str = ""             # "wait" / "call" / "put" / ...
    min_rr: float = 0.8
    market_direction: str = ""          # "bullish" / "bearish" / "" (unknown)
    current_price: float = 0.0         # 当前价，用于近端入场计算
    decoupled_from_benchmark: bool = False  # 个股脱钩大盘
    atr_5min: float = 0.0              # 5分钟 ATR (绝对值)


def calculate_rr(
    entry: float | None, stop_loss: float | None, take_profit: float | None,
) -> float:
    """Calculate risk-reward ratio. Direction auto-detected from entry vs stop_loss."""
    if entry is None or stop_loss is None or take_profit is None:
        return 0.0
    risk = abs(entry - stop_loss)
    reward = abs(take_profit - entry)
    return reward / risk if risk > 0 else 0.0


def reachable_range_pct(ctx: PlanContext) -> float:
    """Estimate remaining reachable price range (%).

    Uses sqrt(t/T) scaling (standard intraday volatility model).
    T = total_session_minutes (US=390, HK=330).
    Returns inf when no historical data, avoiding false restrictions.
    """
    if ctx.avg_daily_range_pct <= 0:
        return float("inf")
    total_range = ctx.avg_daily_range_pct * max(ctx.rvol, 0.1)
    t_over_T = ctx.minutes_to_close / ctx.total_session_minutes if ctx.total_session_minutes > 0 else 0
    time_factor = math.sqrt(t_over_T) if t_over_T > 0 else 0
    remaining = total_range * time_factor
    # 已消耗幅度折减 50%（范围可能扩张，不全额扣减）
    consumed_discount = ctx.intraday_range_pct * 0.5
    # 地板：日均波幅 15%，即使收盘前也保留最低估算
    return max(remaining - consumed_discount, total_range * 0.15)


def compact_option_line(option_rec: OptionRecommendation) -> str | None:
    """Compress option recommendation to a single line."""
    if option_rec is None or option_rec.action == "wait":
        return None

    if option_rec.action in ("bear_call_spread", "bull_put_spread"):
        sm = option_rec.spread_metrics
        if option_rec.legs and len(option_rec.legs) >= 2:
            strikes = sorted(l.strike for l in option_rec.legs)
            spread_name = "Bear Call Spread" if option_rec.action == "bear_call_spread" else "Bull Put Spread"
            parts = [f"📋 合约: {spread_name} {_format_strike(strikes[0])}/{_format_strike(strikes[1])}"]
            if option_rec.dte > 0:
                parts.append(f"DTE {option_rec.dte}")
            if sm and sm.net_credit > 0:
                parts.append(f"净收 {sm.net_credit:.2f}")
            if sm and sm.risk_reward_ratio > 0:
                parts.append(f"R:R {sm.risk_reward_ratio:.1f}:1")
            return " | ".join(parts)
        return None

    # Single leg: call / put
    opt_type = "CALL" if option_rec.action == "call" else "PUT"
    side = "Buy"
    if option_rec.legs:
        leg = option_rec.legs[0]
        parts = [f"📋 合约: {side} {opt_type} {_format_strike(leg.strike)} ({leg.moneyness})"]
        if option_rec.dte > 0:
            parts.append(f"DTE {option_rec.dte}")
        if leg.delta is not None:
            parts.append(f"Δ{leg.delta:+.2f}")
        if leg.open_interest is not None and leg.open_interest > 0:
            parts.append(f"OI {leg.open_interest:,}")
        return " | ".join(parts)

    # No legs but has recommendation
    parts = [f"📋 合约: {side} {opt_type}"]
    if option_rec.dte > 0:
        parts.append(f"DTE {option_rec.dte}")
    return " | ".join(parts)


def format_action_plan(plan: ActionPlan) -> list[str]:
    """Render a single ActionPlan to Telegram HTML lines."""
    tag = "首选" if plan.is_primary else "备选"
    if plan.is_near_entry:
        tag = "近端"
    lines = [f"{plan.emoji} <b>{plan.label}: {_esc(plan.name)}</b> ({tag})"]
    lines.append(f"逻辑: {_esc(plan.logic)}")

    if plan.label == "C" and plan.entry is None:
        # Simplified format for invalidation plan (no entry)
        lines.append(f"  条件: {_esc(plan.trigger)}")
        lines.append(f"  行动: {_esc(plan.logic)}")
        return lines

    lines.append(f"  触发: {_esc(plan.trigger)}")

    # P2: Suppressed plans only show trigger + warning, hide entry/SL/TP details
    if plan.suppressed:
        if plan.demote_reason:
            lines.append(f"  ⚠️ {_esc(plan.demote_reason)}")
        if plan.warning:
            lines.append(f"  ⚠️ {_esc(plan.warning)}")
        return lines

    if plan.entry is not None:
        if plan.entry_zone_price is not None:
            if plan.direction == "bearish":
                lines.append(
                    f"  入场区间: {_esc(plan.entry_zone_label)}({plan.entry_zone_price:,.2f})"
                    f" → VAH({plan.entry:,.2f}) {plan.entry_action}"
                )
            else:
                lines.append(
                    f"  入场区间: VAL({plan.entry:,.2f})"
                    f" → {_esc(plan.entry_zone_label)}({plan.entry_zone_price:,.2f}) {plan.entry_action}"
                )
        else:
            lines.append(f"  入场: {plan.entry:,.2f} ({plan.entry_action})")
    else:
        lines.append("  入场: 等待方向明确后入场")
    if plan.stop_loss is not None:
        sl_line = f"  止损: {plan.stop_loss:,.2f} ({_esc(plan.stop_loss_reason)})"
        if plan.stop_atr_multiple > 0:
            atr_check = "✓" if plan.stop_atr_multiple >= 1.5 else "⚠️"
            sl_line += f" | {plan.stop_atr_multiple:.1f}x ATR {atr_check}"
        if plan.stop_floor_applied:
            sl_line += " [已扩大]"
        lines.append(sl_line)
    if plan.tp1 is not None:
        lines.append(f"  TP1 (50%): {plan.tp1:,.2f} ({plan.tp1_label})")
    if plan.tp2 is not None:
        lines.append(f"  TP2 (清仓): {plan.tp2:,.2f} ({plan.tp2_label})")
    if plan.rr_ratio > 0:
        rr_line = f"  R:R ≈ 1:{plan.rr_ratio:.1f}"
        if plan.effective_rr > 0 and abs(plan.effective_rr - plan.rr_ratio) > 0.05:
            rr_line += f" (有效 1:{plan.effective_rr:.1f})"
        lines.append(rr_line)
    if plan.reachability_tag:
        lines.append(f"  📍 {plan.reachability_tag}")
    if plan.option_line:
        lines.append(plan.option_line)
    if plan.demoted and plan.demote_reason:
        lines.append(f"  ⚠️ {_esc(plan.demote_reason)}")
    if plan.warning:
        lines.append(f"  ⚠️ {_esc(plan.warning)}")
    return lines


def nearest_levels(
    price: float,
    side: str,  # "above" | "below"
    levels: dict[str, float],
    n: int = 2,
) -> list[tuple[str, float]]:
    """Find nearest key levels above/below current price from a dict.

    Returns up to *n* (name, value) tuples sorted by distance from *price*.
    Levels closer than 0.05% are ignored (noise).
    """
    if price <= 0:
        return []

    min_dist_pct = 0.0005  # 0.05%
    candidates = [(name, val) for name, val in levels.items() if val > 0]

    if side == "above":
        filtered = [(name, val) for name, val in candidates if (val - price) / price > min_dist_pct]
    else:
        filtered = [(name, val) for name, val in candidates if (price - val) / price > min_dist_pct]

    filtered.sort(key=lambda x: abs(x[1] - price))
    return filtered[:n]


def find_fade_entry_zone(
    va_edge: float,
    opposite_edge: float,
    levels: dict[str, float],
) -> tuple[str, float] | None:
    """Find the nearest structural level within the VA upper/lower third.

    For shorts (near VAH): zone = [VAH - range/3, VAH)
    For longs  (near VAL): zone = (VAL, VAL + range/3]

    Candidates: all levels except POC/VAH/VAL.
    Returns (label, price) of the nearest to *va_edge*, or None.
    """
    va_range = abs(va_edge - opposite_edge)
    if va_range <= 0:
        return None

    third = va_range / 3.0
    is_short = va_edge > opposite_edge  # near VAH → short
    if is_short:
        zone_lo = va_edge - third
        zone_hi = va_edge
    else:
        zone_lo = va_edge
        zone_hi = va_edge + third

    skip = {"POC", "VAH", "VAL"}
    candidates: list[tuple[str, float]] = []
    for name, val in levels.items():
        if name in skip or val <= 0:
            continue
        if val < zone_lo or val > zone_hi:
            continue
        min_dist_pct = 0.0005
        if va_edge > 0 and abs(val - va_edge) / va_edge < min_dist_pct:
            continue
        candidates.append((name, val))

    if not candidates:
        return None

    candidates.sort(key=lambda x: abs(x[1] - va_edge))
    return candidates[0]


def cap_tp2(
    plan: ActionPlan, ctx: PlanContext, levels: dict[str, float],
) -> ActionPlan:
    """Cap TP2 to reachable range; replace with nearer structural level or clear."""
    if plan.tp2 is None or plan.entry is None:
        return plan
    reachable = reachable_range_pct(ctx)
    tp2_dist = abs(plan.tp2 - plan.entry) / plan.entry * 100
    if tp2_dist <= reachable:
        return plan

    side = "below" if plan.direction == "bearish" else "above"
    candidates = nearest_levels(plan.entry, side, levels, n=3)
    tp1_val = plan.tp1 or 0
    original_tp2 = plan.tp2
    for name, val in candidates:
        if plan.direction == "bearish" and not (original_tp2 < val < plan.entry):
            continue
        if plan.direction == "bullish" and not (plan.entry < val < original_tp2):
            continue
        if abs(val - tp1_val) / max(tp1_val, 1) < 0.001:
            continue
        dist = abs(val - plan.entry) / plan.entry * 100
        tp1_dist = abs(tp1_val - plan.entry) / plan.entry * 100 if tp1_val else 0
        if dist <= reachable and dist > tp1_dist:
            plan.tp2 = val
            plan.tp2_label = name
            return plan

    plan.tp2 = None
    plan.tp2_label = ""
    return plan


def check_entry_reachability(
    plan: ActionPlan, current_price: float, ctx: PlanContext,
) -> ActionPlan:
    """Tag plan reachability as "" / "远端" / "⛔不可达".

    Three tiers:
    - entry_dist <= 0.3% of reachable → "" (near, no tag)
    - entry_dist <= reachable → "远端" (reachable but far)
    - entry_dist > reachable → "⛔不可达" (demoted)
    """
    if plan.entry is None or plan.label == "C":
        return plan
    if current_price <= 0:
        return plan
    entry_dist = abs(plan.entry - current_price) / current_price * 100
    reachable = reachable_range_pct(ctx)
    near_threshold = reachable * 0.3  # 30% of reachable range

    if entry_dist <= near_threshold:
        plan.reachability_tag = ""
    elif entry_dist <= reachable:
        plan.reachability_tag = "远端"
    else:
        plan.reachability_tag = "⛔不可达"
        plan.demoted = True
        plan.demote_reason = (
            f"入场位距当前价 {entry_dist:.1f}%, "
            f"剩余波动预估仅 {reachable:.1f}%"
        )
    return plan


def generate_near_entry_plan(
    current_price: float,
    direction: str,
    levels: dict[str, float],
    option_line: str | None = None,
) -> ActionPlan | None:
    """Generate a near-entry Plan C using the closest structural level.

    Entry is set to the nearest level within 0.3% of current_price on
    the approach side (below for bullish, above for bearish).
    Returns None if no suitable level exists.
    """
    near_pct = 0.003  # 0.3%
    if current_price <= 0 or direction not in ("bullish", "bearish"):
        return None

    # Find nearest level on the entry side
    side = "below" if direction == "bullish" else "above"
    candidates = nearest_levels(current_price, side, levels, n=3)

    entry_level = None
    for name, val in candidates:
        dist = abs(val - current_price) / current_price
        if dist <= near_pct:
            entry_level = (name, val)
            break

    if entry_level is None:
        # No structural level nearby → use current price directly
        entry_val = current_price
        entry_label = "当前价"
    else:
        entry_val = entry_level[1]
        entry_label = entry_level[0]

    # TP: nearest level beyond entry
    tp_side = "above" if direction == "bullish" else "below"
    tp_candidates = nearest_levels(entry_val, tp_side, levels, n=2)
    tp1 = tp_candidates[0] if tp_candidates else None
    tp2 = tp_candidates[1] if len(tp_candidates) >= 2 else None

    # SL: nearest level on the opposite side
    sl_side = "below" if direction == "bullish" else "above"
    sl_candidates = nearest_levels(entry_val, sl_side, levels, n=1)
    sl = sl_candidates[0] if sl_candidates else None

    entry_action = "做多" if direction == "bullish" else "做空"
    emoji = "📈" if direction == "bullish" else "📉"

    rr = calculate_rr(entry_val, sl[1] if sl else None, tp1[1] if tp1 else None)

    plan = ActionPlan(
        label="C", name=f"近端{entry_action}", emoji=emoji, is_primary=False,
        logic=f"当前价附近 {entry_label} 直接入场",
        direction=direction,
        trigger=f"价格在 {entry_label} {entry_val:,.2f} 附近企稳",
        entry=entry_val, entry_action=entry_action,
        stop_loss=sl[1] if sl else None,
        stop_loss_reason=sl[0] if sl else "最近支撑/阻力",
        tp1=tp1[1] if tp1 else None,
        tp1_label=tp1[0] if tp1 else "",
        tp2=tp2[1] if tp2 else None,
        tp2_label=tp2[0] if tp2 else "",
        rr_ratio=rr,
        option_line=option_line,
        is_near_entry=True,
    )
    return plan


def ensure_near_entry_exists(
    plans: list[ActionPlan],
    current_price: float,
    direction: str,
    levels: dict[str, float],
    option_line: str | None = None,
) -> list[ActionPlan]:
    """If no plan has entry within 0.3% of current_price, replace Plan C with a near-entry plan."""
    if current_price <= 0:
        return plans

    near_pct = 0.003
    has_near = False
    for p in plans:
        if p.entry is not None and not p.demoted and not p.suppressed:
            dist = abs(p.entry - current_price) / current_price
            if dist <= near_pct:
                has_near = True
                break

    if has_near:
        return plans

    near_plan = generate_near_entry_plan(current_price, direction, levels, option_line)
    if near_plan is None:
        return plans

    # Replace Plan C (last plan) with near-entry plan
    for i, p in enumerate(plans):
        if p.label == "C":
            plans[i] = near_plan
            return plans

    # No Plan C found — append
    plans.append(near_plan)
    return plans


def apply_wait_coherence(
    plans: list[ActionPlan], ctx: PlanContext,
) -> list[ActionPlan]:
    """When option_rec says 'wait', demote Plan A and suppress Plan B.

    F5: If Plan B is a hedge (different direction from Plan A), skip suppress —
    hedging is more important during uncertain conditions.
    """
    if ctx.option_action != "wait":
        return plans
    plan_a_dir = ""
    for plan in plans:
        if plan.label == "A":
            plan_a_dir = plan.direction
            if plan.entry is not None:
                plan.demoted = True
                plan.demote_reason = "核心结论为观望, 边缘入场需额外确认"
        elif plan.label == "B" and plan.entry is not None:
            # Skip suppress if Plan B is a hedge (opposite direction)
            if plan_a_dir and plan.direction != plan_a_dir:
                continue
            plan.suppressed = True
            plan.demote_reason = "核心结论为观望, 中间区域入场暂缓"
    return plans


def enforce_stop_floor(plan: ActionPlan, ctx: PlanContext) -> ActionPlan:
    """Enforce minimum stop-loss distance based on ATR and daily range."""
    if plan.entry is None or plan.stop_loss is None or ctx.atr_5min <= 0:
        return plan

    avg_daily_range_abs = ctx.avg_daily_range_pct / 100 * plan.entry if ctx.avg_daily_range_pct > 0 else 0.0
    min_stop_distance = max(1.5 * ctx.atr_5min, avg_daily_range_abs * 0.05)
    current_stop_distance = abs(plan.entry - plan.stop_loss)
    plan.stop_atr_multiple = current_stop_distance / ctx.atr_5min if ctx.atr_5min > 0 else 0.0

    if current_stop_distance < min_stop_distance:
        if plan.direction == "bearish":
            plan.stop_loss = plan.entry + min_stop_distance
        else:
            plan.stop_loss = plan.entry - min_stop_distance
        plan.stop_floor_applied = True
        plan.stop_atr_multiple = min_stop_distance / ctx.atr_5min
        plan.rr_ratio = calculate_rr(plan.entry, plan.stop_loss, plan.tp1)

    return plan


def validate_target_reachability(plan: ActionPlan, ctx: PlanContext) -> ActionPlan:
    """Validate TP1 against remaining volatility estimate.

    Tier 1: TP1 > remaining_vol → warn
    Tier 2: TP1 > remaining_vol × 1.5 → demote (不 force-adjust)
    """
    if plan.entry is None or plan.tp1 is None:
        return plan
    remaining_vol = reachable_range_pct(ctx)
    if remaining_vol == float("inf"):
        return plan
    tp1_dist_pct = abs(plan.tp1 - plan.entry) / plan.entry * 100

    # Tier 2: extreme overshoot → demote
    if tp1_dist_pct > remaining_vol * 1.5:
        plan.demoted = True
        plan.demote_reason = f"TP1 距入场 {tp1_dist_pct:.1f}%, 远超剩余波动 {remaining_vol:.1f}%"
        return plan

    # Tier 1: moderate overshoot → warn
    if tp1_dist_pct > remaining_vol:
        w = f"⚠️ TP1 超出剩余空间 ({tp1_dist_pct:.1f}% > {remaining_vol:.1f}%)"
        plan.warning = f"{plan.warning}; {w}" if plan.warning else w

    return plan


def compute_effective_rr(plan: ActionPlan, ctx: PlanContext) -> ActionPlan:
    """Compute probability-weighted effective R:R."""
    if plan.entry is None or plan.stop_loss is None or plan.tp1 is None:
        plan.effective_rr = 0.0
        return plan

    stop_distance = abs(plan.entry - plan.stop_loss)
    tp_distance = abs(plan.tp1 - plan.entry)
    if stop_distance <= 0 or tp_distance <= 0:
        plan.effective_rr = 0.0
        return plan

    # Stop trigger probability based on ATR multiple
    if ctx.atr_5min > 0:
        atr_mult = stop_distance / ctx.atr_5min
        if atr_mult < 0.5:
            stop_prob = _STOP_PROB_TIGHT
        elif atr_mult < 1.0:
            stop_prob = _STOP_PROB_NARROW
        else:
            stop_prob = _STOP_PROB_DEFAULT
    else:
        stop_prob = _STOP_PROB_DEFAULT

    # Target reach probability based on remaining volatility
    remaining_vol = reachable_range_pct(ctx)
    tp_dist_pct = tp_distance / plan.entry * 100 if plan.entry > 0 else 0
    if remaining_vol == float("inf") or remaining_vol <= 0:
        tp_prob = _TP_PROB_DEFAULT
    elif tp_dist_pct <= remaining_vol:
        tp_prob = _TP_PROB_DEFAULT
    elif tp_dist_pct <= remaining_vol * 1.5:
        tp_prob = _TP_PROB_STRETCH
    else:
        tp_prob = _TP_PROB_EXTREME

    plan.effective_rr = (tp_prob * tp_distance) / (stop_prob * stop_distance)
    return plan


def check_all_demoted(plans: list[ActionPlan]) -> list[ActionPlan]:
    """If all plans with entries are demoted/suppressed, add standby warning."""
    actionable = [p for p in plans if p.entry is not None]
    if not actionable:
        return plans
    all_down = all(p.demoted or p.suppressed for p in actionable)
    if all_down:
        for p in actionable:
            w = "所有方案有效R:R不足或被降级, 建议观望"
            p.warning = f"{p.warning}; {w}" if p.warning else w
            break
    return plans


def apply_min_rr_gate(
    plans: list[ActionPlan], ctx: PlanContext,
) -> list[ActionPlan]:
    """Demote plans with R:R below threshold.

    Three-layer check for rr_ratio == 0:
    - tp1 set but no stop_loss → "无止损位, 风险不可控"
    - tp1 set and stop_loss set → "TP1 过近入场位, 无操作空间"
    - tp1 is None → skip (UNCLEAR Plan B "轻仓试探" semantics)

    Also checks effective_rr when available.
    """
    for plan in plans:
        if plan.entry is None:
            continue
        # Skip old-style Plan C (no entry) — already caught above.
        # New Plan C with entry participates in R:R gate.
        if plan.rr_ratio == 0.0:
            if plan.tp1 is None:
                continue  # UNCLEAR 轻仓试探, no TP → skip
            if plan.stop_loss is None:
                plan.demoted = True
                if not plan.demote_reason:
                    plan.demote_reason = "无止损位, 风险不可控"
            else:
                plan.demoted = True
                if not plan.demote_reason:
                    plan.demote_reason = "TP1 过近入场位, 无操作空间"
        elif plan.rr_ratio < ctx.min_rr:
            plan.demoted = True
            if not plan.demote_reason:
                plan.demote_reason = f"R:R 仅 1:{plan.rr_ratio:.1f}, 低于阈值 1:{ctx.min_rr:.1f}"

        # Effective R:R checks (when computed)
        if plan.effective_rr > 0 and not plan.demoted:
            if plan.effective_rr < 1.5:
                plan.demoted = True
                if not plan.demote_reason:
                    plan.demote_reason = f"有效R:R 仅 {plan.effective_rr:.1f}, 低于首选阈值 1.5"
        if plan.effective_rr > 8.0:
            w = f"⚠️ 极端R:R ({plan.effective_rr:.1f}), 请校验止损/目标"
            plan.warning = f"{plan.warning}; {w}" if plan.warning else w
    return plans


def cap_tp1(
    plan: ActionPlan, ctx: PlanContext, levels: dict[str, float],
) -> ActionPlan:
    """Cap TP1 to reachable range; replace with nearer structural level or warn."""
    if plan.tp1 is None or plan.entry is None or plan.label == "C":
        return plan
    reachable = reachable_range_pct(ctx)
    tp1_dist = abs(plan.tp1 - plan.entry) / plan.entry * 100
    if tp1_dist <= reachable:
        return plan

    # Find a nearer structural level as replacement
    side = "below" if plan.direction == "bearish" else "above"
    candidates = nearest_levels(plan.entry, side, levels, n=3)
    for name, val in candidates:
        if plan.direction == "bearish" and not (plan.tp1 < val < plan.entry):
            continue
        if plan.direction == "bullish" and not (plan.entry < val < plan.tp1):
            continue
        dist = abs(val - plan.entry) / plan.entry * 100
        if dist <= reachable and dist > 0.1:
            plan.tp1 = val
            plan.tp1_label = name
            plan.rr_ratio = calculate_rr(plan.entry, plan.stop_loss, plan.tp1)
            return plan

    # No replacement found — keep TP1 but warn
    w = f"TP1 距入场 {tp1_dist:.1f}%, 超出预估波动 {reachable:.1f}%"
    plan.warning = f"{plan.warning}; {w}" if plan.warning else w
    return plan


def check_regime_consistency(
    plans: list[ActionPlan],
    regime_type: str,
    price: float,
    open_price: float,
    ibh: float,
    ibl: float,
    vwap: float,
) -> list[ActionPlan]:
    """Demote plans when range regime but price is outside IB range.

    If price is >0.3% outside IB, demote A/B plans.
    If |price - vwap| / vwap > 1.0%, add VWAP deviation warning.
    Only applies to RANGE / GAP_FILL / NARROW_GRIND regimes.
    """
    if regime_type not in ("RANGE", "GAP_FILL", "NARROW_GRIND"):
        return plans
    if ibh <= 0 or ibl <= 0:
        return plans

    # Check if price is outside IB by > 0.3%
    outside_ib = False
    if price > ibh:
        ib_dist_pct = (price - ibh) / ibh * 100
        if ib_dist_pct > 0.3:
            outside_ib = True
    elif price < ibl:
        ib_dist_pct = (ibl - price) / ibl * 100
        if ib_dist_pct > 0.3:
            outside_ib = True

    if outside_ib:
        for plan in plans:
            if plan.label in ("A", "B") and plan.entry is not None:
                plan.demoted = True
                if not plan.demote_reason:
                    plan.demote_reason = "震荡 regime 但价格已超出 IB 区间, 震荡逻辑存疑"

    # VWAP deviation warning
    if vwap > 0 and price > 0:
        vwap_dev_pct = abs(price - vwap) / vwap * 100
        if vwap_dev_pct > 1.0:
            for plan in plans:
                if plan.label in ("A", "B") and plan.entry is not None:
                    w = f"价格偏离 VWAP {vwap_dev_pct:.1f}%, 均值回归风险"
                    plan.warning = f"{plan.warning}; {w}" if plan.warning else w

    return plans


def check_entry_proximity(
    plan: ActionPlan,
    current_price: float,
    min_dist_pct: float = 0.5,
) -> ActionPlan:
    """Demote plan if entry is too close to current price.

    Skip Plan C and plans with no entry.
    """
    if plan.label == "C" or plan.entry is None:
        return plan
    if current_price <= 0:
        return plan

    dist_pct = abs(plan.entry - current_price) / current_price * 100
    if dist_pct < min_dist_pct:
        plan.demoted = True
        if not plan.demote_reason:
            plan.demote_reason = (
                f"入场位距当前价仅 {dist_pct:.2f}%, "
                f"低于最小距离 {min_dist_pct:.1f}%"
            )
    return plan


def apply_gamma_wall_warning(
    plans: list[ActionPlan],
    price: float,
    gamma_wall: object | None,
    ctx: PlanContext,
    *,
    max_pain_adr_ratio: float = 0.5,
    wall_proximity_adr_ratio: float = 0.3,
) -> list[ActionPlan]:
    """Warn or demote when gamma wall structure conflicts with plan direction.

    Thresholds are adaptive: ADR * ratio.  Falls back to fixed 1.0% / 1.5%
    when ADR is unavailable (ctx.avg_daily_range_pct == 0).
    """
    if gamma_wall is None or price <= 0:
        return plans

    max_pain = getattr(gamma_wall, "max_pain", 0.0)
    call_wall = getattr(gamma_wall, "call_wall_strike", 0.0)
    put_wall = getattr(gamma_wall, "put_wall_strike", 0.0)

    if max_pain <= 0 and call_wall <= 0 and put_wall <= 0:
        return plans

    adr = ctx.avg_daily_range_pct
    if adr > 0:
        warn_thr = adr * max_pain_adr_ratio      # MaxPain deviation %
        prox_thr = adr * wall_proximity_adr_ratio  # wall proximity %
    else:
        warn_thr = 1.0
        prox_thr = 1.5

    for plan in plans:
        if plan.label == "C" or plan.entry is None:
            continue

        warnings: list[str] = []

        if plan.direction == "bullish":
            # MaxPain below price → gravitational pull down
            if max_pain > 0:
                mp_dev = (price - max_pain) / price * 100
                if mp_dev > warn_thr:
                    warnings.append(f"MaxPain({_format_strike(max_pain)})低于现价 {mp_dev:.1f}%, 期权引力偏空")
            # Call wall close above → resistance cap
            if call_wall > 0:
                cw_dist = (call_wall - price) / price * 100
                if 0 < cw_dist < prox_thr:
                    warnings.append(f"Call Wall({_format_strike(call_wall)})近在 {cw_dist:.1f}%, 上方压制")
            # Put wall above price → structure hostile
            if put_wall > 0 and put_wall > price:
                plan.demoted = True
                plan.demote_reason = plan.demote_reason or "期权结构不支持做多 (Put Wall 高于现价)"

        elif plan.direction == "bearish":
            # MaxPain above price → gravitational pull up
            if max_pain > 0:
                mp_dev = (max_pain - price) / price * 100
                if mp_dev > warn_thr:
                    warnings.append(f"MaxPain({_format_strike(max_pain)})高于现价 {mp_dev:.1f}%, 期权引力偏多")
            # Put wall close below → support cushion
            if put_wall > 0:
                pw_dist = (price - put_wall) / price * 100
                if 0 < pw_dist < prox_thr:
                    warnings.append(f"Put Wall({_format_strike(put_wall)})近在 {pw_dist:.1f}%, 下方承接")
            # Call wall below price → structure hostile
            if call_wall > 0 and call_wall < price:
                plan.demoted = True
                plan.demote_reason = plan.demote_reason or "期权结构不支持做空 (Call Wall 低于现价)"

        if warnings:
            new_warning = "; ".join(warnings)
            plan.warning = f"{plan.warning}; {new_warning}" if plan.warning else new_warning

    return plans


def apply_vwap_deviation_warning(
    plans: list[ActionPlan],
    price: float,
    vwap: float,
    threshold: float = 0.5,
) -> list[ActionPlan]:
    """Warn when price-vs-VWAP direction conflicts with plan direction.

    E.g., price below VWAP but plan is bearish (shorting into weakness
    without a bounce to VWAP) — warn the trader to wait for a bounce.
    """
    if vwap <= 0 or price <= 0:
        return plans
    dev_pct = (price - vwap) / vwap * 100
    if abs(dev_pct) < threshold:
        return plans

    for plan in plans:
        if plan.label == "C" or plan.entry is None:
            continue
        # Price below VWAP + bearish plan → chasing weakness
        if dev_pct < -threshold and plan.direction == "bearish":
            w = f"价格已低于 VWAP {abs(dev_pct):.1f}%, 做空需等反弹"
            plan.warning = f"{plan.warning}; {w}" if plan.warning else w
        # Price above VWAP + bullish plan → chasing strength
        elif dev_pct > threshold and plan.direction == "bullish":
            w = f"价格已高于 VWAP {abs(dev_pct):.1f}%, 做多需等回调"
            plan.warning = f"{plan.warning}; {w}" if plan.warning else w
    return plans


def enforce_direction_consistency(
    plans: list[ActionPlan],
    regime_type: str,
    direction: str,
    trend_regimes: set[str] | None = None,
) -> list[ActionPlan]:
    """Strip contradicting entries from trend-regime plans.

    Only applies when ``regime_type`` is a trend regime (GAP_GO / TREND_STRONG / TREND_WEAK).
    Plan C is always exempt.  RANGE / UNCLEAR are unaffected.
    """
    if trend_regimes is None:
        trend_regimes = {"GAP_GO", "TREND_STRONG", "TREND_WEAK"}
    if regime_type not in trend_regimes:
        return plans
    if direction not in ("bullish", "bearish"):
        return plans

    opposite = "bearish" if direction == "bullish" else "bullish"
    for plan in plans:
        if plan.label == "C":
            continue
        # F1: exempt Plan B when it's a hedge (opposite direction)
        if plan.label == "B" and plan.direction != direction:
            continue
        if plan.direction == opposite and plan.entry is not None:
            plan.entry = None
            plan.entry_action = ""
            w = f"方向与 {regime_type} {direction} 矛盾, 入场已移除"
            plan.warning = f"{plan.warning}; {w}" if plan.warning else w
    return plans


def apply_market_direction_warning(
    plans: list[ActionPlan], ctx: PlanContext,
) -> list[ActionPlan]:
    """Warn when plan direction conflicts with market (SPY) direction.

    Only warns — does not demote.  Skips when market_direction is empty or neutral.
    When decoupled_from_benchmark is True, downgrades to informational note.
    """
    mkt = ctx.market_direction
    if not mkt or mkt == "neutral":
        return plans
    for plan in plans:
        if plan.label == "C" or plan.entry is None:
            continue
        if plan.direction in ("bullish", "bearish") and plan.direction != mkt:
            mkt_cn = "偏多" if mkt == "bullish" else "偏空"
            dir_cn = "做多" if plan.direction == "bullish" else "做空"
            if ctx.decoupled_from_benchmark:
                w = f"大盘 {mkt_cn}, 个股 {dir_cn} 逆势 (个股脱钩, 权重降低)"
            else:
                w = f"大盘 {mkt_cn}, 个股 {dir_cn} 逆势, 注意风险"
            plan.warning = f"{plan.warning}; {w}" if plan.warning else w
    return plans
