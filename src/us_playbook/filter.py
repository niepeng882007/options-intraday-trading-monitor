from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import yaml

from src.hk import FilterResult
from src.utils.logger import setup_logger

logger = setup_logger("us_filter")

ET = timezone(timedelta(hours=-5))


def check_us_filters(
    rvol: float,
    prev_high: float,
    prev_low: float,
    current_high: float,
    current_low: float,
    calendar_path: str = "config/us_calendar.yaml",
    today: date | None = None,
    inside_day_rvol_threshold: float = 0.8,
) -> FilterResult:
    """Run US trade filters and return combined result.

    Filters:
    1. Macro calendar — FOMC / NFP / CPI (blocked)
    2. Monthly OpEx — 3rd Friday (elevated; blocked if + Inside Day + low RVOL)
    3. Inside Day + low RVOL (blocked)
    """
    if today is None:
        today = datetime.now(ET).date()

    warnings: list[str] = []
    risk_level = "normal"
    tradeable = True

    # Filter 1: Economic calendar
    cal_warnings, cal_blocked = _check_calendar(today, calendar_path)
    for w in cal_warnings:
        warnings.append(w)
    if cal_blocked:
        risk_level = "blocked"
        tradeable = False

    # Filter 2: Monthly OpEx
    is_opex = _is_monthly_opex(today)
    if is_opex:
        warnings.append("月度期权到期日 (Monthly OpEx) — 注意尾盘波动")
        if risk_level == "normal":
            risk_level = "elevated"

    # Filter 3: Inside Day + low RVOL
    is_inside = False
    if prev_high > 0 and prev_low > 0 and current_high > 0 and current_low > 0:
        is_inside = current_high <= prev_high and current_low >= prev_low
        if is_inside and rvol < inside_day_rvol_threshold:
            warnings.append(
                f"Inside Day + 低 RVOL ({rvol:.2f} < {inside_day_rvol_threshold}) — 假突破概率高"
            )
            risk_level = "blocked"
            tradeable = False
        elif is_inside:
            warnings.append("Inside Day 形态 (RVOL 尚可)")
            if risk_level == "normal":
                risk_level = "elevated"

    # Filter 2+3 combined: OpEx + Inside Day + low RVOL → blocked
    if is_opex and is_inside and rvol < inside_day_rvol_threshold:
        if risk_level != "blocked":
            risk_level = "blocked"
            tradeable = False
            warnings.append("OpEx + Inside Day + 低 RVOL 三因素叠加")

    return FilterResult(tradeable=tradeable, warnings=warnings, risk_level=risk_level)


def _is_monthly_opex(d: date) -> bool:
    """Check if date is monthly options expiration (3rd Friday of month)."""
    return d.weekday() == 4 and 15 <= d.day <= 21


def _check_calendar(today: date, calendar_path: str) -> tuple[list[str], bool]:
    """Check US economic calendar. Returns (warnings, is_blocked)."""
    try:
        with open(calendar_path, "r") as f:
            cal = yaml.safe_load(f)
    except FileNotFoundError:
        return [], False
    except Exception as e:
        logger.warning("Failed to load calendar %s: %s", calendar_path, e)
        return [], False

    events = cal.get("events", [])
    warnings = []
    blocked = False

    for event in events:
        event_date = event.get("date", "")
        if isinstance(event_date, str):
            try:
                event_date = datetime.strptime(event_date, "%Y-%m-%d").date()
            except ValueError:
                continue

        name = event.get("name", "Unknown")
        risk = event.get("risk_level", "medium")

        if event_date == today:
            warnings.append(f"今日宏观事件 [{risk.upper()}]: {name}")
            if risk == "high":
                blocked = True

    return warnings, blocked
