#!/usr/bin/env python3
"""
Intraday price updater — runs every 15 min during NSE market hours via prices.yml.
Uses the same NSE session approach as scan.py (proven to work in GitHub Actions).
Fetches live prices for ALL tracked symbols and writes data/prices_live.json.
"""
import json, os, datetime, time, requests

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

NSE_BASE      = "https://www.nseindia.com"
NSE_QUOTE_URL = "https://www.nseindia.com/api/quote-equity?symbol={symbol}"
NSE_MARKET_STATUS_URL = "https://www.nseindia.com/api/marketStatus"

def get_session():
    """Establish NSE session with cookies — same pattern as scan.py."""
    s = requests.Session()
    s.headers.update(HEADERS)
    try:
        s.get(NSE_BASE, timeout=10)
        time.sleep(0.5)
    except Exception as e:
        print(f"  Warning: NSE session setup failed: {e}")
    return s

def is_market_open(session):
    """Check NSE market status directly."""
    try:
        r = session.get(NSE_MARKET_STATUS_URL, timeout=8)
        if r.status_code == 200:
            data = r.json()
            for market in data.get("marketState", []):
                if market.get("market") == "Capital Market":
                    return market.get("marketStatus") == "Open"
    except Exception:
        pass
    # Fallback: time-based check
    now = datetime.datetime.now(IST)
    if now.weekday() >= 5:
        return False
    open_  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    close_ = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_ <= now <= close_

def fetch_price_nse(session, symbol):
    """Fetch live quote for one symbol from NSE."""
    try:
        url = NSE_QUOTE_URL.format(symbol=symbol)
        r = session.get(url, timeout=8)
        if r.status_code == 200:
            data = r.json()
            price = data.get("priceInfo", {}).get("lastPrice")
            if price and price > 0:
                return round(float(price), 2)
    except Exception:
        pass
    return None

def get_symbols():
    """Get all symbols to track from scan output and ledger."""
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

def main():
    now_ist = datetime.datetime.now(IST)
    print(f"=== NSE Price Update: {now_ist.strftime('%Y-%m-%d %H:%M IST')} ===")

    session = get_session()
    market_open = is_market_open(session)
    print(f"  Market open: {market_open}")

    symbols = get_symbols()
    print(f"  Symbols to fetch: {len(symbols)}")

    prices = {}
    failed = 0
    for i, sym in enumerate(symbols):
        price = fetch_price_nse(session, sym)
        if price:
            prices[sym] = price
        else:
            failed += 1
        # Polite rate limiting — NSE allows ~5 req/sec
        if i > 0 and i % 10 == 0:
            time.sleep(0.5)
        else:
            time.sleep(0.1)

    print(f"  Prices fetched: {len(prices)}/{len(symbols)} ({failed} failed)")
    for sym, p in list(prices.items())[:5]:
        print(f"    {sym}: ₹{p}")

    if len(prices) == 0:
        print("  ⚠ No prices fetched — preserving existing prices_live.json unchanged.")
        print("  This is expected outside market hours or if NSE API is temporarily unavailable.")
        return

    output = {
        "updated_at": now_ist.strftime("%Y-%m-%d %H:%M IST"),
        "market_open": market_open,
        "in_market_hours": market_open,
        "count": len(prices),
        "prices": prices
    }

    os.makedirs("data", exist_ok=True)
    with open("data/prices_live.json", "w") as f:
        json.dump(output, f)
    print(f"  ✓ Written data/prices_live.json with {len(prices)} prices")

if __name__ == "__main__":
    main()
