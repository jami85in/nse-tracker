#!/usr/bin/env python3
"""
Intraday price updater — every 15 min during NSE market hours.
Uses NSE bulk APIs to fetch all prices efficiently.
"""
import json, os, datetime, time, requests

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com",
    "X-Requested-With": "XMLHttpRequest",
}

NSE_BASE = "https://www.nseindia.com"

def get_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    try:
        # Hit multiple pages to get full cookie set
        s.get(NSE_BASE, timeout=15)
        time.sleep(1)
        s.get("https://www.nseindia.com/market-data/live-equity-market", timeout=15)
        time.sleep(1)
    except Exception as e:
        print(f"  Session setup warning: {e}")
    return s

def fetch_all_eq_prices(session):
    """
    Fetch ALL equity prices in one shot using NSE's market data API.
    Returns dict of symbol -> price.
    """
    prices = {}
    try:
        # NSE's full equity market data — returns all listed stocks at once
        url = "https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%20500"
        r = session.get(url, timeout=20)
        print(f"  NIFTY 500 API: HTTP {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            for item in data.get("data", []):
                sym = item.get("symbol")
                price = item.get("lastPrice") or item.get("ltp")
                if sym and price and float(price) > 0:
                    prices[sym] = round(float(price), 2)
            print(f"  Got {len(prices)} prices from NIFTY 500 index API")
    except Exception as e:
        print(f"  NIFTY 500 API failed: {e}")

    if len(prices) < 10:
        # Fallback: try the all-securities API
        try:
            url2 = "https://www.nseindia.com/api/allIndices"
            r2 = session.get(url2, timeout=15)
            print(f"  allIndices API: HTTP {r2.status_code}")
        except Exception as e:
            print(f"  allIndices fallback also failed: {e}")

    return prices

def get_symbols():
    symbols = set()
    try:
        with open("data/scan_latest.json") as f:
            data = json.load(f)
        for section in ["squeeze_stocks", "blast_stocks", "watchlist_stocks"]:
            for s in data.get(section, []):
                symbols.add(s["symbol"])
    except Exception as e:
        print(f"  Warning reading scan_latest.json: {e}")
    try:
        with open("data/active_positions.json") as f:
            ledger = json.load(f)
        for sym, v in ledger.items():
            if v.get("status") in ("SQUEEZE", "BLAST", "WATCHLIST"):
                symbols.add(sym)
    except Exception as e:
        print(f"  Warning reading ledger: {e}")
    return sorted(symbols)

def is_market_hours():
    now = datetime.datetime.now(IST)
    if now.weekday() >= 5:
        return False
    open_  = now.replace(hour=9,  minute=10, second=0, microsecond=0)
    close_ = now.replace(hour=15, minute=35, second=0, microsecond=0)
    return open_ <= now <= close_

def main():
    now_ist = datetime.datetime.now(IST)
    print(f"=== NSE Price Update: {now_ist.strftime('%Y-%m-%d %H:%M IST')} ===")

    in_market = is_market_hours()
    print(f"  In market hours: {in_market}")

    symbols = get_symbols()
    print(f"  Tracked symbols: {len(symbols)}")

    session = get_session()

    # Fetch all prices in bulk
    all_prices = fetch_all_eq_prices(session)

    # Filter to only tracked symbols
    prices = {sym: all_prices[sym] for sym in symbols if sym in all_prices}
    missing = [sym for sym in symbols if sym not in all_prices]

    print(f"  Matched: {len(prices)}/{len(symbols)}")
    if missing:
        print(f"  Missing ({len(missing)}): {missing[:10]}")

    # Show sample
    for sym in list(prices.keys())[:5]:
        print(f"    {sym}: ₹{prices[sym]}")

    if len(prices) == 0:
        print("  ⚠ No prices — preserving existing file.")
        return

    output = {
        "updated_at": now_ist.strftime("%Y-%m-%d %H:%M IST"),
        "market_open": in_market,
        "in_market_hours": in_market,
        "count": len(prices),
        "prices": prices
    }

    os.makedirs("data", exist_ok=True)
    with open("data/prices_live.json", "w") as f:
        json.dump(output, f)
    print(f"  ✓ Written {len(prices)} prices to data/prices_live.json")

if __name__ == "__main__":
    main()
