"""Report generation for HK backtest results."""

from __future__ import annotations

import csv
import io
import json

from src.hk.backtest import HKBacktestResult, LevelEvalResult, RegimeEvalResult, SimResult


def format_report(
    result: HKBacktestResult,
    title: str = "HK Backtest Report",
    period: str = "",
    verbose: bool = False,
) -> str:
    """Generate a formatted text report."""
    lines: list[str] = []
    sep = "=" * 64

    lines.append(sep)
    lines.append(f"  {title}")
    if period:
        lines.append(f"  Period: {period}")
    lines.append(f"  Symbols: {', '.join(result.symbols)}")
    lines.append(f"  Data: {result.data_bars} bars across {result.days} trading days")
    lines.append(sep)

    if result.level_eval:
        lines.append("")
        lines.extend(_format_level_section(result.level_eval, verbose))

    if result.regime_eval:
        lines.append("")
        lines.extend(_format_regime_section(result.regime_eval, verbose))

    if result.sim_result:
        lines.append("")
        lines.extend(_format_sim_section(result.sim_result, verbose))

    lines.append(sep)
    return "\n".join(lines)


def _format_level_section(level: LevelEvalResult, verbose: bool) -> list[str]:
    """Format Section 1: Level Signal Accuracy."""
    lines = [
        "  Section 1: Level Signal Accuracy",
        "  " + "-" * 60,
    ]

    if not level.by_threshold:
        lines.append("  No level events found.")
        return lines

    # Header
    lines.append(
        f"  {'Threshold':<10} {'VAH Touch':>10} {'VAH Bounce':>11} "
        f"{'VAL Touch':>10} {'VAL Bounce':>11} {'Total Rate':>10}"
    )

    for threshold in sorted(level.by_threshold.keys()):
        stats = level.by_threshold[threshold]
        vah_t = stats["vah_touches"]
        vah_b = stats["vah_bounces"]
        val_t = stats["val_touches"]
        val_b = stats["val_bounces"]
        total_t = vah_t + val_t
        total_b = vah_b + val_b
        vah_rate = f"{vah_b/vah_t*100:.1f}%" if vah_t else "N/A"
        val_rate = f"{val_b/val_t*100:.1f}%" if val_t else "N/A"
        total_rate = f"{total_b/total_t*100:.1f}%" if total_t else "N/A"

        lines.append(
            f"  {threshold*100:.1f}%      {vah_t:>10} {vah_rate:>11} "
            f"{val_t:>10} {val_rate:>11} {total_rate:>10}"
        )

    # By session breakdown
    if level.by_session:
        lines.append("")
        lines.append("  By Session:")
        mid_threshold = sorted(level.by_threshold.keys())[len(level.by_threshold) // 2]

        for sess in sorted(level.by_session.keys()):
            sess_data = level.by_session[sess]
            if mid_threshold in sess_data:
                s = sess_data[mid_threshold]
                rate = f"{s['bounces']/s['touches']*100:.1f}%" if s["touches"] else "N/A"
                lines.append(
                    f"    {sess:<12} {s['touches']}T  {rate} bounce rate "
                    f"(@ {mid_threshold*100:.1f}% threshold)"
                )

    # By symbol breakdown
    if verbose and level.by_symbol:
        lines.append("")
        lines.append("  By Symbol:")
        mid_threshold = sorted(level.by_threshold.keys())[len(level.by_threshold) // 2]

        for sym in sorted(level.by_symbol.keys()):
            sym_data = level.by_symbol[sym]
            if mid_threshold in sym_data:
                s = sym_data[mid_threshold]
                rate = f"{s['bounces']/s['touches']*100:.1f}%" if s["touches"] else "N/A"
                lines.append(
                    f"    {sym:<16} {s['touches']}T  {rate} (@ {mid_threshold*100:.1f}%)"
                )

    return lines


def _format_regime_section(regime: RegimeEvalResult, verbose: bool) -> list[str]:
    """Format Section 2: Regime Classification Accuracy."""
    lines = [
        "  Section 2: Regime Classification Accuracy",
        "  " + "-" * 60,
    ]

    if not regime.by_regime:
        lines.append("  No regime evaluations found.")
        return lines

    lines.append(f"  {'Regime':<12} {'Days':>6} {'Accurate':>9} {'Rate':>8}")

    for regime_type in sorted(regime.by_regime.keys()):
        stats = regime.by_regime[regime_type]
        total = stats["total"]
        accurate = stats["accurate"]
        rate = f"{accurate/total*100:.1f}%" if total else "N/A"
        lines.append(
            f"  {regime_type:<12} {total:>6} {accurate:>9} {rate:>8}"
        )

    # By symbol
    if verbose and regime.by_symbol:
        lines.append("")
        lines.append("  By Symbol:")
        for sym in sorted(regime.by_symbol.keys()):
            stats = regime.by_symbol[sym]
            total = stats["total"]
            accurate = stats["accurate"]
            rate = f"{accurate/total*100:.1f}%" if total else "N/A"
            lines.append(f"    {sym:<16} {total}D  {rate} accuracy")

    # Detailed day log
    if verbose and regime.days:
        lines.append("")
        lines.append("  Day-by-Day Log:")
        lines.append(
            f"  {'Date':<12} {'Symbol':<14} {'Regime':<10} {'Conf':>5} "
            f"{'RVOL':>5} {'O':>8} {'H':>8} {'L':>8} {'C':>8} {'OK?':>4}"
        )
        for d in regime.days:
            date_str = str(d.date)[:10]
            ok = "Y" if d.accurate else "N"
            if d.predicted.value in ("whipsaw", "unclear"):
                ok = "-"
            lines.append(
                f"  {date_str:<12} {d.symbol:<14} {d.predicted.value:<10} "
                f"{d.confidence:>5.2f} {d.rvol:>5.2f} "
                f"{d.day_open:>8.0f} {d.day_high:>8.0f} "
                f"{d.day_low:>8.0f} {d.day_close:>8.0f} {ok:>4}"
            )

    return lines


def _format_sim_section(sim: SimResult, verbose: bool) -> list[str]:
    """Format Section 3: Trade Simulation."""
    lines = [
        "  Section 3: Trade Simulation",
        "  " + "-" * 60,
    ]

    if sim.total_trades == 0:
        lines.append("  No simulated trades.")
        return lines

    lines.append(f"  {'Total Trades':<22} {sim.total_trades}")
    lines.append(
        f"  {'Win Rate':<22} {sim.win_rate:.1f}%  "
        f"({sim.winning_trades}W / {sim.losing_trades}L)"
    )
    pf = f"{sim.profit_factor:.2f}" if sim.profit_factor != float("inf") else "inf"
    lines.append(f"  {'Profit Factor':<22} {pf}")
    lines.append(f"  {'Net Return':<22} {sim.total_return_pct:+.2f}% (stock)")
    lines.append(f"  {'Max Drawdown':<22} -{sim.max_drawdown_pct:.2f}%")
    lines.append(f"  {'Avg Win':<22} {sim.avg_win_pct:+.3f}%")
    lines.append(f"  {'Avg Loss':<22} {sim.avg_loss_pct:+.3f}%")
    lines.append(f"  {'Expectancy':<22} {sim.expectancy_pct:+.3f}%/trade")

    # By signal type
    if sim.by_signal_type:
        lines.append("")
        lines.append("  By Signal Type:")
        for sig, stats in sorted(sim.by_signal_type.items()):
            lines.append(
                f"    {sig:<18} {stats['trades']:.0f}T  "
                f"{stats['win_rate']:.0f}%WR  {stats['total_pnl']:+.2f}%"
            )

    # By symbol
    if len(sim.by_symbol) > 1:
        lines.append("")
        lines.append("  By Symbol:")
        for sym, stats in sorted(sim.by_symbol.items()):
            lines.append(
                f"    {sym:<16} {stats['trades']:.0f}T  "
                f"{stats['win_rate']:.0f}%WR  {stats['total_pnl']:+.2f}%"
            )

    # Trade log
    if verbose and sim.trades:
        lines.append("")
        lines.append("  Trade Log:")
        lines.append(
            f"  {'#':<3} {'Symbol':<14} {'Signal':<18} "
            f"{'Entry':>8} {'Exit':>8} {'Net%':>8} {'Peak%':>7} {'Reason'}"
        )
        for i, t in enumerate(sim.trades, 1):
            peak_str = f"{t.peak_pnl_pct:>+6.2f}%" if t.peak_pnl_pct else "     - "
            lines.append(
                f"  {i:<3} {t.symbol:<14} {t.signal_type:<18} "
                f"{t.entry_price:>8.1f} {t.exit_price:>8.1f} "
                f"{t.net_pnl_pct:>+7.3f}% {peak_str} {t.exit_reason}"
            )

    return lines


def format_csv(result: HKBacktestResult) -> str:
    """Export simulation trades as CSV."""
    output = io.StringIO()
    writer = csv.writer(output)

    if result.sim_result and result.sim_result.trades:
        writer.writerow([
            "trade_num", "symbol", "signal_type",
            "entry_time", "exit_time", "entry_price", "exit_price",
            "stock_pnl_pct", "net_pnl_pct", "leveraged_pnl_pct",
            "peak_pnl_pct", "exit_reason", "session",
        ])
        for i, t in enumerate(result.sim_result.trades, 1):
            writer.writerow([
                i, t.symbol, t.signal_type,
                str(t.entry_time), str(t.exit_time) if t.exit_time else "",
                f"{t.entry_price:.2f}", f"{t.exit_price:.2f}",
                f"{t.stock_pnl_pct:.4f}", f"{t.net_pnl_pct:.4f}",
                f"{t.leveraged_pnl_pct:.4f}", f"{t.peak_pnl_pct:.4f}",
                t.exit_reason, t.session,
            ])

    return output.getvalue()


def format_json(result: HKBacktestResult) -> str:
    """Export full results as JSON."""
    data: dict = {
        "meta": {
            "symbols": result.symbols,
            "days": result.days,
            "data_bars": result.data_bars,
        },
    }

    if result.level_eval:
        le = result.level_eval
        data["level_evaluation"] = {
            "total_events": len(le.events),
            "by_threshold": {
                f"{k*100:.1f}%": v for k, v in le.by_threshold.items()
            },
            "by_session": {
                sess: {f"{k*100:.1f}%": v for k, v in thresholds.items()}
                for sess, thresholds in le.by_session.items()
            },
        }

    if result.regime_eval:
        re = result.regime_eval
        data["regime_evaluation"] = {
            "total_days": len(re.days),
            "by_regime": re.by_regime,
            "by_symbol": re.by_symbol,
        }

    if result.sim_result:
        sim = result.sim_result
        data["simulation"] = {
            "summary": {
                "total_trades": sim.total_trades,
                "win_rate": sim.win_rate,
                "profit_factor": sim.profit_factor,
                "total_return_pct": sim.total_return_pct,
                "max_drawdown_pct": sim.max_drawdown_pct,
                "expectancy_pct": sim.expectancy_pct,
            },
            "by_signal_type": sim.by_signal_type,
            "by_symbol": sim.by_symbol,
            "trades": [
                {
                    "symbol": t.symbol,
                    "signal_type": t.signal_type,
                    "entry_price": t.entry_price,
                    "entry_time": str(t.entry_time),
                    "exit_price": t.exit_price,
                    "exit_time": str(t.exit_time) if t.exit_time else None,
                    "stock_pnl_pct": t.stock_pnl_pct,
                    "net_pnl_pct": t.net_pnl_pct,
                    "leveraged_pnl_pct": t.leveraged_pnl_pct,
                    "exit_reason": t.exit_reason,
                    "session": t.session,
                    "peak_pnl_pct": t.peak_pnl_pct,
                }
                for t in sim.trades
            ],
        }

    return json.dumps(data, indent=2, ensure_ascii=False)
