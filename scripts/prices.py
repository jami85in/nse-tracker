#!/usr/bin/env python3
"""
Intraday price updater — runs every 15 min during NSE market hours via prices.yml.
Writes data/prices_live.json for the dashboard to read.
Uses yf.download with 1-minute bars — same approach proven to work in scan.py.
Zero Claude cost. No ledger writes.
"""
import json, os, datetime, time
import yfinance as yf

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

def is_market_hours():
    now = datetime.datetime.now(IST)
    if now.weekday() >= 5:
        return False
    open_  = now.replace(hour=9,  minute=10, second=0, microsecond=0)
    close_ = now.replace(hour=15, minute=35, second=0, microsecond=0)
    return open_ <= now <= close_

def get_symbols():
    symbols = set()
    # From scan outputs
    for path in ["data/scan_latest.json"]:
        try:
            with open(path) as f:
                data = json.load(f)
            for section in ["squeeze_stocks", "blast_stocks", "watchlist_stocks"]:
                for s in data.get(section, []):
                    symbols.add(s["symbol"])
        except Exception as e:
            print(f"  Warning reading {path}: {e}")
    # From ledger — catch any extra symbols not in current scan output
    try:
        with open("data/active_positions.json") as f:
            ledger = json.load(f)
        for sym, v in ledger.items():
            if v.get("status") in ("SQUEEZE", "BLAST"):
                symbols.add(sym)
    except Exception as e:
        print(f"  Warning reading ledger: {e}")
    return sorted(symbols)

def fetch_price_single(symbol):
    """Fetch latest intraday price for one symbol via 1-minute bars."""
    try:
        ticker = symbol + ".NS"
        df = yf.download(ticker, period="1d", interval="1m", progress=False, auto_adjust=True)
        if df is None or df.empty:
            return None
        # Last available close
        close_col = "Close"
        if hasattr(df.columns, 'levels'):
            # MultiIndex when downloading single ticker sometimes
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        last_close = df["Close"].dropna().iloc[-1]
        return round(float(last_close), 2)
    except Exception as e:
        return None

def fetch_prices(symbols):
    """Fetch prices for all symbols. Uses batch download where possible."""
    prices = {}
    if not symbols:
        return prices

    print(f"  Fetching {len(symbols)} symbols via 1m bars...")

    # Batch download is faster
    BATCH = 40
    for i in range(0, len(symbols), BATCH):
        batch = symbols[i:i+BATCH]
        tickers_str = " ".join(s + ".NS" for s in batch)
        try:
            df = yf.download(
                tickers_str,
                period="1d",
                interval="1m",
                progress=False,
                auto_adjust=True,
                group_by="ticker"
            )
            if df is None or df.empty:
                raise ValueError("Empty dataframe")

            for sym in batch:
                try:
                    ticker = sym + ".NS"
                    if len(batch) == 1:
                        col = df["Close"]
                    else:
                        col = df[ticker]["Close"]
                    last = col.dropna().iloc[-1]
                    if last and last > 0:
                        prices[sym] = round(float(last), 2)
                except Exception:
                    pass

        except Exception as e:
            print(f"  Batch {i//BATCH + 1} failed ({e}), trying one by one...")
            for sym in batch:
                price = fetch_price_single(sym)
                if price:
                    prices[sym] = price
                time.sleep(0.2)

        time.sleep(1)  # rate limit between batches

    return prices

def main():
    now_ist = datetime.datetime.now(IST)
    print(f"=== NSE Price Update: {now_ist.strftime('%Y-%m-%d %H:%M IST')} ===")
    print(f"  Market hours: {is_market_hours()}")

    symbols = get_symbols()
    print(f"  Symbols: {len(symbols)}")
    if symbols:
        print(f"  Sample: {symbols[:5]}")

    prices = fetch_prices(symbols)
    print(f"  Prices fetched: {len(prices)}")
    if prices:
        sample = list(prices.items())[:5]
        for sym, p in sample:
            print(f"    {sym}: ₹{p}")

    output = {
        "updated_at": now_ist.strftime("%Y-%m-%d %H:%M IST"),
        "in_market_hours": is_market_hours(),
        "count": len(prices),
        "prices": prices
    }

    os.makedirs("data", exist_ok=True)
    with open("data/prices_live.json", "w") as f:
        json.dump(output, f)

    print(f"  ✓ Written data/prices_live.json with {len(prices)} prices")

if __name__ == "__main__":
    main()
