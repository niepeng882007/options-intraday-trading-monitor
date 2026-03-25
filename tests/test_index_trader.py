"""Tests for the Index Trader module."""

import pytest
from datetime import date

from src.index_trader import (
    ConfidenceReport,
    IndexQuote,
    LevelMap,
    MacroSnapshot,
    Mag7Snapshot,
    Mag7Stock,
    RotationScenario,
    RotationSnapshot,
    ScriptCondition,
    ScriptJudgment,
    ScriptType,
    Signal,
    VIXRegime,
    VolatilityRegime,
)
from src.index_trader.macro import MacroAnalyzer
from src.index_trader.rotation import RotationAnalyzer
from src.index_trader.mag7 import Mag7Analyzer
from src.index_trader.levels import LevelsAnalyzer
from src.index_trader.scenario import ScenarioEngine
from src.index_trader.scorer import ConfidenceScorer
from src.index_trader.risk import RiskCalculator
from src.index_trader.formatter import ReportFormatter


# ── Fixtures ──


@pytest.fixture
def default_config():
    """最小化配置用于测试。"""
    return {
        "indices": [
            {"symbol": "QQQ", "name": "Nasdaq 100 ETF"},
            {"symbol": "SPY", "name": "S&P 500 ETF"},
            {"symbol": "IWM", "name": "Russell 2000 ETF"},
        ],
        "mag7": {
            "symbols": ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA"],
            "kidnap_ratio": 3.0,
            "volume_anomaly_ratio": 2.0,
        },
        "macro": {
            "vix": {"ma_period": 10, "high_deviation": 0.20, "extreme_deviation": 0.40, "low_deviation": -0.05},
            "tnx": {"surge_threshold_bps": 5},
            "uup": {"strong_threshold_pct": 0.5},
        },
        "rotation": {"sync_threshold_pct": 0.2, "spread_threshold_pct": 1.0},
        "script": {"gap_threshold_pct": 0.3},
        "confidence": {
            "weights": {"macro": 0.25, "rotation": 0.20, "mag7": 0.15, "levels": 0.15, "script": 0.25},
            "grade_thresholds": {
                "A": {"min_score": 75, "min_resonance": 4},
                "B": {"min_score": 60, "min_resonance": 3},
                "C": {"min_score": 40, "min_resonance": 0},
            },
        },
        "risk": {
            "normal": {"max_daily_loss_pct": 2.0, "max_single_risk_pct": 1.0, "circuit_breaker_count": 3, "cooldown_minutes": 30},
            "high_volatility": {"max_daily_loss_pct": 1.0, "max_single_risk_pct": 0.5, "circuit_breaker_count": 2, "cooldown_minutes": 9999},
        },
        "level_proximity_pct": 0.001,
    }


@pytest.fixture
def bullish_macro():
    return MacroSnapshot(
        vix_current=14.0, vix_prev_close=15.0, vix_ma10=16.0,
        vix_deviation_pct=-0.125, vix_regime=VIXRegime.LOW,
        tnx_current=4.20, tnx_prev_close=4.25, tnx_change_bps=-5.0,
        uup_current=27.00, uup_prev_close=27.20, uup_change_pct=-0.74,
        dxy_direction="weak", timestamp=0.0,
    )


@pytest.fixture
def bearish_macro():
    return MacroSnapshot(
        vix_current=28.0, vix_prev_close=22.0, vix_ma10=20.0,
        vix_deviation_pct=0.40, vix_regime=VIXRegime.EXTREME,
        tnx_current=4.50, tnx_prev_close=4.42, tnx_change_bps=8.0,
        uup_current=28.00, uup_prev_close=27.50, uup_change_pct=1.82,
        dxy_direction="strong", timestamp=0.0,
    )


@pytest.fixture
def sync_indices():
    return [
        IndexQuote(symbol="QQQ", price=480.0, prev_close=478.0, change_pct=0.42, gap_pct=0.42),
        IndexQuote(symbol="SPY", price=555.0, prev_close=553.0, change_pct=0.36, gap_pct=0.36),
        IndexQuote(symbol="IWM", price=210.0, prev_close=209.2, change_pct=0.38, gap_pct=0.38),
    ]


@pytest.fixture
def diverge_indices():
    return [
        IndexQuote(symbol="QQQ", price=480.0, prev_close=478.0, change_pct=1.2, gap_pct=1.2),
        IndexQuote(symbol="SPY", price=555.0, prev_close=553.0, change_pct=0.36, gap_pct=0.36),
        IndexQuote(symbol="IWM", price=210.0, prev_close=211.5, change_pct=-0.71, gap_pct=-0.71),
    ]


@pytest.fixture
def bullish_mag7():
    return [
        Mag7Stock(code="AAPL", price=230.0, change_pct=0.8),
        Mag7Stock(code="MSFT", price=440.0, change_pct=0.6),
        Mag7Stock(code="GOOGL", price=175.0, change_pct=0.4),
        Mag7Stock(code="AMZN", price=195.0, change_pct=0.3),
        Mag7Stock(code="NVDA", price=140.0, change_pct=1.2),
        Mag7Stock(code="META", price=520.0, change_pct=0.5),
        Mag7Stock(code="TSLA", price=250.0, change_pct=0.7),
    ]


# ── Macro Analyzer ──


class TestMacroAnalyzer:
    def test_bullish_macro(self, default_config, bullish_macro):
        analyzer = MacroAnalyzer(default_config)
        signal = analyzer.analyze(bullish_macro)
        assert signal.source == "macro"
        assert signal.direction == "bullish"
        assert signal.strength > 0

    def test_bearish_macro(self, default_config, bearish_macro):
        analyzer = MacroAnalyzer(default_config)
        signal = analyzer.analyze(bearish_macro)
        assert signal.direction == "bearish"
        assert signal.strength > 0

    def test_neutral_macro(self, default_config):
        neutral = MacroSnapshot(
            vix_current=18.0, vix_prev_close=18.0, vix_ma10=18.0,
            vix_deviation_pct=0.0, vix_regime=VIXRegime.NORMAL,
            tnx_current=4.30, tnx_prev_close=4.30, tnx_change_bps=0.0,
            uup_current=27.50, uup_prev_close=27.50, uup_change_pct=0.0,
            dxy_direction="flat", timestamp=0.0,
        )
        signal = MacroAnalyzer(default_config).analyze(neutral)
        assert signal.direction == "neutral"
        assert signal.strength == 0.0


# ── Rotation Analyzer ──


class TestRotationAnalyzer:
    def test_sync_bullish(self, default_config, sync_indices):
        analyzer = RotationAnalyzer(default_config)
        snap, signal = analyzer.analyze(sync_indices)
        assert snap.scenario == RotationScenario.SYNC
        assert signal.direction == "bullish"

    def test_diverge(self, default_config, diverge_indices):
        analyzer = RotationAnalyzer(default_config)
        snap, signal = analyzer.analyze(diverge_indices)
        assert snap.scenario == RotationScenario.DIVERGE
        assert snap.leader == "QQQ"
        assert snap.laggard == "IWM"

    def test_empty_indices(self, default_config):
        snap, signal = RotationAnalyzer(default_config).analyze([])
        assert signal.direction == "neutral"
        assert signal.strength == 0.0


# ── Mag7 Analyzer ──


class TestMag7Analyzer:
    def test_bullish_consistency(self, default_config, bullish_mag7):
        analyzer = Mag7Analyzer(default_config)
        snap, signal = analyzer.analyze(bullish_mag7, index_avg_change=0.5)
        assert snap.bullish_count == 7
        assert snap.consistency_score == 1.0
        assert signal.direction == "bullish"
        assert signal.strength >= 0.7

    def test_kidnap_detection(self, default_config):
        stocks = [
            Mag7Stock(code="AAPL", price=230.0, change_pct=0.3),
            Mag7Stock(code="MSFT", price=440.0, change_pct=0.2),
            Mag7Stock(code="GOOGL", price=175.0, change_pct=0.1),
            Mag7Stock(code="AMZN", price=195.0, change_pct=0.2),
            Mag7Stock(code="NVDA", price=140.0, change_pct=5.0),  # 绑架
            Mag7Stock(code="META", price=520.0, change_pct=0.3),
            Mag7Stock(code="TSLA", price=250.0, change_pct=-0.1),
        ]
        snap, signal = Mag7Analyzer(default_config).analyze(stocks, index_avg_change=0.3)
        assert snap.is_kidnapped
        assert "NVDA" in snap.kidnap_detail
        assert "vs 指数均值" in snap.kidnap_detail
        assert "偏离" in snap.kidnap_detail and "x）" in snap.kidnap_detail

    def test_empty(self, default_config):
        snap, signal = Mag7Analyzer(default_config).analyze([], 0.0)
        assert signal.direction == "neutral"


# ── Levels Analyzer ──


class TestLevelsAnalyzer:
    def test_price_above_pmh(self, default_config):
        levels = {
            "SPY": LevelMap(
                symbol="SPY", current_price=558.0, pdc=553.0,
                pdh=556.0, pdl=550.0, pmh=557.0, pml=554.0,
            )
        }
        signal = LevelsAnalyzer(default_config).analyze(levels)
        assert signal.direction == "bullish"

    def test_price_below_pdl(self, default_config):
        levels = {
            "SPY": LevelMap(
                symbol="SPY", current_price=548.0, pdc=553.0,
                pdh=556.0, pdl=550.0, pmh=554.0, pml=551.0,
            )
        }
        signal = LevelsAnalyzer(default_config).analyze(levels)
        assert signal.direction == "bearish"

    def test_empty_levels(self, default_config):
        signal = LevelsAnalyzer(default_config).analyze({})
        assert signal.direction == "neutral"


# ── Scenario Engine ──


class TestScenarioEngine:
    def test_gap_and_go(self, default_config, bullish_macro):
        engine = ScenarioEngine(default_config)
        rotation = RotationSnapshot(
            indices=[], leader="SPY", laggard="IWM",
            spread_pct=0.1, scenario=RotationScenario.SYNC,
        )
        mag7 = Mag7Snapshot(
            stocks=[], bullish_count=6, bearish_count=1,
            avg_change_pct=0.5, consistency_score=0.86,
        )
        judgment, signal = engine.judge(bullish_macro, rotation, mag7, gap_pct=0.8, calendar_events=[])
        assert judgment.primary_script == ScriptType.GAP_AND_GO
        assert signal.direction == "bullish"

    def test_chop_small_gap(self, default_config):
        engine = ScenarioEngine(default_config)
        macro = MacroSnapshot(
            vix_current=18.0, vix_prev_close=18.0, vix_ma10=18.0,
            vix_deviation_pct=0.0, vix_regime=VIXRegime.NORMAL,
            tnx_current=4.30, tnx_prev_close=4.30, tnx_change_bps=0.0,
            uup_current=27.5, uup_prev_close=27.5, uup_change_pct=0.0,
            dxy_direction="flat",
        )
        rotation = RotationSnapshot(
            indices=[], leader="SPY", laggard="IWM",
            spread_pct=0.15, scenario=RotationScenario.SYNC,
        )
        mag7 = Mag7Snapshot(
            stocks=[], bullish_count=4, bearish_count=3,
            avg_change_pct=0.0, consistency_score=0.57,
        )
        judgment, signal = engine.judge(macro, rotation, mag7, gap_pct=0.1, calendar_events=[])
        assert judgment.primary_script == ScriptType.CHOP

    def test_insufficient_conditions_fallback(self, default_config):
        """辅助条件 <2 时 signal 应降级为 neutral。"""
        engine = ScenarioEngine(default_config)
        # 构造让所有剧本命中 <2 的场景:
        # VIX deviation +2.8% > 0 → GAP_AND_GO VIX ❌
        # VIX偏离 2.8% < 15% → GAP_FILL VIX ✅ (hit=1)
        # gap bearish + mag7 bearish → aligned → GAP_FILL not_aligned=False ❌
        # 有异常成交量 → PM 量不正常 ❌
        # SEESAW ≠ SYNC → GAP_AND_GO sync ❌
        # VIX NORMAL ≠ HIGH → REVERSAL VIX ❌
        macro = MacroSnapshot(
            vix_current=18.5, vix_prev_close=18.0, vix_ma10=18.0,
            vix_deviation_pct=0.028, vix_regime=VIXRegime.NORMAL,
            tnx_current=4.30, tnx_prev_close=4.30, tnx_change_bps=0.0,
            uup_current=27.5, uup_prev_close=27.5, uup_change_pct=0.0,
            dxy_direction="flat",
        )
        rotation = RotationSnapshot(
            indices=[], leader="SPY", laggard="IWM",
            spread_pct=0.5, scenario=RotationScenario.SEESAW,
        )
        mag7 = Mag7Snapshot(
            stocks=[Mag7Stock(code="NVDA", price=140.0, change_pct=-1.0, is_anomaly=True)],
            bullish_count=0, bearish_count=1,
            avg_change_pct=-1.0, consistency_score=1.0,
        )
        # 各剧本最高 hit=1，触发条件不充分回退
        judgment, signal = engine.judge(macro, rotation, mag7, gap_pct=-0.62, calendar_events=[])
        assert signal.direction == "neutral"
        assert signal.strength <= 0.2
        assert "条件不充分" in signal.reason

    def test_vix_positive_deviation_blocks_gap_and_go(self, default_config):
        """VIX 偏离 >0 时 GAP_AND_GO 的 VIX 条件应该 ❌。"""
        engine = ScenarioEngine(default_config)
        macro = MacroSnapshot(
            vix_current=26.95, vix_prev_close=25.0, vix_ma10=25.36,
            vix_deviation_pct=0.0626, vix_regime=VIXRegime.NORMAL,
            tnx_current=4.39, tnx_prev_close=4.33, tnx_change_bps=5.8,
            uup_current=27.65, uup_prev_close=27.54, uup_change_pct=0.40,
            dxy_direction="flat",
        )
        rotation = RotationSnapshot(
            indices=[], leader="SPY", laggard="IWM",
            spread_pct=0.1, scenario=RotationScenario.SYNC,
        )
        mag7 = Mag7Snapshot(
            stocks=[], bullish_count=6, bearish_count=1,
            avg_change_pct=0.5, consistency_score=0.86,
        )
        judgment, signal = engine.judge(macro, rotation, mag7, gap_pct=0.8, calendar_events=[])
        # VIX 条件被 block，只有 mag7 和 sync 命中 → hit=2
        # 但 GAP_AND_GO 不应该因为 VIX 而得到 3 分
        vix_cond = [c for c in judgment.primary_conditions if "VIX" in c.name]
        if vix_cond:
            assert not vix_cond[0].met

    def test_reversal(self, default_config, bearish_macro):
        engine = ScenarioEngine(default_config)
        rotation = RotationSnapshot(
            indices=[], leader="QQQ", laggard="IWM",
            spread_pct=1.5, scenario=RotationScenario.DIVERGE,
        )
        mag7 = Mag7Snapshot(
            stocks=[], bullish_count=2, bearish_count=5,
            avg_change_pct=-0.5, consistency_score=0.71,
        )
        # gap up but mag7 bearish + vix high + diverge → reversal
        judgment, signal = engine.judge(bearish_macro, rotation, mag7, gap_pct=0.8, calendar_events=[])
        assert judgment.primary_script == ScriptType.REVERSAL


# ── Scorer ──


class TestConfidenceScorer:
    def test_all_bullish(self, default_config):
        scorer = ConfidenceScorer(default_config)
        signals = [
            Signal(source="macro", direction="bullish", strength=0.7, reason="VIX low"),
            Signal(source="rotation", direction="bullish", strength=0.6, reason="sync up"),
            Signal(source="mag7", direction="bullish", strength=0.8, reason="7/7 up"),
            Signal(source="levels", direction="bullish", strength=0.5, reason="above PMH"),
            Signal(source="script", direction="bullish", strength=0.9, reason="gap and go"),
        ]
        report = scorer.score(signals)
        assert report.direction == "bullish"
        assert report.confidence_grade in ("A", "B")
        assert report.resonance_count >= 4
        assert not report.has_conflict

    def test_conflict_detection(self, default_config):
        scorer = ConfidenceScorer(default_config)
        signals = [
            Signal(source="macro", direction="bearish", strength=0.8, reason="VIX high"),
            Signal(source="rotation", direction="bullish", strength=0.7, reason="sync up"),
            Signal(source="mag7", direction="bullish", strength=0.6, reason="5/7 up"),
            Signal(source="levels", direction="bullish", strength=0.5, reason="above PDH"),
            Signal(source="script", direction="bullish", strength=0.7, reason="gap and go"),
        ]
        report = scorer.score(signals)
        assert report.direction == "bullish"
        assert report.has_conflict
        assert "macro" in report.conflict_detail

    def test_grade_d(self, default_config):
        scorer = ConfidenceScorer(default_config)
        signals = [
            Signal(source="macro", direction="neutral", strength=0.1, reason="flat"),
            Signal(source="rotation", direction="neutral", strength=0.0, reason="sync flat"),
            Signal(source="mag7", direction="neutral", strength=0.2, reason="mixed"),
            Signal(source="levels", direction="neutral", strength=0.0, reason="no data"),
            Signal(source="script", direction="neutral", strength=0.1, reason="chop"),
        ]
        report = scorer.score(signals)
        assert report.confidence_grade == "D"
        # total_score = 2.5 + 0 + 3.0 + 0 + 2.5 = 8.0（含 neutral 模块）
        assert report.total_score < 40
        assert report.total_score > 0  # neutral 模块有贡献

    def test_total_score_includes_neutral(self, default_config):
        """total_score 应含全部模块，不排除 neutral。"""
        scorer = ConfidenceScorer(default_config)
        signals = [
            Signal(source="macro", direction="bearish", strength=0.19, reason="vix up"),
            Signal(source="rotation", direction="neutral", strength=0.30, reason="diverge"),
            Signal(source="mag7", direction="neutral", strength=0.30, reason="mixed"),
            Signal(source="levels", direction="bearish", strength=0.17, reason="below pdl"),
            Signal(source="script", direction="bearish", strength=0.60, reason="gap and go"),
        ]
        report = scorer.score(signals)
        # 全部加总: 0.19*0.25*100 + 0.30*0.20*100 + 0.30*0.15*100 + 0.17*0.15*100 + 0.60*0.25*100
        # = 4.75 + 6.0 + 4.5 + 2.55 + 15.0 = 32.8
        assert 32 <= report.total_score <= 34
        assert report.direction == "bearish"
        # 共振: macro(bearish) + levels(bearish) + script(bearish) = 3
        assert report.resonance_count == 3


# ── Risk Calculator ──


class TestRiskCalculator:
    def test_normal_regime(self, default_config, bullish_macro):
        calc = RiskCalculator(default_config)
        confidence = ConfidenceReport(
            signals=[], total_score=70, bullish_score=70, bearish_score=0,
            direction="bullish", direction_pct=1.0, resonance_count=4,
            confidence_grade="B", has_conflict=False,
        )
        params = calc.calculate(bullish_macro, confidence)
        assert params.volatility_regime == VolatilityRegime.NORMAL
        assert params.max_daily_loss_pct == 2.0

    def test_high_vol_regime(self, default_config, bearish_macro):
        calc = RiskCalculator(default_config)
        confidence = ConfidenceReport(
            signals=[], total_score=30, bullish_score=0, bearish_score=30,
            direction="bearish", direction_pct=1.0, resonance_count=1,
            confidence_grade="D", has_conflict=False,
        )
        params = calc.calculate(bearish_macro, confidence)
        assert params.volatility_regime == VolatilityRegime.HIGH
        # Grade D 额外收紧: 1.0 * 0.5 = 0.5
        assert params.max_daily_loss_pct == 0.5

    def test_grade_c_tightening(self, default_config, bullish_macro):
        calc = RiskCalculator(default_config)
        confidence = ConfidenceReport(
            signals=[], total_score=45, bullish_score=45, bearish_score=0,
            direction="bullish", direction_pct=1.0, resonance_count=2,
            confidence_grade="C", has_conflict=False,
        )
        params = calc.calculate(bullish_macro, confidence)
        # Normal regime * C tightening: 2.0 * 0.75 = 1.5
        assert params.max_daily_loss_pct == 1.5


# ── Formatter ──


class TestReportFormatter:
    def test_format_output_contains_sections(self, default_config):
        from src.index_trader import DailyReport, RiskParams

        report = DailyReport(
            date=date(2026, 3, 25),
            timestamp=0.0,
            macro=MacroSnapshot(
                vix_current=18.0, vix_prev_close=18.0, vix_ma10=18.0,
                vix_deviation_pct=0.0, vix_regime=VIXRegime.NORMAL,
                tnx_current=4.30, tnx_prev_close=4.30, tnx_change_bps=0.0,
                uup_current=27.5, uup_prev_close=27.5, uup_change_pct=0.0,
                dxy_direction="flat",
            ),
            rotation=RotationSnapshot(
                indices=[IndexQuote("SPY", 555.0, 553.0, 0.36, gap_pct=0.36)],
                leader="SPY", laggard="SPY", spread_pct=0.0,
                scenario=RotationScenario.SYNC,
            ),
            mag7=Mag7Snapshot(
                stocks=[], bullish_count=4, bearish_count=3,
                avg_change_pct=0.1, consistency_score=0.57,
            ),
            levels={"SPY": LevelMap("SPY", 555.0, 553.0, 556.0, 550.0, 554.0, 552.0)},
            script=ScriptJudgment(
                primary_script=ScriptType.CHOP,
                primary_conditions=[ScriptCondition("gap ≤ 0.3%", True, "gap=0.10%")],
                primary_hit_count=2,
                alternatives=[],
            ),
            confidence=ConfidenceReport(
                signals=[], total_score=45.0, bullish_score=30.0, bearish_score=15.0,
                direction="bullish", direction_pct=0.67, resonance_count=2,
                confidence_grade="C", has_conflict=False,
            ),
            risk=RiskParams(
                volatility_regime=VolatilityRegime.NORMAL,
                max_daily_loss_pct=2.0, max_single_risk_pct=1.0,
                circuit_breaker_count=3, cooldown_minutes=30,
            ),
        )

        formatter = ReportFormatter(default_config)
        html = formatter.format(report)

        assert "Index Trader 盘前报告" in html
        assert "宏观面板" in html
        assert "板块轮动" in html
        assert "Mag7 温度计" in html
        assert "关键点位" in html
        assert "开盘剧本" in html
        assert "评分明细" in html
        assert "chop" in html

    def test_pdc_in_levels(self, default_config):
        """关键点位应包含 PDC。"""
        lm = LevelMap("SPY", 555.0, 553.0, 556.0, 550.0, 554.0, 552.0)
        text = ReportFormatter._format_level_map(lm)
        assert "PDC:553.00" in text

    def test_premarket_rotation_format(self, default_config):
        """盘前模式只显示一个值标注'盘前'。"""
        from src.index_trader import DailyReport, RiskParams

        report = DailyReport(
            date=date(2026, 3, 25), timestamp=0.0,
            macro=MacroSnapshot(
                vix_current=18.0, vix_prev_close=18.0, vix_ma10=18.0,
                vix_deviation_pct=0.0, vix_regime=VIXRegime.NORMAL,
                tnx_current=4.30, tnx_prev_close=4.30, tnx_change_bps=0.0,
                uup_current=27.5, uup_prev_close=27.5, uup_change_pct=0.0,
                dxy_direction="flat",
            ),
            rotation=RotationSnapshot(
                indices=[IndexQuote("SPY", 555.0, 553.0, 0.36, gap_pct=0.36)],
                leader="SPY", laggard="SPY", spread_pct=0.0,
                scenario=RotationScenario.SYNC,
            ),
            mag7=Mag7Snapshot(stocks=[], bullish_count=4, bearish_count=3,
                             avg_change_pct=0.1, consistency_score=0.57),
            levels={},
            script=ScriptJudgment(ScriptType.CHOP, [], 2, []),
            confidence=ConfidenceReport(
                signals=[], total_score=45.0, bullish_score=30.0, bearish_score=15.0,
                direction="bullish", direction_pct=0.67, resonance_count=2,
                confidence_grade="C", has_conflict=False,
            ),
            risk=RiskParams(VolatilityRegime.NORMAL, 2.0, 1.0, 3, 30),
            is_premarket=True,
        )
        formatter = ReportFormatter(default_config)
        html = formatter.format(report)
        assert "盘前:" in html
        assert "缺口:" not in html

    def test_insufficient_script_display(self, default_config):
        """hit_count < 2 时应显示条件不充分。"""
        from src.index_trader import DailyReport, RiskParams

        report = DailyReport(
            date=date(2026, 3, 25), timestamp=0.0,
            macro=MacroSnapshot(
                vix_current=18.0, vix_prev_close=18.0, vix_ma10=18.0,
                vix_deviation_pct=0.0, vix_regime=VIXRegime.NORMAL,
                tnx_current=4.30, tnx_prev_close=4.30, tnx_change_bps=0.0,
                uup_current=27.5, uup_prev_close=27.5, uup_change_pct=0.0,
                dxy_direction="flat",
            ),
            rotation=RotationSnapshot(
                indices=[], leader="SPY", laggard="IWM",
                spread_pct=0.1, scenario=RotationScenario.SYNC,
            ),
            mag7=Mag7Snapshot(stocks=[], bullish_count=4, bearish_count=3,
                             avg_change_pct=0.1, consistency_score=0.57),
            levels={},
            script=ScriptJudgment(ScriptType.GAP_AND_GO, [], 1, []),  # hit=1 < 2
            confidence=ConfidenceReport(
                signals=[], total_score=20.0, bullish_score=10.0, bearish_score=10.0,
                direction="neutral", direction_pct=0.0, resonance_count=0,
                confidence_grade="D", has_conflict=False,
            ),
            risk=RiskParams(VolatilityRegime.NORMAL, 2.0, 1.0, 3, 30),
        )
        formatter = ReportFormatter(default_config)
        html = formatter.format(report)
        assert "条件不充分" in html

    def test_diff_markers(self, default_config):
        """Version 2 报告应标注 △ 变化。"""
        from src.index_trader import DailyReport, RiskParams

        base_kwargs = dict(
            date=date(2026, 3, 25), timestamp=0.0,
            macro=MacroSnapshot(
                vix_current=18.0, vix_prev_close=18.0, vix_ma10=18.0,
                vix_deviation_pct=0.0, vix_regime=VIXRegime.NORMAL,
                tnx_current=4.30, tnx_prev_close=4.30, tnx_change_bps=0.0,
                uup_current=27.5, uup_prev_close=27.5, uup_change_pct=0.0,
                dxy_direction="flat",
            ),
            rotation=RotationSnapshot(
                indices=[], leader="SPY", laggard="IWM",
                spread_pct=0.1, scenario=RotationScenario.SYNC,
            ),
            mag7=Mag7Snapshot(stocks=[], bullish_count=4, bearish_count=3,
                             avg_change_pct=0.1, consistency_score=0.57),
            levels={},
            script=ScriptJudgment(ScriptType.CHOP, [], 2, []),
            risk=RiskParams(VolatilityRegime.NORMAL, 2.0, 1.0, 3, 30),
        )

        prev = DailyReport(
            **base_kwargs,
            confidence=ConfidenceReport(
                signals=[], total_score=45.0, bullish_score=30.0, bearish_score=15.0,
                direction="bullish", direction_pct=0.67, resonance_count=2,
                confidence_grade="C", has_conflict=False,
            ),
        )
        current = DailyReport(
            **base_kwargs,
            confidence=ConfidenceReport(
                signals=[], total_score=65.0, bullish_score=50.0, bearish_score=15.0,
                direction="bullish", direction_pct=0.77, resonance_count=3,
                confidence_grade="B", has_conflict=False,
            ),
        )

        formatter = ReportFormatter(default_config)
        html = formatter.format(current, prev=prev)
        assert "△ 等级 C → B" in html


# ── Common levels (extracted function) ──


class TestExtractPreviousDayHL:
    def test_import_from_common(self):
        from src.common.levels import extract_previous_day_hl
        assert callable(extract_previous_day_hl)

    def test_reexport_from_us(self):
        from src.us_playbook.levels import extract_previous_day_hl
        assert callable(extract_previous_day_hl)
