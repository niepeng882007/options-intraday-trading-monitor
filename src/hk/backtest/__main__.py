"""CLI entry point for HK backtest.

Usage:
    python -m src.hk.backtest -d 20                          # 20 days, full watchlist
    python -m src.hk.backtest -y HK.800000 -d 30 --no-sim   # HSI only, no simulation
    python -m src.hk.backtest -o json -v                     # JSON output, verbose
    python -m src.hk.backtest --slippage 0.05 --exit-mode trailing
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from src.hk.backtest.data_loader import HKDataLoader
from src.hk.backtest.engine import HKBacktestEngine
from src.hk.backtest.report import format_csv, format_json, format_report
from src.utils.logger import setup_logger

logger = setup_logger("hk_backtest_cli")


def _load_hk_settings(path: str = "config/hk_settings.yaml") -> dict:
    p = Path(path)
    if not p.exists():
        logger.warning("HK settings not found: %s, using defaults", path)
        return {}
    with open(p) as f:
        return yaml.safe_load(f) or {}


def _get_default_symbols(settings: dict) -> list[str]:
    """Extract all symbols from HK watchlist config."""
    symbols = []
    wl = settings.get("watchlist", {})
    for group in ("indices", "stocks"):
        for item in wl.get(group, []):
            symbols.append(item["symbol"])
    return symbols


def main() -> None:
    parser = argparse.ArgumentParser(
        description="HK Predict Backtest — validate VP levels and regime classification"
    )
    parser.add_argument(
        "-y", "--symbol",
        help="Comma-separated symbols (default: all from hk_settings.yaml)",
    )
    parser.add_argument(
        "-d", "--days", type=int, default=20,
        help="Trading days to backtest (default: 20)",
    )
    parser.add_argument(
        "--no-sim", action="store_true",
        help="Skip trade simulation (sections 1+2 only)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Show detailed day-by-day log and trade log",
    )
    parser.add_argument(
        "-o", "--output", choices=["table", "csv", "json"], default="table",
        help="Output format (default: table)",
    )
    parser.add_argument("--futu-host", help="FutuOpenD host")
    parser.add_argument("--futu-port", type=int, help="FutuOpenD port")
    parser.add_argument(
        "--vp-lookback", type=int, default=None,
        help="Volume Profile lookback days (default: from config)",
    )
    parser.add_argument(
        "--rvol-lookback", type=int, default=None,
        help="RVOL lookback days (default: from config)",
    )
    parser.add_argument(
        "--tp", type=float, default=None,
        help="Take profit %% for simulation (default: 0.8%%)",
    )
    parser.add_argument(
        "--sl", type=float, default=None,
        help="Stop loss %% for simulation (default: 0.3%%)",
    )
    parser.add_argument(
        "--slippage", type=float, default=None,
        help="Slippage per leg %% (default: 0.05%%)",
    )
    parser.add_argument(
        "--exclude", type=str, default=None,
        help="Comma-separated symbols to exclude from simulation",
    )
    parser.add_argument(
        "--afternoon-levels", action="store_true",
        help="Include afternoon level signals (default: morning only)",
    )
    parser.add_argument(
        "--skip-signals", type=str, default=None,
        help="Comma-separated signal types to skip (e.g. BREAKOUT_long)",
    )
    parser.add_argument(
        "--breakout-rvol", type=float, default=None,
        help="RVOL threshold for BREAKOUT regime (default: from config)",
    )
    parser.add_argument(
        "--range-rvol", type=float, default=None,
        help="RVOL threshold for RANGE regime (default: from config)",
    )
    parser.add_argument(
        "--exit-mode", choices=["fixed", "trailing", "both"], default=None,
        help="Exit mode: fixed (TP/SL), trailing, or both (default: from config)",
    )
    parser.add_argument(
        "--trail-activation", type=float, default=None,
        help="Trailing stop activation %% (default: 0.5%%)",
    )
    parser.add_argument(
        "--trail-pct", type=float, default=None,
        help="Trailing stop drawdown %% (default: 0.3%%)",
    )

    args = parser.parse_args()

    # Load config
    settings = _load_hk_settings()
    futu_cfg = settings.get("futu", {})
    vp_cfg = settings.get("volume_profile", {})
    rvol_cfg = settings.get("rvol", {})
    regime_cfg = settings.get("regime", {})
    sim_cfg = settings.get("simulation", {})

    # Resolve symbols
    if args.symbol:
        symbols = [s.strip() for s in args.symbol.split(",")]
    else:
        symbols = _get_default_symbols(settings)

    if not symbols:
        print("No symbols to backtest. Check config or use -y flag.")
        sys.exit(1)

    # Resolve connection params
    futu_host = args.futu_host or futu_cfg.get("host", "127.0.0.1")
    futu_port = args.futu_port or futu_cfg.get("port", 11111)

    # Resolve backtest params
    vp_lookback = args.vp_lookback or vp_cfg.get("lookback_days", 5)
    rvol_lookback = args.rvol_lookback or rvol_cfg.get("lookback_days", 10)

    # Resolve simulation params (CLI > config > defaults)
    tp = (args.tp / 100) if args.tp else sim_cfg.get("tp_pct", 0.8) / 100
    sl = (args.sl / 100) if args.sl else sim_cfg.get("sl_pct", 0.3) / 100
    slippage = (args.slippage / 100) if args.slippage else sim_cfg.get("slippage_per_leg", 0.05) / 100

    # Resolve regime thresholds
    breakout_rvol = args.breakout_rvol or regime_cfg.get("breakout_rvol", 1.05)
    range_rvol = args.range_rvol or regime_cfg.get("range_rvol", 0.95)

    # Resolve exclude symbols
    if args.exclude:
        exclude_symbols = set(s.strip() for s in args.exclude.split(","))
    else:
        exclude_symbols = set(sim_cfg.get("exclude_symbols", []))

    # Resolve skip signal types
    if args.skip_signals:
        skip_signal_types = set(s.strip() for s in args.skip_signals.split(","))
    else:
        skip_signal_types = set(sim_cfg.get("skip_signal_types", []))

    # Resolve exit mode
    exit_mode = args.exit_mode or sim_cfg.get("exit_mode", "trailing")
    trail_act = (args.trail_activation / 100) if args.trail_activation else sim_cfg.get("trailing_activation_pct", 0.5) / 100
    trail_pct = (args.trail_pct / 100) if args.trail_pct else sim_cfg.get("trailing_trail_pct", 0.3) / 100

    morning_only = not args.afternoon_levels

    # Total days to request = backtest days + max(vp_lookback, rvol_lookback)
    extra_days = max(vp_lookback, rvol_lookback)
    load_days = args.days + extra_days

    print(f"Symbols: {', '.join(symbols)}")
    print(f"Backtest days: {args.days} (loading {load_days} to cover lookback)")
    print(f"VP lookback: {vp_lookback}d, RVOL lookback: {rvol_lookback}d")
    print(f"Regime thresholds: breakout_rvol={breakout_rvol}, range_rvol={range_rvol}")
    if not args.no_sim:
        print(f"Simulation: TP={tp*100:.1f}%, SL={sl*100:.1f}%, Slippage={slippage*100:.2f}%/leg")
        print(f"Exit mode: {exit_mode}", end="")
        if exit_mode in ("trailing", "both"):
            print(f" (activation={trail_act*100:.1f}%, trail={trail_pct*100:.1f}%)", end="")
        print()
        if exclude_symbols:
            print(f"Excluded: {', '.join(sorted(exclude_symbols))}")
        if skip_signal_types:
            print(f"Skipped signals: {', '.join(sorted(skip_signal_types))}")
        if morning_only:
            print("Level signals: morning only")
    print()

    # Load data
    try:
        with HKDataLoader(futu_host=futu_host, futu_port=futu_port) as loader:
            bars = loader.load_all(symbols, days=load_days)
    except ConnectionError as e:
        print(f"Error: {e}")
        sys.exit(1)

    if not bars:
        print("Failed to load market data.")
        sys.exit(1)

    # Build engine
    engine = HKBacktestEngine(
        vp_lookback_days=vp_lookback,
        rvol_lookback_days=rvol_lookback,
        bounce_thresholds=[0.003, 0.005, 0.007, 0.010],
        value_area_pct=vp_cfg.get("value_area_pct", 0.70),
        breakout_rvol=breakout_rvol,
        range_rvol=range_rvol,
        run_sim=not args.no_sim,
        tp_pct=tp,
        sl_pct=sl,
        slippage_per_leg=slippage,
        exclude_symbols=exclude_symbols,
        morning_only_levels=morning_only,
        skip_signal_types=skip_signal_types,
        exit_mode=exit_mode,
        trailing_activation_pct=trail_act,
        trailing_trail_pct=trail_pct,
    )
    result = engine.run(bars)

    # Build period string
    all_dates = set()
    for df in bars.values():
        all_dates.update(df.index.date)
    sorted_dates = sorted(all_dates)
    period = f"{sorted_dates[0]} -> {sorted_dates[-1]} ({len(sorted_dates)} days)" if sorted_dates else ""

    # Output
    if args.output == "csv":
        print(format_csv(result))
    elif args.output == "json":
        print(format_json(result))
    else:
        print(format_report(result, period=period, verbose=args.verbose))


if __name__ == "__main__":
    main()
