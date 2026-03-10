"""HK Playbook — generate and format aggregated playbook messages."""

from __future__ import annotations

import html
from datetime import datetime, timezone, timedelta

from src.hk import (
    RegimeType, RegimeResult, VolumeProfileResult,
    GammaWallResult, FilterResult, Playbook, OptionRecommendation,
)
from src.utils.logger import setup_logger

logger = setup_logger("hk_playbook")

HKT = timezone(timedelta(hours=8))

REGIME_EMOJI = {
    RegimeType.BREAKOUT: "\U0001f680",
    RegimeType.RANGE: "\U0001f4e6",
    RegimeType.WHIPSAW: "\U0001f30a",
    RegimeType.UNCLEAR: "\u2753",
}

REGIME_NAME_CN = {
    RegimeType.BREAKOUT: "\u5355\u8fb9\u7a81\u7834\u65e5",
    RegimeType.RANGE: "\u533a\u95f4\u9707\u8361\u65e5",
    RegimeType.WHIPSAW: "\u9ad8\u6ce2\u6d17\u76d8\u65e5",
    RegimeType.UNCLEAR: "\u4e0d\u660e\u786e\u65e5",
}

REGIME_STRATEGY = {
    RegimeType.BREAKOUT: (
        "\u52a8\u91cf\u98ce\u683c \u2014 \u987a\u52bf\u64cd\u4f5c\n"
        "\u2022 \u4e70\u5165 ATM \u6216\u8f7b\u5ea6 OTM \u671f\u6743 (Delta 0.3-0.5)\n"
        "\u2022 \u4ee5 VWAP \u4e3a\u9632\u5b88\u7ebf\n"
        "\u2022 \u987a\u52bf\u52a0\u4ed3\uff0c\u4e0d\u6284\u5e95/\u6478\u9876"
    ),
    RegimeType.RANGE: (
        "\u5747\u503c\u56de\u5f52\u98ce\u683c \u2014 \u9ad8\u629b\u4f4e\u5438\n"
        "\u2022 \u4e25\u7981\u4e70\u5165\u865a\u503c\u671f\u6743\n"
        "\u2022 \u4e70\u5165\u6df1\u5ea6 ITM \u671f\u6743 (Delta > 0.7)\n"
        "\u2022 \u5728 VAH \u9644\u8fd1\u505a\u7a7a\uff0cVAL \u9644\u8fd1\u505a\u591a\n"
        "\u2022 \u5feb\u8fdb\u5feb\u51fa\uff0c\u4e0d\u604b\u6218"
    ),
    RegimeType.WHIPSAW: (
        "\u53f3\u4fa7\u786e\u8ba4\u98ce\u683c \u2014 \u7b49\u5f85\u786e\u8ba4\n"
        "\u2022 \u964d\u4f4e\u4ed3\u4f4d\u81f3\u6b63\u5e38\u7684 50%\n"
        "\u2022 \u7b49\u5f85\u5e26\u91cf\u7a81\u7834\u540e\u56de\u8e29\u786e\u8ba4\n"
        "\u2022 \u907f\u514d\u5728 Gamma \u5899\u9644\u8fd1\u5f00\u4ed3"
    ),
    RegimeType.UNCLEAR: (
        "\u89c2\u671b\u4e3a\u4e3b \u2014 \u964d\u4f4e\u4ed3\u4f4d\n"
        "\u2022 \u4ed3\u4f4d\u964d\u81f3\u6b63\u5e38\u7684 30%\n"
        "\u2022 \u7b49\u5f85 Regime \u66f4\u65b0\n"
        "\u2022 \u4ec5\u53c2\u4e0e\u9ad8\u786e\u5b9a\u6027\u673a\u4f1a"
    ),
}


def generate_playbook(
    regime: RegimeResult,
    vp: VolumeProfileResult,
    vwap: float,
    gamma_wall: GammaWallResult | None = None,
    filters: FilterResult | None = None,
    symbol: str = "",
    update_type: str = "morning",
    option_rec: OptionRecommendation | None = None,
) -> Playbook:
    """Generate a complete Playbook object."""
    if filters is None:
        filters = FilterResult(tradeable=True)

    key_levels = {
        "POC": vp.poc,
        "VAH": vp.vah,
        "VAL": vp.val,
        "VWAP": vwap,
    }
    if gamma_wall:
        if gamma_wall.call_wall_strike > 0:
            key_levels["Gamma Call Wall"] = gamma_wall.call_wall_strike
        if gamma_wall.put_wall_strike > 0:
            key_levels["Gamma Put Wall"] = gamma_wall.put_wall_strike
        if gamma_wall.max_pain > 0:
            key_levels["Max Pain"] = gamma_wall.max_pain

    strategy_text = REGIME_STRATEGY.get(regime.regime, "")

    return Playbook(
        regime=regime,
        volume_profile=vp,
        gamma_wall=gamma_wall,
        filters=filters,
        vwap=vwap,
        key_levels=key_levels,
        strategy_text=strategy_text,
        generated_at=datetime.now(HKT),
        option_rec=option_rec,
    )


def _confidence_bar(confidence: float) -> str:
    """Render a 5-block confidence bar."""
    filled = int(confidence * 5)
    return "\u2588" * filled + "\u2591" * (5 - filled)


def _price_position(price: float, vp: VolumeProfileResult, vwap: float) -> str:
    """Describe price position relative to VA and VWAP."""
    parts = []
    if price > vp.vah:
        parts.append("VAH \u4e0a\u65b9")
    elif price < vp.val:
        parts.append("VAL \u4e0b\u65b9")
    else:
        parts.append("VA \u5185\u90e8")

    if vwap > 0:
        if price > vwap:
            parts.append("VWAP \u4e0a\u65b9")
        else:
            parts.append("VWAP \u4e0b\u65b9")

    return "\u4ef7\u683c\u4f4d\u4e8e " + ", ".join(parts)


def format_playbook_message(
    playbook: Playbook,
    symbol: str = "",
    update_type: str = "manual",
) -> str:
    """Format playbook as Telegram HTML message — 5-section aggregated output."""
    _esc = html.escape
    r = playbook.regime
    emoji = REGIME_EMOJI.get(r.regime, "\u2753")
    regime_cn = REGIME_NAME_CN.get(r.regime, "\u672a\u77e5")
    now = playbook.generated_at or datetime.now(HKT)

    lines: list[str] = []
    sep = "\u2501" * 20

    # (1) Header
    lines.append(sep)
    lines.append(f"<b>{_esc(symbol)}</b>")
    lines.append(f"{now.strftime('%Y-%m-%d %H:%M:%S')} HKT")
    lines.append("")

    # (2) Market regime
    lines.append(f"{emoji} <b>\u5e02\u573a\u5b9a\u8c03</b>")
    lines.append(f"  Regime: {regime_cn}")
    lines.append(f"  \u7f6e\u4fe1\u5ea6: {_confidence_bar(r.confidence)} {r.confidence:.0%}")
    lines.append(f"  \u89e3\u8bfb: {_esc(r.details)}")
    lines.append("")

    # (3) Real-time data
    lines.append("\U0001f4ca <b>\u5b9e\u65f6\u6570\u636e\u652f\u6491</b>")
    lines.append(f"  \u5f53\u524d\u4ef7: {r.price:,.2f}")

    vwap = playbook.vwap
    if vwap > 0:
        vwap_pct = (r.price - vwap) / vwap * 100
        lines.append(f"  VWAP: {vwap:,.2f} ({vwap_pct:+.2f}%)")

    lines.append(f"  RVOL: {r.rvol:.2f}")

    vp = playbook.volume_profile
    lines.append(f"  POC: {vp.poc:,.2f} | VAH: {vp.vah:,.2f} | VAL: {vp.val:,.2f}")

    gw = playbook.gamma_wall
    if gw and (gw.call_wall_strike > 0 or gw.put_wall_strike > 0):
        gw_parts = []
        if gw.call_wall_strike > 0:
            gw_parts.append(f"Call {gw.call_wall_strike:,.0f}")
        if gw.put_wall_strike > 0:
            gw_parts.append(f"Put {gw.put_wall_strike:,.0f}")
        if gw.max_pain > 0:
            gw_parts.append(f"Max Pain {gw.max_pain:,.0f}")
        lines.append(f"  Gamma Wall: {' | '.join(gw_parts)}")

    lines.append(f"  {_price_position(r.price, vp, vwap)}")
    lines.append("")

    # (4) Option recommendation
    rec = playbook.option_rec
    if rec:
        lines.append("\U0001f3af <b>\u671f\u6743\u64cd\u4f5c\u5efa\u8bae</b>")
        if rec.action == "wait":
            lines.append("  \u5efa\u8bae: \u26d4 <b>\u89c2\u671b</b>")
            if rec.risk_note:
                for reason in rec.risk_note.split("\n"):
                    lines.append(f"  {_esc(reason)}")
            if rec.wait_conditions:
                lines.append("  \u91cd\u65b0\u8bc4\u4f30\u6761\u4ef6:")
                for cond in rec.wait_conditions:
                    lines.append(f"    \u2022 {_esc(cond)}")
        else:
            action_cn = {
                "call": "\u2191 \u4e70\u5165 Call",
                "put": "\u2193 \u4e70\u5165 Put",
                "bull_put_spread": "\u2191 Bull Put Spread",
                "bear_call_spread": "\u2193 Bear Call Spread",
            }
            lines.append(f"  \u5efa\u8bae: <b>{action_cn.get(rec.action, rec.action)}</b>")

            if rec.expiry:
                lines.append(f"  \u5230\u671f\u65e5: {rec.expiry}")

            for leg in rec.legs:
                side_cn = "\u4e70\u5165" if leg.side == "buy" else "\u5356\u51fa"
                lines.append(
                    f"  {side_cn} {leg.option_type.upper()} "
                    f"Strike {leg.strike:,.0f} ({leg.moneyness})"
                )

            if rec.rationale:
                lines.append(f"  \u7406\u7531: {_esc(rec.rationale)}")

            if rec.liquidity_warning:
                lines.append(f"  \u26a0\ufe0f {_esc(rec.liquidity_warning)}")
    else:
        # Fallback to generic strategy text
        lines.append("\U0001f3af <b>\u4ea4\u6613\u98ce\u683c\u5efa\u8bae</b>")
        lines.append(playbook.strategy_text)

    lines.append("")

    # (5) Risk / filters
    lines.append("\u26a0\ufe0f <b>\u98ce\u9669\u8bf4\u660e</b>")
    f = playbook.filters
    if not f.tradeable:
        lines.append("  \U0001f534 <b>\u4eca\u65e5\u4e0d\u5b9c\u4ea4\u6613</b>")
    elif f.risk_level == "high":
        lines.append("  \U0001f7e1 \u9ad8\u98ce\u9669\u65e5 \u2014 \u964d\u4f4e\u4ed3\u4f4d")
    elif f.risk_level == "elevated":
        lines.append("  \U0001f7e1 \u98ce\u9669\u504f\u9ad8 \u2014 \u6ce8\u610f\u63a7\u5236")
    else:
        lines.append("  \U0001f7e2 \u6b63\u5e38\u4ea4\u6613\u65e5")

    for w in f.warnings:
        lines.append(f"  \u26a0\ufe0f {_esc(w)}")

    if rec and rec.action != "wait" and rec.risk_note:
        lines.append(f"  {_esc(rec.risk_note)}")

    lines.append(sep)

    return "\n".join(lines)
