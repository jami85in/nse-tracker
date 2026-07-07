#!/usr/bin/env python3
"""
Live intraday price updater using Zerodha Kite Connect — the OFFICIAL,
licensed data source. Replaces the earlier NSE-scraping prices.py, which
was fundamentally unreliable because NSE blocks datacenter IPs from live
endpoints. Kite Connect has no such restriction since it's a paid, licensed
API — it works reliably from GitHub Actions.

Reads today's access_token from the private secrets repo (see
kite_token_reader.py). If no valid (non-stale) token is found — i.e. you
haven't done today's manual login yet — this exits cleanly without
touching prices_live.json, preserving whatever was there before.

Run every ~5 min during market hours via prices.yml (can be more frequent
than the old NSE-scraping version since Kite Connect has generous rate
limits: ~3 requests/second, quote endpoint supports up to 500 instruments
per call).
"""
import json, os, sys, datetime
from kiteconnect import KiteConnect
from kite_token_reader import get_kite_token

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
API_KEY = os.environ.get("KITE_API_KEY", "")


def is_market_hours():
    now = datetime.datetime.now(IST)
    if now.weekday() >= 5:
        return False
    open_ = now.replace(hour=9, minute=15, second=0, microsecond=0)
    close_ = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_ <= now <= close_


def get_symbols():
    symbols = set()
    try:
        with open("data/scan_latest.json") as f:
            data = json.load(f)
        for section in ["squeeze_stocks", "blast_stocks", "watchlist_stocks",
                        "short_squeeze_stocks", "short_breakdown_stocks"]:
            for s in data.get(section, []):
                symbols.add(s["symbol"])
    except Exception as e:
        print(f"  Warning reading scan_latest.json: {e}")
    try:
        with open("data/active_positions.json") as f:
            ledger = json.load(f)
        for sym, v in ledger.items():
            if isinstance(v, dict) and v.get("status") in ("SQUEEZE", "BLAST", "WATCHLIST"):
                symbols.add(sym)
    except Exception as e:
        print(f"  Warning reading ledger: {e}")
    return sorted(symbols)


def fetch_prices_kite(kite, symbols):
    """Kite's quote API wants 'EXCHANGE:SYMBOL' format and returns rich data
    (ohlc, depth, volume) — we only need last_price here. Supports up to
    500 instruments per call; batch defensively at 400 to leave headroom."""
    prices = {}
    instruments = [f"NSE:{s}" for s in symbols]
    BATCH = 400
    for i in range(0, len(instruments), BATCH):
        batch = instruments[i:i + BATCH]
        try:
            quotes = kite.quote(batch)
            for key, q in quotes.items():
                sym = key.split(":", 1)[1]
                price = q.get("last_price")
                if price and price > 0:
                    prices[sym] = round(float(price), 2)
        except Exception as e:
            print(f"  Batch fetch error: {e}")
    return prices


def main():
    now_ist = datetime.datetime.now(IST)
    print(f"=== Kite Live Price Update: {now_ist.strftime('%Y-%m-%d %H:%M IST')} ===")

    in_market = is_market_hours()
    print(f"  In market hours: {in_market}")
    if not in_market:
        print("  Market closed — preserving existing prices_live.json, exiting.")
        return

    access_token, is_stale = get_kite_token()
    if access_token is None:
        print("  ERROR: no Kite access token available (check SECRETS_REPO_PAT / "
              "SECRETS_REPO secrets are set). Preserving existing file.")
        return
    if is_stale:
        print("  WARNING: today's Kite token hasn't been generated yet — "
              "do the daily manual login (see kite_generate_token.py). "
              "Preserving existing prices_live.json for now.")
        return

    if not API_KEY:
        print("  ERROR: KITE_API_KEY not set. Preserving existing file.")
        return

    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(access_token)

    symbols = get_symbols()
    print(f"  Tracked symbols: {len(symbols)}")

    try:
        prices = fetch_prices_kite(kite, symbols)
    except Exception as e:
        print(f"  ERROR fetching quotes: {e}. Preserving existing file.")
        return

    print(f"  Fetched {len(prices)}/{len(symbols)} prices.")
    for sym in list(prices.keys())[:5]:
        print(f"    {sym}: ₹{prices[sym]}")

    if len(prices) == 0:
        print("  No prices fetched — preserving existing file.")
        return

    output = {
        "updated_at": now_ist.strftime("%Y-%m-%d %H:%M IST"),
        "market_open": True,
        "in_market_hours": True,
        "source": "Kite Connect (licensed, live)",
        "count": len(prices),
        "prices": prices,
    }
    os.makedirs("data", exist_ok=True)
    with open("data/prices_live.json", "w") as f:
        json.dump(output, f)
    print(f"  ✓ Written {len(prices)} live prices to data/prices_live.json")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
