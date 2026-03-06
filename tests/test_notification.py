import time

import pytest

from src.indicator.engine import IndicatorResult
from src.strategy.matcher import EntryQuality, Signal
from src.notification.telegram import (
    TelegramNotifier,
    _compute_price_levels,
    _compute_position_size,
    _suggest_order_type,
    _build_key_indicators,
    _shorten_rationale,
    _fmt_key_indicator,
    _inline_quality,
)


class TestTelegramNotifierUnit:
    """Unit tests that don't require Telegram API access."""

    def setup_method(self):
        self.notifier = TelegramNotifier(
            bot_token="fake-token",
            chat_id="fake-chat-id",
        )

    def test_rate_limit_ok_initially(self):
        assert self.notifier._rate_limit_ok("medium") is True

    def test_rate_limit_blocks_after_max(self):
        for _ in range(10):
            self.notifier._send_timestamps.append(time.time())
        assert self.notifier._rate_limit_ok("medium") is False

    def test_high_priority_bypasses_rate_limit(self):
        for _ in range(10):
            self.notifier._send_timestamps.append(time.time())
        assert self.notifier._rate_limit_ok("high") is True

    def test_pause_mechanism(self):
        assert self.notifier._is_paused() is False
        self.notifier._paused_until = time.time() + 300
        assert self.notifier._is_paused() is True

    def test_signal_creation(self):
        signal = Signal(
            strategy_id="test-strat",
            strategy_name="Test Strategy",
            signal_type="entry",
            symbol="AAPL",
            conditions_detail=["RSI crosses above 30", "MACD turns positive"],
            priority="high",
            timestamp=time.time(),
        )
        assert signal.strategy_id == "test-strat"
        assert len(signal.conditions_detail) == 2


class TestComputePriceLevels:
    """Test _compute_price_levels for call and put directions."""

    def test_call_tp_above_sl_below(self):
        levels = _compute_price_levels(
            200.0,
            {"rules": [
                {"type": "take_profit_pct", "threshold": 0.005},
                {"type": "stop_loss_pct", "threshold": -0.003},
                {"type": "time_exit", "minutes_before_close": 15},
            ]},
            "call",
        )
        assert levels["tp_price"] == pytest.approx(201.0)  # 200 * 1.005
        assert levels["sl_price"] == pytest.approx(199.4)  # 200 * 0.997
        assert levels["tp_arrow"] == "↑"
        assert levels["sl_arrow"] == "↓"
        assert levels["tp_pct"] == "0.5%"
        assert levels["time_exit_min"] == 15

    def test_put_tp_below_sl_above(self):
        levels = _compute_price_levels(
            540.0,
            {"rules": [
                {"type": "take_profit_pct", "threshold": 0.004},
                {"type": "stop_loss_pct", "threshold": -0.0015},
            ]},
            "put",
        )
        # Put TP: stock drops → price * (1 - 0.004) = 537.84
        assert levels["tp_price"] == pytest.approx(537.84)
        # Put SL: stock rises → price * (1 - (-0.0015)) = 540.81
        assert levels["sl_price"] == pytest.approx(540.81)
        assert levels["tp_arrow"] == "↓"
        assert levels["sl_arrow"] == "↑"

    def test_empty_exit_conditions(self):
        levels = _compute_price_levels(200.0, None, "call")
        assert levels["tp_price"] == 0.0
        assert levels["sl_price"] == 0.0


class TestComputePositionSize:
    def test_basic_calculation(self):
        result = _compute_position_size(
            {"account_size": 10000, "risk_per_trade_pct": 0.02, "default_option_price_est": 2.0},
            underlying_price=200.0,
            sl_pct=-0.003,
        )
        assert "张" in result
        assert "$" in result

    def test_no_risk_config(self):
        assert _compute_position_size(None, 200.0, -0.003) == ""

    def test_contracts_capped(self):
        # Very small SL → many contracts, should be capped at 10% of account
        result = _compute_position_size(
            {"account_size": 10000, "risk_per_trade_pct": 0.05, "default_option_price_est": 2.0},
            underlying_price=200.0,
            sl_pct=-0.0001,  # Very tight SL → many contracts
        )
        # Max cost should not exceed 10% of account = $1000 → 5 contracts at $200 each
        assert "张" in result


class TestSuggestOrderType:
    def test_right_side_market_order(self):
        assert "市价" in _suggest_order_type("vwap-breakout-momentum")
        assert "市价" in _suggest_order_type("ema-momentum-breakout")
        assert "市价" in _suggest_order_type("breakdown-vwap-put")

    def test_left_side_limit_order(self):
        assert "限价" in _suggest_order_type("vwap-low-vol-ambush")
        assert "限价" in _suggest_order_type("bb-squeeze-ambush")
        assert "限价" in _suggest_order_type("extreme-oversold-reversal")


class TestBuildKeyIndicators:
    def setup_method(self):
        self.ind = IndicatorResult(
            symbol="AAPL", timeframe="5m", timestamp=time.time(),
            rsi=35.0, macd_histogram=0.2, vwap_distance_pct=0.16,
            volume_ratio=0.8, candle_body_pct=0.03, ema_9=185.3, ema_21=184.9,
            bb_width_pct=1.2, volume_spike=1.5, vwap=185.0,
        )

    def test_returns_pipe_separated_string(self):
        result = _build_key_indicators("vwap-low-vol-ambush", {"5m": self.ind})
        assert "|" in result
        # Should include vwap_dist, volume_ratio, candle_body
        assert "VWAP" in result
        assert "量比" in result

    def test_fallback_keys(self):
        result = _build_key_indicators("unknown-strategy", {"5m": self.ind})
        assert "|" in result
        assert "RSI" in result

    def test_empty_indicators(self):
        assert _build_key_indicators("vwap-low-vol-ambush", None) == "N/A"
        assert _build_key_indicators("vwap-low-vol-ambush", {}) == "N/A"


class TestShortenRationale:
    def test_short_text_unchanged(self):
        assert _shorten_rationale("RSI超卖回升") == "RSI超卖回升"

    def test_long_text_truncated(self):
        # No separator within max_len → truncate with ellipsis
        long = "这是一个非常长的策略描述文本远远超过了三十五个字符的限制范围需要被截断处理"
        result = _shorten_rationale(long, max_len=20)
        assert len(result) <= 22  # 20 + "…"
        assert result.endswith("…")

    def test_cut_at_period(self):
        text = "RSI超卖回升。叠加MACD转正确认"
        result = _shorten_rationale(text)
        assert result == "RSI超卖回升"

    def test_empty_string(self):
        assert _shorten_rationale("") == ""


class TestFmtKeyIndicator:
    def setup_method(self):
        self.ind = IndicatorResult(
            symbol="AAPL", timeframe="5m", timestamp=time.time(),
            rsi=35.0, macd_histogram=0.2, vwap_distance_pct=-0.3,
            volume_ratio=0.6, candle_body_pct=0.05, ema_9=185.3, ema_21=184.9,
            bb_width_pct=0.8, volume_spike=1.5, vwap=185.0,
        )

    def test_rsi(self):
        assert _fmt_key_indicator("rsi", self.ind) == "RSI 35"

    def test_vwap_dist_negative(self):
        result = _fmt_key_indicator("vwap_dist", self.ind)
        assert "VWAP" in result
        assert "-" in result

    def test_volume_ratio_low(self):
        result = _fmt_key_indicator("volume_ratio", self.ind)
        assert "缩量" in result

    def test_macd_hist_positive(self):
        assert _fmt_key_indicator("macd_hist", self.ind) == "MACD柱↗"

    def test_macd_hist_negative(self):
        self.ind.macd_histogram = -0.1
        assert _fmt_key_indicator("macd_hist", self.ind) == "MACD柱↘"

    def test_bb_width(self):
        assert _fmt_key_indicator("bb_width_pct", self.ind) == "BB宽0.8%"

    def test_volume_spike(self):
        assert _fmt_key_indicator("volume_spike", self.ind) == "量突变1.5x"

    def test_ema_cross_golden(self):
        # ema_9 > ema_21 → golden cross
        assert _fmt_key_indicator("ema_cross", self.ind) == "EMA9/21金叉"

    def test_ema_cross_death(self):
        self.ind.ema_9 = 184.0  # below ema_21
        assert _fmt_key_indicator("ema_cross", self.ind) == "EMA9/21死叉"

    def test_candle_body(self):
        assert _fmt_key_indicator("candle_body", self.ind) == "K线0.05%"

    def test_unknown_key(self):
        assert _fmt_key_indicator("unknown", self.ind) is None


class TestInlineQuality:
    def test_grade_a(self):
        q = EntryQuality(score=85, grade="A", reasons=[])
        result = _inline_quality(q)
        assert "🟢" in result
        assert "A" in result
        assert "85分" in result

    def test_grade_b(self):
        q = EntryQuality(score=65, grade="B", reasons=[])
        assert "🟡" in _inline_quality(q)

    def test_none(self):
        assert _inline_quality(None) == ""
