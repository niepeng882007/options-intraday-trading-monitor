"""Combined entry point for US Playbook + HK Playbook.

Usage: python -m src.main
"""

from __future__ import annotations

import asyncio
import os
import signal
from contextlib import suppress

from dotenv import load_dotenv
load_dotenv()

import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import BotCommand, Update
from telegram.error import NetworkError
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest

from src.utils.logger import setup_logger

logger = setup_logger("main")

TELEGRAM_READ_TIMEOUT_SECONDS = 30
TELEGRAM_WRITE_TIMEOUT_SECONDS = 30
TELEGRAM_CONNECT_TIMEOUT_SECONDS = 15
TELEGRAM_POOL_TIMEOUT_SECONDS = 5
TELEGRAM_POLL_START_RETRIES = 3
TELEGRAM_POLL_RETRY_BASE_SECONDS = 2


def _build_telegram_application(bot_token: str) -> Application:
    request = HTTPXRequest(
        read_timeout=TELEGRAM_READ_TIMEOUT_SECONDS,
        write_timeout=TELEGRAM_WRITE_TIMEOUT_SECONDS,
        connect_timeout=TELEGRAM_CONNECT_TIMEOUT_SECONDS,
        pool_timeout=TELEGRAM_POOL_TIMEOUT_SECONDS,
    )
    get_updates_request = HTTPXRequest(
        read_timeout=TELEGRAM_READ_TIMEOUT_SECONDS,
        write_timeout=TELEGRAM_WRITE_TIMEOUT_SECONDS,
        connect_timeout=TELEGRAM_CONNECT_TIMEOUT_SECONDS,
        pool_timeout=TELEGRAM_POOL_TIMEOUT_SECONDS,
    )
    return (
        Application.builder()
        .token(bot_token)
        .request(request)
        .get_updates_request(get_updates_request)
        .build()
    )


async def _shutdown_telegram_application(app: Application) -> None:
    updater = app.updater
    if updater and updater.running:
        with suppress(Exception):
            await updater.stop()
    if app.running:
        with suppress(Exception):
            await app.stop()
    if app.initialized:
        with suppress(Exception):
            await app.shutdown()


async def _start_telegram_polling(app: Application) -> None:
    retry_delay_seconds = TELEGRAM_POLL_RETRY_BASE_SECONDS
    for attempt_number in range(1, TELEGRAM_POLL_START_RETRIES + 1):
        try:
            await app.initialize()
            await app.start()
            assert app.updater is not None
            await app.updater.start_polling(drop_pending_updates=True)
            logger.info("Telegram bot started")
            return
        except NetworkError as exc:
            await _shutdown_telegram_application(app)
            if attempt_number >= TELEGRAM_POLL_START_RETRIES:
                raise
            logger.warning(
                "Telegram polling bootstrap failed (%d/%d): %s; retrying in %ds",
                attempt_number,
                TELEGRAM_POLL_START_RETRIES,
                exc,
                retry_delay_seconds,
            )
            await asyncio.sleep(retry_delay_seconds)
            retry_delay_seconds *= 2


async def _cmd_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/kb or /start — show combined quick-access keyboard."""
    from src.common.telegram_handlers import build_combined_keyboard
    context.bot_data["_kb_active"] = True
    text, markup = build_combined_keyboard(
        us_predictor_key="us_predictor",
        hk_predictor_key="hk_predictor",
        bot_data=context.bot_data,
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def _cmd_keyboard_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/kboff — hide quick-access keyboard."""
    from telegram import ReplyKeyboardRemove
    context.bot_data["_kb_active"] = False
    await update.message.reply_text(
        "⌨️ 快捷键盘已关闭。发送 /kb 重新开启。",
        reply_markup=ReplyKeyboardRemove(),
    )


async def main() -> None:
    from src.collector.futu import FutuCollector
    from src.store import message_archive

    # ── Load configs ──
    us_cfg_path = "config/us_playbook_settings.yaml"
    hk_cfg_path = "config/hk_settings.yaml"

    us_cfg = None
    try:
        with open(us_cfg_path) as f:
            us_cfg = yaml.safe_load(f)
    except FileNotFoundError:
        logger.warning("US config not found: %s", us_cfg_path)

    # ── Shared FutuCollector (for US Predictor) ──
    futu_cfg = (us_cfg or {}).get("futu", {})
    collector = FutuCollector(
        host=futu_cfg.get("host", "127.0.0.1"),
        port=futu_cfg.get("port", 11111),
    )
    await collector.connect()

    # ── Initialize predictors ──
    us_predictor = None
    if us_cfg:
        from src.us_playbook.main import USPredictor
        us_predictor = USPredictor(us_cfg, collector)
        logger.info("US Predictor initialized")

    hk_predictor = None
    try:
        from src.hk.main import HKPredictor
        hk_predictor = HKPredictor(hk_cfg_path)
        await hk_predictor.connect()
        logger.info("HK Predictor initialized")
    except FileNotFoundError:
        logger.warning("HK config not found: %s", hk_cfg_path)
    except Exception:
        logger.warning("HK Predictor init failed", exc_info=True)

    # ── Message archive ──
    message_archive.init("data/monitor.db")

    # ── Telegram ──
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    app = None

    if bot_token and chat_id:
        app = _build_telegram_application(bot_token)

        # Register US handlers
        if us_predictor:
            from src.us_playbook.telegram import register_us_predictor_handlers
            register_us_predictor_handlers(app, us_predictor)

        # Register HK handlers
        if hk_predictor:
            from src.hk.telegram import register_hk_predictor_handlers
            register_hk_predictor_handlers(app, hk_predictor)

        # Keyboard commands
        app.add_handler(CommandHandler("kb", _cmd_keyboard))
        app.add_handler(CommandHandler("start", _cmd_keyboard))
        app.add_handler(CommandHandler("kboff", _cmd_keyboard_off))

        await _start_telegram_polling(app)

        # Menu commands
        commands = [
            BotCommand("hk_help", "港股期权监控说明"),
            BotCommand("us_help", "美股期权监控说明"),
            BotCommand("messages", "查看上一交易日消息归档"),
            BotCommand("kb", "显示快捷查询键盘"),
            BotCommand("kboff", "关闭快捷键盘"),
        ]
        try:
            await app.bot.set_my_commands(commands)
            logger.info("Telegram bot commands menu updated")
        except Exception as e:
            logger.warning("Failed to set TG commands menu: %s", e)

    # ── Auto-scan scheduler ──
    scheduler = AsyncIOScheduler(timezone="America/New_York")

    if us_predictor:
        scan_cfg = us_cfg.get("auto_scan", {})
        if scan_cfg.get("enabled", False):
            interval = scan_cfg.get("interval_seconds", 180)

            async def _us_send_fn(text: str, parse_mode: str = "HTML") -> None:
                if app and chat_id:
                    await app.bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)
                    message_archive.log("us_playbook", "auto_scan", text, "us")
                else:
                    logger.info("US auto-scan (no TG): %s", text[:200])

            scheduler.add_job(
                us_predictor.run_auto_scan, "interval",
                seconds=interval,
                kwargs={"send_fn": _us_send_fn},
                id="us_auto_scan", max_instances=1,
            )
            logger.info("US auto-scan scheduled: every %ds", interval)

    if hk_predictor:
        scan_cfg = hk_predictor._cfg.get("auto_scan", {})
        if scan_cfg.get("enabled", False):
            interval = scan_cfg.get("interval_seconds", 300)

            async def _hk_send_fn(text: str, parse_mode: str = "HTML") -> None:
                if app and chat_id:
                    await app.bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)
                    message_archive.log("hk_playbook", "auto_scan", text, "hk")
                else:
                    logger.info("HK auto-scan (no TG): %s", text[:200])

            scheduler.add_job(
                hk_predictor.run_auto_scan, "interval",
                seconds=interval,
                kwargs={"send_fn": _hk_send_fn},
                id="hk_auto_scan", max_instances=1,
            )
            logger.info("HK auto-scan scheduled: every %ds", interval)

    scheduler.start()
    logger.info("Playbook system started — US=%s, HK=%s",
                "ON" if us_predictor else "OFF",
                "ON" if hk_predictor else "OFF")

    # ── Graceful shutdown ──
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown.set)
    await shutdown.wait()

    scheduler.shutdown(wait=False)
    if app:
        await _shutdown_telegram_application(app)
    if hk_predictor:
        with suppress(Exception):
            await hk_predictor.close()
    await collector.close()
    message_archive.close()
    logger.info("Playbook system stopped")


if __name__ == "__main__":
    asyncio.run(main())
