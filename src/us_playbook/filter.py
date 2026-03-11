from __future__ import annotations

import json
import os
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import yaml

from src.common.types import FilterResult
from src.utils.logger import setup_logger

logger = setup_logger("us_filter")

ET = ZoneInfo("America/New_York")

# Earnings cache: {symbol: {"dates": [...], "ts": epoch}}
_EARNINGS_CACHE_PATH = "data/earnings_cache.json"
_EARNINGS_CACHE_TTL = 86400  # 24h


def check_us_filters(
    rvol: float,
    prev_high: float,
    prev_low: float,
    current_high: float,
    current_low: float,
    calendar_path: str = "config/us_calendar.yaml",
    today: date | None = None,
    inside_day_rvol_threshold: float = 0.8,
    symbol: str | None = None,
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
    block_reasons: list[str] = []

    # Filter 1: Economic calendar
    cal_warnings, cal_blocked = _check_calendar(today, calendar_path)
    for w in cal_warnings:
        warnings.append(w)
    if cal_blocked:
        risk_level = "blocked"
        tradeable = False
        block_reasons.append("calendar")

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
            block_reasons.append("inside_day_rvol")
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
        if "opex_combo" not in block_reasons:
            block_reasons.append("opex_combo")

    # Filter 4: Earnings day (P0-2)
    if symbol:
        earnings_risk, earnings_msg = _check_earnings(symbol, today)
        if earnings_msg:
            warnings.append(earnings_msg)
        if earnings_risk == "blocked":
            risk_level = "blocked"
            tradeable = False
            block_reasons.append("earnings")
        elif earnings_risk == "elevated" and risk_level == "normal":
            risk_level = "elevated"

    return FilterResult(tradeable=tradeable, warnings=warnings, risk_level=risk_level, block_reasons=block_reasons)


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


def _load_earnings_cache() -> dict:
    """Load earnings cache from disk."""
    try:
        with open(_EARNINGS_CACHE_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_earnings_cache(cache: dict) -> None:
    """Save earnings cache to disk."""
    try:
        os.makedirs(os.path.dirname(_EARNINGS_CACHE_PATH) or ".", exist_ok=True)
        with open(_EARNINGS_CACHE_PATH, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        logger.warning("Failed to save earnings cache: %s", e)


def _check_earnings(symbol: str, today: date) -> tuple[str, str]:
    """Check if symbol has earnings today or yesterday.

    Returns (risk_level, warning_message).
    risk_level: "blocked" (earnings day), "elevated" (day after), or "" (no issue).
    """
    cache = _load_earnings_cache()
    now = time.time()

    # Check cache freshness
    entry = cache.get(symbol)
    if entry and now - entry.get("ts", 0) < _EARNINGS_CACHE_TTL:
        earnings_dates = entry.get("dates", [])
    else:
        # Fetch from yfinance
        earnings_dates = _fetch_earnings_dates(symbol)
        cache[symbol] = {"dates": earnings_dates, "ts": now}
        _save_earnings_cache(cache)

    if not earnings_dates:
        return "", ""

    today_str = today.isoformat()
    from datetime import timedelta
    yesterday_str = (today - timedelta(days=1)).isoformat()

    if today_str in earnings_dates:
        return "blocked", f"📢 {symbol} 今日财报日 — VP/Regime 可能失效，建议回避"
    if yesterday_str in earnings_dates:
        return "elevated", f"📢 {symbol} 昨日财报 — Gap + IV crush 风险，谨慎操作"

    return "", ""


def _fetch_earnings_dates(symbol: str) -> list[str]:
    """Fetch upcoming/recent earnings dates from yfinance. Returns ISO date strings."""
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        dates = ticker.earnings_dates
        if dates is None or dates.empty:
            return []
        # Convert to ISO date strings (keep last 5 + next 2)
        result = []
        for dt_idx in dates.index:
            try:
                result.append(dt_idx.date().isoformat())
            except Exception:
                continue
        return result[:10]
    except Exception as e:
        logger.warning("Failed to fetch earnings dates for %s: %s", symbol, e)
        return []
