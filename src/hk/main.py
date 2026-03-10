"""HK Predict orchestrator — on-demand playbook generation (no scheduled pushes).

Usage:
    python -m src.hk          # Run as standalone (dev/debug)
    # Or integrated into main OptionsMonitor via shared Telegram Application
"""

from __future__ import annotations

import asyncio
import html
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import yaml

from src.hk import FilterResult, GammaWallResult, Playbook
from src.hk.collector import HKCollector
from src.hk.filter import check_filters
from src.hk.gamma_wall import calculate_gamma_wall
from src.hk.indicators import (
    calculate_rvol,
    calculate_vwap,
    get_history_bars,
    get_today_bars,
)
from src.hk.option_recommend import recommend, select_expiry
from src.hk.playbook import format_playbook_message, generate_playbook
from src.hk.regime import classify_regime
from src.hk.volume_profile import calculate_volume_profile
from src.hk.watchlist import HKWatchlist
from src.utils.logger import setup_logger

logger = setup_logger("hk_predictor")

HKT = timezone(timedelta(hours=8))
_executor = ThreadPoolExecutor(max_workers=1)
_esc = html.escape

DEFAULT_CONFIG_PATH = "config/hk_settings.yaml"

# Cache TTLs
_VP_CACHE_TTL = 300  # 5 min for K-line + VP
_QUERY_CACHE_TTL = 30  # 30s per-symbol dedup


def _load_config(path: str = DEFAULT_CONFIG_PATH) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


class HKPredictor:
    """Orchestrates HK market prediction pipeline — on-demand, no scheduled pushes.

    Data flow per query:
        1. Fetch 1m K-lines (5 days) → Volume Profile (POC/VAH/VAL)
        2. Fetch today's K-lines → VWAP
        3. Compute RVOL (session-aware)
        4. Fetch option chain OI → Gamma Wall (indices only) + IV
        5. Fetch quote → Filter checks
        6. Classify Regime (with IV)
        7. Generate option recommendation
        8. Format & return Playbook
    """

    def __init__(self, config_path: str = DEFAULT_CONFIG_PATH) -> None:
        self._cfg = _load_config(config_path)
        futu_cfg = self._cfg.get("futu", {})
        self._collector = HKCollector(
            host=futu_cfg.get("host", "127.0.0.1"),
            port=futu_cfg.get("port", 11111),
        )
        self._connected = False

        # Watchlist — dynamic, persisted to JSON
        self.watchlist = HKWatchlist(
            path="data/hk_watchlist.json",
            initial_config=self._cfg,
        )

        # Caches
        self._vp_cache: dict[str, tuple[float, pd.DataFrame, pd.DataFrame]] = {}  # symbol → (ts, hist, today)
        self._query_cache: dict[str, tuple[float, str]] = {}  # symbol → (ts, formatted_msg)

    # ── Lifecycle ──

    async def connect(self) -> None:
        await self._run_sync(self._collector.connect)
        self._connected = True
        logger.info("HKPredictor connected")

    async def close(self) -> None:
        await self._run_sync(self._collector.close)
        self._connected = False

    # ── Sync → Async bridge ──

    async def _run_sync(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_executor, fn, *args)

    # ── Core: on-demand playbook for a single symbol ──

    async def generate_playbook_for_symbol(self, symbol: str) -> str:
        """Generate aggregated playbook for a single symbol. Returns formatted HTML."""
        now = time.time()

        # Per-symbol 30s dedup
        cached = self._query_cache.get(symbol)
        if cached and now - cached[0] < _QUERY_CACHE_TTL:
            return cached[1]

        cfg = self._cfg
        vp_cfg = cfg.get("volume_profile", {})
        regime_cfg = cfg.get("regime", {})
        filter_cfg = cfg.get("filters", {})
        calendar_path = cfg.get("calendar_file", "config/hk_calendar.yaml")
        index_symbols = cfg.get("gamma_wall", {}).get("index_symbols", [])

        # 1. K-lines (with 5min cache)
        hist_bars, today_bars = await self._get_bars_cached(symbol, vp_cfg)

        # 2. Volume Profile
        vp = calculate_volume_profile(
            hist_bars,
            value_area_pct=vp_cfg.get("value_area_pct", 0.70),
        )

        # 3. VWAP
        vwap = calculate_vwap(today_bars)

        # 4. RVOL
        rvol = calculate_rvol(
            today_bars, hist_bars,
            lookback_days=cfg.get("rvol", {}).get("lookback_days", 10),
        )

        # 5. Quote
        quote = await self._run_sync(self._collector.get_quote, symbol)
        price = quote["last_price"]
        turnover = quote["turnover"]

        # 6. Gamma Wall + Option chain (indices or try for stocks)
        gamma_wall: GammaWallResult | None = None
        chain_df: pd.DataFrame = pd.DataFrame()
        expiry_dates: list[dict] = []
        target_expiry: str | None = None
        atm_iv = 0.0
        avg_iv = 0.0

        try:
            # Try to get expiry dates first
            expiry_dates = await self._run_sync(
                self._collector.get_option_expiration_dates, symbol,
            )

            # Pick target expiry for efficient chain fetch
            target_expiry = select_expiry(expiry_dates)

            # Get chain (optionally filtered by expiry)
            chain_df = await self._run_sync(
                self._collector.get_option_chain_with_oi,
                symbol,
                "NORMAL",
                target_expiry,
            )

            if not chain_df.empty:
                # Gamma Wall for indices
                if symbol in index_symbols:
                    gamma_wall = calculate_gamma_wall(chain_df, price)

                # Extract ATM IV
                atm_iv, avg_iv = self._extract_iv(chain_df, price)

        except Exception:
            logger.debug("Option chain fetch failed for %s", symbol, exc_info=True)

        # 7. Filters
        prev_high, prev_low = 0.0, 0.0
        if not hist_bars.empty:
            last_day = hist_bars.index[-1].date()
            last_day_bars = hist_bars[hist_bars.index.date == last_day]
            if not last_day_bars.empty:
                prev_high = float(last_day_bars["High"].max())
                prev_low = float(last_day_bars["Low"].min())

        # Compute IV rank (0-100 percentile proxy)
        iv_rank = 0.0
        if avg_iv > 0 and atm_iv > 0:
            iv_rank = min(100.0, (atm_iv / avg_iv) * 50.0)

        # Parse target expiry as date for filter
        expiry_as_date: date | None = None
        if target_expiry:
            try:
                expiry_as_date = datetime.strptime(target_expiry[:10], "%Y-%m-%d").date()
            except (ValueError, TypeError):
                pass

        filters = check_filters(
            symbol=symbol,
            turnover=turnover,
            prev_high=prev_high,
            prev_low=prev_low,
            current_high=quote["high_price"],
            current_low=quote["low_price"],
            rvol=rvol,
            iv_rank=iv_rank,
            expiry_date=expiry_as_date,
            calendar_path=calendar_path,
            min_turnover=filter_cfg.get("min_turnover_hkd", 1e8),
        )

        # 8. Regime classification (with IV)
        regime = classify_regime(
            price=price,
            rvol=rvol,
            vp=vp,
            gamma_wall=gamma_wall,
            atm_iv=atm_iv,
            avg_iv=avg_iv,
            breakout_rvol=regime_cfg.get("breakout_rvol", 1.2),
            range_rvol=regime_cfg.get("range_rvol", 0.8),
            iv_spike_ratio=regime_cfg.get("iv_spike_ratio", 1.3),
        )

        # 9. Option recommendation
        option_rec = recommend(
            regime=regime,
            vp=vp,
            filters=filters,
            chain_df=chain_df if not chain_df.empty else None,
            expiry_dates=expiry_dates,
            gamma_wall=gamma_wall,
        )

        # 10. Generate & format playbook
        playbook = generate_playbook(
            regime=regime,
            vp=vp,
            vwap=vwap,
            gamma_wall=gamma_wall,
            filters=filters,
            symbol=symbol,
            option_rec=option_rec,
        )

        name = self.watchlist.get_name(symbol)
        display = f"{name} ({symbol})" if name != symbol else symbol
        formatted = format_playbook_message(playbook, symbol=display)

        # Cache result
        self._query_cache[symbol] = (now, formatted)

        return formatted

    # ── Helpers ──

    async def _get_bars_cached(
        self, symbol: str, vp_cfg: dict,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Get hist_bars and today_bars with 5-minute cache."""
        now = time.time()
        cached = self._vp_cache.get(symbol)
        if cached and now - cached[0] < _VP_CACHE_TTL:
            return cached[1], cached[2]

        lookback = vp_cfg.get("lookback_days", 5)
        bars = await self._run_sync(self._collector.get_history_kline, symbol, lookback)
        hist_bars = get_history_bars(bars)
        today_bars = get_today_bars(bars)

        self._vp_cache[symbol] = (now, hist_bars, today_bars)
        return hist_bars, today_bars

    @staticmethod
    def _extract_iv(chain_df: pd.DataFrame, price: float) -> tuple[float, float]:
        """Extract ATM implied volatility from option chain.

        Returns (atm_iv, avg_iv) where avg_iv is a simplified baseline.
        """
        if chain_df.empty or "implied_volatility" not in chain_df.columns:
            return 0.0, 0.0

        # Find ATM options (closest to current price)
        chain_df = chain_df.copy()
        chain_df["_dist"] = (chain_df["strike_price"] - price).abs()
        atm = chain_df.nsmallest(4, "_dist")

        iv_values = atm["implied_volatility"].dropna()
        iv_values = iv_values[iv_values > 0]

        if iv_values.empty:
            return 0.0, 0.0

        atm_iv = float(iv_values.mean())
        # Simplified baseline: use 80% of current ATM IV as historical average proxy
        avg_iv = atm_iv * 0.8

        return atm_iv, avg_iv


# ── Standalone entry point (dev/debug) ──

async def _main() -> None:
    """Run HKPredictor as standalone with Telegram bot polling (no scheduled pushes)."""

    predictor = HKPredictor()

    # Setup Telegram
    cfg = predictor._cfg.get("telegram", {})
    bot_token = cfg.get("bot_token", "")
    chat_id = cfg.get("chat_id", "")

    import os
    if bot_token.startswith("${"):
        bot_token = os.environ.get(bot_token.strip("${}"), "")
    if str(chat_id).startswith("${"):
        chat_id = os.environ.get(str(chat_id).strip("${}"), "")

    await predictor.connect()

    if bot_token and chat_id:
        from telegram.ext import Application
        from src.hk.telegram import register_hk_commands

        app = Application.builder().token(bot_token).build()
        register_hk_commands(app, predictor)

        logger.info("Starting Telegram bot polling (on-demand mode, no scheduled pushes)...")
        async with app:
            await app.start()
            from telegram import BotCommand
            await app.bot.set_my_commands([
                BotCommand("hk_help", "HK 期权监控使用说明"),
            ])
            await app.updater.start_polling(drop_pending_updates=True)
            try:
                while True:
                    await asyncio.sleep(3600)
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
            finally:
                await app.updater.stop()
                await app.stop()
    else:
        logger.warning("Telegram not configured — standalone mode requires TELEGRAM_BOT_TOKEN/CHAT_ID")
        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass

    await predictor.close()
    logger.info("HKPredictor shutdown")


def main() -> None:
    from dotenv import load_dotenv
    load_dotenv()
    asyncio.run(_main())


if __name__ == "__main__":
    main()
