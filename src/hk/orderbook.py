from __future__ import annotations
from datetime import datetime, timezone, timedelta
from src.hk import OrderBookAlert
from src.utils.logger import setup_logger

logger = setup_logger("hk_orderbook")

HKT = timezone(timedelta(hours=8))


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
    lines = []
    symbol = book.get("code", "?")
    lines.append(f"<b>盘口快照 {symbol}</b>")

    asks = book.get("Ask", [])
    bids = book.get("Bid", [])

    # Show asks in reverse (highest first) then bids
    ask_lines = []
    for entry in asks[:top_n]:
        if isinstance(entry, (list, tuple)) and len(entry) >= 3:
            price, vol, orders = float(entry[0]), int(entry[1]), int(entry[2])
            ask_lines.append(f"  卖 {price:>10.2f}  {vol:>8,}  ({orders} 笔)")

    # Reverse asks so lowest ask (best ask) is at bottom, closest to bid
    for line in reversed(ask_lines):
        lines.append(line)

    lines.append(f"  {'─' * 30}")

    for entry in bids[:top_n]:
        if isinstance(entry, (list, tuple)) and len(entry) >= 3:
            price, vol, orders = float(entry[0]), int(entry[1]), int(entry[2])
            lines.append(f"  买 {price:>10.2f}  {vol:>8,}  ({orders} 笔)")

    return "\n".join(lines)


def format_alerts_message(alerts: list[OrderBookAlert]) -> str:
    """Format order book alerts for Telegram push."""
    if not alerts:
        return ""

    lines = ["🚨 <b>盘口异常检测</b>"]
    for a in alerts:
        side_cn = "买盘" if a.side == "bid" else "卖盘"
        emoji = "🟢" if a.side == "bid" else "🔴"
        lines.append(
            f"  {emoji} {a.symbol} {side_cn} {a.price:.2f}: "
            f"{a.volume:,} 股 ({a.ratio:.1f}x 均量)"
        )
    return "\n".join(lines)
