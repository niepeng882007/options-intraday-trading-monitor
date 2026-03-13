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
TELEGRAM_POOL_TIMEOUT_SECONDS = 10
TELEGRAM_CONNECTION_POOL_SIZE = 8
TELEGRAM_POLL_START_RETRIES = 3
TELEGRAM_POLL_RETRY_BASE_SECONDS = 2


def _build_telegram_application(bot_token: str) -> Application:
    proxy_url = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
    request = HTTPXRequest(
        read_timeout=TELEGRAM_READ_TIMEOUT_SECONDS,
        write_timeout=TELEGRAM_WRITE_TIMEOUT_SECONDS,
        connect_timeout=TELEGRAM_CONNECT_TIMEOUT_SECONDS,
        pool_timeout=TELEGRAM_POOL_TIMEOUT_SECONDS,
        connection_pool_size=TELEGRAM_CONNECTION_POOL_SIZE,
        proxy=proxy_url,
    )
    get_updates_request = HTTPXRequest(
        read_timeout=TELEGRAM_READ_TIMEOUT_SECONDS,
        write_timeout=TELEGRAM_WRITE_TIMEOUT_SECONDS,
        connect_timeout=TELEGRAM_CONNECT_TIMEOUT_SECONDS,
        pool_timeout=TELEGRAM_POOL_TIMEOUT_SECONDS,
        connection_pool_size=TELEGRAM_CONNECTION_POOL_SIZE,
        proxy=proxy_url,
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
    if app._initialized:
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


async def _cmd_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/messages — show last trading day's message archive."""
    import datetime
    from zoneinfo import ZoneInfo
    from src.store import message_archive

    et = ZoneInfo("America/New_York")
    now = datetime.datetime.now(et)

    # Find the last trading day (skip weekends)
    day = now.date() - datetime.timedelta(days=1)
    while day.weekday() >= 5:  # Saturday=5, Sunday=6
        day -= datetime.timedelta(days=1)

    start_dt = datetime.datetime.combine(day, datetime.time(0, 0), tzinfo=et)
    end_dt = datetime.datetime.combine(day, datetime.time(23, 59, 59), tzinfo=et)
    rows = message_archive.query(start_dt.timestamp(), end_dt.timestamp())

    if not rows:
        await update.message.reply_text(
            f"📭 {day.isoformat()} 没有消息记录。",
        )
        return

    lines = [f"📬 <b>{day.isoformat()} 消息归档</b> ({len(rows)} 条)\n"]
    for r in rows:
        ts = datetime.datetime.fromtimestamp(r["timestamp"], tz=et)
        time_str = ts.strftime("%H:%M")
        market = r["market"].upper()
        source = r["source"]
        # Truncate long content
        content = r["content"]
        if len(content) > 120:
            content = content[:120] + "…"
        # Strip HTML tags for summary
        import re
        content = re.sub(r"<[^>]+>", "", content)
        lines.append(f"<code>{time_str}</code> [{market}] {source}: {content}")

    text = "\n".join(lines)
    # Telegram message limit is 4096 chars
    if len(text) > 4000:
        text = text[:4000] + "\n\n…(更多消息已截断)"

    await update.message.reply_text(text, parse_mode="HTML")


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
        host=os.getenv("FUTU_HOST", futu_cfg.get("host", "127.0.0.1")),
        port=futu_cfg.get("port", 11111),
    )
    await collector.connect()
    await collector.start_watchdog()

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

        # Keyboard & utility commands
        app.add_handler(CommandHandler("kb", _cmd_keyboard))
        app.add_handler(CommandHandler("start", _cmd_keyboard))
        app.add_handler(CommandHandler("kboff", _cmd_keyboard_off))
        app.add_handler(CommandHandler("messages", _cmd_messages))

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

            async def _us_send_fn(text: str, parse_mode: str = "HTML", photo: bytes | None = None) -> None:
                if app and chat_id:
                    if photo:
                        try:
                            await app.bot.send_photo(chat_id=chat_id, photo=photo)
                        except Exception:
                            logger.warning("US auto-scan: failed to send chart photo")
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
