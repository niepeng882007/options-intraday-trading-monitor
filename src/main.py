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
from src.collector.yahoo import YahooCollector
from src.indicator.engine import IndicatorEngine
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
            data_source=config.get("data_source", "yahoo"),
        )
        self.scheduler = AsyncIOScheduler(timezone="America/New_York")
        self._shutdown_event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_entry_eval: dict[str, float] = {}  # P5.1 dedup timestamps
        self._daily_pnl: float = 0.0  # cumulative daily stock PnL %
        self._daily_pnl_date: str = ""  # current tracking date
        self.us_playbook = self._build_us_playbook()

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
        source = self.config.get("data_source", "yahoo")
        if source == "yahoo":
            return YahooCollector()
        elif source == "futu":
            from src.collector.futu import FutuCollector
            futu_cfg = self.config.get("futu", {})
            return FutuCollector(
                host=futu_cfg.get("host", "127.0.0.1"),
                port=futu_cfg.get("port", 11111),
                subscription_quota=futu_cfg.get("subscription_quota", 300),
            )
        raise ValueError(f"Unknown data source: {source}")

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

        # US Playbook integration
        if self.us_playbook and isinstance(self.us_playbook, dict):
            from src.us_playbook.main import USPlaybook as USPlaybookCls
            pb_cfg = self.us_playbook
            self.us_playbook = USPlaybookCls(pb_cfg, self.collector)
            self.us_playbook.set_send_fn(self.notifier.send_text)
            from src.us_playbook.telegram import register_us_playbook_commands
            register_us_playbook_commands(self.notifier._app, self.us_playbook)
            logger.info("US Playbook module initialized")

        self._register_jobs()
        self.scheduler.start()

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

    def _register_jobs(self) -> None:
        poll = self.config.get("polling", {})

        # If futu push subscription is enabled, use push instead of polling for quotes
        use_push = self.config.get("futu", {}).get("use_push_subscription", False)
        if use_push and self.config.get("data_source") == "futu":
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

        # US Playbook scheduled pushes
        if self.us_playbook and not isinstance(self.us_playbook, dict):
            from apscheduler.triggers.cron import CronTrigger
            self.scheduler.add_job(
                self.us_playbook.run_playbook_cycle,
                CronTrigger(hour=9, minute=45, day_of_week="mon-fri", timezone="America/New_York"),
                kwargs={"update_type": "morning"}, id="us_playbook_morning",
            )
            self.scheduler.add_job(
                self.us_playbook.run_playbook_cycle,
                CronTrigger(hour=10, minute=15, day_of_week="mon-fri", timezone="America/New_York"),
                kwargs={"update_type": "confirm"}, id="us_playbook_confirm",
            )
            logger.info("US Playbook scheduled: 09:45/10:15 ET")

            # Regime monitor: interval job (window guard is inside the method)
            monitor_cfg = self.us_playbook._cfg.get("regime_monitor", {})
            if monitor_cfg.get("enabled", True):
                interval = monitor_cfg.get("check_interval_seconds", 300)
                self.scheduler.add_job(
                    self.us_playbook.run_regime_monitor_cycle,
                    "interval", seconds=interval,
                    id="us_regime_monitor", max_instances=1,
                )
                logger.info("US Regime monitor scheduled: every %ds", interval)

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
        if not self._is_trading_hours():
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
        if not self._is_trading_hours():
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
        if not self._is_trading_hours():
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
        if not self._is_trading_hours():
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
            detail={"reason": exit_signal.exit_reason},
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
        """Periodic check of data source connection health."""
        if self.config.get("data_source") != "futu":
            return
        try:
            await self.collector.health_check()
        except Exception:
            logger.error("Health check failed, attempting reconnect")
            await self.notifier.send_text("⚠️ Futu 连接异常，正在重连...")
            try:
                await self.collector.close()
            except Exception:
                pass
            try:
                await self.collector.connect()
                await self.notifier.send_text("✅ Futu 重连成功")
            except Exception:
                logger.exception("Reconnect failed")
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
