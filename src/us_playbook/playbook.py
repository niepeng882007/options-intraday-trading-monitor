"""US Playbook — format aggregated playbook messages (HK-style 4-section layout)."""

from __future__ import annotations

import html
from datetime import datetime
from zoneinfo import ZoneInfo

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
from src.us_playbook import KeyLevels, USPlaybookResult, USRegimeResult, USRegimeType
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


def _entry_check_text(
    rec: OptionRecommendation,
    regime: USRegimeResult,
    vp: VolumeProfileResult,
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
        return "只在价格仍沿着当前多头方向运行、没有跌回防守线下方时考虑买入，不追已经瞬间拉高很多的合约。"
    if rec.action == "put":
        return "只在价格仍沿着当前空头方向运行、没有重新站回防守线上方时考虑买入，不追已经瞬间杀跌很多的合约。"
    return "先等价格重新满足入场条件，再重新生成剧本。"


def _risk_action_lines(
    rec: OptionRecommendation | None,
    regime: USRegimeResult,
    vp: VolumeProfileResult,
) -> list[str]:
    if rec is None:
        return ["操作建议: 没有具体期权建议时，保持轻仓，只观察关键位反应。"]

    if rec.action == "wait":
        return [
            "操作建议: 当前不下单，保留资金，等重新评估条件满足后再考虑。",
        ]

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

    if rec.action == "call":
        return [
            "止损触发: 标的跌破 VWAP 或原本突破结构被破坏。",
            "触发后怎么做: 直接卖出平仓，不补仓摊平，不把短线单拖成长线。",
            "新手执行: 优先做 ATM 或轻度实值，不追过度虚值合约。",
        ]

    if rec.action == "put":
        return [
            "止损触发: 标的重新站回 VWAP 上方或原本下跌结构被破坏。",
            "触发后怎么做: 直接卖出平仓，不补仓摊平，不把短线单拖成长线。",
            "新手执行: 优先做 ATM 或轻度实值，不追过度虚值合约。",
        ]

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

    else:  # UNCLEAR
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
) -> str:
    """Format US Playbook as Telegram HTML message — 4-section HK-style layout."""
    r = result.regime
    regime_cn = REGIME_NAME_CN.get(r.regime, "未知")
    now = result.generated_at or datetime.now(ET)
    kl = result.key_levels
    vp = result.volume_profile
    gamma_wall = result.gamma_wall
    quote = result.quote

    # Determine direction from price vs VA for emoji/strategy
    if r.price > vp.vah:
        _direction = "bullish"
    elif r.price < vp.val:
        _direction = "bearish"
    elif vp.poc > 0:
        _direction = "bullish" if r.price > vp.poc else "bearish"
    else:
        _direction = "bullish"
    emoji = get_regime_emoji(r.regime, _direction)
    option_market = result.option_market
    recommendation = result.option_rec
    vwap = kl.vwap

    lines: list[str] = []
    sep = "━" * 20

    # ── Header ──
    lines.append(sep)
    lines.append(f"<b>{_esc(result.name)} ({_esc(result.symbol)})</b>")
    lines.append(f"{now.strftime('%Y-%m-%d %H:%M:%S')} ET")

    # 判断是否盘前
    is_premarket = now.hour < 9 or (now.hour == 9 and now.minute < 30)
    if is_premarket:
        lines.append("⏳ <b>盘前数据</b> — RVOL/Regime 待开盘后生效")

    # Market context in header line
    ctx_parts = []
    if spy_result:
        se = REGIME_EMOJI.get(spy_result.regime.regime, "❓")
        sn = REGIME_NAME_CN.get(spy_result.regime.regime, "未知")
        ctx_parts.append(f"SPY: {se} {sn}")
    if qqq_result:
        qe = REGIME_EMOJI.get(qqq_result.regime.regime, "❓")
        qn = REGIME_NAME_CN.get(qqq_result.regime.regime, "未知")
        ctx_parts.append(f"QQQ: {qe} {qn}")
    if ctx_parts:
        lines.append(" | ".join(ctx_parts))
    lines.append("")

    # ── Section 1: Regime ──
    lines.append(
        f"{emoji} <b>{_esc(regime_cn)}</b>  {_confidence_bar(r.confidence)} {r.confidence:.0%}"
    )
    lines.append("")
    lines.append(f"结论: {_esc(_regime_conclusion(r, vp, kl, vwap))}")

    regime_reasons, regime_supports, regime_uncertainties, regime_invalidations = _regime_reason_lines(
        r, vp, kl, vwap, gamma_wall, option_market, quote, option_rec=recommendation,
    )
    if regime_reasons:
        lines.append("")
        lines.append("判断依据:")
        for reason in regime_reasons:
            lines.append(f"  ▸ {_esc(reason)}")
    if regime_supports:
        lines.append("")
        lines.append("加分项:")
        for support in regime_supports:
            lines.append(f"  ▸ {_esc(support)}")
    if regime_uncertainties:
        lines.append("")
        lines.append("注意:")
        for uncertainty in regime_uncertainties:
            lines.append(f"  ▸ {_esc(uncertainty)}")
    if regime_invalidations:
        lines.append("")
        lines.append("⚡ 失效条件:")
        for invalidation in regime_invalidations:
            lines.append(f"  ▸ {_esc(invalidation)}")

    lines.append("")
    lines.append(SECTION_SEP)
    lines.append("")

    # ── Section 2: 实时数据 ──
    lines.append("📊 <b>实时数据</b>" + (" (昨日收盘)" if is_premarket else ""))
    lines.append("")

    if quote:
        change_pct = _pct_change(quote.last_price, quote.prev_close)
        spread_value = quote.ask_price - quote.bid_price if quote.ask_price > 0 and quote.bid_price > 0 else 0.0
        spread_pct = (spread_value / quote.last_price * 100) if quote.last_price > 0 and spread_value > 0 else None

        arrow = "▼" if (change_pct is not None and change_pct < 0) else "▲"
        pct_str = f"{abs(change_pct):.2f}%" if change_pct is not None else "N/A"
        lines.append(f"{quote.last_price:,.2f} {arrow}{pct_str}")

        lines.append(
            f"开 {quote.open_price:,.2f} │ 高 {quote.high_price:,.2f} │ "
            f"低 {quote.low_price:,.2f} │ 昨收 {quote.prev_close:,.2f}"
        )
        if quote.bid_price > 0 or quote.ask_price > 0:
            lines.append(
                f"买一 {quote.bid_price:,.2f} / 卖一 {quote.ask_price:,.2f} │ "
                f"价差 {spread_value:,.2f} ({_format_percent(spread_pct)})"
            )
        lines.append(
            f"成交量 {quote.volume:,} │ 成交额 {_format_turnover_usd(quote.turnover)}"
        )
        day_range_pct = _pct_change(quote.high_price, quote.low_price)
        tr_parts = []
        if quote.turnover_rate > 0:
            tr_parts.append(f"换手率 {_format_percent(quote.turnover_rate, signed=False)}")
        if quote.amplitude > 0:
            tr_parts.append(f"振幅 {_format_percent(quote.amplitude, signed=False)}")
        elif day_range_pct is not None:
            tr_parts.append(f"振幅 {_format_percent(day_range_pct, signed=False)}")
        if tr_parts:
            lines.append(" │ ".join(tr_parts))
    else:
        lines.append(f"{r.price:,.2f}")

    # Key levels block
    lines.append("")
    lines.append("关键位:")
    if vwap > 0:
        vwap_pct = _pct_change(r.price, vwap)
        lines.append(f"  VWAP {vwap:,.2f} ({_format_percent(vwap_pct)}) │ RVOL {r.rvol:.2f}")
    else:
        lines.append(f"  RVOL {r.rvol:.2f}")

    # PDH/PDL
    if kl.pdh > 0 or kl.pdl > 0:
        pdh_str = f"PDH {kl.pdh:,.2f}" if kl.pdh > 0 else ""
        pdl_str = f"PDL {kl.pdl:,.2f}" if kl.pdl > 0 else ""
        parts = [p for p in [pdh_str, pdl_str] if p]
        lines.append(f"  {' │ '.join(parts)}")

    # PMH/PML
    if kl.pmh > 0 or kl.pml > 0:
        pm_tag = ""
        if kl.pm_source == "yahoo":
            pm_tag = " (Yahoo)"
        elif kl.pm_source == "gap_estimate":
            pm_tag = " (估)"
        pmh_str = f"PMH {kl.pmh:,.2f}" if kl.pmh > 0 else ""
        pml_str = f"PML {kl.pml:,.2f}" if kl.pml > 0 else ""
        parts = [p for p in [pmh_str, pml_str] if p]
        lines.append(f"  {' │ '.join(parts)}{pm_tag}")

    lines.append(f"  POC {vp.poc:,.2f}")
    lines.append(f"  VAH {vp.vah:,.2f} │ VAL {vp.val:,.2f}")
    if vp.vah > vp.val and vp.val > 0:
        value_area_width_pct = (vp.vah - vp.val) / vp.val * 100
        lines.append(f"  Value Area 宽度 {vp.vah - vp.val:,.2f} ({value_area_width_pct:.2f}%)")
    lines.append(f"  {_price_position(r.price, vp, vwap, kl)}")

    vp_td = vp.trading_days
    if 0 < vp_td < 3:
        lines.append(f"  ⚠️ VP 仅 {vp_td} 天数据，VAH/VAL 参考性降低")

    if gamma_wall and (
        gamma_wall.call_wall_strike > 0
        or gamma_wall.put_wall_strike > 0
        or gamma_wall.max_pain > 0
    ):
        gamma_parts = []
        if gamma_wall.call_wall_strike > 0:
            gamma_parts.append(f"Call Wall {gamma_wall.call_wall_strike:,.0f}")
        if gamma_wall.put_wall_strike > 0:
            gamma_parts.append(f"Put Wall {gamma_wall.put_wall_strike:,.0f}")
        if gamma_wall.max_pain > 0:
            gamma_parts.append(f"Max Pain {gamma_wall.max_pain:,.0f}")
        lines.append(f"  Gamma / Pain: {' │ '.join(gamma_parts)}")

    # Option environment
    if option_market and option_market.expiry:
        lines.append("")
        lines.append("期权环境:")
        dte_str = ""
        if recommendation and recommendation.dte > 0:
            dte_str = f" ({recommendation.dte} DTE)"
        lines.append(f"  到期日 {option_market.expiry}{dte_str}")
        lines.append(
            f"  合约 {option_market.contract_count}"
            f" (Call {option_market.call_contract_count} / Put {option_market.put_contract_count})"
        )
        if option_market.atm_iv > 0 or option_market.avg_iv > 0:
            lines.append(
                f"  ATM IV {option_market.atm_iv:.2f} │ 全链中位 IV {option_market.avg_iv:.2f}"
                f" │ 比值 {option_market.iv_ratio:.2f}x"
            )

    lines.append("")
    lines.append(SECTION_SEP)
    lines.append("")

    # ── Section 3: 期权建议 ──
    if recommendation:
        if recommendation.action == "wait":
            lines.append(f"🎯 <b>建议: {_action_label(recommendation.action)}</b>")
            lines.append("")
            lines.append(f"白话解释: {_esc(_action_plain_language(recommendation))}")
            if recommendation.rationale:
                lines.append(f"当前结论: {_esc(recommendation.rationale)}")
            for reason in _split_reason_lines(recommendation.risk_note):
                lines.append(f"先别下单的原因: {_esc(reason)}")
            if recommendation.wait_conditions:
                lines.append("重新评估条件:")
                for condition in recommendation.wait_conditions:
                    lines.append(f"  ▸ {_esc(condition)}")
        else:
            lines.append(f"🎯 <b>建议: {_action_label(recommendation.action)}</b>")
            lines.append("")
            lines.append(f"白话解释: {_esc(_action_plain_language(recommendation))}")
            if recommendation.expiry:
                dte_str = f" ({recommendation.dte} DTE)" if recommendation.dte > 0 else ""
                lines.append(f"到期日: {recommendation.expiry}{dte_str}")
            lines.append(f"{_position_size_text(r.confidence)}")
            lines.append("")
            lines.append(f"入场前提: {_esc(_entry_check_text(recommendation, r, vp))}")
            lines.append("")
            for leg in recommendation.legs:
                for leg_line in _format_leg_line(leg):
                    lines.append(_esc(leg_line))

            sm = recommendation.spread_metrics
            if sm and sm.max_loss > 0:
                lines.append("")
                lines.append("📋 Spread 损益:")
                lines.append(f"  净收入 {sm.net_credit:,.3f} │ 最大亏损 {sm.max_loss:,.3f}")
                lines.append(
                    f"  盈亏平衡 {sm.breakeven:,.3f} │ R:R {sm.risk_reward_ratio:.2f}:1"
                )
                if sm.win_probability > 0:
                    lines.append(
                        f"  到期盈利概率 ~{sm.win_probability:.0%} (基于 Delta)"
                    )

            lines.append("")
            if recommendation.rationale:
                lines.append(f"为什么是这单: {_esc(recommendation.rationale)}")
            if recommendation.action in {"bull_put_spread", "bear_call_spread"}:
                lines.append(_spread_execution_text(recommendation))
            else:
                lines.append("执行: 直接买入单腿期权，限价单优先，不追已经瞬间拉开价差的合约。")
            if recommendation.liquidity_warning:
                lines.append(f"⚠️ 流动性提醒: {_esc(recommendation.liquidity_warning)}")
    else:
        lines.append("🎯 <b>交易风格建议</b>")
        strategy_text = get_regime_strategy(r.regime, _direction)
        for strategy_line in strategy_text.splitlines():
            lines.append(_esc(strategy_line))

    lines.append("")
    lines.append(SECTION_SEP)
    lines.append("")

    # ── Section 4: 风险 ──
    lines.append(f"⚠️ <b>风险</b>  {_risk_status_text(result.filters)}")
    lines.append("")

    level_items = _level_distance_items(r.price, vp, kl, gamma_wall)
    if level_items:
        lines.append(f"距关键位: {' │ '.join(level_items)}")

    # DTE gamma warning
    if recommendation and recommendation.dte > 0 and recommendation.dte <= 3 and recommendation.action != "wait":
        lines.append(f"⚠️ 仅剩 {recommendation.dte} DTE, Gamma 风险极高, 价格小幅波动可能导致大幅亏损")

    for warning in result.filters.warnings:
        lines.append(f"风险提示: {_esc(warning)}")

    if recommendation and recommendation.risk_note and recommendation.action != "wait":
        for risk_line in _split_reason_lines(recommendation.risk_note):
            if "DTE" in risk_line and "Gamma" in risk_line:
                continue
            if "DTE" in risk_line and "Theta" in risk_line:
                continue
            lines.append(_esc(risk_line))

    lines.append("")
    for risk_action_line in _risk_action_lines(recommendation, r, vp):
        lines.append(_esc(risk_action_line))

    lines.append(sep)
    return "\n".join(lines)
