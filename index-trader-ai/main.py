"""Index Trader AI — 入口 + 调度。

启动流程：
1. 加载 config.yaml + .env
2. 启动 FutuOpenD 连接
3. 执行数据可用性检查，打印各数据源状态
4. 打印当前订阅额度使用情况（必须 ≤ 20）
5. 启动 Telegram Bot
6. 注册定时任务（09:00 和 09:25 推送）
7. 09:30 后切换到盘中监控模式
8. 16:00 后记录当日数据到日志
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.ext import ApplicationBuilder

from bot import TelegramBot
from calendar_fetcher import get_today_events
from collector import DataCollector
from config import load_config
from formatter import DataFormatter
from monitor import IntraDayMonitor

_ET = ZoneInfo("America/New_York")

# 日志
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(name)s][%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("main")


async def main() -> None:
    """主入口。"""
    # ── Step 1: 加载配置 ──
    cfg = load_config()
    logger.info("Config loaded")

    bot_token = cfg.get("telegram", {}).get("bot_token", "")
    chat_id = cfg.get("telegram", {}).get("chat_id", "")

    if not bot_token:
        logger.error("TELEGRAM_BOT_TOKEN not configured")
        return
    if not chat_id:
        logger.warning("TELEGRAM_CHAT_ID not configured — commands will accept all users")

    # ── Step 2: 启动 FutuOpenD 连接 ──
    collector = DataCollector(cfg)
    try:
        await collector.start()
    except Exception:
        logger.error("Futu connection failed", exc_info=True)
        return

    # ── Step 3: 数据可用性检查 ──
    statuses = await collector.check_availability()
    logger.info("=== 数据源状态 ===")
    for s in statuses:
        icon = "✅" if s.ok else "❌"
        detail = f" — {s.detail}" if s.detail else ""
        logger.info("  %s %s%s", icon, s.source, detail)

    # ── Step 4: 订阅额度 ──
    sub_count = collector.get_subscription_count()
    logger.info("订阅额度使用: %d (上限 20)", sub_count)
    if sub_count > 20:
        logger.error("订阅额度超限！当前 %d > 20", sub_count)
        collector.close()
        return

    # ── Step 5: 启动 Telegram Bot ──
    formatter = DataFormatter(cfg)
    bot = TelegramBot(cfg, collector, formatter)

    tg_app = (
        ApplicationBuilder()
        .token(bot_token)
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(15)
        .pool_timeout(10)
        .build()
    )

    await bot.start(tg_app)

    # 初始化并启动 Telegram polling
    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(
        drop_pending_updates=True,
        allowed_updates=["message"],
    )
    logger.info("Telegram Bot started")

    # ── Step 6: 注册定时任务 ──
    scheduler = AsyncIOScheduler(timezone="America/New_York")

    sched_cfg = cfg.get("schedule", {})

    # 09:00 ET 第一次推送
    t1 = sched_cfg.get("report_push_1", "09:00").split(":")
    scheduler.add_job(
        bot.push_report, "cron",
        hour=int(t1[0]), minute=int(t1[1]),
        kwargs={"is_update": False},
        id="report_v1", max_instances=1,
    )

    # 09:25 ET 第二次推送（带 △ 标记）
    t2 = sched_cfg.get("report_push_2", "09:25").split(":")
    scheduler.add_job(
        bot.push_report, "cron",
        hour=int(t2[0]), minute=int(t2[1]),
        kwargs={"is_update": True},
        id="report_v2", max_instances=1,
    )

    # ── Step 7: 盘中监控 ──
    monitor = IntraDayMonitor(cfg, collector)

    # 设置日历事件
    cal_path = cfg.get("calendar_file", "../config/us_calendar.yaml")
    calendar_events = get_today_events(cal_path)
    monitor.set_calendar(calendar_events)

    async def _monitor_send(text: str) -> None:
        if chat_id:
            try:
                await tg_app.bot.send_message(
                    chat_id=chat_id, text=text,
                )
            except Exception:
                logger.warning("Monitor send failed: %s", text[:100])

    # 09:30 激活监控
    market_open = sched_cfg.get("market_open", "09:30").split(":")
    scheduler.add_job(
        _activate_monitor, "cron",
        hour=int(market_open[0]), minute=int(market_open[1]),
        args=[monitor, _monitor_send],
        id="monitor_start", max_instances=1,
    )

    # 每分钟检查日历倒计时
    scheduler.add_job(
        monitor.check_calendar_countdown, "interval",
        minutes=1,
        id="calendar_countdown", max_instances=1,
    )

    # ── Step 8: 16:00 后记录当日数据 ──
    market_close = sched_cfg.get("market_close", "16:00").split(":")
    scheduler.add_job(
        _end_of_day, "cron",
        hour=int(market_close[0]), minute=int(market_close[1]),
        args=[collector, monitor],
        id="eod_log", max_instances=1,
    )

    scheduler.start()
    logger.info(
        "Scheduler started — 推送 %s/%s, 开盘 %s, 收盘 %s",
        sched_cfg.get("report_push_1", "09:00"),
        sched_cfg.get("report_push_2", "09:25"),
        sched_cfg.get("market_open", "09:30"),
        sched_cfg.get("market_close", "16:00"),
    )

    logger.info("=== Index Trader AI 就绪 ===")

    # ── 优雅关闭 ──
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown.set)

    await shutdown.wait()
    logger.info("Shutting down...")

    scheduler.shutdown(wait=False)
    monitor.stop()
    await tg_app.updater.stop()
    await tg_app.stop()
    await tg_app.shutdown()
    collector.close()

    logger.info("Goodbye.")


async def _activate_monitor(
    monitor: IntraDayMonitor,
    send_fn,
) -> None:
    """09:30 激活盘中监控。"""
    await monitor.start(send_fn)
    logger.info("IntraDayMonitor activated (market open)")


async def _end_of_day(
    collector: DataCollector,
    monitor: IntraDayMonitor,
) -> None:
    """16:00 收盘后：最终归档 + 停止监控。"""
    logger.info("End of day — archiving final snapshot")

    try:
        result = await collector.collect_full()
        collector.archive_raw(result)
    except Exception:
        logger.warning("EOD archive failed", exc_info=True)

    monitor.stop()
    monitor.reset_daily()
    logger.info("Monitor stopped, daily state reset")


if __name__ == "__main__":
    asyncio.run(main())
