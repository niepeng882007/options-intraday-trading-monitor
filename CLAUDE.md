# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Options Intraday Trading Monitor — a real-time async Python system that monitors stock prices, calculates technical indicators, evaluates YAML-defined strategy rules, and sends Telegram notifications for entry/exit signals.

## Commands

```bash
python -m src.main              # Run the US monitor (includes US Playbook)
python -m src.hk                # Run the HK predictor (standalone)
python -m src.us_playbook       # Run the US Predictor (standalone)
pytest tests/ -v                # Run all tests
pytest tests/test_hk.py -v      # Run a single test file
pytest tests/test_us_playbook.py -v  # Run US Playbook tests
python -m src.backtest           # Run backtest (US)

# Run HK backtest
python -m src.hk.backtest -d 20
python -m src.hk.backtest -d 30 --exclude HK.800000 HK.00941 --exit-mode trailing -v

# Run US Predictor backtest
python -m src.us_playbook.backtest -d 30
python -m src.us_playbook.backtest -y SPY,AAPL -d 20 --no-sim -v
python -m src.us_playbook.backtest --exit-mode trailing --no-adaptive -o json

docker compose up --build       # Docker
```

## Architecture

**US Pipeline:** Collector → IndicatorEngine → RuleMatcher → StateManager → TelegramNotifier → SQLiteStore

- **`src/main.py`** — `OptionsMonitor` orchestrator. Futu push mode (QUOTE + K_1M) or APScheduler polling fallback. Futu disconnect detection pauses signal processing and notifies via Telegram; auto-recovers on reconnect. Daily summary cron at 16:05 ET + `/summary` command.
- **`src/collector/`** — `FutuCollector` (sole real-time source). Returns `StockQuote`, `OptionQuote`, and bar DataFrames. `yfinance` is only used in backtest (`src/backtest/data_loader.py`) and premarket fallback (`src/collector/futu.py::_fetch_yahoo_premarket()`).
- **`src/indicator/engine.py`** — Per-symbol bar data across timeframes (1m, 5m, 15m). Calculates RSI, MACD, EMA, ATR, VWAP, Bollinger Bands, ADX, Stochastic, candle shadow metrics.
- **`src/strategy/`** — `StrategyLoader` hot-reloads YAML from `config/strategies/`. `RuleMatcher` evaluates nested rule trees with quality scoring, `confirm_bars`, `market_context_filters` (ADX overlap zone 20-30). `StrategyStateManager` tracks WATCHING → ENTRY_TRIGGERED → HOLDING → EXIT_TRIGGERED cycle. PUT-aware trailing stop tracks `lowest_price`.
- **`src/notification/telegram.py`** — Telegram bot with signal notifications and interactive commands (`/confirm`, `/skip`, `/status`, etc.).
- **`src/store/`** — `RedisStore` (caching/pubsub) and `SQLiteStore` (persistent signal history).
- **`src/backtest/`** — Intra-bar simulation (O/H/L price probes), PUT-direction exit logic, per-strategy cooldown, midday no-trade window, daily loss circuit breaker.

### Shared Common Module (`src/common/`)

Shared utilities extracted from HK and US modules to eliminate cross-module dependencies. Both market modules import from `src/common/` instead of from each other.

- **`types.py`** — 9 shared dataclasses: `VolumeProfileResult`, `GammaWallResult`, `FilterResult`, `OptionLeg`, `ChaseRiskResult`, `SpreadMetrics`, `OptionRecommendation`, `QuoteSnapshot`, `OptionMarketSnapshot`. HK-specific types (`RegimeType`, `RegimeResult`, `Playbook`, `ScanSignal`, `ScanAlertRecord`, `OrderBookAlert`) remain in `src/hk/__init__.py`.
- **`volume_profile.py`** — `calculate_volume_profile()` (POC/VAH/VAL). Re-export shim at `src/hk/volume_profile.py`.
- **`gamma_wall.py`** — `calculate_gamma_wall()`, `format_gamma_wall_message()`. Re-export shim at `src/hk/gamma_wall.py`.
- **`formatting.py`** — 12 playbook formatting utilities: `confidence_bar()`, `pct_change()`, `format_percent()`, `split_reason_lines()`, `closest_value_area_edge()`, `action_label()`, `action_plain_language()`, `format_strike()`, `format_leg_line()`, `position_size_text()`, `spread_execution_text()`, `risk_status_text()`. Market-specific formatters (`_format_turnover`, `_price_position`, `_regime_reason_lines`) remain in each module's `playbook.py`.
- **`option_utils.py`** — `classify_moneyness()`, `option_leg_from_row()`, `calculate_spread_metrics()`, `is_positive_ev()`, `recommend_single_leg()`, `recommend_spread()`, `assess_chase_risk()`. Defaults match HK values (min_oi=50, chase 2.0/3.5%); US callers pass tighter overrides (min_oi=100, chase 1.5/2.5%).
- **`indicators.py`** — `calculate_vwap()`. RVOL stays in each module (different algorithms).
- **`watchlist.py`** — `Watchlist` base class with `config_parser` callback. `HKWatchlist` and `USWatchlist` are thin wrappers.
- **`telegram_handlers.py`** — `handle_query_base()`, `handle_add_base()`, `handle_remove_base()`, `handle_watchlist_base()`. Market modules keep regex patterns, help text, and `register_*_handlers()`.
- **`chart.py`** — `generate_chart()` / `generate_chart_async()` produce dark-themed candlestick PNG (BytesIO) with key levels + VP sidebar. `ChartData` input dataclass. Both `HKPredictor` and `USPredictor` return `PlaybookResponse(html, chart)` from `generate_playbook_for_symbol()`. `handle_query_base()` sends chart photo before HTML text; chart failure degrades gracefully to text-only.
- **`daily_report.py`** — `collect_pipeline_data()` reads today's signals from SQLite, matches entry/exit pairs, calculates per-trade PnL. `format_daily_summary()` renders Telegram HTML with signal stats, quality/strategy distribution, best/worst trades. Auto-sent at 16:05 ET via cron; manual trigger via `/summary` command.

**Backward compatibility:** `src/hk/__init__.py` re-exports shared types, `src/hk/volume_profile.py` and `src/hk/gamma_wall.py` are re-export shims. Old import paths (`from src.hk import VolumeProfileResult`) continue to work.

### HK Playbook Module (`src/hk/`)

On-demand HK market playbook system, integrated into `OptionsMonitor` via shared Telegram Application. No scheduled pushes — pure text-triggered playbook generation.

- **Core:** `HKPredictor` orchestrator (on-demand, no APScheduler). `HKCollector` sync Futu wrapper, `indicators` (RVOL, trading time checks), `regime` (BREAKOUT/RANGE/WHIPSAW/UNCLEAR with IV spike detection).
- **Option Recommendation:** `option_recommend.py` — direction from regime + price position, expiry selection (filters DTE=0), delegates to `src.common.option_utils` for single-leg/spread/chase-risk, strict wait policy (must have concrete strike + expiry, otherwise observe).
- **Watchlist:** `watchlist.py` — `HKWatchlist(Watchlist)` thin wrapper + `normalize_symbol()`. JSON persistence (`data/hk_watchlist.json`), `+09988` add / `-09988` remove / `wl` view. Falls back to `hk_settings.yaml` on first run.
- **Signals:** `playbook` (5-section Telegram HTML: regime, data, option rec, risk; uses `src.common.formatting` utilities), `filter` (5 filters: calendar, Inside Day, IV+RVOL, min turnover, expiry risk).
- **`src/hk/telegram.py`** — Text-triggered handlers delegating to `src.common.telegram_handlers` base functions. Regex patterns: `09988`/`HK09988` query, `+code` add, `-code` remove, `wl` list. `/hk_help` command.
- **`src/hk/backtest/`** — Validates VP levels (bounce rates) and regime accuracy on historical data. Trade simulator with fixed/trailing/both exit modes. Run via `python -m src.hk.backtest`.

**Futu API gotchas (HK):** `get_market_snapshot` for bid/ask (not `get_stock_quote`). `option_open_interest` from snapshot for OI (not `option_area_type`). K-line timezone is HKT.

### US Predictor Module (`src/us_playbook/`)

On-demand US options trading predictor, mirroring HK Playbook design. Integrated into `OptionsMonitor` via shared Telegram Application. No scheduled pushes — text-triggered playbook generation + auto-scan strong signal alerts. Imports shared logic from `src/common/` (no dependency on `src/hk/`).

- **Core:** `USPredictor` orchestrator (on-demand + auto-scan). Reuses `FutuCollector` from US pipeline. `get_snapshot()` for quotes (no subscription needed), `get_history_bars()` for multi-day 1m bars, `get_premarket_hl()` for PM range. Binary bar caching (history cached 120s TTL, today always fresh). SPY context with 300s TTL shared between on-demand and auto-scan.
- **Analysis:** `levels` (PDH/PDL, PMH/PML, VP via `src.common.volume_profile`, Gamma Wall via `src.common.gamma_wall`), `indicators` (window-based RVOL, adaptive thresholds), `regime` (GAP_AND_GO/TREND_DAY/FADE_CHOP/UNCLEAR with SPY context).
- **Option Recommendation:** `option_recommend.py` — direction from regime + price position, expiry selection (filters 0DTE, prefers 2-7 DTE weekly), delegates to `src.common.option_utils` with US overrides (min_oi=100, chase 1.5/2.5%), Greeks degradation handling (fallback to moneyness when delta unavailable).
- **Watchlist:** `watchlist.py` — `USWatchlist(Watchlist)` thin wrapper + `normalize_us_symbol()`. JSON persistence (`data/us_watchlist.json`), `+AAPL` add / `-AAPL` remove / `uswl` view. Falls back to `us_playbook_settings.yaml` on first run.
- **Auto-scan:** L1 lightweight screen → L2 full pipeline verification. Structure/execution decoupled (structure gates push, option rec is informational). 3-layer frequency control (same-signal 30min cooldown, per-session max 2, daily max 3) with override exceptions.
- **Filters:** `filter` (FOMC/NFP/CPI calendar, Monthly OpEx auto-detect, Inside Day + low RVOL).
- **`src/us_playbook/telegram.py`** — Text-triggered handlers delegating to `src.common.telegram_handlers` base functions. Regex patterns: `SPY`/`AAPL` query, `+code` add, `-code` remove, `uswl` list. `/us_help` command.
- **Config:** `config/us_playbook_settings.yaml` (watchlist, VP/RVOL/regime params, auto_scan, chase_risk, option_recommend), `config/us_calendar.yaml` (2026 FOMC/NFP/CPI/holidays).

**Futu API gotchas (US):** Use `get_market_snapshot` (not `get_stock_quote`) for US Predictor quotes — avoids subscription requirement. Option chain `get_stock_quote` needs subscription; Gamma Wall uses 10s hard timeout with graceful fallback. `get_option_expiration_dates()` uses lightweight `get_option_chain()` call (structure only, no quote/snapshot) with 5min TTL cache.

### US Predictor Backtest (`src/us_playbook/backtest/`)

Validates VP levels (VAH/VAL/PDH/PDL bounce rates) and regime classification accuracy using historical data. All regime parameters strictly mirror production `src/us_playbook/main.py`.

- **`data_loader.py`** — `USDataLoader` fetches 1m bars from Futu, CSV cache at `data/us_backtest_cache/`. Reuses `normalize_futu_kline()` for ET timezone. US hours 09:30-16:00 (no lunch break).
- **`evaluators.py`** — `evaluate_levels()` tests VAH/VAL/PDH/PDL bounce rates. `evaluate_regimes()` mirrors production `classify_us_regime()` with full parameters (adaptive RVOL, SPY context, PM gap_estimate fallback). UNCLEAR not scored (D3: `scorable=False`).
- **`simulator.py`** — `USTradeSimulator` with fixed/trailing/both exit modes. EOD exit 15:50 ET. Regime entry at 09:38 ET.
- **`engine.py`** — `USBacktestEngine` chains level eval → regime eval → optional simulation.
- **`report.py`** — 3-section report: Level Accuracy (VAH/VAL/PDH/PDL), Regime Accuracy (D3 scoring), Trade Simulation.
- **Config:** `simulation` block in `config/us_playbook_settings.yaml` (tp 0.5%, sl 0.25%, slippage 0.03%/leg, trailing exit).

## Strategy YAML Format (US)

Strategies live in `config/strategies/` and are hot-reloaded. Required fields: `strategy_id`, `name`, `enabled`, `watchlist`, `entry_conditions`. Entry/exit conditions use nested rule trees with `operator: AND/OR/MIN_MATCH`. Rules can compare indicator values against thresholds or other indicator fields via `reference_field`. See `strategies.md` for full strategy documentation.

## HK Configuration

- `config/hk_settings.yaml` — HK initial watchlist (indices + stocks, runtime managed via `data/hk_watchlist.json`), regime thresholds (`breakout_rvol`, `range_rvol`, `iv_spike_ratio`), filter params (min turnover), gamma wall settings, `simulation` block (tp/sl/slippage, exit_mode, trailing params, exclude_symbols, skip_signal_types).
- `config/hk_calendar.yaml` — Economic calendar (FOMC, HKMA, China PMI/GDP, HK holidays, HSI option expiry dates). Manually maintained.

## US Predictor Configuration

- `config/us_playbook_settings.yaml` — US watchlist (SPY/QQQ/AAPL/TSLA/NVDA/META/AMD/AMZN, runtime managed via `data/us_watchlist.json`), VP lookback (5d), RVOL params (skip_open 3min, lookback 10d), regime thresholds (adaptive enabled, gap_and_go 1.5, trend_day 1.2, fade_chop 1.0), market context symbols, Gamma Wall toggle, auto_scan (interval 180s, breakout/range_reversal configs, cooldown/override), chase_risk thresholds, option_recommend (dte_min 1, dte_preferred_max 7, delta 0.30-0.50, min_oi 100), hist_cache_ttl 120s, `simulation` block (tp/sl/slippage, exit_mode, trailing params, exclude_symbols, skip_signal_types).
- `config/us_calendar.yaml` — 2026 US macro calendar (FOMC/NFP/CPI/holidays). Monthly OpEx auto-calculated.

## Key Conventions

- Python 3.11, async throughout (asyncio + APScheduler)
- Dependencies in `requirements.txt` (no pyproject.toml)
- Config in `config/settings.yaml` (US) and `config/hk_settings.yaml` (HK), secrets via `.env` (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
- Tests use pytest with synthetic bar data helpers
- Shared logic lives in `src/common/` — market-specific modules (`src/hk/`, `src/us_playbook/`) import from common, never from each other
- Re-export shims in `src/hk/` preserve backward compatibility for old import paths (e.g., `from src.hk import VolumeProfileResult` still works)
- When adding shared functionality, put it in `src/common/` with sensible defaults; market modules pass overrides as needed
