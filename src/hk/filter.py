from __future__ import annotations

from datetime import datetime, date, timezone, timedelta

import yaml

from src.hk import FilterResult
from src.utils.logger import setup_logger

logger = setup_logger("hk_filter")

HKT = timezone(timedelta(hours=8))


def check_filters(
    symbol: str,
    turnover: float,
    prev_high: float,
    prev_low: float,
    current_high: float,
    current_low: float,
    atr_current: float = 0.0,
    atr_prev: float = 0.0,
    atm_iv: float = 0.0,
    iv_rank: float = 0.0,
    rvol: float = 1.0,
    expiry_date: date | None = None,
    calendar_path: str = "config/hk_calendar.yaml",
    min_turnover: float = 1e8,
    today: date | None = None,
) -> FilterResult:
    """Run all trade filters and return combined result.

    Filters:
    1. Economic calendar -- major macro events
    2. Inside Day -- today's range inside yesterday's range with shrinking ATR
    3. IV + RVOL -- high IV rank with low RVOL (premium overpriced, no direction)
    4. Min turnover -- minimum daily turnover threshold
    5. Expiry risk -- options expiring today have extreme theta decay
    """
    if today is None:
        today = datetime.now(HKT).date()

    warnings: list[str] = []
    risk_level = "normal"
    tradeable = True
    block_reasons: list[str] = []

    # Filter 1: Economic calendar
    calendar_warnings = _check_calendar(today, calendar_path)
    if calendar_warnings:
        for w in calendar_warnings:
            warnings.append(w)
            if "high" in w.lower():
                risk_level = "high"
                tradeable = False
                block_reasons.append("calendar")
            elif risk_level == "normal":
                risk_level = "elevated"

    # Filter 2: Inside Day
    if prev_high > 0 and prev_low > 0 and current_high > 0 and current_low > 0:
        is_inside = current_high <= prev_high and current_low >= prev_low
        atr_shrinking = (
            atr_current < atr_prev * 0.7
            if atr_prev > 0 and atr_current > 0
            else False
        )
        if is_inside and atr_shrinking:
            warnings.append(
                f"Inside Day: \u4eca\u65e5\u533a\u95f4 [{current_low:.2f}-{current_high:.2f}] "
                f"\u5728\u6628\u65e5 [{prev_low:.2f}-{prev_high:.2f}] \u5185\uff0cATR \u840e\u7f29"
            )
            if risk_level == "normal":
                risk_level = "elevated"
        elif is_inside:
            warnings.append("Inside Day \u5f62\u6001 (ATR \u672a\u660e\u663e\u840e\u7f29)")

    # Filter 3: IV + RVOL mismatch
    if iv_rank > 80 and rvol < 1.0:
        warnings.append(
            f"IV Rank {iv_rank:.0f}% (>80) + RVOL {rvol:.2f} (<1.0): "
            f"\u671f\u6743\u6ea2\u4ef7\u8fc7\u9ad8\u4e14\u65e0\u65b9\u5411"
        )
        tradeable = False
        risk_level = "high"
        block_reasons.append("iv_rvol_mismatch")
    elif iv_rank > 60:
        warnings.append(f"IV Rank \u504f\u9ad8 ({iv_rank:.0f}%)")
        if risk_level == "normal":
            risk_level = "elevated"

    # Filter 4: Min turnover
    if turnover < min_turnover and turnover > 0:
        warnings.append(
            f"\u6210\u4ea4\u989d {turnover / 1e8:.2f} \u4ebf < \u9608\u503c {min_turnover / 1e8:.0f} \u4ebf HKD"
        )
        if risk_level == "normal":
            risk_level = "elevated"

    # Filter 5: Expiry risk
    if expiry_date and expiry_date == today:
        warnings.append(
            "\u672b\u65e5\u671f\u6743 \u2014 \u5230\u671f\u65e5 Theta \u8870\u51cf\u6781\u5feb\uff0c\u5efa\u8bae\u4ed3\u4f4d\u51cf\u534a"
        )
        if risk_level != "high":
            risk_level = "high"

    return FilterResult(tradeable=tradeable, warnings=warnings, risk_level=risk_level, block_reasons=block_reasons)


def _check_calendar(today: date, calendar_path: str) -> list[str]:
    """Check economic calendar for today and tomorrow."""
    try:
        with open(calendar_path, "r") as f:
            cal = yaml.safe_load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.warning("Failed to load calendar %s: %s", calendar_path, e)
        return []

    events = cal.get("events", [])
    tomorrow = today + timedelta(days=1)
    warnings = []

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
            warnings.append(f"\u4eca\u65e5\u5b8f\u89c2\u4e8b\u4ef6 [{risk.upper()}]: {name}")
        elif event_date == tomorrow:
            warnings.append(f"\u660e\u65e5\u5b8f\u89c2\u4e8b\u4ef6 [{risk.upper()}]: {name}")

    return warnings
