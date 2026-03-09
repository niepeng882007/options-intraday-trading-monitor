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
    "EMA": {
        "ema_9": "ema_9",
        "ema_21": "ema_21",
        "ema_50": "ema_50",
        "ema_200": "ema_200",
    },
    "VWAP": {"value": "vwap"},
    "ATR": {"value": "atr"},
    "BOLLINGER": {
        "upper": "bb_upper",
        "lower": "bb_lower",
        "width_pct": "bb_width_pct",
        "width_percentile": "bb_width_percentile",
        "middle": "bb_middle",
        "pct_b": "bb_pct_b",
        "width_expansion": "bb_width_expansion",
    },
    "STOCHASTIC": {
        "k": "stoch_k",
        "d": "stoch_d",
    },
    "ADX": {"value": "adx"},
    "CANDLE": {
        "body_pct": "candle_body_pct",
        "range_pct": "candle_range_pct",
        "spread_pct": "candle_range_pct",
        "upper_shadow_pct": "upper_shadow_pct",
        "lower_shadow_pct": "lower_shadow_pct",
    },
    "PRICE": {
        "close": "close",
        "open": "open",
        "high": "high",
        "low": "low",
        "day_open": "day_open",
        "day_change_pct": "day_change_pct",
        "vwap_distance_pct": "vwap_distance_pct",
        "abs_vwap_distance_pct": "abs_vwap_distance_pct",
        "prev_bar_high": "prev_bar_high",
        "prev_bar_close": "prev_bar_close",
        "prev_bar_low": "prev_bar_low",
        "volume_ratio": "volume_ratio",
        "volume_spike": "volume_spike",
        "range_percentile": "range_percentile",
        "ema_50": "ema_50",
        "ema_200": "ema_200",
    },
}


def _get_indicator_value(
    indicators: IndicatorResult, indicator_name: str, field_name: str
) -> float | None:
    mapping = INDICATOR_FIELD_MAP.get(indicator_name, {})
    attr_name = mapping.get(field_name, field_name)
    return getattr(indicators, attr_name, None)


class RuleMatcher:
    """Evaluates strategy entry/exit rules against indicator values.

    Supports:
    - Standard comparisons (indicator vs fixed threshold)
    - Reference field comparisons (indicator field vs another indicator field)
    - Nested OR/AND sub-rule groups
    - Stateful comparators (crosses_above, turns_positive, etc.)
    """

    _simulated_time: datetime | None = None  # set by BacktestEngine per bar

    def __init__(self) -> None:
        self._prev_values: dict[str, dict[str, float | None]] = {}
        self._confirmation_counts: dict[str, int] = {}

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
            passed, detail = self._evaluate_rule_or_group(
                strategy.strategy_id, symbol, rule, indicators_by_tf
            )
            results.append((passed, detail))

        if operator == "AND":
            triggered = all(r[0] for r in results)
        elif operator == "MIN_MATCH":
            min_count = entry.get("min_count", len(rules))
            passed_count = sum(1 for r in results if r[0])
            triggered = passed_count >= min_count
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
        highest_price: float | None = None,
        lowest_price: float | None = None,
        direction: str = "call",
        indicators_by_tf: dict[str, IndicatorResult | None] | None = None,
    ) -> Signal | None:
        exit_conds = strategy.exit_conditions
        if not exit_conds or not exit_conds.get("rules"):
            return None

        raw_pnl_pct = (current_price - entry_price) / entry_price if entry_price > 0 else 0.0
        pnl_pct = -raw_pnl_pct if direction == "put" else raw_pnl_pct

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

            if rule_type == "indicator_target" and indicators_by_tf is not None:
                tf = rule.get("timeframe", "15m")
                ind = indicators_by_tf.get(tf)
                if ind is not None:
                    ind_name = rule.get("indicator", "")
                    ind_field = rule.get("field", "")
                    target_value = _get_indicator_value(ind, ind_name, ind_field)
                    if target_value is not None:
                        hit = False
                        if direction == "call" and current_price >= target_value:
                            hit = True
                        elif direction == "put" and current_price <= target_value:
                            hit = True
                        if hit:
                            return Signal(
                                strategy_id=strategy.strategy_id,
                                strategy_name=strategy.name,
                                signal_type="exit",
                                symbol=symbol,
                                exit_reason=(
                                    f"指标止盈: 到达 {ind_name}.{ind_field} "
                                    f"({target_value:.2f})"
                                ),
                                priority="medium",
                            )

            if rule_type == "trailing_stop":
                activation_pct = rule.get("activation_pct", 0.50)
                trail_pct = rule.get("trail_pct", 0.20)

                if direction == "put":
                    lp = lowest_price if lowest_price and lowest_price > 0 else current_price
                    peak_pnl = (entry_price - lp) / entry_price if entry_price > 0 else 0.0
                    drawdown_from_peak = (current_price - lp) / lp if lp > 0 else 0.0
                else:
                    hp = highest_price if highest_price is not None else current_price
                    peak_pnl = (hp - entry_price) / entry_price if entry_price > 0 else 0.0
                    drawdown_from_peak = (hp - current_price) / hp if hp > 0 else 0.0

                if peak_pnl >= activation_pct and drawdown_from_peak >= trail_pct:
                    return Signal(
                        strategy_id=strategy.strategy_id,
                        strategy_name=strategy.name,
                        signal_type="exit",
                        symbol=symbol,
                        exit_reason=(
                            f"追踪止盈 (峰值收益={peak_pnl:+.1%}, "
                            f"回撤={drawdown_from_peak:.1%})"
                        ),
                        priority="medium",
                    )

        return None

    def _evaluate_rule_or_group(
        self,
        strategy_id: str,
        symbol: str,
        rule: dict[str, Any],
        indicators_by_tf: dict[str, IndicatorResult | None],
    ) -> tuple[bool, str]:
        """Evaluate a single rule or a nested sub-group with its own operator."""
        if "rules" in rule and "indicator" not in rule:
            return self._evaluate_sub_group(strategy_id, symbol, rule, indicators_by_tf)
        return self._evaluate_rule(strategy_id, symbol, rule, indicators_by_tf)

    def _evaluate_sub_group(
        self,
        strategy_id: str,
        symbol: str,
        group: dict[str, Any],
        indicators_by_tf: dict[str, IndicatorResult | None],
    ) -> tuple[bool, str]:
        """Evaluate a nested group of rules with its own AND/OR operator."""
        sub_operator = group.get("operator", "AND").upper()
        sub_rules = group.get("rules", [])
        results: list[tuple[bool, str]] = []

        for rule in sub_rules:
            passed, detail = self._evaluate_rule_or_group(
                strategy_id, symbol, rule, indicators_by_tf
            )
            results.append((passed, detail))

        if sub_operator == "AND":
            triggered = all(r[0] for r in results)
        elif sub_operator == "MIN_MATCH":
            min_count = group.get("min_count", len(sub_rules))
            passed_count = sum(1 for r in results if r[0])
            triggered = passed_count >= min_count
        else:
            triggered = any(r[0] for r in results)

        if sub_operator == "MIN_MATCH":
            min_count = group.get("min_count", len(sub_rules))
            passed_count = sum(1 for r in results if r[0])
            group_detail = f"[MIN_MATCH {passed_count}/{min_count}: {' | '.join(r[1] for r in results)}]"
        else:
            group_detail = f"[{sub_operator}: {' | '.join(r[1] for r in results)}]"
        return triggered, f"{'✅' if triggered else '❌'} {group_detail}"

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
        threshold = rule.get("threshold")
        reference_field = rule.get("reference_field")
        timeframe = rule.get("timeframe", "5m")

        indicators = indicators_by_tf.get(timeframe)
        if indicators is None:
            return False, f"{indicator_name}.{field_name} [{timeframe}]: no data"

        current_value = _get_indicator_value(indicators, indicator_name, field_name)
        if current_value is None:
            return False, f"{indicator_name}.{field_name} [{timeframe}]: N/A"

        # within_pct_of: needs both reference_field (target) and threshold (pct tolerance)
        if comparator == "within_pct_of" and reference_field is not None:
            ref_value = getattr(indicators, reference_field, None)
            if ref_value is None:
                return False, (
                    f"{indicator_name}.{field_name} [{timeframe}]: "
                    f"ref {reference_field} N/A"
                )
            if threshold is None or abs(ref_value) < 1e-9:
                return False, (
                    f"{indicator_name}.{field_name} [{timeframe}]: "
                    f"within_pct_of requires threshold and non-zero ref"
                )
            distance_pct = abs(current_value - ref_value) / abs(ref_value)
            passed = distance_pct <= threshold
            detail = (
                f"{indicator_name}({field_name}) [{timeframe}] "
                f"within_pct_of {reference_field} ±{threshold:.4%} → "
                f"{'✅' if passed else '❌'} 当前={current_value:.4f} "
                f"(参考值={ref_value:.4f}, 偏离={distance_pct:.4%})"
            )
            return passed, detail

        # reference_field: compare current value against another indicator field
        if reference_field is not None:
            ref_value = getattr(indicators, reference_field, None)
            if ref_value is None:
                return False, (
                    f"{indicator_name}.{field_name} [{timeframe}]: "
                    f"ref {reference_field} N/A"
                )
            threshold = ref_value

        if threshold is None:
            return False, f"{indicator_name}.{field_name} [{timeframe}]: no threshold"

        prev_key = self._prev_key(strategy_id, symbol)
        value_key = f"{indicator_name}.{field_name}.{timeframe}"
        prev_value = self._get_prev(prev_key, value_key)
        self._set_prev(prev_key, value_key, current_value)

        min_magnitude = rule.get("min_magnitude", 0.0)

        # For crosses_*/breaks_* with reference_field, use previous ref value
        # so we compare prev_current vs prev_ref and current vs current_ref
        if reference_field is not None and comparator in (
            "crosses_above", "crosses_below", "breaks_above", "breaks_below",
        ):
            ref_key = f"ref.{reference_field}.{timeframe}"
            prev_ref = self._get_prev(prev_key, ref_key)
            self._set_prev(prev_key, ref_key, ref_value)
            passed = self._compare(comparator, current_value, threshold, prev_value, prev_ref, min_magnitude)
        else:
            passed = self._compare(comparator, current_value, threshold, prev_value, min_magnitude=min_magnitude)

        # N-bar confirmation: require consecutive passes before triggering
        confirm_bars = rule.get("confirm_bars", 1)
        if confirm_bars > 1:
            confirm_key = f"{strategy_id}:{symbol}:{value_key}"
            if passed:
                self._confirmation_counts[confirm_key] = self._confirmation_counts.get(confirm_key, 0) + 1
                count = self._confirmation_counts[confirm_key]
                if count < confirm_bars:
                    passed = False
                    logger.debug("Confirmation %d/%d for %s", count, confirm_bars, confirm_key)
            else:
                self._confirmation_counts[confirm_key] = 0

        threshold_label = reference_field if reference_field else f"{threshold}"
        detail = (
            f"{indicator_name}({field_name}) [{timeframe}] "
            f"{comparator} {threshold_label} → "
            f"{'✅' if passed else '❌'} 当前={current_value:.4f}"
        )
        if reference_field is not None:
            detail += f" (参考值={threshold:.4f})"
        if prev_value is not None:
            detail += f" (前值={prev_value:.4f})"
        if confirm_bars > 1:
            count = self._confirmation_counts.get(f"{strategy_id}:{symbol}:{value_key}", 0)
            detail += f" (确认 {count}/{confirm_bars})"

        return passed, detail

    @staticmethod
    def _compare(
        comparator: str,
        current: float,
        threshold: float,
        previous: float | None,
        prev_threshold: float | None = None,
        min_magnitude: float = 0.0,
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

        # For cross/break with dynamic reference, use prev_threshold for the "before" check
        pt = prev_threshold if prev_threshold is not None else threshold

        if comparator == "crosses_above":
            return previous <= pt < current
        if comparator == "crosses_below":
            return previous >= pt > current
        if comparator == "breaks_above":
            return previous <= pt and current > threshold * 1.0001
        if comparator == "breaks_below":
            return previous >= pt and current < threshold * 0.9999
        if comparator == "turns_positive":
            return previous <= 0 and current > min_magnitude
        if comparator == "turns_negative":
            return previous >= 0 and current < -min_magnitude

        logger.warning("Unknown comparator: %s", comparator)
        return False

    def evaluate_entry_quality(
        self,
        strategy: StrategyConfig,
        indicators_by_tf: dict[str, IndicatorResult | None],
    ) -> EntryQuality:
        """Score the quality of an entry signal based on left-side ambush filters.

        Returns an EntryQuality with score 0-100.  Strategies without
        ``entry_quality_filters`` automatically receive a perfect score.
        """
        filters = strategy.entry_quality_filters
        if not filters:
            return EntryQuality(score=100, grade="A", reasons=["无质量过滤器"])

        ind_5m = indicators_by_tf.get("5m")
        ind_15m = indicators_by_tf.get("15m")
        ind = ind_5m or indicators_by_tf.get("1m")
        if ind is None:
            return EntryQuality(score=50, grade="C", reasons=["指标数据不足"])

        score = filters.get("base_score", 100)
        reasons: list[str] = []

        score = self._quality_vwap_proximity(filters, ind, score, reasons)
        score = self._quality_volume(filters, ind, score, reasons)
        score = self._quality_candle_body(filters, ind, score, reasons)
        score = self._quality_bb_width(filters, ind, score, reasons)
        score = self._quality_bb_pct_b(filters, ind, score, reasons)
        score = self._quality_ema200_position(filters, ind, score, reasons)
        score = self._quality_rsi_extreme(filters, ind_15m or ind, score, reasons)
        score = self._quality_vwap_deviation(filters, ind, score, reasons)
        score = self._quality_adx_environment(filters, ind_15m or ind, score, reasons)
        score = self._quality_reversal_strength(filters, ind, ind_15m, score, reasons)

        score = self._quality_time_of_day(strategy, score, reasons)

        score = max(0, min(100, score))
        grade = self._score_to_grade(score)

        return EntryQuality(
            score=score,
            vwap_distance_pct=ind.vwap_distance_pct or 0.0,
            range_percentile=ind.range_percentile or 0.0,
            volume_ratio=ind.volume_ratio or 0.0,
            grade=grade,
            reasons=reasons,
        )

    # ── Quality helpers for left-side ambush strategies ──

    @staticmethod
    def _quality_vwap_proximity(
        filters: dict, ind: IndicatorResult, score: int, reasons: list[str]
    ) -> int:
        """Strategy 1: reward closeness to VWAP."""
        max_dist = filters.get("max_distance_from_vwap_pct")
        if max_dist is not None and ind.abs_vwap_distance_pct is not None:
            if ind.abs_vwap_distance_pct > max_dist:
                penalty = min(30, int(ind.abs_vwap_distance_pct / max_dist * 15))
                score -= penalty
                reasons.append(
                    f"距VWAP偏离 {ind.vwap_distance_pct:+.2f}% (限制 ±{max_dist}%)"
                )
            else:
                score += 5
                reasons.append(f"VWAP附近 {ind.vwap_distance_pct:+.2f}% ✓")
        return score

    @staticmethod
    def _quality_volume(
        filters: dict, ind: IndicatorResult, score: int, reasons: list[str]
    ) -> int:
        """Score volume: reward low volume for left-side, high volume for right-side."""
        if filters.get("prefer_high_volume"):
            # Right-side breakout: reward high volume
            vol = ind.volume_spike if ind.volume_spike is not None else ind.volume_ratio
            if vol is None:
                return score
            min_spike = filters.get("min_volume_spike", 1.5)
            if vol >= min_spike:
                bonus = min(15, int((vol - 1.0) * 10))
                score += bonus
                reasons.append(f"放量突破 量突变={vol:.2f}x ✓")
            elif vol >= 1.0:
                score += 3
                reasons.append(f"量能正常 {vol:.2f}x")
            else:
                penalty = min(15, int((1.0 - vol) * 20))
                score -= penalty
                reasons.append(f"突破缩量 {vol:.2f}x (右侧策略不宜)")
            return score

        if not filters.get("prefer_low_volume"):
            return score
        if ind.volume_ratio is None:
            return score
        if ind.volume_ratio < 0.5:
            score += 10
            reasons.append(f"极度缩量 量比={ind.volume_ratio:.2f}x ✓")
        elif ind.volume_ratio < 0.8:
            score += 5
            reasons.append(f"缩量 量比={ind.volume_ratio:.2f}x ✓")
        elif ind.volume_ratio > 1.5:
            penalty = min(15, int((ind.volume_ratio - 1.5) * 10))
            score -= penalty
            reasons.append(f"量比偏高 {ind.volume_ratio:.2f}x (埋伏策略不宜)")
        return score

    @staticmethod
    def _quality_candle_body(
        filters: dict, ind: IndicatorResult, score: int, reasons: list[str]
    ) -> int:
        """Strategy 1: reward small candle body (sideways market)."""
        max_body = filters.get("max_candle_body_pct")
        if max_body is None or ind.candle_body_pct is None:
            return score
        if ind.candle_body_pct < max_body:
            score += 5
            reasons.append(f"K线实体小 {ind.candle_body_pct:.3f}% ✓")
        else:
            penalty = min(15, int((ind.candle_body_pct - max_body) / 0.02 * 5))
            score -= penalty
            reasons.append(f"K线实体 {ind.candle_body_pct:.3f}% (限制 <{max_body}%)")
        return score

    @staticmethod
    def _quality_bb_width(
        filters: dict, ind: IndicatorResult, score: int, reasons: list[str]
    ) -> int:
        """Strategy 2: reward tight Bollinger Band squeeze."""
        max_bbw = filters.get("max_bb_width_pct")
        if max_bbw is None or ind.bb_width_pct is None:
            return score
        if ind.bb_width_pct < max_bbw:
            score += 10
            reasons.append(f"布林带极度挤压 BBW={ind.bb_width_pct:.4f}% ✓")
        else:
            penalty = min(20, int((ind.bb_width_pct - max_bbw) / 0.05 * 5))
            score -= penalty
            reasons.append(f"布林带宽度 {ind.bb_width_pct:.4f}% (限制 <{max_bbw}%)")
        return score

    @staticmethod
    def _quality_bb_pct_b(
        filters: dict, ind: IndicatorResult, score: int, reasons: list[str]
    ) -> int:
        """Reward extreme %B values (deep piercing of BB band)."""
        if not filters.get("prefer_extreme_pct_b"):
            return score
        if ind.bb_pct_b is None:
            return score
        # %B < 0 or > 1 means outside bands — more extreme = better for reversion
        extremity = max(0.0, -ind.bb_pct_b, ind.bb_pct_b - 1.0)
        if extremity > 0.1:
            bonus = min(15, int(extremity * 50))
            score += bonus
            reasons.append(f"BB %B极端 ={ind.bb_pct_b:.2f} ✓")
        elif 0 <= ind.bb_pct_b <= 1:
            score -= 5
            reasons.append(f"BB %B在通道内 ={ind.bb_pct_b:.2f}")
        return score

    @staticmethod
    def _quality_ema200_position(
        filters: dict, ind: IndicatorResult, score: int, reasons: list[str]
    ) -> int:
        """Strategy 2: reward price above EMA 200."""
        if not filters.get("prefer_above_ema200"):
            return score
        if ind.close is None or ind.ema_200 is None:
            return score
        if ind.close > ind.ema_200:
            score += 10
            reasons.append(f"价格在EMA200之上 ✓")
        else:
            score -= 15
            reasons.append(f"价格在EMA200之下 (偏空)")
        return score

    @staticmethod
    def _quality_rsi_extreme(
        filters: dict, ind: IndicatorResult, score: int, reasons: list[str]
    ) -> int:
        """Strategy 3: reward extreme oversold RSI on 15m."""
        max_rsi = filters.get("max_rsi_15m")
        if max_rsi is None or ind.rsi is None:
            return score
        if ind.rsi < max_rsi:
            score += 10
            reasons.append(f"15m RSI极度超卖 ={ind.rsi:.1f} ✓")
        else:
            penalty = min(20, int((ind.rsi - max_rsi) / 3 * 5))
            score -= penalty
            reasons.append(f"15m RSI={ind.rsi:.1f} (限制 <{max_rsi})")
        return score

    @staticmethod
    def _quality_vwap_deviation(
        filters: dict, ind: IndicatorResult, score: int, reasons: list[str]
    ) -> int:
        """Strategy 3: reward large negative VWAP deviation (oversold bounce)."""
        min_dev = filters.get("min_vwap_deviation_pct")
        if min_dev is None or ind.vwap_distance_pct is None:
            return score
        actual_deviation = abs(ind.vwap_distance_pct)
        if actual_deviation >= min_dev:
            score += 10
            reasons.append(f"VWAP乖离率充足 {ind.vwap_distance_pct:+.2f}% ✓")
        else:
            penalty = min(15, int((min_dev - actual_deviation) / 0.3 * 5))
            score -= penalty
            reasons.append(
                f"VWAP乖离率 {ind.vwap_distance_pct:+.2f}% (需要 >{min_dev}%)"
            )
        return score

    @staticmethod
    def _quality_time_of_day(
        strategy: StrategyConfig, score: int, reasons: list[str]
    ) -> int:
        """Penalize signals during unfavorable time windows (first 30min, lunch, last 15min)."""
        if not strategy.raw.get("time_penalty_enabled", False):
            return score
        from datetime import datetime, timezone, timedelta
        et = timezone(timedelta(hours=-5))
        now = RuleMatcher._simulated_time or datetime.now(et)
        hour, minute = now.hour, now.minute
        t = hour * 60 + minute

        if t < 600:  # before 10:00 ET (first 30 min)
            score -= 15
            reasons.append("开盘前30分钟波动大 (-15)")
        elif 720 <= t < 780:  # 12:00-13:00 lunch
            score -= 10
            reasons.append("午休流动性差 (-10)")
        elif t >= 945:  # after 15:45
            score -= 20
            reasons.append("尾盘0DTE时间价值急剧衰减 (-20)")
        return score

    @staticmethod
    def _quality_adx_environment(
        filters: dict, ind: IndicatorResult, score: int, reasons: list[str]
    ) -> int:
        """Reward low ADX (range-bound, good for mean reversion); penalize trending."""
        if not filters.get("prefer_low_adx"):
            return score
        if ind.adx is None:
            return score
        max_adx = filters.get("ideal_max_adx", 25)
        if ind.adx < max_adx:
            bonus = min(15, int((max_adx - ind.adx) / max_adx * 15))
            score += bonus
            reasons.append(f"ADX低(震荡) ={ind.adx:.1f} +{bonus}")
        elif ind.adx < 35:
            score -= 5
            reasons.append(f"ADX中等 ={ind.adx:.1f} -5")
        else:
            penalty = min(20, int((ind.adx - 35) / 5 * 5))
            score -= penalty
            reasons.append(f"ADX高(趋势强) ={ind.adx:.1f} -{penalty}")
        return score

    @staticmethod
    def _quality_reversal_strength(
        filters: dict, ind: IndicatorResult, ind_15m: IndicatorResult | None,
        score: int, reasons: list[str],
    ) -> int:
        """Score reversal strength: how far price recovered from BB band piercing."""
        if not filters.get("prefer_reversal_strength"):
            return score
        target = ind_15m or ind
        if target is None or target.bb_pct_b is None:
            return score
        pct_b = target.bb_pct_b
        # For call (lower band piercing): %B closer to 0.5 = stronger reversal
        # For put (upper band piercing): %B closer to 0.5 = stronger reversal
        # Measure distance from midpoint (0.5)
        distance_from_mid = abs(pct_b - 0.5)
        if distance_from_mid < 0.3:
            bonus = min(10, int((0.5 - distance_from_mid) * 20))
            score += bonus
            reasons.append(f"回扑力度强 %B={pct_b:.2f} +{bonus}")
        elif distance_from_mid > 0.6:
            penalty = min(10, int((distance_from_mid - 0.6) * 15))
            score -= penalty
            reasons.append(f"回扑力度弱 %B={pct_b:.2f} -{penalty}")
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

    def export_prev_values(self) -> dict[str, dict[str, float | None]]:
        return {k: dict(v) for k, v in self._prev_values.items()}

    def import_prev_values(self, data: dict[str, dict[str, float | None]]) -> None:
        self._prev_values = {k: dict(v) for k, v in data.items()}
        logger.info("Imported prev_values for %d keys", len(data))

    @staticmethod
    def _latest_ts(indicators_by_tf: dict[str, IndicatorResult | None]) -> float:
        ts = 0.0
        for ind in indicators_by_tf.values():
            if ind and ind.timestamp > ts:
                ts = ind.timestamp
        return ts
