"""Tests for checklist validator."""

import pytest

from src.common.action_plan import ActionPlan, PlanContext
from src.common.checklist import validate_checklist


def _make_plan(label="A", entry=100.0, direction="bullish", **kwargs):
    defaults = dict(
        name="test", emoji="📈", is_primary=(label == "A"),
        logic="test", trigger="test",
        entry_action="做多" if direction == "bullish" else "做空",
        stop_loss=95.0, stop_loss_reason="test",
        tp1=105.0, tp1_label="TP1", tp2=110.0, tp2_label="TP2",
        rr_ratio=2.0,
    )
    defaults.update(kwargs)
    return ActionPlan(label=label, direction=direction, entry=entry, **defaults)


def _make_ctx(**kwargs):
    defaults = dict(
        minutes_to_close=300,
        rvol=1.0,
        avg_daily_range_pct=2.0,
        atr_5min=0.5,
    )
    defaults.update(kwargs)
    return PlanContext(**defaults)


def _run_checklist(plans=None, direction="bullish", regime_type="TREND_STRONG",
                   minutes_since_open=60, market="us", ctx=None, **kwargs):
    if plans is None:
        plans = [
            _make_plan("A", 100.0, "bullish"),
            _make_plan("B", 105.0, "bearish"),
        ]
    if ctx is None:
        ctx = _make_ctx()
    defaults = dict(
        has_version_diff=True,
        has_relative_strength=True,
        is_index=False,
    )
    defaults.update(kwargs)
    return validate_checklist(
        plans=plans, ctx=ctx, direction=direction, regime_type=regime_type,
        minutes_since_open=minutes_since_open, market=market, **defaults,
    )


class TestChecklist:
    def test_all_pass(self):
        """Healthy playbook should have no violations."""
        plans = [
            _make_plan("A", 100.0, "bullish", stop_atr_multiple=2.0, effective_rr=2.5),
            _make_plan("B", 105.0, "bearish"),
        ]
        v = _run_checklist(plans=plans)
        # Only #8 (RVOL) should fire if minutes_since_open <= 15
        assert not any("#1" in x for x in v)
        assert not any("#3" in x for x in v)

    def test_neutral_timeout(self):
        """#1: neutral direction after 60min."""
        v = _run_checklist(direction="neutral", minutes_since_open=90)
        assert any("#1" in x for x in v)

    def test_neutral_no_timeout(self):
        """#1: neutral within 60min should not trigger."""
        v = _run_checklist(direction="neutral", minutes_since_open=30)
        assert not any("#1" in x for x in v)

    def test_entry_unreachable(self):
        """#2: primary plan with unreachable entry."""
        plan_a = _make_plan("A", 100.0, "bullish")
        plan_a.reachability_tag = "⛔不可达"
        v = _run_checklist(plans=[plan_a])
        assert any("#2" in x for x in v)

    def test_missing_hedge(self):
        """#3: no opposite-direction plan with entry."""
        plans = [
            _make_plan("A", 100.0, "bullish"),
            _make_plan("B", 105.0, "bullish"),  # same direction
        ]
        v = _run_checklist(plans=plans)
        assert any("#3" in x for x in v)

    def test_stop_too_narrow(self):
        """#4: stop < 1.5x ATR."""
        plan_a = _make_plan("A", 100.0, "bullish", stop_atr_multiple=1.0)
        v = _run_checklist(plans=[plan_a, _make_plan("B", 105.0, "bearish")])
        assert any("#4" in x for x in v)

    def test_rr_insufficient(self):
        """#6: R:R < 1.5."""
        plan_a = _make_plan("A", 100.0, "bullish", rr_ratio=1.0, effective_rr=0.0)
        v = _run_checklist(plans=[plan_a, _make_plan("B", 105.0, "bearish")])
        assert any("#6" in x for x in v)

    def test_rvol_early_us(self):
        """#8: early US session RVOL warning."""
        v = _run_checklist(minutes_since_open=10, market="us")
        assert any("#8" in x for x in v)

    def test_rvol_early_hk_skip(self):
        """#8: HK should skip RVOL warning."""
        v = _run_checklist(minutes_since_open=10, market="hk")
        assert not any("#8" in x for x in v)

    def test_missing_rs(self):
        """#9: non-index US stock without relative strength."""
        v = _run_checklist(has_relative_strength=False, is_index=False, market="us")
        assert any("#9" in x for x in v)

    def test_rs_skip_for_index(self):
        """#9: index should skip RS check."""
        v = _run_checklist(has_relative_strength=False, is_index=True, market="us")
        assert not any("#9" in x for x in v)

    def test_unclear_timeout(self):
        """#10: UNCLEAR after 60min."""
        v = _run_checklist(regime_type="UNCLEAR", minutes_since_open=90)
        assert any("#10" in x for x in v)

    def test_hk_skips_rs_and_rvol(self):
        """HK should skip #8 and #9."""
        v = _run_checklist(market="hk", has_relative_strength=False, minutes_since_open=10)
        assert not any("#8" in x for x in v)
        assert not any("#9" in x for x in v)
