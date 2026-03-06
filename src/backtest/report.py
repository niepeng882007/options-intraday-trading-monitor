from __future__ import annotations

import csv
import io
import json

from src.backtest.trade_tracker import BacktestResult, Trade


def format_report(
    result: BacktestResult,
    title: str = "Backtest Report",
    period: str = "",
    verbose: bool = False,
) -> str:
    lines: list[str] = []
    sep = "=" * 54

    lines.append(sep)
    lines.append(f"  {title}")
    if period:
        lines.append(f"  Period: {period}")
    lines.append(sep)
    lines.append("")

    lines.append(f"  {'Total Trades':<22} {result.total_trades}")
    if result.total_trades == 0:
        lines.append("  No trades generated.")
        lines.append(sep)
        return "\n".join(lines)

    lines.append(
        f"  {'Win Rate':<22} {result.win_rate:.1f}%  "
        f"({result.winning_trades}W / {result.losing_trades}L)"
    )
    pf = f"{result.profit_factor:.2f}" if result.profit_factor != float("inf") else "inf"
    lines.append(f"  {'Profit Factor':<22} {pf}")
    lines.append(f"  {'Total Return':<22} {result.total_return_pct:+.2f}%")
    lines.append(f"  {'Max Drawdown':<22} -{result.max_drawdown_pct:.2f}%")
    lines.append(f"  {'Avg Holding Time':<22} {result.avg_holding_minutes:.0f} min")
    lines.append(f"  {'Avg Win':<22} {result.avg_win_pct:+.2f}%")
    lines.append(f"  {'Avg Loss':<22} {result.avg_loss_pct:+.2f}%")
    lines.append(f"  {'Best Trade':<22} {result.best_trade_pct:+.2f}%")
    lines.append(f"  {'Worst Trade':<22} {result.worst_trade_pct:+.2f}%")
    lines.append(f"  {'Trades/Day':<22} {result.trades_per_day:.1f}")
    lines.append("")

    # By strategy breakdown
    if len(result.by_strategy) > 1:
        lines.append("  By Strategy:")
        for sid, stats in sorted(result.by_strategy.items()):
            lines.append(
                f"    {sid:<30} {stats['trades']}T  "
                f"{stats['win_rate']:.0f}%WR  {stats['total_pnl']:+.2f}%"
            )
        lines.append("")

    # By symbol breakdown
    if len(result.by_symbol) > 1:
        lines.append("  By Symbol:")
        for sym, stats in sorted(result.by_symbol.items()):
            lines.append(
                f"    {sym:<8} {stats['trades']}T  "
                f"{stats['win_rate']:.0f}%WR  {stats['total_pnl']:+.2f}%"
            )
        lines.append("")

    # Trade log
    if verbose and result.trades:
        lines.append("  Trade Log")
        lines.append(
            f"  {'#':<3} {'Date':<12} {'Entry':>9} {'Exit':>9} "
            f"{'P&L%':>8} {'Reason':<14} {'Quality'}"
        )
        lines.append("  " + "-" * 70)
        for i, t in enumerate(result.trades, 1):
            date_str = t.entry_time.strftime("%m/%d %H:%M")
            quality = f"{t.quality_grade}({t.quality_score})" if t.quality_grade else "-"
            lines.append(
                f"  {i:<3} {date_str:<12} ${t.entry_price:>8.2f} ${t.exit_price:>8.2f} "
                f"{t.direction_pnl_pct:>+7.2f}% {t.exit_reason:<14} {quality}"
            )
        lines.append("")

    lines.append(sep)
    return "\n".join(lines)


def format_csv(result: BacktestResult) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "trade_num", "strategy_id", "symbol", "direction",
        "entry_time", "exit_time", "entry_price", "exit_price",
        "stock_pnl_pct", "direction_pnl_pct", "exit_reason",
        "holding_min", "quality_score", "quality_grade",
    ])
    for i, t in enumerate(result.trades, 1):
        writer.writerow([
            i, t.strategy_id, t.symbol, t.direction,
            t.entry_time.isoformat(), t.exit_time.isoformat() if t.exit_time else "",
            f"{t.entry_price:.2f}", f"{t.exit_price:.2f}",
            f"{t.stock_pnl_pct:.4f}", f"{t.direction_pnl_pct:.4f}",
            t.exit_reason, f"{t.holding_minutes:.1f}",
            t.quality_score, t.quality_grade,
        ])
    return output.getvalue()


def format_json(result: BacktestResult) -> str:
    data = {
        "summary": {
            "total_trades": result.total_trades,
            "winning_trades": result.winning_trades,
            "losing_trades": result.losing_trades,
            "win_rate": result.win_rate,
            "profit_factor": result.profit_factor,
            "total_return_pct": result.total_return_pct,
            "max_drawdown_pct": result.max_drawdown_pct,
            "avg_holding_minutes": result.avg_holding_minutes,
            "avg_win_pct": result.avg_win_pct,
            "avg_loss_pct": result.avg_loss_pct,
            "best_trade_pct": result.best_trade_pct,
            "worst_trade_pct": result.worst_trade_pct,
            "trades_per_day": result.trades_per_day,
        },
        "by_strategy": result.by_strategy,
        "by_symbol": result.by_symbol,
        "trades": [
            {
                "strategy_id": t.strategy_id,
                "symbol": t.symbol,
                "direction": t.direction,
                "entry_time": t.entry_time.isoformat(),
                "exit_time": t.exit_time.isoformat() if t.exit_time else None,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "stock_pnl_pct": t.stock_pnl_pct,
                "direction_pnl_pct": t.direction_pnl_pct,
                "exit_reason": t.exit_reason,
                "holding_minutes": t.holding_minutes,
                "quality_score": t.quality_score,
                "quality_grade": t.quality_grade,
            }
            for t in result.trades
        ],
    }
    return json.dumps(data, indent=2, ensure_ascii=False)
