"""Telegram 7 段结构化报告格式化。"""

from __future__ import annotations

from src.common.formatting import confidence_bar, format_percent
from src.index_trader import (
    DailyReport,
    LevelMap,
    RotationScenario,
    ScriptType,
    VIXRegime,
    VolatilityRegime,
)
from src.utils.logger import setup_logger

logger = setup_logger("index_formatter")

# ── 剧本 emoji ──
_SCRIPT_EMOJI = {
    ScriptType.GAP_AND_GO: "🚀",
    ScriptType.GAP_FILL: "🔄",
    ScriptType.REVERSAL: "↩️",
    ScriptType.CHOP: "🌊",
}

_GRADE_EMOJI = {"A": "🟢", "B": "🟡", "C": "🟠", "D": "🔴"}

_ROTATION_LABEL = {
    RotationScenario.SYNC: "同步",
    RotationScenario.SEESAW: "跷跷板",
    RotationScenario.DIVERGE: "分化",
}


class ReportFormatter:
    """将 DailyReport 格式化为 Telegram HTML 消息。"""

    def __init__(self, config: dict) -> None:
        self._cfg = config

    def format(self, report: DailyReport, prev: DailyReport | None = None) -> str:
        """生成 7 段 HTML 报告。prev 非空时标注 △ 变化项。"""
        sections = [
            self._section_overview(report, prev),
            self._section_macro(report, prev),
            self._section_rotation(report, prev),
            self._section_mag7(report, prev),
            self._section_levels(report),
            self._section_script(report, prev),
            self._section_score_detail(report),
        ]
        return "\n\n".join(sections)

    # ── Section 1: 总览 ──

    def _section_overview(self, r: DailyReport, prev: DailyReport | None) -> str:
        grade = r.confidence.confidence_grade
        emoji = _GRADE_EMOJI.get(grade, "⚪")
        bar = confidence_bar(r.confidence.total_score / 100)

        lines = [
            f"<b>📊 Index Trader 盘前报告</b>",
            f"📅 {r.date.isoformat()}",
            f"评级: {emoji} <b>{grade}</b> | 得分: {r.confidence.total_score:.0f}/100 {bar}",
            f"方向: {r.confidence.direction} | 共振: {r.confidence.resonance_count}",
        ]
        if r.confidence.has_conflict:
            lines.append(f"⚠️ 冲突: {r.confidence.conflict_detail}")
        if r.calendar_events:
            lines.append(f"📅 日历: {', '.join(r.calendar_events)}")

        # △ 标注变化
        if prev and prev.confidence.confidence_grade != grade:
            lines.append(f"△ 等级 {prev.confidence.confidence_grade} → {grade}")
        if prev and prev.confidence.direction != r.confidence.direction:
            lines.append(f"△ 方向 {prev.confidence.direction} → {r.confidence.direction}")

        return "\n".join(lines)

    # ── Section 2: 宏观 ──

    def _section_macro(self, r: DailyReport, prev: DailyReport | None) -> str:
        m = r.macro
        vix_emoji = "🔴" if m.vix_regime in (VIXRegime.HIGH, VIXRegime.EXTREME) else "🟢" if m.vix_regime == VIXRegime.LOW else "⚪"

        lines = [
            "<b>🌍 宏观面板</b>",
            f"VIX: {vix_emoji} {m.vix_current:.2f} (MA10: {m.vix_ma10:.2f}, 偏离: {format_percent(m.vix_deviation_pct * 100)})",
            f"TNX: {m.tnx_current:.3f}% ({m.tnx_change_bps:+.1f}bps)",
            f"UUP: {m.uup_current:.2f} ({format_percent(m.uup_change_pct)}) → DXY {m.dxy_direction}",
        ]

        if prev:
            if prev.macro.vix_regime != m.vix_regime:
                lines.append(f"△ VIX regime {prev.macro.vix_regime.value} → {m.vix_regime.value}")
            if prev.macro.dxy_direction != m.dxy_direction:
                lines.append(f"△ DXY {prev.macro.dxy_direction} → {m.dxy_direction}")

        return "\n".join(lines)

    # ── Section 3: 轮动 ──

    def _section_rotation(self, r: DailyReport, prev: DailyReport | None) -> str:
        rot = r.rotation
        label = _ROTATION_LABEL.get(rot.scenario, rot.scenario.value)

        lines = ["<b>🔄 板块轮动</b>"]
        for idx in rot.indices:
            if r.is_premarket:
                lines.append(f"  {idx.symbol}: {idx.price:.2f} 盘前:{format_percent(idx.change_pct)}")
            else:
                lines.append(
                    f"  {idx.symbol}: {idx.price:.2f} "
                    f"涨跌:{format_percent(idx.change_pct)} "
                    f"缺口:{format_percent(idx.gap_pct)}"
                )
        lines.append(f"格局: {label} | 领先: {rot.leader} | 落后: {rot.laggard} | 极差: {rot.spread_pct:.2f}%")

        if prev and prev.rotation.scenario != rot.scenario:
            lines.append(f"△ 格局 {_ROTATION_LABEL.get(prev.rotation.scenario, '?')} → {label}")

        return "\n".join(lines)

    # ── Section 4: Mag7 ──

    def _section_mag7(self, r: DailyReport, prev: DailyReport | None) -> str:
        m7 = r.mag7
        lines = [
            "<b>🌡 Mag7 温度计</b>",
            f"方向: {m7.bullish_count}涨 / {m7.bearish_count}跌 | 一致性: {m7.consistency_score:.0%} | 均涨: {format_percent(m7.avg_change_pct)}",
        ]
        for s in m7.stocks:
            anomaly_tag = " ⚡" if s.is_anomaly else ""
            lines.append(f"  {s.code}: {format_percent(s.change_pct)}{anomaly_tag}")
        if m7.is_kidnapped:
            lines.append(f"⚠️ 绑架: {m7.kidnap_detail}")

        if prev and prev.mag7.consistency_score != m7.consistency_score:
            lines.append(f"△ 一致性 {prev.mag7.consistency_score:.0%} → {m7.consistency_score:.0%}")

        return "\n".join(lines)

    # ── Section 5: 点位 ──

    def _section_levels(self, r: DailyReport) -> str:
        lines = ["<b>📐 关键点位</b>"]
        for _sym, lm in r.levels.items():
            lines.append(self._format_level_map(lm))
        return "\n".join(lines)

    @staticmethod
    def _format_level_map(lm: LevelMap) -> str:
        parts = [f"<b>{lm.symbol}</b> @ {lm.current_price:.2f}"]
        levels_line = []
        if lm.pdc > 0:
            levels_line.append(f"PDC:{lm.pdc:.2f}")
        if lm.pdh > 0:
            levels_line.append(f"PDH:{lm.pdh:.2f}")
        if lm.pdl > 0:
            levels_line.append(f"PDL:{lm.pdl:.2f}")
        if lm.pmh > 0:
            levels_line.append(f"PMH:{lm.pmh:.2f}")
        if lm.pml > 0:
            levels_line.append(f"PML:{lm.pml:.2f}")
        if lm.poc > 0:
            levels_line.append(f"POC:{lm.poc:.2f}")
        if lm.vah > 0:
            levels_line.append(f"VAH:{lm.vah:.2f}")
        if lm.val > 0:
            levels_line.append(f"VAL:{lm.val:.2f}")
        if lm.gamma_call_wall > 0:
            levels_line.append(f"CallWall:{lm.gamma_call_wall:.0f}")
        if lm.gamma_put_wall > 0:
            levels_line.append(f"PutWall:{lm.gamma_put_wall:.0f}")
        if lm.weekly_high > 0:
            levels_line.append(f"WkH:{lm.weekly_high:.2f}")
        if lm.weekly_low > 0:
            levels_line.append(f"WkL:{lm.weekly_low:.2f}")
        parts.append("  " + " | ".join(levels_line))
        return "\n".join(parts)

    # ── Section 6: 剧本 ──

    def _section_script(self, r: DailyReport, prev: DailyReport | None) -> str:
        j = r.script
        emoji = _SCRIPT_EMOJI.get(j.primary_script, "❓")

        lines = ["<b>🎬 开盘剧本</b>"]
        if j.primary_hit_count < 2:
            lines.append(f"⚠️ 条件不充分 — {j.primary_script.value} 仅命中 {j.primary_hit_count} 条件")
        else:
            lines.append(f"主判定: {emoji} <b>{j.primary_script.value}</b> (命中 {j.primary_hit_count} 条件)")
        for cond in j.primary_conditions:
            check = "✅" if cond.met else "❌"
            detail = f" — {cond.detail}" if cond.detail else ""
            lines.append(f"  {check} {cond.name}{detail}")

        if j.alternatives:
            alt_text = ", ".join(f"{s.value}({n})" for s, n in j.alternatives)
            lines.append(f"备选: {alt_text}")

        if prev and prev.script.primary_script != j.primary_script:
            lines.append(f"△ 剧本 {prev.script.primary_script.value} → {j.primary_script.value}")

        return "\n".join(lines)

    # ── Section 7: 评分明细 ──

    def _section_score_detail(self, r: DailyReport) -> str:
        c = r.confidence
        lines = [
            "<b>📊 评分明细</b>",
            f"Bullish: {c.bullish_score:.1f} | Bearish: {c.bearish_score:.1f}",
        ]
        for s in c.signals:
            dir_emoji = "📈" if s.direction == "bullish" else "📉" if s.direction == "bearish" else "➖"
            score = s.strength * s.weight * 100
            lines.append(
                f"  {dir_emoji} {s.source}: {s.direction} str={s.strength:.2f} "
                f"w={s.weight:.2f} → {score:.1f}"
            )
        if r.risk.volatility_regime == VolatilityRegime.HIGH:
            lines.append(f"⚠️ 高波动风控: 单笔≤{r.risk.max_single_risk_pct}%, 日内≤{r.risk.max_daily_loss_pct}%")
        else:
            lines.append(f"风控: 单笔≤{r.risk.max_single_risk_pct}%, 日内≤{r.risk.max_daily_loss_pct}%")

        return "\n".join(lines)
