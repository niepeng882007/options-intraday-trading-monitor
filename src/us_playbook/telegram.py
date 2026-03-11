"""US Predictor Telegram integration — text-triggered playbook and watchlist management."""

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
from src.us_playbook.watchlist import normalize_us_symbol
from src.utils.logger import setup_logger

if TYPE_CHECKING:
    from src.us_playbook.main import USPredictor

logger = setup_logger("us_telegram")

_esc = html.escape

# Regex patterns — strict uppercase 2-5 alpha chars for query
_RE_QUERY = re.compile(r"^[A-Z]{2,5}$")
_RE_ADD = re.compile(r"^\+([A-Za-z]{1,5})\s*(.*)$")
_RE_REMOVE = re.compile(r"^-([A-Za-z]{1,5})$")
_RE_WL = re.compile(r"^(?:wl|watchlist)$", re.IGNORECASE)

_PREDICTOR_KEY = "us_predictor"


def register_us_predictor_handlers(application, predictor: USPredictor) -> None:
    """Register US Predictor handlers onto an existing Telegram Application."""
    application.bot_data[_PREDICTOR_KEY] = predictor

    nc = ~filters.COMMAND

    application.add_handler(MessageHandler(
        filters.Regex(_RE_ADD) & nc,
        _handle_add, block=False,
    ))
    application.add_handler(MessageHandler(
        filters.Regex(_RE_REMOVE) & nc,
        _handle_remove, block=False,
    ))
    application.add_handler(MessageHandler(
        filters.Regex(_RE_WL) & nc,
        _handle_watchlist, block=False,
    ))
    application.add_handler(MessageHandler(
        filters.Regex(_RE_QUERY) & nc,
        _handle_query, block=False,
    ))

    application.add_handler(CommandHandler(["us_help", "ush"], _cmd_us_help, block=False))

    logger.info("US Predictor handlers registered (text triggers + /us_help)")


def _format_watchlist_message(items: list[dict]) -> str:
    lines = [
        "📋 <b>US 监控列表</b>",
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
    lines.append("• 直接发送大写代码查询 (如 <code>SPY</code>)")
    lines.append("• <code>+代码</code> 添加 (如 <code>+TSLA Tesla</code>)")
    lines.append("• <code>-代码</code> 删除 (如 <code>-TSLA</code>)")
    return "\n".join(lines)


# ── Handlers ──

async def _handle_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    await handle_query_base(
        update, context,
        predictor_key=_PREDICTOR_KEY,
        normalize_fn=normalize_us_symbol,
        analyzing_text="正在拉取现价、VWAP、Value Area、期权链...",
        not_in_list_text="未在美股监控列表中",
        add_hint_template="+{symbol}",
        wl_command="wl",
        market="us",
    )


async def _handle_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    m = _RE_ADD.match(text)
    if not m:
        return
    symbol = normalize_us_symbol(m.group(1))
    await handle_add_base(
        update, context,
        predictor_key=_PREDICTOR_KEY,
        regex_match_groups=(m.group(1), m.group(2)),
        normalize_fn=normalize_us_symbol,
        market_label="美股",
        symbol_hint=symbol or m.group(1).upper(),
        market="us",
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
        normalize_fn=normalize_us_symbol,
        market="us",
    )


async def _handle_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_watchlist_base(
        update, context,
        predictor_key=_PREDICTOR_KEY,
        market_label="US",
        empty_hint="+SPY S&amp;P 500",
        format_fn=_format_watchlist_message,
        market="us",
    )


# ── Help ──

async def _cmd_us_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/us_help — show available commands."""
    from src.common.telegram_handlers import _log_to_archive

    text = (
        "<b>US 期权监控使用说明</b>\n"
        + "━" * 20 + "\n"
        "\n"
        "<b>查询方式</b>\n"
        "直接发送大写代码即可获取完整剧本:\n"
        "  <code>SPY</code> / <code>AAPL</code> / <code>TSLA</code>\n"
        "\n"
        "<b>常用操作</b>\n"
        "• <code>+AAPL Apple</code> 添加标的 (名称可选)\n"
        "• <code>-AAPL</code> 移除标的\n"
        "• <code>wl</code> 查看当前监控列表\n"
        "\n"
        "<b>返回内容</b>\n"
        "• 大盘环境 (SPY/QQQ Regime)\n"
        "• Regime 分类 + 置信度\n"
        "• 关键点位 (VP/PDH/PDL/PMH/PML/Gamma)\n"
        "• 期权操作建议 (Call/Put/Spread/观望)\n"
        "• 交易策略 + 风险过滤\n"
        "\n"
        "<b>自动扫描</b>\n"
        "• 交易时段自动扫描强信号 (BREAKOUT)\n"
        "• 同一信号 30 分钟冷却\n"
        "• 每 session ≤2，每日 ≤3\n"
        "\n"
        "<b>限制说明</b>\n"
        "• 仅支持监控列表内标的\n"
        "• 小写/混合大小写不触发查询\n"
        "• 单字母 ticker (A/C/V) 不支持\n"
        "\n"
        "<b>风险提示</b>\n"
        "• 期权交易有风险，务必控制仓位并设置止损\n"
        "• 本系统仅提供参考，不构成投资建议"
    )
    await update.message.reply_text(text, parse_mode="HTML")
    _log_to_archive("us_playbook", "cmd_us_help", text, "us")
