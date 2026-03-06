# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Options Intraday Trading Monitor — a real-time async Python system that monitors stock prices, calculates technical indicators, evaluates YAML-defined strategy rules, and sends Telegram notifications for entry/exit signals.

## Commands

```bash
# Run the monitor
python -m src.main

# Run all tests
pytest tests/ -v

# Run a single test file
pytest tests/test_indicator.py -v

# Run a specific test
pytest tests/test_indicator.py::TestIndicatorEngine::test_rsi_calculation -v

# Docker
docker compose up --build
```

## Architecture

**Pipeline flow:** Collector → IndicatorEngine → RuleMatcher → StateManager → TelegramNotifier → SQLiteStore

- **`src/main.py`** — `OptionsMonitor` orchestrator. Runs three APScheduler polling jobs: stock quotes (10s), option chains (30s), 1-minute bars (60s). All yfinance calls run in a 4-worker thread pool.
- **`src/collector/`** — `BaseCollector` ABC with `YahooCollector` implementation. Returns `StockQuote`, `OptionQuote`, and bar DataFrames.
- **`src/indicator/engine.py`** — `IndicatorEngine` manages per-symbol bar data across timeframes (1m, 5m, 15m via resampling). Calculates RSI, MACD, EMA (9/21/50/200), ATR, VWAP, Bollinger Bands. Returns `IndicatorResult` dicts.
- **`src/strategy/loader.py`** — `StrategyLoader` reads YAML from `config/strategies/`. Uses watchdog `PollingObserver` for hot-reload (3s interval).
- **`src/strategy/matcher.py`** — `RuleMatcher` evaluates entry/exit rules against indicators. Supports comparators (`>`, `<`, `crosses_above`, `within_pct_of`), nested AND/OR groups, and entry quality scoring (0-100, grades A-D).
- **`src/strategy/state.py`** — `StrategyStateManager` tracks per-(strategy, symbol) state: WATCHING → ENTRY_TRIGGERED → HOLDING → EXIT_TRIGGERED → WATCHING. 5-minute timeout on unconfirmed entries.
- **`src/notification/telegram.py`** — `TelegramNotifier` sends formatted signals and handles `/confirm`, `/skip` commands.
- **`src/store/`** — `RedisStore` (async, caching/pubsub) and `SQLiteStore` (persistent signal history).

## Strategy YAML Format

Strategies live in `config/strategies/` and are hot-reloaded. Required fields: `strategy_id`, `name`, `enabled`, `watchlist`, `entry_conditions`. Entry/exit conditions use nested rule trees with `operator: AND/OR`.

## Key Conventions

- Python 3.11, async throughout (asyncio + APScheduler)
- Dependencies in `requirements.txt` (no pyproject.toml)
- Config in `config/settings.yaml`, secrets via `.env` (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
- Tests use pytest with synthetic bar data helpers
