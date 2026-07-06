#!/usr/bin/env python3
"""
Intraday price updater — every ~7 min during NSE market hours via prices.yml.
Fetches live prices using NSE's index-constituents API with multiple index
endpoints as fallbacks (NSE rotates/deprecates these periodically).
Only writes during market hours; preserves existing file if it gets nothing.
"""
import json, os, datetime, time, requests

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com/market-data/live-equity-market",
    "X-Requested-With": "XMLHttpRequest",
}

NSE_BASE = "https://www.nseindia.com"

# NSE serves index constituents (with live lastPrice) under these index names.
# We pull several broad indices to cover the whole tracked universe. Together
# these span most of the Nifty 500. NSE occasionally 404s a specific index
# name, so we try many and merge whatever succeeds.
NSE_INDEX_ENDPOINTS = [
    "NIFTY 500",
    "NIFTY 50",
    "NIFTY NEXT 50",
    "NIFTY MIDCAP 100",
    "NIFTY SMALLCAP 100",
    "NIFTY MIDCAP 150",
    "NIFTY SMALLCAP 250",
    "SECURITIES IN F&O",
]

def is_market_hours():
    now = datetime.datetime.now(IST)
    if now.weekday() >= 5:
        return False
    open_  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    close_ = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_ <= now <= close_

def get_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    try:
        s.get(NSE_BASE, timeout=15)
        time.sleep(1)
        s.get("https://www.nseindia.com/market-data/live-equity-market", timeout=15)
        time.sleep(1)
    except Exception as e:
        print(f"  Session setup warning: {e}")
    return s

def fetch_index(session, index_name):
    """Fetch live prices for one index's constituents. Returns {symbol: price}."""
    prices = {}
    try:
        # URL-encode the index name (spaces -> %20)
        from urllib.parse import quote
        url = f"https://www.nseindia.com/api/equity-stockIndices?index={quote(index_name)}"
        r = session.get(url, timeout=20)
        if r.status_code != 200:
            print(f"    [{index_name}] HTTP {r.status_code}")
            return prices
        data = r.json()
        for item in data.get("data", []):
            sym = item.get("symbol")
            price = item.get("lastPrice") or item.get("ltP") or item.get("ltp")
            if sym and price:
                try:
                    p = float(str(price).replace(",", ""))
                    if p > 0:
                        prices[sym] = round(p, 2)
                except (ValueError, TypeError):
                    pass
        print(f"    [{index_name}] {len(prices)} prices")
    except Exception as e:
        print(f"    [{index_name}] error: {e}")
    return prices

def fetch_quote_single(session, symbol):
    """Per-symbol fallback via quote-equity API."""
    try:
        from urllib.parse import quote
        url = f"https://www.nseindia.com/api/quote-equity?symbol={quote(symbol)}"
        r = session.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            price = data.get("priceInfo", {}).get("lastPrice")
            if price and float(price) > 0:
                return round(float(price), 2)
    except Exception:
        pass
    return None

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

def main():
    now_ist = datetime.datetime.now(IST)
    print(f"=== NSE Price Update: {now_ist.strftime('%Y-%m-%d %H:%M IST')} ===")

    in_market = is_market_hours()
    print(f"  In market hours (9:15am-3:30pm IST): {in_market}")
    if not in_market:
        print("  Market closed — preserving existing prices, exiting.")
        return

    symbols = get_symbols()
    print(f"  Tracked symbols: {len(symbols)}")
    tracked = set(symbols)

    session = get_session()

    # Merge prices from all index endpoints that respond
    all_prices = {}
    for idx in NSE_INDEX_ENDPOINTS:
        idx_prices = fetch_index(session, idx)
        for sym, p in idx_prices.items():
            all_prices[sym] = p
        # Stop early if we've already covered everything we track
        if tracked.issubset(all_prices.keys()):
            print(f"  All tracked symbols covered after {idx}")
            break
        time.sleep(0.5)

    prices = {sym: all_prices[sym] for sym in symbols if sym in all_prices}
    missing = [sym for sym in symbols if sym not in all_prices]

    # Per-symbol fallback for whatever's still missing (cap to avoid long runs)
    if missing:
        print(f"  {len(missing)} missing after index sweep, trying per-symbol (max 40)...")
        for sym in missing[:40]:
            p = fetch_quote_single(session, sym)
            if p:
                prices[sym] = p
            time.sleep(0.3)

    print(f"  Final matched: {len(prices)}/{len(symbols)}")
    for sym in list(prices.keys())[:5]:
        print(f"    {sym}: ₹{prices[sym]}")

    if len(prices) == 0:
        print("  ⚠ No prices fetched — preserving existing file (all endpoints failed).")
        return

    output = {
        "updated_at": now_ist.strftime("%Y-%m-%d %H:%M IST"),
        "market_open": True,
        "in_market_hours": True,
        "count": len(prices),
        "prices": prices,
    }
    os.makedirs("data", exist_ok=True)
    with open("data/prices_live.json", "w") as f:
        json.dump(output, f)
    print(f"  ✓ Written {len(prices)} prices to data/prices_live.json")

if __name__ == "__main__":
    main()
