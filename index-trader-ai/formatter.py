"""纯数据格式化 — Telegram Markdown 和 Raw Text 两种模式。

关键规则：
- 数据不可用时显示 [不可用]，不显示零值或默认值
- 涨跌幅由系统自算，不使用 Futu change_rate
- 价格精确到 2 位小数，百分比精确到 2 位小数
- 成交量用 K/M 缩写
"""

from __future__ import annotations

from models import CollectionResult, IndexData, Mag7Data


class DataFormatter:
    """将 CollectionResult 格式化为文本。"""

    def __init__(self, config: dict) -> None:
        self._cfg = config
        thresholds = config.get("update_thresholds", {})
        self._price_delta_pct = thresholds.get("price_change_pct", 0.05)
        self._pct_delta_abs = thresholds.get("pct_change_abs", 0.10)

    # ── 公开接口 ──

    def format_telegram(
        self, data: CollectionResult, prev: CollectionResult | None = None,
    ) -> str:
        """Telegram Markdown 格式（带 emoji）。用于定时推送和 /report。"""
        sections = [
            self._header_tg(data),
            self._macro_tg(data, prev),
            self._indices_tg(data, prev),
            self._mag7_tg(data, prev),
            self._options_vp_tg(data),
            self._calendar_tg(data),
            self._status_tg(data),
        ]
        return "\n\n".join(sections)

    def format_raw(self, data: CollectionResult) -> str:
        """纯文本格式（无 emoji、无 Markdown）。用于 /raw 命令。"""
        sections = [
            self._header_raw(data),
            self._macro_raw(data),
            self._indices_raw(data),
            self._mag7_raw(data),
            self._options_vp_raw(data),
            self._calendar_raw(data),
            self._status_raw(data),
        ]
        return "\n\n".join(sections)

    def format_levels(self, data: CollectionResult, symbol: str | None = None) -> str:
        """单个指数的全部关键点位（纯文本）。"""
        indices = data.indices
        if symbol:
            indices = [i for i in indices if i.symbol.upper() == symbol.upper()]

        if not indices:
            return f"未找到指数: {symbol}" if symbol else "无指数数据"

        lines = []
        for idx in indices:
            lines.append(self._level_detail(idx))
        return "\n\n".join(lines)

    def format_mag7(self, data: CollectionResult) -> str:
        """Mag7 盘前数据（Telegram Markdown 格式）。"""
        return self._mag7_tg(data, None)

    def format_calendar(self, data: CollectionResult) -> str:
        """经济日历（纯文本）。"""
        return self._calendar_raw(data)

    def format_risk(self, risk_data: dict) -> str:
        """风控参数查表结果。"""
        regime = risk_data.get("regime", "normal")
        dev = risk_data.get("vix_deviation_pct")
        dev_str = _fmt_pct_signed(dev * 100) if dev is not None else "[不可用]"

        label = "⚠️ 高波动模式" if regime == "high_volatility" else "常规模式"
        lines = [
            f"🛡 *风控参数* ({label})",
            f"VIX 偏离 MA10: {dev_str}",
            f"单笔风险上限: {risk_data.get('max_single_risk_pct', '?')}%",
            f"日内止损上限: {risk_data.get('max_daily_loss_pct', '?')}%",
            f"熔断触发: {risk_data.get('circuit_breaker_count', '?')} 笔连续止损",
            f"冷却时间: {risk_data.get('cooldown_minutes', '?')} 分钟",
        ]
        return "\n".join(lines)

    def format_status(self, data: CollectionResult, sub_count: int = 0) -> str:
        """系统状态。"""
        lines = ["📡 *系统状态*", ""]

        # 数据源
        ok_count = sum(1 for s in data.statuses if s.ok)
        total = len(data.statuses) if data.statuses else 0
        lines.append(f"数据源: {ok_count}/{total} 正常" if total > 0 else "数据源: 未检测")

        for s in data.statuses:
            icon = "✅" if s.ok else "❌"
            detail = f" — {s.detail}" if s.detail else ""
            lines.append(f"  {icon} {s.source}{detail}")

        lines.append(f"订阅额度: {sub_count}")
        lines.append(f"上次采集: {data.time_str}")

        return "\n".join(lines)

    # ── Telegram Markdown 各段 ──

    def _header_tg(self, data: CollectionResult) -> str:
        return (
            f"📊 *INDEX TRADER 盘前数据*\n"
            f"📅 日期: {data.date_str}\n"
            f"🕐 时间: {data.time_str}"
        )

    def _macro_tg(self, data: CollectionResult, prev: CollectionResult | None) -> str:
        m = data.macro
        pm = prev.macro if prev else None

        vix_line = f"VIX: {_fmt_f2(m.vix_current)}"
        if m.vix_current is not None:
            vix_line += f" (昨收:{_fmt_f2(m.vix_prev_close)}, MA10:{_fmt_f2(m.vix_ma10)}, 偏离MA10:{_fmt_pct_signed((m.vix_deviation_pct or 0) * 100)})"
        if pm and _pct_changed(pm.vix_current, m.vix_current, self._price_delta_pct):
            vix_line += " △"

        tnx_line = f"TNX: {_fmt_f3_pct(m.tnx_current)}"
        if m.tnx_current is not None:
            tnx_line += f" (昨收:{_fmt_f3_pct(m.tnx_prev_close)}, 变动:{_fmt_bps(m.tnx_change_bps)})"
        if pm and _abs_changed(pm.tnx_change_bps, m.tnx_change_bps, self._pct_delta_abs):
            tnx_line += " △"

        uup_line = f"UUP: {_fmt_dollar(m.uup_current)}"
        if m.uup_current is not None:
            uup_line += f" (昨收:{_fmt_dollar(m.uup_prev_close)}, 涨跌:{_fmt_pct_signed(m.uup_change_pct)})"
        if pm and _abs_changed(pm.uup_change_pct, m.uup_change_pct, self._pct_delta_abs):
            uup_line += " △"

        return f"🌍 *--- 宏观 ---*\n{vix_line}\n{tnx_line}\n{uup_line}"

    def _indices_tg(self, data: CollectionResult, prev: CollectionResult | None) -> str:
        prev_map = {i.symbol: i for i in prev.indices} if prev else {}
        lines = ["📈 *--- 指数盘前 ---*"]
        for idx in data.indices:
            lines.append(self._index_block_tg(idx, prev_map.get(idx.symbol)))
        return "\n".join(lines)

    def _index_block_tg(self, idx: IndexData, prev: IndexData | None) -> str:
        delta = ""
        if prev and _abs_changed(prev.change_pct, idx.change_pct, self._pct_delta_abs):
            delta = " △"

        header = f"{idx.symbol}: {_fmt_dollar(idx.price)} (盘前:{_fmt_pct_signed(idx.change_pct)}, 量:{_fmt_vol(idx.volume)}){delta}"
        l1 = f"  PDC:{_fmt_f2(idx.pdc)} | PDH:{_fmt_f2(idx.pdh)} | PDL:{_fmt_f2(idx.pdl)}"
        l2 = f"  PMH:{_fmt_f2(idx.pmh)} | PML:{_fmt_f2(idx.pml)}"
        l3 = f"  WkH:{_fmt_f2(idx.weekly_high)} | WkL:{_fmt_f2(idx.weekly_low)}"
        l4 = f"  缺口:{_fmt_pct_signed(idx.gap_pct)}"
        return f"{header}\n{l1}\n{l2}\n{l3}\n{l4}"

    def _mag7_tg(self, data: CollectionResult, prev: CollectionResult | None) -> str:
        prev_map = {m.symbol: m for m in prev.mag7} if prev else {}
        lines = ["🌡 *--- Mag7 盘前 ---*"]
        for m in data.mag7:
            delta = ""
            pm = prev_map.get(m.symbol)
            if pm and _abs_changed(pm.change_pct, m.change_pct, self._pct_delta_abs):
                delta = " △"
            lines.append(
                f"{m.symbol}: {_fmt_pct_signed(m.change_pct)} "
                f"(量:{_fmt_vol(m.volume)}, 量比:{_fmt_ratio(m.volume_ratio)}){delta}"
            )
        return "\n".join(lines)

    def _options_vp_tg(self, data: CollectionResult) -> str:
        lines = ["📊 *--- 期权/成交量分布 ---*"]
        for idx in data.indices:
            lines.append(
                f"{idx.symbol}: "
                f"CallWall:{_fmt_f0(idx.gamma_call_wall)} | "
                f"PutWall:{_fmt_f0(idx.gamma_put_wall)} | "
                f"POC:{_fmt_f2(idx.poc)} | "
                f"VAH:{_fmt_f2(idx.vah)} | "
                f"VAL:{_fmt_f2(idx.val)}"
            )
        return "\n".join(lines)

    def _calendar_tg(self, data: CollectionResult) -> str:
        lines = ["📅 *--- 经济日历 ---*"]
        if not data.calendar:
            lines.append("(无其他高重要度数据)")
        else:
            for e in data.calendar:
                prev_str = f" | 前值:{e.previous}" if e.previous else ""
                fcst_str = f" | 预期:{e.forecast}" if e.forecast else ""
                lines.append(
                    f"{e.time} | {e.name} | 重要度:{e.importance}{prev_str}{fcst_str}"
                )
        return "\n".join(lines)

    def _status_tg(self, data: CollectionResult) -> str:
        lines = ["🔧 *--- 数据状态 ---*"]
        errors = [s for s in data.statuses if not s.ok]
        if not errors:
            lines.append("全部正常")
        else:
            for s in errors:
                lines.append(f"❌ {s.source}: {s.detail}")
        return "\n".join(lines)

    # ── Raw Text 各段 ──

    def _header_raw(self, data: CollectionResult) -> str:
        return (
            f"=== INDEX TRADER 盘前数据 ===\n"
            f"日期: {data.date_str}\n"
            f"时间: {data.time_str}"
        )

    def _macro_raw(self, data: CollectionResult) -> str:
        m = data.macro
        vix_line = f"VIX: {_fmt_f2(m.vix_current)}"
        if m.vix_current is not None:
            vix_line += f" (昨收:{_fmt_f2(m.vix_prev_close)}, MA10:{_fmt_f2(m.vix_ma10)}, 偏离MA10:{_fmt_pct_signed((m.vix_deviation_pct or 0) * 100)})"

        tnx_line = f"TNX: {_fmt_f3_pct(m.tnx_current)}"
        if m.tnx_current is not None:
            tnx_line += f" (昨收:{_fmt_f3_pct(m.tnx_prev_close)}, 变动:{_fmt_bps(m.tnx_change_bps)})"

        uup_line = f"UUP: {_fmt_dollar(m.uup_current)}"
        if m.uup_current is not None:
            uup_line += f" (昨收:{_fmt_dollar(m.uup_prev_close)}, 涨跌:{_fmt_pct_signed(m.uup_change_pct)})"

        return f"--- 宏观 ---\n{vix_line}\n{tnx_line}\n{uup_line}"

    def _indices_raw(self, data: CollectionResult) -> str:
        lines = ["--- 指数盘前 ---"]
        for idx in data.indices:
            lines.append(self._index_block_raw(idx))
        return "\n".join(lines)

    def _index_block_raw(self, idx: IndexData) -> str:
        header = f"{idx.symbol}: ${_fmt_f2(idx.price)} (盘前:{_fmt_pct_signed(idx.change_pct)}, 量:{_fmt_vol(idx.volume)})"
        l1 = f"  PDC:{_fmt_f2(idx.pdc)} | PDH:{_fmt_f2(idx.pdh)} | PDL:{_fmt_f2(idx.pdl)}"
        l2 = f"  PMH:{_fmt_f2(idx.pmh)} | PML:{_fmt_f2(idx.pml)}"
        l3 = f"  WkH:{_fmt_f2(idx.weekly_high)} | WkL:{_fmt_f2(idx.weekly_low)}"
        l4 = f"  缺口:{_fmt_pct_signed(idx.gap_pct)}"
        return f"{header}\n{l1}\n{l2}\n{l3}\n{l4}"

    def _mag7_raw(self, data: CollectionResult) -> str:
        lines = ["--- Mag7 盘前 ---"]
        for m in data.mag7:
            lines.append(
                f"{m.symbol}: {_fmt_pct_signed(m.change_pct)} "
                f"(量:{_fmt_vol(m.volume)}, 量比:{_fmt_ratio(m.volume_ratio)})"
            )
        return "\n".join(lines)

    def _options_vp_raw(self, data: CollectionResult) -> str:
        lines = ["--- 期权/成交量分布 ---"]
        for idx in data.indices:
            lines.append(
                f"{idx.symbol}: "
                f"CallWall:{_fmt_f0(idx.gamma_call_wall)} | "
                f"PutWall:{_fmt_f0(idx.gamma_put_wall)} | "
                f"POC:{_fmt_f2(idx.poc)} | "
                f"VAH:{_fmt_f2(idx.vah)} | "
                f"VAL:{_fmt_f2(idx.val)}"
            )
        return "\n".join(lines)

    def _calendar_raw(self, data: CollectionResult) -> str:
        lines = ["--- 经济日历 ---"]
        if not data.calendar:
            lines.append("(无其他高重要度数据)")
        else:
            for e in data.calendar:
                prev_str = f" | 前值:{e.previous}" if e.previous else ""
                fcst_str = f" | 预期:{e.forecast}" if e.forecast else ""
                lines.append(
                    f"{e.time} | {e.name} | 重要度:{e.importance}{prev_str}{fcst_str}"
                )
        return "\n".join(lines)

    def _status_raw(self, data: CollectionResult) -> str:
        lines = ["--- 数据状态 ---"]
        errors = [s for s in data.statuses if not s.ok]
        if not errors:
            lines.append("全部正常")
        else:
            for s in errors:
                lines.append(f"[异常] {s.source}: {s.detail}")
        return "\n".join(lines)

    # ── 点位详情 ──

    def _level_detail(self, idx: IndexData) -> str:
        lines = [f"{idx.symbol} 关键点位 (当前: {_fmt_dollar(idx.price)})"]
        _add_level(lines, "PDC", idx.pdc)
        _add_level(lines, "PDH", idx.pdh)
        _add_level(lines, "PDL", idx.pdl)
        _add_level(lines, "PMH", idx.pmh)
        _add_level(lines, "PML", idx.pml)
        _add_level(lines, "WkH", idx.weekly_high)
        _add_level(lines, "WkL", idx.weekly_low)
        _add_level(lines, "POC", idx.poc)
        _add_level(lines, "VAH", idx.vah)
        _add_level(lines, "VAL", idx.val)
        _add_level(lines, "CallWall", idx.gamma_call_wall, fmt="f0")
        _add_level(lines, "PutWall", idx.gamma_put_wall, fmt="f0")
        return "\n".join(lines)


# ── 格式化工具函数 ──


def _fmt_f2(v: float | None) -> str:
    """float → "123.45" 或 "[不可用]"。"""
    return f"{v:.2f}" if v is not None else "[不可用]"


def _fmt_f3_pct(v: float | None) -> str:
    """TNX yield → "4.300%" 或 "[不可用]"。"""
    return f"{v:.3f}%" if v is not None else "[不可用]"


def _fmt_f0(v: float | None) -> str:
    """float → "520" 或 "[不可用]"。"""
    return f"{v:.0f}" if v is not None else "[不可用]"


def _fmt_dollar(v: float | None) -> str:
    """float → "$123.45" 或 "[不可用]"。"""
    return f"${v:.2f}" if v is not None else "[不可用]"


def _fmt_pct_signed(v: float | None) -> str:
    """float → "+0.42%" / "-1.23%" 或 "[不可用]"。"""
    if v is None:
        return "[不可用]"
    return f"{v:+.2f}%"


def _fmt_bps(v: float | None) -> str:
    """float → "+5.0bps" 或 "[不可用]"。"""
    if v is None:
        return "[不可用]"
    return f"{v:+.1f}bps"


def _fmt_vol(v: int | None) -> str:
    """int → "1.23M" / "320K" 或 "[不可用]"。"""
    if v is None or v == 0:
        return "[不可用]"
    if v >= 1_000_000:
        return f"{v / 1_000_000:.2f}M"
    if v >= 1_000:
        return f"{v / 1_000:.0f}K"
    return str(v)


def _fmt_ratio(v: float | None) -> str:
    """float → "1.23x" 或 "[不可用]"。"""
    if v is None:
        return "[不可用]"
    return f"{v:.2f}x"


def _pct_changed(prev: float | None, curr: float | None, threshold: float) -> bool:
    """价格变化是否超过阈值（百分比）。"""
    if prev is None or curr is None or prev == 0:
        return False
    return abs((curr - prev) / prev * 100) > threshold


def _abs_changed(prev: float | None, curr: float | None, threshold: float) -> bool:
    """绝对值变化是否超过阈值。"""
    if prev is None or curr is None:
        return False
    return abs(curr - prev) > threshold


def _add_level(lines: list[str], name: str, value: float | None, fmt: str = "f2") -> None:
    """向点位列表添加一行。"""
    if value is not None:
        if fmt == "f0":
            lines.append(f"  {name}: {value:.0f}")
        else:
            lines.append(f"  {name}: {value:.2f}")
    else:
        lines.append(f"  {name}: [不可用]")
