"""HK Predict Telegram integration — text-triggered playbook and watchlist management."""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes, MessageHandler, filters

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
_RE_WATCHLIST = re.compile(r"^(?:wl|watchlist)$", re.IGNORECASE)


def register_hk_commands(application, predictor: HKPredictor) -> None:
    """Register HK handlers onto an existing Telegram Application."""
    # Store predictor reference
    application.bot_data["hk_predictor"] = predictor

    # MessageHandlers with regex — exclude /commands
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
    # Query must be after add/remove to avoid matching +09988 as 09988
    application.add_handler(MessageHandler(
        filters.Regex(_RE_QUERY) & not_command,
        _handle_query, block=False,
    ))

    # Keep /hk_help as a command
    application.add_handler(CommandHandler(["hk_help", "hkh"], _cmd_hk_help, block=False))

    logger.info("HK Telegram handlers registered (text triggers + /hk_help)")


async def _get_predictor(context: ContextTypes.DEFAULT_TYPE) -> HKPredictor:
    return context.bot_data["hk_predictor"]


# ── Query: send aggregated playbook ──

async def _handle_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle symbol query (e.g. 09988, HK09988, HK.09988) → aggregated playbook."""
    predictor = await _get_predictor(context)
    text = update.message.text.strip()
    symbol = normalize_symbol(text)
    if not symbol:
        return

    wl = predictor.watchlist
    if not wl.contains(symbol):
        await update.message.reply_text(
            f"{_esc(symbol)} \u4e0d\u5728\u76d1\u63a7\u5217\u8868\u4e2d\u3002\n"
            f"\u53d1\u9001 <code>+{text}</code> \u6dfb\u52a0\uff0c\u6216 <code>wl</code> \u67e5\u770b\u5217\u8868\u3002",
            parse_mode="HTML",
        )
        return

    try:
        await update.message.reply_text("\u2699\ufe0f \u6b63\u5728\u5206\u6790...", parse_mode="HTML")
        msg = await predictor.generate_playbook_for_symbol(symbol)
        await update.message.reply_text(msg, parse_mode="HTML")
    except Exception as e:
        logger.exception("Playbook generation failed for %s", symbol)
        await update.message.reply_text(f"\u274c \u5206\u6790\u5931\u8d25: {_esc(str(e))}")


# ── Watchlist management ──

async def _handle_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle +09988 [name] → add to watchlist."""
    predictor = await _get_predictor(context)
    text = update.message.text.strip()
    m = _RE_ADD.match(text)
    if not m:
        return
    code = m.group(1)
    name = m.group(2).strip()
    symbol = normalize_symbol(code)
    if not symbol:
        await update.message.reply_text("\u274c \u65e0\u6548\u7684\u4ee3\u7801\u683c\u5f0f")
        return

    added = predictor.watchlist.add(symbol, name)
    if added:
        display = f"{name} ({symbol})" if name else symbol
        await update.message.reply_text(
            f"\u2705 \u5df2\u6dfb\u52a0 <b>{_esc(display)}</b> \u5230\u76d1\u63a7\u5217\u8868",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            f"{_esc(symbol)} \u5df2\u5728\u76d1\u63a7\u5217\u8868\u4e2d",
        )


async def _handle_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle -09988 → remove from watchlist."""
    predictor = await _get_predictor(context)
    text = update.message.text.strip()
    m = _RE_REMOVE.match(text)
    if not m:
        return
    code = m.group(1)
    symbol = normalize_symbol(code)
    if not symbol:
        await update.message.reply_text("\u274c \u65e0\u6548\u7684\u4ee3\u7801\u683c\u5f0f")
        return

    name = predictor.watchlist.get_name(symbol)
    removed = predictor.watchlist.remove(symbol)
    if removed:
        display = f"{name} ({symbol})" if name != symbol else symbol
        await update.message.reply_text(
            f"\u2705 \u5df2\u79fb\u9664 <b>{_esc(display)}</b>",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(f"{_esc(symbol)} \u4e0d\u5728\u76d1\u63a7\u5217\u8868\u4e2d")


async def _handle_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle wl / watchlist → show current watchlist."""
    predictor = await _get_predictor(context)
    items = predictor.watchlist.list_all()

    if not items:
        await update.message.reply_text("\u76d1\u63a7\u5217\u8868\u4e3a\u7a7a\u3002\u53d1\u9001 <code>+09988</code> \u6dfb\u52a0\u6807\u7684\u3002", parse_mode="HTML")
        return

    lines = [
        "\U0001f4cb <b>HK \u76d1\u63a7\u5217\u8868</b>",
        "\u2501" * 20,
        "",
    ]
    for item in items:
        sym = item["symbol"]
        name = item["name"]
        display = f"{_esc(name)} ({_esc(sym)})" if name != sym else _esc(sym)
        lines.append(f"  \u2022 {display}")

    lines.append("")
    lines.append(f"\u5171 {len(items)} \u4e2a\u6807\u7684")
    lines.append("\u53d1\u9001\u4ee3\u7801\u67e5\u8be2 | <code>+\u4ee3\u7801</code> \u6dfb\u52a0 | <code>-\u4ee3\u7801</code> \u5220\u9664")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ── Help ──

async def _cmd_hk_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/hk_help — show available commands."""
    text = (
        "<b>HK \u671f\u6743\u76d1\u63a7 \u4f7f\u7528\u8bf4\u660e</b>\n"
        "\u2501" * 20 + "\n"
        "\n"
        "<b>\u67e5\u8be2\u6807\u7684</b>\n"
        "\u53d1\u9001\u4ee3\u7801\u5373\u53ef\u83b7\u53d6\u805a\u5408\u5f0f Playbook:\n"
        "  <code>09988</code> / <code>HK09988</code> / <code>HK.09988</code>\n"
        "\n"
        "<b>Watchlist \u7ba1\u7406</b>\n"
        "  <code>+09988 \u963f\u91cc\u5df4\u5df4</code> \u2014 \u6dfb\u52a0\u6807\u7684 (\u540d\u79f0\u53ef\u9009)\n"
        "  <code>-09988</code> \u2014 \u79fb\u9664\u6807\u7684\n"
        "  <code>wl</code> \u2014 \u67e5\u770b\u5f53\u524d\u5217\u8868\n"
        "\n"
        "<b>\u8fd4\u56de\u5185\u5bb9</b>\n"
        "\u2022 \u5e02\u573a\u5b9a\u8c03 (Regime + \u7f6e\u4fe1\u5ea6)\n"
        "\u2022 \u5b9e\u65f6\u6570\u636e (VWAP, RVOL, VP, Gamma Wall)\n"
        "\u2022 \u671f\u6743\u64cd\u4f5c\u5efa\u8bae (Call/Put/Spread/\u89c2\u671b)\n"
        "\u2022 \u98ce\u9669\u8bf4\u660e\n"
        "\n"
        "<b>\u652f\u6301\u7684\u5efa\u8bae\u7c7b\u578b</b>\n"
        "\u2022 \u5355\u8155 Call / Put\n"
        "\u2022 Bull Put Spread / Bear Call Spread\n"
        "\u2022 \u89c2\u671b (\u9644\u91cd\u65b0\u8bc4\u4f30\u6761\u4ef6)\n"
        "\n"
        "<b>\u9650\u5236\u6761\u4ef6</b>\n"
        "\u2022 \u4ec5\u652f\u6301 Watchlist \u5185\u6807\u7684\n"
        "\u2022 \u65e0\u671f\u6743\u94fe\u65f6\u81ea\u52a8\u964d\u7ea7\u4e3a\u65b9\u5411\u5efa\u8bae\n"
        "\n"
        "<b>\u98ce\u9669\u63d0\u793a</b>\n"
        "\u2022 \u671f\u6743\u4ea4\u6613\u6709\u98ce\u9669\uff0c\u5efa\u8bae\u4e25\u683c\u6b62\u635f\n"
        "\u2022 \u672c\u7cfb\u7edf\u4ec5\u63d0\u4f9b\u53c2\u8003\uff0c\u4e0d\u6784\u6210\u6295\u8d44\u5efa\u8bae"
    )
    await update.message.reply_text(text, parse_mode="HTML")
