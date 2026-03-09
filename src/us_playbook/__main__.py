"""Standalone entry point for US Playbook.

Usage: python -m src.us_playbook
"""

import asyncio
import os
import signal

from dotenv import load_dotenv
load_dotenv()

import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.collector.futu import FutuCollector
from src.us_playbook.main import USPlaybook
from src.us_playbook.telegram import register_us_playbook_commands
from src.utils.logger import setup_logger

logger = setup_logger("us_playbook_main")


def _load_config(path: str = "config/us_playbook_settings.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


async def main() -> None:
    cfg = _load_config()
    futu_cfg = cfg.get("futu", {})

    collector = FutuCollector(
        host=futu_cfg.get("host", "127.0.0.1"),
        port=futu_cfg.get("port", 11111),
    )
    await collector.connect()

    playbook = USPlaybook(cfg, collector)

    # Optional: Telegram integration
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if bot_token and chat_id:
        from telegram.ext import ApplicationBuilder
        app = ApplicationBuilder().token(bot_token).build()
        register_us_playbook_commands(app, playbook)

        async def send_text(text: str, parse_mode: str = "HTML") -> None:
            await app.bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)

        playbook.set_send_fn(send_text)
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        logger.info("Telegram bot started")

    # Schedule playbook pushes
    scheduler = AsyncIOScheduler(timezone="America/New_York")
    scheduler.add_job(
        playbook.run_playbook_cycle,
        CronTrigger(hour=9, minute=45, day_of_week="mon-fri", timezone="America/New_York"),
        kwargs={"update_type": "morning"}, id="us_playbook_morning",
    )
    scheduler.add_job(
        playbook.run_playbook_cycle,
        CronTrigger(hour=10, minute=15, day_of_week="mon-fri", timezone="America/New_York"),
        kwargs={"update_type": "confirm"}, id="us_playbook_confirm",
    )
    scheduler.start()

    logger.info("US Playbook started — scheduled 09:45/10:15 ET pushes")

    # Run initial cycle if during market hours
    # await playbook.run_playbook_cycle(update_type="morning")

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown.set)
    await shutdown.wait()

    scheduler.shutdown(wait=False)
    await collector.close()
    logger.info("US Playbook stopped")


if __name__ == "__main__":
    asyncio.run(main())
