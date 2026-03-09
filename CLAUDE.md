# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Options Intraday Trading Monitor — a real-time async Python system that monitors stock prices, calculates technical indicators, evaluates YAML-defined strategy rules, and sends Telegram notifications for entry/exit signals.

## Commands

```bash
# Run the US monitor
python -m src.main

# Run the HK predictor (standalone)
python -m src.hk

# Run all tests
pytest tests/ -v

# Run a single test file
pytest tests/test_indicator.py -v
pytest tests/test_hk.py -v

# Run a specific test
pytest tests/test_indicator.py::TestIndicatorEngine::test_rsi_calculation -v

# Run backtest (US)
python -m src.backtest

# Run HK backtest (VP levels + regime accuracy + trade simulation)
python -m src.hk.backtest -d 20
python -m src.hk.backtest -y HK.800000 -d 30 --no-sim
python -m src.hk.backtest -o json -v
python -m src.hk.backtest -d 30 --exclude HK.800000 HK.00941
python -m src.hk.backtest -d 30 --exit-mode trailing --trail-activation 0.5 --trail-pct 0.3
python -m src.hk.backtest -d 30 --slippage 0.05 --breakout-rvol 1.05 --range-rvol 0.95

# HK data probe (verify Futu API for HK market)
python scripts/hk_data_probe.py

# Docker
docker compose up --build
```

## Architecture

**Pipeline flow:** Collector → IndicatorEngine → RuleMatcher → StateManager → TelegramNotifier → SQLiteStore

- **`src/main.py`** — `OptionsMonitor` orchestrator. With Futu push mode: subscribes to QUOTE + K_1M push for real-time quotes and 1-minute bars. Fallback: APScheduler polling for stock quotes (10s). History bars polled every 300s. Includes health check (60s) and heartbeat (300s) jobs. Data source configured via `data_source` in `config/settings.yaml`.
- **`src/collector/`** — `BaseCollector` ABC with `FutuCollector` (primary) and `YahooCollector` (fallback). `FutuCollector` supports real-time K-line push (`subscribe_kline`), quote caching, health check, connection info, call timeout (30s) with thread pool reset, and extended `StockQuote` fields (OHLC, change_pct, turnover, amplitude). Returns `StockQuote`, `OptionQuote`, and bar DataFrames.
- **`src/indicator/engine.py`** — `IndicatorEngine` manages per-symbol bar data across timeframes (1m, 5m, 15m via resampling). Calculates RSI, MACD, EMA (9/21/50/200), ATR, VWAP, Bollinger Bands (upper/lower/middle/%B/width_expansion), ADX, Stochastic (%K/%D). Also computes candle shadow metrics (upper/lower_shadow_pct) and prev_bar fields. Returns `IndicatorResult` dicts.
- **`src/strategy/loader.py`** — `StrategyLoader` reads YAML from `config/strategies/`. Uses watchdog `PollingObserver` for hot-reload (3s interval).
- **`src/strategy/matcher.py`** — `RuleMatcher` evaluates entry/exit rules against indicators. Supports comparators (`>`, `<`, `crosses_above`, `within_pct_of`), nested AND/OR/MIN_MATCH groups, entry quality scoring (0-100, grades A-D with configurable `base_score`), `confirm_bars` multi-bar confirmation, `min_magnitude` threshold, `market_context_filters` (`max_adx` for left-side, `min_adx` for right-side, with 20-30 overlap zone), and `reference_field` for indicator-vs-indicator comparison. Exit types: `take_profit_pct`, `stop_loss_pct`, `trailing_stop` (PUT-aware, tracks lowest_price), `indicator_target` (dynamic exit on indicator value), `time_exit`. Quality scoring includes %B extremity, ADX environment, and reversal strength modules.
- **`src/strategy/state.py`** — `StrategyStateManager` tracks per-(strategy, symbol) state: WATCHING → ENTRY_TRIGGERED → HOLDING → EXIT_TRIGGERED → WATCHING. 5-minute timeout on unconfirmed entries. `PositionInfo` tracks both `highest_price` and `lowest_price` for directional trailing stops.
- **`src/notification/telegram.py`** — `TelegramNotifier` sends formatted signals. Bot commands: `/status`, `/market` (all symbols OHLC), `/chain`, `/strategies`, `/enable`, `/disable`, `/reload`, `/confirm`, `/skip`, `/detail`, `/test`, `/conn` (Futu diagnostics). Supports concurrent updates and command timeouts.
- **`src/store/`** — `RedisStore` (async, caching/pubsub) and `SQLiteStore` (persistent signal history).
- **`src/backtest/`** — Backtesting framework with intra-bar simulation (partial candle effect via O/H/L price probes), PUT-direction exit logic, per-strategy cooldown tracking, and configurable midday no-trade window / daily loss circuit breaker from `settings.yaml`. Run via `python -m src.backtest`.

### HK Predict Module (`src/hk/`)

Independent HK market prediction system, fully decoupled from the US pipeline. Generates daily Playbooks with regime classification and key levels for HK index option trading.

- **`src/hk/main.py`** — `HKPredictor` orchestrator. APScheduler drives three daily pushes: 09:35 (morning), 10:05 (confirm), 13:05 (afternoon) HKT. Order book monitoring runs every 60s during trading hours.
- **`src/hk/collector.py`** — `HKCollector` sync Futu wrapper for HK market. Uses `get_market_snapshot` for bid/ask (not available in `get_stock_quote` for HK). `get_option_chain_with_oi()` fetches real OI from snapshot (fixes `option_area_type` bug). K-line timezone is HKT (Asia/Hong_Kong).
- **`src/hk/volume_profile.py`** — Calculates POC/VAH/VAL from 1m bars. Auto-detects tick size (50 for HSI, 1.0 for stocks ~100-1000, 0.5 for ~10-100). Uses volume distribution across H-L range.
- **`src/hk/indicators.py`** — VWAP (continuous across 12:00-13:00 lunch break) and RVOL (session-aware: morning 09:30-12:00, afternoon 13:00-16:00). Helper functions `get_today_bars()`/`get_history_bars()` for splitting multi-day DataFrames.
- **`src/hk/regime.py`** — Classifies market into 4 regimes: BREAKOUT (RVOL>breakout_rvol + outside VAH/VAL), RANGE (RVOL<range_rvol + inside value), WHIPSAW (IV spike + near Gamma wall), UNCLEAR (default). Thresholds configurable (default: breakout_rvol=1.05, range_rvol=0.95). Returns confidence score.
- **`src/hk/playbook.py`** — Generates `Playbook` with key levels and strategy text. Formats as Telegram HTML with regime emoji, confidence bar, key levels, strategy advice, and filter status.
- **`src/hk/filter.py`** — 5 trade filters: economic calendar (`config/hk_calendar.yaml`), Inside Day + ATR shrink, IV Rank + RVOL mismatch, minimum turnover (1亿 HKD), expiry-day risk.
- **`src/hk/orderbook.py`** — LV2 order book large order detection (volume > N× average across levels). Formats order book summary and alerts for Telegram.
- **`src/hk/gamma_wall.py`** — Calculates Call Wall, Put Wall (max OI strikes), and Max Pain from option chain. Index options only (HSI/HSTECH).
- **`src/hk/telegram.py`** — Bot commands: `/hk` (status), `/hk_playbook` (regenerate), `/hk_orderbook` (LV2 snapshot), `/hk_gamma` (Gamma Wall), `/hk_levels` (VP key levels), `/hk_regime` (regime classification), `/hk_quote` (detailed quote), `/hk_filters` (trade filter status), `/hk_watchlist` (all symbols overview), `/hk_help` (command reference).
- **`src/hk/backtest/`** — HK backtest framework validating VP levels and regime classification. `data_loader.py` fetches 1m bars from Futu with CSV caching. `evaluators.py` tests VAH/VAL bounce rates (multi-threshold) and regime accuracy, supports `exclude_symbols`. `simulator.py` simulates trades with 3 exit modes (fixed/trailing/both), configurable TP/SL/slippage, symbol exclusion, morning-only levels, signal type filtering, and peak P&L tracking. `engine.py` orchestrates the pipeline with config file fallback. `report.py` generates table/CSV/JSON reports with Peak% column. Run via `python -m src.hk.backtest`.

## Strategy YAML Format (US)

Strategies live in `config/strategies/` and are hot-reloaded. Required fields: `strategy_id`, `name`, `enabled`, `watchlist`, `entry_conditions`. Entry/exit conditions use nested rule trees with `operator: AND/OR/MIN_MATCH`. Rules can compare indicator values against thresholds or other indicator fields via `reference_field`. See `strategies.md` for full strategy documentation.

## HK Configuration

- `config/hk_settings.yaml` — HK watchlist (indices + stocks), regime thresholds (`breakout_rvol`, `range_rvol`), playbook push times, filter params, order book and gamma wall settings, `simulation` block (tp/sl/slippage, exit_mode, trailing params, exclude_symbols, skip_signal_types).
- `config/hk_calendar.yaml` — Economic calendar (FOMC, HKMA, China PMI/GDP, HK holidays, HSI option expiry dates). Manually maintained.

## Key Conventions

- Python 3.11, async throughout (asyncio + APScheduler)
- Dependencies in `requirements.txt` (no pyproject.toml)
- Config in `config/settings.yaml` (US) and `config/hk_settings.yaml` (HK), secrets via `.env` (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
- Tests use pytest with synthetic bar data helpers
- HK module is fully independent — own collector, indicators, and Telegram commands
- Futu API: HK uses `get_market_snapshot` for bid/ask, `option_open_interest` for OI (not `option_area_type`)
