"""经济日历获取 — 从 us_calendar.yaml 读取今日事件。"""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from models import CalendarEvent

logger = logging.getLogger("calendar")

_ET = ZoneInfo("America/New_York")

# FOMC/NFP/CPI 的典型发布时间（YAML 中不含时间字段）
_DEFAULT_TIMES: dict[str, str] = {
    "FOMC Meeting": "14:00",
    "Non-Farm Payroll": "08:30",
    "CPI Release": "08:30",
}


def get_today_events(
    calendar_path: str = "../config/us_calendar.yaml",
    today: date | None = None,
) -> list[CalendarEvent]:
    """从 YAML 日历获取今日事件。

    Parameters
    ----------
    calendar_path : str
        YAML 日历文件路径（相对于本文件或绝对路径）。
    today : date | None
        指定日期，默认为美东今天。

    Returns
    -------
    list[CalendarEvent]
        今日事件列表，按时间排序。无事件时返回空列表。
    """
    if today is None:
        today = datetime.now(_ET).date()

    today_str = today.isoformat()

    path = Path(calendar_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path

    if not path.exists():
        logger.warning("日历文件不存在: %s", path)
        return []

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        logger.warning("日历文件解析失败: %s", path, exc_info=True)
        return []

    events: list[CalendarEvent] = []

    for entry in data.get("events", []):
        entry_date = str(entry.get("date", ""))
        if entry_date != today_str:
            continue

        name = entry.get("name", "Unknown")
        risk_level = entry.get("risk_level", "medium")

        # 从默认时间表查找，否则标记为"全天"
        event_time = _DEFAULT_TIMES.get(name, "全天")

        # 假日（无 behavior 字段）不算经济事件，标记为全天
        if "behavior" not in entry and risk_level == "high":
            event_time = "全天"

        events.append(
            CalendarEvent(
                time=event_time,
                name=name,
                importance=risk_level,
                previous="",  # YAML 中无前值/预期
                forecast="",
            )
        )

    # 检查月度 OpEx（每月第三个周五）
    if _is_monthly_opex(today):
        events.append(
            CalendarEvent(
                time="全天",
                name="月度 OpEx (期权到期日)",
                importance="medium",
            )
        )

    # 按时间排序（"全天" 排最后）
    events.sort(key=lambda e: ("99:99" if e.time == "全天" else e.time))

    return events


def _is_monthly_opex(d: date) -> bool:
    """判断是否为月度 OpEx（每月第三个周五）。"""
    if d.weekday() != 4:  # 不是周五
        return False
    # 第三个周五：日期在 15-21 之间
    return 15 <= d.day <= 21


def is_market_holiday(
    calendar_path: str = "../config/us_calendar.yaml",
    today: date | None = None,
) -> bool:
    """判断今天是否为美股假日。"""
    if today is None:
        today = datetime.now(_ET).date()

    path = Path(calendar_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path

    if not path.exists():
        return False

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return False

    holidays = [str(h) for h in data.get("market_holidays", [])]
    return today.isoformat() in holidays
