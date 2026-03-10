"""Shared Telegram handler logic — used by both HK and US telegram modules."""

from __future__ import annotations

import asyncio
import html
import io
from typing import Callable

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.error import NetworkError, TimedOut
from telegram.ext import ContextTypes

from src.common.types import PlaybookResponse
from src.utils.logger import setup_logger

logger = setup_logger("common_telegram")

_esc = html.escape


async def _retry_send(coro_fn, max_retries=3):
    """Retry a Telegram send on transient network errors."""
    for attempt in range(max_retries):
        try:
            return await coro_fn()
        except (NetworkError, TimedOut) as e:
            if attempt == max_retries - 1:
                raise
            delay = 2 * (attempt + 1)
            logger.warning("Telegram send retry %d/%d: %s, wait %ds", attempt + 1, max_retries, type(e).__name__, delay)
            await asyncio.sleep(delay)


async def handle_query_base(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    predictor_key: str,
    normalize_fn: Callable[[str], str | None],
    analyzing_text: str,
    not_in_list_text: str,
    add_hint_template: str,
    wl_command: str,
) -> None:
    """Base handler for symbol query → playbook generation."""
    predictor = context.bot_data[predictor_key]
    text = update.message.text.strip()
    symbol = normalize_fn(text)
    if not symbol:
        return

    wl = predictor.watchlist
    if not wl.contains(symbol):
        await update.message.reply_text(
            f"{_esc(symbol)} {not_in_list_text}\n"
            "可用操作:\n"
            f"• 发送 <code>{add_hint_template.format(text=text, symbol=symbol)}</code> 添加\n"
            f"• 发送 <code>{wl_command}</code> 查看当前列表",
            parse_mode="HTML",
        )
        return

    try:
        try:
            await update.message.reply_text(
                f"⚙️ 正在分析 <b>{_esc(symbol)}</b>\n"
                f"{analyzing_text}",
                parse_mode="HTML",
                read_timeout=15,
                write_timeout=15,
            )
        except Exception:
            logger.debug("Failed to send 'analyzing' message for %s, continuing", symbol)

        result = await predictor.generate_playbook_for_symbol(symbol)
        if isinstance(result, str):
            result = PlaybookResponse(html=result)
        if result.chart:
            try:
                chart_bytes = result.chart
                await _retry_send(lambda: update.message.reply_photo(
                    photo=io.BytesIO(chart_bytes),
                    read_timeout=30,
                    write_timeout=30,
                ))
            except Exception:
                logger.warning("Failed to send chart for %s", symbol)
        await _retry_send(lambda: update.message.reply_text(
            result.html, parse_mode="HTML", read_timeout=30, write_timeout=30,
        ))
    except Exception as e:
        logger.exception("Playbook generation failed for %s", symbol)
        try:
            err_msg = f"❌ 分析失败\n原因: {_esc(str(e))}"
            await _retry_send(lambda: update.message.reply_text(
                err_msg, parse_mode="HTML", read_timeout=15, write_timeout=15,
            ))
        except Exception:
            logger.warning("Failed to send error message for %s", symbol)


async def handle_add_base(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    predictor_key: str,
    regex_match_groups: tuple[str, str],
    normalize_fn: Callable[[str], str | None],
    market_label: str,
    symbol_hint: str = "",
) -> None:
    """Base handler for +SYMBOL [name] → add to watchlist."""
    predictor = context.bot_data[predictor_key]
    raw_code, name = regex_match_groups
    name = name.strip()
    symbol = normalize_fn(raw_code)
    if not symbol:
        await update.message.reply_text("❌ 无效的代码格式")
        return

    added = predictor.watchlist.add(symbol, name)
    if added:
        display = f"{name} ({symbol})" if name else symbol
        hint = symbol_hint or symbol
        await update.message.reply_text(
            f"✅ 已添加 <b>{_esc(display)}</b> 到{market_label}监控列表\n"
            f"现在直接发送 <code>{hint}</code> 即可查看完整剧本。",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            f"{_esc(symbol)} 已在监控列表中",
        )


async def handle_remove_base(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    predictor_key: str,
    raw_code: str,
    normalize_fn: Callable[[str], str | None],
) -> None:
    """Base handler for -SYMBOL → remove from watchlist."""
    predictor = context.bot_data[predictor_key]
    symbol = normalize_fn(raw_code)
    if not symbol:
        await update.message.reply_text("❌ 无效的代码格式")
        return

    name = predictor.watchlist.get_name(symbol)
    removed = predictor.watchlist.remove(symbol)
    if removed:
        display = f"{name} ({symbol})" if name != symbol else symbol
        await update.message.reply_text(
            f"✅ 已移除 <b>{_esc(display)}</b>",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(f"{_esc(symbol)} 不在监控列表中")


async def handle_watchlist_base(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    predictor_key: str,
    market_label: str,
    empty_hint: str,
    format_fn: Callable[[list[dict]], str],
) -> None:
    """Base handler for watchlist view."""
    predictor = context.bot_data[predictor_key]
    items = predictor.watchlist.list_all()

    if not items:
        await update.message.reply_text(
            "监控列表为空\n"
            f"发送 <code>{empty_hint}</code> 添加标的。",
            parse_mode="HTML",
        )
        return

    # Build keyboard layout: 3 or 4 symbols per row
    keyboard = []
    row = []
    for item in items:
        symbol = item["symbol"]
        row.append(KeyboardButton(symbol))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    reply_markup = ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
        one_time_keyboard=False,
    )

    await update.message.reply_text(
        format_fn(items), 
        parse_mode="HTML",
        reply_markup=reply_markup
    )

