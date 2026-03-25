"""IndexTrader 编排器 — 6 步流水线 → DailyReport。"""

from __future__ import annotations

import time
from datetime import date
from typing import TYPE_CHECKING

from src.index_trader import DailyReport
from src.index_trader.collector import IndexDataCollector
from src.index_trader.formatter import ReportFormatter
from src.index_trader.levels import LevelsAnalyzer
from src.index_trader.macro import MacroAnalyzer
from src.index_trader.mag7 import Mag7Analyzer
from src.index_trader.risk import RiskCalculator
from src.index_trader.rotation import RotationAnalyzer
from src.index_trader.scenario import ScenarioEngine
from src.index_trader.scorer import ConfidenceScorer
from src.us_playbook.filter import get_today_macro_context
from src.utils.logger import setup_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Any

    from src.collector.futu import FutuCollector

logger = setup_logger("index_trader")


class IndexTrader:
    """指数日内交易盘前分析编排器。"""

    def __init__(self, config: dict, collector: FutuCollector) -> None:
        self._cfg = config
        self._data = IndexDataCollector(collector, config)
        self._macro_analyzer = MacroAnalyzer(config)
        self._rotation_analyzer = RotationAnalyzer(config)
        self._mag7_analyzer = Mag7Analyzer(config)
        self._levels_analyzer = LevelsAnalyzer(config)
        self._scenario_engine = ScenarioEngine(config)
        self._scorer = ConfidenceScorer(config)
        self._risk_calc = RiskCalculator(config)
        self._formatter = ReportFormatter(config)

        self._prev_report: DailyReport | None = None
        self._report_cache: tuple[float, DailyReport | None] = (0.0, None)
        self._report_cache_ttl = 60  # 1 分钟内重复请求使用缓存

    async def start(self) -> None:
        """初始化数据采集层（加载每日缓存）。"""
        await self._data.start()
        logger.info("IndexTrader started")

    async def generate_report(self) -> DailyReport:
        """执行完整 6 步流水线 → DailyReport。"""
        now = time.time()
        if self._report_cache[1] and now - self._report_cache[0] < self._report_cache_ttl:
            return self._report_cache[1]

        # Step 1: 宏观
        macro = await self._data.fetch_macro()
        macro_signal = self._macro_analyzer.analyze(macro)
        logger.debug("Macro signal: %s %s (%.2f)", macro_signal.direction, macro_signal.reason, macro_signal.strength)

        # Step 2: 轮动
        indices = await self._data.fetch_indices()
        rotation, rotation_signal = self._rotation_analyzer.analyze(indices)

        # Step 3: Mag7
        mag7_stocks = await self._data.fetch_mag7()
        index_avg_change = sum(i.change_pct for i in indices) / len(indices) if indices else 0.0
        mag7, mag7_signal = self._mag7_analyzer.analyze(mag7_stocks, index_avg_change)

        # Step 4: 点位
        levels: dict = {}
        for cfg_item in self._cfg.get("indices", []):
            sym = cfg_item["symbol"]
            levels[sym] = await self._data.fetch_levels(sym)
        levels_signal = self._levels_analyzer.analyze(levels)

        # Step 5: 剧本
        # 使用 SPY 的 gap 作为整体 gap
        spy_gap = 0.0
        if indices:
            spy_indices = [i for i in indices if i.symbol == "SPY"]
            if spy_indices:
                spy_gap = spy_indices[0].gap_pct
            else:
                spy_gap = indices[0].gap_pct

        calendar_path = self._cfg.get("calendar_file", "config/us_calendar.yaml")
        cal_ctx = get_today_macro_context(calendar_path)
        calendar_events = [cal_ctx["event_name"]] if cal_ctx.get("event_name") else []

        script, script_signal = self._scenario_engine.judge(
            macro, rotation, mag7, spy_gap, calendar_events,
        )

        # Step 5.5: 评分（5 信号）
        signals = [macro_signal, rotation_signal, mag7_signal, levels_signal, script_signal]
        confidence = self._scorer.score(signals)

        # Step 6: 风控（独立输出，不参与评分）
        risk = self._risk_calc.calculate(macro, confidence)

        report = DailyReport(
            date=date.today(),
            timestamp=now,
            macro=macro,
            rotation=rotation,
            mag7=mag7,
            levels=levels,
            script=script,
            confidence=confidence,
            risk=risk,
            calendar_events=calendar_events,
            is_premarket=self._data._is_premarket(),
        )

        self._report_cache = (now, report)
        logger.info(
            "Report generated: grade=%s score=%.1f direction=%s script=%s",
            confidence.confidence_grade, confidence.total_score,
            confidence.direction, script.primary_script.value,
        )
        return report

    async def push_report(
        self,
        send_fn: Callable[..., Coroutine[Any, Any, None]],
        is_update: bool = False,
    ) -> None:
        """生成报告并通过 Telegram 推送。"""
        report = await self.generate_report()
        prev = self._prev_report if is_update else None
        html = self._formatter.format(report, prev=prev)

        await send_fn(html)

        # 保存为 prev（供下次 update diff 使用）
        if not is_update:
            self._prev_report = report
        else:
            self._prev_report = report

    async def generate_section(self, section: str) -> str:
        """生成单个段落（用于 /levels, /mag7 等命令）。"""
        report = await self.generate_report()
        section_map = {
            "macro": self._formatter._section_macro,
            "rotation": self._formatter._section_rotation,
            "mag7": self._formatter._section_mag7,
            "levels": self._formatter._section_levels,
            "script": self._formatter._section_script,
            "score": self._formatter._section_score_detail,
        }
        fn = section_map.get(section)
        if fn is None:
            return f"未知段落: {section}"

        # levels 和 score_detail 不需要 prev 参数
        if section in ("levels", "score"):
            return fn(report)
        return fn(report, None)

    def close(self) -> None:
        """清理资源。"""
        self._report_cache = (0.0, None)
        self._prev_report = None
        logger.info("IndexTrader closed")
