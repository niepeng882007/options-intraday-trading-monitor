import json
import time

import pytest

from src.common.daily_report import (
    DailySummaryData,
    TradeRecord,
    collect_pipeline_data,
    format_daily_summary,
    _parse_detail,
    _exit_reason_cn,
)


class FakeSQLiteStore:
    """Minimal stub that returns pre-configured signals."""

    def __init__(self, signals: list[dict]):
        self._signals = signals

    def get_today_signals(self) -> list[dict]:
        return self._signals


def _make_entry(strategy_id, strategy_name, symbol, price, grade="B", score=70, ts=None):
    return {
        "signal_id": f"SIG-{strategy_id}-{symbol}",
        "strategy_id": strategy_id,
        "strategy_name": strategy_name,
        "signal_type": "entry",
        "symbol": symbol,
        "detail": json.dumps({
            "conditions": [],
            "quality_score": score,
            "quality_grade": grade,
            "underlying_price": price,
        }),
        "timestamp": ts or time.time(),
    }


def _make_exit(strategy_id, strategy_name, symbol, entry_price, exit_price, direction="call", reason="take_profit", ts=None):
    return {
        "signal_id": f"EXIT-{int(ts or time.time())}",
        "strategy_id": strategy_id,
        "strategy_name": strategy_name,
        "signal_type": "exit",
        "symbol": symbol,
        "detail": json.dumps({
            "reason": reason,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "direction": direction,
        }),
        "timestamp": ts or time.time(),
    }


class TestParseDetail:

    def test_dict_passthrough(self):
        assert _parse_detail({"key": "val"}) == {"key": "val"}

    def test_json_string(self):
        assert _parse_detail('{"a": 1}') == {"a": 1}

    def test_empty_string(self):
        assert _parse_detail("") == {}

    def test_none(self):
        assert _parse_detail(None) == {}

    def test_invalid_json(self):
        assert _parse_detail("not json") == {}


class TestExitReasonCn:

    def test_known_reasons(self):
        assert _exit_reason_cn("take_profit") == "止盈"
        assert _exit_reason_cn("stop_loss") == "止损"
        assert _exit_reason_cn("trailing_stop") == "跟踪止损"

    def test_unknown_passes_through(self):
        assert _exit_reason_cn("custom_exit") == "custom_exit"


class TestCollectPipelineData:

    def test_empty_signals(self):
        store = FakeSQLiteStore([])
        data = collect_pipeline_data(store, 0.0)
        assert data.total_entries == 0
        assert data.completed_trades == 0
        assert data.trades == []

    def test_entries_only_no_trades(self):
        signals = [
            _make_entry("strat1", "VWAP 埋伏", "AAPL", 185.0, grade="A"),
            _make_entry("strat2", "BB Squeeze", "TSLA", 245.0, grade="B"),
        ]
        store = FakeSQLiteStore(signals)
        data = collect_pipeline_data(store, 0.5)
        assert data.total_entries == 2
        assert data.completed_trades == 0
        assert data.daily_pnl == 0.5
        assert data.strategy_dist == {"VWAP 埋伏": 1, "BB Squeeze": 1}
        assert data.quality_dist == {"A": 1, "B": 1}

    def test_entry_exit_pair_call(self):
        ts = time.time()
        signals = [
            _make_entry("s1", "VWAP 埋伏", "AAPL", 185.0, grade="A", ts=ts),
            _make_exit("s1", "VWAP 埋伏", "AAPL", 185.0, 186.0, direction="call", reason="take_profit", ts=ts + 60),
        ]
        store = FakeSQLiteStore(signals)
        data = collect_pipeline_data(store, 0.54)

        assert data.total_entries == 1
        assert data.completed_trades == 1
        assert len(data.trades) == 1

        trade = data.trades[0]
        assert trade.symbol == "AAPL"
        assert trade.entry_price == 185.0
        assert trade.exit_price == 186.0
        assert trade.pnl_pct > 0  # call direction: positive when price went up
        assert trade.quality_grade == "A"
        assert trade.exit_reason == "take_profit"

    def test_entry_exit_pair_put(self):
        ts = time.time()
        signals = [
            _make_entry("s2", "Breakdown", "TSLA", 245.0, grade="B", ts=ts),
            _make_exit("s2", "Breakdown", "TSLA", 245.0, 243.0, direction="put", reason="take_profit", ts=ts + 60),
        ]
        store = FakeSQLiteStore(signals)
        data = collect_pipeline_data(store, 0.0)

        trade = data.trades[0]
        # PUT direction: price went down -> PnL should be positive
        assert trade.pnl_pct > 0
        assert trade.direction == "put"

    def test_trades_sorted_by_pnl(self):
        ts = time.time()
        signals = [
            _make_entry("s1", "Strat A", "AAPL", 100.0, ts=ts),
            _make_exit("s1", "Strat A", "AAPL", 100.0, 99.5, direction="call", reason="stop_loss", ts=ts + 60),
            _make_entry("s2", "Strat B", "TSLA", 200.0, ts=ts),
            _make_exit("s2", "Strat B", "TSLA", 200.0, 201.0, direction="call", reason="take_profit", ts=ts + 120),
        ]
        store = FakeSQLiteStore(signals)
        data = collect_pipeline_data(store, -0.1)

        assert len(data.trades) == 2
        # Best trade first (positive PnL)
        assert data.trades[0].pnl_pct > 0
        assert data.trades[1].pnl_pct < 0

    def test_exit_without_matching_entry(self):
        """Exit with price data but no matching entry — should still create a trade."""
        signals = [
            _make_exit("s1", "Strat A", "AAPL", 100.0, 101.0, direction="call"),
        ]
        store = FakeSQLiteStore(signals)
        data = collect_pipeline_data(store, 0.0)

        assert data.total_entries == 0
        assert data.completed_trades == 1
        assert data.trades[0].quality_grade == "?"


class TestFormatDailySummary:

    def test_no_activity(self):
        data = DailySummaryData(date_str="2026-03-10 (Tue)", total_entries=0, completed_trades=0)
        text = format_daily_summary(data)
        assert "今日无交易活动" in text
        assert "Daily Summary" in text

    def test_with_trades(self):
        data = DailySummaryData(
            date_str="2026-03-10 (Tue)",
            total_entries=3,
            completed_trades=2,
            trades=[
                TradeRecord("VWAP 埋伏", "AAPL", 185.0, 186.0, 0.54, "call", "take_profit", "10:05", "A"),
                TradeRecord("BB Squeeze", "TSLA", 245.0, 244.0, -0.41, "call", "stop_loss", "11:30", "B"),
            ],
            daily_pnl=0.13,
            strategy_dist={"VWAP 埋伏": 2, "BB Squeeze": 1},
            quality_dist={"A": 1, "B": 2},
        )
        text = format_daily_summary(data)
        assert "入场信号: 3" in text
        assert "完成交易: 2" in text
        assert "+0.1%" in text
        assert "VWAP 埋伏: 2" in text
        assert "最优交易" in text
        assert "最差交易" in text
        assert "AAPL" in text
        assert "TSLA" in text
        assert "止盈" in text
        assert "止损" in text

    def test_negative_pnl(self):
        data = DailySummaryData(
            date_str="2026-03-10 (Tue)",
            total_entries=1,
            completed_trades=1,
            trades=[
                TradeRecord("Strat", "SPY", 500.0, 498.0, -0.40, "call", "stop_loss", "09:45", "C"),
            ],
            daily_pnl=-0.40,
            strategy_dist={"Strat": 1},
            quality_dist={"C": 1},
        )
        text = format_daily_summary(data)
        assert "-0.4%" in text

    def test_all_winners_no_worst_section(self):
        data = DailySummaryData(
            date_str="2026-03-10 (Tue)",
            total_entries=1,
            completed_trades=1,
            trades=[
                TradeRecord("Strat", "AAPL", 100.0, 101.0, 1.0, "call", "take_profit", "10:00", "A"),
            ],
            daily_pnl=1.0,
            strategy_dist={"Strat": 1},
            quality_dist={"A": 1},
        )
        text = format_daily_summary(data)
        assert "最优交易" in text
        assert "最差交易" not in text
