#!/usr/bin/env python3
"""
fetch_zerodha_holdings.py — pulls your ACTUAL Zerodha demat holdings via
Kite Connect's holdings() API, so stocks you hold that were bought outside
this app's own "Invest" flow can show up in Long-Term Holdings automatically
instead of needing manual entry.

Reads today's access_token the same way prices_kite.py does (see
kite_token_reader.py). Safe degradation: if no fresh token, exits cleanly
without touching the existing data/zerodha_holdings.json.

WHAT THIS DOES NOT DO: it does not write into the app's Long-Term Holdings
list directly — that list lives in the browser's localStorage on your
phone, which a GitHub Action has no way to reach. This script only writes
the server-side data/zerodha_holdings.json; index.html reads that file and
merges it into your local holdings on load (see the merge logic there for
exactly how new vs. already-known vs. manually-added holdings are handled
— manually-added entries are never touched or duplicated).

Kite's holdings() response does not include an original purchase date, so
synced holdings show "Synced from Zerodha" instead of a since-date.
"""
import json, os, sys, datetime
from kiteconnect import KiteConnect
from kite_token_reader import get_kite_token

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
API_KEY = os.environ.get("KITE_API_KEY", "")
OUT_PATH = "data/zerodha_holdings.json"


def main():
    now_ist = datetime.datetime.now(IST)
    print(f"=== Zerodha Holdings Sync: {now_ist.strftime('%Y-%m-%d %H:%M IST')} ===")

    access_token, is_stale = get_kite_token()
    if access_token is None:
        print("  ERROR: no Kite access token available. Preserving existing file.")
        return
    if is_stale:
        print("  WARNING: today's Kite token hasn't been generated yet. "
              "Preserving existing file.")
        return
    if not API_KEY:
        print("  ERROR: KITE_API_KEY not set. Preserving existing file.")
        return

    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(access_token)

    try:
        raw_holdings = kite.holdings()
    except Exception as e:
        print(f"  ERROR fetching holdings: {e}. Preserving existing file.")
        return

    holdings = {}
    for h in raw_holdings:
        if h.get("exchange") != "NSE":
            continue  # NSE only, matching this project's convention throughout
        qty = (h.get("quantity") or 0) + (h.get("t1_quantity") or 0)
        if qty <= 0:
            continue
        sym = h.get("tradingsymbol")
        avg_price = h.get("average_price")
        if not sym or not avg_price:
            continue
        holdings[sym] = {
            "qty": int(qty),
            "avgPrice": round(float(avg_price), 2),
            "lastPrice": round(float(h.get("last_price") or 0), 2),
            "pnl": round(float(h.get("pnl") or 0), 2),
        }

    print(f"  Fetched {len(raw_holdings)} raw holdings, {len(holdings)} NSE with qty > 0.")
    for sym, h in list(holdings.items())[:5]:
        print(f"    {sym}: qty={h['qty']} avg={h['avgPrice']}")

    output = {
        "updated_at": now_ist.strftime("%Y-%m-%d %H:%M IST"),
        "source": "Kite Connect (licensed) — kite.holdings()",
        "count": len(holdings),
        "holdings": holdings,
    }
    os.makedirs("data", exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(output, f)
    print(f"  ✓ Written {len(holdings)} holdings to {OUT_PATH}")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
