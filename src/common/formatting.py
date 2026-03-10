"""Shared playbook formatting utilities — used by both HK and US playbook modules."""

from __future__ import annotations

from src.common.types import (
    FilterResult,
    OptionRecommendation,
    VolumeProfileResult,
)


def confidence_bar(confidence: float) -> str:
    """Render a 5-block confidence bar."""
    filled = int(confidence * 5)
    return "█" * filled + "░" * (5 - filled)


def pct_change(current_value: float, base_value: float) -> float | None:
    if current_value <= 0 or base_value <= 0:
        return None
    return (current_value - base_value) / base_value * 100


def format_percent(value: float | None, decimals: int = 2, signed: bool = True) -> str:
    if value is None:
        return "N/A"
    sign = "+" if signed else ""
    return f"{value:{sign}.{decimals}f}%"


def split_reason_lines(raw_text: str) -> list[str]:
    if not raw_text:
        return []
    normalized = raw_text.replace("; ", "\n").replace("；", "\n")
    return [part.strip() for part in normalized.splitlines() if part.strip()]


def closest_value_area_edge(price: float, vp: VolumeProfileResult) -> tuple[str, float]:
    if vp.vah <= 0 or vp.val <= 0:
        return "", 0.0
    distance_to_vah = abs(vp.vah - price)
    distance_to_val = abs(price - vp.val)
    if distance_to_vah <= distance_to_val:
        return "VAH", distance_to_vah
    return "VAL", distance_to_val


def action_label(action: str) -> str:
    return {
        "call": "↑ 买入 Call",
        "put": "↓ 买入 Put",
        "bull_put_spread": "↑ Bull Put Spread",
        "bear_call_spread": "↓ Bear Call Spread",
        "wait": "⛔ 观望",
    }.get(action, action)


def action_plain_language(rec: OptionRecommendation) -> str:
    action = rec.action
    if action == "call":
        return "这是直接买入看涨期权，适合判断标的会继续上冲的人。盈利来自标的继续上涨，若走势不对，亏损主要是已付权利金。"
    if action == "put":
        return "这是直接买入看跌期权，适合判断标的会继续下跌的人。盈利来自标的继续下跌，若走势不对，亏损主要是已付权利金。"
    if action == "bull_put_spread":
        return "这是牛市 Put 价差。核心想法不是赌暴涨，而是赌价格不要明显跌破下方支撑，同时用买入的保护腿把风险封顶。"
    if action == "bear_call_spread":
        return "这是熊市 Call 价差。核心想法不是赌暴跌，而是赌价格不要明显涨破上方压力，同时用买入的保护腿把风险封顶。"
    return "当前没有足够把握下单，先保留资金，等条件更清晰。"


def format_strike(strike: float) -> str:
    """Format strike price — show 1 decimal if fractional, otherwise integer."""
    if strike % 1:
        return f"{strike:,.1f}"
    return f"{strike:,.0f}"


def format_leg_line(leg) -> list[str]:
    """Format a single option leg as 2-line display."""
    side_cn = "买" if leg.side == "buy" else "卖"
    header = f"  {side_cn} {leg.option_type.upper()} {format_strike(leg.strike)} ({leg.moneyness})"
    metrics = []
    if leg.delta is not None:
        metrics.append(f"Δ {leg.delta:+.2f}")
    if leg.open_interest is not None:
        metrics.append(f"OI {leg.open_interest:,}")
    if leg.last_price is not None and leg.last_price > 0:
        metrics.append(f"价 {leg.last_price:,.3f}")
    if leg.implied_volatility is not None and leg.implied_volatility > 0:
        metrics.append(f"IV {leg.implied_volatility:.1f}")
    if leg.volume is not None and leg.volume > 0:
        metrics.append(f"量 {leg.volume:,}")
    result = [header]
    if metrics:
        result.append(f"    {' │ '.join(metrics)}")
    return result


def position_size_text(confidence: float) -> str:
    """Generate position sizing recommendation based on confidence."""
    if confidence >= 0.85:
        return f"仓位参考: 正常仓位 (置信度 {confidence:.0%})"
    if confidence >= 0.70:
        return f"仓位参考: 正常仓位的 70% (置信度 {confidence:.0%})"
    if confidence >= 0.55:
        return f"仓位参考: 正常仓位的 50% (置信度 {confidence:.0%})"
    return f"仓位参考: 最小仓位或观望 (置信度 {confidence:.0%})"


def spread_execution_text(rec: OptionRecommendation) -> str:
    """Generate single-line execution instruction for spreads."""
    if len(rec.legs) < 2:
        return "执行: 组合单一次提交，限价单优先，价差太宽不追。"
    first = rec.legs[0]
    second = rec.legs[1]
    first_action = "卖" if first.side == "sell" else "买"
    second_action = "买" if second.side == "buy" else "卖"
    return (
        f"执行: 组合单一次提交 ({first_action} {first.option_type.upper()} {format_strike(first.strike)}"
        f" + {second_action} {second.option_type.upper()} {format_strike(second.strike)})，限价单优先，价差太宽不追。"
    )


def risk_status_text(filters: FilterResult) -> str:
    if not filters.tradeable:
        return "🔴 今日不宜交易"
    if filters.risk_level == "high":
        return "🟡 高风险日 - 只适合非常轻仓或直接放弃"
    if filters.risk_level == "elevated":
        return "🟡 风险偏高 - 必须控制节奏"
    return "🟢 正常交易日"
