from __future__ import annotations

import asyncio
import html
import re
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Any

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from src.indicator.engine import IndicatorResult
from src.strategy.matcher import EntryQuality, Signal
from src.utils.logger import setup_logger

logger = setup_logger("telegram_notifier")

ET = timezone(timedelta(hours=-5))

ENTRY_SIGNAL_TEMPLATE = (
    "🟢 <b>入场信号 | {strategy_name}</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "{strategy_rationale}"
    "{quality_section}"
    "📌 标的: {symbol} (${underlying_price})\n"
    "{quote_detail}"
    "\n📊 触发条件:\n"
    "{conditions_detail}\n"
    "\n{indicator_snapshot}"
    "{sop_section}"
    "{option_section}"
    "{exit_plan_section}"
    "⏱ {trigger_time} ET{trading_window_hint}\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "🆔 信号ID: <code>{signal_id}</code>\n"
    "⚠️ 数据源: Yahoo Finance (延迟~15s)\n\n"
    "确认建仓: /confirm {signal_id} &lt;股票价格&gt;\n"
    "  (输入建仓时底层股票价格，非期权价格)\n"
    "跳过: /skip {signal_id}"
)


_esc = html.escape


def _format_indicator_value(value: float | None, decimals: int = 4) -> str:
    if value is None:
        return "N/A"
    return f"{value:.{decimals}f}"


QUALITY_GRADE_EMOJI = {"A": "🟢", "B": "🟡", "C": "🟠", "D": "🔴"}


def _build_quality_section(quality: EntryQuality | None) -> str:
    if quality is None:
        return ""
    emoji = QUALITY_GRADE_EMOJI.get(quality.grade, "⚪")
    lines = [f"⭐ <b>入场质量: {emoji} {_esc(quality.grade)} ({quality.score}/100)</b>"]
    if quality.vwap_distance_pct:
        lines.append(f"   📍 距VWAP: {quality.vwap_distance_pct:+.2f}%")
    if quality.range_percentile:
        lines.append(f"   📍 日内位置: {quality.range_percentile:.0f}%")
    if quality.volume_ratio:
        vol_label = "缩量" if quality.volume_ratio < 0.8 else "放量" if quality.volume_ratio > 1.2 else "正常"
        lines.append(f"   📍 量比: {quality.volume_ratio:.1f}x ({vol_label})")
    if quality.reasons:
        for reason in quality.reasons:
            lines.append(f"   • {_esc(reason)}")
    return "\n".join(lines) + "\n\n"


def _build_strategy_rationale(meta: dict[str, Any]) -> str:
    desc = meta.get("description", "").strip()
    if not desc:
        return ""
    return f"💡 <b>策略逻辑:</b> {_esc(desc)}\n\n"


def _build_sop_section(meta: dict[str, Any]) -> str:
    checklist = meta.get("sop_checklist", [])
    if not checklist:
        return ""
    lines = ["📋 <b>操作清单:</b>"]
    for i, item in enumerate(checklist, 1):
        lines.append(f"   {i}. {_esc(item)}")
    return "\n".join(lines) + "\n\n"


def _build_option_section(meta: dict[str, Any]) -> str:
    text = meta.get("option_selection", "")
    if not text:
        return ""
    return f"🎯 <b>期权选择:</b> {_esc(text)}\n\n"


def _build_exit_plan_section(meta: dict[str, Any]) -> str:
    plan = meta.get("exit_plan", {})
    if not plan:
        return ""
    lines = ["🚪 <b>止盈止损:</b>"]
    sl = plan.get("stop_loss", "")
    tp = plan.get("take_profit", "")
    if sl:
        lines.append(f"   🔴 止损: {_esc(sl)}")
    if tp:
        lines.append(f"   🟢 止盈: {_esc(tp)}")
    return "\n".join(lines) + "\n\n"


def _build_trading_window_hint(meta: dict[str, Any]) -> str:
    tw = meta.get("trading_window", "")
    if not tw:
        return ""
    return f" | 🕐 窗口 {tw}"


def _build_indicator_snapshot(
    indicators_by_tf: dict[str, IndicatorResult | None] | None,
) -> str:
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
    return "\n".join(sections) + "\n\n"

EXIT_SIGNAL_TEMPLATE = (
    "🔴 <b>出场信号 | {strategy_name}</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "📌 标的: {symbol} (${underlying_price})\n"
    "📊 触发: {exit_reason}\n"
    "   股票: ${entry_price} → ${current_price} ({pnl_pct})\n"
    "   📊 期权参考: ATM 0DTE 约 {option_pnl_est}\n"
    "⏱ 持仓 {hold_duration}\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "⚠️ 数据源: Yahoo Finance (延迟~15s)"
)

STRATEGY_UPDATE_TEMPLATE = (
    "🔄 <b>策略已更新</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "📋 策略: {strategy_name}\n"
    "🆔 ID: <code>{strategy_id}</code>\n"
    "状态: {status}"
)

MAX_NOTIFICATIONS_PER_MINUTE = 10


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
        self._app: Application | None = None
        self._send_timestamps: deque[float] = deque()
        self._paused_until: float = 0.0

    # ── Bot setup ──

    def build_app(self) -> Application:
        self._app = (
            Application.builder()
            .token(self._bot_token)
            .build()
        )
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("quote", self._cmd_quote))
        self._app.add_handler(CommandHandler("chain", self._cmd_chain))
        self._app.add_handler(CommandHandler("strategies", self._cmd_strategies))
        self._app.add_handler(CommandHandler("enable", self._cmd_enable))
        self._app.add_handler(CommandHandler("disable", self._cmd_disable))
        self._app.add_handler(CommandHandler("pause", self._cmd_pause))
        self._app.add_handler(CommandHandler("history", self._cmd_history))
        self._app.add_handler(CommandHandler("confirm", self._cmd_confirm))
        self._app.add_handler(CommandHandler("skip", self._cmd_skip))
        self._app.add_handler(CommandHandler("test", self._cmd_test))
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

    # ── Notification sending ──

    async def send_entry_signal(
        self,
        signal: Signal,
        signal_id: str,
        underlying_price: float = 0.0,
        quote_detail: dict[str, Any] | None = None,
        indicators_by_tf: dict[str, IndicatorResult | None] | None = None,
    ) -> bool:
        if self._is_paused():
            logger.debug("Notifications paused, skipping entry signal")
            return False

        if not self._rate_limit_ok(signal.priority):
            logger.warning("Rate limit exceeded, skipping entry signal")
            return False

        conditions_text = "\n".join(
            f"   {_esc(c)}" for c in signal.conditions_detail
        )
        trigger_time = datetime.now(ET).strftime("%H:%M:%S")

        quote_text = ""
        if quote_detail:
            bid = quote_detail.get("bid", 0)
            ask = quote_detail.get("ask", 0)
            volume = quote_detail.get("volume", 0)
            quote_text = f"   Bid: ${bid:.2f} / Ask: ${ask:.2f} | Vol: {volume:,}\n"

        meta = signal.strategy_meta or {}

        message = ENTRY_SIGNAL_TEMPLATE.format(
            strategy_name=_esc(signal.strategy_name),
            strategy_rationale=_build_strategy_rationale(meta),
            quality_section=_build_quality_section(signal.entry_quality),
            symbol=_esc(signal.symbol),
            underlying_price=f"{underlying_price:.2f}",
            quote_detail=quote_text,
            conditions_detail=conditions_text,
            indicator_snapshot=_build_indicator_snapshot(indicators_by_tf),
            sop_section=_build_sop_section(meta),
            option_section=_build_option_section(meta),
            exit_plan_section=_build_exit_plan_section(meta),
            trigger_time=trigger_time,
            trading_window_hint=_build_trading_window_hint(meta),
            signal_id=_esc(signal_id),
        )
        return await self._send_message(message)

    async def send_exit_signal(
        self,
        signal: Signal,
        underlying_price: float = 0.0,
        entry_price: float = 0.0,
        current_price: float = 0.0,
        hold_duration: str = "",
    ) -> bool:
        if self._is_paused() and signal.priority != "high":
            return False

        pnl_pct = ((current_price - entry_price) / entry_price * 100) if entry_price > 0 else 0
        # Rough ATM 0DTE option PnL estimate: ~15x stock move for 0DTE ATM
        option_pnl_low = pnl_pct * 12
        option_pnl_high = pnl_pct * 20
        option_pnl_est = f"{option_pnl_low:+.0f}% ~ {option_pnl_high:+.0f}%"
        message = EXIT_SIGNAL_TEMPLATE.format(
            strategy_name=_esc(signal.strategy_name),
            symbol=_esc(signal.symbol),
            underlying_price=f"{underlying_price:.2f}",
            exit_reason=_esc(signal.exit_reason),
            entry_price=f"{entry_price:.2f}",
            current_price=f"{current_price:.2f}",
            pnl_pct=f"{pnl_pct:+.1f}%",
            option_pnl_est=option_pnl_est,
            hold_duration=_esc(hold_duration),
        )
        return await self._send_message(message)

    async def send_strategy_update(
        self, strategy_id: str, strategy_name: str, status: str
    ) -> bool:
        message = STRATEGY_UPDATE_TEMPLATE.format(
            strategy_name=_esc(strategy_name),
            strategy_id=_esc(strategy_id),
            status=_esc(status),
        )
        return await self._send_message(message)

    async def send_text(self, text: str) -> bool:
        return await self._send_message(text)

    _STRIP_HTML_RE = re.compile(r"<[^>]+>")

    async def _send_message(self, text: str) -> bool:
        if not (self._app and self._app.bot):
            return False
        try:
            await self._app.bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode="HTML",
            )
            self._send_timestamps.append(time.time())
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

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")  # type: ignore[union-attr]

    async def _cmd_quote(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await update.message.reply_text("用法: /quote AAPL")  # type: ignore[union-attr]
            return

        symbol = context.args[0].upper()
        if self._collector is None:
            await update.message.reply_text("数据采集器未初始化")  # type: ignore[union-attr]
            return

        try:
            quote = await self._collector.get_stock_quote(symbol)
            text = (
                f"📈 <b>{symbol}</b>\n"
                f"💰 价格: ${quote.price:.2f}\n"
                f"Bid: ${quote.bid:.2f} / Ask: ${quote.ask:.2f}\n"
                f"成交量: {quote.volume:,}\n"
                f"⚠️ 延迟~15s"
            )
        except Exception as exc:
            text = f"查询失败: {exc}"

        await update.message.reply_text(text, parse_mode="HTML")  # type: ignore[union-attr]

    async def _cmd_chain(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args or len(context.args) < 4:
            await update.message.reply_text(  # type: ignore[union-attr]
                "用法: /chain AAPL 230 C 0321\n(标的 行权价 C/P MMDD)"
            )
            return

        symbol = context.args[0].upper()
        strike = context.args[1]
        opt_type = context.args[2].upper()
        exp_mmdd = context.args[3]

        if self._collector is None:
            await update.message.reply_text("数据采集器未初始化")  # type: ignore[union-attr]
            return

        try:
            options = await self._collector.get_option_chain(symbol)
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
                    f"⚠️ 延迟~15s"
                )
        except Exception as exc:
            text = f"查询失败: {exc}"

        await update.message.reply_text(text, parse_mode="HTML")  # type: ignore[union-attr]

    async def _cmd_strategies(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._strategy_loader is None:
            await update.message.reply_text("策略管理器未初始化")  # type: ignore[union-attr]
            return

        strategies = self._strategy_loader.strategies
        if not strategies:
            await update.message.reply_text("暂无策略配置")  # type: ignore[union-attr]
            return

        lines = ["📋 <b>所有策略</b>", "━━━━━━━━━━━━━━━━━━━━"]
        for sid, s in strategies.items():
            icon = "🟢" if s.enabled else "🔴"
            lines.append(f"{icon} {s.name}")
            lines.append(f"   ID: <code>{sid}</code>")
            lines.append(f"   标的: {', '.join(s.underlyings)}")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")  # type: ignore[union-attr]

    async def _cmd_enable(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await update.message.reply_text("用法: /enable <strategy_id>")  # type: ignore[union-attr]
            return
        sid = context.args[0]
        if self._strategy_loader and self._strategy_loader.set_enabled(sid, True):
            await update.message.reply_text(f"✅ 策略 {sid} 已启用")  # type: ignore[union-attr]
        else:
            await update.message.reply_text(f"❌ 策略 {sid} 不存在")  # type: ignore[union-attr]

    async def _cmd_disable(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await update.message.reply_text("用法: /disable <strategy_id>")  # type: ignore[union-attr]
            return
        sid = context.args[0]
        if self._strategy_loader and self._strategy_loader.set_enabled(sid, False):
            await update.message.reply_text(f"✅ 策略 {sid} 已禁用")  # type: ignore[union-attr]
        else:
            await update.message.reply_text(f"❌ 策略 {sid} 不存在")  # type: ignore[union-attr]

    async def _cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        minutes = 30
        if context.args:
            try:
                minutes = int(context.args[0])
            except ValueError:
                pass
        self._paused_until = time.time() + minutes * 60
        await update.message.reply_text(  # type: ignore[union-attr]
            f"⏸ 通知已静默 {minutes} 分钟 (至 "
            f"{datetime.now(ET).strftime('%H:%M')} ET + {minutes}min)"
        )

    async def _cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._sqlite_store is None:
            await update.message.reply_text("存储未初始化")  # type: ignore[union-attr]
            return

        signals = self._sqlite_store.get_today_signals()
        if not signals:
            await update.message.reply_text("📭 今日暂无信号记录")  # type: ignore[union-attr]
            return

        lines = ["📜 <b>今日信号记录</b>", "━━━━━━━━━━━━━━━━━━━━"]
        for s in signals[-20:]:
            icon = "🟢" if s.get("signal_type") == "entry" else "🔴"
            t = datetime.fromtimestamp(s.get("timestamp", 0), tz=ET).strftime("%H:%M:%S")
            lines.append(f"{icon} {t} | {s.get('strategy_name', '')} | {s.get('symbol', '')}")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")  # type: ignore[union-attr]

    async def _cmd_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args or len(context.args) < 2:
            await update.message.reply_text(  # type: ignore[union-attr]
                "用法: /confirm <signal_id> <股票价格>\n"
                "  请输入建仓时底层股票价格（非期权价格）"
            )
            return

        signal_id = context.args[0]
        try:
            entry_price = float(context.args[1])
        except ValueError:
            await update.message.reply_text("价格格式错误")  # type: ignore[union-attr]
            return

        if self._state_manager is None:
            await update.message.reply_text("状态管理器未初始化")  # type: ignore[union-attr]
            return

        if self._state_manager.confirm_entry(signal_id, entry_price):
            await update.message.reply_text(  # type: ignore[union-attr]
                f"✅ 已确认建仓 {signal_id} @ ${entry_price:.2f} (股票价格)\n开始追踪出场条件"
            )
        else:
            await update.message.reply_text(  # type: ignore[union-attr]
                f"❌ 信号 {signal_id} 无法确认 (不存在或已过期)"
            )

    async def _cmd_skip(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await update.message.reply_text("用法: /skip <signal_id>")  # type: ignore[union-attr]
            return

        signal_id = context.args[0]
        if self._state_manager and self._state_manager.skip_entry(signal_id):
            await update.message.reply_text(f"⏭ 已跳过信号 {signal_id}")  # type: ignore[union-attr]
        else:
            await update.message.reply_text(f"❌ 信号 {signal_id} 不存在或已处理")  # type: ignore[union-attr]

    async def _cmd_test(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """发送模拟入场与出场提醒，用于验证 Telegram 推送链路是否正常。"""
        TEST_SIGNAL_ID = f"TEST-{int(time.time())}"
        TEST_UNDERLYING_PRICE = 185.50
        TEST_ENTRY_PRICE = 185.20  # Stock entry price (not option)
        TEST_EXIT_PRICE = 186.10   # Stock current price

        await update.message.reply_text("🧪 开始发送测试提醒...")  # type: ignore[union-attr]

        entry_signal = Signal(
            strategy_id="test-alert",
            strategy_name="测试策略",
            signal_type="entry",
            symbol="TEST",
            conditions_detail=[
                "RSI(value) [5m] crosses_above 30 → ✅ 当前=35.0000 (前值=28.0000)",
                "MACD(histogram) [5m] turns_positive → ✅ 当前=0.2000 (前值=-0.1000)",
            ],
            priority="high",
            timestamp=time.time(),
            strategy_meta={
                "description": "RSI 从超卖区回升叠加 MACD 转正，短期抛压耗尽、多头动能回归。",
                "sop_checklist": [
                    "确认RSI从超卖区上穿30",
                    "确认MACD柱状图由负转正",
                    "买入ATM Call",
                ],
                "option_selection": "ATM Call（高Delta捕捉反弹）",
                "exit_plan": {
                    "stop_loss": "浮亏20%立刻砍仓",
                    "take_profit": "浮盈50%止盈",
                },
                "trading_window": "09:45-11:00 US/Eastern",
            },
        )

        mock_indicators = {
            "5m": IndicatorResult(
                symbol="TEST", timeframe="5m", timestamp=time.time(),
                rsi=35.0, macd_line=0.1500, macd_signal=0.0800, macd_histogram=0.2000,
                ema_9=185.30, ema_21=184.90, vwap=185.20, atr=1.2500,
            ),
            "1m": IndicatorResult(
                symbol="TEST", timeframe="1m", timestamp=time.time(),
                rsi=38.5, macd_line=0.1200, macd_signal=0.0600, macd_histogram=0.1800,
                ema_9=185.35, ema_21=184.95, vwap=185.22, atr=0.8500,
            ),
        }
        mock_quote = {"bid": 185.45, "ask": 185.55, "volume": 12345678}

        entry_sent = await self.send_entry_signal(
            entry_signal,
            TEST_SIGNAL_ID,
            underlying_price=TEST_UNDERLYING_PRICE,
            quote_detail=mock_quote,
            indicators_by_tf=mock_indicators,
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

        exit_signal = Signal(
            strategy_id="test-alert",
            strategy_name="测试策略",
            signal_type="exit",
            symbol="TEST",
            exit_reason=f"止盈 (+{(TEST_EXIT_PRICE - TEST_ENTRY_PRICE) / TEST_ENTRY_PRICE:+.1%})",
            priority="high",
        )

        exit_sent = await self.send_exit_signal(
            exit_signal,
            underlying_price=190.00,
            entry_price=TEST_ENTRY_PRICE,
            current_price=TEST_EXIT_PRICE,
            hold_duration="2h 15m",
        )

        if exit_sent:
            await update.message.reply_text("✅ 出场提醒发送成功\n\n🟢 Telegram 推送链路正常")  # type: ignore[union-attr]
        else:
            await update.message.reply_text("❌ 出场提醒发送失败")  # type: ignore[union-attr]
