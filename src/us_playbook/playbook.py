"""US Playbook — format aggregated playbook messages (institutional-grade intraday playbook)."""

from __future__ import annotations

import html
from datetime import datetime
from zoneinfo import ZoneInfo

from src.common.action_plan import (  # noqa: F401 — re-export for backward compat
    ActionPlan,
    PlanContext,
    apply_gamma_wall_warning as _apply_gamma_wall_warning_common,
    apply_market_direction_warning as _apply_market_direction_warning,
    apply_min_rr_gate as _apply_min_rr_gate,
    apply_vwap_deviation_warning as _apply_vwap_deviation_warning_common,
    apply_wait_coherence as _apply_wait_coherence,
    calculate_rr as _calculate_rr,
    cap_tp1 as _cap_tp1_common,
    cap_tp2 as _cap_tp2_common,
    check_entry_reachability as _check_entry_reachability,
    compact_option_line as _compact_option_line,
    find_fade_entry_zone as _find_fade_entry_zone_common,
    format_action_plan as _format_action_plan,
    nearest_levels as _nearest_levels_common,
    reachable_range_pct as _reachable_range_pct,
)
from src.common.formatting import (
    action_label as _action_label,
    action_plain_language as _action_plain_language,
    closest_value_area_edge as _closest_value_area_edge,
    confidence_bar as _confidence_bar,
    format_leg_line as _format_leg_line,
    format_percent as _format_percent,
    format_strike as _format_strike,
    pct_change as _pct_change,
    position_size_text as _position_size_text,
    risk_status_text as _risk_status_text,
    split_reason_lines as _split_reason_lines,
    spread_execution_text as _spread_execution_text,
)
from src.common.types import (
    FilterResult,
    GammaWallResult,
    OptionMarketSnapshot,
    OptionRecommendation,
    QuoteSnapshot,
    VolumeProfileResult,
)
from src.us_playbook import KeyLevels, MarketTone, USPlaybookResult, USRegimeResult, USRegimeType
from src.us_playbook.option_recommend import _decide_direction
from src.utils.logger import setup_logger

logger = setup_logger("us_playbook")

ET = ZoneInfo("America/New_York")
_esc = html.escape

REGIME_EMOJI = {
    USRegimeType.GAP_AND_GO: "\U0001f680",   # 🚀
    USRegimeType.TREND_DAY: "\U0001f4c8",    # 📈
    USRegimeType.FADE_CHOP: "\U0001f4e6",    # 📦
    USRegimeType.UNCLEAR: "\u2753",           # ❓
}


def get_regime_emoji(regime: USRegimeType, direction: str) -> str:
    """Direction-aware emoji for TREND_DAY and GAP_AND_GO."""
    if regime == USRegimeType.TREND_DAY:
        return "\U0001f4c8" if direction == "bullish" else "\U0001f4c9"  # 📈 / 📉
    if regime == USRegimeType.GAP_AND_GO:
        return "\U0001f680" if direction == "bullish" else "\U0001f4a5"  # 🚀 / 💥
    return REGIME_EMOJI.get(regime, "\u2753")

REGIME_NAME_CN = {
    USRegimeType.GAP_AND_GO: "缺口追击日",
    USRegimeType.TREND_DAY: "趋势日",
    USRegimeType.FADE_CHOP: "震荡日",
    USRegimeType.UNCLEAR: "不明确日",
}

REGIME_STRATEGY = {
    USRegimeType.GAP_AND_GO: (
        "🚀 缺口追击 — 顺势操作\n"
        "• ATM/轻度 OTM 期权 (Delta 0.3-0.5)\n"
        "• VWAP 为止损线\n"
        "• 顺势加仓，不抄底/摸顶"
    ),
    USRegimeType.TREND_DAY: (
        "📈 趋势日 — 方向跟随\n"
        "• ATM 期权 (Delta 0.4-0.6)\n"
        "• PDH/PDL 为止损线\n"
        "• 目标 VAH/VAL → Gamma Wall"
    ),
    USRegimeType.FADE_CHOP: (
        "📦 震荡日 — 均值回归\n"
        "• 严禁 OTM，深度 ITM (Delta > 0.7)\n"
        "• VAH 附近做空，VAL 附近做多\n"
        "• 快进快出，不恋战"
    ),
    USRegimeType.UNCLEAR: (
        "❓ 观望为主 — 等待确认\n"
        "• 等待 Regime 明确后再入场\n"
        "• 仅参与高确定性机会\n"
        "• 仓位降至正常的 30%"
    ),
}


# ActionPlan, PlanContext, _calculate_rr, _reachable_range_pct, _compact_option_line,
# _format_action_plan are imported from src.common.action_plan above.


def get_regime_strategy(regime: USRegimeType, direction: str) -> str:
    """Direction-aware strategy text for TREND_DAY and GAP_AND_GO."""
    if regime == USRegimeType.TREND_DAY:
        if direction == "bullish":
            return (
                "📈 趋势日 — 向上跟随\n"
                "• ATM Call (Delta 0.4-0.6)\n"
                "• PDH 为止损参考\n"
                "• 目标 VAH → Call Wall"
            )
        return (
            "📉 趋势日 — 向下跟随\n"
            "• ATM Put (Delta 0.4-0.6)\n"
            "• PDL 为止损参考\n"
            "• 目标 VAL → Put Wall"
        )
    if regime == USRegimeType.GAP_AND_GO:
        if direction == "bullish":
            return (
                "🚀 缺口追击 — 向上顺势\n"
                "• ATM/轻度 OTM Call (Delta 0.3-0.5)\n"
                "• VWAP 为止损线\n"
                "• 顺势加仓，不抄底"
            )
        return (
            "💥 缺口追击 — 向下顺势\n"
            "• ATM/轻度 OTM Put (Delta 0.3-0.5)\n"
            "• VWAP 为止损线\n"
            "• 顺势加仓，不摸顶"
        )
    return REGIME_STRATEGY.get(regime, "")

SECTION_SEP = "─ ─ ─ ─ ─ ─ ─ ─ ─ ─"


def _infer_market_direction(result: USPlaybookResult | None) -> str:
    """Infer market direction from a USPlaybookResult (typically SPY).

    Uses _decide_direction first; falls back to price-vs-VWAP comparison.
    Returns "" if result is None.
    """
    if result is None:
        return ""
    r = result.regime
    vp = result.volume_profile
    kl = result.key_levels
    direction = _decide_direction(
        r, vp, vwap=kl.vwap, pdl=kl.pdl, pdh=kl.pdh, pml=kl.pml, pmh=kl.pmh,
    )
    if direction == "neutral" and kl.vwap > 0:
        direction = "bullish" if r.price > kl.vwap else "bearish"
    return direction


# ── Grade bar helper ──

_GRADE_BAR = {
    "A+": "████████",
    "A": "███████░",
    "B+": "██████░░",
    "B": "████░░░░",
    "C": "██░░░░░░",
    "D": "█░░░░░░░",
}

_GRADE_LABEL = {
    "A+": "强势对齐",
    "A": "多数对齐",
    "B+": "偏向有利",
    "B": "信号一般",
    "C": "信号混杂",
    "D": "高度不确定",
}

_POSITION_HINT_CN = {
    "full": "正常仓位",
    "reduced": "缩减仓位",
    "minimal": "观望为主，仅参与高确定性机会",
    "sit_out": "今日回避",
}


def _format_market_tone_section(tone: MarketTone) -> list[str]:
    """Format Section 0: Market Tone for Telegram HTML."""
    grade_bar = _GRADE_BAR.get(tone.grade, "????")
    grade_label = _GRADE_LABEL.get(tone.grade, "")
    lines: list[str] = []

    lines.append(f"\U0001f4cb <b>市场定调: {tone.grade}</b> {grade_bar} {_esc(grade_label)}")

    # Macro
    if tone.macro_signal == "clear":
        lines.append("├ 宏观: ✅ 无重大事件")
    elif tone.macro_signal == "range_then_trend":
        lines.append("├ 宏观: ⚠️ FOMC 日 (2PM 前震荡为主)")
    elif tone.macro_signal == "data_reaction":
        lines.append("├ 宏观: ⚠️ 数据日 (盘前发布，10AM 后交易)")
    else:
        lines.append("├ 宏观: ❌ 受限")

    # Gap
    gap_pct = tone.gap_pct
    if tone.gap_signal == "gap_and_go":
        emoji = "🚀" if gap_pct > 0 else "💥"
        lines.append(f"├ Gap: {emoji} SPY {gap_pct:+.1f}% 大缺口")
    elif tone.gap_signal == "gap_fill":
        lines.append(f"├ Gap: ↔ 小幅缺口 {gap_pct:+.1f}%")
    else:
        lines.append(f"├ Gap: ↔ 中性 {gap_pct:+.1f}%")

    # ORB
    if tone.orb:
        if tone.orb.confirmed and tone.orb.breakout_direction:
            dir_label = "上轨" if tone.orb.breakout_direction == "bullish" else "下轨"
            extra = " 10AM 确认" if tone.orb.reversal_failed else ""
            lines.append(f"├ ORB: ✅ 突破{dir_label}{extra}")
        elif tone.orb.high > 0:
            lines.append(f"├ ORB: ❓ 待确认 ({tone.orb.low:.2f}-{tone.orb.high:.2f})")
        else:
            lines.append("├ ORB: ❓ 数据不足")
    else:
        lines.append("├ ORB: ❓ 待确认")

    # VWAP
    if tone.vwap_status:
        vs = tone.vwap_status
        slope_arrow = {"rising": "↑", "falling": "↓", "flat": "→"}.get(vs.slope_label, "")
        pos_cn = "上方" if vs.position == "above" else "下方"
        # Check consistency
        consistent = (
            (vs.slope_label == "rising" and vs.position == "above")
            or (vs.slope_label == "falling" and vs.position == "below")
            or vs.slope_label == "flat"
        )
        emoji = "✅" if consistent else "❌"
        conflict_note = "" if consistent else " 矛盾"
        lines.append(f"├ VWAP: {emoji} SPY 在 VWAP {pos_cn} 斜率{slope_arrow}{conflict_note}")
    else:
        lines.append("├ VWAP: ❓ 数据不足")

    # Breadth
    if tone.breadth:
        b = tone.breadth
        if b.alignment_label == "strong_aligned":
            idx_note = " 指数共振" if b.index_aligned else ""
            lines.append(f"├ 广度: ✅ {b.aligned_count}/{b.total_count} 同向{idx_note}")
        elif b.alignment_label == "divergent":
            lines.append(f"├ 广度: ❌ 分化 ({b.details})")
        else:
            lines.append(f"├ 广度: ⚠️ 混合 ({b.details})")
    else:
        lines.append("├ 广度: ❓ 数据不足")

    # VIX
    if tone.vix:
        v = tone.vix
        if v.signal == "caution":
            lines.append(f"├ VIX: ⚠️ {v.level:.1f} ({v.change_pct:+.1f}%) 飙升")
        elif v.signal == "supportive":
            lines.append(f"├ VIX: ✅ {v.level:.1f} ({v.change_pct:+.1f}%) 回落中")
        else:
            lines.append(f"├ VIX: ↔ {v.level:.1f} ({v.change_pct:+.1f}%)")
        if v.stale:
            lines[-1] += " (延迟)"

    # Position hint
    dir_cn = {"bullish": "顺势做多", "bearish": "顺势做空", "neutral": "方向待定"}.get(
        tone.direction, ""
    )
    pos_cn = _POSITION_HINT_CN.get(tone.position_size_hint, "")
    if tone.grade in ("A+", "A"):
        lines.append(f"仓位: {pos_cn}，{dir_cn}")
    elif tone.grade in ("B+", "B"):
        lines.append(f"仓位: {pos_cn}")
    else:
        lines.append(f"仓位: {pos_cn}")

    return lines



def _format_turnover_usd(turnover: float) -> str:
    """Format turnover in USD (亿/万)."""
    if turnover >= 1e8:
        return f"{turnover / 1e8:.2f} 亿 USD"
    if turnover >= 1e4:
        return f"{turnover / 1e4:.2f} 万 USD"
    return f"{turnover:,.0f} USD"




def _price_position(
    price: float,
    vp: VolumeProfileResult,
    vwap: float,
    kl: KeyLevels,
) -> str:
    """Describe price position relative to VA, VWAP, and PM range."""
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

    # PM range position
    if kl.pmh > 0 and kl.pml > 0 and kl.pmh > kl.pml:
        if price > kl.pmh:
            parts.append("盘前高点上方")
        elif price < kl.pml:
            parts.append("盘前低点下方")

    return "价格位于 " + ", ".join(parts)


def _level_distance_items(
    price: float,
    vp: VolumeProfileResult,
    kl: KeyLevels,
    gamma_wall: GammaWallResult | None,
) -> list[str]:
    """Collect level distance strings, including PDH/PDL."""
    items: list[str] = []
    if vp.vah > 0 and price > 0:
        if price <= vp.vah:
            pct = (vp.vah - price) / price * 100
            items.append(f"VAH {vp.vah:,.2f} (↑{pct:.1f}%)")
        else:
            pct = (price - vp.vah) / price * 100
            items.append(f"VAH {vp.vah:,.2f} (已突破 {pct:.1f}%)")

    if vp.val > 0 and price > 0:
        if price >= vp.val:
            pct = (price - vp.val) / price * 100
            items.append(f"VAL {vp.val:,.2f} (↓{pct:.1f}%)")
        else:
            pct = (vp.val - price) / price * 100
            items.append(f"VAL {vp.val:,.2f} (已跌破 {pct:.1f}%)")

    if kl.pdh > 0 and price > 0:
        pct = (kl.pdh - price) / price * 100
        arrow = "↑" if kl.pdh > price else "↓"
        items.append(f"PDH {kl.pdh:,.2f} ({arrow}{abs(pct):.1f}%)")

    if kl.pdl > 0 and price > 0:
        pct = (price - kl.pdl) / price * 100
        arrow = "↓" if kl.pdl < price else "↑"
        items.append(f"PDL {kl.pdl:,.2f} ({arrow}{abs(pct):.1f}%)")

    if gamma_wall and price > 0:
        if gamma_wall.call_wall_strike > 0:
            pct = abs(gamma_wall.call_wall_strike - price) / price * 100
            arrow = "↑" if gamma_wall.call_wall_strike > price else "↓"
            items.append(f"Call Wall {gamma_wall.call_wall_strike:,.0f} ({arrow}{pct:.1f}%)")
        if gamma_wall.put_wall_strike > 0:
            pct = abs(price - gamma_wall.put_wall_strike) / price * 100
            arrow = "↓" if gamma_wall.put_wall_strike < price else "↑"
            items.append(f"Put Wall {gamma_wall.put_wall_strike:,.0f} ({arrow}{pct:.1f}%)")
    return items


def _us_key_levels_to_dict(
    vp: VolumeProfileResult,
    kl: KeyLevels | None = None,
    gamma_wall: GammaWallResult | None = None,
    current_price: float = 0.0,
    max_gamma_distance_pct: float = 10.0,
) -> dict[str, float]:
    """Convert US-specific VP/KeyLevels/GammaWall types to a flat dict.

    Gamma wall strikes farther than *max_gamma_distance_pct* % from
    *current_price* are excluded — they cannot serve as meaningful
    intraday SL/TP targets and would produce extreme R:R values.
    """
    d: dict[str, float] = {}
    if vp.poc > 0:
        d["POC"] = vp.poc
    if vp.vah > 0:
        d["VAH"] = vp.vah
    if vp.val > 0:
        d["VAL"] = vp.val
    if kl:
        if kl.vwap > 0:
            d["VWAP"] = kl.vwap
        if kl.pdh > 0:
            d["PDH"] = kl.pdh
        if kl.pdl > 0:
            d["PDL"] = kl.pdl
        if kl.pmh > 0:
            d["PMH"] = kl.pmh
        if kl.pml > 0:
            d["PML"] = kl.pml
    if gamma_wall and current_price > 0:
        if gamma_wall.call_wall_strike > 0:
            dist = abs(gamma_wall.call_wall_strike - current_price) / current_price * 100
            if dist <= max_gamma_distance_pct:
                d["Call Wall"] = gamma_wall.call_wall_strike
        if gamma_wall.put_wall_strike > 0:
            dist = abs(current_price - gamma_wall.put_wall_strike) / current_price * 100
            if dist <= max_gamma_distance_pct:
                d["Put Wall"] = gamma_wall.put_wall_strike
    elif gamma_wall:
        # No current_price → include both (backward compat)
        if gamma_wall.call_wall_strike > 0:
            d["Call Wall"] = gamma_wall.call_wall_strike
        if gamma_wall.put_wall_strike > 0:
            d["Put Wall"] = gamma_wall.put_wall_strike
    return d


def _nearest_levels(
    price: float,
    side: str,  # "above" | "below"
    vp: VolumeProfileResult,
    kl: KeyLevels | None = None,
    gamma_wall: GammaWallResult | None = None,
    n: int = 2,
) -> list[tuple[str, float]]:
    """Find nearest key levels above/below current price (wrapper)."""
    levels = _us_key_levels_to_dict(vp, kl, gamma_wall, current_price=price)
    return _nearest_levels_common(price, side, levels, n)


def _find_fade_entry_zone(
    va_edge: float,
    opposite_edge: float,
    kl: KeyLevels,
    gamma_wall: GammaWallResult | None,
    current_price: float = 0.0,
) -> tuple[str, float] | None:
    """Find the nearest structural level within the VA upper/lower third (wrapper)."""
    levels = _us_key_levels_to_dict(VolumeProfileResult(0, 0, 0), kl, gamma_wall, current_price=current_price)
    return _find_fade_entry_zone_common(va_edge, opposite_edge, levels)


def _entry_check_text(
    rec: OptionRecommendation,
    regime: USRegimeResult,
    vp: VolumeProfileResult,
    kl: KeyLevels | None = None,
    gamma_wall: GammaWallResult | None = None,
) -> str:
    if rec.action == "bear_call_spread":
        return (
            f"只在价格靠近 VAH {vp.vah:,.2f} 一带但还没有带量站稳上方时考虑开仓；"
            "如果已经放量突破压力位，这笔单取消。"
        )
    if rec.action == "bull_put_spread":
        return (
            f"只在价格靠近 VAL {vp.val:,.2f} 一带但还没有带量跌破下方时考虑开仓；"
            "如果已经放量失守支撑位，这笔单取消。"
        )
    if rec.action == "call":
        if regime.regime == USRegimeType.FADE_CHOP:
            nearby = _nearest_levels(regime.price, "below", vp, kl, gamma_wall, n=1)
            if nearby:
                name, val = nearby[0]
                return f"只在价格回调至 {name} {val:,.2f} 附近、没有带量跌破下方时考虑买入 Call，不追已经瞬间拉高的合约。"
        return "只在价格仍沿着当前多头方向运行、没有跌回防守线下方时考虑买入，不追已经瞬间拉高很多的合约。"
    if rec.action == "put":
        if regime.regime == USRegimeType.FADE_CHOP:
            nearby = _nearest_levels(regime.price, "above", vp, kl, gamma_wall, n=1)
            if nearby:
                name, val = nearby[0]
                return f"只在价格反弹至 {name} {val:,.2f} 附近、没有带量突破上方时考虑买入 Put，不追已经瞬间杀跌的合约。"
        return "只在价格仍沿着当前空头方向运行、没有重新站回防守线上方时考虑买入，不追已经瞬间杀跌很多的合约。"
    return "先等价格重新满足入场条件，再重新生成剧本。"


def _entry_zone_text(
    price: float,
    direction: str,
    regime: USRegimeResult,
    vp: VolumeProfileResult,
    kl: KeyLevels | None = None,
    gamma_wall: GammaWallResult | None = None,
) -> str | None:
    """Generate best entry zone text based on nearby key levels.

    Returns None for UNCLEAR/wait (no zone to show).
    """
    if regime.regime == USRegimeType.UNCLEAR:
        return None

    vwap = kl.vwap if kl else 0.0

    if regime.regime == USRegimeType.FADE_CHOP:
        if direction == "bearish":
            # Put: find resistance above
            nearby = _nearest_levels(price, "above", vp, kl, gamma_wall, n=2)
            if len(nearby) >= 2:
                n1, n2 = nearby[0], nearby[1]
                lo, hi = sorted([n1[1], n2[1]])
                lo_name = n1[0] if n1[1] == lo else n2[0]
                hi_name = n1[0] if n1[1] == hi else n2[0]
                return f"最佳入场区间: {lo:,.2f}-{hi:,.2f} ({lo_name} - {hi_name})"
            if len(nearby) == 1:
                return f"最佳入场位: {nearby[0][0]} {nearby[0][1]:,.2f} 附近"
            return "当前价已高于主要阻力位，可直接入场"
        else:
            # Call: find support below
            nearby = _nearest_levels(price, "below", vp, kl, gamma_wall, n=2)
            if len(nearby) >= 2:
                n1, n2 = nearby[0], nearby[1]
                lo, hi = sorted([n1[1], n2[1]])
                lo_name = n1[0] if n1[1] == lo else n2[0]
                hi_name = n1[0] if n1[1] == hi else n2[0]
                return f"最佳入场区间: {lo:,.2f}-{hi:,.2f} ({lo_name} - {hi_name})"
            if len(nearby) == 1:
                return f"最佳入场位: {nearby[0][0]} {nearby[0][1]:,.2f} 附近"
            return "当前价已低于主要支撑位，可直接入场"

    if regime.regime in (USRegimeType.GAP_AND_GO, USRegimeType.TREND_DAY):
        if vwap > 0:
            if direction == "bullish":
                return f"回调至 VWAP {vwap:,.2f} 附近是更优入场时机"
            else:
                return f"反弹至 VWAP {vwap:,.2f} 附近是更优入场时机"

    return None


def _entry_zone_distance_warning(
    price: float,
    entry_zone_text: str | None,
    direction: str,
    vp: VolumeProfileResult,
    kl: KeyLevels | None = None,
    gamma_wall: GammaWallResult | None = None,
) -> str | None:
    """Warn if current price is far (>1%) from the entry zone center."""
    if not entry_zone_text or price <= 0:
        return None

    # Find the nearest key level that forms the entry zone
    if direction == "bearish":
        nearby = _nearest_levels(price, "above", vp, kl, gamma_wall, n=2)
    else:
        nearby = _nearest_levels(price, "below", vp, kl, gamma_wall, n=2)

    if not nearby:
        return None

    if len(nearby) >= 2:
        zone_center = (nearby[0][1] + nearby[1][1]) / 2
    else:
        zone_center = nearby[0][1]

    dist_pct = abs(price - zone_center) / price * 100
    if dist_pct > 1.0:
        return f"\u26a0\ufe0f 当前价距入场区较远 ({dist_pct:.1f}%)，建议等待回调或放弃本轮"
    return None


def _risk_action_lines(
    rec: OptionRecommendation | None,
    regime: USRegimeResult,
    vp: VolumeProfileResult,
    kl: KeyLevels | None = None,
    gamma_wall: GammaWallResult | None = None,
) -> list[str]:
    if rec is None:
        return ["操作建议: 没有具体期权建议时，保持轻仓，只观察关键位反应。"]

    if rec.action == "wait":
        return [
            "操作建议: 当前不下单，保留资金，等重新评估条件满足后再考虑。",
        ]

    dte = rec.dte
    low_dte = dte > 0 and dte <= 3
    sm = rec.spread_metrics

    if rec.action == "bear_call_spread":
        stop_ref = f"盈亏平衡 {sm.breakeven:,.2f}" if sm and sm.breakeven > 0 else f"VAH {vp.vah:,.2f}"
        lines = [
            f"止损触发: 标的涨破{stop_ref}，或 Regime 从 FADE_CHOP 转成 TREND_DAY。",
        ]
        if sm and sm.max_loss > 0:
            buy_strike = max(l.strike for l in rec.legs) if rec.legs else 0
            lines.append(f"最坏情况: 最大亏损 {sm.max_loss:,.3f} / 合约 (到期时标的 > {_format_strike(buy_strike)})")
        lines.append("触发后怎么做: 直接把整组 Bear Call Spread 一次性平仓，不要只平卖出腿。")
        lines.append("新手执行: 先用最小张数，若盘口价差明显变宽，优先撤单等待，不硬做。")
        return lines

    if rec.action == "bull_put_spread":
        stop_ref = f"盈亏平衡 {sm.breakeven:,.2f}" if sm and sm.breakeven > 0 else f"VAL {vp.val:,.2f}"
        lines = [
            f"止损触发: 标的跌破{stop_ref}，或 Regime 从 FADE_CHOP 转成 TREND_DAY。",
        ]
        if sm and sm.max_loss > 0:
            buy_strike = min(l.strike for l in rec.legs) if rec.legs else 0
            lines.append(f"最坏情况: 最大亏损 {sm.max_loss:,.3f} / 合约 (到期时标的 < {_format_strike(buy_strike)})")
        lines.append("触发后怎么做: 直接把整组 Bull Put Spread 一次性平仓，不要只留卖出腿。")
        lines.append("新手执行: 先用最小张数，若盘口价差明显变宽，优先撤单等待，不硬做。")
        return lines

    # ── Single leg: call / put ──
    # Premium stop-loss price (40% loss) for low-DTE legs
    premium_stop = None
    if low_dte and rec.legs and rec.legs[0].last_price and rec.legs[0].last_price > 0:
        premium_stop = rec.legs[0].last_price * 0.60  # 40% loss → 60% of entry

    if rec.action == "call":
        if low_dte:
            if regime.regime == USRegimeType.FADE_CHOP:
                # P1-2: FADE_CHOP bullish — skip VAL (entry premise), use deeper support
                all_below = _nearest_levels(regime.price, "below", vp, kl, gamma_wall, n=3)
                deeper = [(n, v) for n, v in all_below if n not in ("VAL",)]
                if deeper:
                    stop_name, stop_val = deeper[0]
                    stop_line = f"止损触发: 标的跌破 {stop_name} {stop_val:,.2f} (VAL {vp.val:,.2f} 附近允许短暂 wick)"
                else:
                    buffer_val = vp.val * 0.997  # VAL 下方 0.3%
                    stop_line = f"止损触发: 标的跌破 {buffer_val:,.2f} (VAL {vp.val:,.2f} - 0.3% 缓冲)"
            else:
                nearby = _nearest_levels(regime.price, "below", vp, kl, gamma_wall, n=1)
                if nearby:
                    stop_name, stop_val = nearby[0]
                    stop_line = f"止损触发: 标的跌破 {stop_name} {stop_val:,.2f} (最近支撑位)"
                else:
                    stop_line = "止损触发: 标的跌破 VWAP 或原本突破结构被破坏。"
        else:
            stop_line = "止损触发: 标的跌破 VWAP 或原本突破结构被破坏。"
        lines = [
            stop_line,
            "触发后怎么做: 直接卖出平仓，不补仓摊平，不把短线单拖成长线。",
            "新手执行: 优先做 ATM 或轻度实值，不追过度虚值合约。",
        ]
        if low_dte and premium_stop is not None:
            lines.append(f"⚠️ 低 DTE ({dte}天): 期权跌至 ${premium_stop:.2f} 即平仓 (亏损 40%，不等标的到止损位)")
        return lines

    if rec.action == "put":
        if low_dte:
            if regime.regime == USRegimeType.FADE_CHOP:
                # P1-2: FADE_CHOP bearish — skip VAH (entry premise), use deeper resistance
                all_above = _nearest_levels(regime.price, "above", vp, kl, gamma_wall, n=3)
                deeper = [(n, v) for n, v in all_above if n not in ("VAH",)]
                if deeper:
                    stop_name, stop_val = deeper[0]
                    stop_line = f"止损触发: 标的突破 {stop_name} {stop_val:,.2f} (VAH {vp.vah:,.2f} 附近允许短暂 wick)"
                else:
                    buffer_val = vp.vah * 1.003  # VAH 上方 0.3%
                    stop_line = f"止损触发: 标的突破 {buffer_val:,.2f} (VAH {vp.vah:,.2f} + 0.3% 缓冲)"
            else:
                nearby = _nearest_levels(regime.price, "above", vp, kl, gamma_wall, n=1)
                if nearby:
                    stop_name, stop_val = nearby[0]
                    stop_line = f"止损触发: 标的突破 {stop_name} {stop_val:,.2f} (最近阻力位)"
                else:
                    stop_line = "止损触发: 标的重新站回 VWAP 上方或原本下跌结构被破坏。"
        else:
            stop_line = "止损触发: 标的重新站回 VWAP 上方或原本下跌结构被破坏。"
        lines = [
            stop_line,
            "触发后怎么做: 直接卖出平仓，不补仓摊平，不把短线单拖成长线。",
            "新手执行: 优先做 ATM 或轻度实值，不追过度虚值合约。",
        ]
        if low_dte and premium_stop is not None:
            lines.append(f"⚠️ 低 DTE ({dte}天): 期权跌至 ${premium_stop:.2f} 即平仓 (亏损 40%，不等标的到止损位)")
        return lines

    return ["操作建议: 出现失效信号时，先平仓，再等新的结构。"]


# ── US Regime analysis functions ──


def _regime_conclusion(
    regime: USRegimeResult,
    vp: VolumeProfileResult,
    kl: KeyLevels,
    vwap: float,
) -> str:
    """Generate a narrative conclusion for the current US regime."""
    if regime.regime == USRegimeType.GAP_AND_GO:
        if regime.price > vp.vah:
            return "缺口向上 + 盘前突破价值区上沿，按向上跳空追击处理。"
        if regime.price < vp.val:
            return "缺口向下 + 跌出价值区下沿，按向下跳空追击处理。"
        return "缺口明显但价格仍在价值区内，先观察能否有效突破 VA 边界。"

    if regime.regime == USRegimeType.TREND_DAY:
        if regime.price > vp.vah:
            return "价格已脱离价值区上沿，量能配合趋势方向，按向上趋势日处理。"
        if regime.price < vp.val:
            return "价格已跌出价值区下沿，量能配合趋势方向，按向下趋势日处理。"
        return "趋势方向初现但价格仍在价值区内，跟踪 RVOL 是否持续抬升。"

    if regime.regime == USRegimeType.FADE_CHOP:
        edge, _ = _closest_value_area_edge(regime.price, vp)
        if edge == "VAH":
            return "当前更偏向区间内震荡，优先按上沿回落思路看待，不按单边突破处理。"
        return "当前更偏向区间内震荡，优先按下沿反弹思路看待，不按单边突破处理。"

    if vwap > 0:
        return "多空信号混杂，先观察价格相对 VWAP 与价值区边界的反应。"
    return "多空信号混杂，当前没有足够把握给出明确方向。"


def _regime_reason_lines(
    regime: USRegimeResult,
    vp: VolumeProfileResult,
    kl: KeyLevels,
    vwap: float,
    gamma_wall: GammaWallResult | None,
    option_market: OptionMarketSnapshot | None,
    quote: QuoteSnapshot | None,
    option_rec: OptionRecommendation | None = None,
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Return (reasons, supports, uncertainties, invalidations) for regime analysis."""
    reasons: list[str] = []
    supports: list[str] = []
    uncertainties: list[str] = []
    invalidations: list[str] = []

    if regime.regime == USRegimeType.GAP_AND_GO:
        reasons.append(f"Gap {regime.gap_pct:+.2f}%，开盘跳空幅度已达到追击阈值。")
        reasons.append(f"RVOL {regime.rvol:.2f} 配合跳空方向，量能支撑。")
        if regime.price > vp.vah:
            reasons.append(f"价格 {regime.price:,.2f} 高于 VAH {vp.vah:,.2f}，已经脱离价值区上沿。")
        elif regime.price < vp.val:
            reasons.append(f"价格 {regime.price:,.2f} 低于 VAL {vp.val:,.2f}，已经脱离价值区下沿。")
        if kl.pmh > 0 and kl.pml > 0:
            if regime.price > kl.pmh:
                supports.append(f"价格已突破盘前高点 PMH {kl.pmh:,.2f}，跳空结构进一步确认。")
            elif regime.price < kl.pml:
                supports.append(f"价格已跌破盘前低点 PML {kl.pml:,.2f}，跳空结构进一步确认。")
        if regime.adaptive_thresholds:
            at = regime.adaptive_thresholds
            supports.append(
                f"自适应阈值: P{at.get('sample', '?')}d GAP_AND_GO={at.get('gap_and_go', 0):.2f}"
                f" (rank {at.get('pctl_rank', 0):.0f}%)"
            )
        if regime.spy_regime in (USRegimeType.GAP_AND_GO, USRegimeType.TREND_DAY):
            supports.append("SPY 同步处于动量/趋势状态，大盘环境配合。")
        invalidations.append(f"若价格重新回到 VWAP {vwap:,.2f} 下方 (多头) 或上方 (空头)，跳空动量可能衰竭。")
        invalidations.append("若 RVOL 明显回落同时价格回到价值区内，需重新定调。")

    elif regime.regime == USRegimeType.TREND_DAY:
        reasons.append(f"RVOL {regime.rvol:.2f}，量能达到趋势日级别。")
        if regime.price > vp.vah:
            reasons.append(f"价格 {regime.price:,.2f} 高于 VAH {vp.vah:,.2f}，已经脱离价值区上沿。")
        elif regime.price < vp.val:
            reasons.append(f"价格 {regime.price:,.2f} 低于 VAL {vp.val:,.2f}，已经脱离价值区下沿。")
        else:
            reasons.append(
                f"价格 {regime.price:,.2f} 仍在价值区 {vp.val:,.2f} - {vp.vah:,.2f} 内，"
                "但量能和结构偏趋势。"
            )
        if vwap > 0:
            vwap_relation = "高于" if regime.price > vwap else "低于"
            direction_text = "多头" if regime.price > vwap else "空头"
            reasons.append(f"当前价{vwap_relation} VWAP {vwap:,.2f}，盘中 {direction_text} 结构仍在。")
        if kl.pdh > 0 and regime.price > kl.pdh:
            supports.append(f"已突破前日高点 PDH {kl.pdh:,.2f}，趋势延伸信号。")
        elif kl.pdl > 0 and regime.price < kl.pdl:
            supports.append(f"已跌破前日低点 PDL {kl.pdl:,.2f}，趋势延伸信号。")
        if regime.spy_regime in (USRegimeType.GAP_AND_GO, USRegimeType.TREND_DAY):
            supports.append("SPY 同步处于动量/趋势状态，大盘环境配合。")
        invalidations.append(f"若价格重新回到价值区内 ({vp.val:,.2f} - {vp.vah:,.2f})，趋势判断失效。")
        invalidations.append("若后续量能明显回落，需要重新评估。")

    elif regime.regime == USRegimeType.FADE_CHOP:
        reasons.append(f"RVOL {regime.rvol:.2f}，当前量能更接近震荡而不是趋势展开。")
        reasons.append(
            f"价格 {regime.price:,.2f} 仍位于 Value Area {vp.val:,.2f} - {vp.vah:,.2f} 内部。"
        )
        if vwap > 0:
            if regime.price >= vwap:
                reasons.append(f"当前价略高于 VWAP {vwap:,.2f}，但还没有形成有效趋势延伸。")
            else:
                reasons.append(f"当前价低于 VWAP {vwap:,.2f}，短线偏弱但还不是单边下杀。")

        edge, edge_distance = _closest_value_area_edge(regime.price, vp)
        if edge:
            supports.append(
                f"价格距离 {edge} 更近（约 {edge_distance:,.2f} 点），边界位置更适合观察回归反应。"
            )

        if quote and vp.vah > vp.val:
            value_area_width = vp.vah - vp.val
            intraday_range = max(0.0, quote.high_price - quote.low_price)
            if value_area_width > 0 and intraday_range > 0:
                range_ratio = intraday_range / value_area_width
                if range_ratio > 0.3:
                    uncertainties.append(
                        f"日内振幅已占 Value Area 的 {range_ratio:.0%}，说明区间结构正在被来回消耗。"
                    )

        invalidations.append(f"若价格带量突破 VAH {vp.vah:,.2f} 或跌破 VAL {vp.val:,.2f}，区间判断失效。")
        invalidations.append("若 RVOL 快速抬升并持续放大，需要重新评估是否切换到 TREND_DAY。")

    else:  # UNCLEAR (P0-3: sub-type aware guidance)
        lean = getattr(regime, "lean", "neutral")
        if regime.rvol >= 1.2 and vp.val <= regime.price <= vp.vah:
            # Sub-type 2: high volume in VA — buildup
            reasons.append("量能已达到趋势日水平，但价格仍在价值区内。")
            lean_text = "偏多" if lean == "bullish" else "偏空" if lean == "bearish" else "方向待定"
            reasons.append(f"可能是突破前蓄力，当前倾向: {lean_text}。")
            supports.append("若后续带量突破 VA 边界，可转为 TREND_DAY 处理。")
        elif (regime.price > vp.vah or regime.price < vp.val) and regime.rvol < 1.0:
            # Sub-type 3: outside VA but low volume — likely false breakout
            reasons.append("价格虽已脱离价值区，但量能不足，可能是假突破。")
            lean_text = "偏空回归" if lean == "bearish" else "偏多回归" if lean == "bullish" else "等待确认"
            reasons.append(f"倾向: {lean_text}，等待量价配合确认。")
            uncertainties.append("低量突破后回归价值区概率较高，不宜追。")
        else:
            # Sub-type 1 or generic
            reasons.append("当前价格、量能和位置关系没有形成一致信号。")
        if vwap > 0:
            reasons.append(f"先观察价格相对 VWAP {vwap:,.2f} 的站稳或跌破情况。")
        if vp.vah > 0 and vp.val > 0:
            reasons.append(f"同时观察是否会有效突破 VAH {vp.vah:,.2f} 或跌破 VAL {vp.val:,.2f}。")
        invalidations.append("若价格脱离价值区并伴随量能扩张，可重新生成更明确的剧本。")

    # Gamma wall proximity
    if gamma_wall:
        if gamma_wall.call_wall_strike > 0 and regime.price > 0:
            call_distance_pct = abs(gamma_wall.call_wall_strike - regime.price) / regime.price * 100
            if call_distance_pct <= 1.5:
                supports.append(
                    f"Call Wall {gamma_wall.call_wall_strike:,.0f} 距离当前价仅 {call_distance_pct:.1f}%，上方压力更值得关注。"
                )
        if gamma_wall.put_wall_strike > 0 and regime.price > 0:
            put_distance_pct = abs(regime.price - gamma_wall.put_wall_strike) / regime.price * 100
            if put_distance_pct <= 1.5:
                supports.append(
                    f"Put Wall {gamma_wall.put_wall_strike:,.0f} 距离当前价仅 {put_distance_pct:.1f}%，下方承接更值得关注。"
                )

    # IV interpretation
    if option_market and option_market.atm_iv > 0 and option_market.avg_iv > 0:
        is_seller = option_rec is not None and option_rec.action in {
            "bear_call_spread", "bull_put_spread",
        }
        if option_market.iv_ratio >= 1.2:
            supports.append(
                f"ATM IV / 中位 IV = {option_market.iv_ratio:.2f}x，隐波偏高，适合卖方策略(价差)。"
            )
        elif option_market.iv_ratio <= 0.85:
            if is_seller:
                uncertainties.append(
                    f"ATM IV / 中位 IV = {option_market.iv_ratio:.2f}x，隐波偏低，卖方 premium 收入偏少，风险回报可能不理想。"
                )
            else:
                supports.append(
                    f"ATM IV / 中位 IV = {option_market.iv_ratio:.2f}x，隐波偏低，期权定价相对便宜。"
                )
        elif option_market.iv_ratio <= 0.9:
            supports.append(
                f"ATM IV / 中位 IV = {option_market.iv_ratio:.2f}x，隐波没有明显异常抬升。"
            )

    return reasons, supports, uncertainties, invalidations


# ── Action Plan generation engine ──


def _cap_tp1(
    plan: ActionPlan, ctx: PlanContext,
    vp: VolumeProfileResult, kl: KeyLevels,
    gamma_wall: GammaWallResult | None,
    current_price: float = 0.0,
) -> ActionPlan:
    """Cap TP1 to reachable range (wrapper)."""
    levels = _us_key_levels_to_dict(vp, kl, gamma_wall, current_price=current_price)
    return _cap_tp1_common(plan, ctx, levels)


def _cap_tp2(
    plan: ActionPlan, ctx: PlanContext,
    vp: VolumeProfileResult, kl: KeyLevels,
    gamma_wall: GammaWallResult | None,
    current_price: float = 0.0,
) -> ActionPlan:
    """Cap TP2 to reachable range (wrapper)."""
    levels = _us_key_levels_to_dict(vp, kl, gamma_wall, current_price=current_price)
    return _cap_tp2_common(plan, ctx, levels)


def _generate_action_plans(
    regime: USRegimeResult,
    direction: str,
    vp: VolumeProfileResult,
    kl: KeyLevels,
    gamma_wall: GammaWallResult | None,
    option_rec: OptionRecommendation | None,
    ctx: PlanContext | None = None,
    trend_downgrade_confidence: float = 0.0,
) -> list[ActionPlan]:
    """Generate A/B/C action plans based on regime and direction."""
    price = regime.price
    option_line = _compact_option_line(option_rec) if option_rec else None

    # Issue 4: wait + low confidence trend → downgrade to UNCLEAR plans
    if (
        trend_downgrade_confidence > 0
        and option_rec is not None
        and option_rec.action == "wait"
        and regime.confidence < trend_downgrade_confidence
        and regime.regime in (USRegimeType.GAP_AND_GO, USRegimeType.TREND_DAY)
    ):
        plans = _plans_unclear(price, vp, kl, gamma_wall, regime, option_line)
        if ctx:
            plans = [_cap_tp1(p, ctx, vp, kl, gamma_wall, current_price=price) for p in plans]
            plans = [_cap_tp2(p, ctx, vp, kl, gamma_wall, current_price=price) for p in plans]
            plans = [_check_entry_reachability(p, price, ctx) for p in plans]
            plans = _apply_vwap_deviation_warning_common(plans, price, kl.vwap)
            plans = _apply_gamma_wall_warning_common(plans, price, gamma_wall, ctx)
            plans = _apply_wait_coherence(plans, ctx)
            plans = _apply_min_rr_gate(plans, ctx)
            plans = _apply_market_direction_warning(plans, ctx)
        return plans

    if regime.regime in (USRegimeType.GAP_AND_GO, USRegimeType.TREND_DAY):
        if direction == "bullish":
            plans = _plans_trend_bullish(price, vp, kl, gamma_wall, option_line)
        else:
            plans = _plans_trend_bearish(price, vp, kl, gamma_wall, option_line)
    elif regime.regime == USRegimeType.FADE_CHOP:
        edge, _ = _closest_value_area_edge(price, vp)
        # P1: direction consistency check — if direction conflicts with VA edge,
        # fall through to UNCLEAR plans instead of generating contradictory fade plans.
        # e.g., edge=VAL + direction=bearish means "price near support but trending down"
        #       → fade long would fight the trend, so go to UNCLEAR instead.
        direction_conflicts = (
            (edge == "VAL" and direction == "bearish")
            or (edge == "VAH" and direction == "bullish")
        )
        if direction_conflicts:
            plans = _plans_unclear(price, vp, kl, gamma_wall, regime, option_line)
        elif edge == "VAH":
            plans = _plans_fade_bearish(price, vp, kl, gamma_wall, option_line)
        else:
            plans = _plans_fade_bullish(price, vp, kl, gamma_wall, option_line)
    else:
        # UNCLEAR
        plans = _plans_unclear(price, vp, kl, gamma_wall, regime, option_line)

    # 后处理（仅当有 ctx 时执行）
    if ctx:
        plans = [_cap_tp1(p, ctx, vp, kl, gamma_wall, current_price=price) for p in plans]
        plans = [_cap_tp2(p, ctx, vp, kl, gamma_wall, current_price=price) for p in plans]
        plans = [_check_entry_reachability(p, price, ctx) for p in plans]
        plans = _apply_vwap_deviation_warning_common(plans, price, kl.vwap)
        plans = _apply_gamma_wall_warning_common(plans, price, gamma_wall, ctx)
        plans = _apply_wait_coherence(plans, ctx)
        plans = _apply_min_rr_gate(plans, ctx)
        plans = _apply_market_direction_warning(plans, ctx)
    return plans


def _plans_trend_bullish(
    price: float, vp: VolumeProfileResult, kl: KeyLevels,
    gamma_wall: GammaWallResult | None, option_line: str | None,
) -> list[ActionPlan]:
    # Anchor TP search from VWAP (entry), fallback to price if no results
    above = _nearest_levels(kl.vwap, "above", vp, kl, gamma_wall, n=3) if kl.vwap > 0 else []
    if not above:
        above = _nearest_levels(price, "above", vp, kl, gamma_wall, n=3)
    below_vwap = _nearest_levels(kl.vwap, "below", vp, kl, gamma_wall, n=1)
    sl_a = below_vwap[0] if below_vwap else None
    tp1_a = above[0] if above else None
    tp2_a = above[1] if len(above) >= 2 else None

    plan_a = ActionPlan(
        label="A", name="趋势回调做多", emoji="📈", is_primary=True,
        logic="回调至 VWAP 附近接多, 顺势而为",
        direction="bullish",
        trigger=f"价格回调至 VWAP {kl.vwap:,.2f} 附近企稳",
        entry=kl.vwap, entry_action="做多",
        stop_loss=sl_a[1] if sl_a else None,
        stop_loss_reason=sl_a[0] if sl_a else "VWAP 下方",
        tp1=tp1_a[1] if tp1_a else None,
        tp1_label=tp1_a[0] if tp1_a else "",
        tp2=tp2_a[1] if tp2_a else None,
        tp2_label=tp2_a[0] if tp2_a else "",
        rr_ratio=_calculate_rr(kl.vwap, sl_a[1] if sl_a else None, tp1_a[1] if tp1_a else None),
        option_line=option_line,
    )

    # Plan B: breakout add-on
    entry_b = tp1_a[1] if tp1_a else None
    above_b = _nearest_levels(entry_b, "above", vp, kl, gamma_wall, n=2) if entry_b else []
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
        logic="价格跌回 VA 内 + RVOL 回落 → 转 FADE_CHOP",
        direction="bearish",
        trigger=f"跌破 VAH {vp.vah:,.2f} + RVOL 回落",
        entry=None, entry_action="",
        stop_loss=None, stop_loss_reason="",
        tp1=None, tp1_label="", tp2=None, tp2_label="", rr_ratio=0.0,
    )
    return [plan_a, plan_b, plan_c]


def _plans_trend_bearish(
    price: float, vp: VolumeProfileResult, kl: KeyLevels,
    gamma_wall: GammaWallResult | None, option_line: str | None,
) -> list[ActionPlan]:
    # Anchor TP search from VWAP (entry), fallback to price if no results
    below = _nearest_levels(kl.vwap, "below", vp, kl, gamma_wall, n=3) if kl.vwap > 0 else []
    if not below:
        below = _nearest_levels(price, "below", vp, kl, gamma_wall, n=3)
    above_vwap = _nearest_levels(kl.vwap, "above", vp, kl, gamma_wall, n=1)
    sl_a = above_vwap[0] if above_vwap else None
    tp1_a = below[0] if below else None
    tp2_a = below[1] if len(below) >= 2 else None

    plan_a = ActionPlan(
        label="A", name="趋势反弹做空", emoji="📉", is_primary=True,
        logic="反弹至 VWAP 附近接空, 顺势而为",
        direction="bearish",
        trigger=f"价格反弹至 VWAP {kl.vwap:,.2f} 附近受阻",
        entry=kl.vwap, entry_action="做空",
        stop_loss=sl_a[1] if sl_a else None,
        stop_loss_reason=sl_a[0] if sl_a else "VWAP 上方",
        tp1=tp1_a[1] if tp1_a else None,
        tp1_label=tp1_a[0] if tp1_a else "",
        tp2=tp2_a[1] if tp2_a else None,
        tp2_label=tp2_a[0] if tp2_a else "",
        rr_ratio=_calculate_rr(kl.vwap, sl_a[1] if sl_a else None, tp1_a[1] if tp1_a else None),
        option_line=option_line,
    )

    entry_b = tp1_a[1] if tp1_a else None
    below_b = _nearest_levels(entry_b, "below", vp, kl, gamma_wall, n=2) if entry_b else []
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
        logic="价格涨回 VA 内 + RVOL 回落 → 转 FADE_CHOP",
        direction="bullish",
        trigger=f"站回 VAL {vp.val:,.2f} + RVOL 回落",
        entry=None, entry_action="",
        stop_loss=None, stop_loss_reason="",
        tp1=None, tp1_label="", tp2=None, tp2_label="", rr_ratio=0.0,
    )
    return [plan_a, plan_b, plan_c]


_FADE_MAX_SL_DISTANCE_PCT = 0.02  # 2% — max SL distance for fade plans


def _cap_fade_sl(entry: float, sl: float | None, sl_reason: str, direction: str) -> tuple[float | None, str]:
    """Cap SL distance for fade plans to _FADE_MAX_SL_DISTANCE_PCT."""
    if sl is None or entry <= 0:
        return sl, sl_reason
    dist = abs(sl - entry) / entry
    if dist > _FADE_MAX_SL_DISTANCE_PCT:
        if direction == "bearish":
            capped = round(entry * (1 + _FADE_MAX_SL_DISTANCE_PCT), 2)
        else:
            capped = round(entry * (1 - _FADE_MAX_SL_DISTANCE_PCT), 2)
        return capped, "固定止损"
    return sl, sl_reason


def _plans_fade_bearish(
    price: float, vp: VolumeProfileResult, kl: KeyLevels,
    gamma_wall: GammaWallResult | None, option_line: str | None,
) -> list[ActionPlan]:
    """FADE_CHOP near VAH → short bias."""
    above = _nearest_levels(vp.vah, "above", vp, kl, gamma_wall, n=1)
    sl_a = above[0] if above else None

    zone = _find_fade_entry_zone(vp.vah, vp.val, kl, gamma_wall, current_price=price)

    # P0-1b: Cap SL distance for fade plans
    raw_sl = sl_a[1] if sl_a else None
    raw_sl_reason = sl_a[0] if sl_a else "VAH 上方"
    capped_sl, capped_sl_reason = _cap_fade_sl(vp.vah, raw_sl, raw_sl_reason, "bearish")

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
        stop_loss=capped_sl,
        stop_loss_reason=capped_sl_reason,
        tp1=vp.poc, tp1_label="POC",
        tp2=vp.val, tp2_label="VAL",
        rr_ratio=_calculate_rr(vp.vah, capped_sl, vp.poc),
        option_line=option_line,
    )

    # Plan B: VWAP regression — dynamic SL: nearest structure above entry
    plan_b_entry = kl.vwap if kl.vwap > vp.poc else None
    if plan_b_entry:
        sl_b_candidates = _nearest_levels(plan_b_entry, "above", vp, kl, gamma_wall, n=1)
        sl_b_price = sl_b_candidates[0][1] if sl_b_candidates else vp.vah
        sl_b_label = sl_b_candidates[0][0] if sl_b_candidates else "VAH"
    else:
        sl_b_price = vp.vah
        sl_b_label = "VAH"
    plan_b = ActionPlan(
        label="B", name="VWAP 回归做空", emoji="📉", is_primary=False,
        logic="VWAP 上方接空, 目标 POC",
        direction="bearish",
        trigger=f"价格反弹至 VWAP {kl.vwap:,.2f}" if plan_b_entry else "价格反弹至 VAH 附近",
        entry=plan_b_entry, entry_action="做空",
        stop_loss=sl_b_price, stop_loss_reason=sl_b_label,
        tp1=vp.poc, tp1_label="POC",
        tp2=vp.val, tp2_label="VAL",
        rr_ratio=_calculate_rr(plan_b_entry, sl_b_price, vp.poc),
    )

    plan_c = ActionPlan(
        label="C", name="失效反转", emoji="⚡", is_primary=False,
        logic="放量突破 VAH → 转 TREND_DAY",
        direction="bullish",
        trigger=f"放量站稳 VAH {vp.vah:,.2f} 上方",
        entry=None, entry_action="",
        stop_loss=None, stop_loss_reason="",
        tp1=None, tp1_label="", tp2=None, tp2_label="", rr_ratio=0.0,
    )
    return [plan_a, plan_b, plan_c]


def _plans_fade_bullish(
    price: float, vp: VolumeProfileResult, kl: KeyLevels,
    gamma_wall: GammaWallResult | None, option_line: str | None,
) -> list[ActionPlan]:
    """FADE_CHOP near VAL → long bias."""
    below = _nearest_levels(vp.val, "below", vp, kl, gamma_wall, n=1)
    sl_a = below[0] if below else None

    zone = _find_fade_entry_zone(vp.val, vp.vah, kl, gamma_wall, current_price=price)

    # P0-1b: Cap SL distance for fade plans
    raw_sl = sl_a[1] if sl_a else None
    raw_sl_reason = sl_a[0] if sl_a else "VAL 下方"
    capped_sl, capped_sl_reason = _cap_fade_sl(vp.val, raw_sl, raw_sl_reason, "bullish")

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
        stop_loss=capped_sl,
        stop_loss_reason=capped_sl_reason,
        tp1=vp.poc, tp1_label="POC",
        tp2=vp.vah, tp2_label="VAH",
        rr_ratio=_calculate_rr(vp.val, capped_sl, vp.poc),
        option_line=option_line,
    )

    # Plan B: VWAP regression — dynamic SL: nearest structure below entry
    plan_b_entry = kl.vwap if kl.vwap < vp.poc else None
    if plan_b_entry:
        sl_b_candidates = _nearest_levels(plan_b_entry, "below", vp, kl, gamma_wall, n=1)
        sl_b_price = sl_b_candidates[0][1] if sl_b_candidates else vp.val
        sl_b_label = sl_b_candidates[0][0] if sl_b_candidates else "VAL"
    else:
        sl_b_price = vp.val
        sl_b_label = "VAL"
    plan_b = ActionPlan(
        label="B", name="VWAP 回归做多", emoji="📈", is_primary=False,
        logic="VWAP 下方接多, 目标 POC",
        direction="bullish",
        trigger=f"价格回调至 VWAP {kl.vwap:,.2f}" if plan_b_entry else "价格回调至 VAL 附近",
        entry=plan_b_entry, entry_action="做多",
        stop_loss=sl_b_price, stop_loss_reason=sl_b_label,
        tp1=vp.poc, tp1_label="POC",
        tp2=vp.vah, tp2_label="VAH",
        rr_ratio=_calculate_rr(plan_b_entry, sl_b_price, vp.poc),
    )

    plan_c = ActionPlan(
        label="C", name="失效反转", emoji="⚡", is_primary=False,
        logic="放量跌破 VAL → 转 TREND_DAY",
        direction="bearish",
        trigger=f"放量跌破 VAL {vp.val:,.2f}",
        entry=None, entry_action="",
        stop_loss=None, stop_loss_reason="",
        tp1=None, tp1_label="", tp2=None, tp2_label="", rr_ratio=0.0,
    )
    return [plan_a, plan_b, plan_c]


def _make_unclear_fade_plan(
    lean: str, vp: VolumeProfileResult, kl: KeyLevels,
    gamma_wall: GammaWallResult | None, option_line: str | None,
) -> ActionPlan | None:
    """Build a mean-reversion fade plan for UNCLEAR + low RVOL + directional lean.

    Uses single-point entry at VWAP (no entry_zone) with nearest VA edge as TP1
    and nearest structural level as SL.  This avoids the wide entry_zone problem
    that occurs when VWAP is far from structure.

    Returns None when VWAP is too close to the target VA edge (< 0.15%),
    making the fade unprofitable after spread costs.
    """
    _MIN_FADE_REWARD_PCT = 0.0015  # 0.15% — min distance between entry and TP1
    _MAX_SL_DISTANCE_PCT = 0.01   # 1.0% — cap SL distance for low-vol fade
    _MIN_FADE_RR = 0.8            # min R:R for fade to be actionable

    if lean == "bearish":
        # Price above VA → fade short back to VA edge
        tp1_price = vp.vah if vp.vah > 0 else vp.poc
        tp1_label = "VAH" if vp.vah > 0 else "POC"
        # Direction guard: bearish fade TP must be below entry
        if kl.vwap > 0 and tp1_price >= kl.vwap:
            return None
        # Guard: VWAP too close to target → fade unprofitable
        if kl.vwap > 0 and abs(tp1_price - kl.vwap) / kl.vwap < _MIN_FADE_REWARD_PCT:
            return None
        sl_candidates = _nearest_levels(kl.vwap, "above", vp, kl, gamma_wall, n=1)
        sl_price = sl_candidates[0][1] if sl_candidates else vp.vah
        sl_label = sl_candidates[0][0] if sl_candidates else "VAH"
        # Cap SL distance — low-vol fade shouldn't have wide stops
        if kl.vwap > 0 and abs(sl_price - kl.vwap) / kl.vwap > _MAX_SL_DISTANCE_PCT:
            sl_price = round(kl.vwap * (1 + _MAX_SL_DISTANCE_PCT), 2)
            sl_label = "固定止损"
        # Pre-check: R:R too low → don't show misleading plan
        if _calculate_rr(kl.vwap, sl_price, tp1_price) < _MIN_FADE_RR:
            return None
        return ActionPlan(
            label="B", name="均值回归做空", emoji="📉", is_primary=False,
            logic="低 RVOL + UNCLEAR, 博均值回归做空",
            direction="bearish",
            trigger=f"价格反弹至 VWAP({kl.vwap:,.2f}) 附近企稳",
            entry=kl.vwap, entry_action="做空",
            stop_loss=sl_price, stop_loss_reason=sl_label,
            tp1=tp1_price, tp1_label=tp1_label,
            tp2=vp.poc if tp1_label != "POC" else None,
            tp2_label="POC" if tp1_label != "POC" else "",
            rr_ratio=_calculate_rr(kl.vwap, sl_price, tp1_price),
            option_line=option_line,
        )
    else:
        # lean == "bullish", price below VA → fade long back to VA edge
        tp1_price = vp.val if vp.val > 0 else vp.poc
        tp1_label = "VAL" if vp.val > 0 else "POC"
        # Direction guard: bullish fade TP must be above entry
        if kl.vwap > 0 and tp1_price <= kl.vwap:
            return None
        # Guard: VWAP too close to target → fade unprofitable
        if kl.vwap > 0 and abs(tp1_price - kl.vwap) / kl.vwap < _MIN_FADE_REWARD_PCT:
            return None
        sl_candidates = _nearest_levels(kl.vwap, "below", vp, kl, gamma_wall, n=1)
        sl_price = sl_candidates[0][1] if sl_candidates else vp.val
        sl_label = sl_candidates[0][0] if sl_candidates else "VAL"
        # Cap SL distance — low-vol fade shouldn't have wide stops
        if kl.vwap > 0 and abs(sl_price - kl.vwap) / kl.vwap > _MAX_SL_DISTANCE_PCT:
            sl_price = round(kl.vwap * (1 - _MAX_SL_DISTANCE_PCT), 2)
            sl_label = "固定止损"
        # Pre-check: R:R too low → don't show misleading plan
        if _calculate_rr(kl.vwap, sl_price, tp1_price) < _MIN_FADE_RR:
            return None
        return ActionPlan(
            label="B", name="均值回归做多", emoji="📈", is_primary=False,
            logic="低 RVOL + UNCLEAR, 博均值回归做多",
            direction="bullish",
            trigger=f"价格回调至 VWAP({kl.vwap:,.2f}) 附近企稳",
            entry=kl.vwap, entry_action="做多",
            stop_loss=sl_price, stop_loss_reason=sl_label,
            tp1=tp1_price, tp1_label=tp1_label,
            tp2=vp.poc if tp1_label != "POC" else None,
            tp2_label="POC" if tp1_label != "POC" else "",
            rr_ratio=_calculate_rr(kl.vwap, sl_price, tp1_price),
            option_line=option_line,
        )


def _make_unclear_directional_plan(
    lean: str, vp: VolumeProfileResult, kl: KeyLevels,
    option_line: str | None,
) -> ActionPlan:
    """Build the original light-position directional plan for UNCLEAR with lean.

    Used when is_chop_likely is False (RVOL >= 1.0 or confidence > 0.30).
    """
    dir_cn = "做多" if lean == "bullish" else "做空"
    return ActionPlan(
        label="B", name=f"轻仓{dir_cn}", emoji="📈" if lean == "bullish" else "📉",
        is_primary=False,
        logic=f"有 {lean} 倾向, 轻仓试探",
        direction=lean,
        trigger=f"价格确认 VWAP {'上方' if lean == 'bullish' else '下方'}",
        entry=kl.vwap, entry_action=dir_cn,
        stop_loss=None, stop_loss_reason="严格止损",
        tp1=vp.vah if lean == "bullish" else vp.val,
        tp1_label="VAH" if lean == "bullish" else "VAL",
        tp2=None, tp2_label="", rr_ratio=0.0,
        option_line=option_line,
    )


def _plans_unclear(
    price: float, vp: VolumeProfileResult, kl: KeyLevels,
    gamma_wall: GammaWallResult | None, regime: USRegimeResult,
    option_line: str | None,
) -> list[ActionPlan]:
    lean = getattr(regime, "lean", "neutral")
    # is_chop_likely: low RVOL + low confidence → mean-reversion fade opportunity
    # Note: when lean="neutral", fade plan is NOT generated (no directional edge)
    is_chop_likely = regime.rvol < 1.0 and regime.confidence <= 0.30

    plan_a = ActionPlan(
        label="A", name="等待确认", emoji="⏳", is_primary=True,
        logic="多空信号混杂, 等待方向明确后入场",
        direction="neutral",
        trigger="Regime 转为 TREND_DAY 或 FADE_CHOP",
        entry=None, entry_action="",
        stop_loss=None, stop_loss_reason="",
        tp1=None, tp1_label="", tp2=None, tp2_label="", rr_ratio=0.0,
    )

    if lean in ("bullish", "bearish"):
        if is_chop_likely:
            plan_b = _make_unclear_fade_plan(lean, vp, kl, gamma_wall, option_line)
            if plan_b is None:
                plan_b = _make_unclear_directional_plan(lean, vp, kl, option_line)
        else:
            plan_b = _make_unclear_directional_plan(lean, vp, kl, option_line)
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


# ── Formatting helpers for new layout ──


def _alternate_regime_info(regime: USRegimeResult) -> tuple[str, int]:
    """Infer alternate regime name and probability (no regime module changes)."""
    conf_pct = int(regime.confidence * 100)
    alt_map = {
        USRegimeType.GAP_AND_GO: "趋势日",
        USRegimeType.TREND_DAY: "震荡日",
        USRegimeType.FADE_CHOP: "趋势日",
        USRegimeType.UNCLEAR: "震荡日",
    }
    return alt_map.get(regime.regime, "震荡日"), 100 - conf_pct


def _core_conclusion_text(
    regime: USRegimeResult,
    direction: str,
    vp: VolumeProfileResult,
    kl: KeyLevels,
    option_rec: OptionRecommendation | None,
) -> str:
    """One-line action directive."""
    if option_rec and option_rec.action == "wait":
        # P1-1: data-wait (no chain/expiry) should not mask regime conclusion
        if getattr(option_rec, "wait_category", "market") == "data":
            # Show regime-based conclusion + data caveat suffix
            regime_conclusion = _regime_based_conclusion(regime, direction, vp, kl)
            data_note = option_rec.risk_note or "期权数据不可用"
            return f"{regime_conclusion} (⚠️ {data_note})"
        if option_rec.wait_conditions:
            cond = option_rec.wait_conditions[0]
            # P1-2: avoid "观望, 等待 等待..." duplication
            cond = cond.removeprefix("等待")
            return f"观望 — {cond}"
        return "观望, 等待方向明确"

    return _regime_based_conclusion(regime, direction, vp, kl)


def _regime_based_conclusion(
    regime: USRegimeResult,
    direction: str,
    vp: VolumeProfileResult,
    kl: KeyLevels,
) -> str:
    """Regime-driven one-line conclusion (no option_rec dependency)."""
    price = regime.price
    if regime.regime in (USRegimeType.GAP_AND_GO, USRegimeType.TREND_DAY):
        if direction == "bullish":
            target = _nearest_levels(price, "above", vp, kl, n=1)
            tgt = f", 目标 {target[0][0]} {target[0][1]:,.2f}" if target else ""
            return f"回调至 VWAP {kl.vwap:,.2f} 附近做多{tgt}"
        target = _nearest_levels(price, "below", vp, kl, n=1)
        tgt = f", 目标 {target[0][0]} {target[0][1]:,.2f}" if target else ""
        return f"反弹至 VWAP {kl.vwap:,.2f} 附近做空{tgt}"

    if regime.regime == USRegimeType.FADE_CHOP:
        edge, _ = _closest_value_area_edge(price, vp)
        if edge == "VAH":
            return f"VAH {vp.vah:,.2f} 附近做空, 目标 POC {vp.poc:,.2f}"
        return f"VAL {vp.val:,.2f} 附近做多, 目标 POC {vp.poc:,.2f}"

    return "观望, 等待 Regime 明确后再入场"


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


# ── Preserved: _collect_levels (test compatible) ──


def _collect_levels(
    kl: KeyLevels,
    current_price: float,
) -> list[tuple[str, float, str]]:
    """Collect all non-zero levels as (name, value, annotation) tuples."""
    items: list[tuple[str, float, str]] = []

    if kl.gamma_call_wall > 0:
        items.append(("Call Wall", kl.gamma_call_wall, ""))
    if kl.pdh > 0:
        items.append(("PDH", kl.pdh, ""))
    if kl.pmh > 0:
        pm_tag = ""
        if kl.pm_source == "yahoo":
            pm_tag = " (Yahoo)"
        elif kl.pm_source == "gap_estimate":
            pm_tag = " (估)"
        items.append(("PMH", kl.pmh, pm_tag))
    if kl.vah > 0:
        items.append(("VAH", kl.vah, ""))
    if kl.vwap > 0:
        items.append(("VWAP", kl.vwap, ""))
    if kl.poc > 0:
        items.append(("POC", kl.poc, ""))
    if kl.val > 0:
        items.append(("VAL", kl.val, ""))
    if kl.pdl > 0:
        items.append(("PDL", kl.pdl, ""))
    if kl.pml > 0:
        pm_tag_l = ""
        if kl.pm_source == "yahoo":
            pm_tag_l = " (Yahoo)"
        elif kl.pm_source == "gap_estimate":
            pm_tag_l = " (估)"
        items.append(("PML", kl.pml, pm_tag_l))
    if kl.gamma_put_wall > 0:
        items.append(("Put Wall", kl.gamma_put_wall, ""))
    if kl.gamma_max_pain > 0:
        items.append(("Max Pain", kl.gamma_max_pain, ""))

    if items and current_price > 0:
        closest_idx = min(range(len(items)), key=lambda i: abs(items[i][1] - current_price))
        name, val, ann = items[closest_idx]
        if abs(val - current_price) / current_price < 0.005:
            items[closest_idx] = (name, val, "current")

    return items


# ── Main formatter ──


def format_us_playbook_message(
    result: USPlaybookResult,
    spy_result: USPlaybookResult | None = None,
    qqq_result: USPlaybookResult | None = None,
    trend_downgrade_confidence: float = 0.70,
) -> str:
    """Format US Playbook as Telegram HTML message — institutional-grade intraday playbook."""
    r = result.regime
    regime_cn = REGIME_NAME_CN.get(r.regime, "未知")
    now = result.generated_at or datetime.now(ET)
    kl = result.key_levels
    vp = result.volume_profile
    gamma_wall = result.gamma_wall
    quote = result.quote

    vwap = kl.vwap

    # Determine direction via unified logic (structure-aware)
    _direction = _decide_direction(
        r, vp, vwap=vwap, pdl=kl.pdl, pdh=kl.pdh, pml=kl.pml, pmh=kl.pmh,
    )
    if _direction == "neutral":
        if r.price > vp.vah:
            _direction = "bullish"
        elif r.price < vp.val:
            _direction = "bearish"
        elif vwap > 0:
            _direction = "bullish" if r.price > vwap else "bearish"
        elif vp.poc > 0:
            _direction = "bullish" if r.price > vp.poc else "bearish"
        else:
            _direction = "bullish"
    emoji = get_regime_emoji(r.regime, _direction)
    option_market = result.option_market
    recommendation = result.option_rec

    lines: list[str] = []
    sep = "━" * 20

    # ── Section 1: Header ──
    lines.append(f"━━━ <b>{_esc(result.symbol)} ({_esc(result.name)})</b> | 日内交易剧本 ━━━")
    lines.append(f"⏰ {now.strftime('%Y-%m-%d %H:%M:%S')} ET")

    is_premarket = now.hour < 9 or (now.hour == 9 and now.minute < 30)
    if is_premarket:
        lines.append("⏳ 盘前数据 — RVOL/Regime 待开盘后生效")

    # Market context
    ctx_parts = []
    if spy_result:
        spy_dir = _infer_market_direction(spy_result)
        se = get_regime_emoji(spy_result.regime.regime, spy_dir or "neutral")
        sn = REGIME_NAME_CN.get(spy_result.regime.regime, "未知")
        ctx_parts.append(f"SPY {se}{sn}")
    if qqq_result:
        qqq_dir = _infer_market_direction(qqq_result)
        qe = get_regime_emoji(qqq_result.regime.regime, qqq_dir or "neutral")
        qn = REGIME_NAME_CN.get(qqq_result.regime.regime, "未知")
        ctx_parts.append(f"QQQ {qe}{qn}")
    if ctx_parts:
        lines.append(f"大盘: {' | '.join(ctx_parts)}")

    # Primary/alternate regime expectation
    alt_name, alt_pct = _alternate_regime_info(r)
    conf_pct = int(r.confidence * 100)
    lines.append(f"预期: {emoji}{regime_cn} ({conf_pct}%) / {alt_name} ({alt_pct}%)")
    lines.append("")

    # ── Section 2: 核心结论 ──
    conclusion = _core_conclusion_text(r, _direction, vp, kl, recommendation)
    lines.append(f"🎯 <b>核心结论: {_esc(conclusion)}</b>")
    lines.append(f"▸ 当前状态: {_esc(_price_position(r.price, vp, vwap, kl))}")

    strategy_text = get_regime_strategy(r.regime, _direction)
    first_line = strategy_text.splitlines()[0] if strategy_text else ""
    lines.append(f"▸ 核心策略: {_esc(first_line)}")

    lines.append("")
    lines.append(SECTION_SEP)

    # ── Section 3: 剧本推演 ──
    lines.append("⚔️ <b>剧本推演</b>")
    lines.append("")

    _close_et = now.replace(hour=16, minute=0, second=0, microsecond=0)
    _min_left = max(0, int((_close_et - now).total_seconds() / 60))
    _intraday_range = 0.0
    if quote and quote.high_price > 0 and quote.low_price > 0:
        _intraday_range = (quote.high_price - quote.low_price) / quote.low_price * 100
    _spy_direction = _infer_market_direction(spy_result)
    _plan_ctx = PlanContext(
        minutes_to_close=_min_left,
        rvol=r.rvol,
        avg_daily_range_pct=getattr(result, "avg_daily_range_pct", 0.0),
        intraday_range_pct=_intraday_range,
        option_action=recommendation.action if recommendation else "",
        market_direction=_spy_direction,
    )

    plans = _generate_action_plans(
        r, _direction, vp, kl, gamma_wall, recommendation,
        ctx=_plan_ctx, trend_downgrade_confidence=trend_downgrade_confidence,
    )
    for plan in plans:
        for plan_line in _format_action_plan(plan):
            lines.append(plan_line)
        lines.append("")

    lines.append(SECTION_SEP)

    # ── Section 4: 盘面逻辑 ──
    lines.append("📝 <b>盘面逻辑</b>")
    rvol_label = _rvol_assessment(r.rvol)
    lines.append(f"▸ 量能: RVOL {r.rvol:.2f} ({rvol_label})")

    # Space analysis — upside/downside distances
    above = _nearest_levels(r.price, "above", vp, kl, gamma_wall, n=1)
    below = _nearest_levels(r.price, "below", vp, kl, gamma_wall, n=1)
    space_parts = []
    if above and r.price > 0:
        up_pct = (above[0][1] - r.price) / r.price * 100
        space_parts.append(f"上方至{above[0][0]} {up_pct:.1f}%")
    if below and r.price > 0:
        dn_pct = (r.price - below[0][1]) / r.price * 100
        space_parts.append(f"下方至{below[0][0]} {dn_pct:.1f}%")
    if space_parts:
        lines.append(f"▸ 空间: {' / '.join(space_parts)}")

    # IV environment (compact)
    if option_market and option_market.atm_iv > 0 and option_market.avg_iv > 0:
        iv_label = "偏高" if option_market.iv_ratio >= 1.2 else "偏低" if option_market.iv_ratio <= 0.85 else "正常"
        lines.append(f"▸ IV: ATM/Avg = {option_market.iv_ratio:.2f}x ({iv_label})")

    # Adaptive thresholds info
    if r.adaptive_thresholds:
        at = r.adaptive_thresholds
        lines.append(
            f"▸ 自适应阈值: P{at.get('sample', '?')}d GAP_AND_GO={at.get('gap_and_go', 0):.2f}"
            f" (rank {at.get('pctl_rank', 0):.0f}%)"
        )

    lines.append("")
    lines.append(SECTION_SEP)

    # ── Section 5: 数据雷达 ──
    price_display = quote.last_price if quote else r.price
    lines.append(f"📊 <b>数据雷达</b> (当前: {price_display:,.2f})")

    # Compact key data line
    vwap_pct = _pct_change(r.price, vwap)
    vwap_str = f"VWAP {vwap:,.2f} ({_format_percent(vwap_pct)})" if vwap > 0 else ""
    amp_str = ""
    if quote and quote.high_price > 0 and quote.low_price > 0:
        amp = (quote.high_price - quote.low_price) / quote.low_price * 100
        amp_str = f"振幅 {_format_percent(amp, signed=False)}"
    elif quote and quote.amplitude > 0:
        amp_str = f"振幅 {_format_percent(quote.amplitude, signed=False)}"
    compact_items = [x for x in [vwap_str, f"RVOL {r.rvol:.2f}", amp_str] if x]
    lines.append(" | ".join(compact_items))

    # VP levels
    vp_suffix = f" ({vp.trading_days}d)" if vp.trading_days > 0 else ""
    lines.append(f"VAH {vp.vah:,.2f} | POC {vp.poc:,.2f} | VAL {vp.val:,.2f}{vp_suffix}")

    # PDH/PDL
    pd_parts = []
    if kl.pdh > 0:
        pd_parts.append(f"PDH {kl.pdh:,.2f}")
    if kl.pdl > 0:
        pd_parts.append(f"PDL {kl.pdl:,.2f}")
    if pd_parts:
        lines.append(" | ".join(pd_parts))

    # PMH/PML
    pm_parts = []
    if kl.pmh > 0:
        pm_parts.append(f"PMH {kl.pmh:,.2f}")
    if kl.pml > 0:
        pm_parts.append(f"PML {kl.pml:,.2f}")
    if pm_parts:
        pm_tag = ""
        if kl.pm_source == "yahoo":
            pm_tag = " (Yahoo)"
        elif kl.pm_source == "gap_estimate":
            pm_tag = " (估)"
        lines.append(" | ".join(pm_parts) + pm_tag)

    # Gamma wall
    if gamma_wall:
        gw_parts = []
        if gamma_wall.call_wall_strike > 0:
            gw_parts.append(f"Call Wall {_format_strike(gamma_wall.call_wall_strike)}")
        if gamma_wall.put_wall_strike > 0:
            gw_parts.append(f"Put Wall {_format_strike(gamma_wall.put_wall_strike)}")
        if gamma_wall.max_pain > 0:
            gw_parts.append(f"MaxPain {_format_strike(gamma_wall.max_pain)}")
        if gw_parts:
            lines.append(" | ".join(gw_parts))

    # VP thin warning
    vp_td = vp.trading_days
    if 0 < vp_td < 3:
        lines.append(f"⚠️ VP 仅 {vp_td} 天数据")

    # Filter warnings
    for warning in result.filters.warnings:
        lines.append(f"⚠️ {_esc(warning)}")

    # DTE gamma warning
    if recommendation and recommendation.dte > 0 and recommendation.dte <= 3 and recommendation.action != "wait":
        lines.append(f"⚠️ 仅剩 {recommendation.dte} DTE, Gamma 风险极高")

    lines.append(sep)
    return "\n".join(lines)
