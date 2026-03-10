"""Daily summary report — collects US Pipeline signals and formats a Telegram report."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

ET = timezone(timedelta(hours=-5))


@dataclass
class TradeRecord:
    strategy_name: str
    symbol: str
    entry_price: float
    exit_price: float
    pnl_pct: float
    direction: str
    exit_reason: str
    entry_time: str       # HH:MM ET
    quality_grade: str    # A/B/C/D


@dataclass
class DailySummaryData:
    date_str: str
    total_entries: int
    completed_trades: int
    trades: list[TradeRecord] = field(default_factory=list)
    daily_pnl: float = 0.0
    strategy_dist: dict[str, int] = field(default_factory=dict)
    quality_dist: dict[str, int] = field(default_factory=dict)


def collect_pipeline_data(sqlite_store, daily_pnl: float) -> DailySummaryData:
    """Collect today's signals from SQLite and build summary data."""
    signals = sqlite_store.get_today_signals()
    now = datetime.now(ET)
    date_str = now.strftime("%Y-%m-%d (%a)")

    entries = [s for s in signals if s.get("signal_type") == "entry"]
    exits = [s for s in signals if s.get("signal_type") == "exit"]

    if not entries and not exits:
        return DailySummaryData(date_str=date_str, total_entries=0, completed_trades=0, daily_pnl=daily_pnl)

    # Strategy distribution & quality distribution
    strategy_dist: dict[str, int] = {}
    quality_dist: dict[str, int] = {}
    for entry in entries:
        name = entry.get("strategy_name", "Unknown")
        strategy_dist[name] = strategy_dist.get(name, 0) + 1

        detail = _parse_detail(entry.get("detail"))
        grade = detail.get("quality_grade", "?")
        quality_dist[grade] = quality_dist.get(grade, 0) + 1

    # Build entry lookup: (strategy_id, symbol) -> list of entries (chronological)
    entry_lookup: dict[tuple[str, str], list[dict]] = {}
    for entry in entries:
        key = (entry["strategy_id"], entry["symbol"])
        entry_lookup.setdefault(key, []).append(entry)

    # Match exits to entries
    trades: list[TradeRecord] = []
    for exit_sig in exits:
        exit_detail = _parse_detail(exit_sig.get("detail"))
        entry_price = exit_detail.get("entry_price")
        exit_price = exit_detail.get("exit_price")
        direction = exit_detail.get("direction", "call")
        exit_reason = exit_detail.get("reason", "unknown")

        if entry_price is None or exit_price is None or entry_price <= 0:
            continue

        # Calculate PnL with direction correction
        raw_pnl = (exit_price - entry_price) / entry_price * 100
        pnl_pct = raw_pnl if direction == "call" else -raw_pnl

        # Find matching entry for metadata
        key = (exit_sig["strategy_id"], exit_sig["symbol"])
        matched_entries = entry_lookup.get(key, [])
        quality_grade = "?"
        entry_time = ""
        if matched_entries:
            matched = matched_entries[0]  # earliest unmatched entry
            matched_detail = _parse_detail(matched.get("detail"))
            quality_grade = matched_detail.get("quality_grade", "?")
            entry_ts = matched.get("timestamp", 0)
            if entry_ts:
                entry_time = datetime.fromtimestamp(entry_ts, tz=ET).strftime("%H:%M")
            # Consume the matched entry
            matched_entries.pop(0)

        trades.append(TradeRecord(
            strategy_name=exit_sig.get("strategy_name", "Unknown"),
            symbol=exit_sig["symbol"],
            entry_price=entry_price,
            exit_price=exit_price,
            pnl_pct=round(pnl_pct, 2),
            direction=direction,
            exit_reason=exit_reason,
            entry_time=entry_time,
            quality_grade=quality_grade,
        ))

    # Sort trades by PnL descending
    trades.sort(key=lambda t: t.pnl_pct, reverse=True)

    return DailySummaryData(
        date_str=date_str,
        total_entries=len(entries),
        completed_trades=len(trades),
        trades=trades,
        daily_pnl=daily_pnl,
        strategy_dist=strategy_dist,
        quality_dist=quality_dist,
    )


def format_daily_summary(data: DailySummaryData) -> str:
    """Format DailySummaryData into Telegram HTML message."""
    now_et = datetime.now(ET).strftime("%H:%M")

    if data.total_entries == 0 and data.completed_trades == 0:
        return (
            f"\U0001f4ed <b>Daily Summary</b> | {data.date_str}\n"
            f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
            f"\u274c \u4eca\u65e5\u65e0\u4ea4\u6613\u6d3b\u52a8\n\n"
            f"\u23f1 {now_et} ET"
        )

    pnl_sign = "+" if data.daily_pnl >= 0 else ""
    pnl_emoji = "\U0001f4b0" if data.daily_pnl >= 0 else "\U0001f4a8"

    lines = [
        f"\U0001f4ca <b>Daily Summary</b> | {data.date_str}",
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501",
        "",
        f"\U0001f4cb <b>\u4fe1\u53f7\u7edf\u8ba1</b>",
        f"  \u5165\u573a\u4fe1\u53f7: {data.total_entries} | \u5b8c\u6210\u4ea4\u6613: {data.completed_trades}",
        f"  {pnl_emoji} \u65e5P&L: {pnl_sign}{data.daily_pnl:.1f}%",
    ]

    # Quality distribution
    if data.quality_dist:
        grade_parts = []
        for grade in ["A", "B", "C", "D"]:
            count = data.quality_dist.get(grade, 0)
            if count > 0:
                emoji = {"A": "\U0001f7e2", "B": "\U0001f7e1", "C": "\U0001f7e0", "D": "\U0001f534"}.get(grade, "")
                grade_parts.append(f"{emoji}{grade}:{count}")
        if grade_parts:
            lines.append(f"  \U0001f3af \u8d28\u91cf: {' '.join(grade_parts)}")

    # Strategy distribution
    if data.strategy_dist:
        lines.append("")
        lines.append(f"\U0001f4ca <b>\u7b56\u7565\u5206\u5e03</b>")
        for name, count in sorted(data.strategy_dist.items(), key=lambda x: -x[1]):
            lines.append(f"  {name}: {count}")

    # Best / worst trades (top 3 each)
    if data.trades:
        best = [t for t in data.trades if t.pnl_pct >= 0][:3]
        worst = [t for t in reversed(data.trades) if t.pnl_pct < 0][:3]

        if best:
            lines.append("")
            lines.append(f"\U0001f4c8 <b>\u6700\u4f18\u4ea4\u6613</b>")
            for t in best:
                lines.append(_format_trade_line(t))

        if worst:
            lines.append("")
            lines.append(f"\U0001f4c9 <b>\u6700\u5dee\u4ea4\u6613</b>")
            for t in worst:
                lines.append(_format_trade_line(t))

    lines.append("")
    lines.append(f"\u23f1 {now_et} ET")
    return "\n".join(lines)


def _format_trade_line(t: TradeRecord) -> str:
    """Format a single trade record as a compact line."""
    pnl_emoji = "\U0001f7e2" if t.pnl_pct >= 0 else "\U0001f534"
    pnl_sign = "+" if t.pnl_pct >= 0 else ""
    grade_emoji = {"A": "\U0001f7e2", "B": "\U0001f7e1", "C": "\U0001f7e0", "D": "\U0001f534"}.get(t.quality_grade, "\u2b1c")
    reason_cn = _exit_reason_cn(t.exit_reason)
    return (
        f"  {pnl_emoji} {t.symbol} | {t.strategy_name} | {grade_emoji}{t.quality_grade}\n"
        f"  ${t.entry_price:.2f} \u2192 ${t.exit_price:.2f} ({pnl_sign}{t.pnl_pct:.2f}%) | {reason_cn}"
    )


def _exit_reason_cn(reason: str) -> str:
    """Translate exit reason to concise Chinese label."""
    mapping = {
        "take_profit": "\u6b62\u76c8",
        "stop_loss": "\u6b62\u635f",
        "trailing_stop": "\u8ddf\u8e2a\u6b62\u635f",
        "time_exit": "\u5230\u671f\u51fa\u573a",
        "eod_exit": "\u5c3e\u76d8\u51fa\u573a",
    }
    return mapping.get(reason, reason)


def _parse_detail(detail) -> dict:
    """Parse signal detail field — may be JSON string or dict."""
    if isinstance(detail, dict):
        return detail
    if isinstance(detail, str) and detail:
        try:
            return json.loads(detail)
        except (json.JSONDecodeError, ValueError):
            pass
    return {}
