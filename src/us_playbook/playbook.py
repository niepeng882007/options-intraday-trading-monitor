from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone

from src.us_playbook import KeyLevels, USPlaybookResult, USRegimeResult, USRegimeType
from src.utils.logger import setup_logger

logger = setup_logger("us_playbook")

ET = timezone(timedelta(hours=-5))

REGIME_EMOJI = {
    USRegimeType.GAP_AND_GO: "\U0001f680",   # 🚀
    USRegimeType.TREND_DAY: "\U0001f4c8",    # 📈
    USRegimeType.FADE_CHOP: "\U0001f4e6",    # 📦
    USRegimeType.UNCLEAR: "\u2753",           # ❓
}

REGIME_NAME_CN = {
    USRegimeType.GAP_AND_GO: "缺口追击日",
    USRegimeType.TREND_DAY: "趋势日",
    USRegimeType.FADE_CHOP: "震荡日",
    USRegimeType.UNCLEAR: "不明确日",
}

REGIME_STRATEGY = {
    USRegimeType.GAP_AND_GO: (
        "🚀 缺口追击 — 顺势操作\n"
        "• ATM/轻度 OTM 期权 (Delta 0.3-0.5)\n"
        "• VWAP 为止损线\n"
        "• 顺势加仓，不抄底/摸顶"
    ),
    USRegimeType.TREND_DAY: (
        "📈 趋势日 — 方向跟随\n"
        "• ATM 期权 (Delta 0.4-0.6)\n"
        "• PDH/PDL 为止损线\n"
        "• 目标 VAH/VAL → Gamma Wall"
    ),
    USRegimeType.FADE_CHOP: (
        "📦 震荡日 — 均值回归\n"
        "• 严禁 OTM，深度 ITM (Delta > 0.7)\n"
        "• VAH 附近做空，VAL 附近做多\n"
        "• 快进快出，不恋战"
    ),
    USRegimeType.UNCLEAR: (
        "❓ 观望为主 — 等待确认\n"
        "• 等 10:15 确认更新\n"
        "• 仅参与高确定性机会\n"
        "• 仓位降至正常的 30%"
    ),
}


def format_us_playbook_message(
    result: USPlaybookResult,
    update_type: str = "morning",
    spy_result: USPlaybookResult | None = None,
    qqq_result: USPlaybookResult | None = None,
) -> str:
    """Format US Playbook as Telegram HTML message.

    update_type:
        "morning" → ⚠️ 初步 (09:45, 15min data)
        "confirm" → ✅ 确认 (10:15, 45min data)
    """
    r = result.regime
    emoji = REGIME_EMOJI.get(r.regime, "❓")
    regime_cn = REGIME_NAME_CN.get(r.regime, "未知")
    now = result.generated_at or datetime.now(ET)

    update_label = "⚠️初步" if update_type == "morning" else "✅确认"

    lines = [
        f"━━━ 🇺🇸 {result.name} Playbook {update_label} ━━━",
        "",
    ]

    # Section 1: Market context
    lines.append("📊 <b>【大盘环境】</b>")
    if spy_result:
        se = REGIME_EMOJI.get(spy_result.regime.regime, "❓")
        sn = REGIME_NAME_CN.get(spy_result.regime.regime, "未知")
        lines.append(f"SPY: {se} {sn} (RVOL {spy_result.regime.rvol:.2f})")
    if qqq_result:
        qe = REGIME_EMOJI.get(qqq_result.regime.regime, "❓")
        qn = REGIME_NAME_CN.get(qqq_result.regime.regime, "未知")
        lines.append(f"QQQ: {qe} {qn} (RVOL {qqq_result.regime.rvol:.2f})")
    lines.append("")

    # Section 2: Regime
    conf_bar = _confidence_bar(r.confidence)
    lines.append(
        f"🎯 <b>{result.symbol}</b> — {emoji} {regime_cn} "
        f"(置信度 {conf_bar} {r.confidence:.0%})"
    )
    rvol_line = f"RVOL: {r.rvol:.2f} | Gap: {r.gap_pct:+.2f}%"
    if r.adaptive_thresholds:
        at = r.adaptive_thresholds
        rvol_line += f" | 自适应 P{at.get('sample', '?')}d={at.get('gap_and_go', '?'):.2f} (rank {at.get('pctl_rank', 0):.0f}%)"
    lines.append(rvol_line)
    lines.append("")

    # Section 3: Key levels (sorted descending by price)
    lines.append("📍 <b>【关键点位】</b>")
    kl = result.key_levels
    level_items = _collect_levels(kl, r.price)
    for name, val, annotation in sorted(level_items, key=lambda x: -x[1]):
        marker = " ← current" if annotation == "current" else ""
        oi_note = f" {annotation}" if annotation and annotation != "current" else ""
        lines.append(f"  {name:15s} {val:>10,.2f}{marker}{oi_note}")

    # VP thin data warning
    vp_td = result.volume_profile.trading_days
    if 0 < vp_td < 3:
        lines.append(f"  ⚠️ VP 仅 {vp_td} 天数据，VAH/VAL 参考性降低")

    lines.append("")

    # Section 4: Strategy advice
    lines.append("📋 <b>【交易建议】</b>")
    strategy_text = result.strategy_text or REGIME_STRATEGY.get(r.regime, "")
    lines.append(strategy_text)
    lines.append("")

    # Section 5: Filters
    f = result.filters
    lines.append("⚡ <b>【风险过滤】</b>")
    if not f.tradeable:
        lines.append("  🔴 <b>今日不宜交易</b>")
    elif f.risk_level in ("high", "blocked"):
        lines.append("  🔴 高风险日 — 降低仓位")
    elif f.risk_level == "elevated":
        lines.append("  🟡 风险偏高 — 注意控制")
    else:
        lines.append("  🟢 今日无重大风险事件")

    for w in f.warnings:
        lines.append(f"  ⚠️ {html.escape(w)}")

    lines.append("")
    lines.append(f"⏱ {now.strftime('%H:%M:%S')} ET")

    return "\n".join(lines)


def _confidence_bar(confidence: float) -> str:
    filled = int(confidence * 6)
    return "█" * filled + "░" * (6 - filled)


def _collect_levels(
    kl: KeyLevels,
    current_price: float,
) -> list[tuple[str, float, str]]:
    """Collect all non-zero levels as (name, value, annotation) tuples."""
    items: list[tuple[str, float, str]] = []

    if kl.gamma_call_wall > 0:
        items.append(("Call Wall", kl.gamma_call_wall, ""))
    if kl.pdh > 0:
        items.append(("PDH", kl.pdh, ""))
    if kl.pmh > 0:
        pm_tag = ""
        if kl.pm_source == "yahoo":
            pm_tag = " (Yahoo)"
        elif kl.pm_source == "gap_estimate":
            pm_tag = " (估)"
        items.append(("PMH", kl.pmh, pm_tag))
    if kl.vah > 0:
        items.append(("VAH", kl.vah, ""))
    if kl.vwap > 0:
        items.append(("VWAP", kl.vwap, ""))
    if kl.poc > 0:
        items.append(("POC", kl.poc, ""))
    if kl.val > 0:
        items.append(("VAL", kl.val, ""))
    if kl.pdl > 0:
        items.append(("PDL", kl.pdl, ""))
    if kl.pml > 0:
        pm_tag_l = ""
        if kl.pm_source == "yahoo":
            pm_tag_l = " (Yahoo)"
        elif kl.pm_source == "gap_estimate":
            pm_tag_l = " (估)"
        items.append(("PML", kl.pml, pm_tag_l))
    if kl.gamma_put_wall > 0:
        items.append(("Put Wall", kl.gamma_put_wall, ""))
    if kl.gamma_max_pain > 0:
        items.append(("Max Pain", kl.gamma_max_pain, ""))

    # Mark closest level to current price
    if items and current_price > 0:
        closest_idx = min(range(len(items)), key=lambda i: abs(items[i][1] - current_price))
        name, val, ann = items[closest_idx]
        if abs(val - current_price) / current_price < 0.005:
            items[closest_idx] = (name, val, "current")

    return items


def format_regime_change_alert(
    symbol: str,
    name: str,
    old_regime: USRegimeResult,
    new_regime: USRegimeResult,
    key_levels: KeyLevels | None = None,
) -> str:
    """Format regime change alert as Telegram HTML message."""
    now = datetime.now(ET)
    old_emoji = REGIME_EMOJI.get(old_regime.regime, "❓")
    old_name = REGIME_NAME_CN.get(old_regime.regime, "未知")
    new_emoji = REGIME_EMOJI.get(new_regime.regime, "❓")
    new_name = REGIME_NAME_CN.get(new_regime.regime, "未知")

    lines = [
        f"⚠️🔄 <b>REGIME 变更 — {html.escape(name)}</b>",
        "━" * 22,
        f"❌ 旧: {old_emoji} {old_name} ({old_regime.confidence:.0%})",
        f"✅ 新: {new_emoji} {new_name} ({new_regime.confidence:.0%})",
        "",
        "📊 <b>变化原因</b>",
        f"• RVOL: {old_regime.rvol:.2f} → {new_regime.rvol:.2f}",
        f"• 价格: ${old_regime.price:,.2f} → ${new_regime.price:,.2f}",
    ]

    # Compact key levels
    if key_levels:
        lines.append("")
        lines.append("📍 <b>关键位 (简)</b>")
        compact = [
            ("VAH", key_levels.vah),
            ("VWAP", key_levels.vwap),
            ("POC", key_levels.poc),
            ("VAL", key_levels.val),
        ]
        for lbl, val in compact:
            if val > 0:
                marker = " ← current" if abs(val - new_regime.price) / new_regime.price < 0.005 else ""
                lines.append(f"  {lbl:6s} {val:>10,.2f}{marker}")

    # Strategy for new regime
    strategy = REGIME_STRATEGY.get(new_regime.regime, "")
    if strategy:
        lines.append("")
        lines.append(f"📋 <b>新策略</b>: {strategy.split(chr(10))[0]}")

    lines.append("")
    lines.append(f"⏱ {now.strftime('%H:%M')} ET")

    return "\n".join(lines)
