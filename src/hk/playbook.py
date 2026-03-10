"""HK Playbook — generate and format aggregated playbook messages."""

from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone

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
from src.hk import (
    Playbook,
    RegimeResult,
    RegimeType,
)
from src.utils.logger import setup_logger

logger = setup_logger("hk_playbook")

HKT = timezone(timedelta(hours=8))

REGIME_EMOJI = {
    RegimeType.BREAKOUT: "🚀",
    RegimeType.RANGE: "📦",
    RegimeType.WHIPSAW: "🌊",
    RegimeType.UNCLEAR: "❓",
}

REGIME_NAME_CN = {
    RegimeType.BREAKOUT: "单边突破日",
    RegimeType.RANGE: "区间震荡日",
    RegimeType.WHIPSAW: "高波洗盘日",
    RegimeType.UNCLEAR: "不明确日",
}

REGIME_STRATEGY = {
    RegimeType.BREAKOUT: (
        "动量风格 - 顺势操作\n"
        "▸ 买入 ATM 或轻度 OTM 期权 (Delta 0.3-0.5)\n"
        "▸ 以 VWAP 为防守线\n"
        "▸ 顺势加仓, 不抄底/摸顶"
    ),
    RegimeType.RANGE: (
        "均值回归风格 - 高抛低吸\n"
        "▸ 严禁买入虚值期权\n"
        "▸ 买入深度 ITM 期权 (Delta > 0.7)\n"
        "▸ 在 VAH 附近做空, VAL 附近做多\n"
        "▸ 快进快出, 不恋战"
    ),
    RegimeType.WHIPSAW: (
        "右侧确认风格 - 等待确认\n"
        "▸ 降低仓位至正常的 50%\n"
        "▸ 等待带量突破后回踩确认\n"
        "▸ 避免在 Gamma 墙附近开仓"
    ),
    RegimeType.UNCLEAR: (
        "观望为主 - 降低仓位\n"
        "▸ 仓位降至正常的 30%\n"
        "▸ 等待 Regime 更新\n"
        "▸ 仅参与高确定性机会"
    ),
}

SECTION_SEP = "─ ─ ─ ─ ─ ─ ─ ─ ─ ─"


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
) -> Playbook:
    """Generate a complete Playbook object."""
    if filters is None:
        filters = FilterResult(tradeable=True)

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

    strategy_text = REGIME_STRATEGY.get(regime.regime, "")

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
    )



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


def _level_distance_items(
    price: float,
    vp: VolumeProfileResult,
    gamma_wall: GammaWallResult | None,
) -> list[str]:
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




def _entry_check_text(rec: OptionRecommendation, regime: RegimeResult, vp: VolumeProfileResult) -> str:
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
    regime: RegimeResult,
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
            f"止损触发: 标的涨破{stop_ref}，或 Regime 从 RANGE 转成 BREAKOUT。",
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
            f"止损触发: 标的跌破{stop_ref}，或 Regime 从 RANGE 转成 BREAKOUT。",
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




def _regime_conclusion(
    regime: RegimeResult,
    vp: VolumeProfileResult,
    vwap: float,
) -> str:
    if regime.regime == RegimeType.BREAKOUT:
        is_momentum = "Momentum" in (regime.details or "")
        if is_momentum:
            if regime.price > vp.vah:
                return "价格稳步脱离价值区上沿，虽然整体量能偏低但价格结构偏多，按低量趋势日处理。"
            return "价格稳步脱离价值区下沿，虽然整体量能偏低但价格结构偏空，按低量趋势日处理。"
        if regime.price > vp.vah:
            return "价格已脱离价值区上沿，当前按向上突破而不是区间震荡处理。"
        return "价格已跌出价值区下沿，当前按向下突破而不是区间震荡处理。"
    if regime.regime == RegimeType.RANGE:
        edge, _ = _closest_value_area_edge(regime.price, vp)
        if edge == "VAH":
            return "当前更偏向区间内震荡，优先按上沿回落思路看待，不按单边突破处理。"
        return "当前更偏向区间内震荡，优先按下沿反弹思路看待，不按单边突破处理。"
    if regime.regime == RegimeType.WHIPSAW:
        return "波动放大但方向不稳定，当前以等待确认优先，不适合抢跑。"
    if vwap > 0:
        return "多空信号混杂，先观察价格相对 VWAP 与价值区边界的反应。"
    return "多空信号混杂，当前没有足够把握给出明确方向。"


def _regime_reason_lines(
    regime: RegimeResult,
    vp: VolumeProfileResult,
    vwap: float,
    gamma_wall: GammaWallResult | None,
    option_market: OptionMarketSnapshot | None,
    quote: QuoteSnapshot | None,
    option_rec: OptionRecommendation | None = None,
) -> tuple[list[str], list[str], list[str], list[str]]:
    reasons: list[str] = []
    supports: list[str] = []
    uncertainties: list[str] = []
    invalidations: list[str] = []

    if regime.regime == RegimeType.BREAKOUT:
        is_momentum = "Momentum" in (regime.details or "")
        if regime.price > vp.vah:
            reasons.append(f"价格 {regime.price:,.2f} 高于 VAH {vp.vah:,.2f}，已经脱离价值区上沿。")
            invalidations.append(f"若价格重新跌回 VAH {vp.vah:,.2f} 下方，突破可信度会明显下降。")
        elif regime.price < vp.val:
            reasons.append(f"价格 {regime.price:,.2f} 低于 VAL {vp.val:,.2f}，已经脱离价值区下沿。")
            invalidations.append(f"若价格重新站回 VAL {vp.val:,.2f} 上方，突破可信度会明显下降。")
        if is_momentum:
            reasons.append(
                f"RVOL {regime.rvol:.2f} 低于突破阈值，但价格已明显脱离价值区，结构上更像低量漂移趋势日。"
            )
            if "volume surge" in (regime.details or ""):
                reasons.append("近期出现量能突变，短线动量正在加速。")
            invalidations.append("若量能持续萎缩且价格开始回落至 VA 内，可能是虚假突破。")
        else:
            reasons.append(f"RVOL {regime.rvol:.2f} 显示量能已配合突破方向。")
        if vwap > 0:
            vwap_relation = "高于" if regime.price > vwap else "低于"
            direction_text = "多头" if regime.price > vwap else "空头"
            reasons.append(f"当前价{vwap_relation} VWAP {vwap:,.2f}，盘中 {direction_text} 结构仍在。")
        invalidations.append("若后续量能明显回落，同时价格重新回到价值区内，需要重新定调。")

    elif regime.regime == RegimeType.RANGE:
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
        invalidations.append("若 RVOL 快速抬升并持续放大，需要重新评估是否切换到 BREAKOUT。")

    elif regime.regime == RegimeType.WHIPSAW:
        reasons.append("当前属于高波洗盘结构，价格容易反复穿越短线关键位。")
        if gamma_wall and gamma_wall.call_wall_strike > 0:
            reasons.append(f"上方 Gamma 墙在 {gamma_wall.call_wall_strike:,.0f}，容易形成短线压制。")
        if gamma_wall and gamma_wall.put_wall_strike > 0:
            reasons.append(f"下方 Gamma 墙在 {gamma_wall.put_wall_strike:,.0f}，容易形成短线承接。")
        if option_market and option_market.atm_iv > 0 and option_market.avg_iv > 0:
            reasons.append(
                f"ATM IV {option_market.atm_iv:.2f} 高于全链中位 IV {option_market.avg_iv:.2f}，波动预期偏高。"
            )
        uncertainties.append("这类结构里假突破和快速反抽都更常见，入场过早容易被洗出。")
        invalidations.append("只有在价格脱离 Gamma 墙附近并伴随量能确认后，才考虑改判为趋势日。")

    else:
        reasons.append("当前价格、量能和位置关系没有形成一致信号。")
        if vwap > 0:
            reasons.append(f"先观察价格相对 VWAP {vwap:,.2f} 的站稳或跌破情况。")
        if vp.vah > 0 and vp.val > 0:
            reasons.append(f"同时观察是否会有效突破 VAH {vp.vah:,.2f} 或跌破 VAL {vp.val:,.2f}。")
        invalidations.append("若价格脱离价值区并伴随量能扩张，可重新生成更明确的剧本。")

    if gamma_wall:
        if gamma_wall.call_wall_strike > 0:
            call_distance_pct = abs(gamma_wall.call_wall_strike - regime.price) / regime.price * 100
            if call_distance_pct <= 1.5:
                supports.append(
                    f"Call Wall {gamma_wall.call_wall_strike:,.0f} 距离当前价仅 {call_distance_pct:.1f}%，上方压力更值得关注。"
                )
        if gamma_wall.put_wall_strike > 0:
            put_distance_pct = abs(regime.price - gamma_wall.put_wall_strike) / regime.price * 100
            if put_distance_pct <= 1.5:
                supports.append(
                    f"Put Wall {gamma_wall.put_wall_strike:,.0f} 距离当前价仅 {put_distance_pct:.1f}%，下方承接更值得关注。"
                )

    # Phase 3: IV interpretation — context-aware (buyer vs seller strategy)
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


def format_playbook_message(
    playbook: Playbook,
    symbol: str = "",
    update_type: str = "manual",
) -> str:
    """Format playbook as Telegram HTML message — 5-section aggregated output."""
    _esc = html.escape
    regime = playbook.regime
    regime_emoji = REGIME_EMOJI.get(regime.regime, "❓")
    regime_cn = REGIME_NAME_CN.get(regime.regime, "未知")
    now = playbook.generated_at or datetime.now(HKT)
    vwap = playbook.vwap
    vp = playbook.volume_profile
    gamma_wall = playbook.gamma_wall
    quote = playbook.quote
    option_market = playbook.option_market
    recommendation = playbook.option_rec

    lines: list[str] = []
    sep = "━" * 20

    # ── Header ──
    lines.append(sep)
    lines.append(f"<b>{_esc(symbol)}</b>")
    lines.append(f"{now.strftime('%Y-%m-%d %H:%M:%S')} HKT")
    lines.append("")

    # ── Section 1: 市场定调 (compact header) ──
    lines.append(
        f"{regime_emoji} <b>{_esc(regime_cn)}</b>  {_confidence_bar(regime.confidence)} {regime.confidence:.0%}"
    )
    lines.append("")
    lines.append(f"结论: {_esc(_regime_conclusion(regime, vp, vwap))}")

    regime_reasons, regime_supports, regime_uncertainties, regime_invalidations = _regime_reason_lines(
        regime, vp, vwap, gamma_wall, option_market, quote, option_rec=recommendation,
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
    lines.append("📊 <b>实时数据</b>")
    lines.append("")
    if quote:
        change_pct = _pct_change(quote.last_price, quote.prev_close)
        spread_value = quote.ask_price - quote.bid_price if quote.ask_price > 0 and quote.bid_price > 0 else 0.0
        spread_pct = (spread_value / quote.last_price * 100) if quote.last_price > 0 and spread_value > 0 else None

        # Price line: compact
        arrow = "▼" if (change_pct is not None and change_pct < 0) else "▲"
        pct_str = f"{abs(change_pct):.2f}%" if change_pct is not None else "N/A"
        lines.append(f"{quote.last_price:,.2f} {arrow}{pct_str}")

        # OHLC with │ separators
        lines.append(
            f"开 {quote.open_price:,.2f} │ 高 {quote.high_price:,.2f} │ "
            f"低 {quote.low_price:,.2f} │ 昨收 {quote.prev_close:,.2f}"
        )
        # Bid/Ask
        lines.append(
            f"买一 {quote.bid_price:,.2f} / 卖一 {quote.ask_price:,.2f} │ "
            f"价差 {spread_value:,.2f} ({_format_percent(spread_pct)})"
        )
        # Volume/Turnover
        lines.append(
            f"成交量 {quote.volume:,} │ 成交额 {_format_turnover(quote.turnover)}"
        )
        # Turnover rate / Amplitude
        day_range_pct = _pct_change(quote.high_price, quote.low_price)
        lines.append(
            f"换手率 {_format_percent(quote.turnover_rate, signed=False)} │ "
            f"振幅 {_format_percent(day_range_pct, signed=False)}"
        )
    else:
        lines.append(f"{regime.price:,.2f}")

    # Key levels block (compact, no bullet prefix)
    lines.append("")
    lines.append("关键位:")
    if vwap > 0:
        vwap_pct = _pct_change(regime.price, vwap)
        lines.append(f"  VWAP {vwap:,.2f} ({_format_percent(vwap_pct)}) │ RVOL {regime.rvol:.2f}")
    else:
        lines.append(f"  RVOL {regime.rvol:.2f}")
    lines.append(f"  POC {vp.poc:,.2f}")
    lines.append(f"  VAH {vp.vah:,.2f} │ VAL {vp.val:,.2f}")
    if vp.vah > vp.val and vp.val > 0:
        value_area_width_pct = (vp.vah - vp.val) / vp.val * 100
        lines.append(f"  Value Area 宽度 {vp.vah - vp.val:,.2f} ({value_area_width_pct:.2f}%)")
    lines.append(f"  {_price_position(regime.price, vp, vwap)}")

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
            lines.append(f"{_position_size_text(regime.confidence)}")
            lines.append("")
            lines.append(f"入场前提: {_esc(_entry_check_text(recommendation, regime, vp))}")
            lines.append("")
            # Legs: 2-line format
            for leg in recommendation.legs:
                for leg_line in _format_leg_line(leg):
                    lines.append(_esc(leg_line))

            # Spread P&L block
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
        for strategy_line in playbook.strategy_text.splitlines():
            lines.append(_esc(strategy_line))

    lines.append("")
    lines.append(SECTION_SEP)
    lines.append("")

    # ── Section 4: 风险 ──
    lines.append(f"⚠️ <b>风险</b>  {_risk_status_text(playbook.filters)}")
    lines.append("")

    level_items = _level_distance_items(regime.price, vp, gamma_wall)
    if level_items:
        lines.append(f"距关键位: {' │ '.join(level_items)}")

    # DTE gamma warning (from risk_note)
    if recommendation and recommendation.dte > 0 and recommendation.dte <= 3 and recommendation.action != "wait":
        lines.append(f"⚠️ 仅剩 {recommendation.dte} DTE, Gamma 风险极高, 价格小幅波动可能导致大幅亏损")

    for warning in playbook.filters.warnings:
        lines.append(f"风险提示: {_esc(warning)}")

    if recommendation and recommendation.risk_note and recommendation.action != "wait":
        for risk_line in _split_reason_lines(recommendation.risk_note):
            # Skip DTE warnings already shown above
            if "DTE" in risk_line and "Gamma" in risk_line:
                continue
            if "DTE" in risk_line and "Theta" in risk_line:
                continue
            lines.append(_esc(risk_line))

    lines.append("")
    for risk_action_line in _risk_action_lines(recommendation, regime, vp):
        lines.append(_esc(risk_action_line))

    lines.append(sep)
    return "\n".join(lines)
