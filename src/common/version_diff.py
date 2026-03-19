"""Version diff engine — compares playbook snapshots across queries."""

from __future__ import annotations

import time

from src.common.action_plan import ActionPlan
from src.common.types import PlaybookSnapshot


def extract_snapshot(
    symbol: str,
    trading_day: str,
    direction: str,
    regime_type: str,
    confidence: float,
    plans: list[ActionPlan],
) -> PlaybookSnapshot:
    """Extract a frozen snapshot from current playbook state."""
    plan_entries: dict[str, float | None] = {}
    plan_directions: dict[str, str] = {}
    for p in plans:
        plan_entries[p.label] = p.entry
        plan_directions[p.label] = p.direction
    return PlaybookSnapshot(
        symbol=symbol,
        timestamp=time.time(),
        trading_day=trading_day,
        direction=direction,
        regime_type=regime_type,
        confidence=confidence,
        plan_entries=plan_entries,
        plan_directions=plan_directions,
    )


def diff_snapshots(
    prev: PlaybookSnapshot | None,
    curr: PlaybookSnapshot,
) -> str:
    """Compare two snapshots and return a human-readable diff string.

    Returns empty string for first query or new trading day.
    """
    if prev is None:
        return ""
    if prev.trading_day != curr.trading_day:
        return ""

    changes: list[str] = []

    # Direction change
    if prev.direction != curr.direction:
        changes.append(f"方向: {_direction_cn(prev.direction)} → {_direction_cn(curr.direction)}")

    # Regime change
    if prev.regime_type != curr.regime_type:
        changes.append(f"日型: {prev.regime_type} → {curr.regime_type}")

    # Entry changes (delta > 0.1%)
    all_labels = sorted(set(prev.plan_entries) | set(curr.plan_entries))
    for label in all_labels:
        prev_entry = prev.plan_entries.get(label)
        curr_entry = curr.plan_entries.get(label)
        if prev_entry is None and curr_entry is not None:
            changes.append(f"方案{label}: 新增入场 {curr_entry:,.2f}")
        elif prev_entry is not None and curr_entry is None:
            changes.append(f"方案{label}: 入场移除")
        elif prev_entry is not None and curr_entry is not None:
            if prev_entry > 0:
                delta_pct = abs(curr_entry - prev_entry) / prev_entry * 100
                if delta_pct > 0.1:
                    changes.append(f"方案{label}: 入场 {prev_entry:,.2f} → {curr_entry:,.2f}")

    # Plan direction changes
    for label in all_labels:
        prev_dir = prev.plan_directions.get(label, "")
        curr_dir = curr.plan_directions.get(label, "")
        if prev_dir and curr_dir and prev_dir != curr_dir:
            changes.append(f"方案{label}: {_direction_cn(prev_dir)} → {_direction_cn(curr_dir)}")

    if not changes:
        return "⚠️ 无实质变化"

    return " | ".join(changes)


def _direction_cn(direction: str) -> str:
    """Convert direction to Chinese label."""
    return {"bullish": "偏多", "bearish": "偏空", "neutral": "中性"}.get(direction, direction)
