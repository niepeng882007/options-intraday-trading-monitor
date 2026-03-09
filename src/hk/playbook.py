from __future__ import annotations

import html
from datetime import datetime, timezone, timedelta

from src.hk import (
    RegimeType, RegimeResult, VolumeProfileResult,
    GammaWallResult, FilterResult, Playbook,
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
        "\u2022 \u7b49\u5f85 10:05 / 13:05 Regime \u66f4\u65b0\n"
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
    )


def _confidence_bar(confidence: float) -> str:
    """Render a 5-block confidence bar."""
    filled = int(confidence * 5)
    return "\u2588" * filled + "\u2591" * (5 - filled)


def format_playbook_message(
    playbook: Playbook,
    symbol: str = "",
    update_type: str = "morning",
) -> str:
    """Format playbook as Telegram HTML message."""
    r = playbook.regime
    emoji = REGIME_EMOJI.get(r.regime, "\u2753")
    regime_cn = REGIME_NAME_CN.get(r.regime, "\u672a\u77e5")
    now = playbook.generated_at or datetime.now(HKT)

    # Update type label
    update_labels = {
        "morning": "\u65e9\u76d8 Playbook",
        "confirm": "10:05 \u786e\u8ba4\u66f4\u65b0",
        "afternoon": "\u5348\u540e Playbook",
        "alert": "\u76d8\u53e3\u5f02\u5e38\u544a\u8b66",
    }
    label = update_labels.get(update_type, "Playbook")

    lines = [
        f"{emoji} <b>\u3010{label}\u3011{symbol}</b>",
        "\u2501" * 20,
        "",
        "<b>\u4eca\u65e5\u5e02\u573a\u5b9a\u8c03</b>",
        f"  \u98ce\u683c: {emoji} {regime_cn}",
        f"  \u4fe1\u5fc3: {_confidence_bar(r.confidence)} {r.confidence:.0%}",
        f"  RVOL: {r.rvol:.2f}",
        f"  \u8be6\u60c5: {html.escape(r.details)}",
        "",
        "<b>\u5173\u952e\u70b9\u4f4d</b>",
    ]

    for name, val in sorted(playbook.key_levels.items(), key=lambda x: -x[1]):
        # Mark current price position relative to levels
        marker = ""
        if abs(val - r.price) / r.price < 0.002:
            marker = " \u2190 \u5f53\u524d"
        lines.append(f"  {name}: {val:,.2f}{marker}")

    lines.append(f"  \u5f53\u524d\u4ef7: {r.price:,.2f}")
    lines.append("")
    lines.append("<b>\u4ea4\u6613\u98ce\u683c\u5efa\u8bae</b>")
    lines.append(playbook.strategy_text)

    # Filters section
    f = playbook.filters
    lines.append("")
    lines.append("<b>\u4ea4\u6613\u8fc7\u6ee4\u72b6\u6001</b>")
    if not f.tradeable:
        lines.append("  \U0001f534 <b>\u4eca\u65e5\u4e0d\u5b9c\u4ea4\u6613</b>")
    elif f.risk_level == "high":
        lines.append("  \U0001f7e1 \u9ad8\u98ce\u9669\u65e5 \u2014 \u964d\u4f4e\u4ed3\u4f4d")
    elif f.risk_level == "elevated":
        lines.append("  \U0001f7e1 \u98ce\u9669\u504f\u9ad8 \u2014 \u6ce8\u610f\u63a7\u5236")
    else:
        lines.append("  \U0001f7e2 \u6b63\u5e38\u4ea4\u6613\u65e5")

    for w in f.warnings:
        lines.append(f"  \u26a0\ufe0f {html.escape(w)}")

    lines.append("")
    lines.append(f"\u23f1 {now.strftime('%H:%M:%S')} HKT")

    return "\n".join(lines)
