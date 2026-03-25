"""测试数据模型。"""

from models import (
    CalendarEvent,
    CollectionResult,
    DataStatus,
    IndexData,
    MacroData,
    Mag7Data,
    RiskLookup,
)


class TestMacroData:
    def test_defaults_are_none(self):
        m = MacroData()
        assert m.vix_current is None
        assert m.tnx_current is None
        assert m.uup_current is None
        assert m.timestamp == 0.0

    def test_with_values(self):
        m = MacroData(vix_current=18.5, vix_prev_close=19.0, vix_ma10=17.0)
        assert m.vix_current == 18.5
        assert m.vix_prev_close == 19.0


class TestIndexData:
    def test_defaults(self):
        idx = IndexData(symbol="QQQ")
        assert idx.symbol == "QQQ"
        assert idx.price is None
        assert idx.status == "ok"

    def test_with_full_data(self):
        idx = IndexData(
            symbol="SPY", price=555.0, prev_close=553.0,
            change_pct=0.36, volume=1200000, gap_pct=0.36,
            pdh=556.0, pdl=550.0, pmh=554.0, pml=552.0,
        )
        assert idx.change_pct == 0.36
        assert idx.pdh == 556.0


class TestMag7Data:
    def test_defaults(self):
        m = Mag7Data(symbol="AAPL")
        assert m.change_pct is None
        assert m.volume_ratio is None

    def test_with_data(self):
        m = Mag7Data(symbol="NVDA", change_pct=1.5, volume=3500000, volume_ratio=2.3)
        assert m.volume_ratio == 2.3


class TestCollectionResult:
    def test_empty_result(self):
        r = CollectionResult(
            timestamp=0.0,
            date_str="2026-03-25",
            time_str="09:00 ET",
            macro=MacroData(),
        )
        assert r.indices == []
        assert r.mag7 == []
        assert r.calendar == []
        assert r.statuses == []


class TestRiskLookup:
    def test_normal_defaults(self):
        r = RiskLookup()
        assert r.regime == "normal"
        assert r.max_daily_loss_pct == 2.0
        assert r.cooldown_minutes == 30
