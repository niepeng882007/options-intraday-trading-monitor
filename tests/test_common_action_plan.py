"""Tests for src/common/action_plan.py — shared ActionPlan types and utilities."""

import pytest

from src.common.action_plan import (
    ActionPlan,
    PlanContext,
    calculate_rr,
    reachable_range_pct,
    compact_option_line,
    format_action_plan,
    nearest_levels,
    find_fade_entry_zone,
    cap_tp2,
    check_entry_reachability,
    apply_wait_coherence,
    apply_min_rr_gate,
)
from src.common.types import OptionRecommendation, OptionLeg, SpreadMetrics


class TestCalculateRR:
    def test_basic_long(self):
        rr = calculate_rr(entry=100, stop_loss=95, take_profit=115)
        assert abs(rr - 3.0) < 0.01  # 15/5 = 3.0

    def test_basic_short(self):
        rr = calculate_rr(entry=100, stop_loss=105, take_profit=85)
        assert abs(rr - 3.0) < 0.01  # 15/5 = 3.0

    def test_none_returns_zero(self):
        assert calculate_rr(None, 95, 115) == 0.0
        assert calculate_rr(100, None, 115) == 0.0
        assert calculate_rr(100, 95, None) == 0.0

    def test_zero_risk(self):
        assert calculate_rr(100, 100, 110) == 0.0


class TestReachableRangePct:
    def test_us_session(self):
        ctx = PlanContext(
            minutes_to_close=195, total_session_minutes=390,
            rvol=1.0, avg_daily_range_pct=2.0, intraday_range_pct=0.5,
        )
        r = reachable_range_pct(ctx)
        assert r > 0
        assert r < 2.5  # can't exceed total range * rvol much

    def test_hk_session(self):
        ctx = PlanContext(
            minutes_to_close=165, total_session_minutes=330,
            rvol=1.0, avg_daily_range_pct=2.0, intraday_range_pct=0.5,
        )
        r = reachable_range_pct(ctx)
        assert r > 0

    def test_no_history_returns_inf(self):
        ctx = PlanContext(avg_daily_range_pct=0.0)
        assert reachable_range_pct(ctx) == float("inf")

    def test_floor_15pct(self):
        """Even near close, should return at least 15% of daily range."""
        ctx = PlanContext(
            minutes_to_close=5, total_session_minutes=390,
            rvol=1.0, avg_daily_range_pct=2.0, intraday_range_pct=1.8,
        )
        r = reachable_range_pct(ctx)
        assert r >= 2.0 * 0.15  # floor = 0.30


class TestCompactOptionLine:
    def test_wait_returns_none(self):
        rec = OptionRecommendation(action="wait", direction="neutral")
        assert compact_option_line(rec) is None

    def test_none_returns_none(self):
        assert compact_option_line(None) is None

    def test_single_call(self):
        rec = OptionRecommendation(
            action="call", direction="bullish", dte=5,
            legs=[OptionLeg(
                side="buy", option_type="call", strike=100.0,
                pct_from_price=1.0, moneyness="OTM 1.0%",
                delta=0.45, open_interest=500,
            )],
        )
        line = compact_option_line(rec)
        assert line is not None
        assert "CALL" in line
        assert "100" in line
        assert "DTE 5" in line

    def test_spread(self):
        rec = OptionRecommendation(
            action="bear_call_spread", direction="bearish", dte=7,
            legs=[
                OptionLeg(side="sell", option_type="call", strike=98.0,
                          pct_from_price=1.0, moneyness="OTM", last_price=1.2),
                OptionLeg(side="buy", option_type="call", strike=100.0,
                          pct_from_price=2.0, moneyness="OTM", last_price=0.5),
            ],
            spread_metrics=SpreadMetrics(
                net_credit=0.7, max_profit=0.7, max_loss=1.3,
                breakeven=98.7, risk_reward_ratio=0.54, win_probability=0.6,
            ),
        )
        line = compact_option_line(rec)
        assert line is not None
        assert "Bear Call Spread" in line
        assert "98" in line


class TestApplyWaitCoherence:
    def _plan(self, label, entry=100.0):
        return ActionPlan(
            label=label, name="test", emoji="📈", is_primary=(label == "A"),
            logic="test", direction="bullish", trigger="test",
            entry=entry, entry_action="做多",
            stop_loss=95.0, stop_loss_reason="test",
            tp1=105.0, tp1_label="TP1", tp2=110.0, tp2_label="TP2",
            rr_ratio=2.0,
        )

    def test_wait_demotes_plan_a(self):
        plans = [self._plan("A"), self._plan("B"), self._plan("C", entry=None)]
        ctx = PlanContext(option_action="wait")
        result = apply_wait_coherence(plans, ctx)
        assert result[0].demoted is True
        assert result[1].suppressed is True

    def test_non_wait_no_change(self):
        plans = [self._plan("A"), self._plan("B")]
        ctx = PlanContext(option_action="call")
        result = apply_wait_coherence(plans, ctx)
        assert result[0].demoted is False
        assert result[1].suppressed is False


class TestApplyMinRRGate:
    def _plan(self, label, rr=2.0):
        return ActionPlan(
            label=label, name="test", emoji="📈", is_primary=True,
            logic="test", direction="bullish", trigger="test",
            entry=100.0, entry_action="做多",
            stop_loss=95.0, stop_loss_reason="test",
            tp1=105.0, tp1_label="TP1", tp2=110.0, tp2_label="TP2",
            rr_ratio=rr,
        )

    def test_low_rr_demoted(self):
        plans = [self._plan("A", rr=0.5)]
        ctx = PlanContext(min_rr=0.8)
        result = apply_min_rr_gate(plans, ctx)
        assert result[0].demoted is True
        assert "R:R" in result[0].demote_reason

    def test_good_rr_not_demoted(self):
        plans = [self._plan("A", rr=1.5)]
        ctx = PlanContext(min_rr=0.8)
        result = apply_min_rr_gate(plans, ctx)
        assert result[0].demoted is False

    def test_plan_c_skipped(self):
        plans = [self._plan("C", rr=0.3)]
        ctx = PlanContext(min_rr=0.8)
        result = apply_min_rr_gate(plans, ctx)
        assert result[0].demoted is False


class TestNearestLevels:
    def test_above(self):
        levels = {"VAH": 510, "PDH": 520, "CallWall": 530}
        result = nearest_levels(500, "above", levels, n=2)
        assert len(result) == 2
        assert result[0] == ("VAH", 510)
        assert result[1] == ("PDH", 520)

    def test_below(self):
        levels = {"VAL": 490, "PDL": 480, "PutWall": 470}
        result = nearest_levels(500, "below", levels, n=2)
        assert len(result) == 2
        assert result[0] == ("VAL", 490)

    def test_empty_levels(self):
        assert nearest_levels(500, "above", {}) == []

    def test_filters_too_close(self):
        levels = {"VAH": 500.01}  # 0.002% from price — below 0.05% threshold
        result = nearest_levels(500, "above", levels, n=2)
        assert len(result) == 0


class TestFormatActionPlan:
    def test_primary_plan(self):
        plan = ActionPlan(
            label="A", name="趋势做多", emoji="📈", is_primary=True,
            logic="回调至 VAL 做多", direction="bullish", trigger="价格回踩 VAL",
            entry=490.0, entry_action="做多",
            stop_loss=485.0, stop_loss_reason="VAL 下方",
            tp1=500.0, tp1_label="POC", tp2=510.0, tp2_label="VAH",
            rr_ratio=2.0,
        )
        lines = format_action_plan(plan)
        assert any("首选" in l for l in lines)
        assert any("490" in l for l in lines)
        assert any("R:R" in l for l in lines)

    def test_plan_c_simplified(self):
        plan = ActionPlan(
            label="C", name="保持空仓", emoji="⚡", is_primary=False,
            logic="无信号时保留资金", direction="neutral", trigger="全天信号混杂",
            entry=None, entry_action="",
            stop_loss=None, stop_loss_reason="",
            tp1=None, tp1_label="", tp2=None, tp2_label="",
            rr_ratio=0.0,
        )
        lines = format_action_plan(plan)
        assert any("条件" in l for l in lines)
        assert not any("R:R" in l for l in lines)
