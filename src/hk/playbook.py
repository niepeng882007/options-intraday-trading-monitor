"""HK Playbook — generate and format aggregated playbook messages.

Upgraded to 5-section institutional-grade format with ActionPlan engine,
matching the US Playbook architecture.
"""

from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone

from src.common.action_plan import (
    ActionPlan,
    PlanContext,
    apply_min_rr_gate as _apply_min_rr_gate,
    apply_wait_coherence as _apply_wait_coherence,
    calculate_rr as _calculate_rr,
    cap_tp2 as _cap_tp2_common,
    check_entry_reachability as _check_entry_reachability,
    compact_option_line as _compact_option_line,
    find_fade_entry_zone as _find_fade_entry_zone_common,
    format_action_plan as _format_action_plan,
    nearest_levels as _nearest_levels_common,
)
from src.common.formatting import (
    closest_value_area_edge as _closest_value_area_edge,
    confidence_bar as _confidence_bar,
    format_leg_line as _format_leg_line,  # noqa: F401 — re-export for tests
    format_percent as _format_percent,
    pct_change as _pct_change,
)
from src.common.types import (
    FilterResult,
    GammaWallResult,
    OptionMarketSnapshot,
    OptionRecommendation,
    QuoteSnapshot,
    VolumeProfileResult,
)
from src.hk import (
    HKKeyLevels,
    Playbook,
    RegimeResult,
    RegimeType,
)
from src.hk.indicators import hk_key_levels_to_dict, minutes_to_close_hk
from src.utils.logger import setup_logger

logger = setup_logger("hk_playbook")

HKT = timezone(timedelta(hours=8))
_esc = html.escape

# ── Regime display maps (new 5-class + deprecated compat) ──

REGIME_EMOJI = {
    RegimeType.GAP_AND_GO: "🚀",
    RegimeType.TREND_DAY: "📈",
    RegimeType.FADE_CHOP: "📦",
    RegimeType.WHIPSAW: "🌪️",
    RegimeType.UNCLEAR: "❓",
    # Deprecated — backward compat
    RegimeType.BREAKOUT: "🚀",
    RegimeType.RANGE: "📦",
}

REGIME_NAME_CN = {
    RegimeType.GAP_AND_GO: "缺口追击日",
    RegimeType.TREND_DAY: "趋势日",
    RegimeType.FADE_CHOP: "震荡日",
    RegimeType.WHIPSAW: "高波洗盘日",
    RegimeType.UNCLEAR: "不明确日",
    # Deprecated
    RegimeType.BREAKOUT: "单边突破日",
    RegimeType.RANGE: "区间震荡日",
}


def get_hk_regime_emoji(regime: RegimeType, direction: str) -> str:
    """Direction-aware emoji for TREND_DAY and GAP_AND_GO."""
    if regime == RegimeType.TREND_DAY:
        return "📈" if direction == "bullish" else "📉"
    if regime == RegimeType.GAP_AND_GO:
        return "🚀" if direction == "bullish" else "💥"
    return REGIME_EMOJI.get(regime, "❓")


SECTION_SEP = "─ ─ ─ ─ ─ ─ ─ ─ ─ ─"


# ── Key levels dict helper ──


def _hk_levels_dict(
    kl: HKKeyLevels | None,
    vp: VolumeProfileResult | None = None,
    gamma_wall: GammaWallResult | None = None,
) -> dict[str, float]:
    """Convert HK levels to dict for common ActionPlan functions."""
    if kl is not None:
        return hk_key_levels_to_dict(kl)
    # Fallback: build from VP + gamma_wall
    d: dict[str, float] = {}
    if vp:
        if vp.poc > 0:
            d["POC"] = vp.poc
        if vp.vah > 0:
            d["VAH"] = vp.vah
        if vp.val > 0:
            d["VAL"] = vp.val
    if gamma_wall:
        if gamma_wall.call_wall_strike > 0:
            d["Call Wall"] = gamma_wall.call_wall_strike
        if gamma_wall.put_wall_strike > 0:
            d["Put Wall"] = gamma_wall.put_wall_strike
    return d


# ── ActionPlan generation engine ──


def _nearest_levels(
    price: float, side: str, levels: dict[str, float], n: int = 2,
) -> list[tuple[str, float]]:
    return _nearest_levels_common(price, side, levels, n)


def _find_fade_entry_zone(
    va_edge: float, opposite_edge: float, levels: dict[str, float],
) -> tuple[str, float] | None:
    return _find_fade_entry_zone_common(va_edge, opposite_edge, levels)


def _cap_tp2(
    plan: ActionPlan, ctx: PlanContext, levels: dict[str, float],
) -> ActionPlan:
    return _cap_tp2_common(plan, ctx, levels)


def _plans_trend_bullish(
    price: float, vp: VolumeProfileResult, levels: dict[str, float],
    vwap: float, option_line: str | None,
) -> list[ActionPlan]:
    """GAP_AND_GO / TREND_DAY bullish plans."""
    above = _nearest_levels(price, "above", levels, n=3)
    below_vwap = _nearest_levels(vwap, "below", levels, n=1) if vwap > 0 else []
    sl_a = below_vwap[0] if below_vwap else None
    tp1_a = above[0] if above else None
    tp2_a = above[1] if len(above) >= 2 else None

    plan_a = ActionPlan(
        label="A", name="趋势回调做多", emoji="📈", is_primary=True,
        logic="回调至 VWAP 附近接多, 顺势而为",
        direction="bullish",
        trigger=f"价格回调至 VWAP {vwap:,.2f} 附近企稳" if vwap > 0 else "价格回调至支撑位企稳",
        entry=vwap if vwap > 0 else None, entry_action="做多",
        stop_loss=sl_a[1] if sl_a else None,
        stop_loss_reason=sl_a[0] if sl_a else "VWAP 下方",
        tp1=tp1_a[1] if tp1_a else None,
        tp1_label=tp1_a[0] if tp1_a else "",
        tp2=tp2_a[1] if tp2_a else None,
        tp2_label=tp2_a[0] if tp2_a else "",
        rr_ratio=_calculate_rr(
            vwap if vwap > 0 else None,
            sl_a[1] if sl_a else None,
            tp1_a[1] if tp1_a else None,
        ),
        option_line=option_line,
    )

    # Plan B: breakout add-on
    entry_b = tp1_a[1] if tp1_a else None
    above_b = _nearest_levels(entry_b, "above", levels, n=2) if entry_b else []
    tp1_b = above_b[0] if above_b else None
    tp2_b = above_b[1] if len(above_b) >= 2 else None
    plan_b = ActionPlan(
        label="B", name="突破加仓", emoji="📈", is_primary=False,
        logic=f"突破 {tp1_a[0] if tp1_a else '阻力'} 后追多",
        direction="bullish",
        trigger=f"放量突破 {tp1_a[0]} {tp1_a[1]:,.2f}" if tp1_a else "放量突破最近阻力",
        entry=entry_b, entry_action="做多",
        stop_loss=tp1_a[1] if tp1_a else None,
        stop_loss_reason=f"回落 {tp1_a[0]} 下方" if tp1_a else "突破位下方",
        tp1=tp1_b[1] if tp1_b else None,
        tp1_label=tp1_b[0] if tp1_b else "",
        tp2=tp2_b[1] if tp2_b else None,
        tp2_label=tp2_b[0] if tp2_b else "",
        rr_ratio=_calculate_rr(entry_b, tp1_a[1] if tp1_a else None, tp1_b[1] if tp1_b else None),
    )

    plan_c = ActionPlan(
        label="C", name="失效反转", emoji="⚡", is_primary=False,
        logic="价格跌回 VA 内 + RVOL 回落 → 转震荡日",
        direction="bearish",
        trigger=f"跌破 VAH {vp.vah:,.2f} + RVOL 回落",
        entry=None, entry_action="",
        stop_loss=None, stop_loss_reason="",
        tp1=None, tp1_label="", tp2=None, tp2_label="", rr_ratio=0.0,
    )
    return [plan_a, plan_b, plan_c]


def _plans_trend_bearish(
    price: float, vp: VolumeProfileResult, levels: dict[str, float],
    vwap: float, option_line: str | None,
) -> list[ActionPlan]:
    """GAP_AND_GO / TREND_DAY bearish plans."""
    below = _nearest_levels(price, "below", levels, n=3)
    above_vwap = _nearest_levels(vwap, "above", levels, n=1) if vwap > 0 else []
    sl_a = above_vwap[0] if above_vwap else None
    tp1_a = below[0] if below else None
    tp2_a = below[1] if len(below) >= 2 else None

    plan_a = ActionPlan(
        label="A", name="趋势反弹做空", emoji="📉", is_primary=True,
        logic="反弹至 VWAP 附近接空, 顺势而为",
        direction="bearish",
        trigger=f"价格反弹至 VWAP {vwap:,.2f} 附近受阻" if vwap > 0 else "价格反弹至阻力位受阻",
        entry=vwap if vwap > 0 else None, entry_action="做空",
        stop_loss=sl_a[1] if sl_a else None,
        stop_loss_reason=sl_a[0] if sl_a else "VWAP 上方",
        tp1=tp1_a[1] if tp1_a else None,
        tp1_label=tp1_a[0] if tp1_a else "",
        tp2=tp2_a[1] if tp2_a else None,
        tp2_label=tp2_a[0] if tp2_a else "",
        rr_ratio=_calculate_rr(
            vwap if vwap > 0 else None,
            sl_a[1] if sl_a else None,
            tp1_a[1] if tp1_a else None,
        ),
        option_line=option_line,
    )

    entry_b = tp1_a[1] if tp1_a else None
    below_b = _nearest_levels(entry_b, "below", levels, n=2) if entry_b else []
    tp1_b = below_b[0] if below_b else None
    tp2_b = below_b[1] if len(below_b) >= 2 else None
    plan_b = ActionPlan(
        label="B", name="破位加仓", emoji="📉", is_primary=False,
        logic=f"跌破 {tp1_a[0] if tp1_a else '支撑'} 后追空",
        direction="bearish",
        trigger=f"放量跌破 {tp1_a[0]} {tp1_a[1]:,.2f}" if tp1_a else "放量跌破最近支撑",
        entry=entry_b, entry_action="做空",
        stop_loss=tp1_a[1] if tp1_a else None,
        stop_loss_reason=f"反弹 {tp1_a[0]} 上方" if tp1_a else "破位位上方",
        tp1=tp1_b[1] if tp1_b else None,
        tp1_label=tp1_b[0] if tp1_b else "",
        tp2=tp2_b[1] if tp2_b else None,
        tp2_label=tp2_b[0] if tp2_b else "",
        rr_ratio=_calculate_rr(entry_b, tp1_a[1] if tp1_a else None, tp1_b[1] if tp1_b else None),
    )

    plan_c = ActionPlan(
        label="C", name="失效反转", emoji="⚡", is_primary=False,
        logic="价格涨回 VA 内 + RVOL 回落 → 转震荡日",
        direction="bullish",
        trigger=f"站回 VAL {vp.val:,.2f} + RVOL 回落",
        entry=None, entry_action="",
        stop_loss=None, stop_loss_reason="",
        tp1=None, tp1_label="", tp2=None, tp2_label="", rr_ratio=0.0,
    )
    return [plan_a, plan_b, plan_c]


def _plans_fade_bearish(
    price: float, vp: VolumeProfileResult, levels: dict[str, float],
    vwap: float, option_line: str | None,
) -> list[ActionPlan]:
    """FADE_CHOP near VAH → short bias."""
    above = _nearest_levels(vp.vah, "above", levels, n=1)
    sl_a = above[0] if above else None

    zone = _find_fade_entry_zone(vp.vah, vp.val, levels)

    plan_a = ActionPlan(
        label="A", name="上沿做空", emoji="📉", is_primary=True,
        logic="价格进入 VA 上沿区间, 博均值回归做空" if zone
              else "价格靠近 VAH, 博均值回归做空",
        direction="bearish",
        trigger=f"价格进入 {zone[0]}→VAH 区间 ({zone[1]:,.2f}-{vp.vah:,.2f})" if zone
                else f"价格触及 VAH {vp.vah:,.2f} 附近",
        entry=vp.vah, entry_action="做空",
        entry_zone_price=zone[1] if zone else None,
        entry_zone_label=zone[0] if zone else "",
        stop_loss=sl_a[1] if sl_a else None,
        stop_loss_reason=sl_a[0] if sl_a else "VAH 上方",
        tp1=vp.poc, tp1_label="POC",
        tp2=vp.val, tp2_label="VAL",
        rr_ratio=_calculate_rr(vp.vah, sl_a[1] if sl_a else None, vp.poc),
        option_line=option_line,
    )

    # Plan B: VWAP regression
    plan_b_entry = vwap if vwap > vp.poc else None
    if plan_b_entry:
        sl_b_candidates = _nearest_levels(plan_b_entry, "above", levels, n=1)
        sl_b_price = sl_b_candidates[0][1] if sl_b_candidates else vp.vah
        sl_b_label = sl_b_candidates[0][0] if sl_b_candidates else "VAH"
    else:
        sl_b_price = None
        sl_b_label = ""
    plan_b = ActionPlan(
        label="B", name="VWAP 回归做空", emoji="📉", is_primary=False,
        logic="VWAP 上方接空, 目标 POC",
        direction="bearish",
        trigger=f"价格反弹至 VWAP {vwap:,.2f}" if plan_b_entry else "价格反弹至 VAH 附近",
        entry=plan_b_entry, entry_action="做空",
        stop_loss=sl_b_price, stop_loss_reason=sl_b_label,
        tp1=vp.poc, tp1_label="POC",
        tp2=vp.val, tp2_label="VAL",
        rr_ratio=_calculate_rr(plan_b_entry, sl_b_price, vp.poc),
    )

    plan_c = ActionPlan(
        label="C", name="失效反转", emoji="⚡", is_primary=False,
        logic="放量突破 VAH → 转趋势日",
        direction="bullish",
        trigger=f"放量站稳 VAH {vp.vah:,.2f} 上方",
        entry=None, entry_action="",
        stop_loss=None, stop_loss_reason="",
        tp1=None, tp1_label="", tp2=None, tp2_label="", rr_ratio=0.0,
    )
    return [plan_a, plan_b, plan_c]


def _plans_fade_bullish(
    price: float, vp: VolumeProfileResult, levels: dict[str, float],
    vwap: float, option_line: str | None,
) -> list[ActionPlan]:
    """FADE_CHOP near VAL → long bias."""
    below = _nearest_levels(vp.val, "below", levels, n=1)
    sl_a = below[0] if below else None

    zone = _find_fade_entry_zone(vp.val, vp.vah, levels)

    plan_a = ActionPlan(
        label="A", name="下沿做多", emoji="📈", is_primary=True,
        logic="价格进入 VA 下沿区间, 博均值回归做多" if zone
              else "价格靠近 VAL, 博均值回归做多",
        direction="bullish",
        trigger=f"价格进入 VAL→{zone[0]} 区间 ({vp.val:,.2f}-{zone[1]:,.2f})" if zone
                else f"价格触及 VAL {vp.val:,.2f} 附近",
        entry=vp.val, entry_action="做多",
        entry_zone_price=zone[1] if zone else None,
        entry_zone_label=zone[0] if zone else "",
        stop_loss=sl_a[1] if sl_a else None,
        stop_loss_reason=sl_a[0] if sl_a else "VAL 下方",
        tp1=vp.poc, tp1_label="POC",
        tp2=vp.vah, tp2_label="VAH",
        rr_ratio=_calculate_rr(vp.val, sl_a[1] if sl_a else None, vp.poc),
        option_line=option_line,
    )

    # Plan B: VWAP regression
    plan_b_entry = vwap if vwap < vp.poc else None
    if plan_b_entry:
        sl_b_candidates = _nearest_levels(plan_b_entry, "below", levels, n=1)
        sl_b_price = sl_b_candidates[0][1] if sl_b_candidates else vp.val
        sl_b_label = sl_b_candidates[0][0] if sl_b_candidates else "VAL"
    else:
        sl_b_price = None
        sl_b_label = ""
    plan_b = ActionPlan(
        label="B", name="VWAP 回归做多", emoji="📈", is_primary=False,
        logic="VWAP 下方接多, 目标 POC",
        direction="bullish",
        trigger=f"价格回调至 VWAP {vwap:,.2f}" if plan_b_entry else "价格回调至 VAL 附近",
        entry=plan_b_entry, entry_action="做多",
        stop_loss=sl_b_price, stop_loss_reason=sl_b_label,
        tp1=vp.poc, tp1_label="POC",
        tp2=vp.vah, tp2_label="VAH",
        rr_ratio=_calculate_rr(plan_b_entry, sl_b_price, vp.poc),
    )

    plan_c = ActionPlan(
        label="C", name="失效反转", emoji="⚡", is_primary=False,
        logic="放量跌破 VAL → 转趋势日",
        direction="bearish",
        trigger=f"放量跌破 VAL {vp.val:,.2f}",
        entry=None, entry_action="",
        stop_loss=None, stop_loss_reason="",
        tp1=None, tp1_label="", tp2=None, tp2_label="", rr_ratio=0.0,
    )
    return [plan_a, plan_b, plan_c]


def _plans_whipsaw(
    price: float, vp: VolumeProfileResult, levels: dict[str, float],
    vwap: float, regime: RegimeResult, option_line: str | None,
) -> list[ActionPlan]:
    """WHIPSAW plans — wait for confirmation, reduced size."""
    plan_a = ActionPlan(
        label="A", name="等待确认后入场", emoji="🌪️", is_primary=True,
        logic="等待价格脱离双扫区间 + 带量确认方向后入场",
        direction="neutral",
        trigger="价格带量突破 IB/VA 边界并站稳",
        entry=None, entry_action="",
        stop_loss=None, stop_loss_reason="",
        tp1=None, tp1_label="", tp2=None, tp2_label="", rr_ratio=0.0,
    )

    # Plan B: light position if lean is detectable
    lean = regime.direction or "neutral"
    if lean in ("bullish", "bearish"):
        dir_cn = "做多" if lean == "bullish" else "做空"
        plan_b = ActionPlan(
            label="B", name=f"轻仓{dir_cn}", emoji="📈" if lean == "bullish" else "📉",
            is_primary=False,
            logic=f"方向偏 {lean}, 半仓试探",
            direction=lean,
            trigger=f"价格确认 VWAP {'上方' if lean == 'bullish' else '下方'}",
            entry=vwap if vwap > 0 else None, entry_action=dir_cn,
            stop_loss=None, stop_loss_reason="严格止损, 仓位减半",
            tp1=vp.vah if lean == "bullish" else vp.val,
            tp1_label="VAH" if lean == "bullish" else "VAL",
            tp2=None, tp2_label="", rr_ratio=0.0,
            option_line=option_line,
        )
    else:
        plan_b = ActionPlan(
            label="B", name="观察关键位", emoji="👀", is_primary=False,
            logic="观察 IB/VA 边界反应后决策",
            direction="neutral",
            trigger=f"价格触及 VAH {vp.vah:,.2f} 或 VAL {vp.val:,.2f}",
            entry=None, entry_action="",
            stop_loss=None, stop_loss_reason="",
            tp1=None, tp1_label="", tp2=None, tp2_label="", rr_ratio=0.0,
        )

    plan_c = ActionPlan(
        label="C", name="保持空仓", emoji="⚡", is_primary=False,
        logic="波动过大, 不参与",
        direction="neutral",
        trigger="双扫持续 + 方向信号持续矛盾",
        entry=None, entry_action="",
        stop_loss=None, stop_loss_reason="",
        tp1=None, tp1_label="", tp2=None, tp2_label="", rr_ratio=0.0,
    )
    return [plan_a, plan_b, plan_c]


def _plans_unclear(
    price: float, vp: VolumeProfileResult, levels: dict[str, float],
    vwap: float, regime: RegimeResult, option_line: str | None,
) -> list[ActionPlan]:
    """UNCLEAR plans."""
    lean = regime.lean or "neutral"

    plan_a = ActionPlan(
        label="A", name="等待确认", emoji="⏳", is_primary=True,
        logic="多空信号混杂, 等待方向明确后入场",
        direction="neutral",
        trigger="Regime 转为趋势日或震荡日",
        entry=None, entry_action="",
        stop_loss=None, stop_loss_reason="",
        tp1=None, tp1_label="", tp2=None, tp2_label="", rr_ratio=0.0,
    )

    if lean in ("bullish", "bearish"):
        dir_cn = "做多" if lean == "bullish" else "做空"
        plan_b = ActionPlan(
            label="B", name=f"轻仓{dir_cn}", emoji="📈" if lean == "bullish" else "📉",
            is_primary=False,
            logic=f"有 {lean} 倾向, 轻仓试探",
            direction=lean,
            trigger=f"价格确认 VWAP {'上方' if lean == 'bullish' else '下方'}",
            entry=vwap if vwap > 0 else None, entry_action=dir_cn,
            stop_loss=None, stop_loss_reason="严格止损",
            tp1=vp.vah if lean == "bullish" else vp.val,
            tp1_label="VAH" if lean == "bullish" else "VAL",
            tp2=None, tp2_label="", rr_ratio=0.0,
            option_line=option_line,
        )
    else:
        plan_b = ActionPlan(
            label="B", name="观察关键位", emoji="👀", is_primary=False,
            logic="观察 VA 边界和 VWAP 的反应",
            direction="neutral",
            trigger=f"价格触及 VAH {vp.vah:,.2f} 或 VAL {vp.val:,.2f}",
            entry=None, entry_action="",
            stop_loss=None, stop_loss_reason="",
            tp1=None, tp1_label="", tp2=None, tp2_label="", rr_ratio=0.0,
        )

    plan_c = ActionPlan(
        label="C", name="保持空仓", emoji="⚡", is_primary=False,
        logic="无明确信号时保留资金",
        direction="neutral",
        trigger="全天信号混杂",
        entry=None, entry_action="",
        stop_loss=None, stop_loss_reason="",
        tp1=None, tp1_label="", tp2=None, tp2_label="", rr_ratio=0.0,
    )
    return [plan_a, plan_b, plan_c]


def _generate_action_plans(
    regime: RegimeResult,
    direction: str,
    vp: VolumeProfileResult,
    levels: dict[str, float],
    vwap: float,
    option_rec: OptionRecommendation | None,
    ctx: PlanContext | None = None,
) -> list[ActionPlan]:
    """Generate A/B/C action plans based on regime and direction."""
    price = regime.price
    option_line = _compact_option_line(option_rec) if option_rec else None

    if regime.regime in (RegimeType.GAP_AND_GO, RegimeType.TREND_DAY):
        if direction == "bullish":
            plans = _plans_trend_bullish(price, vp, levels, vwap, option_line)
        else:
            plans = _plans_trend_bearish(price, vp, levels, vwap, option_line)
    elif regime.regime == RegimeType.FADE_CHOP:
        edge, _ = _closest_value_area_edge(price, vp)
        if edge == "VAH":
            plans = _plans_fade_bearish(price, vp, levels, vwap, option_line)
        else:
            plans = _plans_fade_bullish(price, vp, levels, vwap, option_line)
    elif regime.regime == RegimeType.WHIPSAW:
        plans = _plans_whipsaw(price, vp, levels, vwap, regime, option_line)
    else:
        # UNCLEAR
        plans = _plans_unclear(price, vp, levels, vwap, regime, option_line)

    # Post-processing
    if ctx:
        plans = [_cap_tp2(p, ctx, levels) for p in plans]
        plans = [_check_entry_reachability(p, price, ctx) for p in plans]
        plans = _apply_wait_coherence(plans, ctx)
        plans = _apply_min_rr_gate(plans, ctx)
    return plans


# ── Playbook object generation ──


def generate_playbook(
    regime: RegimeResult,
    vp: VolumeProfileResult,
    vwap: float,
    gamma_wall: GammaWallResult | None = None,
    filters: FilterResult | None = None,
    symbol: str = "",
    update_type: str = "morning",
    option_rec: OptionRecommendation | None = None,
    quote: QuoteSnapshot | None = None,
    option_market: OptionMarketSnapshot | None = None,
    key_levels_obj: HKKeyLevels | None = None,
    avg_daily_range_pct: float = 0.0,
) -> Playbook:
    """Generate a complete Playbook object."""
    if filters is None:
        filters = FilterResult(tradeable=True)

    # Build key_levels dict from HKKeyLevels or fallback to VP+gamma
    if key_levels_obj is not None:
        key_levels = hk_key_levels_to_dict(key_levels_obj)
    else:
        key_levels = {
            "POC": vp.poc,
            "VAH": vp.vah,
            "VAL": vp.val,
            "VWAP": vwap,
        }
        if gamma_wall:
            if gamma_wall.call_wall_strike > 0:
                key_levels["Gamma Call Wall"] = gamma_wall.call_wall_strike
            if gamma_wall.put_wall_strike > 0:
                key_levels["Gamma Put Wall"] = gamma_wall.put_wall_strike
            if gamma_wall.max_pain > 0:
                key_levels["Max Pain"] = gamma_wall.max_pain

    strategy_text = ""  # ActionPlan engine replaces old strategy_text

    return Playbook(
        regime=regime,
        volume_profile=vp,
        gamma_wall=gamma_wall,
        filters=filters,
        vwap=vwap,
        quote=quote,
        option_market=option_market,
        key_levels=key_levels,
        strategy_text=strategy_text,
        generated_at=datetime.now(HKT),
        option_rec=option_rec,
        key_levels_obj=key_levels_obj,
        avg_daily_range_pct=avg_daily_range_pct,
    )


# ── Formatting helpers ──


def _format_turnover(turnover: float) -> str:
    if turnover >= 1e8:
        return f"{turnover / 1e8:.2f} 亿 HKD"
    if turnover >= 1e4:
        return f"{turnover / 1e4:.2f} 万 HKD"
    return f"{turnover:,.0f} HKD"


def _price_position(price: float, vp: VolumeProfileResult, vwap: float) -> str:
    """Describe price position relative to VA and VWAP."""
    parts = []
    if price > vp.vah:
        parts.append("VAH 上方")
    elif price < vp.val:
        parts.append("VAL 下方")
    else:
        parts.append("VA 内部")

    if vwap > 0:
        if price > vwap:
            parts.append("VWAP 上方")
        else:
            parts.append("VWAP 下方")

    return "价格位于 " + ", ".join(parts)


def _rvol_assessment(rvol: float) -> str:
    if rvol < 0.5:
        return "极寒"
    if rvol < 0.8:
        return "偏弱"
    if rvol < 1.2:
        return "正常"
    if rvol < 1.5:
        return "活跃"
    return "趋势级"


def _core_conclusion_text(
    regime: RegimeResult,
    direction: str,
    vp: VolumeProfileResult,
    vwap: float,
    option_rec: OptionRecommendation | None,
) -> str:
    """One-line action directive."""
    if option_rec and option_rec.action == "wait":
        if option_rec.wait_conditions:
            return f"观望, 等待 {option_rec.wait_conditions[0]}"
        return "观望, 等待方向明确"

    price = regime.price
    if regime.regime in (RegimeType.GAP_AND_GO, RegimeType.TREND_DAY):
        if direction == "bullish":
            return f"回调至 VWAP {vwap:,.2f} 附近做多" if vwap > 0 else "回调至支撑位做多"
        return f"反弹至 VWAP {vwap:,.2f} 附近做空" if vwap > 0 else "反弹至阻力位做空"

    if regime.regime == RegimeType.FADE_CHOP:
        edge, dist = _closest_value_area_edge(price, vp)
        va_width = vp.vah - vp.val
        if va_width > 0 and dist / va_width > 0.5:
            return "观望, 等待价格靠近 VA 边沿再入场"
        if edge == "VAH":
            return f"VAH {vp.vah:,.2f} 附近做空, 目标 POC {vp.poc:,.2f}"
        return f"VAL {vp.val:,.2f} 附近做多, 目标 POC {vp.poc:,.2f}"

    if regime.regime == RegimeType.WHIPSAW:
        return "波动放大、方向不稳, 等待确认后再入场"

    return "观望, 等待 Regime 明确后再入场"


def _alternate_regime_info(regime: RegimeResult) -> tuple[str, int]:
    """Infer alternate regime name and probability."""
    conf_pct = int(regime.confidence * 100)
    alt_map = {
        RegimeType.GAP_AND_GO: "趋势日",
        RegimeType.TREND_DAY: "震荡日",
        RegimeType.FADE_CHOP: "趋势日",
        RegimeType.WHIPSAW: "震荡日",
        RegimeType.UNCLEAR: "震荡日",
    }
    return alt_map.get(regime.regime, "震荡日"), 100 - conf_pct


# ── DEPRECATED: preserved for backtest report compatibility ──


def _regime_conclusion(
    regime: RegimeResult,
    vp: VolumeProfileResult,
    vwap: float,
) -> str:
    # DEPRECATED — use _core_conclusion_text instead
    return _core_conclusion_text(regime, regime.direction or "neutral", vp, vwap, None)


def _regime_reason_lines(
    regime: RegimeResult,
    vp: VolumeProfileResult,
    vwap: float,
    gamma_wall: GammaWallResult | None,
    option_market: OptionMarketSnapshot | None,
    quote: QuoteSnapshot | None,
    option_rec: OptionRecommendation | None = None,
) -> tuple[list[str], list[str], list[str], list[str]]:
    # DEPRECATED — kept for any remaining callers
    reasons: list[str] = []
    supports: list[str] = []
    uncertainties: list[str] = []
    invalidations: list[str] = []
    reasons.append(f"Regime: {REGIME_NAME_CN.get(regime.regime, '未知')}")
    reasons.append(f"RVOL: {regime.rvol:.2f}")
    return reasons, supports, uncertainties, invalidations


# ── Main formatter: 5-section layout ──


def format_playbook_message(
    playbook: Playbook,
    symbol: str = "",
    update_type: str = "manual",
    hsi_regime: RegimeResult | None = None,
    hstech_regime: RegimeResult | None = None,
) -> str:
    """Format playbook as Telegram HTML message — 5-section institutional-grade output."""
    regime = playbook.regime
    now = playbook.generated_at or datetime.now(HKT)
    vwap = playbook.vwap
    vp = playbook.volume_profile
    gamma_wall = playbook.gamma_wall
    quote = playbook.quote
    option_market = playbook.option_market
    recommendation = playbook.option_rec
    kl = playbook.key_levels_obj

    # Direction
    if regime.direction:
        _direction = regime.direction
    elif regime.price > vp.vah:
        _direction = "bullish"
    elif regime.price < vp.val:
        _direction = "bearish"
    elif vp.poc > 0:
        _direction = "bullish" if regime.price > vp.poc else "bearish"
    else:
        _direction = "neutral"

    emoji = get_hk_regime_emoji(regime.regime, _direction)
    regime_cn = REGIME_NAME_CN.get(regime.regime, "未知")

    lines: list[str] = []
    sep = "━" * 20

    # ── Section 1: Header ──
    lines.append(f"━━━ <b>{_esc(symbol)}</b> | 日内交易剧本 ━━━")
    lines.append(f"⏰ {now.strftime('%Y-%m-%d %H:%M:%S')} HKT")

    # Market context (HSI / HSTECH)
    ctx_parts = []
    if hsi_regime:
        hsi_emoji = get_hk_regime_emoji(hsi_regime.regime, hsi_regime.direction or "neutral")
        hsi_cn = REGIME_NAME_CN.get(hsi_regime.regime, "未知")
        ctx_parts.append(f"HSI {hsi_emoji}{hsi_cn}")
    if hstech_regime:
        hst_emoji = get_hk_regime_emoji(hstech_regime.regime, hstech_regime.direction or "neutral")
        hst_cn = REGIME_NAME_CN.get(hstech_regime.regime, "未知")
        ctx_parts.append(f"HST {hst_emoji}{hst_cn}")
    if ctx_parts:
        lines.append(f"大盘: {' | '.join(ctx_parts)}")

    # Regime expectation
    alt_name, alt_pct = _alternate_regime_info(regime)
    conf_pct = int(regime.confidence * 100)
    lines.append(f"预期: {emoji}{regime_cn} ({conf_pct}%) / {alt_name} ({alt_pct}%)")
    lines.append(f"{_confidence_bar(regime.confidence)} {regime.confidence:.0%}")
    lines.append("")

    # ── Section 2: 核心结论 ──
    conclusion = _core_conclusion_text(regime, _direction, vp, vwap, recommendation)
    lines.append(f"🎯 <b>核心结论: {_esc(conclusion)}</b>")
    lines.append(f"▸ 当前状态: {_esc(_price_position(regime.price, vp, vwap))}")

    lines.append("")
    lines.append(SECTION_SEP)

    # ── Section 3: 剧本推演 ──
    lines.append("⚔️ <b>剧本推演</b>")
    lines.append("")

    # Build levels dict and PlanContext
    levels = _hk_levels_dict(kl, vp, gamma_wall)
    _min_left = minutes_to_close_hk(now)
    _intraday_range = 0.0
    if quote and quote.high_price > 0 and quote.low_price > 0:
        _intraday_range = (quote.high_price - quote.low_price) / quote.low_price * 100
    _plan_ctx = PlanContext(
        minutes_to_close=_min_left,
        total_session_minutes=330,
        rvol=regime.rvol,
        avg_daily_range_pct=playbook.avg_daily_range_pct,
        intraday_range_pct=_intraday_range,
        option_action=recommendation.action if recommendation else "",
    )

    plans = _generate_action_plans(
        regime, _direction, vp, levels, vwap, recommendation, ctx=_plan_ctx,
    )
    for plan in plans:
        for plan_line in _format_action_plan(plan):
            lines.append(plan_line)
        lines.append("")

    lines.append(SECTION_SEP)

    # ── Section 4: 盘面逻辑 ──
    lines.append("📝 <b>盘面逻辑</b>")
    rvol_label = _rvol_assessment(regime.rvol)
    lines.append(f"▸ 量能: RVOL {regime.rvol:.2f} ({rvol_label})")

    # Space analysis
    above = _nearest_levels(regime.price, "above", levels, n=1)
    below = _nearest_levels(regime.price, "below", levels, n=1)
    space_parts = []
    if above and regime.price > 0:
        up_pct = (above[0][1] - regime.price) / regime.price * 100
        space_parts.append(f"上方至{above[0][0]} {up_pct:.1f}%")
    if below and regime.price > 0:
        dn_pct = (regime.price - below[0][1]) / regime.price * 100
        space_parts.append(f"下方至{below[0][0]} {dn_pct:.1f}%")
    if space_parts:
        lines.append(f"▸ 空间: {' / '.join(space_parts)}")

    # IV environment
    if option_market and option_market.atm_iv > 0 and option_market.avg_iv > 0:
        iv_label = "偏高" if option_market.iv_ratio >= 1.2 else "偏低" if option_market.iv_ratio <= 0.85 else "正常"
        lines.append(f"▸ IV: ATM/Avg = {option_market.iv_ratio:.2f}x ({iv_label})")

    lines.append("")
    lines.append(SECTION_SEP)

    # ── Section 5: 数据雷达 ──
    price_display = quote.last_price if quote else regime.price
    lines.append(f"📊 <b>数据雷达</b> (当前: {price_display:,.2f})")

    # Compact key data line
    vwap_pct = _pct_change(regime.price, vwap)
    vwap_str = f"VWAP {vwap:,.2f} ({_format_percent(vwap_pct)})" if vwap > 0 else ""
    compact_items = [x for x in [vwap_str, f"RVOL {regime.rvol:.2f}"] if x]
    if quote:
        day_range_pct = _pct_change(quote.high_price, quote.low_price)
        if day_range_pct is not None:
            compact_items.append(f"振幅 {_format_percent(day_range_pct, signed=False)}")
    lines.append(" | ".join(compact_items))

    # VP levels
    lines.append(f"VAH {vp.vah:,.2f} | POC {vp.poc:,.2f} | VAL {vp.val:,.2f}")

    # HK-specific levels (IBH/IBL, PDH/PDL, PDC, Open)
    if kl:
        pd_parts = []
        if kl.pdh > 0:
            pd_parts.append(f"PDH {kl.pdh:,.2f}")
        if kl.pdl > 0:
            pd_parts.append(f"PDL {kl.pdl:,.2f}")
        if kl.pdc > 0:
            pd_parts.append(f"PDC {kl.pdc:,.2f}")
        if pd_parts:
            lines.append(" | ".join(pd_parts))

        ib_parts = []
        if kl.ibh > 0:
            ib_parts.append(f"IBH {kl.ibh:,.2f}")
        if kl.ibl > 0:
            ib_parts.append(f"IBL {kl.ibl:,.2f}")
        if kl.day_open > 0:
            ib_parts.append(f"Open {kl.day_open:,.2f}")
        if ib_parts:
            lines.append(" | ".join(ib_parts))

    # Gamma wall
    if gamma_wall:
        gw_parts = []
        if gamma_wall.call_wall_strike > 0:
            gw_parts.append(f"Call Wall {gamma_wall.call_wall_strike:,.0f}")
        if gamma_wall.put_wall_strike > 0:
            gw_parts.append(f"Put Wall {gamma_wall.put_wall_strike:,.0f}")
        if gamma_wall.max_pain > 0:
            gw_parts.append(f"MaxPain {gamma_wall.max_pain:,.0f}")
        if gw_parts:
            lines.append(" | ".join(gw_parts))

    # Filter warnings
    for warning in playbook.filters.warnings:
        lines.append(f"⚠️ {_esc(warning)}")

    # DTE gamma warning
    if recommendation and recommendation.dte > 0 and recommendation.dte <= 3 and recommendation.action != "wait":
        lines.append(f"⚠️ 仅剩 {recommendation.dte} DTE, Gamma 风险极高")

    lines.append(sep)
    return "\n".join(lines)
