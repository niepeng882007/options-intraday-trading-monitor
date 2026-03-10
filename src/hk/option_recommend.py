"""HK Option Recommendation Engine — direction, strike, expiry selection."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd

from src.hk import (
    FilterResult,
    GammaWallResult,
    OptionLeg,
    OptionRecommendation,
    RegimeResult,
    RegimeType,
    VolumeProfileResult,
)
from src.utils.logger import setup_logger

logger = setup_logger("hk_option_rec")

MIN_OI = 50
MAX_SPREAD_PCT = 0.05  # 5% of underlying price


# ── Expiry selection ──


def select_expiry(expiry_dates: list[dict], today: date | None = None) -> str | None:
    """Select best expiry from available dates.

    Rules:
    - Filter out DTE=0 (same day)
    - Prefer nearest DTE >= 7 (weekly)
    - Fallback to nearest monthly
    - None if nothing available
    """
    if today is None:
        today = date.today()

    candidates = []
    for row in expiry_dates:
        exp_str = row.get("strike_time", "")
        if not exp_str:
            continue
        try:
            exp_date = datetime.strptime(exp_str[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        dte = (exp_date - today).days
        if dte <= 0:
            continue
        candidates.append((exp_date, dte, exp_str[:10]))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[1])

    # Prefer weekly (DTE >= 1)
    for exp_date, dte, exp_str in candidates:
        if dte >= 1:
            return exp_str

    return None


# ── Moneyness classification ──


def classify_moneyness(strike: float, price: float, option_type: str) -> str:
    """Classify a strike as ATM / OTM X% / ITM X%."""
    if price == 0:
        return "N/A"
    pct = abs(strike - price) / price * 100
    if pct < 0.5:
        return "ATM"
    if option_type.lower() == "call":
        if strike > price:
            return f"OTM {pct:.1f}%"
        return f"ITM {pct:.1f}%"
    else:  # put
        if strike < price:
            return f"OTM {pct:.1f}%"
        return f"ITM {pct:.1f}%"


# ── Direction decision ──


def _decide_direction(
    regime: RegimeResult,
    vp: VolumeProfileResult,
) -> str:
    """Decide bullish / bearish / neutral based on regime + price position."""
    price = regime.price

    if regime.regime == RegimeType.BREAKOUT:
        if price > vp.vah:
            return "bullish"
        if price < vp.val:
            return "bearish"
        return "bullish" if price > vp.poc else "bearish"

    if regime.regime == RegimeType.RANGE:
        # Mean reversion: near VAH → bearish, near VAL → bullish
        if vp.vah > vp.val:
            mid = (vp.vah + vp.val) / 2
            return "bearish" if price > mid else "bullish"
        return "neutral"

    return "neutral"  # WHIPSAW / UNCLEAR


# ── Wait decision ──


def should_wait(
    regime: RegimeResult,
    filters: FilterResult,
    vp: VolumeProfileResult,
    chain_available: bool,
    expiry_available: bool,
) -> tuple[bool, list[str], list[str]]:
    """Determine if we should wait instead of trade.

    Returns: (should_wait, reasons, wait_conditions)
    """
    reasons: list[str] = []
    conditions: list[str] = []

    if not filters.tradeable:
        reasons.append("过滤器标记为不宜交易")
        for w in filters.warnings:
            reasons.append(f"  - {w}")

    if regime.regime == RegimeType.UNCLEAR and regime.confidence < 0.4:
        reasons.append(f"Regime UNCLEAR, 置信度仅 {regime.confidence:.0%}")
        conditions.append("等待 Regime 更新为 BREAKOUT 或 RANGE")

    if regime.regime == RegimeType.WHIPSAW:
        reasons.append("高波洗盘日, 方向不明确")
        conditions.append("等待带量突破确认方向")

    if regime.rvol < 0.5:
        reasons.append(f"RVOL {regime.rvol:.2f} 极低, 量能不足")
        conditions.append(f"RVOL 回升至 0.8 以上")

    if not chain_available or not expiry_available:
        reasons.append("无可用期权链或到期日")
        conditions.append("检查标的是否有期权合约")

    # Price too close to POC — no edge
    if vp.poc > 0 and regime.price > 0:
        dist_to_poc = abs(regime.price - vp.poc) / vp.poc
        if dist_to_poc < 0.003 and regime.regime != RegimeType.BREAKOUT:
            reasons.append(f"价格距 POC 仅 {dist_to_poc:.1%}, 无方向性优势")
            conditions.append(f"价格突破 VAH {vp.vah:,.2f} 或跌破 VAL {vp.val:,.2f}")

    return bool(reasons), reasons, conditions


# ── Single leg recommendation ──


def recommend_single_leg(
    direction: str,
    chain_df: pd.DataFrame,
    price: float,
    expiry: str,
) -> OptionLeg | None:
    """Pick best single-leg option.

    Call: ATM or slightly OTM (strike >= price, prefer delta 0.3-0.5)
    Put:  ATM or slightly OTM (strike <= price, prefer delta -0.3 to -0.5)
    """
    if chain_df.empty:
        return None

    opt_type = "CALL" if direction == "bullish" else "PUT"
    expiry_match = chain_df["strike_time"].astype(str).str[:10] == expiry
    type_match = chain_df["option_type"].str.upper() == opt_type
    candidates = chain_df[expiry_match & type_match].copy()

    if candidates.empty:
        return None

    # Filter by OI
    if "open_interest" in candidates.columns:
        candidates = candidates[candidates["open_interest"] >= 10]
    if candidates.empty:
        return None

    # Prefer ATM/slightly OTM
    candidates = candidates.copy()
    candidates["dist"] = abs(candidates["strike_price"] - price)
    candidates = candidates.sort_values("dist")

    # Prefer delta 0.3-0.5 range if available
    if "delta" in candidates.columns:
        abs_delta = candidates["delta"].abs()
        good_delta = candidates[(abs_delta >= 0.3) & (abs_delta <= 0.5)]
        if not good_delta.empty:
            candidates = good_delta.sort_values("dist")

    best = candidates.iloc[0]
    strike = float(best["strike_price"])
    pct_from = (strike - price) / price * 100

    return OptionLeg(
        side="buy",
        option_type=opt_type.lower(),
        strike=strike,
        pct_from_price=pct_from,
        moneyness=classify_moneyness(strike, price, opt_type),
    )


# ── Spread recommendation ──


def recommend_spread(
    direction: str,
    chain_df: pd.DataFrame,
    price: float,
    expiry: str,
) -> list[OptionLeg] | None:
    """Build vertical spread (2 legs).

    Bullish (Bull Put Spread): sell higher Put + buy lower Put
    Bearish (Bear Call Spread): sell lower Call + buy higher Call
    """
    if chain_df.empty:
        return None

    expiry_match = chain_df["strike_time"].astype(str).str[:10] == expiry

    if direction == "bullish":
        puts = chain_df[expiry_match & (chain_df["option_type"].str.upper() == "PUT")].copy()
        if "open_interest" in puts.columns:
            puts = puts[puts["open_interest"] >= MIN_OI]
        if len(puts) < 2:
            return None
        puts = puts.sort_values("strike_price", ascending=False)
        # Sell ATM/slightly OTM put, buy further OTM put
        atm_puts = puts[puts["strike_price"] <= price]
        if len(atm_puts) < 2:
            return None
        sell_leg = atm_puts.iloc[0]
        buy_leg = atm_puts.iloc[1]
        return [
            OptionLeg(
                side="sell", option_type="put",
                strike=float(sell_leg["strike_price"]),
                pct_from_price=(float(sell_leg["strike_price"]) - price) / price * 100,
                moneyness=classify_moneyness(float(sell_leg["strike_price"]), price, "PUT"),
            ),
            OptionLeg(
                side="buy", option_type="put",
                strike=float(buy_leg["strike_price"]),
                pct_from_price=(float(buy_leg["strike_price"]) - price) / price * 100,
                moneyness=classify_moneyness(float(buy_leg["strike_price"]), price, "PUT"),
            ),
        ]
    else:  # bearish
        calls = chain_df[expiry_match & (chain_df["option_type"].str.upper() == "CALL")].copy()
        if "open_interest" in calls.columns:
            calls = calls[calls["open_interest"] >= MIN_OI]
        if len(calls) < 2:
            return None
        calls = calls.sort_values("strike_price")
        atm_calls = calls[calls["strike_price"] >= price]
        if len(atm_calls) < 2:
            return None
        sell_leg = atm_calls.iloc[0]
        buy_leg = atm_calls.iloc[1]
        return [
            OptionLeg(
                side="sell", option_type="call",
                strike=float(sell_leg["strike_price"]),
                pct_from_price=(float(sell_leg["strike_price"]) - price) / price * 100,
                moneyness=classify_moneyness(float(sell_leg["strike_price"]), price, "CALL"),
            ),
            OptionLeg(
                side="buy", option_type="call",
                strike=float(buy_leg["strike_price"]),
                pct_from_price=(float(buy_leg["strike_price"]) - price) / price * 100,
                moneyness=classify_moneyness(float(buy_leg["strike_price"]), price, "CALL"),
            ),
        ]


# ── Liquidity check ──


def _check_liquidity(chain_df: pd.DataFrame, price: float) -> str | None:
    """Return liquidity warning or None."""
    if chain_df.empty:
        return "期权链为空, 无法评估流动性"

    warnings = []
    if "open_interest" in chain_df.columns:
        max_oi = chain_df["open_interest"].max()
        if max_oi < MIN_OI:
            warnings.append(f"最大 OI 仅 {max_oi}, 流动性极差")

    if "last_price" in chain_df.columns and price > 0:
        # Check bid-ask spread via proxy (not always available)
        pass  # bid/ask not in chain_df; skip

    return "; ".join(warnings) if warnings else None


# ── Main entry point ──


def recommend(
    regime: RegimeResult,
    vp: VolumeProfileResult,
    filters: FilterResult,
    chain_df: pd.DataFrame | None = None,
    expiry_dates: list[dict] | None = None,
    gamma_wall: GammaWallResult | None = None,
) -> OptionRecommendation:
    """Generate option recommendation based on regime, levels, and chain data."""
    price = regime.price
    has_chain = chain_df is not None and not chain_df.empty
    has_expiry = bool(expiry_dates)

    # Direction first — needed for degraded recommendations
    direction = _decide_direction(regime, vp)

    # Check regime/filter-based wait conditions first.
    # Chain/expiry availability is handled explicitly below (lines 357+)
    # to preserve direction hint in the wait recommendation.
    wait, reasons, conditions = should_wait(
        regime, filters, vp,
        chain_available=True,
        expiry_available=True,
    )
    if wait:
        return OptionRecommendation(
            action="wait",
            direction="neutral",
            rationale="观望",
            risk_note="\n".join(reasons),
            wait_conditions=conditions,
        )

    if direction == "neutral":
        return OptionRecommendation(
            action="wait",
            direction="neutral",
            rationale="方向不明确, 建议观望",
            risk_note="Regime 未给出明确方向",
            wait_conditions=["等待价格突破关键位后再入场"],
        )

    # Expiry
    expiry = select_expiry(expiry_dates or [])

    # No expiry or no chain → wait (recommendations require specific strike + expiry)
    if not expiry or not has_chain:
        wait_reasons = []
        wait_conds = []
        if not expiry:
            wait_reasons.append("无可用非末日到期日")
            wait_conds.append("等待新的期权合约上市")
        if not has_chain:
            wait_reasons.append("期权链数据不可用")
            wait_conds.append("检查标的是否有期权合约")
        dir_hint = "看多" if direction == "bullish" else "看空"
        return OptionRecommendation(
            action="wait",
            direction=direction,
            rationale=f"方向偏{dir_hint}, 但缺少可交易的期权合约",
            risk_note="; ".join(wait_reasons),
            wait_conditions=wait_conds,
            liquidity_warning="该标的期权链不可用或流动性不足",
        )

    # Liquidity check
    liq_warn = _check_liquidity(chain_df, price)

    # Try spread for RANGE regime with high IV
    if regime.regime == RegimeType.RANGE:
        spread_legs = recommend_spread(direction, chain_df, price, expiry)
        if spread_legs:
            spread_action = "bull_put_spread" if direction == "bullish" else "bear_call_spread"
            return OptionRecommendation(
                action=spread_action,
                direction=direction,
                expiry=expiry,
                legs=spread_legs,
                moneyness=spread_legs[0].moneyness,
                rationale=_build_rationale(regime, vp, direction, spread=True),
                risk_note=_build_risk_note(regime, vp, direction),
                liquidity_warning=liq_warn,
            )

    # Single leg
    leg = recommend_single_leg(direction, chain_df, price, expiry)
    if leg:
        action = "call" if direction == "bullish" else "put"
        return OptionRecommendation(
            action=action,
            direction=direction,
            expiry=expiry,
            legs=[leg],
            moneyness=leg.moneyness,
            rationale=_build_rationale(regime, vp, direction),
            risk_note=_build_risk_note(regime, vp, direction),
            liquidity_warning=liq_warn,
        )

    # No suitable strike found → wait
    dir_hint = "看多" if direction == "bullish" else "看空"
    return OptionRecommendation(
        action="wait",
        direction=direction,
        rationale=f"方向偏{dir_hint}, 但未找到合适的行权价",
        risk_note="期权链中无满足 OI/delta 条件的合约",
        wait_conditions=["等待期权链流动性改善后重新评估"],
        liquidity_warning=liq_warn or "期权链流动性不足",
    )


def _build_rationale(
    regime: RegimeResult,
    vp: VolumeProfileResult,
    direction: str,
    spread: bool = False,
) -> str:
    """Build human-readable rationale."""
    parts = []

    regime_names = {
        RegimeType.BREAKOUT: "单边突破",
        RegimeType.RANGE: "区间震荡",
        RegimeType.WHIPSAW: "高波洗盘",
        RegimeType.UNCLEAR: "不明确",
    }
    parts.append(f"Regime: {regime_names.get(regime.regime, '未知')}")

    if regime.regime == RegimeType.BREAKOUT:
        if direction == "bullish":
            parts.append(f"价格 {regime.price:,.2f} 突破 VAH {vp.vah:,.2f}")
        else:
            parts.append(f"价格 {regime.price:,.2f} 跌破 VAL {vp.val:,.2f}")
        parts.append(f"RVOL {regime.rvol:.2f} 量能配合")

    elif regime.regime == RegimeType.RANGE:
        if direction == "bullish":
            parts.append(f"价格 {regime.price:,.2f} 靠近 VAL {vp.val:,.2f}, 低吸机会")
        else:
            parts.append(f"价格 {regime.price:,.2f} 靠近 VAH {vp.vah:,.2f}, 高抛机会")
        if spread:
            parts.append("震荡市适合使用价差策略, 利用时间价值衰减")

    return "; ".join(parts)


def _build_risk_note(regime: RegimeResult, vp: VolumeProfileResult, direction: str) -> str:
    """Build risk note / defense line."""
    parts = []
    if regime.regime == RegimeType.BREAKOUT:
        parts.append(f"防守线: VWAP, 跌破建议止损")
        parts.append(f"失效条件: RVOL 回落至 1.0 以下")
    elif regime.regime == RegimeType.RANGE:
        if direction == "bullish":
            parts.append(f"止损: 跌破 VAL {vp.val:,.2f}")
        else:
            parts.append(f"止损: 突破 VAH {vp.vah:,.2f}")
        parts.append("失效条件: 带量突破 VA 边界转为 BREAKOUT")
    return "; ".join(parts)
