"""Trading day utilities — previous trading day lookup and time range conversion."""

from __future__ import annotations

from datetime import date, datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo

import yaml

from src.utils.logger import setup_logger

logger = setup_logger("trading_days")

# Timezone constants
_TZ_ET = ZoneInfo("America/New_York")
_TZ_HKT = ZoneInfo("Asia/Hong_Kong")

# Cache loaded holidays to avoid re-reading YAML on every call
_holidays_cache: dict[str, set[date]] = {}


def _load_holidays(market: str) -> set[date]:
    """Load market holidays from the calendar YAML."""
    if market in _holidays_cache:
        return _holidays_cache[market]

    path = "config/us_calendar.yaml" if market == "us" else "config/hk_calendar.yaml"
    holidays: set[date] = set()
    try:
        with open(path) as f:
            cal = yaml.safe_load(f) or {}
        for d_str in cal.get("market_holidays", []):
            try:
                holidays.add(date.fromisoformat(str(d_str)))
            except (ValueError, TypeError):
                pass
    except FileNotFoundError:
        logger.warning("Calendar file not found: %s", path)
    except Exception:
        logger.warning("Failed to load holidays from %s", path, exc_info=True)

    _holidays_cache[market] = holidays
    return holidays


def previous_trading_day(market: str = "us", ref_date: date | None = None) -> date:
    """Return the most recent trading day before ref_date.

    Skips weekends and market holidays from the calendar YAML.
    """
    if ref_date is None:
        tz = _TZ_ET if market == "us" else _TZ_HKT
        ref_date = datetime.now(tz).date()

    holidays = _load_holidays(market)
    d = ref_date - timedelta(days=1)

    # Walk back up to 10 days (covers long holiday stretches)
    for _ in range(10):
        if d.weekday() < 5 and d not in holidays:
            return d
        d -= timedelta(days=1)

    # Fallback: just return the date we landed on
    return d


def trading_day_range(d: date, market: str = "us") -> tuple[float, float]:
    """Return (start_ts, end_ts) for a given trading day.

    Covers 00:00:00 ~ 23:59:59 in the market's local timezone.
    US uses ET (America/New_York), HK uses HKT (Asia/Hong_Kong).
    """
    tz = _TZ_ET if market == "us" else _TZ_HKT
    start = datetime.combine(d, dt_time.min, tzinfo=tz)
    end = datetime.combine(d, dt_time(23, 59, 59), tzinfo=tz)
    return start.timestamp(), end.timestamp()
