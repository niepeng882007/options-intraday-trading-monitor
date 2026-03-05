from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.indicator.engine import IndicatorResult
from src.strategy.loader import StrategyConfig
from src.utils.logger import setup_logger

logger = setup_logger("rule_matcher")


QUALITY_GRADE_THRESHOLDS = {"A": 80, "B": 60, "C": 40}


@dataclass
class EntryQuality:
    score: int  # 0-100
    vwap_distance_pct: float = 0.0
    range_percentile: float = 0.0
    volume_ratio: float = 0.0
    grade: str = "C"
    reasons: list[str] = field(default_factory=list)

    @property
    def is_acceptable(self, min_score: int = 50) -> bool:
        return self.score >= min_score


@dataclass
class Signal:
    strategy_id: str
    strategy_name: str
    signal_type: str  # "entry" | "exit"
    symbol: str
    conditions_detail: list[str] = field(default_factory=list)
    exit_reason: str = ""
    priority: str = "medium"
    timestamp: float = 0.0
    strategy_meta: dict[str, Any] = field(default_factory=dict)
    entry_quality: EntryQuality | None = None


INDICATOR_FIELD_MAP = {
    "RSI": {"value": "rsi"},
    "MACD": {
        "line": "macd_line",
        "signal": "macd_signal",
        "histogram": "macd_histogram",
    },
    "EMA": {"ema_9": "ema_9", "ema_21": "ema_21"},
    "VWAP": {"value": "vwap"},
    "ATR": {"value": "atr"},
}


def _get_indicator_value(
    indicators: IndicatorResult, indicator_name: str, field_name: str
) -> float | None:
    mapping = INDICATOR_FIELD_MAP.get(indicator_name, {})
    attr_name = mapping.get(field_name, field_name)
    return getattr(indicators, attr_name, None)


class RuleMatcher:
    """Evaluates strategy entry/exit rules against indicator values.

    Maintains previous indicator values per (strategy, symbol) so that
    stateful comparators like crosses_above / turns_positive work correctly.
    """

    def __init__(self) -> None:
        self._prev_values: dict[str, dict[str, float | None]] = {}

    def _prev_key(self, strategy_id: str, symbol: str) -> str:
        return f"{strategy_id}:{symbol}"

    def _get_prev(self, key: str, field: str) -> float | None:
        return self._prev_values.get(key, {}).get(field)

    def _set_prev(self, key: str, field: str, value: float | None) -> None:
        if key not in self._prev_values:
            self._prev_values[key] = {}
        self._prev_values[key][field] = value

    # ── Rule evaluation ──

    def evaluate_entry(
        self,
        strategy: StrategyConfig,
        symbol: str,
        indicators_by_tf: dict[str, IndicatorResult | None],
    ) -> Signal | None:
        entry = strategy.entry_conditions
        if not entry or not entry.get("rules"):
            return None

        operator = entry.get("operator", "AND").upper()
        rules = entry["rules"]
        results: list[tuple[bool, str]] = []

        for rule in rules:
            passed, detail = self._evaluate_rule(strategy.strategy_id, symbol, rule, indicators_by_tf)
            results.append((passed, detail))

        if operator == "AND":
            triggered = all(r[0] for r in results)
        else:
            triggered = any(r[0] for r in results)

        if triggered:
            return Signal(
                strategy_id=strategy.strategy_id,
                strategy_name=strategy.name,
                signal_type="entry",
                symbol=symbol,
                conditions_detail=[r[1] for r in results if r[0]],
                priority=strategy.priority,
                timestamp=self._latest_ts(indicators_by_tf),
                strategy_meta={
                    "description": strategy.description,
                    "sop_checklist": strategy.sop_checklist,
                    "option_selection": strategy.option_selection_text,
                    "exit_plan": strategy.exit_plan,
                    "trading_window": strategy.trading_window_text,
                },
            )
        return None

    def evaluate_exit(
        self,
        strategy: StrategyConfig,
        symbol: str,
        current_price: float,
        entry_price: float,
        minutes_to_close: int,
    ) -> Signal | None:
        exit_conds = strategy.exit_conditions
        if not exit_conds or not exit_conds.get("rules"):
            return None

        pnl_pct = (current_price - entry_price) / entry_price if entry_price > 0 else 0.0

        for rule in exit_conds["rules"]:
            rule_type = rule.get("type", "")
            threshold = rule.get("threshold", 0)

            if rule_type == "take_profit_pct" and pnl_pct >= threshold:
                return Signal(
                    strategy_id=strategy.strategy_id,
                    strategy_name=strategy.name,
                    signal_type="exit",
                    symbol=symbol,
                    exit_reason=f"止盈 ({pnl_pct:+.1%})",
                    priority="medium",
                )

            if rule_type == "stop_loss_pct" and pnl_pct <= threshold:
                return Signal(
                    strategy_id=strategy.strategy_id,
                    strategy_name=strategy.name,
                    signal_type="exit",
                    symbol=symbol,
                    exit_reason=f"止损 ({pnl_pct:+.1%})",
                    priority="high",
                )

            if rule_type == "time_exit":
                minutes_before = rule.get("minutes_before_close", 15)
                if minutes_to_close <= minutes_before:
                    return Signal(
                        strategy_id=strategy.strategy_id,
                        strategy_name=strategy.name,
                        signal_type="exit",
                        symbol=symbol,
                        exit_reason=f"收盘前 {minutes_before} 分钟强制退出",
                        priority="medium",
                    )

        return None

    def _evaluate_rule(
        self,
        strategy_id: str,
        symbol: str,
        rule: dict[str, Any],
        indicators_by_tf: dict[str, IndicatorResult | None],
    ) -> tuple[bool, str]:
        indicator_name = rule.get("indicator", "")
        field_name = rule.get("field", "value")
        comparator = rule.get("comparator", ">")
        threshold = rule.get("threshold", 0)
        timeframe = rule.get("timeframe", "5m")

        indicators = indicators_by_tf.get(timeframe)
        if indicators is None:
            return False, f"{indicator_name}.{field_name} [{timeframe}]: no data"

        current_value = _get_indicator_value(indicators, indicator_name, field_name)
        if current_value is None:
            return False, f"{indicator_name}.{field_name} [{timeframe}]: N/A"

        prev_key = self._prev_key(strategy_id, symbol)
        value_key = f"{indicator_name}.{field_name}.{timeframe}"
        prev_value = self._get_prev(prev_key, value_key)
        self._set_prev(prev_key, value_key, current_value)

        passed = self._compare(comparator, current_value, threshold, prev_value)

        detail = (
            f"{indicator_name}({field_name}) [{timeframe}] "
            f"{comparator} {threshold} → "
            f"{'✅' if passed else '❌'} 当前={current_value:.4f}"
        )
        if prev_value is not None:
            detail += f" (前值={prev_value:.4f})"

        return passed, detail

    @staticmethod
    def _compare(
        comparator: str,
        current: float,
        threshold: float,
        previous: float | None,
    ) -> bool:
        if comparator == ">":
            return current > threshold
        if comparator == "<":
            return current < threshold
        if comparator == ">=":
            return current >= threshold
        if comparator == "<=":
            return current <= threshold
        if comparator == "==":
            return abs(current - threshold) < 1e-9

        if previous is None:
            return False

        if comparator == "crosses_above":
            return previous <= threshold < current
        if comparator == "crosses_below":
            return previous >= threshold > current
        if comparator == "turns_positive":
            return previous <= 0 < current
        if comparator == "turns_negative":
            return previous >= 0 > current

        logger.warning("Unknown comparator: %s", comparator)
        return False

    def evaluate_entry_quality(
        self,
        strategy: StrategyConfig,
        indicators_by_tf: dict[str, IndicatorResult | None],
    ) -> EntryQuality:
        """Score the quality of an entry signal based on price position filters.

        Returns an EntryQuality with score 0-100.  Strategies without
        ``entry_quality_filters`` automatically receive a perfect score.
        """
        filters = strategy.entry_quality_filters
        if not filters:
            return EntryQuality(score=100, grade="A", reasons=["无质量过滤器"])

        ind = indicators_by_tf.get("5m") or indicators_by_tf.get("1m")
        if ind is None:
            return EntryQuality(score=50, grade="C", reasons=["指标数据不足"])

        score = 100
        reasons: list[str] = []

        score = self._penalty_vwap_distance(filters, ind, score, reasons)
        score = self._penalty_range_percentile(filters, ind, score, reasons)
        score = self._penalty_day_low_distance(filters, ind, score, reasons)
        score = self._penalty_volume_ratio(filters, ind, score, reasons)
        score = self._penalty_rsi_cap(filters, ind, score, reasons)

        score = max(0, score)
        grade = self._score_to_grade(score)

        return EntryQuality(
            score=score,
            vwap_distance_pct=ind.vwap_distance_pct or 0.0,
            range_percentile=ind.range_percentile or 0.0,
            volume_ratio=ind.volume_ratio or 0.0,
            grade=grade,
            reasons=reasons,
        )

    # ── Quality penalty helpers ──

    @staticmethod
    def _penalty_vwap_distance(
        filters: dict, ind: IndicatorResult, score: int, reasons: list[str]
    ) -> int:
        max_dist = filters.get("max_distance_from_vwap_pct")
        if max_dist is not None and ind.vwap_distance_pct is not None:
            if abs(ind.vwap_distance_pct) > max_dist:
                penalty = min(30, int(abs(ind.vwap_distance_pct) / max_dist * 15))
                score -= penalty
                reasons.append(
                    f"距VWAP偏离 {ind.vwap_distance_pct:+.2f}% (限制 ±{max_dist}%)"
                )
        return score

    @staticmethod
    def _penalty_range_percentile(
        filters: dict, ind: IndicatorResult, score: int, reasons: list[str]
    ) -> int:
        max_range = filters.get("max_range_percentile")
        if max_range is not None and ind.range_percentile is not None:
            if ind.range_percentile > max_range:
                penalty = min(25, int((ind.range_percentile - max_range) / 10) * 5)
                score -= penalty
                reasons.append(
                    f"日内位置 {ind.range_percentile:.0f}% (限制 <{max_range}%)"
                )
        return score

    @staticmethod
    def _penalty_day_low_distance(
        filters: dict, ind: IndicatorResult, score: int, reasons: list[str]
    ) -> int:
        max_dist = filters.get("max_distance_from_day_low_pct")
        if max_dist is None or ind.close is None or ind.day_low is None:
            return score
        if ind.day_low <= 0:
            return score
        actual = (ind.close - ind.day_low) / ind.day_low * 100
        if actual > max_dist:
            penalty = min(25, int((actual - max_dist) / 0.5) * 5)
            score -= penalty
            reasons.append(f"距日低 {actual:.2f}% (限制 <{max_dist}%)")
        return score

    @staticmethod
    def _penalty_volume_ratio(
        filters: dict, ind: IndicatorResult, score: int, reasons: list[str]
    ) -> int:
        min_ratio = filters.get("min_volume_ratio")
        if min_ratio is not None and ind.volume_ratio is not None:
            if ind.volume_ratio < min_ratio:
                penalty = min(20, int((min_ratio - ind.volume_ratio) * 10))
                score -= penalty
                reasons.append(
                    f"量比 {ind.volume_ratio:.1f}x (需要 >{min_ratio}x)"
                )
        return score

    @staticmethod
    def _penalty_rsi_cap(
        filters: dict, ind: IndicatorResult, score: int, reasons: list[str]
    ) -> int:
        max_rsi = filters.get("max_rsi_at_entry")
        if max_rsi is not None and ind.rsi is not None:
            if ind.rsi > max_rsi:
                penalty = min(25, int((ind.rsi - max_rsi) / 5) * 10)
                score -= penalty
                reasons.append(f"RSI={ind.rsi:.1f} (限制 <{max_rsi})")
        return score

    @staticmethod
    def _score_to_grade(score: int) -> str:
        if score >= QUALITY_GRADE_THRESHOLDS["A"]:
            return "A"
        if score >= QUALITY_GRADE_THRESHOLDS["B"]:
            return "B"
        if score >= QUALITY_GRADE_THRESHOLDS["C"]:
            return "C"
        return "D"

    @staticmethod
    def _latest_ts(indicators_by_tf: dict[str, IndicatorResult | None]) -> float:
        ts = 0.0
        for ind in indicators_by_tf.values():
            if ind and ind.timestamp > ts:
                ts = ind.timestamp
        return ts
