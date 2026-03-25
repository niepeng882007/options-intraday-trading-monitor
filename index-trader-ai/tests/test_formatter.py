"""测试格式化输出。"""

import re

from formatter import DataFormatter, _fmt_f2, _fmt_vol, _fmt_pct_signed, _fmt_bps
from models import (
    CalendarEvent,
    CollectionResult,
    DataStatus,
    IndexData,
    MacroData,
    Mag7Data,
)


# ── 工具函数测试 ──


class TestFormatUtils:
    def test_fmt_f2_value(self):
        assert _fmt_f2(123.456) == "123.46"

    def test_fmt_f2_none(self):
        assert _fmt_f2(None) == "[不可用]"

    def test_fmt_vol_millions(self):
        assert _fmt_vol(1_234_567) == "1.23M"

    def test_fmt_vol_thousands(self):
        assert _fmt_vol(320_000) == "320K"

    def test_fmt_vol_none(self):
        assert _fmt_vol(None) == "[不可用]"

    def test_fmt_vol_zero(self):
        assert _fmt_vol(0) == "[不可用]"

    def test_fmt_pct_positive(self):
        assert _fmt_pct_signed(0.42) == "+0.42%"

    def test_fmt_pct_negative(self):
        assert _fmt_pct_signed(-1.23) == "-1.23%"

    def test_fmt_pct_none(self):
        assert _fmt_pct_signed(None) == "[不可用]"

    def test_fmt_bps_positive(self):
        assert _fmt_bps(5.0) == "+5.0bps"

    def test_fmt_bps_none(self):
        assert _fmt_bps(None) == "[不可用]"


# ── Telegram Markdown 格式 ──


class TestTelegramFormat:
    def _make_result(self) -> CollectionResult:
        return CollectionResult(
            timestamp=0.0,
            date_str="2026-03-25",
            time_str="09:00 ET",
            macro=MacroData(
                vix_current=18.50, vix_prev_close=19.00, vix_ma10=17.00,
                vix_deviation_pct=0.088, tnx_current=4.300, tnx_prev_close=4.250,
                tnx_change_bps=5.0, uup_current=27.50, uup_prev_close=27.40,
                uup_change_pct=0.36,
            ),
            indices=[
                IndexData(
                    symbol="QQQ", price=480.50, prev_close=478.00, change_pct=0.52,
                    volume=1500000, gap_pct=0.52, pdc=478.00, pdh=481.00, pdl=475.00,
                    pmh=480.00, pml=477.00, weekly_high=483.00, weekly_low=472.00,
                    poc=479.00, vah=481.50, val=476.50,
                    gamma_call_wall=490.0, gamma_put_wall=470.0,
                ),
            ],
            mag7=[
                Mag7Data(symbol="AAPL", change_pct=0.80, volume=2500000, volume_ratio=1.50),
                Mag7Data(symbol="NVDA", change_pct=-0.30, volume=None, volume_ratio=None),
            ],
            calendar=[
                CalendarEvent(time="08:30", name="CPI Release", importance="high"),
            ],
        )

    def test_contains_all_sections(self, default_config):
        fmt = DataFormatter(default_config)
        result = self._make_result()
        text = fmt.format_telegram(result)

        assert "INDEX TRADER" in text
        assert "宏观" in text
        assert "指数盘前" in text
        assert "Mag7 盘前" in text
        assert "期权/成交量分布" in text
        assert "经济日历" in text
        assert "数据状态" in text

    def test_none_renders_unavailable(self, default_config):
        fmt = DataFormatter(default_config)
        result = CollectionResult(
            timestamp=0.0, date_str="2026-03-25", time_str="09:00 ET",
            macro=MacroData(),  # 全部 None
            indices=[IndexData(symbol="QQQ")],  # 全部 None
            mag7=[Mag7Data(symbol="AAPL")],
        )
        text = fmt.format_telegram(result)
        assert "[不可用]" in text

    def test_delta_markers(self, default_config):
        fmt = DataFormatter(default_config)
        result = self._make_result()

        # 创建 prev，VIX 有明显变化
        prev = CollectionResult(
            timestamp=0.0, date_str="2026-03-25", time_str="08:30 ET",
            macro=MacroData(
                vix_current=17.00, vix_prev_close=19.00, vix_ma10=17.00,
                vix_deviation_pct=0.0, tnx_current=4.250, tnx_prev_close=4.250,
                tnx_change_bps=0.0, uup_current=27.40, uup_prev_close=27.40,
                uup_change_pct=0.0,
            ),
            indices=[
                IndexData(symbol="QQQ", price=478.00, change_pct=0.0),
            ],
            mag7=[
                Mag7Data(symbol="AAPL", change_pct=0.10),
            ],
        )

        text = fmt.format_telegram(result, prev=prev)
        assert "△" in text  # VIX 从 17 到 18.5 变化 >0.05%

    def test_calendar_events_shown(self, default_config):
        fmt = DataFormatter(default_config)
        result = self._make_result()
        text = fmt.format_telegram(result)
        assert "CPI Release" in text
        assert "08:30" in text

    def test_status_all_normal(self, default_config):
        fmt = DataFormatter(default_config)
        result = self._make_result()
        text = fmt.format_telegram(result)
        assert "全部正常" in text


# ── Raw Text 格式 ──


class TestRawFormat:
    def test_no_emoji(self, default_config):
        fmt = DataFormatter(default_config)
        result = CollectionResult(
            timestamp=0.0, date_str="2026-03-25", time_str="09:00 ET",
            macro=MacroData(vix_current=18.0, vix_prev_close=18.0, vix_ma10=17.0,
                            vix_deviation_pct=0.06, tnx_current=4.3, tnx_prev_close=4.3,
                            tnx_change_bps=0.0, uup_current=27.5, uup_prev_close=27.5,
                            uup_change_pct=0.0),
            indices=[IndexData(symbol="SPY", price=555.0, prev_close=553.0,
                               change_pct=0.36, volume=1000000)],
            mag7=[Mag7Data(symbol="AAPL", change_pct=0.5, volume=500000, volume_ratio=1.2)],
        )
        text = fmt.format_raw(result)

        # 不应包含 emoji
        emoji_pattern = re.compile(
            "["
            "\U0001F300-\U0001F9FF"  # 各类 emoji
            "\U00002702-\U000027B0"
            "\U0001F600-\U0001F64F"
            "\U0001F680-\U0001F6FF"
            "]+",
            flags=re.UNICODE,
        )
        assert not emoji_pattern.search(text), f"Found emoji in raw text: {text[:200]}"

        # 不应包含 Markdown 标记
        assert "**" not in text
        assert "*" not in text  # Telegram Markdown bold

    def test_raw_header(self, default_config):
        fmt = DataFormatter(default_config)
        result = CollectionResult(
            timestamp=0.0, date_str="2026-03-25", time_str="09:00 ET",
            macro=MacroData(),
        )
        text = fmt.format_raw(result)
        assert "=== INDEX TRADER 盘前数据 ===" in text
        assert "2026-03-25" in text

    def test_raw_unavailable(self, default_config):
        fmt = DataFormatter(default_config)
        result = CollectionResult(
            timestamp=0.0, date_str="2026-03-25", time_str="09:00 ET",
            macro=MacroData(),
            indices=[IndexData(symbol="QQQ")],
        )
        text = fmt.format_raw(result)
        assert "[不可用]" in text


# ── 点位格式 ──


class TestLevelsFormat:
    def test_single_index(self, default_config):
        fmt = DataFormatter(default_config)
        result = CollectionResult(
            timestamp=0.0, date_str="2026-03-25", time_str="09:00 ET",
            macro=MacroData(),
            indices=[
                IndexData(symbol="SPY", price=555.0, pdc=553.0, pdh=556.0,
                          pdl=550.0, pmh=554.0, pml=552.0, poc=553.50,
                          vah=555.50, val=551.50),
            ],
        )
        text = fmt.format_levels(result, "SPY")
        assert "SPY" in text
        assert "PDH" in text
        assert "556.00" in text

    def test_unknown_symbol(self, default_config):
        fmt = DataFormatter(default_config)
        result = CollectionResult(
            timestamp=0.0, date_str="2026-03-25", time_str="09:00 ET",
            macro=MacroData(),
        )
        text = fmt.format_levels(result, "XYZ")
        assert "未找到" in text


# ── 风控格式 ──


class TestRiskFormat:
    def test_normal_regime(self, default_config):
        fmt = DataFormatter(default_config)
        text = fmt.format_risk({
            "regime": "normal",
            "vix_deviation_pct": 0.05,
            "max_single_risk_pct": 1.0,
            "max_daily_loss_pct": 2.0,
            "circuit_breaker_count": 3,
            "cooldown_minutes": 30,
        })
        assert "常规模式" in text
        assert "1.0%" in text

    def test_high_vol_regime(self, default_config):
        fmt = DataFormatter(default_config)
        text = fmt.format_risk({
            "regime": "high_volatility",
            "vix_deviation_pct": 0.25,
            "max_single_risk_pct": 0.5,
            "max_daily_loss_pct": 1.0,
            "circuit_breaker_count": 2,
            "cooldown_minutes": 999,
        })
        assert "高波动" in text
        assert "0.5%" in text
