from __future__ import annotations

import argparse
import sys

from src.backtest.data_loader import DataLoader
from src.backtest.engine import BacktestEngine
from src.backtest.report import format_csv, format_json, format_report
from src.strategy.loader import StrategyLoader
from src.utils.logger import setup_logger

logger = setup_logger("backtest_cli")


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

    # Load data
    data_loader = DataLoader()
    load_kwargs = {}
    if args.start_date and args.end_date:
        load_kwargs["start_date"] = args.start_date
        load_kwargs["end_date"] = args.end_date
    else:
        load_kwargs["days"] = args.days

    bars = data_loader.load_all(symbols, strategies=strategies, **load_kwargs)
    if not bars:
        print("Failed to load market data.")
        sys.exit(1)

    # Run backtest
    engine = BacktestEngine(strategies, list(bars.keys()))
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
