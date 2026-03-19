"""Tests for version diff engine."""

import time

import pytest

from src.common.action_plan import ActionPlan
from src.common.types import PlaybookSnapshot
from src.common.version_diff import diff_snapshots, extract_snapshot


def _make_plan(label="A", entry=100.0, direction="bullish"):
    return ActionPlan(
        label=label, name="test", emoji="📈", is_primary=(label == "A"),
        logic="test", direction=direction, trigger="test",
        entry=entry, entry_action="做多" if direction == "bullish" else "做空",
        stop_loss=95.0, stop_loss_reason="test",
        tp1=105.0, tp1_label="TP1", tp2=110.0, tp2_label="TP2",
        rr_ratio=2.0,
    )


def _make_snapshot(**kwargs):
    defaults = dict(
        symbol="SPY",
        timestamp=time.time(),
        trading_day="2026-03-19",
        direction="bullish",
        regime_type="TREND_STRONG",
        confidence=0.75,
        plan_entries={"A": 394.50, "B": 392.80},
        plan_directions={"A": "bullish", "B": "bearish"},
    )
    defaults.update(kwargs)
    return PlaybookSnapshot(**defaults)


class TestExtractSnapshot:
    def test_basic_extraction(self):
        plans = [_make_plan("A", 100.0, "bullish"), _make_plan("B", 95.0, "bearish")]
        snap = extract_snapshot("SPY", "2026-03-19", "bullish", "TREND_STRONG", 0.75, plans)
        assert snap.symbol == "SPY"
        assert snap.trading_day == "2026-03-19"
        assert snap.direction == "bullish"
        assert snap.plan_entries == {"A": 100.0, "B": 95.0}
        assert snap.plan_directions == {"A": "bullish", "B": "bearish"}


class TestDiffSnapshots:
    def test_first_query_returns_empty(self):
        """First query (no previous) should return empty string."""
        curr = _make_snapshot()
        assert diff_snapshots(None, curr) == ""

    def test_direction_change(self):
        """Direction change should be detected."""
        prev = _make_snapshot(direction="bullish")
        curr = _make_snapshot(direction="bearish")
        result = diff_snapshots(prev, curr)
        assert "方向" in result
        assert "偏多" in result
        assert "偏空" in result

    def test_regime_change(self):
        """Regime type change should be detected."""
        prev = _make_snapshot(regime_type="TREND_STRONG")
        curr = _make_snapshot(regime_type="RANGE")
        result = diff_snapshots(prev, curr)
        assert "日型" in result
        assert "TREND_STRONG" in result
        assert "RANGE" in result

    def test_small_entry_change_ignored(self):
        """Entry changes < 0.1% should be ignored."""
        prev = _make_snapshot(plan_entries={"A": 394.50})
        curr = _make_snapshot(plan_entries={"A": 394.52})  # ~0.005% change
        result = diff_snapshots(prev, curr)
        assert "方案A" not in result or "无实质变化" in result

    def test_large_entry_change_detected(self):
        """Entry changes > 0.1% should be detected."""
        prev = _make_snapshot(plan_entries={"A": 394.50})
        curr = _make_snapshot(plan_entries={"A": 396.00})  # ~0.38% change
        result = diff_snapshots(prev, curr)
        assert "方案A" in result
        assert "394.50" in result
        assert "396.00" in result

    def test_new_plan_detected(self):
        """New plan entry should be detected."""
        prev = _make_snapshot(plan_entries={"A": 394.50})
        curr = _make_snapshot(plan_entries={"A": 394.50, "B": 392.80})
        result = diff_snapshots(prev, curr)
        assert "方案B" in result
        assert "新增" in result

    def test_no_change_shows_warning(self):
        """When nothing changed, show warning text."""
        snap = _make_snapshot()
        result = diff_snapshots(snap, snap)
        assert "无实质变化" in result

    def test_cross_day_returns_empty(self):
        """Different trading days should return empty string."""
        prev = _make_snapshot(trading_day="2026-03-18")
        curr = _make_snapshot(trading_day="2026-03-19")
        assert diff_snapshots(prev, curr) == ""
