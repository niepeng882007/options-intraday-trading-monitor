# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Options Intraday Trading Monitor — an async Python system providing on-demand playbook analysis and auto-scan alerts for US and HK options trading via Telegram.

## Commands

```bash
python -m src.main              # Run combined entry (US + HK Playbook)
python -m src.hk                # Run the HK predictor (standalone)
python -m src.us_playbook       # Run the US Predictor (standalone)
pytest tests/ -v                # Run all tests
pytest tests/test_hk.py -v      # Run a single test file
pytest tests/test_us_playbook.py -v  # Run US Playbook tests

# Run HK backtest
python -m src.hk.backtest -d 20
python -m src.hk.backtest -d 30 --exclude HK.800000 HK.00941 --exit-mode trailing -v

# Run US Predictor backtest
python -m src.us_playbook.backtest -d 30
python -m src.us_playbook.backtest -y SPY,AAPL -d 20 --no-sim -v
python -m src.us_playbook.backtest --exit-mode trailing --no-adaptive -o json

# Run Daily Bias Signal Validation (Phase 0)
python -m src.us_playbook.backtest.daily_bias_eval -d 180 --all-watchlist -v
python -m src.us_playbook.backtest.daily_bias_eval -d 60 -y SPY,AAPL,TSLA -v
python -m src.us_playbook.backtest.daily_bias_eval -d 180 --all-watchlist -o json

docker compose up --build       # Docker
```

## Architecture

**`src/main.py`** — Combined entry point. Creates shared `FutuCollector`, initializes `USPredictor` + `HKPredictor`, single Telegram Application with both modules' handlers, APScheduler for auto-scan, `/kb`/`/kboff` keyboard commands. Graceful shutdown via SIGTERM/SIGINT.

**`src/collector/`** — `FutuCollector` (shared real-time source). Returns `StockQuote`, `OptionQuote`, and bar DataFrames. `yfinance` used for VIX, premarket fallback, and backtests.

**`src/store/`** — `message_archive.py` (Telegram message logging to SQLite).

### Shared Common Module (`src/common/`)

Shared utilities extracted from HK and US modules to eliminate cross-module dependencies. Both market modules import from `src/common/` instead of from each other.

- **`types.py`** — 9 shared dataclasses: `VolumeProfileResult`, `GammaWallResult`, `FilterResult`, `OptionLeg`, `ChaseRiskResult`, `SpreadMetrics`, `OptionRecommendation`, `QuoteSnapshot`, `OptionMarketSnapshot`. HK-specific types (`RegimeType`, `RegimeResult`, `HKKeyLevels`, `Playbook`, `ScanSignal`, `ScanAlertRecord`, `OrderBookAlert`) remain in `src/hk/__init__.py`.
- **`volume_profile.py`** — `calculate_volume_profile()` (POC/VAH/VAL). Re-export shim at `src/hk/volume_profile.py`.
- **`gamma_wall.py`** — `calculate_gamma_wall()`, `format_gamma_wall_message()`. Re-export shim at `src/hk/gamma_wall.py`.
- **`formatting.py`** — 12 playbook formatting utilities: `confidence_bar()`, `pct_change()`, `format_percent()`, `split_reason_lines()`, `closest_value_area_edge()`, `action_label()`, `action_plain_language()`, `format_strike()`, `format_leg_line()`, `position_size_text()`, `spread_execution_text()`, `risk_status_text()`. Market-specific formatters (`_format_turnover`, `_price_position`, `_regime_reason_lines`) remain in each module's `playbook.py`.
- **`option_utils.py`** — `classify_moneyness()`, `option_leg_from_row()`, `calculate_spread_metrics()`, `is_positive_ev()`, `recommend_single_leg()`, `recommend_spread()`, `assess_chase_risk()`. Defaults match HK values (min_oi=50, chase 2.0/3.5%); US callers pass tighter overrides (min_oi=100, chase 1.5/2.5%).
- **`indicators.py`** — `calculate_vwap()`. RVOL stays in each module (different algorithms).
- **`action_plan.py`** — `ActionPlan`, `PlanContext` dataclasses + 12 shared plan utilities (`calculate_rr`, `reachable_range_pct`, `compact_option_line`, `format_action_plan`, `nearest_levels`, `find_fade_entry_zone`, `cap_tp2`, `check_entry_reachability`, `apply_wait_coherence`, `apply_min_rr_gate`). US and HK playbooks both import from here.
- **`watchlist.py`** — `Watchlist` base class with `config_parser` callback. `HKWatchlist` and `USWatchlist` are thin wrappers.
- **`telegram_handlers.py`** — `handle_query_base()`, `handle_add_base()`, `handle_remove_base()`, `handle_watchlist_base()`, `build_combined_keyboard()`. Market modules keep regex patterns, help text, and `register_*_handlers()`.
- **`chart.py`** — `generate_chart()` / `generate_chart_async()` produce dark-themed candlestick PNG (BytesIO) with key levels + VP sidebar. `ChartData` input dataclass. Both `HKPredictor` and `USPredictor` return `PlaybookResponse(html, chart)` from `generate_playbook_for_symbol()`. `handle_query_base()` sends chart photo before HTML text; chart failure degrades gracefully to text-only.

**Backward compatibility:** `src/hk/__init__.py` re-exports shared types, `src/hk/volume_profile.py` and `src/hk/gamma_wall.py` are re-export shims. Old import paths (`from src.hk import VolumeProfileResult`) continue to work.

### HK Playbook Module (`src/hk/`)

On-demand HK market playbook system, integrated via shared Telegram Application. No scheduled pushes — pure text-triggered playbook generation.

- **Core:** `HKPredictor` orchestrator (on-demand, no APScheduler). `HKCollector` sync Futu wrapper, `indicators` (RVOL, trading time checks, `calculate_initial_balance()` for IBH/IBL, `minutes_to_close_hk()` for 330min session, `calculate_avg_daily_range()`, `build_hk_key_levels()`/`hk_key_levels_to_dict()`), `regime` (GAP_AND_GO/TREND_DAY/FADE_CHOP/WHIPSAW/UNCLEAR; deprecated BREAKOUT/RANGE kept for backward compat).
- **Key Levels:** `HKKeyLevels` dataclass (POC/VAH/VAL/PDH/PDL/PDC/IBH/IBL/day_open/VWAP/Gamma). IBH/IBL = Initial Balance (first 30min high/low, replacing PMH/PML).
- **Playbook:** ActionPlan engine from `src.common.action_plan`. 5-section format: header + 核心结论 + 剧本推演(A/B/C ActionPlans) + 盘面逻辑 + 数据雷达. Market context: HSI/HSTECH regime in header (`_get_market_context_regime`, 300s TTL cache).
- **Option Recommendation:** `option_recommend.py` — direction from regime + price position, expiry selection (filters DTE=0), delegates to `src.common.option_utils` for single-leg/spread/chase-risk, strict wait policy (must have concrete strike + expiry, otherwise observe).
- **Watchlist:** `watchlist.py` — `HKWatchlist(Watchlist)` thin wrapper + `normalize_symbol()`. JSON persistence (`data/hk_watchlist.json`), `+09988` add / `-09988` remove / `wl` view. Falls back to `hk_settings.yaml` on first run.
- **Filters:** `filter` (5 filters: calendar, Inside Day, IV+RVOL, min turnover, expiry risk).
- **`src/hk/telegram.py`** — Text-triggered handlers delegating to `src.common.telegram_handlers` base functions. Regex patterns: `09988`/`HK09988` query, `+code` add, `-code` remove, `wl` list. `/hk_help` command.
- **`src/hk/backtest/`** — Validates VP levels (bounce rates) and regime accuracy on historical data. Trade simulator with fixed/trailing/both exit modes. Run via `python -m src.hk.backtest`.

**Futu API gotchas (HK):** `get_market_snapshot` for bid/ask (not `get_stock_quote`). `option_open_interest` from snapshot for OI (not `option_area_type`). K-line timezone is HKT.

### US Predictor Module (`src/us_playbook/`)

On-demand US options trading predictor, mirroring HK Playbook design. Integrated via shared Telegram Application. No scheduled pushes — text-triggered playbook generation + auto-scan strong signal alerts. Imports shared logic from `src/common/` (no dependency on `src/hk/`).

- **Core:** `USPredictor` orchestrator (on-demand + auto-scan). Reuses shared `FutuCollector`. `get_snapshot()` for quotes (no subscription needed), `get_history_bars()` for multi-day 1m bars, `get_premarket_hl()` for PM range. Binary bar caching (history cached 120s TTL, today always fresh). SPY context with 300s TTL shared between on-demand and auto-scan.
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
- **`daily_bias_eval.py`** — Phase 0 daily bias signal validation. `DailyBiasEvaluator` tests 5 sub-signals (daily structure HH/HL, yesterday candle, volume modifier, hourly EMA crossover, ATR-normalized gap) against dual labels (Label A: raw direction close-vs-open/VWAP; Label B: regime-aligned P&L sign via `evaluate_regimes()` + `USTradeSimulator`). Parameter scanning per signal (structure windows [5,8,10,15], candle body_ratio [0.3-0.7], EMA pairs [8/21,13/34,20/50], gap ATR multipliers [0.2,0.3,0.5]). Analysis: per-param win rates with binomial p-value, Pearson/Spearman correlation matrix, 5 aggregation weight schemes, confidence sensitivity (±modifier on scan/observe thresholds), time-segment (AM1/AM2/PM exploratory), VIX stratification (low/mid/high via yfinance history). 6 Go/No-Go criteria (G1-G6) with PASS/FAIL/INCONCLUSIVE verdicts. CLI: `python -m src.us_playbook.backtest.daily_bias_eval`.
- **Config:** `simulation` block in `config/us_playbook_settings.yaml` (tp 0.5%, sl 0.25%, slippage 0.03%/leg, trailing exit).

**Futu `INTERVAL_MAP`:** Supports `1m`, `5m`, `15m`, `1d`. Unknown intervals raise `ValueError` (no silent fallback). `_fetch_history_bars` uses dynamic `max_count` (daily: `days+10`, intraday: `(days+3)*400`).

## HK Configuration

- `config/hk_settings.yaml` — HK initial watchlist (indices + stocks, runtime managed via `data/hk_watchlist.json`), regime thresholds (`gap_and_go_gap_pct`, `trend_day_rvol`, `fade_chop_rvol`, `ib_window_minutes` + legacy `breakout_rvol`/`range_rvol`), `market_context` (hsi_symbol, hstech_symbol, context_ttl_seconds), filter params (min turnover), gamma wall settings, `simulation` block (tp/sl/slippage, exit_mode, trailing params, exclude_symbols, skip_signal_types).
- `config/hk_calendar.yaml` — Economic calendar (FOMC, HKMA, China PMI/GDP, HK holidays, HSI option expiry dates). Manually maintained.

## US Predictor Configuration

- `config/us_playbook_settings.yaml` — US watchlist (SPY/QQQ/AAPL/TSLA/NVDA/META/AMD/AMZN, runtime managed via `data/us_watchlist.json`), VP lookback (5d), RVOL params (skip_open 3min, lookback 10d), regime thresholds (adaptive enabled, gap_and_go 1.5, trend_day 1.2, fade_chop 1.0), market context symbols, Gamma Wall toggle, auto_scan (interval 180s, breakout/range_reversal configs, cooldown/override), chase_risk thresholds, option_recommend (dte_min 1, dte_preferred_max 7, delta 0.30-0.50, min_oi 100), hist_cache_ttl 120s, `simulation` block (tp/sl/slippage, exit_mode, trailing params, exclude_symbols, skip_signal_types).
- `config/us_calendar.yaml` — 2026 US macro calendar (FOMC/NFP/CPI/holidays). Monthly OpEx auto-calculated.

## Key Conventions

- Python 3.11, async throughout (asyncio + APScheduler)
- Dependencies in `requirements.txt` (no pyproject.toml)
- Config in `config/us_playbook_settings.yaml` (US) and `config/hk_settings.yaml` (HK), secrets via `.env` (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
- Tests use pytest with synthetic bar data helpers
- Shared logic lives in `src/common/` — market-specific modules (`src/hk/`, `src/us_playbook/`) import from common, never from each other
- Re-export shims in `src/hk/` preserve backward compatibility for old import paths (e.g., `from src.hk import VolumeProfileResult` still works)
- When adding shared functionality, put it in `src/common/` with sensible defaults; market modules pass overrides as needed
