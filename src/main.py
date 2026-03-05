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
        )
        self.scheduler = AsyncIOScheduler(timezone="America/New_York")
        self._shutdown_event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    def _build_collector(self) -> BaseCollector:
        source = self.config.get("data_source", "yahoo")
        if source == "yahoo":
            return YahooCollector()
        raise ValueError(f"Unknown data source: {source}")

    # ── Lifecycle ──

    async def start(self) -> None:
        logger.info("Starting Options Monitor...")
        self._loop = asyncio.get_running_loop()

        await self.redis_store.connect()
        self.sqlite_store.connect()

        self.strategy_loader.load_all()
        self.strategy_loader.on_change(self._on_strategy_change)
        self.strategy_loader.start_watching()

        self._restore_states()

        self.notifier.build_app()
        await self.notifier.start_polling()

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

        self.scheduler.add_job(
            self._poll_stock_quotes,
            "interval",
            seconds=poll.get("stock_quote_interval_seconds", 10),
            id="stock_quotes",
            max_instances=1,
        )
        self.scheduler.add_job(
            self._poll_option_chains,
            "interval",
            seconds=poll.get("option_chain_interval_seconds", 30),
            id="option_chains",
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

    async def _poll_option_chains(self) -> None:
        if not self._is_trading_hours():
            return

        symbols = self.strategy_loader.get_all_symbols()
        for symbol in symbols:
            try:
                chain = await self.collector.get_option_chain(symbol)
                await self.redis_store.publish_options(symbol, chain)
            except Exception:
                logger.exception("Failed to poll option chain for %s", symbol)

    async def _poll_history(self) -> None:
        if not self._is_trading_hours():
            return

        symbols = self.strategy_loader.get_all_symbols() | set(
            self.config.get("watchlist", {}).get("symbols", [])
        )
        for symbol in symbols:
            try:
                df = await self.collector.get_history(symbol, interval="1m", period="1d")
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

        for strategy in self.strategy_loader.get_active():
            if symbol not in strategy.underlyings:
                continue

            if not self._is_in_trading_window(strategy, now):
                continue

            state = self.state_manager.get_state(strategy.strategy_id, symbol)
            if state.state != StrategyState.WATCHING:
                continue

            if await self.redis_store.is_in_cooldown(strategy.strategy_id, symbol):
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

        exit_signal = self.rule_matcher.evaluate_exit(
            strategy, symbol, current_price, entry_price, minutes_to_close
        )
        if exit_signal is None:
            return

        self.state_manager.trigger_exit(strategy_id, symbol)

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

    def _persist_states(self) -> None:
        states = self.state_manager.export_all()
        if states:
            self.sqlite_store.save_strategy_states(states)

    def _restore_states(self) -> None:
        states = self.sqlite_store.load_strategy_states()
        if states:
            self.state_manager.import_all(states)
            logger.info("Restored %d strategy states from SQLite", len(states))

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
