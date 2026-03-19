"""Checklist validator — read-only verification of playbook quality."""

from __future__ import annotations

from src.common.action_plan import ActionPlan, PlanContext, reachable_range_pct


def validate_checklist(
    plans: list[ActionPlan],
    ctx: PlanContext,
    direction: str,
    regime_type: str,
    minutes_since_open: int,
    has_version_diff: bool,
    has_relative_strength: bool,
    is_index: bool,
    market: str,  # "us" | "hk"
) -> list[str]:
    """Validate playbook against 10 quality checks.

    Returns list of violation descriptions. Empty list = all passed.
    All checks are read-only — no plans or direction are modified.
    """
    violations: list[str] = []

    # Find primary plan (Plan A or first non-suppressed)
    primary = None
    for p in plans:
        if p.label == "A" and not p.suppressed:
            primary = p
            break
    if primary is None:
        for p in plans:
            if p.is_primary and not p.suppressed:
                primary = p
                break

    # #1: 观望超时 — UNCLEAR > 60min should force classification
    if direction == "neutral" and minutes_since_open > 60:
        violations.append("#1 观望超时: 已开盘 {}min 仍为中性方向".format(minutes_since_open))

    # #2: 入场可达 — primary plan should have reachable entry
    if primary and primary.entry is not None and not primary.demoted:
        if primary.reachability_tag == "⛔不可达":
            violations.append("#2 入场不可达: 主方案入场位超出剩余波动范围")

    # #3: 反向对冲 — at least one plan with opposite direction and entry
    if primary and primary.direction in ("bullish", "bearish"):
        opposite = "bearish" if primary.direction == "bullish" else "bullish"
        has_hedge = any(
            p.direction == opposite and p.entry is not None
            for p in plans if p.label != "C"
        )
        if not has_hedge:
            violations.append("#3 缺少反向对冲: 无反向方案提供入场位")

    # #4: 止损下限 — stop >= 1.5x ATR
    if primary and primary.stop_atr_multiple > 0 and primary.stop_atr_multiple < 1.5:
        violations.append(
            "#4 止损过窄: 主方案止损仅 {:.1f}x ATR (需 ≥ 1.5x)".format(
                primary.stop_atr_multiple
            )
        )

    # #5: TP1 可达 — TP1 within remaining volatility
    if primary and primary.entry is not None and primary.tp1 is not None:
        remaining = reachable_range_pct(ctx)
        if remaining < float("inf"):
            tp1_dist = abs(primary.tp1 - primary.entry) / primary.entry * 100 if primary.entry > 0 else 0
            if tp1_dist > remaining:
                violations.append(
                    "#5 TP1 超出剩余波动: {:.1f}% > {:.1f}%".format(tp1_dist, remaining)
                )

    # #6: R:R 门槛 — effective_rr >= 1.5 or rr_ratio >= 1.5
    if primary and primary.entry is not None and not primary.demoted:
        rr = primary.effective_rr if primary.effective_rr > 0 else primary.rr_ratio
        if 0 < rr < 1.5:
            violations.append("#6 R:R 不足: 主方案 R:R 仅 {:.1f} (需 ≥ 1.5)".format(rr))

    # #7: 版本 diff — should exist for non-first queries
    if not has_version_diff:
        pass  # First query — skip (no violation)

    # #8: RVOL 校正 — early session US warning
    if market == "us" and minutes_since_open <= 15:
        violations.append("#8 RVOL 开盘校正: 开盘 {}min 内 RVOL 数据偏高, 需校正参考".format(minutes_since_open))

    # #9: 相对强度 — should exist for non-index US stocks
    if market == "us" and not is_index and not has_relative_strength:
        violations.append("#9 缺少相对强度: 个股未提供 vs SPY 相对强度数据")

    # #10: 日型归类 — UNCLEAR should be within first 60min
    unclear_types = {"UNCLEAR", "unclear"}
    if regime_type in unclear_types and minutes_since_open > 60:
        violations.append("#10 日型未归类: 已开盘 {}min 仍为 UNCLEAR".format(minutes_since_open))

    return violations
