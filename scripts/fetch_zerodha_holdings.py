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
    skipped = []
    for h in raw_holdings:
        sym = h.get("tradingsymbol")
        if not sym:
            skipped.append((h, "no tradingsymbol at all"))
            continue
        # NO exchange filter — a prior version dropped anything not tagged
        # exchange=="NSE" in Kite's response, but Kite tags holdings by
        # whichever exchange the position happens to be registered under
        # (often BSE for some stocks even when the same shares are fully
        # NSE-tradeable), which was very likely silently dropping real
        # holdings (confirmed pattern: IDEA, IDFCFIRSTB, MEESHO and others
        # went missing this way). Every symbol is captured regardless of
        # its exchange tag; downstream price lookups are NSE-specific
        # anyway and work correctly off the tradingsymbol alone.
        #
        # Quantity includes collateral (pledged shares) too — those are
        # still real holdings you own, just not immediately sellable,
        # and excluding them would be the same class of silent-drop bug.
        qty = ((h.get("quantity") or 0) + (h.get("t1_quantity") or 0)
               + (h.get("collateral_quantity") or 0))
        if qty <= 0:
            skipped.append((h, f"zero/negative total quantity ({qty})"))
            continue
        # NO avg_price filter — a prior version dropped any holding with a
        # falsy average_price, which incorrectly treats a legitimate 0
        # (e.g. bonus/gifted shares with no cost basis) the same as missing
        # data. Missing is None; 0 is a valid, real value. Default to 0.0
        # only when truly absent, never silently drop the holding.
        avg_price = h.get("average_price")
        holdings[sym] = {
            "qty": int(qty),
            "avgPrice": round(float(avg_price), 2) if avg_price is not None else 0.0,
            "lastPrice": round(float(h.get("last_price") or 0), 2),
            "pnl": round(float(h.get("pnl") or 0), 2),
            "exchange": h.get("exchange"),  # kept for visibility/debugging, not filtered on
        }

    print(f"  Fetched {len(raw_holdings)} raw holdings from Kite, {len(holdings)} captured "
          f"(no exchange/avg_price filtering), {len(skipped)} genuinely skipped.")
    print(f"  All captured symbols: {sorted(holdings.keys())}")
    for h, reason in skipped:
        print(f"    SKIPPED {h.get('tradingsymbol')}: {reason} (raw: {h})")

    # Recently-bought stocks can sit in kite.positions() for up to a day
    # before they settle into kite.holdings() (T+1 settlement) — a real,
    # normal Zerodha/exchange behavior, not a bug. A stock bought yesterday
    # or today would be invisible to holdings() alone. Merge in any CNC
    # (delivery, not intraday) position with a positive net quantity that
    # holdings() didn't already report, using the position's own average
    # buy price. Only ADDS symbols missing from holdings() — never
    # overwrites a holdings()-sourced entry, since that's the more
    # authoritative, fully-settled source once it exists.
    try:
        raw_positions = kite.positions()
        added_from_positions = []
        for p in raw_positions.get("net", []):
            sym = p.get("tradingsymbol")
            if not sym or sym in holdings:
                continue
            if p.get("product") != "CNC":  # CNC = delivery equity, not MIS/intraday
                continue
            qty = p.get("quantity") or 0
            if qty <= 0:
                continue
            avg_price = p.get("average_price")
            holdings[sym] = {
                "qty": int(qty),
                "avgPrice": round(float(avg_price), 2) if avg_price is not None else 0.0,
                "lastPrice": round(float(p.get("last_price") or 0), 2),
                "pnl": round(float(p.get("pnl") or 0), 2),
                "exchange": p.get("exchange"),
                "source_note": "from positions() — not yet settled into holdings()",
            }
            added_from_positions.append(sym)
        if added_from_positions:
            print(f"  +{len(added_from_positions)} symbols from positions() not yet in "
                  f"holdings() (likely bought very recently, pending T+1 settlement): "
                  f"{added_from_positions}")
    except Exception as e:
        print(f"  WARNING: kite.positions() fetch failed ({e}) — continuing with "
              f"holdings()-only data, not fatal.")

    # Sanity guard: the frontend now treats "symbol absent from this file"
    # as "you sold it" and REMOVES it from Long-Term Holdings. That makes
    # writing a suspiciously-empty or drastically-shrunk result dangerous —
    # a transient Kite API glitch that returns an empty (not erroring) list
    # could otherwise silently wipe out real holdings on the next page load.
    # Compare against whatever's already committed and refuse to overwrite
    # if this looks like a glitch rather than genuine, large-scale selling.
    prev_count = 0
    if os.path.exists(OUT_PATH):
        try:
            prev_count = json.load(open(OUT_PATH)).get("count", 0)
        except Exception:
            prev_count = 0
    if prev_count >= 3 and len(holdings) == 0:
        print(f"  REFUSING to write: got 0 holdings but {prev_count} were previously recorded — "
              f"this looks like a transient API issue, not genuine full liquidation. "
              f"Preserving existing file untouched.")
        return
    if prev_count >= 5 and len(holdings) < prev_count * 0.3:
        print(f"  REFUSING to write: holdings count dropped from {prev_count} to {len(holdings)} "
              f"(>70% drop) in a single sync — too large a swing to trust without confirmation. "
              f"Preserving existing file untouched. If you genuinely sold this much, next run "
              f"after the count stabilizes will pick it up correctly.")
        return

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
