#!/usr/bin/env python3
"""
Intraday price updater — runs every 30 min during NSE market hours.
Reads the current active symbols from scan_latest.json and active_positions.json,
fetches live prices via yfinance, writes data/prices_live.json.

Zero Claude cost. No ledger writes. Pure price data only.
"""
import json, os, datetime, sys
import yfinance as yf

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

def is_market_hours():
    now = datetime.datetime.now(IST)
    # NSE market hours: 9:15am - 3:30pm IST, Mon-Fri
    if now.weekday() >= 5:
        return False
    market_open  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_open <= now <= market_close

def get_tracked_symbols():
    symbols = set()
    # From scan_latest.json
    try:
        with open("data/scan_latest.json") as f:
            data = json.load(f)
        for section in ["squeeze_stocks", "blast_stocks", "watchlist_stocks"]:
            for s in data.get(section, []):
                symbols.add(s["symbol"])
    except Exception as e:
        print(f"  scan_latest.json: {e}")
    # From active_positions.json
    try:
        with open("data/active_positions.json") as f:
            ledger = json.load(f)
        for sym, entry in ledger.items():
            if entry.get("status") in ("SQUEEZE", "BLAST", "WATCHLIST"):
                symbols.add(sym)
    except Exception as e:
        print(f"  active_positions.json: {e}")
    return list(symbols)

def fetch_prices(symbols):
    if not symbols:
        return {}
    prices = {}
    # yfinance accepts comma-separated tickers
    tickers = [s + ".NS" for s in symbols]
    print(f"  Fetching {len(tickers)} symbols via yfinance...")
    try:
        data = yf.download(
            tickers,
            period="1d",
            interval="1m",
            progress=False,
            group_by="ticker",
            auto_adjust=True
        )
        now_ist = datetime.datetime.now(IST)
        for sym in symbols:
            ticker = sym + ".NS"
            try:
                if len(tickers) == 1:
                    # Single ticker — different DataFrame structure
                    col = data["Close"]
                else:
                    col = data[ticker]["Close"]
                last = col.dropna()
                if not last.empty:
                    prices[sym] = round(float(last.iloc[-1]), 2)
            except Exception as e:
                pass  # symbol may not have data
    except Exception as e:
        print(f"  yfinance download error: {e}")
        # Fallback: try one by one for small sets
        if len(symbols) <= 10:
            for sym in symbols:
                try:
                    t = yf.Ticker(sym + ".NS")
                    info = t.fast_info
                    price = getattr(info, "last_price", None)
                    if price:
                        prices[sym] = round(float(price), 2)
                except:
                    pass
    return prices

def main():
    print(f"=== Price Update: {datetime.datetime.now(IST).strftime('%Y-%m-%d %H:%M IST')} ===")

    in_market = is_market_hours()
    print(f"  Market hours: {'YES' if in_market else 'NO'}")

    symbols = get_tracked_symbols()
    print(f"  Tracked symbols: {len(symbols)}")

    prices = {}
    if symbols:
        prices = fetch_prices(symbols)
        print(f"  Prices fetched: {len(prices)}")

    output = {
        "updated_at": datetime.datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"),
        "in_market_hours": in_market,
        "prices": prices
    }

    os.makedirs("data", exist_ok=True)
    with open("data/prices_live.json", "w") as f:
        json.dump(output, f)
    print(f"  Written: data/prices_live.json ({len(prices)} prices)")

if __name__ == "__main__":
    main()
