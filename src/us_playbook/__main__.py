"""Standalone entry point for US Predictor.

Usage: python -m src.us_playbook
"""

import asyncio
import os
import signal
from contextlib import suppress

from dotenv import load_dotenv
load_dotenv()

import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.error import NetworkError
from telegram.ext import Application
from telegram.request import HTTPXRequest

from src.us_playbook.main import USPredictor
from src.us_playbook.telegram import register_us_predictor_handlers
from src.utils.logger import setup_logger

logger = setup_logger("us_playbook_main")

TELEGRAM_READ_TIMEOUT_SECONDS = 30
TELEGRAM_WRITE_TIMEOUT_SECONDS = 30
TELEGRAM_CONNECT_TIMEOUT_SECONDS = 15
TELEGRAM_POOL_TIMEOUT_SECONDS = 5
TELEGRAM_POLL_START_RETRIES = 3
TELEGRAM_POLL_RETRY_BASE_SECONDS = 2


def _load_config(path: str = "config/us_playbook_settings.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _build_telegram_application(bot_token: str) -> Application:
    proxy_url = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
    request = HTTPXRequest(
        read_timeout=TELEGRAM_READ_TIMEOUT_SECONDS,
        write_timeout=TELEGRAM_WRITE_TIMEOUT_SECONDS,
        connect_timeout=TELEGRAM_CONNECT_TIMEOUT_SECONDS,
        pool_timeout=TELEGRAM_POOL_TIMEOUT_SECONDS,
        proxy=proxy_url,
    )
    get_updates_request = HTTPXRequest(
        read_timeout=TELEGRAM_READ_TIMEOUT_SECONDS,
        write_timeout=TELEGRAM_WRITE_TIMEOUT_SECONDS,
        connect_timeout=TELEGRAM_CONNECT_TIMEOUT_SECONDS,
        pool_timeout=TELEGRAM_POOL_TIMEOUT_SECONDS,
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


async def main() -> None:
    from src.collector.futu import FutuCollector

    cfg = _load_config()
    futu_cfg = cfg.get("futu", {})

    collector = FutuCollector(
        host=os.getenv("FUTU_HOST", futu_cfg.get("host", "127.0.0.1")),
        port=futu_cfg.get("port", 11111),
    )
    await collector.connect()
    await collector.start_watchdog()

    predictor = USPredictor(cfg, collector)

    from src.store import message_archive
    message_archive.init("data/monitor.db")

    # Optional: Telegram integration
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    app = None

    if bot_token and chat_id:
        app = _build_telegram_application(bot_token)
        register_us_predictor_handlers(app, predictor)
        await _start_telegram_polling(app)

    # Auto-scan scheduler
    scheduler = AsyncIOScheduler(timezone="America/New_York")
    scan_cfg = cfg.get("auto_scan", {})
    if scan_cfg.get("enabled", False):
        interval = scan_cfg.get("interval_seconds", 180)

        async def _send_fn(text: str, parse_mode: str = "HTML") -> None:
            if app and chat_id:
                await app.bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)
                message_archive.log("us_playbook", "auto_scan", text, "us")
            else:
                logger.info("Auto-scan (no TG): %s", text[:200])

        scheduler.add_job(
            predictor.run_auto_scan, "interval",
            seconds=interval,
            kwargs={"send_fn": _send_fn},
            id="us_auto_scan", max_instances=1,
        )
        logger.info("Auto-scan scheduled: every %ds", interval)

    scheduler.start()
    logger.info("US Predictor started — on-demand + auto-scan mode")

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown.set)
    await shutdown.wait()

    scheduler.shutdown(wait=False)
    if app:
        await _shutdown_telegram_application(app)
    await collector.close()
    message_archive.close()
    logger.info("US Predictor stopped")


if __name__ == "__main__":
    asyncio.run(main())
