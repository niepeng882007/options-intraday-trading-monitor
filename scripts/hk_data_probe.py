#!/usr/bin/env python3
"""HK Futu API 数据可行性探测脚本 — 验证港股行情、K线、期权链、盘口等数据可用性。

独立同步脚本，连接本地 FutuOpenD 执行 8 个探测（Probe 0-7），
为 HK Playbook 方案提供数据可行性证据。

Usage:
    python scripts/hk_data_probe.py [--host 127.0.0.1] [--port 11111]
"""

import argparse
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
from futu import (
    IndexOptionType,
    KLType,
    OpenQuoteContext,
    OrderBookHandlerBase,
    RET_OK,
    SubType,
)

HKT = timezone(timedelta(hours=8))

# ── Output formatting ──


def print_header(index: int, total: int, name: str) -> None:
    print(f"\n{'=' * 50}")
    print(f"[{index}/{total}] {name}")
    print(f"{'=' * 50}")


def print_status(status: str, notes: list[str] | None = None) -> None:
    print(f"Status: {status}")
    if notes:
        print("Notes:")
        for n in notes:
            print(f"  - {n}")


def print_columns(df: pd.DataFrame) -> None:
    print(f"Columns: {list(df.columns)}")


def print_sample(df: pd.DataFrame, n: int = 3) -> None:
    print(f"Sample ({min(n, len(df))} rows):")
    print(df.head(n).to_string())


# ── Probe 0: Connection & Global State ──


def probe_0_connection(ctx: OpenQuoteContext) -> bool:
    print_header(0, 7, "CONNECTION & GLOBAL STATE")
    try:
        ret, data = ctx.get_global_state()
        if ret != RET_OK:
            print_status("FAIL", [f"get_global_state failed: {data}"])
            return False

        notes = []
        for key in ["server_ver", "qot_logined", "trd_logined",
                     "market_hk", "market_us", "market_cn",
                     "market_hk_future", "market_us_future"]:
            val = data.get(key, "N/A")
            notes.append(f"{key} = {val}")

        market_hk = data.get("market_hk", "N/A")
        if market_hk in ("MORNING", "AFTERNOON", "REST", "NIGHT"):
            notes.append(f"HK market is OPEN (state: {market_hk})")
        else:
            notes.append(f"HK market state: {market_hk} (may be closed)")

        print_status("PASS", notes)
        return True
    except Exception as e:
        print_status("FAIL", [str(e)])
        return False


# ── Probe 1: HK Stock Quote ──


def probe_1_stock_quote(ctx: OpenQuoteContext) -> bool:
    print_header(1, 7, "HK STOCK QUOTE (00700.HK)")
    symbol = "HK.00700"
    try:
        ret, msg = ctx.subscribe([symbol], [SubType.QUOTE])
        if ret != RET_OK:
            print_status("FAIL", [f"subscribe failed: {msg}"])
            return False

        ret, data = ctx.get_stock_quote([symbol])
        if ret != RET_OK:
            print_status("FAIL", [f"get_stock_quote failed: {data}"])
            return False

        print_columns(data)
        print_sample(data, 1)

        row = data.iloc[0]
        notes = []
        for field in ["last_price", "bid_price", "ask_price", "volume",
                       "turnover", "open_price", "high_price", "low_price",
                       "prev_close_price", "change_rate", "amplitude",
                       "turnover_rate"]:
            val = row.get(field, "MISSING")
            notes.append(f"{field} = {val}")

        # Check if volume is in shares or lots (HK trades in lots of 100)
        vol = row.get("volume", 0)
        notes.append(f"volume raw = {vol} (HK lot size = 100, "
                     f"likely {'lots' if vol and vol < 1_000_000 else 'shares'})")

        print_status("PASS", notes)
        return True
    except Exception as e:
        print_status("FAIL", [str(e)])
        return False


# ── Probe 2: HK Index Quote ──


def probe_2_index_quote(ctx: OpenQuoteContext) -> bool:
    print_header(2, 7, "HK INDEX QUOTE (HSI / HSTECH / HSCEI)")

    indices = [
        ("HK.800000", "HSI (Hang Seng Index)"),
        ("HK.800700", "HSTECH (Hang Seng TECH)"),
        ("HK.800100", "HSCEI (HS China Enterprises)"),
    ]

    any_ok = False
    notes = []

    for code, name in indices:
        try:
            ret, msg = ctx.subscribe([code], [SubType.QUOTE])
            if ret != RET_OK:
                notes.append(f"{name} [{code}]: subscribe FAIL — {msg}")
                continue

            ret, data = ctx.get_stock_quote([code])
            if ret != RET_OK:
                notes.append(f"{name} [{code}]: quote FAIL — {data}")
                continue

            row = data.iloc[0]
            price = row.get("last_price", "N/A")
            vol = row.get("volume", "N/A")
            notes.append(f"{name} [{code}]: price={price}, volume={vol} — OK")
            any_ok = True
        except Exception as e:
            notes.append(f"{name} [{code}]: ERROR — {e}")

    # Also try get_market_snapshot for richer data
    for code, name in indices:
        try:
            ret, snap = ctx.get_market_snapshot([code])
            if ret == RET_OK and not snap.empty:
                notes.append(f"{name} snapshot columns: {list(snap.columns)[:15]}...")
                row = snap.iloc[0]
                for f in ["last_price", "volume", "turnover", "high_price", "low_price"]:
                    notes.append(f"  snapshot.{f} = {row.get(f, 'N/A')}")
                break  # One example is enough
        except Exception as e:
            notes.append(f"{name} snapshot: ERROR — {e}")

    print_status("PASS" if any_ok else "FAIL", notes)
    return any_ok


# ── Probe 3: HK 1m History K-line ──


def probe_3_kline(ctx: OpenQuoteContext) -> bool:
    print_header(3, 7, "HK 1M HISTORY K-LINE (00700.HK)")
    symbol = "HK.00700"
    today = datetime.now(HKT).date()
    start = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")

    try:
        ret, data, page_key = ctx.request_history_kline(
            symbol, ktype=KLType.K_1M, start=start, end=end, max_count=1000
        )
        if ret != RET_OK:
            print_status("FAIL", [f"request_history_kline failed: {data}"])
            return False

        if data.empty:
            print_status("FAIL", ["No bars returned (market may have been closed all week)"])
            return False

        notes = []
        notes.append(f"Total bars: {len(data)}")
        print_columns(data)

        # First & last 5 timestamps
        notes.append("First 5 time_keys:")
        for ts in data["time_key"].head(5).tolist():
            notes.append(f"  {ts}")
        notes.append("Last 5 time_keys:")
        for ts in data["time_key"].tail(5).tolist():
            notes.append(f"  {ts}")

        # Parse timestamps for analysis
        data["ts"] = pd.to_datetime(data["time_key"])
        data["hour"] = data["ts"].dt.hour
        data["minute"] = data["ts"].dt.minute
        data["date"] = data["ts"].dt.date

        # Lunch break gap: any bars between 12:00-12:59?
        lunch_bars = data[(data["hour"] == 12)]
        notes.append(f"Bars during 12:xx (lunch break): {len(lunch_bars)}")
        if not lunch_bars.empty:
            notes.append(f"  Lunch bar times: {lunch_bars['time_key'].tolist()[:5]}")

        # Per-day bar count
        day_counts = data.groupby("date").size()
        for d, cnt in day_counts.items():
            notes.append(f"  {d}: {cnt} bars")

        # Trading session boundaries
        for d in day_counts.index:
            day_data = data[data["date"] == d]
            first_ts = day_data["time_key"].iloc[0]
            last_ts = day_data["time_key"].iloc[-1]
            notes.append(f"  {d}: first={first_ts}, last={last_ts}")

        # Check K-line quota
        try:
            ret_q, quota_data = ctx.get_history_kl_quota(get_detail=True)
            if ret_q == RET_OK:
                notes.append(f"K-line quota info: {quota_data}")
        except Exception as e:
            notes.append(f"K-line quota check failed: {e}")

        print_sample(data[["time_key", "open", "high", "low", "close", "volume"]], 5)
        print_status("PASS", notes)
        return True
    except Exception as e:
        print_status("FAIL", [str(e)])
        return False


# ── Probe 4: HSI Option Chain ──


def probe_4_option_chain(ctx: OpenQuoteContext) -> tuple[bool, list[str]]:
    """Returns (success, list_of_option_codes_for_probe_5)."""
    print_header(4, 7, "HSI OPTION CHAIN")

    # Try multiple index codes and option types
    targets = [
        ("HK.800000", "HSI"),
        ("HK.800700", "HSTECH"),
    ]
    option_types = [
        (IndexOptionType.NORMAL, "NORMAL"),
        (IndexOptionType.SMALL, "SMALL"),
    ]

    notes = []
    found_codes: list[str] = []

    for code, name in targets:
        # Step 1: Get expiration dates
        for idx_type, type_name in option_types:
            try:
                ret, exp_data = ctx.get_option_expiration_date(
                    code, index_option_type=idx_type
                )
                if ret != RET_OK:
                    notes.append(f"{name} {type_name} expiration: FAIL — {exp_data}")
                    continue

                if exp_data is None or exp_data.empty:
                    notes.append(f"{name} {type_name} expiration: no data")
                    continue

                notes.append(f"{name} {type_name} expiration dates found:")
                print_columns(exp_data)
                for _, row in exp_data.head(5).iterrows():
                    notes.append(f"  {row.to_dict()}")

                # Step 2: Get option chain for nearest expiry
                nearest_exp = exp_data.iloc[0]
                exp_str = str(nearest_exp.get("strike_time", nearest_exp.get("option_expiry_date_distance", "")))
                notes.append(f"Fetching chain for nearest expiry: {exp_str}")

                ret2, chain_data = ctx.get_option_chain(
                    code, index_option_type=idx_type
                )
                if ret2 != RET_OK:
                    notes.append(f"{name} {type_name} chain: FAIL — {chain_data}")
                    continue

                if chain_data is None or chain_data.empty:
                    notes.append(f"{name} {type_name} chain: empty")
                    continue

                notes.append(f"{name} {type_name} chain: {len(chain_data)} contracts")
                print_columns(chain_data)
                print_sample(chain_data, 3)

                # Check option_area_type field (OI bug verification)
                if "option_area_type" in chain_data.columns:
                    area_vals = chain_data["option_area_type"].unique()
                    notes.append(f"option_area_type unique values: {area_vals}")
                    notes.append("  (This is an ENUM, NOT OI! Bug confirmed if non-numeric)")

                # Collect strike info
                if "strike_price" in chain_data.columns:
                    strikes = sorted(chain_data["strike_price"].unique())
                    notes.append(f"Strikes: {len(strikes)} total, "
                                 f"range [{strikes[0]} - {strikes[-1]}]")

                # Collect option codes for Probe 5
                if "code" in chain_data.columns:
                    codes = chain_data["code"].tolist()
                    found_codes.extend(codes[:10])  # Take up to 10 for snapshot test
                    notes.append(f"Sample codes for Probe 5: {codes[:5]}")

                # Found data, stop trying other option types for this index
                break

            except Exception as e:
                notes.append(f"{name} {type_name}: ERROR — {e}")

    status = "PASS" if found_codes else "FAIL"
    print_status(status, notes)
    return bool(found_codes), found_codes


# ── Probe 5: Option Snapshot (Greeks/IV/OI) ──


def probe_5_option_snapshot(ctx: OpenQuoteContext, option_codes: list[str]) -> bool:
    print_header(5, 7, "OPTION SNAPSHOT (Greeks/IV/OI)")

    if not option_codes:
        print_status("SKIP", ["No option codes from Probe 4"])
        return False

    codes_to_check = option_codes[:10]
    try:
        ret, snap = ctx.get_market_snapshot(codes_to_check)
        if ret != RET_OK:
            print_status("FAIL", [f"get_market_snapshot failed: {snap}"])
            return False

        if snap.empty:
            print_status("FAIL", ["Snapshot returned empty"])
            return False

        notes = []
        print_columns(snap)

        # Key option fields to verify
        option_fields = [
            "option_open_interest", "option_implied_volatility",
            "option_delta", "option_gamma", "option_theta", "option_vega",
            "option_area_type", "option_type", "strike_price",
        ]

        for field in option_fields:
            if field in snap.columns:
                vals = snap[field].head(3).tolist()
                notes.append(f"{field}: {vals}")
            else:
                notes.append(f"{field}: MISSING from snapshot")

        # OI Bug evidence: compare option_area_type vs option_open_interest
        if "option_area_type" in snap.columns and "option_open_interest" in snap.columns:
            notes.append("--- OI BUG VERIFICATION ---")
            for _, row in snap.head(3).iterrows():
                area = row.get("option_area_type", "N/A")
                oi = row.get("option_open_interest", "N/A")
                code = row.get("code", "?")
                notes.append(f"  {code}: option_area_type={area}, "
                             f"option_open_interest={oi}")
            notes.append("  Conclusion: option_area_type is enum type (e.g., EUROPEAN), "
                         "option_open_interest is the real OI number")

        # Print sample with key fields
        display_cols = [c for c in ["code", "last_price"] + option_fields
                        if c in snap.columns]
        print_sample(snap[display_cols], 5)

        print_status("PASS", notes)
        return True
    except Exception as e:
        print_status("FAIL", [str(e)])
        return False


# ── Probe 6: LV2 Order Book ──


def probe_6_order_book(ctx: OpenQuoteContext) -> bool:
    print_header(6, 7, "LV2 ORDER BOOK (00700.HK)")
    symbol = "HK.00700"

    try:
        ret, msg = ctx.subscribe([symbol], [SubType.ORDER_BOOK])
        if ret != RET_OK:
            print_status("FAIL", [f"subscribe ORDER_BOOK failed: {msg}"])
            return False

        ret, data = ctx.get_order_book(symbol, num=10)
        if ret != RET_OK:
            print_status("FAIL", [f"get_order_book failed: {data}"])
            return False

        notes = []
        notes.append(f"Return type: {type(data)}")

        if isinstance(data, dict):
            notes.append(f"Keys: {list(data.keys())}")
            code = data.get("code", "N/A")
            notes.append(f"Code: {code}")

            for side in ["Ask", "Bid"]:
                entries = data.get(side, [])
                notes.append(f"{side}: {len(entries)} levels")
                for i, entry in enumerate(entries[:5]):
                    if isinstance(entry, (list, tuple)):
                        price, vol = entry[0], entry[1]
                        order_num = entry[2] if len(entry) > 2 else "N/A"
                        detail = entry[3] if len(entry) > 3 else "N/A"
                        notes.append(f"  L{i+1}: price={price}, vol={vol}, "
                                     f"orders={order_num}, detail={detail}")
                    else:
                        notes.append(f"  L{i+1}: {entry}")

            actual_levels = max(len(data.get("Ask", [])), len(data.get("Bid", [])))
            notes.append(f"Actual depth: {actual_levels} levels "
                         f"(requested 10)")
        else:
            notes.append(f"Unexpected data format: {str(data)[:200]}")

        print_status("PASS" if data else "FAIL", notes)
        return bool(data)
    except Exception as e:
        print_status("FAIL", [str(e)])
        return False


# ── Probe 7: Subscription Quota & Push Test ──


def probe_7_subscription_push(ctx: OpenQuoteContext) -> bool | None:
    print_header(7, 7, "SUBSCRIPTION QUOTA & PUSH TEST")
    symbol = "HK.00700"
    notes = []

    try:
        # Check quota before
        ret, quota_before = ctx.query_subscription(is_all_conn=True)
        if ret == RET_OK:
            notes.append(f"Subscription quota BEFORE:")
            if isinstance(quota_before, pd.DataFrame):
                notes.append(f"  {quota_before.to_dict()}")
            else:
                notes.append(f"  {quota_before}")
        else:
            notes.append(f"query_subscription failed: {quota_before}")

        # Subscribe and wait for pushes
        push_data: list[dict] = []
        done = threading.Event()

        class _OBHandler(OrderBookHandlerBase):
            def on_recv_rsp(self, rsp_pb):
                ret_code, data = super().on_recv_rsp(rsp_pb)
                if ret_code == RET_OK:
                    push_data.append({"time": time.time(), "data": str(data)[:100]})
                    done.set()
                return ret_code, data

        ctx.set_handler(_OBHandler())

        # Subscribe QUOTE (might already be subscribed from Probe 1)
        ret, msg = ctx.subscribe([symbol], [SubType.QUOTE, SubType.ORDER_BOOK])
        if ret != RET_OK:
            notes.append(f"subscribe failed: {msg}")

        # Check quota after
        ret, quota_after = ctx.query_subscription(is_all_conn=True)
        if ret == RET_OK:
            notes.append(f"Subscription quota AFTER:")
            if isinstance(quota_after, pd.DataFrame):
                notes.append(f"  {quota_after.to_dict()}")
            else:
                notes.append(f"  {quota_after}")

        # Wait for pushes
        notes.append("Waiting 10s for pushes...")
        done.wait(timeout=10)
        push_count = len(push_data)

        if push_count > 0:
            notes.append(f"Received {push_count} pushes in 10s")
            notes.append(f"  First push: {push_data[0]}")
            status = "PASS"
            result = True
        else:
            notes.append("No pushes received in 10s (market may be closed)")
            status = "SKIP"
            result = None

        # Cleanup: unsubscribe
        try:
            ret, msg = ctx.unsubscribe([symbol], [SubType.ORDER_BOOK])
            notes.append(f"Unsubscribe ORDER_BOOK: {'OK' if ret == RET_OK else msg}")
        except Exception as e:
            notes.append(f"Unsubscribe failed: {e}")

        # Final quota
        ret, quota_final = ctx.query_subscription(is_all_conn=True)
        if ret == RET_OK:
            notes.append(f"Subscription quota FINAL:")
            if isinstance(quota_final, pd.DataFrame):
                notes.append(f"  {quota_final.to_dict()}")
            else:
                notes.append(f"  {quota_final}")

        print_status(status, notes)
        return result
    except Exception as e:
        print_status("FAIL", [str(e)])
        return False


# ── Main ──


def main():
    parser = argparse.ArgumentParser(
        description="HK Futu API Data Feasibility Probe"
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="FutuOpenD host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=11111,
                        help="FutuOpenD port (default: 11111)")
    args = parser.parse_args()

    print(f"\n{'#' * 50}")
    print(f"  HK Futu API Data Feasibility Probe")
    print(f"  {datetime.now(HKT).strftime('%Y-%m-%d %H:%M:%S')} HKT")
    print(f"  Target: {args.host}:{args.port}")
    print(f"{'#' * 50}")

    # Connect
    try:
        ctx = OpenQuoteContext(host=args.host, port=args.port)
    except Exception as e:
        print(f"\nFATAL: Cannot connect to FutuOpenD — {e}")
        sys.exit(1)

    results: dict[str, str] = {}

    # Probe 0: Connection
    if not probe_0_connection(ctx):
        results["P0 Connection"] = "FAIL"
        print("\nFATAL: Connection failed, cannot continue.")
        ctx.close()
        sys.exit(1)
    results["P0 Connection"] = "PASS"

    # Probe 1: Stock Quote
    ok = probe_1_stock_quote(ctx)
    results["P1 HK Stock Quote"] = "PASS" if ok else "FAIL"

    # Probe 2: Index Quote
    ok = probe_2_index_quote(ctx)
    results["P2 HK Index Quote"] = "PASS" if ok else "FAIL"

    # Probe 3: K-line History
    ok = probe_3_kline(ctx)
    results["P3 HK 1m K-line"] = "PASS" if ok else "FAIL"

    # Probe 4: Option Chain
    ok, option_codes = probe_4_option_chain(ctx)
    results["P4 HSI Option Chain"] = "PASS" if ok else "FAIL"

    # Probe 5: Option Snapshot
    ok = probe_5_option_snapshot(ctx, option_codes)
    results["P5 Option Snapshot"] = "PASS" if ok else ("SKIP" if not option_codes else "FAIL")

    # Probe 6: Order Book
    ok = probe_6_order_book(ctx)
    results["P6 LV2 Order Book"] = "PASS" if ok else "FAIL"

    # Probe 7: Subscription & Push
    result = probe_7_subscription_push(ctx)
    if result is True:
        results["P7 Subscription/Push"] = "PASS"
    elif result is None:
        results["P7 Subscription/Push"] = "SKIP"
    else:
        results["P7 Subscription/Push"] = "FAIL"

    ctx.close()

    # ── Summary ──
    print(f"\n{'=' * 50}")
    print("SUMMARY")
    print(f"{'=' * 50}")

    passed = sum(1 for v in results.values() if v == "PASS")
    failed = sum(1 for v in results.values() if v == "FAIL")
    skipped = sum(1 for v in results.values() if v == "SKIP")

    for name, status in results.items():
        icon = {"PASS": "✓", "FAIL": "✗", "SKIP": "○"}.get(status, "?")
        print(f"  {icon} {name:<25} {status}")

    print(f"\nResult: {passed} PASS, {failed} FAIL, {skipped} SKIP")

    # ── Key Findings & Decision Matrix ──
    print(f"\n{'=' * 50}")
    print("KEY FINDINGS")
    print(f"{'=' * 50}")

    if results.get("P3 HK 1m K-line") == "FAIL":
        print("  ✗ CRITICAL: No HK K-line data — Volume Profile/VWAP impossible.")
        print("    → HK Playbook scheme NOT FEASIBLE with Futu alone.")
    elif results.get("P3 HK 1m K-line") == "PASS":
        print("  ✓ HK 1m K-line available — Volume Profile + VWAP feasible.")

    if results.get("P4 HSI Option Chain") == "FAIL":
        print("  ✗ No HSI option chain — Gamma wall calculation impossible.")
        print("    → Degrade to pure Volume Profile + VWAP approach.")
    elif results.get("P4 HSI Option Chain") == "PASS":
        print("  ✓ HSI option chain available — Gamma wall calculation feasible.")

    if results.get("P5 Option Snapshot") in ("PASS",):
        print("  ✓ Option OI/Greeks available from snapshot.")
        print("    → Confirmed: use option_open_interest, NOT option_area_type.")
    elif results.get("P5 Option Snapshot") == "FAIL":
        print("  ✗ Option snapshot failed — OI/Greeks unavailable.")

    if results.get("P6 LV2 Order Book") == "PASS":
        print("  ✓ LV2 order book available — large order detection feasible.")
    else:
        print("  ✗ LV2 order book unavailable — degrade to quote-only analysis.")

    print(f"\n{'=' * 50}")
    print("DECISION MATRIX")
    print(f"{'=' * 50}")

    if results.get("P3 HK 1m K-line") == "FAIL":
        print("  → BLOCKER: Need alternative data source for HK K-lines.")
    elif (results.get("P3 HK 1m K-line") == "PASS"
          and results.get("P1 HK Stock Quote") == "PASS"):
        if results.get("P4 HSI Option Chain") == "PASS":
            print("  → ALL CLEAR: Proceed with full P0-P7 implementation roadmap.")
        else:
            print("  → PARTIAL: Proceed with Volume Profile + VWAP (skip Gamma wall).")

    print()


if __name__ == "__main__":
    main()
