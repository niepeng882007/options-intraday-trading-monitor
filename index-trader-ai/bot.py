"""Telegram Bot — 定时推送 + 命令响应。

所有命令只响应配置中指定的 TELEGRAM_CHAT_ID，其他用户无响应。
定时推送：09:00 ET 完整数据，09:25 ET 带 △ 标记的更新数据。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

if TYPE_CHECKING:
    from collector import DataCollector
    from formatter import DataFormatter
    from models import CollectionResult

logger = logging.getLogger("bot")


class TelegramBot:
    """Telegram Bot — 命令 + 定时推送。"""

    def __init__(
        self,
        config: dict,
        collector: DataCollector,
        formatter: DataFormatter,
    ) -> None:
        self._cfg = config
        self._collector = collector
        self._formatter = formatter
        self._chat_id = str(config.get("telegram", {}).get("chat_id", ""))
        self._app: Application | None = None
        self._prev_result: CollectionResult | None = None

    async def start(self, app: Application) -> None:
        """注册所有 handlers 到 Telegram Application。"""
        self._app = app

        app.add_handler(CommandHandler("report", self._cmd_report, block=False))
        app.add_handler(CommandHandler("update", self._cmd_update, block=False))
        app.add_handler(CommandHandler("levels", self._cmd_levels, block=False))
        app.add_handler(CommandHandler("mag7", self._cmd_mag7, block=False))
        app.add_handler(CommandHandler("raw", self._cmd_raw, block=False))
        app.add_handler(CommandHandler("calendar", self._cmd_calendar, block=False))
        app.add_handler(CommandHandler("risk", self._cmd_risk, block=False))
        app.add_handler(CommandHandler("status", self._cmd_status, block=False))
        app.add_handler(CommandHandler("help", self._cmd_help, block=False))

        logger.info("Telegram handlers registered")

    # ── 鉴权 ──

    def _check_auth(self, update: Update) -> bool:
        """检查消息来源是否为授权 chat_id。"""
        if not self._chat_id:
            return True  # 未配置 chat_id 则放行
        chat_id = str(update.effective_chat.id) if update.effective_chat else ""
        return chat_id == self._chat_id

    # ── 定时推送接口 ──

    async def push_report(self, is_update: bool = False) -> None:
        """定时推送接口（由 APScheduler 调用）。"""
        if not self._app or not self._chat_id:
            logger.warning("Cannot push: app or chat_id not configured")
            return

        from calendar_fetcher import get_today_events

        cal_path = self._cfg.get("calendar_file", "../config/us_calendar.yaml")
        events = get_today_events(cal_path)

        result = await self._collector.collect_full(calendar_events=events)

        prev = self._prev_result if is_update else None
        text = self._formatter.format_telegram(result, prev=prev)

        try:
            await self._app.bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode="Markdown",
            )
            logger.info("Report pushed (is_update=%s)", is_update)
        except Exception:
            logger.warning("Failed to push report", exc_info=True)

        self._prev_result = result

    # ── 命令 handlers ──

    async def _cmd_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/report — 手动触发完整数据采集 + 推送。"""
        if not self._check_auth(update):
            return

        from calendar_fetcher import get_today_events

        cal_path = self._cfg.get("calendar_file", "../config/us_calendar.yaml")
        events = get_today_events(cal_path)

        result = await self._collector.collect_full(calendar_events=events)
        text = self._formatter.format_telegram(result)

        await update.message.reply_text(text, parse_mode="Markdown")
        self._prev_result = result

    async def _cmd_update(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/update — 精简快照：三大指数 + VIX/TNX/UUP 当前值。"""
        if not self._check_auth(update):
            return

        from calendar_fetcher import get_today_events

        cal_path = self._cfg.get("calendar_file", "../config/us_calendar.yaml")
        events = get_today_events(cal_path)

        result = await self._collector.collect_full(calendar_events=events)

        # 精简模式：一行一个
        lines = ["📊 *快速更新*", ""]

        # 指数
        for idx in result.indices:
            price_str = f"${idx.price:.2f}" if idx.price else "[不可用]"
            pct_str = f"{idx.change_pct:+.2f}%" if idx.change_pct is not None else "[不可用]"
            lines.append(f"{idx.symbol}: {price_str} ({pct_str})")

        # 宏观
        m = result.macro
        lines.append(f"VIX: {m.vix_current:.2f}" if m.vix_current else "VIX: [不可用]")
        lines.append(f"TNX: {m.tnx_current:.3f}%" if m.tnx_current else "TNX: [不可用]")
        lines.append(f"UUP: ${m.uup_current:.2f}" if m.uup_current else "UUP: [不可用]")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        self._prev_result = result

    async def _cmd_levels(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/levels [QQQ|SPY|IWM] — 输出指数的全部关键点位。"""
        if not self._check_auth(update):
            return

        args = context.args
        symbol = args[0].upper() if args else None

        from calendar_fetcher import get_today_events

        cal_path = self._cfg.get("calendar_file", "../config/us_calendar.yaml")
        events = get_today_events(cal_path)

        result = await self._collector.collect_full(calendar_events=events)
        text = self._formatter.format_levels(result, symbol)
        await update.message.reply_text(text)

    async def _cmd_mag7(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/mag7 — 输出 Mag7 七只股票的盘前数据。"""
        if not self._check_auth(update):
            return

        from calendar_fetcher import get_today_events

        cal_path = self._cfg.get("calendar_file", "../config/us_calendar.yaml")
        events = get_today_events(cal_path)

        result = await self._collector.collect_full(calendar_events=events)
        text = self._formatter.format_mag7(result)
        await update.message.reply_text(text, parse_mode="Markdown")

    async def _cmd_raw(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/raw — 纯文本输出（无 emoji、无 Markdown，直接可复制给 LLM）。"""
        if not self._check_auth(update):
            return

        from calendar_fetcher import get_today_events

        cal_path = self._cfg.get("calendar_file", "../config/us_calendar.yaml")
        events = get_today_events(cal_path)

        result = await self._collector.collect_full(calendar_events=events)
        text = self._formatter.format_raw(result)
        # 纯文本发送，不使用 parse_mode
        await update.message.reply_text(text)

    async def _cmd_calendar(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/calendar — 今日经济日历。"""
        if not self._check_auth(update):
            return

        from calendar_fetcher import get_today_events
        from models import CollectionResult, MacroData

        cal_path = self._cfg.get("calendar_file", "../config/us_calendar.yaml")
        events = get_today_events(cal_path)

        # 构造只包含日历的 CollectionResult
        import time
        from datetime import datetime
        from zoneinfo import ZoneInfo

        et_now = datetime.now(ZoneInfo("America/New_York"))
        dummy = CollectionResult(
            timestamp=time.time(),
            date_str=et_now.strftime("%Y-%m-%d"),
            time_str=et_now.strftime("%H:%M") + " ET",
            macro=MacroData(),
            calendar=events,
        )
        text = self._formatter.format_calendar(dummy)
        await update.message.reply_text(text)

    async def _cmd_risk(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/risk — VIX 偏离查表，输出风控参数。"""
        if not self._check_auth(update):
            return

        from collector import lookup_risk

        # 获取最新 VIX 偏离
        macro = await self._collector._collect_macro()
        risk_data = lookup_risk(macro.vix_deviation_pct, self._cfg)
        text = self._formatter.format_risk(risk_data)
        await update.message.reply_text(text, parse_mode="Markdown")

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/status — 系统运行状态。"""
        if not self._check_auth(update):
            return

        statuses = await self._collector.check_availability()

        from models import CollectionResult, MacroData
        import time
        from datetime import datetime
        from zoneinfo import ZoneInfo

        et_now = datetime.now(ZoneInfo("America/New_York"))
        dummy = CollectionResult(
            timestamp=time.time(),
            date_str=et_now.strftime("%Y-%m-%d"),
            time_str=et_now.strftime("%H:%M") + " ET",
            macro=MacroData(),
            statuses=statuses,
        )
        sub_count = self._collector.get_subscription_count()
        text = self._formatter.format_status(dummy, sub_count)
        await update.message.reply_text(text, parse_mode="Markdown")

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/help — 帮助。"""
        if not self._check_auth(update):
            return

        text = (
            "📊 *Index Trader AI 命令*\n\n"
            "/report — 完整盘前数据报告\n"
            "/update — 精简快照（三大指数 + 宏观）\n"
            "/levels \\[QQQ|SPY|IWM\\] — 指数关键点位\n"
            "/mag7 — Mag7 盘前数据\n"
            "/raw — 纯文本数据（可直接喂给 LLM）\n"
            "/calendar — 今日经济日历\n"
            "/risk — VIX 风控参数查表\n"
            "/status — 系统状态\n"
            "/help — 本帮助"
        )
        await update.message.reply_text(text, parse_mode="Markdown")
