"""HK Playbook Telegram integration — text-triggered playbook and watchlist management."""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes, MessageHandler, filters

from src.common.telegram_handlers import (
    handle_add_base,
    handle_query_base,
    handle_remove_base,
    handle_watchlist_base,
)
from src.hk.watchlist import normalize_symbol
from src.utils.logger import setup_logger

if TYPE_CHECKING:
    from src.hk.main import HKPredictor

logger = setup_logger("hk_telegram")

_esc = html.escape

# Regex patterns — exclude /commands to avoid conflicts with US pipeline
_RE_QUERY = re.compile(r"^(?:HK\.?)?(\d{4,6})$", re.IGNORECASE)
_RE_ADD = re.compile(r"^\+(?:HK\.?)?(\d{4,6})\s*(.*)$", re.IGNORECASE)
_RE_REMOVE = re.compile(r"^-(?:HK\.?)?(\d{4,6})$", re.IGNORECASE)
_RE_WATCHLIST = re.compile(r"^(?:hkwl|hk_watchlist)$", re.IGNORECASE)

_PREDICTOR_KEY = "hk_predictor"


def register_hk_predictor_handlers(application, predictor: HKPredictor) -> None:
    """Register HK Predictor handlers onto an existing Telegram Application."""
    application.bot_data[_PREDICTOR_KEY] = predictor

    not_command = ~filters.COMMAND

    application.add_handler(MessageHandler(
        filters.Regex(_RE_ADD) & not_command,
        _handle_add, block=False,
    ))
    application.add_handler(MessageHandler(
        filters.Regex(_RE_REMOVE) & not_command,
        _handle_remove, block=False,
    ))
    application.add_handler(MessageHandler(
        filters.Regex(_RE_WATCHLIST) & not_command,
        _handle_watchlist, block=False,
    ))
    application.add_handler(MessageHandler(
        filters.Regex(_RE_QUERY) & not_command,
        _handle_query, block=False,
    ))

    application.add_handler(CommandHandler(["hk_help", "hkh"], _cmd_hk_help, block=False))

    logger.info("HK Predictor handlers registered (text triggers + /hk_help)")


def _format_watchlist_message(items: list[dict]) -> str:
    lines = [
        "📋 <b>HK 监控列表</b>",
        "━" * 20,
        "",
    ]
    for item in items:
        symbol = item["symbol"]
        name = item["name"]
        display = f"{_esc(name)} ({_esc(symbol)})" if name != symbol else _esc(symbol)
        lines.append(f"• {display}")

    lines.append("")
    lines.append(f"共 {len(items)} 个标的")
    lines.append("快捷操作:")
    lines.append("• 直接发送代码查询")
    lines.append("• <code>+代码</code> 添加")
    lines.append("• <code>-代码</code> 删除")
    return "\n".join(lines)


# ── Handlers ──

async def _handle_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    await handle_query_base(
        update, context,
        predictor_key=_PREDICTOR_KEY,
        normalize_fn=normalize_symbol,
        analyzing_text="正在拉取现价、VWAP、Value Area、期权链与风险过滤结果...",
        not_in_list_text="不在监控列表中",
        add_hint_template="+{text}",
        wl_command="hkwl",
        market="hk",
    )


async def _handle_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    m = _RE_ADD.match(text)
    if not m:
        return
    await handle_add_base(
        update, context,
        predictor_key=_PREDICTOR_KEY,
        regex_match_groups=(m.group(1), m.group(2)),
        normalize_fn=normalize_symbol,
        market_label="",
        market="hk",
    )


async def _handle_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    m = _RE_REMOVE.match(text)
    if not m:
        return
    await handle_remove_base(
        update, context,
        predictor_key=_PREDICTOR_KEY,
        raw_code=m.group(1),
        normalize_fn=normalize_symbol,
        market="hk",
    )


async def _handle_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_watchlist_base(
        update, context,
        predictor_key=_PREDICTOR_KEY,
        market_label="HK",
        empty_hint="+09988",
        format_fn=_format_watchlist_message,
        market="hk",
    )


# ── Help ──

async def _cmd_hk_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/hk_help — show available commands."""
    from src.common.telegram_handlers import _log_to_archive

    text = (
        "<b>HK 期权监控使用说明</b>\n"
        + "━" * 20 + "\n"
        "\n"
        "<b>查询方式</b>\n"
        "直接发送代码即可获取完整剧本:\n"
        "  <code>09988</code> / <code>HK09988</code> / <code>HK.09988</code>\n"
        "\n"
        "<b>常用操作</b>\n"
        "• <code>+09988 阿里巴巴</code> 添加标的 (名称可选)\n"
        "• <code>-09988</code> 移除标的\n"
        "• <code>hkwl</code> 查看当前监控列表\n"
        "\n"
        "<b>返回内容</b>\n"
        "• 市场定调\n"
        "• 实时数据支撑\n"
        "• 期权操作建议\n"
        "• 风险说明\n"
        "\n"
        "<b>支持的建议类型</b>\n"
        "• 单腿 Call / Put\n"
        "• Bull Put Spread / Bear Call Spread\n"
        "• 观望 (附重新评估条件)\n"
        "\n"
        "<b>限制说明</b>\n"
        "• 仅支持监控列表内标的\n"
        "• 期权链不可用时会降级为观望或保守提示\n"
        "\n"
        "<b>风险提示</b>\n"
        "• 期权交易有风险，务必控制仓位并设置止损\n"
        "• 本系统仅提供参考，不构成投资建议"
    )
    await update.message.reply_text(text, parse_mode="HTML")
    _log_to_archive("hk_playbook", "cmd_hk_help", text, "hk")
