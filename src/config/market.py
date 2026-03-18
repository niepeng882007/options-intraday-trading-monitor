"""Market configuration for US and HK trading sessions.

Provides a frozen MarketConfig dataclass with pre-built US_CONFIG and HK_CONFIG
instances. Designed to replace hardcoded session times, timezone strings, and
path constants scattered across src/us_playbook/ and src/hk/.

Usage::

    from src.config.market import US_CONFIG, HK_CONFIG

    remaining = US_CONFIG.minutes_to_close(now)
    if HK_CONFIG.is_trading_time(now):
        ...
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class MarketConfig:
    """Immutable per-market trading configuration.

    Parameters
    ----------
    market_id : str
        Short identifier, e.g. ``"US"`` or ``"HK"``.
    trading_sessions : list[tuple[time, time]]
        Ordered list of (open, close) local-time pairs.
        Single-segment for US, two-segment for HK (lunch break).
    total_session_minutes : int
        Sum of all session durations in minutes.
    rvol_correction_window : tuple[time, time] | None
        Window where RVOL values should carry an "open-rotation" warning.
        ``None`` means no correction is needed (HK).
    rvol_skip_open_minutes : int
        Minutes to skip after the first session open when computing RVOL.
    benchmark_index : str
        Symbol used as broad-market context (``"SPY"`` / ``"HSI"``).
    option_chain_available : bool
        Whether the market normally has liquid option chains.
    va_lookback_days : int
        Default Volume-Profile lookback in trading days.
    timezone : str
        IANA timezone string (``"America/New_York"`` / ``"Asia/Hong_Kong"``).
    premarket_session : tuple[time, time] | None
        Pre-market window if applicable (US only).
    calendar_path : str
        Relative path to the macro-calendar YAML.
    settings_path : str
        Relative path to the market-specific settings YAML.
    """

    market_id: str
    trading_sessions: list[tuple[time, time]] = field(default_factory=list)
    total_session_minutes: int = 390
    rvol_correction_window: tuple[time, time] | None = None
    rvol_skip_open_minutes: int = 0
    benchmark_index: str = ""
    option_chain_available: bool = True
    va_lookback_days: int = 5
    timezone: str = "America/New_York"
    premarket_session: tuple[time, time] | None = None
    calendar_path: str = ""
    settings_path: str = ""

    # -- helpers ----------------------------------------------------------

    def tz_info(self) -> ZoneInfo:
        """Return the ``ZoneInfo`` object for this market's timezone."""
        return ZoneInfo(self.timezone)

    def session_close(self) -> time:
        """Return the close time of the last trading session."""
        if not self.trading_sessions:
            return time(16, 0)
        return self.trading_sessions[-1][1]

    def is_trading_time(self, now: datetime | None = None) -> bool:
        """Return ``True`` if *now* falls within any trading session.

        *now* is interpreted in the market's local timezone.  If *now* is
        naive, it is assumed to already represent local time.
        """
        if now is None:
            now = datetime.now(self.tz_info())
        elif now.tzinfo is not None:
            now = now.astimezone(self.tz_info())
        t = now.time()
        return any(start <= t < end for start, end in self.trading_sessions)

    def minutes_to_close(self, now: datetime | None = None) -> int:
        """Return remaining trading minutes, correctly handling lunch breaks.

        *now* is interpreted in the market's local timezone.  If *now* is
        naive, it is assumed to already represent local time.
        """
        if now is None:
            now = datetime.now(self.tz_info())
        elif now.tzinfo is not None:
            now = now.astimezone(self.tz_info())

        current = now.hour * 60 + now.minute

        # Before all sessions — full day remaining.
        first_open = self.trading_sessions[0][0]
        if current < first_open.hour * 60 + first_open.minute:
            return self.total_session_minutes

        remaining = 0
        for sess_open, sess_close in self.trading_sessions:
            open_min = sess_open.hour * 60 + sess_open.minute
            close_min = sess_close.hour * 60 + sess_close.minute
            if current >= close_min:
                # This session is over.
                continue
            if current < open_min:
                # Haven't reached this session yet — count it fully.
                remaining += close_min - open_min
            else:
                # Inside this session.
                remaining += close_min - current

        return max(remaining, 0)


# ── Pre-built instances ──────────────────────────────────────────────────

US_CONFIG = MarketConfig(
    market_id="US",
    trading_sessions=[(time(9, 30), time(16, 0))],
    total_session_minutes=390,
    rvol_correction_window=(time(9, 30), time(9, 45)),
    rvol_skip_open_minutes=3,
    benchmark_index="SPY",
    option_chain_available=True,
    va_lookback_days=5,
    timezone="America/New_York",
    premarket_session=(time(4, 0), time(9, 30)),
    calendar_path="config/us_calendar.yaml",
    settings_path="config/us_playbook_settings.yaml",
)

HK_CONFIG = MarketConfig(
    market_id="HK",
    trading_sessions=[(time(9, 30), time(12, 0)), (time(13, 0), time(16, 0))],
    total_session_minutes=330,
    rvol_correction_window=None,
    rvol_skip_open_minutes=0,
    benchmark_index="HSI",
    option_chain_available=True,
    va_lookback_days=5,
    timezone="Asia/Hong_Kong",
    premarket_session=None,
    calendar_path="config/hk_calendar.yaml",
    settings_path="config/hk_settings.yaml",
)
