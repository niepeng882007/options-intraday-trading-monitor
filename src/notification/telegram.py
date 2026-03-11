from __future__ import annotations

import asyncio
import html
import re
import time
from collections import deque, OrderedDict
from datetime import datetime, timezone, timedelta
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from src.indicator.engine import IndicatorResult
from src.strategy.matcher import EntryQuality, Signal
from src.utils.logger import setup_logger

logger = setup_logger("telegram_notifier")

ET = timezone(timedelta(hours=-5))

# ── Templates ──

ENTRY_SIGNAL_TEMPLATE = (
    "{dir_emoji} <b>入场 | {strategy_name}</b>\n"
    "📌 {symbol} ${underlying_price} | {quality_inline}\n"
    "\n"
    "▶ 买入 {option_desc}\n"
    "📍 {order_type}\n"
    "{position_line}"
    "\n"
    "🎯 目标: ${tp_price} ({tp_arrow}{tp_pct})\n"
    "🚫 止损: ${sl_price} ({sl_arrow}{sl_pct})\n"
    "{time_exit_line}"
    "\n"
    "📊 {key_indicators}\n"
    "💡 {rationale}\n"
    "\n"
    "⏱ {trigger_time} ET{trading_window_hint}\n"
    "🆔 <code>{signal_id}</code>"
)

EXIT_SIGNAL_TEMPLATE = (
    "🔴 <b>出场 | {strategy_name}</b>\n"
    "📌 {symbol} ${entry_price} → ${current_price}\n"
    "\n"
    "📊 {exit_reason} ({pnl_arrow}{pnl_pct})\n"
    "📈 期权参考: {option_pnl_est}\n"
    "⏱ 持仓 {hold_duration}\n"
    "\n"
    "{cooldown_line}"
    "💰 今日累计: {daily_pnl}"
)

STRATEGY_UPDATE_TEMPLATE = (
    "🔄 <b>策略已更新</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "📋 策略: {strategy_name}\n"
    "🆔 ID: <code>{strategy_id}</code>\n"
    "状态: {status}"
)

_esc = html.escape

QUALITY_GRADE_EMOJI = {"A": "🟢", "B": "🟡", "C": "🟠", "D": "🔴"}

# ── Strategy → key indicators mapping ──

STRATEGY_KEY_INDICATORS: dict[str, list[str]] = {
    "vwap-low-vol-ambush":       ["vwap_dist", "volume_ratio", "candle_body"],
    "vwap-rejection-put":        ["vwap_dist", "rsi", "volume_ratio"],
    "bb-squeeze-ambush":         ["bb_width_pct", "rsi", "vwap_dist"],
    "bb-squeeze-bearish":        ["bb_width_pct", "rsi", "vwap_dist"],
    "extreme-oversold-reversal": ["rsi", "vwap_dist", "volume_spike"],
    "vwap-breakout-momentum":    ["vwap_dist", "volume_spike", "macd_hist"],
    "ema-momentum-breakout":     ["ema_cross", "rsi", "volume_ratio"],
    "breakdown-vwap-put":        ["vwap_dist", "volume_spike", "macd_hist"],
    "morning-trap-put":          ["rsi", "macd_hist", "vwap_dist"],
    "spy-vwap-ambush":           ["vwap_dist", "volume_ratio", "rsi"],
}

RIGHT_SIDE_STRATEGIES = {"vwap-breakout-momentum", "ema-momentum-breakout", "breakdown-vwap-put"}

MAX_NOTIFICATIONS_PER_MINUTE = 10
MAX_SIGNAL_CACHE = 20
COMMAND_TIMEOUT_SECONDS = 15


# ── Helper functions ──

def _fmt_volume(vol: int) -> str:
    if vol >= 1_000_000_000:
        return f"{vol / 1_000_000_000:.1f}B"
    if vol >= 1_000_000:
        return f"{vol / 1_000_000:.1f}M"
    if vol >= 1_000:
        return f"{vol / 1_000:.0f}K"
    return str(vol)


def _format_indicator_value(value: float | None, decimals: int = 4) -> str:
    if value is None:
        return "N/A"
    return f"{value:.{decimals}f}"


def _inline_quality(quality: EntryQuality | None) -> str:
    if quality is None:
        return ""
    emoji = QUALITY_GRADE_EMOJI.get(quality.grade, "⚪")
    return f"{emoji}{quality.grade} ({quality.score}分)"


def _build_strategy_rationale(meta: dict[str, Any]) -> str:
    desc = meta.get("description", "").strip()
    if not desc:
        return ""
    return f"💡 <b>策略逻辑:</b> {_esc(desc)}\n\n"


def _build_trading_window_hint(meta: dict[str, Any]) -> str:
    tw = meta.get("trading_window", "")
    if not tw:
        return ""
    return f" | 窗口 {tw}"


def _compute_price_levels(
    underlying_price: float,
    exit_conditions: dict | None,
    option_type: str,
) -> dict[str, Any]:
    """Extract TP/SL from exit_conditions rules and compute concrete prices."""
    result: dict[str, Any] = {
        "tp_price": 0.0, "tp_pct": "", "tp_arrow": "",
        "sl_price": 0.0, "sl_pct": "", "sl_arrow": "",
        "time_exit_min": 0,
        "has_trailing": False, "trail_activation": 0.0, "trail_distance": 0.0,
    }
    if not exit_conditions or not underlying_price:
        return result

    rules = exit_conditions.get("rules", [])
    tp_threshold = 0.0
    sl_threshold = 0.0

    for rule in rules:
        rtype = rule.get("type", "")
        if rtype == "take_profit_pct":
            tp_threshold = rule.get("threshold", 0)
        elif rtype == "stop_loss_pct":
            sl_threshold = rule.get("threshold", 0)
        elif rtype == "time_exit":
            result["time_exit_min"] = rule.get("minutes_before_close", 15)
        elif rtype == "trailing_stop":
            result["has_trailing"] = True
            result["trail_activation"] = rule.get("activation_pct", 0)
            result["trail_distance"] = rule.get("trail_pct", 0)

    is_put = option_type == "put"

    if tp_threshold:
        if is_put:
            result["tp_price"] = underlying_price * (1 - tp_threshold)
            result["tp_arrow"] = "↓"
        else:
            result["tp_price"] = underlying_price * (1 + tp_threshold)
            result["tp_arrow"] = "↑"
        result["tp_pct"] = f"{abs(tp_threshold) * 100:.1f}%"

    if sl_threshold:
        if is_put:
            # sl_threshold is negative; put SL = stock rises → price * (1 - sl_threshold) = price * (1 + |sl|)
            result["sl_price"] = underlying_price * (1 - sl_threshold)
            result["sl_arrow"] = "↑"
        else:
            # sl_threshold is negative; call SL = stock drops → price * (1 + sl_threshold) = price * (1 - |sl|)
            result["sl_price"] = underlying_price * (1 + sl_threshold)
            result["sl_arrow"] = "↓"
        result["sl_pct"] = f"{abs(sl_threshold) * 100:.2g}%"

    return result


def _compute_position_size(
    risk_config: dict | None,
    underlying_price: float,
    sl_pct: float,
    option_price_est: float | None = None,
) -> str:
    """Estimate position size based on risk config. Returns e.g. '2张 (~$400)' or ''."""
    if not risk_config:
        return ""
    account_size = risk_config.get("account_size", 0)
    risk_pct = risk_config.get("risk_per_trade_pct", 0)
    if not account_size or not risk_pct:
        return ""

    opt_price = option_price_est or risk_config.get("default_option_price_est", 2.0)
    risk_amount = account_size * risk_pct  # e.g. $200

    # Estimate per-contract risk: option_price * 100 shares * |sl_stock_%| * 15x leverage
    abs_sl = abs(sl_pct) if sl_pct else 0.003  # default 0.3%
    risk_per_contract = opt_price * 100 * abs_sl * 15
    if risk_per_contract <= 0:
        return ""

    contracts = max(1, int(risk_amount / risk_per_contract))
    # Cap: don't exceed 10% of account per trade
    max_contracts = max(1, int(account_size * 0.1 / (opt_price * 100)))
    contracts = min(contracts, max_contracts)
    cost = contracts * opt_price * 100
    return f"{contracts}张 (~${cost:.0f})"


def _suggest_order_type(strategy_id: str) -> str:
    if strategy_id in RIGHT_SIDE_STRATEGIES:
        return "市价单 (突破追入)"
    return "限价单 @ Bid附近"


def _fmt_key_indicator(key: str, ind: IndicatorResult) -> str | None:
    """Format a single indicator key into a short Chinese label."""
    if key == "rsi":
        if ind.rsi is not None:
            return f"RSI {ind.rsi:.0f}"
    elif key == "vwap_dist":
        if ind.vwap_distance_pct is not None:
            return f"VWAP{ind.vwap_distance_pct:+.2f}%"
    elif key == "volume_ratio":
        if ind.volume_ratio is not None:
            label = "缩量" if ind.volume_ratio < 0.8 else "放量" if ind.volume_ratio > 1.2 else ""
            return f"量比{ind.volume_ratio:.1f}x{label}"
    elif key == "macd_hist":
        if ind.macd_histogram is not None:
            arrow = "↗" if ind.macd_histogram > 0 else "↘"
            return f"MACD柱{arrow}"
    elif key == "bb_width_pct":
        if ind.bb_width_pct is not None:
            return f"BB宽{ind.bb_width_pct:.1f}%"
    elif key == "volume_spike":
        if ind.volume_spike is not None:
            return f"量突变{ind.volume_spike:.1f}x"
    elif key == "ema_cross":
        if ind.ema_9 is not None and ind.ema_21 is not None:
            if ind.ema_9 > ind.ema_21:
                return "EMA9/21金叉"
            return "EMA9/21死叉"
    elif key == "candle_body":
        if ind.candle_body_pct is not None:
            return f"K线{ind.candle_body_pct:.2f}%"
    return None


def _build_key_indicators(
    strategy_id: str,
    indicators_by_tf: dict[str, IndicatorResult | None] | None,
) -> str:
    """Build a single-line '|'-separated key indicators string."""
    if not indicators_by_tf:
        return "N/A"

    keys = STRATEGY_KEY_INDICATORS.get(strategy_id, ["rsi", "vwap_dist", "volume_ratio"])

    # Pick best available timeframe: prefer 5m, fallback 1m, 15m
    ind: IndicatorResult | None = None
    for tf in ("5m", "1m", "15m"):
        if indicators_by_tf.get(tf) is not None:
            ind = indicators_by_tf[tf]
            break
    if ind is None:
        return "N/A"

    parts: list[str] = []
    for key in keys:
        formatted = _fmt_key_indicator(key, ind)
        if formatted:
            parts.append(formatted)

    return " | ".join(parts) if parts else "N/A"


def _shorten_rationale(description: str, max_len: int = 35) -> str:
    """Shorten strategy description to first sentence or max_len chars."""
    text = description.strip()
    if not text:
        return ""
    # Try to cut at first period/comma
    for sep in ("。", "，", ".", ","):
        idx = text.find(sep)
        if 0 < idx <= max_len:
            return text[:idx]
    if len(text) <= max_len:
        return text
    return text[:max_len] + "…"


def _build_entry_keyboard(signal_id: str) -> InlineKeyboardMarkup:
    """入场信号按钮: [确认][跳过] / [详情]"""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ 确认入场", callback_data=f"cfm:{signal_id}"),
            InlineKeyboardButton("⏭ 跳过", callback_data=f"skip:{signal_id}"),
        ],
        [InlineKeyboardButton("📋 详情", callback_data=f"dtl:{signal_id}")],
    ])


def _build_actioned_keyboard(action_text: str) -> InlineKeyboardMarkup:
    """操作完成后的单按钮（防双击）"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(action_text, callback_data="noop")],
    ])


def _build_indicator_snapshot(
    indicators_by_tf: dict[str, IndicatorResult | None] | None,
) -> str:
    """Full indicator snapshot — used only for /detail command."""
    if not indicators_by_tf:
        return ""

    sections: list[str] = []
    for timeframe in ("15m", "5m", "1m"):
        ind = indicators_by_tf.get(timeframe)
        if ind is None:
            continue

        lines = [f"📈 <b>指标快照 [{timeframe}]</b>:"]
        lines.append(f"   RSI(14): {_format_indicator_value(ind.rsi, 2)}")
        lines.append(
            f"   MACD: {_format_indicator_value(ind.macd_line)} / "
            f"Signal: {_format_indicator_value(ind.macd_signal)} / "
            f"Hist: {_format_indicator_value(ind.macd_histogram)}"
        )
        ema_parts = [f"9={_format_indicator_value(ind.ema_9, 2)}"]
        ema_parts.append(f"21={_format_indicator_value(ind.ema_21, 2)}")
        if ind.ema_50 is not None:
            ema_parts.append(f"50={_format_indicator_value(ind.ema_50, 2)}")
        if ind.ema_200 is not None:
            ema_parts.append(f"200={_format_indicator_value(ind.ema_200, 2)}")
        lines.append(f"   EMA: {' / '.join(ema_parts)}")
        lines.append(f"   VWAP: {_format_indicator_value(ind.vwap, 2)}")
        if ind.bb_width_pct is not None:
            lines.append(f"   BB宽度: {_format_indicator_value(ind.bb_width_pct, 4)}%")
        lines.append(f"   ATR(14): {_format_indicator_value(ind.atr, 4)}")
        if ind.candle_body_pct is not None:
            lines.append(f"   K线实体: {_format_indicator_value(ind.candle_body_pct, 3)}%")
        if ind.volume_ratio is not None:
            lines.append(f"   量比: {_format_indicator_value(ind.volume_ratio, 2)}x")
        sections.append("\n".join(lines))

    if not sections:
        return ""
    return "\n".join(sections) + "\n"


class TelegramNotifier:
    """Sends trading signals via Telegram and handles Bot commands."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        collector: Any = None,
        strategy_loader: Any = None,
        state_manager: Any = None,
        sqlite_store: Any = None,
    ) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._collector = collector
        self._strategy_loader = strategy_loader
        self._state_manager = state_manager
        self._sqlite_store = sqlite_store
        self._data_source_label = "Futu OpenD (实时推送)"
        self._app: Application | None = None
        self._send_timestamps: deque[float] = deque()
        self._paused_until: float = 0.0
        self._signal_cache: OrderedDict[str, dict] = OrderedDict()

    # ── Bot setup ──

    def build_app(self) -> Application:
        self._app = (
            Application.builder()
            .token(self._bot_token)
            .concurrent_updates(True)
            .build()
        )
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("market", self._cmd_market))
        self._app.add_handler(CommandHandler("chain", self._cmd_chain))
        self._app.add_handler(CommandHandler("strategies", self._cmd_strategies))
        self._app.add_handler(CommandHandler("enable", self._cmd_enable))
        self._app.add_handler(CommandHandler("disable", self._cmd_disable))
        self._app.add_handler(CommandHandler("pause", self._cmd_pause))
        self._app.add_handler(CommandHandler("history", self._cmd_history))
        self._app.add_handler(CommandHandler("confirm", self._cmd_confirm))
        self._app.add_handler(CommandHandler("skip", self._cmd_skip))
        self._app.add_handler(CommandHandler("detail", self._cmd_detail))
        self._app.add_handler(CommandHandler("test", self._cmd_test))
        self._app.add_handler(CommandHandler("conn", self._cmd_conn))
        self._app.add_handler(CommandHandler("messages", self._cmd_messages))
        self._app.add_handler(
            CallbackQueryHandler(self._on_callback_query, pattern=r"^(cfm|skip|dtl|noop)")
        )
        self._app.add_error_handler(self._on_error)
        return self._app

    async def start_polling(self) -> None:
        if self._app is None:
            self.build_app()
        assert self._app is not None
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)  # type: ignore[union-attr]
        logger.info("Telegram bot polling started")

    async def stop(self) -> None:
        if self._app:
            await self._app.updater.stop()  # type: ignore[union-attr]
            await self._app.stop()
            await self._app.shutdown()

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Telegram handler error: %s", context.error, exc_info=context.error)

    # ── Notification sending ──

    async def _reply_and_log(
        self,
        update: Update,
        text: str,
        trigger: str,
        market: str = "us",
        **kwargs,
    ) -> None:
        """Reply to user and log the message to archive."""
        await update.message.reply_text(text, **kwargs)  # type: ignore[union-attr]
        from src.store import message_archive
        message_archive.log("us_pipeline", trigger, text, market)

    async def send_entry_signal(
        self,
        signal: Signal,
        signal_id: str,
        underlying_price: float = 0.0,
        quote_detail: dict[str, Any] | None = None,
        indicators_by_tf: dict[str, IndicatorResult | None] | None = None,
        exit_conditions: dict | None = None,
        option_filter: dict | None = None,
        risk_config: dict | None = None,
    ) -> bool:
        if self._is_paused():
            logger.debug("Notifications paused, skipping entry signal")
            return False

        if not self._rate_limit_ok(signal.priority):
            logger.warning("Rate limit exceeded, skipping entry signal")
            return False

        trigger_time = datetime.now(ET).strftime("%H:%M")
        meta = signal.strategy_meta or {}

        # Determine direction
        opt_filter = option_filter or {}
        option_type = opt_filter.get("type", "call")
        is_put = option_type == "put"
        dir_emoji = "🔴" if is_put else "🟢"

        # Option description
        moneyness = opt_filter.get("moneyness", "ATM")
        max_dte = opt_filter.get("max_dte", 0)
        dte_label = "0DTE" if max_dte <= 1 else f"{max_dte}DTE"
        opt_type_label = "Put" if is_put else "Call"
        option_desc = f"{moneyness} {opt_type_label} {dte_label}"

        # Quality inline
        quality_inline = _inline_quality(signal.entry_quality)

        # Price levels
        levels = _compute_price_levels(underlying_price, exit_conditions, option_type)

        # Order type
        order_type = _suggest_order_type(signal.strategy_id)

        # Position size
        sl_threshold = 0.0
        if exit_conditions:
            for rule in exit_conditions.get("rules", []):
                if rule.get("type") == "stop_loss_pct":
                    sl_threshold = rule.get("threshold", 0)
        position_text = _compute_position_size(risk_config, underlying_price, sl_threshold)
        position_line = f"💰 建议: {position_text}\n" if position_text else ""

        # Key indicators
        key_indicators = _build_key_indicators(signal.strategy_id, indicators_by_tf)

        # Rationale
        rationale = _shorten_rationale(meta.get("description", ""))

        # Time exit line
        time_exit_line = ""
        if levels["time_exit_min"]:
            time_exit_line = f"⏰ 尾盘{levels['time_exit_min']}min强退\n"

        # Trading window hint
        trading_window_hint = _build_trading_window_hint(meta)

        message = ENTRY_SIGNAL_TEMPLATE.format(
            dir_emoji=dir_emoji,
            strategy_name=_esc(signal.strategy_name),
            symbol=_esc(signal.symbol),
            underlying_price=f"{underlying_price:.2f}",
            quality_inline=quality_inline,
            option_desc=option_desc,
            order_type=order_type,
            position_line=position_line,
            tp_price=f"{levels['tp_price']:.2f}" if levels["tp_price"] else "N/A",
            tp_arrow=levels["tp_arrow"],
            tp_pct=levels["tp_pct"] or "N/A",
            sl_price=f"{levels['sl_price']:.2f}" if levels["sl_price"] else "N/A",
            sl_arrow=levels["sl_arrow"],
            sl_pct=levels["sl_pct"] or "N/A",
            time_exit_line=time_exit_line,
            key_indicators=key_indicators,
            rationale=_esc(rationale),
            trigger_time=trigger_time,
            trading_window_hint=trading_window_hint,
            signal_id=_esc(signal_id),
        )

        # Cache signal details for /detail command
        self._cache_signal(signal_id, {
            "signal": signal,
            "underlying_price": underlying_price,
            "indicators_by_tf": indicators_by_tf,
            "exit_conditions": exit_conditions,
            "timestamp": time.time(),
        })

        keyboard = _build_entry_keyboard(signal_id)
        return await self._send_message(
            message, reply_markup=keyboard,
            source="us_pipeline", trigger="entry_signal", market="us",
        )

    async def send_exit_signal(
        self,
        signal: Signal,
        underlying_price: float = 0.0,
        entry_price: float = 0.0,
        current_price: float = 0.0,
        hold_duration: str = "",
        cooldown_seconds: int = 120,
        daily_pnl: float = 0.0,
        option_type: str = "call",
    ) -> bool:
        if self._is_paused() and signal.priority != "high":
            return False

        # PnL calculation — for put, stock drop = profit
        stock_move_pct = ((current_price - entry_price) / entry_price * 100) if entry_price > 0 else 0
        if option_type == "put":
            effective_pnl = -stock_move_pct  # put profits when stock drops
        else:
            effective_pnl = stock_move_pct

        pnl_arrow = "↑" if effective_pnl >= 0 else "↓"

        # Option PnL estimate: ~12-20x stock move
        option_pnl_low = effective_pnl * 12
        option_pnl_high = effective_pnl * 20
        option_pnl_est = f"{option_pnl_low:+.0f}% ~ {option_pnl_high:+.0f}%"

        # Cooldown
        cooldown_min = cooldown_seconds // 60
        cooldown_line = f"📋 冷却 {cooldown_min}min 后可重新入场\n" if cooldown_min else ""

        # Daily PnL display
        daily_pnl_str = f"{daily_pnl:+.1f}%"

        message = EXIT_SIGNAL_TEMPLATE.format(
            strategy_name=_esc(signal.strategy_name),
            symbol=_esc(signal.symbol),
            entry_price=f"{entry_price:.2f}",
            current_price=f"{current_price:.2f}",
            exit_reason=_esc(signal.exit_reason),
            pnl_arrow=pnl_arrow,
            pnl_pct=f"{abs(effective_pnl):.1f}%",
            option_pnl_est=option_pnl_est,
            hold_duration=_esc(hold_duration),
            cooldown_line=cooldown_line,
            daily_pnl=daily_pnl_str,
        )
        return await self._send_message(
            message, source="us_pipeline", trigger="exit_signal", market="us",
        )

    async def send_strategy_update(
        self, strategy_id: str, strategy_name: str, status: str
    ) -> bool:
        message = STRATEGY_UPDATE_TEMPLATE.format(
            strategy_name=_esc(strategy_name),
            strategy_id=_esc(strategy_id),
            status=_esc(status),
        )
        return await self._send_message(
            message, source="us_pipeline", trigger="strategy_update", market="us",
        )

    async def send_text(
        self,
        text: str,
        source: str = "system",
        trigger: str = "generic",
        market: str = "us",
    ) -> bool:
        return await self._send_message(
            text, source=source, trigger=trigger, market=market,
        )

    # ── Signal cache for /detail ──

    def _cache_signal(self, signal_id: str, data: dict) -> None:
        self._signal_cache[signal_id] = data
        while len(self._signal_cache) > MAX_SIGNAL_CACHE:
            self._signal_cache.popitem(last=False)

    _STRIP_HTML_RE = re.compile(r"<[^>]+>")

    async def _send_message(
        self,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
        source: str = "system",
        trigger: str = "generic",
        market: str = "us",
    ) -> bool:
        if not (self._app and self._app.bot):
            return False
        try:
            await self._app.bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
            self._send_timestamps.append(time.time())
            from src.store import message_archive
            message_archive.log(source, trigger, text, market)
            return True
        except BadRequest as exc:
            logger.warning("HTML parse failed, retrying as plain text: %s", exc)
            try:
                plain = self._STRIP_HTML_RE.sub("", text)
                await self._app.bot.send_message(
                    chat_id=self._chat_id,
                    text=plain,
                )
                self._send_timestamps.append(time.time())
                from src.store import message_archive
                message_archive.log(source, trigger, plain, market)
                return True
            except Exception:
                logger.exception("Plain-text fallback also failed")
        except Exception:
            logger.exception("Failed to send Telegram message")
        return False

    # ── Rate limiting ──

    def _rate_limit_ok(self, priority: str = "medium") -> bool:
        if priority == "high":
            return True
        now = time.time()
        while self._send_timestamps and now - self._send_timestamps[0] > 60:
            self._send_timestamps.popleft()
        return len(self._send_timestamps) < MAX_NOTIFICATIONS_PER_MINUTE

    def _is_paused(self) -> bool:
        return time.time() < self._paused_until

    # ── Bot command handlers ──

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        lines = ["📊 <b>系统状态</b>", "━━━━━━━━━━━━━━━━━━━━"]

        if self._strategy_loader:
            strategies = self._strategy_loader.get_active()
            lines.append(f"📋 活跃策略: {len(strategies)}")
            for s in strategies:
                lines.append(f"  • {s.name} ({s.strategy_id})")

        if self._state_manager:
            holding = self._state_manager.get_holding_positions()
            lines.append(f"\n💼 持仓中: {len(holding)}")
            for h in holding:
                lines.append(
                    f"  • {h.strategy_id}:{h.symbol} @ ${h.position.entry_price:.2f}"
                )

        paused = "是 ⏸" if self._is_paused() else "否"
        lines.append(f"\n⏸ 静默: {paused}")
        lines.append(f"⏱ {datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S')} ET")

        await self._reply_and_log(update, "\n".join(lines), "cmd_status", parse_mode="HTML")

    async def _cmd_market(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show real-time trading info for all monitored symbols."""
        if self._collector is None:
            await self._reply_and_log(update, "数据采集器未初始化", "cmd_market")
            return
        if self._strategy_loader is None:
            await self._reply_and_log(update, "策略管理器未初始化", "cmd_market")
            return

        symbols = sorted(self._strategy_loader.get_all_symbols())
        if not symbols:
            await self._reply_and_log(update, "监控列表为空", "cmd_market")
            return

        lines = ["📊 <b>实时行情</b>", "━━━━━━━━━━━━━━━━━━━━"]
        max_cache_age = 0.0

        for symbol in symbols:
            try:
                q = self._collector.get_cached_quote(symbol)
                if q is not None:
                    max_cache_age = max(max_cache_age, time.time() - q.timestamp)
                else:
                    q = await asyncio.wait_for(
                        self._collector.get_stock_quote(symbol),
                        timeout=COMMAND_TIMEOUT_SECONDS,
                    )

                # Line 1: symbol, price, change
                if q.change_pct is not None:
                    arrow = "🔺" if q.change_pct >= 0 else "🔻"
                    change_str = f" {arrow}{q.change_pct:+.2f}%"
                else:
                    change_str = ""
                lines.append(f"<b>{_esc(symbol)}</b> ${q.price:.2f}{change_str}")

                # Line 2: OHLC
                if q.open_price is not None:
                    lines.append(
                        f"  O {q.open_price:.2f} H {q.high_price:.2f}"
                        f" L {q.low_price:.2f}"
                    )

                # Line 3: volume + turnover rate + amplitude
                vol_parts: list[str] = [f"  Vol {_fmt_volume(q.volume)}"]
                if q.turnover_rate is not None:
                    vol_parts.append(f"换手{q.turnover_rate:.2f}%")
                if q.amplitude is not None:
                    vol_parts.append(f"振幅{q.amplitude:.2f}%")
                lines.append(" | ".join(vol_parts))

                # Line 4: bid/ask spread
                spread = q.ask - q.bid if q.ask and q.bid else 0
                lines.append(f"  Bid {q.bid:.2f} / Ask {q.ask:.2f} (spd {spread:.2f})")
                lines.append("")  # blank separator
            except Exception:
                lines.append(f"<b>{_esc(symbol)}</b> ❌ 查询失败")
                lines.append("")

        cache_note = f" | 缓存≤{int(max_cache_age)}s" if max_cache_age > 0 else ""
        lines.append(
            f"⏱ {datetime.now(ET).strftime('%H:%M:%S')} ET"
            f" | {self._data_source_label}{cache_note}"
        )
        await self._reply_and_log(update, "\n".join(lines), "cmd_market", parse_mode="HTML")

    async def _cmd_chain(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args or len(context.args) < 4:
            text = (
                "❌ <b>缺少参数</b>\n"
                "点击下方灰色区域可快速复制修改：\n\n"
                "<code>/chain AAPL 230 C 0321</code>\n\n"
                "<b>参数说明</b>：\n"
                "• 第1位: 股票代码 (如 AAPL)\n"
                "• 第2位: 行权价 (如 230)\n"
                "• 第3位: 类型 C 或 P (Call/Put)\n"
                "• 第4位: 到期日 MMDD (如 0321)"
            )
            await self._reply_and_log(update, text, "cmd_chain", parse_mode="HTML")
            return

        symbol = context.args[0].upper()
        strike = context.args[1]
        opt_type = context.args[2].upper()

        if self._collector is None:
            await self._reply_and_log(update, "数据采集器未初始化", "cmd_chain")
            return

        try:
            options = await asyncio.wait_for(
                self._collector.get_option_chain(symbol),
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
            matched = [
                o for o in options
                if abs(o.strike - float(strike)) < 0.01
                and o.option_type == ("call" if opt_type == "C" else "put")
            ]
            if not matched:
                text = f"未找到匹配的期权: {symbol} {strike} {opt_type}"
            else:
                opt = matched[0]
                text = (
                    f"📋 <b>{opt.contract_symbol}</b>\n"
                    f"标的: {symbol} | {opt.option_type} ${opt.strike}\n"
                    f"到期: {opt.expiration}\n"
                    f"Bid: ${opt.bid:.2f} / Ask: ${opt.ask:.2f}\n"
                    f"最新: ${opt.last:.2f}\n"
                    f"成交量: {opt.volume:,} | OI: {opt.open_interest:,}\n"
                    f"IV: {opt.implied_volatility:.2%}\n"
                    f"⚠️ 数据源: {self._data_source_label}"
                )
        except TimeoutError:
            text = f"⏱ 查询超时: {symbol} 数据源无响应"
        except Exception as exc:
            text = f"查询失败: {exc}"

        await self._reply_and_log(update, text, "cmd_chain", parse_mode="HTML")

    async def _cmd_strategies(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._strategy_loader is None:
            await self._reply_and_log(update, "策略管理器未初始化", "cmd_strategies")
            return

        strategies = self._strategy_loader.strategies
        if not strategies:
            await self._reply_and_log(update, "暂无策略配置", "cmd_strategies")
            return

        lines = ["📋 <b>所有策略</b>", "━━━━━━━━━━━━━━━━━━━━"]
        for sid, s in strategies.items():
            icon = "🟢" if s.enabled else "🔴"
            lines.append(f"{icon} {s.name}")
            lines.append(f"   ID: <code>{sid}</code>")
            lines.append(f"   标的: {', '.join(s.underlyings)}")

        await self._reply_and_log(update, "\n".join(lines), "cmd_strategies", parse_mode="HTML")

    async def _cmd_enable(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await self._reply_and_log(update, "用法: /enable <strategy_id>", "cmd_enable")
            return
        sid = context.args[0]
        if self._strategy_loader and self._strategy_loader.set_enabled(sid, True):
            await self._reply_and_log(update, f"✅ 策略 {sid} 已启用", "cmd_enable")
        else:
            await self._reply_and_log(update, f"❌ 策略 {sid} 不存在", "cmd_enable")

    async def _cmd_disable(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await self._reply_and_log(update, "用法: /disable <strategy_id>", "cmd_disable")
            return
        sid = context.args[0]
        if self._strategy_loader and self._strategy_loader.set_enabled(sid, False):
            await self._reply_and_log(update, f"✅ 策略 {sid} 已禁用", "cmd_disable")
        else:
            await self._reply_and_log(update, f"❌ 策略 {sid} 不存在", "cmd_disable")

    async def _cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        minutes = 30
        if context.args:
            try:
                minutes = int(context.args[0])
            except ValueError:
                pass
        self._paused_until = time.time() + minutes * 60
        await self._reply_and_log(
            update,
            f"⏸ 通知已静默 {minutes} 分钟 (至 "
            f"{datetime.now(ET).strftime('%H:%M')} ET + {minutes}min)",
            "cmd_pause",
        )

    async def _cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._sqlite_store is None:
            await self._reply_and_log(update, "存储未初始化", "cmd_history")
            return

        signals = self._sqlite_store.get_today_signals()
        if not signals:
            await self._reply_and_log(update, "📭 今日暂无信号记录", "cmd_history")
            return

        lines = ["📜 <b>今日信号记录</b>", "━━━━━━━━━━━━━━━━━━━━"]
        for s in signals[-20:]:
            icon = "🟢" if s.get("signal_type") == "entry" else "🔴"
            t = datetime.fromtimestamp(s.get("timestamp", 0), tz=ET).strftime("%H:%M:%S")
            lines.append(f"{icon} {t} | {s.get('strategy_name', '')} | {s.get('symbol', '')}")

        await self._reply_and_log(update, "\n".join(lines), "cmd_history", parse_mode="HTML")

    async def _cmd_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args or len(context.args) < 2:
            await self._reply_and_log(
                update,
                "用法: /confirm <signal_id> <股票价格>\n"
                "  请输入建仓时底层股票价格（非期权价格）",
                "cmd_confirm",
            )
            return

        signal_id = context.args[0]
        try:
            entry_price = float(context.args[1])
        except ValueError:
            await self._reply_and_log(update, "价格格式错误", "cmd_confirm")
            return

        if self._state_manager is None:
            await self._reply_and_log(update, "状态管理器未初始化", "cmd_confirm")
            return

        if self._state_manager.confirm_entry(signal_id, entry_price):
            await self._reply_and_log(
                update,
                f"✅ 已确认建仓 {signal_id} @ ${entry_price:.2f} (股票价格)\n开始追踪出场条件",
                "cmd_confirm",
            )
        else:
            await self._reply_and_log(
                update,
                f"❌ 信号 {signal_id} 无法确认 (不存在或已过期)",
                "cmd_confirm",
            )

    async def _cmd_skip(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await self._reply_and_log(update, "用法: /skip <signal_id>", "cmd_skip")
            return

        signal_id = context.args[0]
        if self._state_manager and self._state_manager.skip_entry(signal_id):
            await self._reply_and_log(update, f"⏭ 已跳过信号 {signal_id}", "cmd_skip")
        else:
            await self._reply_and_log(update, f"❌ 信号 {signal_id} 不存在或已处理", "cmd_skip")

    async def _cmd_detail(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show full indicator snapshot for a cached signal."""
        if not context.args:
            await self._reply_and_log(update, "用法: /detail <signal_id>", "cmd_detail")
            return

        signal_id = context.args[0]
        cached = self._signal_cache.get(signal_id)
        if not cached:
            await self._reply_and_log(
                update, f"❌ 信号 {signal_id} 详情已过期或不存在", "cmd_detail",
            )
            return

        sig: Signal = cached["signal"]
        indicators_by_tf = cached.get("indicators_by_tf")

        lines = [
            f"📋 <b>信号详情 | {_esc(sig.strategy_name)}</b>",
            f"🆔 {_esc(signal_id)} | {_esc(sig.symbol)}",
            "",
        ]

        # Full indicator snapshot
        snapshot = _build_indicator_snapshot(indicators_by_tf)
        if snapshot:
            lines.append(snapshot)

        # Conditions detail
        if sig.conditions_detail:
            lines.append("📊 <b>触发条件:</b>")
            for c in sig.conditions_detail:
                lines.append(f"   {_esc(c)}")

        await self._reply_and_log(update, "\n".join(lines), "cmd_detail", parse_mode="HTML")

    async def _cmd_test(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """发送模拟入场与出场提醒，用于验证 Telegram 推送链路是否正常。"""
        TEST_SIGNAL_ID = f"TEST-{int(time.time())}"
        TEST_UNDERLYING_PRICE = 185.50
        TEST_ENTRY_PRICE = 185.20
        TEST_EXIT_PRICE = 186.15

        await update.message.reply_text("🧪 开始发送测试提醒...")  # type: ignore[union-attr]

        entry_signal = Signal(
            strategy_id="vwap-low-vol-ambush",
            strategy_name="VWAP 极度缩量埋伏",
            signal_type="entry",
            symbol="AAPL",
            conditions_detail=[
                "RSI(value) [5m] crosses_above 30 → ✅ 当前=35.0000 (前值=28.0000)",
                "MACD(histogram) [5m] turns_positive → ✅ 当前=0.2000 (前值=-0.1000)",
            ],
            priority="high",
            timestamp=time.time(),
            strategy_meta={
                "description": "价格回落到VWAP附近且成交量极度萎缩，在横盘中从容埋伏进场。",
                "trading_window": "09:45-11:00",
            },
            entry_quality=EntryQuality(score=82, grade="A", reasons=["VWAP附近", "缩量"]),
        )

        mock_indicators = {
            "5m": IndicatorResult(
                symbol="AAPL", timeframe="5m", timestamp=time.time(),
                rsi=35.0, macd_line=0.1500, macd_signal=0.0800, macd_histogram=0.2000,
                ema_9=185.30, ema_21=184.90, vwap=185.20, atr=1.2500,
                vwap_distance_pct=0.16, volume_ratio=0.8, candle_body_pct=0.03,
            ),
        }
        mock_exit_conditions = {
            "operator": "OR",
            "rules": [
                {"type": "take_profit_pct", "threshold": 0.005},
                {"type": "stop_loss_pct", "threshold": -0.003},
                {"type": "time_exit", "minutes_before_close": 15},
            ],
        }
        mock_option_filter = {"type": "call", "max_dte": 2, "moneyness": "ATM"}
        mock_risk_config = {
            "account_size": 10000,
            "risk_per_trade_pct": 0.02,
            "default_option_price_est": 2.00,
        }

        entry_sent = await self.send_entry_signal(
            entry_signal,
            TEST_SIGNAL_ID,
            underlying_price=TEST_UNDERLYING_PRICE,
            indicators_by_tf=mock_indicators,
            exit_conditions=mock_exit_conditions,
            option_filter=mock_option_filter,
            risk_config=mock_risk_config,
        )

        if not entry_sent:
            await update.message.reply_text(  # type: ignore[union-attr]
                "❌ 入场提醒发送失败\n"
                "请检查:\n"
                "  • TELEGRAM_BOT_TOKEN 是否正确\n"
                "  • TELEGRAM_CHAT_ID 是否正确\n"
                "  • Bot 是否已被封禁或通知已静默"
            )
            return

        await update.message.reply_text("✅ 入场提醒发送成功")  # type: ignore[union-attr]

        # Test put exit signal
        exit_signal = Signal(
            strategy_id="vwap-low-vol-ambush",
            strategy_name="VWAP 极度缩量埋伏",
            signal_type="exit",
            symbol="AAPL",
            exit_reason="止盈",
            priority="high",
        )

        exit_sent = await self.send_exit_signal(
            exit_signal,
            underlying_price=TEST_EXIT_PRICE,
            entry_price=TEST_ENTRY_PRICE,
            current_price=TEST_EXIT_PRICE,
            hold_duration="45m",
            cooldown_seconds=180,
            daily_pnl=0.8,
            option_type="call",
        )

        if exit_sent:
            await update.message.reply_text("✅ 出场提醒发送成功\n\n🟢 Telegram 推送链路正常")  # type: ignore[union-attr]
        else:
            await update.message.reply_text("❌ 出场提醒发送失败")  # type: ignore[union-attr]

    async def _cmd_conn(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show Futu connection status and diagnostics."""
        if self._collector is None:
            await self._reply_and_log(update, "数据采集器未初始化", "cmd_conn")
            return

        try:
            info = await asyncio.wait_for(
                self._collector.get_connection_info(),
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            await self._reply_and_log(update, "⏱ 连接状态查询超时", "cmd_conn")
            return
        except Exception as exc:
            await self._reply_and_log(update, f"❌ 查询失败: {exc}", "cmd_conn")
            return

        connected = info.get("connected", False)
        status_icon = "🟢" if connected else "🔴"
        global_state = info.get("global_state", "N/A")
        state_icon = "✅" if global_state == "OK" else "❌"

        lines = [
            "📡 <b>Futu 连接状态</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            f"{status_icon} 连接: {'已连接' if connected else '已断开'}",
            f"🖥 节点: {info.get('host')}:{info.get('port')}",
            f"{state_icon} 状态: {global_state}",
        ]

        # Server info
        server_ver = info.get("server_ver")
        if server_ver and server_ver != "N/A":
            lines.append(f"📦 服务端版本: {server_ver}")

        market_us = info.get("market_us")
        if market_us and market_us != "N/A":
            lines.append(f"🇺🇸 美股市场: {market_us}")

        # Subscription
        used = info.get("subscription_used", 0)
        quota = info.get("subscription_quota", 0)
        pct = (used / quota * 100) if quota else 0
        lines.append(f"\n📊 订阅: {used}/{quota} ({pct:.0f}%)")

        # Quote cache
        cached = info.get("cached_quotes", 0)
        lines.append(f"💾 报价缓存: {cached} 个标的")

        quote_ages = info.get("quote_ages")
        if quote_ages:
            age_parts = []
            for sym, age in quote_ages.items():
                if age < 60:
                    age_parts.append(f"{sym} {age:.0f}s")
                else:
                    age_parts.append(f"{sym} {age / 60:.1f}m")
            lines.append(f"   {' | '.join(age_parts)}")

        lines.append(f"\n⏱ {datetime.now(ET).strftime('%H:%M:%S')} ET")

        await self._reply_and_log(update, "\n".join(lines), "cmd_conn", parse_mode="HTML")

    # ── /messages command ──

    async def _cmd_messages(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show archived messages from the previous trading day."""
        from src.common.trading_days import previous_trading_day, trading_day_range
        from src.store import message_archive

        market = "us"
        if context.args and context.args[0].lower() in ("hk", "us"):
            market = context.args[0].lower()

        prev_day = previous_trading_day(market)
        start_ts, end_ts = trading_day_range(prev_day, market)
        messages = message_archive.query(start_ts, end_ts, market)

        day_str = prev_day.strftime("%Y-%m-%d")
        weekday = prev_day.strftime("%a")
        market_upper = market.upper()

        if not messages:
            await self._reply_and_log(
                update,
                f"📋 上一交易日消息归档\n{day_str} ({weekday}) · {market_upper} · 共 0 条",
                "cmd_messages",
                market=market,
            )
            return

        lines = [
            f"📋 <b>上一交易日消息归档</b>",
            f"{day_str} ({weekday}) · {market_upper} · 共 {len(messages)} 条",
            "",
        ]

        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York") if market == "us" else ZoneInfo("Asia/Hong_Kong")

        for msg in messages:
            ts = msg["timestamp"]
            t_str = datetime.fromtimestamp(ts, tz=tz).strftime("%H:%M")
            source = msg["source"]
            trigger = msg["trigger"]
            content = msg["content"]
            # Truncate long content — strip HTML tags for preview
            preview = self._STRIP_HTML_RE.sub("", content)[:80]
            if len(self._STRIP_HTML_RE.sub("", content)) > 80:
                preview += "…"
            lines.append(f"{t_str} [{source}/{trigger}]")
            lines.append(preview)
            lines.append("")

        # Split into chunks if total is too long
        full_text = "\n".join(lines)
        if len(full_text) <= 4000:
            await self._reply_and_log(update, full_text, "cmd_messages", market=market)
        else:
            # Send in chunks
            chunk = ""
            for line in lines:
                if len(chunk) + len(line) + 1 > 4000:
                    await self._reply_and_log(update, chunk, "cmd_messages", market=market)
                    chunk = ""
                chunk += line + "\n"
            if chunk.strip():
                await self._reply_and_log(update, chunk, "cmd_messages", market=market)

    # ── Inline keyboard callback handlers ──

    async def _on_callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()

        data = query.data or ""
        if data == "noop":
            return

        if ":" not in data:
            return
        action, signal_id = data.split(":", 1)

        if action == "cfm":
            await self._handle_confirm_callback(query, signal_id)
        elif action == "skip":
            await self._handle_skip_callback(query, signal_id)
        elif action == "dtl":
            await self._handle_detail_callback(query, signal_id)

    async def _handle_confirm_callback(self, query: Any, signal_id: str) -> None:
        cached = self._signal_cache.get(signal_id)
        if not cached:
            await query.answer("⚠️ 缓存已过期，请用 /confirm 手动确认", show_alert=True)
            return

        price = cached.get("underlying_price", 0.0)
        if not price:
            await query.answer("⚠️ 无法获取价格，请用 /confirm 手动确认", show_alert=True)
            return

        if self._state_manager is None:
            await query.answer("⚠️ 状态管理器未初始化", show_alert=True)
            return

        if self._state_manager.confirm_entry(signal_id, price):
            try:
                await query.edit_message_reply_markup(
                    reply_markup=_build_actioned_keyboard(f"✅ 已确认 @ ${price:.2f}"),
                )
            except Exception:
                logger.debug("edit_message_reply_markup failed (confirm)")
            await self._send_message(
                f"✅ 已确认建仓 {_esc(signal_id)} @ ${price:.2f}\n开始追踪出场条件"
            )
        else:
            await query.answer("❌ 信号不存在或已处理", show_alert=True)

    async def _handle_skip_callback(self, query: Any, signal_id: str) -> None:
        if self._state_manager and self._state_manager.skip_entry(signal_id):
            try:
                await query.edit_message_reply_markup(
                    reply_markup=_build_actioned_keyboard("⏭ 已跳过"),
                )
            except Exception:
                logger.debug("edit_message_reply_markup failed (skip)")
        else:
            await query.answer("❌ 信号不存在或已处理", show_alert=True)

    async def _handle_detail_callback(self, query: Any, signal_id: str) -> None:
        cached = self._signal_cache.get(signal_id)
        if not cached:
            await query.answer("❌ 信号详情已过期", show_alert=True)
            return

        sig: Signal = cached["signal"]
        indicators_by_tf = cached.get("indicators_by_tf")

        lines = [
            f"📋 <b>信号详情 | {_esc(sig.strategy_name)}</b>",
            f"🆔 {_esc(signal_id)} | {_esc(sig.symbol)}",
            "",
        ]

        snapshot = _build_indicator_snapshot(indicators_by_tf)
        if snapshot:
            lines.append(snapshot)

        if sig.conditions_detail:
            lines.append("📊 <b>触发条件:</b>")
            for c in sig.conditions_detail:
                lines.append(f"   {_esc(c)}")

        await self._send_message("\n".join(lines))
