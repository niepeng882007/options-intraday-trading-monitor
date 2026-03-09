from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from src.backtest.data_loader import DataLoader
from src.backtest.engine import BacktestEngine
from src.backtest.report import format_csv, format_json, format_report
from src.strategy.loader import StrategyLoader
from src.utils.logger import setup_logger

logger = setup_logger("backtest_cli")


def _load_settings(settings_path: str = "config/settings.yaml") -> dict:
    """Load full settings from settings.yaml."""
    path = Path(settings_path)
    if not path.exists():
        logger.warning("Settings file not found: %s, using defaults", settings_path)
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _load_risk_config(settings_path: str = "config/settings.yaml") -> dict:
    """Load risk management config from settings.yaml."""
    return _load_settings(settings_path).get("risk_management", {})


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backtest options intraday trading strategies"
    )
    parser.add_argument("-s", "--strategy", help="Strategy ID to backtest")
    parser.add_argument("--all", action="store_true", help="Backtest all active strategies")
    parser.add_argument("-y", "--symbol", help="Comma-separated symbols (default: strategy watchlist)")
    parser.add_argument("-d", "--days", type=int, default=5, help="Trading days (default: 5)")
    parser.add_argument("--start-date", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end-date", help="End date (YYYY-MM-DD)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print trade log")
    parser.add_argument("-o", "--output", choices=["table", "csv", "json"], default="table")
    parser.add_argument("--strategies-dir", default="config/strategies")
    parser.add_argument("--data-source", choices=["futu", "yahoo"], default=None,
                        help="Data source (default: from settings.yaml or futu)")
    parser.add_argument("--futu-host", help="FutuOpenD host (overrides settings.yaml)")
    parser.add_argument("--futu-port", type=int, help="FutuOpenD port (overrides settings.yaml)")

    args = parser.parse_args()

    if not args.strategy and not args.all:
        parser.error("Specify --strategy/-s or --all")

    # Load strategies
    loader = StrategyLoader(args.strategies_dir)
    loader.load_all()

    if args.all:
        strategies = loader.get_active()
    else:
        strat = loader.get(args.strategy)
        if strat is None:
            print(f"Strategy '{args.strategy}' not found. Available:")
            for s in loader.get_active():
                print(f"  - {s.strategy_id}: {s.name}")
            sys.exit(1)
        strategies = [strat]

    if not strategies:
        print("No active strategies found.")
        sys.exit(1)

    # Determine symbols
    if args.symbol:
        symbols = [s.strip().upper() for s in args.symbol.split(",")]
    else:
        symbols = sorted({sym for s in strategies for sym in s.underlyings})

    if not symbols:
        print("No symbols to backtest.")
        sys.exit(1)

    print(f"Strategies: {', '.join(s.strategy_id for s in strategies)}")
    print(f"Symbols: {', '.join(symbols)}")

    # Resolve data source and Futu config from settings.yaml + CLI overrides
    settings = _load_settings()
    futu_cfg = settings.get("futu", {})
    data_source = args.data_source or settings.get("data_source", "futu")
    futu_host = args.futu_host or futu_cfg.get("host", "127.0.0.1")
    futu_port = args.futu_port or futu_cfg.get("port", 11111)

    # Load data
    load_kwargs = {}
    if args.start_date and args.end_date:
        load_kwargs["start_date"] = args.start_date
        load_kwargs["end_date"] = args.end_date
    else:
        load_kwargs["days"] = args.days

    with DataLoader(
        data_source=data_source, futu_host=futu_host, futu_port=futu_port
    ) as data_loader:
        bars = data_loader.load_all(symbols, strategies=strategies, **load_kwargs)

    if not bars:
        print("Failed to load market data.")
        sys.exit(1)

    # Load risk management config from settings.yaml
    risk_cfg = settings.get("risk_management", {})
    midday_cfg = risk_cfg.get("midday_no_trade", {})
    midday_no_trade = midday_cfg.get("enabled", True)
    midday_start_str = midday_cfg.get("start", "11:00")
    midday_end_str = midday_cfg.get("end", "13:00")
    sh, sm = map(int, midday_start_str.split(":"))
    eh, em = map(int, midday_end_str.split(":"))
    max_daily_loss_pct = risk_cfg.get("max_daily_loss_pct", -1.5)

    # Run backtest
    engine = BacktestEngine(
        strategies,
        list(bars.keys()),
        midday_no_trade=midday_no_trade,
        midday_start=sh * 60 + sm,
        midday_end=eh * 60 + em,
        max_daily_loss_pct=max_daily_loss_pct,
    )
    result = engine.run(bars)

    # Build period string
    if args.start_date and args.end_date:
        period = f"{args.start_date} -> {args.end_date}"
    else:
        all_dates = set()
        for df in bars.values():
            all_dates.update(df.index.date)
        if all_dates:
            sorted_dates = sorted(all_dates)
            period = f"{sorted_dates[0]} -> {sorted_dates[-1]} ({len(sorted_dates)} days)"
        else:
            period = ""

    title = f"Backtest: {', '.join(s.strategy_id for s in strategies)} ({', '.join(symbols)})"

    # Output
    if args.output == "csv":
        print(format_csv(result))
    elif args.output == "json":
        print(format_json(result))
    else:
        print(format_report(result, title=title, period=period, verbose=args.verbose))


if __name__ == "__main__":
    main()
