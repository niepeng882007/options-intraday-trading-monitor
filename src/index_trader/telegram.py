"""Telegram 命令注册 — Index Trader。

所有命令使用 /前缀（CommandHandler），不与 US/HK 的文本正则冲突。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from src.utils.logger import setup_logger

if TYPE_CHECKING:
    from src.index_trader.main import IndexTrader

logger = setup_logger("index_telegram")


def register_index_trader_handlers(app: Application, trader: IndexTrader) -> None:
    """注册 Index Trader 命令到共享 Telegram Application。"""
    app.bot_data["index_trader"] = trader

    app.add_handler(CommandHandler("report", _cmd_report, block=False))
    app.add_handler(CommandHandler("update", _cmd_update, block=False))
    app.add_handler(CommandHandler("levels", _cmd_levels, block=False))
    app.add_handler(CommandHandler("mag7", _cmd_mag7, block=False))
    app.add_handler(CommandHandler("score", _cmd_score, block=False))
    app.add_handler(CommandHandler("risk", _cmd_risk, block=False))
    app.add_handler(CommandHandler("idx_help", _cmd_help, block=False))

    logger.info("Index Trader Telegram handlers registered")


def _get_trader(context: ContextTypes.DEFAULT_TYPE) -> IndexTrader | None:
    return context.bot_data.get("index_trader")


async def _cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/report — 生成完整盘前分析报告。"""
    trader = _get_trader(context)
    if not trader:
        await update.message.reply_text("Index Trader 未初始化")
        return

    await update.message.reply_text("⏳ 正在生成指数盘前报告...")

    async def _send(text: str, parse_mode: str = "HTML") -> None:
        await update.message.reply_text(text, parse_mode=parse_mode)

    await trader.push_report(_send, is_update=False)


async def _cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/update — 生成更新报告（标注与上次的变化）。"""
    trader = _get_trader(context)
    if not trader:
        await update.message.reply_text("Index Trader 未初始化")
        return

    await update.message.reply_text("⏳ 正在生成更新报告...")

    async def _send(text: str, parse_mode: str = "HTML") -> None:
        await update.message.reply_text(text, parse_mode=parse_mode)

    await trader.push_report(_send, is_update=True)


async def _cmd_levels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/levels — 查看关键点位。"""
    trader = _get_trader(context)
    if not trader:
        return
    text = await trader.generate_section("levels")
    await update.message.reply_text(text, parse_mode="HTML")


async def _cmd_mag7(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/mag7 — 查看 Mag7 温度计。"""
    trader = _get_trader(context)
    if not trader:
        return
    text = await trader.generate_section("mag7")
    await update.message.reply_text(text, parse_mode="HTML")


async def _cmd_score(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/score — 查看评分明细。"""
    trader = _get_trader(context)
    if not trader:
        return
    text = await trader.generate_section("score")
    await update.message.reply_text(text, parse_mode="HTML")


async def _cmd_risk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/risk — 查看风控参数。"""
    trader = _get_trader(context)
    if not trader:
        return
    report = await trader.generate_report()
    r = report.risk
    text = (
        "<b>🛡 风控参数</b>\n"
        f"波动率: {r.volatility_regime.value}\n"
        f"单笔风险: ≤{r.max_single_risk_pct}%\n"
        f"日内止损: ≤{r.max_daily_loss_pct}%\n"
        f"熔断: {r.circuit_breaker_count} 笔连续止损 → 停 {r.cooldown_minutes}min"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def _cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/idx_help — 显示帮助信息。"""
    text = (
        "<b>📊 Index Trader 指令</b>\n\n"
        "/report — 完整盘前分析报告\n"
        "/update — 更新报告（标注变化）\n"
        "/levels — 关键点位\n"
        "/mag7 — Mag7 温度计\n"
        "/score — 评分明细\n"
        "/risk — 风控参数\n"
        "/idx_help — 本帮助"
    )
    await update.message.reply_text(text, parse_mode="HTML")
