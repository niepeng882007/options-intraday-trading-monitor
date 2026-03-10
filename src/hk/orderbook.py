from __future__ import annotations
from datetime import datetime, timezone, timedelta
from src.hk import OrderBookAlert
from src.utils.logger import setup_logger

logger = setup_logger("hk_orderbook")

HKT = timezone(timedelta(hours=8))


def _extract_levels(book_side: list, top_n: int) -> list[tuple[float, int, int]]:
    levels: list[tuple[float, int, int]] = []
    for entry in book_side[:top_n]:
        if isinstance(entry, (list, tuple)) and len(entry) >= 3:
            price = float(entry[0])
            volume = int(entry[1])
            order_count = int(entry[2])
            levels.append((price, volume, order_count))
    return levels


def analyze_order_book(
    book: dict,
    large_order_ratio: float = 3.0,
) -> list[OrderBookAlert]:
    """Analyze LV2 order book for unusual large orders.

    A level is flagged as "large" if its volume > large_order_ratio * average volume
    across all visible levels.

    Args:
        book: Raw order book dict from Futu (keys: code, Ask, Bid)
        large_order_ratio: threshold multiplier vs average level volume

    Returns:
        List of OrderBookAlert for any detected anomalies
    """
    alerts: list[OrderBookAlert] = []
    symbol = book.get("code", "")
    now = datetime.now(HKT)

    for side_key, side_name in [("Ask", "ask"), ("Bid", "bid")]:
        levels = book.get(side_key, [])
        if not levels:
            continue

        # Extract volumes from all levels
        volumes = []
        for entry in levels:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                volumes.append(int(entry[1]))
            else:
                volumes.append(0)

        if not volumes:
            continue

        avg_vol = sum(volumes) / len(volumes)
        if avg_vol <= 0:
            continue

        # Check each level for anomaly
        for i, entry in enumerate(levels):
            if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                continue

            price = float(entry[0])
            vol = int(entry[1])
            ratio = vol / avg_vol

            if ratio >= large_order_ratio:
                alerts.append(OrderBookAlert(
                    symbol=symbol,
                    side=side_name,
                    price=price,
                    volume=vol,
                    avg_volume=avg_vol,
                    ratio=ratio,
                    timestamp=now,
                ))
                logger.info(
                    "Large order detected: %s %s L%d price=%.2f vol=%d (%.1fx avg)",
                    symbol, side_name, i + 1, price, vol, ratio,
                )

    return alerts


def format_order_book_summary(book: dict, top_n: int = 5) -> str:
    """Format order book into readable text for Telegram."""
    symbol = book.get("code", "?")
    lines = [f"📚 <b>盘口快照 {symbol}</b>"]

    ask_levels = _extract_levels(book.get("Ask", []), top_n)
    bid_levels = _extract_levels(book.get("Bid", []), top_n)
    if not ask_levels and not bid_levels:
        lines.append("  暂无可用盘口数据")
        return "\n".join(lines)

    best_ask = ask_levels[0][0] if ask_levels else 0.0
    best_bid = bid_levels[0][0] if bid_levels else 0.0
    spread = best_ask - best_bid if best_ask > 0 and best_bid > 0 else 0.0
    ask_total_volume = sum(level[1] for level in ask_levels)
    bid_total_volume = sum(level[1] for level in bid_levels)

    lines.append(
        f"  最优卖价: {best_ask:,.2f} | 最优买价: {best_bid:,.2f} | 价差: {spread:,.2f}"
    )
    lines.append(
        f"  前 {top_n} 档卖盘总量: {ask_total_volume:,} | 前 {top_n} 档买盘总量: {bid_total_volume:,}"
    )

    if ask_levels:
        heaviest_ask = max(ask_levels, key=lambda item: item[1])
        lines.append(
            f"  最大卖盘: {heaviest_ask[0]:,.2f} / {heaviest_ask[1]:,} 股 ({heaviest_ask[2]} 笔)"
        )
    if bid_levels:
        heaviest_bid = max(bid_levels, key=lambda item: item[1])
        lines.append(
            f"  最大买盘: {heaviest_bid[0]:,.2f} / {heaviest_bid[1]:,} 股 ({heaviest_bid[2]} 笔)"
        )

    lines.append("")
    lines.append("  盘口结论:")
    if ask_total_volume > bid_total_volume * 1.2:
        lines.append("  • 卖盘略强，短线更容易先看到上方抛压。")
    elif bid_total_volume > ask_total_volume * 1.2:
        lines.append("  • 买盘略强，短线更容易先看到下方承接。")
    else:
        lines.append("  • 买卖盘力量接近，盘口暂未给出明显方向。")
    if spread > 0:
        lines.append("  • 若价差持续放宽，追价成交的滑点风险会明显增加。")

    return "\n".join(lines)


def format_alerts_message(alerts: list[OrderBookAlert]) -> str:
    """Format order book alerts for Telegram push."""
    if not alerts:
        return ""

    lines = ["🚨 <b>盘口异常检测</b>"]
    sorted_alerts = sorted(alerts, key=lambda item: item.ratio, reverse=True)
    for a in sorted_alerts:
        side_cn = "买盘" if a.side == "bid" else "卖盘"
        emoji = "🟢" if a.side == "bid" else "🔴"
        lines.append(
            f"  {emoji} {a.symbol} {side_cn} {a.price:.2f}: "
            f"{a.volume:,} 股 ({a.ratio:.1f}x 均量)"
        )
        if a.side == "bid":
            lines.append("  • 解读: 该价位买盘明显偏大，短线更容易形成托底。")
        else:
            lines.append("  • 解读: 该价位卖盘明显偏大，短线更容易形成压盘。")
    return "\n".join(lines)
