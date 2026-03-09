"""US Playbook Telegram integration — bot commands."""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from src.utils.logger import setup_logger

if TYPE_CHECKING:
    from src.us_playbook.main import USPlaybook

logger = setup_logger("us_telegram")

_esc = html.escape


def register_us_playbook_commands(application, us_playbook: USPlaybook) -> None:
    """Register US Playbook bot commands onto an existing Telegram Application."""
    handlers = {
        ("us_playbook", "uspb"): _cmd_us_playbook,
        ("us_levels", "usl"): _cmd_us_levels,
        ("us_regime", "usr"): _cmd_us_regime,
        ("us_filters", "usf"): _cmd_us_filters,
        ("us_gamma", "usg"): _cmd_us_gamma,
        ("us_help", "ush"): _cmd_us_help,
    }
    for cmds, handler in handlers.items():
        application.add_handler(CommandHandler(list(cmds), handler, block=False))

    application.bot_data["us_playbook"] = us_playbook
    all_cmds = [c for cmds in handlers for c in cmds]
    logger.info("US Playbook commands registered: %s", ", ".join(f"/{c}" for c in all_cmds))


async def _get_playbook(context: ContextTypes.DEFAULT_TYPE) -> USPlaybook:
    return context.bot_data["us_playbook"]


async def _cmd_us_playbook(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/us_playbook [symbol] — Generate and show US playbook."""
    pb = await _get_playbook(context)
    args = context.args or []
    symbol = args[0].upper() if args else None
    try:
        msg = await pb.get_playbook_text(symbol)
        await update.message.reply_text(msg, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Error: {_esc(str(e))}")


async def _cmd_us_levels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/us_levels [symbol] — Show key levels."""
    pb = await _get_playbook(context)
    args = context.args or []
    symbol = args[0].upper() if args else None
    try:
        msg = await pb.get_levels_text(symbol)
        await update.message.reply_text(msg, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Error: {_esc(str(e))}")


async def _cmd_us_regime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/us_regime [symbol] — Show regime classification."""
    pb = await _get_playbook(context)
    args = context.args or []
    symbol = args[0].upper() if args else None
    try:
        msg = await pb.get_regime_text(symbol)
        await update.message.reply_text(msg, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Error: {_esc(str(e))}")


async def _cmd_us_filters(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/us_filters — Show filter status."""
    pb = await _get_playbook(context)
    try:
        msg = await pb.get_filters_text()
        await update.message.reply_text(msg, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Error: {_esc(str(e))}")


async def _cmd_us_gamma(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/us_gamma [symbol] — Show Gamma Wall."""
    pb = await _get_playbook(context)
    args = context.args or []
    symbol = args[0].upper() if args else None
    try:
        msg = await pb.get_gamma_text(symbol)
        await update.message.reply_text(msg, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Error: {_esc(str(e))}")


async def _cmd_us_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/us_help — Show all available US Playbook commands."""
    text = (
        "<b>US Playbook 指令列表</b>\n"
        "━" * 20 + "\n"
        "\n"
        "<b>Playbook 与分析</b>\n"
        "/us_playbook [symbol] — 生成 Playbook (默认 SPY) (别名: /uspb)\n"
        "/us_levels [symbol] — 关键点位 VP/PDH/PDL/Gamma (别名: /usl)\n"
        "/us_regime [symbol] — Regime 分类 + 交易建议 (别名: /usr)\n"
        "/us_filters — 风险过滤状态 (别名: /usf)\n"
        "/us_gamma [symbol] — Gamma Wall + Max Pain (别名: /usg)\n"
        "\n"
        "<b>说明</b>\n"
        "• [symbol] 可选，默认 SPY\n"
        "• 自动推送: 09:45 / 10:15 ET\n"
        "• 09:45 为初步 (15min RVOL)，10:15 为确认 (45min RVOL)\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")
