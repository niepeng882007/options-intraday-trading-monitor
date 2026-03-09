"""HK Predict Telegram integration — scheduled pushes and bot commands."""

from __future__ import annotations

import html
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from src.utils.logger import setup_logger

if TYPE_CHECKING:
    from src.hk.main import HKPredictor

logger = setup_logger("hk_telegram")

HKT = timezone(timedelta(hours=8))
_esc = html.escape

REGIME_EMOJI = {
    "breakout": "\U0001f680",
    "range": "\U0001f4e6",
    "whipsaw": "\U0001f30a",
    "unclear": "\u2753",
}


def register_hk_commands(application, predictor: HKPredictor) -> None:
    """Register HK-specific bot commands onto an existing Telegram Application."""
    handlers = {
        ("hk",): _cmd_hk_status,
        ("hk_playbook", "hkpb"): _cmd_hk_playbook,
        ("hk_orderbook", "hkob"): _cmd_hk_orderbook,
        ("hk_gamma", "hkg"): _cmd_hk_gamma,
        ("hk_levels", "hkl"): _cmd_hk_levels,
        ("hk_regime", "hkr"): _cmd_hk_regime,
        ("hk_quote", "hkq"): _cmd_hk_quote,
        ("hk_filters", "hkf"): _cmd_hk_filters,
        ("hk_watchlist", "hkw"): _cmd_hk_watchlist,
        ("hk_help", "hkh"): _cmd_hk_help,
    }
    for cmds, handler in handlers.items():
        application.add_handler(CommandHandler(list(cmds), handler, block=False))

    # Store predictor reference on bot_data for handler access
    application.bot_data["hk_predictor"] = predictor
    all_cmds = [c for cmds in handlers for c in cmds]
    logger.info("HK Telegram commands registered: %s", ", ".join(f"/{c}" for c in all_cmds))


async def _get_predictor(context: ContextTypes.DEFAULT_TYPE) -> HKPredictor:
    return context.bot_data["hk_predictor"]


# ── Original commands ──

async def _cmd_hk_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/hk — Show current HK market status snapshot."""
    predictor = await _get_predictor(context)
    try:
        status = await predictor.get_status_text()
        await update.message.reply_text(status, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Error: {_esc(str(e))}")


async def _cmd_hk_playbook(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/hk_playbook [symbol] — Regenerate and show playbook."""
    predictor = await _get_predictor(context)
    args = context.args or []
    symbol = args[0] if args else None
    try:
        msg = await predictor.generate_and_format_playbook(symbol=symbol, update_type="manual")
        await update.message.reply_text(msg, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Error: {_esc(str(e))}")


async def _cmd_hk_orderbook(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/hk_orderbook [symbol] — Show LV2 order book snapshot."""
    predictor = await _get_predictor(context)
    args = context.args or []
    symbol = args[0] if args else "HK.00700"
    try:
        msg = await predictor.get_orderbook_text(symbol)
        await update.message.reply_text(msg, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Error: {_esc(str(e))}")


async def _cmd_hk_gamma(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/hk_gamma [symbol] — Show Gamma Wall for index."""
    predictor = await _get_predictor(context)
    args = context.args or []
    symbol = args[0] if args else "HK.800000"
    try:
        msg = await predictor.get_gamma_wall_text(symbol)
        await update.message.reply_text(msg, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Error: {_esc(str(e))}")


# ── New commands ──

async def _cmd_hk_levels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/hk_levels [symbol] — Show today's key VP levels + VWAP + current price."""
    predictor = await _get_predictor(context)
    args = context.args or []
    symbol = args[0] if args else None
    try:
        msg = await predictor.get_levels_text(symbol)
        await update.message.reply_text(msg, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Error: {_esc(str(e))}")


async def _cmd_hk_regime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/hk_regime [symbol] — Show current regime classification."""
    predictor = await _get_predictor(context)
    args = context.args or []
    symbol = args[0] if args else None
    try:
        msg = await predictor.get_regime_text(symbol)
        await update.message.reply_text(msg, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Error: {_esc(str(e))}")


async def _cmd_hk_quote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/hk_quote <symbol> — Full quote snapshot for a single symbol."""
    predictor = await _get_predictor(context)
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "用法: /hk_quote <symbol>\n"
            "例如: /hk_quote HK.00700"
        )
        return
    symbol = args[0]
    try:
        msg = await predictor.get_quote_text(symbol)
        await update.message.reply_text(msg, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Error: {_esc(str(e))}")


async def _cmd_hk_filters(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/hk_filters [symbol] — Show active trade filter status."""
    predictor = await _get_predictor(context)
    args = context.args or []
    symbol = args[0] if args else None
    try:
        msg = await predictor.get_filters_text(symbol)
        await update.message.reply_text(msg, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Error: {_esc(str(e))}")


async def _cmd_hk_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/hk_watchlist — All watchlist symbols with quick quotes."""
    predictor = await _get_predictor(context)
    try:
        msg = await predictor.get_watchlist_text()
        await update.message.reply_text(msg, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Error: {_esc(str(e))}")


async def _cmd_hk_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/hk_help — Show all available HK commands."""
    text = (
        "<b>HK Predict 指令列表</b>\n"
        "━" * 20 + "\n"
        "\n"
        "<b>市场概览</b>\n"
        "/hk — 市场状态快照\n"
        "/hk_watchlist — 全部监控标的行情 (别名: /hkw)\n"
        "/hk_quote &lt;symbol&gt; — 单个标的详细报价 (别名: /hkq)\n"
        "\n"
        "<b>Playbook 与分析</b>\n"
        "/hk_playbook [symbol] — 重新生成 Playbook (别名: /hkpb)\n"
        "/hk_levels [symbol] — 关键点位 POC/VAH/VAL/VWAP (别名: /hkl)\n"
        "/hk_regime [symbol] — 当前 Regime 分类 (别名: /hkr)\n"
        "/hk_filters [symbol] — 交易过滤状态 (别名: /hkf)\n"
        "\n"
        "<b>衍生品与盘口</b>\n"
        "/hk_gamma [symbol] — Gamma Wall (别名: /hkg)\n"
        "/hk_orderbook [symbol] — LV2 盘口快照 (别名: /hkob)\n"
        "\n"
        "<b>说明</b>\n"
        "• [symbol] 可选，默认主指数 (HK.800000)\n"
        "• 自动推送: 09:35 / 10:05 / 13:05 HKT\n"
        "• 盘口异常每 60s 检测\n"
        "• 所有命令均支持短别名，输入 / 可弹出菜单\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")
