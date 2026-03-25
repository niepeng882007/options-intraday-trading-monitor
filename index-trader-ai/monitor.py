"""盘中事件监控 — 09:30 后激活，事件驱动推送。

功能：
- 点位触及提醒（距离 ≤ 0.1%），同一点位不重复推送
- VWAP 穿越提醒（开盘后实时计算 VWAP）
- 经济数据发布前 5 分钟倒计时
- 成交量异常放大提醒（5分钟量 > 均值 3x）
"""

from __future__ import annotations

import logging
from datetime import datetime, time as dt_time
from typing import TYPE_CHECKING, Callable, Coroutine, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from collector import DataCollector
    from models import CalendarEvent, IndexData

logger = logging.getLogger("monitor")

_ET = ZoneInfo("America/New_York")


class IntraDayMonitor:
    """盘中事件监控。09:30 ET 后激活。"""

    def __init__(self, config: dict, collector: DataCollector) -> None:
        self._cfg = config
        self._collector = collector
        self._send_fn: Callable[..., Coroutine[Any, Any, None]] | None = None

        # 配置参数
        level_cfg = config.get("levels", {})
        mon_cfg = config.get("monitor", {})
        self._proximity_pct = level_cfg.get("proximity_pct", 0.001)
        self._vol_anomaly_ratio = mon_cfg.get("volume_anomaly_ratio", 3.0)
        self._alert_minutes_before = mon_cfg.get("data_alert_minutes_before", 5)

        # 去重追踪
        self._triggered_levels: set[tuple[str, str]] = set()  # (symbol, level_name)
        self._vwap_side: dict[str, str] = {}  # symbol → "above" / "below"
        self._alerted_events: set[str] = set()  # event name

        # VWAP 计算数据
        self._intraday_bars: dict[str, list[dict]] = {}  # symbol → list of bars
        self._avg_5m_volume: dict[str, float] = {}  # symbol → 均量

        # 日历事件
        self._calendar: list[CalendarEvent] = []

        self._active = False

    def set_calendar(self, events: list[CalendarEvent]) -> None:
        """设置今日经济日历事件。"""
        self._calendar = events

    async def start(self, send_fn: Callable[..., Coroutine[Any, Any, None]]) -> None:
        """注册推送回调，准备就绪。"""
        self._send_fn = send_fn
        self._active = True
        logger.info("IntraDayMonitor started")

    def stop(self) -> None:
        """停止监控。"""
        self._active = False

    def reset_daily(self) -> None:
        """每日重置所有追踪状态。"""
        self._triggered_levels.clear()
        self._vwap_side.clear()
        self._alerted_events.clear()
        self._intraday_bars.clear()
        self._avg_5m_volume.clear()

    # ── 报价更新回调 ──

    async def on_quote_update(self, symbol: str, price: float, levels: dict[str, float]) -> None:
        """报价更新时检查：点位逼近 + VWAP 穿越。

        Parameters
        ----------
        symbol : str
            股票代码（如 "QQQ"）。
        price : float
            当前价格。
        levels : dict
            关键点位 {name: value}，如 {"PDH": 520.50, "VAH": 519.20, ...}。
        """
        if not self._active or price <= 0:
            return

        alerts: list[str] = []

        # 点位逼近检查
        for name, level in levels.items():
            if level <= 0:
                continue
            distance = abs(price - level) / level
            if distance <= self._proximity_pct:
                key = (symbol, name)
                if key not in self._triggered_levels:
                    self._triggered_levels.add(key)
                    direction = "↑" if price >= level else "↓"
                    alerts.append(
                        f"📍 {symbol} 接近 {name}({level:.2f}) "
                        f"| 当前 {price:.2f} {direction} 距离 {distance:.3%}"
                    )

        # VWAP 穿越检查
        vwap = self._compute_vwap(symbol)
        if vwap > 0:
            side = "above" if price > vwap else "below"
            prev_side = self._vwap_side.get(symbol)
            if prev_side is not None and prev_side != side:
                emoji = "🟢" if side == "above" else "🔴"
                alerts.append(
                    f"{emoji} {symbol} VWAP 穿越 "
                    f"| VWAP={vwap:.2f} 价格={price:.2f} ({side})"
                )
            self._vwap_side[symbol] = side

        # 推送
        for alert in alerts:
            await self._send(alert)

    # ── K 线回调 ──

    async def on_kline_5m(self, symbol: str, bar: dict) -> None:
        """5 分钟 K 线回调：累积数据 + 检查成交量异常。

        Parameters
        ----------
        symbol : str
            股票代码。
        bar : dict
            {"open": ..., "high": ..., "low": ..., "close": ..., "volume": ...}
        """
        if not self._active:
            return

        # 累积 bar（用于 VWAP 计算）
        if symbol not in self._intraday_bars:
            self._intraday_bars[symbol] = []
        self._intraday_bars[symbol].append(bar)

        # 成交量异常检查
        volume = bar.get("volume", 0)
        avg = self._avg_5m_volume.get(symbol, 0)

        if avg > 0 and volume > avg * self._vol_anomaly_ratio:
            ratio = volume / avg
            await self._send(
                f"⚡ {symbol} 成交量异常 "
                f"| 5min量={volume:,.0f} ({ratio:.1f}x 均值)"
            )

    # ── 经济数据倒计时 ──

    async def check_calendar_countdown(self) -> None:
        """检查经济数据发布倒计时（每分钟调用一次）。"""
        if not self._active or not self._calendar:
            return

        et_now = datetime.now(_ET)

        for event in self._calendar:
            if event.name in self._alerted_events:
                continue
            if event.time == "全天":
                continue

            try:
                parts = event.time.split(":")
                event_time = et_now.replace(
                    hour=int(parts[0]), minute=int(parts[1]),
                    second=0, microsecond=0,
                )
            except (ValueError, IndexError):
                continue

            diff_minutes = (event_time - et_now).total_seconds() / 60

            if 0 < diff_minutes <= self._alert_minutes_before:
                self._alerted_events.add(event.name)
                await self._send(
                    f"⏰ 经济数据预警 | {event.name} 将在 {int(diff_minutes)} 分钟后发布 "
                    f"({event.time} ET) | 重要度: {event.importance}"
                )

    # ── VWAP 计算 ──

    def _compute_vwap(self, symbol: str) -> float:
        """从盘中累积 bar 计算 VWAP。"""
        bars = self._intraday_bars.get(symbol, [])
        if not bars:
            return 0.0

        total_pv = 0.0
        total_v = 0.0
        for bar in bars:
            typical = (bar.get("high", 0) + bar.get("low", 0) + bar.get("close", 0)) / 3
            vol = bar.get("volume", 0)
            total_pv += typical * vol
            total_v += vol

        return total_pv / total_v if total_v > 0 else 0.0

    def set_avg_5m_volume(self, symbol: str, avg: float) -> None:
        """设置 5 分钟均量基准（从历史数据计算）。"""
        self._avg_5m_volume[symbol] = avg

    # ── 推送 ──

    async def _send(self, text: str) -> None:
        """通过回调推送消息。"""
        if self._send_fn:
            try:
                await self._send_fn(text)
            except Exception:
                logger.warning("Monitor alert send failed: %s", text[:100])
        else:
            logger.info("Monitor alert (no send_fn): %s", text)
