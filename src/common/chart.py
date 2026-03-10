"""Shared chart module — generates price structure PNG for playbook messages.

Produces a dark-themed candlestick chart with key levels and volume profile sidebar,
suitable for sending as a Telegram photo alongside the HTML playbook text.
"""

from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass, field

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from src.common.types import GammaWallResult, VolumeProfileResult

logger = logging.getLogger("common_chart")

# ── Dark theme colours ──
BG_COLOR = "#1a1a2e"
PANEL_COLOR = "#16213e"
TEXT_COLOR = "#e0e0e0"
GRID_COLOR = "#2a2a4a"
UP_COLOR = "#26a69a"
DOWN_COLOR = "#ef5350"
VOLUME_UP = "#26a69a80"
VOLUME_DOWN = "#ef535080"

# Key level colours & styles
LEVEL_STYLES: dict[str, dict] = {
    "POC":             {"color": "#ffd700", "ls": "-",  "lw": 1.2},
    "VAH":             {"color": "#87ceeb", "ls": "-",  "lw": 1.0},
    "VAL":             {"color": "#87ceeb", "ls": "-",  "lw": 1.0},
    "VWAP":            {"color": "#ff69b4", "ls": "--", "lw": 1.0},
    "PDH":             {"color": "#ffa500", "ls": ":",  "lw": 1.0},
    "PDL":             {"color": "#ffa500", "ls": ":",  "lw": 1.0},
    "PMH":             {"color": "#9370db", "ls": ":",  "lw": 1.0},
    "PML":             {"color": "#9370db", "ls": ":",  "lw": 1.0},
    "Gamma Call Wall": {"color": "#ff4444", "ls": "--", "lw": 1.5},
    "Gamma Put Wall":  {"color": "#44ff44", "ls": "--", "lw": 1.5},
}


@dataclass
class ChartData:
    """Input data for chart generation."""
    symbol: str
    today_bars: pd.DataFrame       # 1m OHLCV, datetime index
    volume_profile: VolumeProfileResult
    vwap: float
    last_price: float
    prev_close: float
    regime_label: str              # e.g. "TREND_DAY 72%"
    key_levels: dict[str, float] = field(default_factory=dict)
    gamma_wall: GammaWallResult | None = None


def generate_chart(data: ChartData) -> io.BytesIO | None:
    """Generate a price structure chart as PNG bytes in a BytesIO buffer.

    Returns None if today_bars is empty or has fewer than 5 rows.
    """
    bars = data.today_bars
    if bars is None or bars.empty or len(bars) < 5:
        return None

    try:
        return _render_chart(data)
    except Exception:
        logger.warning("Chart generation failed for %s", data.symbol, exc_info=True)
        return None


async def generate_chart_async(data: ChartData) -> io.BytesIO | None:
    """Async wrapper — runs generate_chart in a thread to avoid blocking."""
    return await asyncio.to_thread(generate_chart, data)


def _render_chart(data: ChartData) -> io.BytesIO | None:
    """Core rendering logic."""
    bars = data.today_bars.copy()

    # Ensure datetime index
    if not isinstance(bars.index, pd.DatetimeIndex):
        bars.index = pd.to_datetime(bars.index)

    # Normalise column names
    col_map = {}
    for needed, alts in [
        ("Open", ["open"]), ("High", ["high"]), ("Low", ["low"]),
        ("Close", ["close"]), ("Volume", ["volume"]),
    ]:
        if needed not in bars.columns:
            for alt in alts:
                if alt in bars.columns:
                    col_map[alt] = needed
                    break
    if col_map:
        bars = bars.rename(columns=col_map)

    for c in ("Open", "High", "Low", "Close", "Volume"):
        if c not in bars.columns:
            return None

    opens = bars["Open"].values.astype(float)
    highs = bars["High"].values.astype(float)
    lows = bars["Low"].values.astype(float)
    closes = bars["Close"].values.astype(float)
    volumes = bars["Volume"].values.astype(float)
    times = bars.index

    n = len(bars)

    # ── Figure layout ──
    fig = plt.figure(figsize=(12, 7), facecolor=BG_COLOR)
    # GridSpec: 2 rows (candle 75%, vol 25%) x 2 cols (main 85%, VP 15%)
    gs = fig.add_gridspec(
        2, 2,
        height_ratios=[3, 1],
        width_ratios=[85, 15],
        hspace=0.05, wspace=0.02,
    )
    ax_candle = fig.add_subplot(gs[0, 0])
    ax_vol = fig.add_subplot(gs[1, 0], sharex=ax_candle)
    ax_vp = fig.add_subplot(gs[0, 1], sharey=ax_candle)
    # Hide bottom-right cell
    ax_empty = fig.add_subplot(gs[1, 1])
    ax_empty.set_visible(False)

    for ax in (ax_candle, ax_vol, ax_vp):
        ax.set_facecolor(PANEL_COLOR)
        ax.tick_params(colors=TEXT_COLOR, labelsize=8)
        for spine in ax.spines.values():
            spine.set_color(GRID_COLOR)

    # ── Candlestick chart ──
    x = np.arange(n)
    width = 0.6

    up = closes >= opens
    down = ~up

    # Bodies
    ax_candle.bar(
        x[up], (closes - opens)[up], width, bottom=opens[up],
        color=UP_COLOR, edgecolor=UP_COLOR, linewidth=0.5,
    )
    ax_candle.bar(
        x[down], (opens - closes)[down], width, bottom=closes[down],
        color=DOWN_COLOR, edgecolor=DOWN_COLOR, linewidth=0.5,
    )
    # Wicks
    ax_candle.vlines(x[up], lows[up], highs[up], color=UP_COLOR, linewidth=0.6)
    ax_candle.vlines(x[down], lows[down], highs[down], color=DOWN_COLOR, linewidth=0.6)

    ax_candle.set_xlim(-1, n + 0.5)
    ax_candle.grid(True, alpha=0.15, color=GRID_COLOR)
    ax_candle.tick_params(axis="x", labelbottom=False)

    # Y-axis formatting
    price_range = highs.max() - lows.min()
    ax_candle.set_ylim(lows.min() - price_range * 0.05, highs.max() + price_range * 0.08)

    # ── Key levels on candle chart ──
    y_min, y_max = ax_candle.get_ylim()
    levels = data.key_levels.copy()
    # Ensure VP levels are always present
    vp = data.volume_profile
    if vp.poc > 0 and "POC" not in levels:
        levels["POC"] = vp.poc
    if vp.vah > 0 and "VAH" not in levels:
        levels["VAH"] = vp.vah
    if vp.val > 0 and "VAL" not in levels:
        levels["VAL"] = vp.val
    if data.vwap > 0 and "VWAP" not in levels:
        levels["VWAP"] = data.vwap

    for label, price in levels.items():
        if price <= 0:
            continue
        if not (y_min <= price <= y_max):
            continue
        style = LEVEL_STYLES.get(label, {"color": "#aaaaaa", "ls": "--", "lw": 0.8})
        ax_candle.axhline(
            price, **style, alpha=0.8, zorder=1,
        )
        ax_candle.text(
            n + 0.3, price, f" {label} {price:.2f}",
            color=style["color"], fontsize=7, va="center",
            fontweight="bold", alpha=0.9,
        )

    # ── Value Area shading ──
    if vp.vah > 0 and vp.val > 0:
        ax_candle.axhspan(vp.val, vp.vah, color="#87ceeb", alpha=0.06, zorder=0)

    # ── Title ──
    ax_candle.set_title(
        f"{data.symbol}  {data.regime_label}    Last: {data.last_price:.2f}",
        color=TEXT_COLOR, fontsize=11, fontweight="bold", loc="left", pad=8,
    )

    # ── Volume bars ──
    vol_colors = [VOLUME_UP if c >= o else VOLUME_DOWN for c, o in zip(closes, opens)]
    ax_vol.bar(x, volumes, width, color=vol_colors)
    ax_vol.set_xlim(-1, n + 0.5)
    ax_vol.grid(True, alpha=0.15, color=GRID_COLOR)
    ax_vol.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v/1e3:.0f}K" if v >= 1e3 else f"{v:.0f}"))

    # X-axis time labels
    tick_step = max(1, n // 8)
    tick_positions = list(range(0, n, tick_step))
    tick_labels = [times[i].strftime("%H:%M") for i in tick_positions]
    ax_vol.set_xticks(tick_positions)
    ax_vol.set_xticklabels(tick_labels, rotation=0, fontsize=7)

    # ── Volume Profile sidebar ──
    vbp = vp.volume_by_price
    if vbp:
        prices_vp = sorted(vbp.keys())
        vols_vp = [vbp[p] for p in prices_vp]
        max_vol = max(vols_vp) if vols_vp else 1

        # Normalise to 0-1 for horizontal bars
        norm_vols = [v / max_vol for v in vols_vp]

        # Bar height = price bin width (approximate from sorted prices)
        if len(prices_vp) >= 2:
            bar_h = (prices_vp[-1] - prices_vp[0]) / len(prices_vp) * 0.9
        else:
            bar_h = price_range * 0.01

        colors_vp = []
        for p in prices_vp:
            if vp.val <= p <= vp.vah:
                colors_vp.append("#87ceeb60")
            else:
                colors_vp.append("#ffffff30")

        ax_vp.barh(prices_vp, norm_vols, height=bar_h, color=colors_vp, align="center")

        # Mark POC
        if vp.poc > 0:
            ax_vp.axhline(vp.poc, color="#ffd700", ls="-", lw=1, alpha=0.8)
            ax_vp.text(0.5, vp.poc, "POC", color="#ffd700", fontsize=7,
                       ha="center", va="bottom", fontweight="bold")

    ax_vp.set_xlim(0, 1.2)
    ax_vp.tick_params(axis="x", labelbottom=False)
    ax_vp.tick_params(axis="y", labelleft=False)
    ax_vp.set_title("VP", color=TEXT_COLOR, fontsize=9, pad=4)

    # ── Export ──
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight",
                facecolor=BG_COLOR, edgecolor="none")
    plt.close(fig)
    buf.seek(0)
    return buf
