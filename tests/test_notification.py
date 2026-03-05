import time

import pytest

from src.strategy.matcher import Signal
from src.notification.telegram import TelegramNotifier


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
