from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.collector.base import BaseCollector
from src.indicator.engine import IndicatorEngine
from telegram import Update
from telegram.ext import ContextTypes

from src.notification.telegram import TelegramNotifier
from src.store.redis_store import RedisStore
from src.store.sqlite_store import SQLiteStore
from src.strategy.loader import StrategyLoader
from src.strategy.matcher import RuleMatcher
from src.strategy.state import StrategyState, StrategyStateManager
from src.utils.logger import setup_logger

logger = setup_logger("main")

ET = timezone(timedelta(hours=-5))


def load_config(path: str = "config/settings.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh)

    config["telegram"]["bot_token"] = os.environ.get(
        "TELEGRAM_BOT_TOKEN", config["telegram"].get("bot_token", "")
    )
    config["telegram"]["chat_id"] = os.environ.get(
        "TELEGRAM_CHAT_ID", config["telegram"].get("chat_id", "")
    )
    redis_env = os.environ.get("REDIS_URL")
    if redis_env:
        config["redis"]["url"] = redis_env

    return config


class OptionsMonitor:
    """Main application — wires all components and runs the event loop."""

    def __init__(self, config: dict) -> None:
        self.config = config
        self.collector: BaseCollector = self._build_collector()
        self.redis_store = RedisStore(
            url=config["redis"]["url"],
            max_connections=config["redis"].get("max_connections", 10),
        )
        self.sqlite_store = SQLiteStore(db_path=config.get("sqlite", {}).get("db_path", "data/monitor.db"))
        self.indicator_engine = IndicatorEngine()
        self.strategy_loader = StrategyLoader(config.get("strategies_dir", "config/strategies"))
        self.rule_matcher = RuleMatcher()
        self.state_manager = StrategyStateManager()
        self.notifier = TelegramNotifier(
            bot_token=config["telegram"]["bot_token"],
            chat_id=config["telegram"]["chat_id"],
            collector=self.collector,
            strategy_loader=self.strategy_loader,
            state_manager=self.state_manager,
            sqlite_store=self.sqlite_store,
        )
        self.scheduler = AsyncIOScheduler(timezone="America/New_York")
        self._shutdown_event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._futu_connected: bool = True
        self._last_entry_eval: dict[str, float] = {}  # P5.1 dedup timestamps
        self._daily_pnl: float = 0.0  # cumulative daily stock PnL %
        self._daily_pnl_date: str = ""  # current tracking date
        self.us_playbook = self._build_us_playbook()
        self.hk_playbook = self._build_hk_playbook()

    def _build_hk_playbook(self):
        """Build HK Playbook module if config exists."""
        config_path = "config/hk_settings.yaml"
        try:
            from src.hk.main import HKPredictor
            return HKPredictor(config_path)
        except FileNotFoundError:
            return None
        except Exception:
            logger.warning("Failed to initialize HK Playbook", exc_info=True)
            return None

    def _build_us_playbook(self):
        """Build US Playbook module if config exists."""
        config_path = "config/us_playbook_settings.yaml"
        try:
            with open(config_path) as f:
                pb_cfg = yaml.safe_load(f)
            if pb_cfg:
                return pb_cfg  # store config; instantiate after collector is ready
        except FileNotFoundError:
            pass
        return None

    def _build_collector(self) -> BaseCollector:
        from src.collector.futu import FutuCollector
        futu_cfg = self.config.get("futu", {})
        return FutuCollector(
            host=futu_cfg.get("host", "127.0.0.1"),
            port=futu_cfg.get("port", 11111),
            subscription_quota=futu_cfg.get("subscription_quota", 300),
        )

    # ── Lifecycle ──

    async def start(self) -> None:
        logger.info("Starting Options Monitor...")
        self._loop = asyncio.get_running_loop()

        await self.redis_store.connect()
        self.sqlite_store.connect()
        await self.collector.connect()

        self.strategy_loader.load_all()
        self.strategy_loader.on_change(self._on_strategy_change)
        self.strategy_loader.start_watching()

        self._restore_states()

        self.notifier.build_app()
        await self.notifier.start_polling()

        # US Predictor integration (on-demand + auto-scan)
        if self.us_playbook and isinstance(self.us_playbook, dict):
            from src.us_playbook.main import USPredictor
            pb_cfg = self.us_playbook
            self.us_playbook = USPredictor(pb_cfg, self.collector)
            from src.us_playbook.telegram import register_us_predictor_handlers
            register_us_predictor_handlers(self.notifier._app, self.us_playbook)
            logger.info("US Predictor module initialized (on-demand + auto-scan)")

        # HK Playbook integration (on-demand, no scheduled pushes)
        if self.hk_playbook:
            try:
                await self.hk_playbook.connect()
                from src.hk.telegram import register_hk_predictor_handlers
                register_hk_predictor_handlers(self.notifier._app, self.hk_playbook)
                logger.info("HK Playbook module initialized (on-demand mode)")
            except Exception:
                logger.warning("Failed to connect HK Playbook", exc_info=True)
                self.hk_playbook = None

        # /summary command — manual daily summary trigger
        from telegram.ext import CommandHandler
        self.notifier._app.add_handler(CommandHandler("summary", self._cmd_summary))

        # Quick keyboard commands — /kb, /kboff, /start
        self.notifier._app.add_handler(CommandHandler("kb", self._cmd_keyboard))
        self.notifier._app.add_handler(CommandHandler("start", self._cmd_keyboard))
        self.notifier._app.add_handler(CommandHandler("kboff", self._cmd_keyboard_off))

        self._register_jobs()
        self.scheduler.start()

        await self._setup_telegram_menu()

        logger.info("Options Monitor started — monitoring %d symbols with %d strategies",
                     len(self.strategy_loader.get_all_symbols()),
                     len(self.strategy_loader.get_active()))

        await self.notifier.send_text(
            "🟢 <b>系统已启动</b>\n"
            f"📋 策略: {len(self.strategy_loader.get_active())} 个活跃\n"
            f"📌 标的: {', '.join(sorted(self.strategy_loader.get_all_symbols()))}\n"
            f"⏱ {datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S')} ET"
        )

    async def stop(self) -> None:
        logger.info("Shutting down Options Monitor...")
        self.scheduler.shutdown(wait=False)
        self.strategy_loader.stop_watching()
        self._persist_states()
        await self.notifier.stop()
        if self.hk_playbook:
            try:
                await self.hk_playbook.close()
            except Exception:
                pass
        await self.collector.close()
        await self.redis_store.close()
        self.sqlite_store.close()
        logger.info("Options Monitor stopped")

    async def run_forever(self) -> None:
        await self.start()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._shutdown_event.set)
        await self._shutdown_event.wait()
        await self.stop()

    # ── Scheduled jobs ──

    async def _setup_telegram_menu(self) -> None:
        """Register the bot command menu based on all added handlers."""
        app = self.notifier._app
        if not app or not app.bot:
            return
        
        from telegram import BotCommand
        commands = [
            BotCommand("status", "系统运行和持仓状态"),
            BotCommand("market", "获取监控列表的实时行情"),
            BotCommand("summary", "生成并发送每日交易汇总"),
            BotCommand("strategies", "查看所有策略及其状态"),
            BotCommand("history", "查看最近10条信号记录"),
            BotCommand("pause", "暂停/恢复通知推送 (参数: 分钟数)"),
            BotCommand("chain", "查询期权链 (如 /chain AAPL 230 C 0321)"),
            BotCommand("hk_help", "港股期权监控说明"),
            BotCommand("us_help", "美股期权监控说明"),
            BotCommand("conn", "检查Futu和Redis连接状态"),
            BotCommand("kb", "显示快捷查询键盘"),
            BotCommand("kboff", "关闭快捷键盘"),
        ]
        try:
            await app.bot.set_my_commands(commands)
            logger.info("Telegram bot commands menu updated")
        except Exception as e:
            logger.warning("Failed to set TG commands menu: %s", e)

    def _register_jobs(self) -> None:
        poll = self.config.get("polling", {})

        # If futu push subscription is enabled, use push instead of polling for quotes
        use_push = self.config.get("futu", {}).get("use_push_subscription", False)
        if use_push:
            symbols = list(
                self.strategy_loader.get_all_symbols()
                | set(self.config.get("watchlist", {}).get("symbols", []))
            )
            self.collector.subscribe_quotes(symbols, self._on_quote_push)
            self.collector.subscribe_kline(symbols, self._on_kline_push)
            logger.info("Using Futu push subscription for %d symbols (QUOTE+K_1M)", len(symbols))
        else:
            self.scheduler.add_job(
                self._poll_stock_quotes,
                "interval",
                seconds=poll.get("stock_quote_interval_seconds", 10),
                id="stock_quotes",
                max_instances=1,
            )
        self.scheduler.add_job(
            self._poll_history,
            "interval",
            seconds=poll.get("history_interval_seconds", 300),
            id="history",
            max_instances=1,
        )
        self.scheduler.add_job(
            self._check_timeouts,
            "interval",
            seconds=30,
            id="timeout_check",
            max_instances=1,
        )
        self.scheduler.add_job(
            self._persist_states,
            "interval",
            seconds=60,
            id="persist_states",
            max_instances=1,
        )
        self.scheduler.add_job(
            self._health_check,
            "interval",
            seconds=60,
            id="health_check",
            max_instances=1,
        )
        self.scheduler.add_job(
            self._heartbeat,
            "interval",
            seconds=300,
            id="heartbeat",
            max_instances=1,
        )

        # US Predictor auto-scan
        if self.us_playbook and not isinstance(self.us_playbook, dict):
            scan_cfg = self.us_playbook._cfg.get("auto_scan", {})
            if scan_cfg.get("enabled", False):
                interval = scan_cfg.get("interval_seconds", 180)
                self.scheduler.add_job(
                    self._us_auto_scan,
                    "interval",
                    seconds=interval,
                    id="us_auto_scan",
                    max_instances=1,
                )
                logger.info("US Auto-scan scheduled: every %ds", interval)

        # Daily summary report at 16:05 ET (Mon-Fri)
        self.scheduler.add_job(
            self._send_daily_summary,
            "cron",
            hour=16, minute=5, day_of_week="mon-fri",
            id="daily_summary",
            max_instances=1,
        )

        # HK Auto-scan: morning breakout scanner
        if self.hk_playbook:
            scan_cfg = self.hk_playbook._cfg.get("auto_scan", {})
            if scan_cfg.get("enabled", False):
                interval = scan_cfg.get("interval_seconds", 300)
                self.scheduler.add_job(
                    self._hk_auto_scan,
                    "interval",
                    seconds=interval,
                    id="hk_auto_scan",
                    max_instances=1,
                )
                logger.info("HK Auto-scan scheduled: every %ds", interval)

    async def _us_auto_scan(self) -> None:
        """Run US auto-scan (window/weekday check is inside run_auto_scan)."""
        await self.us_playbook.run_auto_scan(self.notifier.send_text)

    async def _hk_auto_scan(self) -> None:
        """Run HK auto-scan (window/weekday check is inside run_auto_scan)."""
        await self.hk_playbook.run_auto_scan(self.notifier.send_text)

    def _is_trading_hours(self) -> bool:
        now = datetime.now(ET)
        hours = self.config.get("trading_hours", {})
        open_str = hours.get("market_open", "09:30")
        close_str = hours.get("market_close", "16:00")
        pre_min = hours.get("pre_market_minutes", 30)

        open_h, open_m = map(int, open_str.split(":"))
        close_h, close_m = map(int, close_str.split(":"))

        market_open = now.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
        market_close = now.replace(hour=close_h, minute=close_m, second=0, microsecond=0)
        pre_market = market_open - timedelta(minutes=pre_min)

        if now.weekday() >= 5:
            return False
        return pre_market <= now <= market_close

    def _minutes_to_close(self) -> int:
        now = datetime.now(ET)
        hours = self.config.get("trading_hours", {})
        close_str = hours.get("market_close", "16:00")
        close_h, close_m = map(int, close_str.split(":"))
        market_close = now.replace(hour=close_h, minute=close_m, second=0, microsecond=0)
        delta = (market_close - now).total_seconds()
        return max(0, int(delta / 60))

    # ── Polling tasks ──

    async def _poll_stock_quotes(self) -> None:
        if not self._futu_connected or not self._is_trading_hours():
            return

        symbols = self.strategy_loader.get_all_symbols() | set(
            self.config.get("watchlist", {}).get("symbols", [])
        )
        for symbol in symbols:
            try:
                quote = await self.collector.get_stock_quote(symbol)
                await self.redis_store.publish_quote(quote)

                for holding in self.state_manager.get_holding_positions():
                    if holding.symbol == symbol:
                        self.state_manager.update_highest_price(
                            holding.strategy_id, symbol, quote.price
                        )
                        await self._evaluate_exit(holding.strategy_id, symbol, quote.price)

                indicators = self.indicator_engine.update_live_price(
                    symbol, quote.price, quote.timestamp
                )
                if any(v is not None for v in indicators.values()):
                    await self._evaluate_entries(symbol, indicators)
            except Exception:
                logger.exception("Failed to poll quote for %s", symbol)

    async def _on_quote_push(self, quote) -> None:
        """Callback for Futu real-time quote push — processes a single symbol."""
        if not self._futu_connected or not self._is_trading_hours():
            return
        symbol = quote.symbol
        try:
            await self.redis_store.publish_quote(quote)

            for holding in self.state_manager.get_holding_positions():
                if holding.symbol == symbol:
                    self.state_manager.update_highest_price(
                        holding.strategy_id, symbol, quote.price
                    )
                    await self._evaluate_exit(holding.strategy_id, symbol, quote.price)

            indicators = self.indicator_engine.update_live_price(
                symbol, quote.price, quote.timestamp
            )
            if any(v is not None for v in indicators.values()):
                await self._evaluate_entries(symbol, indicators)
        except Exception:
            logger.exception("Failed to process push quote for %s", symbol)

    async def _on_kline_push(self, symbol: str, kline_df) -> None:
        """Callback for Futu real-time 1-minute K-line push."""
        if not self._futu_connected or not self._is_trading_hours():
            return
        try:
            self.indicator_engine.update_bars(symbol, kline_df)

            results = self.indicator_engine.calculate_all(symbol)
            for tf, result in results.items():
                if result:
                    await self.redis_store.publish_indicators(symbol, tf, result.to_dict())

            await self._evaluate_entries(symbol, results)
        except Exception:
            logger.exception("Failed to process kline push for %s", symbol)

    async def _poll_history(self) -> None:
        if not self._futu_connected or not self._is_trading_hours():
            return

        symbols = self.strategy_loader.get_all_symbols() | set(
            self.config.get("watchlist", {}).get("symbols", [])
        )
        for symbol in symbols:
            try:
                period = "5d" if self.indicator_engine.needs_warmup(symbol) else "1d"
                if period == "5d":
                    logger.info("Warming up indicators for %s (fetching 5d history)", symbol)

                df = await self.collector.get_history(symbol, interval="1m", period=period)
                if df.empty:
                    continue

                self.indicator_engine.update_bars(symbol, df)

                history_json = df.tail(5).to_json()
                await self.redis_store.publish_history(symbol, history_json)

                results = self.indicator_engine.calculate_all(symbol)
                for tf, result in results.items():
                    if result:
                        await self.redis_store.publish_indicators(symbol, tf, result.to_dict())
                        self.sqlite_store.save_indicators(symbol, tf, result.to_dict(), result.timestamp)

                await self._evaluate_entries(symbol, results)
            except Exception:
                logger.exception("Failed to poll history for %s", symbol)

    # ── Risk management helpers ──

    def _is_midday_blocked(self, now: datetime) -> bool:
        """Check if current time falls in the midday no-trade window."""
        rm = self.config.get("risk_management", {})
        midday = rm.get("midday_no_trade", {})
        if not midday.get("enabled", False):
            return False
        start_str = midday.get("start", "11:00")
        end_str = midday.get("end", "13:00")
        s_h, s_m = map(int, start_str.split(":"))
        e_h, e_m = map(int, end_str.split(":"))
        t = now.hour * 60 + now.minute
        return s_h * 60 + s_m <= t < e_h * 60 + e_m

    def _check_daily_loss_limit(self) -> bool:
        """Return True if daily loss limit has been breached."""
        rm = self.config.get("risk_management", {})
        limit = rm.get("max_daily_loss_pct")
        if limit is None:
            return False
        return self._daily_pnl <= limit

    def _track_exit_pnl(self, strategy_id: str, entry_price: float, exit_price: float) -> None:
        """Accumulate daily PnL from a closed trade."""
        strategy = self.strategy_loader.get(strategy_id)
        if strategy is None:
            return
        today = datetime.now(ET).strftime("%Y-%m-%d")
        if self._daily_pnl_date != today:
            self._daily_pnl = 0.0
            self._daily_pnl_date = today
        stock_pnl = (exit_price - entry_price) / entry_price * 100 if entry_price > 0 else 0.0
        direction = strategy.option_filter.get("type", "call")
        self._daily_pnl += stock_pnl if direction == "call" else -stock_pnl

    # ── Strategy evaluation ──

    @staticmethod
    def _is_in_trading_window(strategy, now: datetime) -> bool:
        start_str = strategy.trading_window_start
        end_str = strategy.trading_window_end
        if not start_str or not end_str:
            return True

        start_h, start_m = map(int, start_str.split(":"))
        end_h, end_m = map(int, end_str.split(":"))

        window_start = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
        window_end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
        return window_start <= now <= window_end

    async def _evaluate_entries(self, symbol: str, indicators_by_tf: dict) -> None:
        now = datetime.now(ET)
        now_ts = time.time()

        # P5.2: Data staleness check — skip if indicators are older than 10 minutes
        latest_ind_ts = 0.0
        for ind in indicators_by_tf.values():
            if ind and ind.timestamp > latest_ind_ts:
                latest_ind_ts = ind.timestamp
        if latest_ind_ts > 0 and now_ts - latest_ind_ts > 600:
            logger.warning("Stale indicators for %s (%.0fs old), skipping", symbol, now_ts - latest_ind_ts)
            return

        # P0: Midday no-trade filter (11:00-13:00 ET)
        if self._is_midday_blocked(now):
            return

        # P2: Daily loss circuit breaker
        if self._check_daily_loss_limit():
            logger.warning("Daily loss limit breached (%.2f%%), no new entries", self._daily_pnl)
            return

        for strategy in self.strategy_loader.get_active():
            if symbol not in strategy.underlyings:
                continue

            if not self._is_in_trading_window(strategy, now):
                continue

            # P5.1: Per-(strategy, symbol) 5-second dedup
            dedup_key = f"{strategy.strategy_id}:{symbol}"
            if now_ts - self._last_entry_eval.get(dedup_key, 0) < 5:
                continue
            self._last_entry_eval[dedup_key] = now_ts

            state = self.state_manager.get_state(strategy.strategy_id, symbol)
            if state.state != StrategyState.WATCHING:
                continue

            if await self.redis_store.is_in_cooldown(strategy.strategy_id, symbol):
                continue

            # P2.2: Market context filters
            mcf = strategy.market_context_filters
            if mcf:
                skip = False
                spy_ind = self.indicator_engine.get_last("SPY", "5m")
                if spy_ind:
                    max_drop = mcf.get("max_spy_day_drop_pct")
                    if max_drop is not None and spy_ind.day_change_pct is not None:
                        if spy_ind.day_change_pct < max_drop:
                            logger.debug(
                                "Market filter: SPY drop %.2f%% < %.2f%%, skip %s",
                                spy_ind.day_change_pct, max_drop, strategy.strategy_id,
                            )
                            skip = True
                max_adx = mcf.get("max_adx")
                if not skip and max_adx is not None:
                    sym_ind = indicators_by_tf.get("5m")
                    if sym_ind and sym_ind.adx is not None and sym_ind.adx > max_adx:
                        logger.debug(
                            "Market filter: ADX %.1f > %.1f, skip %s",
                            sym_ind.adx, max_adx, strategy.strategy_id,
                        )
                        skip = True
                min_adx = mcf.get("min_adx")
                if not skip and min_adx is not None:
                    sym_ind = indicators_by_tf.get("5m")
                    if sym_ind and sym_ind.adx is not None and sym_ind.adx < min_adx:
                        logger.debug(
                            "Market filter: ADX %.1f < %.1f (min), skip %s",
                            sym_ind.adx, min_adx, strategy.strategy_id,
                        )
                        skip = True
                if skip:
                    continue

            entry_signal = self.rule_matcher.evaluate_entry(strategy, symbol, indicators_by_tf)
            if entry_signal is None:
                continue

            quality = self.rule_matcher.evaluate_entry_quality(strategy, indicators_by_tf)
            min_score = strategy.entry_quality_filters.get("min_score", 0)
            if quality.score < min_score:
                logger.info(
                    "Entry rejected by quality filter: %s %s score=%d/%d grade=%s — %s",
                    strategy.strategy_id, symbol, quality.score, min_score,
                    quality.grade, "; ".join(quality.reasons),
                )
                continue

            entry_signal.entry_quality = quality

            signal_id = self.state_manager.trigger_entry(strategy.strategy_id, symbol)
            if signal_id is None:
                continue

            quote = await self.redis_store.get_quote(symbol)
            underlying_price = quote.get("price", 0) if quote else 0

            await self.notifier.send_entry_signal(
                entry_signal,
                signal_id,
                underlying_price=underlying_price,
                quote_detail=quote,
                indicators_by_tf=indicators_by_tf,
                exit_conditions=strategy.exit_conditions,
                option_filter=strategy.option_filter,
                risk_config=self.config.get("risk_management"),
            )
            await self.redis_store.set_cooldown(strategy.strategy_id, symbol, strategy.cooldown_seconds)

            self.sqlite_store.save_signal(
                signal_id=signal_id,
                strategy_id=strategy.strategy_id,
                strategy_name=strategy.name,
                signal_type="entry",
                symbol=symbol,
                detail={
                    "conditions": entry_signal.conditions_detail,
                    "quality_score": quality.score,
                    "quality_grade": quality.grade,
                    "underlying_price": underlying_price,
                },
            )
            logger.info(
                "Entry signal sent: %s %s signal=%s quality=%s(%d)",
                strategy.strategy_id, symbol, signal_id, quality.grade, quality.score,
            )

    async def _evaluate_exit(self, strategy_id: str, symbol: str, current_price: float) -> None:
        strategy = self.strategy_loader.get(strategy_id)
        if strategy is None:
            return

        state = self.state_manager.get_state(strategy_id, symbol)
        if state.state != StrategyState.HOLDING:
            return

        entry_price = state.position.entry_price
        minutes_to_close = self._minutes_to_close()

        direction = strategy.option_filter.get("type", "call")
        indicators_by_tf = self.indicator_engine.calculate_all(symbol)
        exit_signal = self.rule_matcher.evaluate_exit(
            strategy, symbol, current_price, entry_price, minutes_to_close,
            highest_price=state.position.highest_price,
            lowest_price=state.position.lowest_price,
            direction=direction,
            indicators_by_tf=indicators_by_tf,
        )
        if exit_signal is None:
            return

        self.state_manager.trigger_exit(strategy_id, symbol)
        self._track_exit_pnl(strategy_id, entry_price, current_price)

        hold_seconds = time.time() - state.position.entry_timestamp
        hold_h = int(hold_seconds // 3600)
        hold_m = int((hold_seconds % 3600) // 60)
        hold_duration = f"{hold_h}h {hold_m}m" if hold_h else f"{hold_m}m"

        quote = await self.redis_store.get_quote(symbol)
        underlying_price = quote.get("price", 0) if quote else current_price

        await self.notifier.send_exit_signal(
            exit_signal,
            underlying_price=underlying_price,
            entry_price=entry_price,
            current_price=current_price,
            hold_duration=hold_duration,
            cooldown_seconds=strategy.cooldown_seconds,
            daily_pnl=self._daily_pnl,
            option_type=strategy.option_filter.get("type", "call"),
        )

        self.state_manager.confirm_exit(strategy_id, symbol)

        self.sqlite_store.save_signal(
            signal_id=f"EXIT-{int(time.time())}",
            strategy_id=strategy_id,
            strategy_name=strategy.name,
            signal_type="exit",
            symbol=symbol,
            detail={
                "reason": exit_signal.exit_reason,
                "entry_price": entry_price,
                "exit_price": current_price,
                "direction": direction,
            },
        )
        logger.info("Exit signal sent: %s %s reason=%s", strategy_id, symbol, exit_signal.exit_reason)

    # ── Timeout / state persistence ──

    async def _check_timeouts(self) -> None:
        timed_out = self.state_manager.check_timeouts()
        for entry in timed_out:
            await self.notifier.send_text(
                f"⏰ 入场信号超时: {entry.strategy_id}:{entry.symbol}\n"
                f"信号 {entry.signal_id} 未确认，已重置为 WATCHING"
            )

    async def _health_check(self) -> None:
        """Periodic check of Futu connection health.

        On disconnect: sets _futu_connected=False and pauses signal processing.
        On reconnect: sets _futu_connected=True and sends recovery notification.
        """
        was_connected = self._futu_connected
        try:
            await self.collector.health_check()
            # Health check passed — restore if previously disconnected
            if not self._futu_connected:
                self._futu_connected = True
                logger.info("Futu connection restored")
                await self.notifier.send_text("✅ Futu 连接已恢复，信号推送已恢复")
        except Exception:
            logger.error("Health check failed, attempting reconnect")
            self._futu_connected = False
            if was_connected:
                await self.notifier.send_text(
                    "⚠️ Futu 连接断开，信号推送已暂停\n正在尝试重连..."
                )
            try:
                await self.collector.close()
            except Exception:
                pass
            try:
                await self.collector.connect()
                self._futu_connected = True
                logger.info("Futu reconnect succeeded")
                await self.notifier.send_text("✅ Futu 重连成功，信号推送已恢复")
            except Exception:
                logger.exception("Reconnect failed")
                if was_connected:
                    await self.notifier.send_text("❌ Futu 重连失败，请检查 FutuOpenD")

    async def _heartbeat(self) -> None:
        """Periodic heartbeat log for liveness monitoring."""
        active = len(self.strategy_loader.get_active())
        holding = len(self.state_manager.get_holding_positions())
        logger.info(
            "Heartbeat: strategies=%d, holding=%d, daily_pnl=%.2f%%",
            active, holding, self._daily_pnl,
        )

    def _persist_states(self) -> None:
        states = self.state_manager.export_all()
        if states:
            self.sqlite_store.save_strategy_states(states)
        # P1.3: Persist prev_values for crosses_*/turns_* continuity
        prev_data = self.rule_matcher.export_prev_values()
        if prev_data:
            self.sqlite_store.save_prev_values(prev_data)

    def _restore_states(self) -> None:
        states = self.sqlite_store.load_strategy_states()
        if states:
            self.state_manager.import_all(states)
            logger.info("Restored %d strategy states from SQLite", len(states))
        # P1.3: Restore prev_values
        prev_data = self.sqlite_store.load_prev_values()
        if prev_data:
            self.rule_matcher.import_prev_values(prev_data)

    # ── Daily summary ──

    async def _send_daily_summary(self) -> None:
        from src.common.daily_report import collect_pipeline_data, format_daily_summary
        data = collect_pipeline_data(self.sqlite_store, self._daily_pnl)
        await self.notifier.send_text(format_daily_summary(data))

    async def _cmd_summary(self, update, context) -> None:
        await self._send_daily_summary()

    async def _cmd_keyboard(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/kb or /start — show combined quick-access keyboard."""
        from src.common.telegram_handlers import build_combined_keyboard
        text, markup = build_combined_keyboard(
            us_predictor_key="us_predictor",
            hk_predictor_key="hk_predictor",
            bot_data=context.bot_data,
        )
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)

    async def _cmd_keyboard_off(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/kboff — hide quick-access keyboard."""
        from telegram import ReplyKeyboardRemove
        await update.message.reply_text(
            "⌨️ 快捷键盘已关闭。发送 /kb 重新开启。",
            reply_markup=ReplyKeyboardRemove(),
        )

    # ── Hot-reload callback ──

    def _on_strategy_change(self, strategy_id: str, config) -> None:
        self.state_manager.reset(strategy_id)
        if config:
            status = "🟢 已启用" if config.enabled else "🔴 已禁用"
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = self._loop
            asyncio.run_coroutine_threadsafe(
                self.notifier.send_strategy_update(strategy_id, config.name, status),
                loop,
            )


async def main() -> None:
    config = load_config()
    monitor = OptionsMonitor(config)
    await monitor.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
