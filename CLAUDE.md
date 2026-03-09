# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Options Intraday Trading Monitor — a real-time async Python system that monitors stock prices, calculates technical indicators, evaluates YAML-defined strategy rules, and sends Telegram notifications for entry/exit signals.

## Commands

```bash
python -m src.main              # Run the US monitor (includes US Playbook)
python -m src.hk                # Run the HK predictor (standalone)
python -m src.us_playbook       # Run the US Playbook (standalone)
pytest tests/ -v                # Run all tests
pytest tests/test_hk.py -v      # Run a single test file
pytest tests/test_us_playbook.py -v  # Run US Playbook tests
python -m src.backtest           # Run backtest (US)

# Run HK backtest
python -m src.hk.backtest -d 20
python -m src.hk.backtest -d 30 --exclude HK.800000 HK.00941 --exit-mode trailing -v

docker compose up --build       # Docker
```

## Architecture

**US Pipeline:** Collector → IndicatorEngine → RuleMatcher → StateManager → TelegramNotifier → SQLiteStore

- **`src/main.py`** — `OptionsMonitor` orchestrator. Futu push mode (QUOTE + K_1M) or APScheduler polling fallback. Data source configured via `config/settings.yaml`.
- **`src/collector/`** — `FutuCollector` (primary) and `YahooCollector` (fallback). Returns `StockQuote`, `OptionQuote`, and bar DataFrames.
- **`src/indicator/engine.py`** — Per-symbol bar data across timeframes (1m, 5m, 15m). Calculates RSI, MACD, EMA, ATR, VWAP, Bollinger Bands, ADX, Stochastic, candle shadow metrics.
- **`src/strategy/`** — `StrategyLoader` hot-reloads YAML from `config/strategies/`. `RuleMatcher` evaluates nested rule trees with quality scoring, `confirm_bars`, `market_context_filters` (ADX overlap zone 20-30). `StrategyStateManager` tracks WATCHING → ENTRY_TRIGGERED → HOLDING → EXIT_TRIGGERED cycle. PUT-aware trailing stop tracks `lowest_price`.
- **`src/notification/telegram.py`** — Telegram bot with signal notifications and interactive commands (`/confirm`, `/skip`, `/status`, etc.).
- **`src/store/`** — `RedisStore` (caching/pubsub) and `SQLiteStore` (persistent signal history).
- **`src/backtest/`** — Intra-bar simulation (O/H/L price probes), PUT-direction exit logic, per-strategy cooldown, midday no-trade window, daily loss circuit breaker.

### HK Predict Module (`src/hk/`)

Independent HK market prediction system, fully decoupled from US pipeline. Generates daily Playbooks with regime classification and key levels for HK index option trading.

- **Core:** `HKPredictor` orchestrator with 3 daily pushes (09:35/10:05/13:05 HKT). `HKCollector` sync Futu wrapper, `volume_profile` (POC/VAH/VAL), `indicators` (VWAP, RVOL), `regime` (BREAKOUT/RANGE/WHIPSAW/UNCLEAR).
- **Signals:** `playbook` (Telegram-formatted strategy), `filter` (5 trade filters), `orderbook` (LV2 large orders), `gamma_wall` (Call/Put Wall, Max Pain for indices).
- **`src/hk/telegram.py`** — Bot commands prefixed `/hk_*` (playbook, orderbook, gamma, levels, regime, quote, filters, watchlist, help).
- **`src/hk/backtest/`** — Validates VP levels (bounce rates) and regime accuracy on historical data. Trade simulator with fixed/trailing/both exit modes. Run via `python -m src.hk.backtest`.

**Futu API gotchas (HK):** `get_market_snapshot` for bid/ask (not `get_stock_quote`). `option_open_interest` from snapshot for OI (not `option_area_type`). K-line timezone is HKT.

### US Playbook Module (`src/us_playbook/`)

Daily US options trading playbook, mirroring HK module design. Integrated into `OptionsMonitor` via shared APScheduler + Telegram bot.

- **Core:** `USPlaybook` orchestrator with 2 daily pushes (09:45/10:15 ET). Reuses `FutuCollector` from US pipeline. `get_snapshot()` for quotes (no subscription needed), `get_history_bars()` for multi-day 1m bars, `get_premarket_hl()` for PM range.
- **Analysis:** `volume_profile` (reuses HK VP with US `tick_size`), `indicators` (VWAP, window-based RVOL), `levels` (PDH/PDL, PMH/PML, VP, Gamma Wall), `regime` (GAP_AND_GO/TREND_DAY/FADE_CHOP/UNCLEAR with SPY context).
- **Filters:** `filter` (FOMC/NFP/CPI calendar, Monthly OpEx auto-detect, Inside Day + low RVOL).
- **`src/us_playbook/telegram.py`** — Bot commands prefixed `/us_*` (playbook, levels, regime, filters, gamma, help).
- **Config:** `config/us_playbook_settings.yaml` (watchlist, VP/RVOL/regime params), `config/us_calendar.yaml` (2026 FOMC/NFP/CPI/holidays).

**Futu API gotchas (US):** Use `get_market_snapshot` (not `get_stock_quote`) for US Playbook quotes — avoids subscription requirement. Option chain `get_stock_quote` needs subscription; Gamma Wall uses 10s hard timeout with graceful fallback.

## Strategy YAML Format (US)

Strategies live in `config/strategies/` and are hot-reloaded. Required fields: `strategy_id`, `name`, `enabled`, `watchlist`, `entry_conditions`. Entry/exit conditions use nested rule trees with `operator: AND/OR/MIN_MATCH`. Rules can compare indicator values against thresholds or other indicator fields via `reference_field`. See `strategies.md` for full strategy documentation.

## HK Configuration

- `config/hk_settings.yaml` — HK watchlist (indices + stocks), regime thresholds (`breakout_rvol`, `range_rvol`), playbook push times, filter params, order book and gamma wall settings, `simulation` block (tp/sl/slippage, exit_mode, trailing params, exclude_symbols, skip_signal_types).
- `config/hk_calendar.yaml` — Economic calendar (FOMC, HKMA, China PMI/GDP, HK holidays, HSI option expiry dates). Manually maintained.

## US Playbook Configuration

- `config/us_playbook_settings.yaml` — US watchlist (SPY/QQQ/AAPL/TSLA/NVDA/META/AMD/AMZN), VP lookback (3d), RVOL window (15min), regime thresholds (gap_and_go 1.5/2.0, trend_day 1.2, fade_chop 1.0), market context symbols, Gamma Wall toggle.
- `config/us_calendar.yaml` — 2026 US macro calendar (FOMC/NFP/CPI/holidays). Monthly OpEx auto-calculated.

## Key Conventions

- Python 3.11, async throughout (asyncio + APScheduler)
- Dependencies in `requirements.txt` (no pyproject.toml)
- Config in `config/settings.yaml` (US) and `config/hk_settings.yaml` (HK), secrets via `.env` (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
- Tests use pytest with synthetic bar data helpers
- HK module is fully independent — own collector, indicators, and Telegram commands
