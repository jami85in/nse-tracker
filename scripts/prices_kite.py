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
import market_session

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
API_KEY = os.environ.get("KITE_API_KEY", "")


def is_market_hours():
    """NSE trading right now? Uses market_session.py, which knows weekday
    holidays and special sessions such as Diwali Muhurat trading on a Sunday.
    (Until 8 Nov 2026 this returned False for every Saturday and Sunday, so the
    Muhurat session got no live prices even after the daily login.)
    FORCE_SESSION=1 (the workflow's force input) skips the calendar for a
    special session nobody added yet; quotes are still checked for freshness."""
    if os.environ.get("FORCE_SESSION", "").strip().lower() in ("1", "true", "yes"):
        return True
    return market_session.is_open()


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
    # Real Zerodha holdings (written by fetch_zerodha_holdings.py). This is
    # THE fix for Long-Term Holdings showing "no price" — those symbols live
    # in the browser's localStorage, which no server-side script can see
    # directly, but this file (built from the same kite.holdings() call) is
    # a server-knowable proxy for "what the user actually holds". Without
    # this, any holding with no active scan signal had no reliable price
    # source at all — the frontend's browser-side Yahoo fallback was
    # covering that gap, but Yahoo has since locked down that endpoint
    # entirely (confirmed: HTTP 401 "user is unable to access this
    # feature") — this is the real, durable fix, not another fallback.
    n_before = len(symbols)
    try:
        with open("data/zerodha_holdings.json") as f:
            zdata = json.load(f)
        for sym in zdata.get("holdings", {}):
            symbols.add(sym)
    except Exception as e:
        print(f"  Warning reading zerodha_holdings.json: {e}")
    print(f"  +{len(symbols) - n_before} symbols added from Zerodha holdings.")
    # STRONG seasonal-cycle stocks in their up-season or buy month, for the
    # cycle-dip alerts (scripts/notify.py) and the intraday archive.
    n_before = len(symbols)
    try:
        month = datetime.datetime.now(IST).month
        with open("data/cycles/summary.json") as f:
            for s in json.load(f).get("stocks", []):
                if s.get("grade") == "STRONG" and s.get("entry") and \
                        0 <= (month - s["entry"]) % 12 <= (s.get("hold") or 0) - 1:
                    symbols.add(s["symbol"])
    except Exception as e:
        print(f"  Warning reading cycles summary: {e}")
    print(f"  +{len(symbols) - n_before} symbols added from STRONG cycles in season or in their buy month.")
    return sorted(symbols)


def get_family():
    """Holdings of the family accounts other than the HUF, written by
    fetch_family_holdings.py: symbol -> Kite instruments to try, best first
    (Zerodha's own name, such as "OLAPP-E1" or a BSE-only code, can differ
    from the symbol the ledger and the dashboard use)."""
    try:
        with open("data/family_holdings.json") as f:
            return {s: v.get("try") or [f"NSE:{s}"] for s, v in json.load(f).get("holdings", {}).items()}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"  Warning reading family_holdings.json: {e}")
        return {}


def fetch_family_prices(kite, family, have):
    """Quote each family holding not already priced, by the first of its
    instruments that Kite knows, and file the price under the ledger's symbol."""
    need = {s: c for s, c in family.items() if s not in have}
    if not need:
        return {}
    wanted = sorted({i for c in need.values() for i in c})
    got = {}
    for i in range(0, len(wanted), 400):
        try:
            for key, q in kite.quote(wanted[i:i + 400]).items():
                p = q.get("last_price")
                if p and p > 0:
                    got[key] = round(float(p), 2)
        except Exception as e:
            print(f"  Family batch fetch error: {e}")
    out = {}
    for s, c in need.items():
        for inst in c:
            if inst in got:
                out[s] = got[inst]
                break
    missing = sorted(set(need) - set(out))
    print(f"  Family holdings: {len(out)}/{len(need)} priced" + (f"; no quote for {missing}" if missing else ""))
    return out


PREV_CLOSE = {}   # yesterday's close per symbol, from the same quote call (ohlc.close)
TRADED_TODAY = set()   # symbols whose last trade is from today (an unlisted holiday leaves this empty)
CIRCUITS = {}   # today's [lower, upper] circuit limits per symbol (order links stay inside them)


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
            try:
                quotes = kite.quote(batch)
            except Exception as e1:              # one retry, so a hiccup doesn't drop ~400 symbols
                print(f"  Batch fetch error ({type(e1).__name__}) - retrying once")
                import time
                time.sleep(2)
                quotes = kite.quote(batch)
            for key, q in quotes.items():
                sym = key.split(":", 1)[1]
                price = q.get("last_price")
                if price and price > 0:
                    prices[sym] = round(float(price), 2)
                    lo, hi = q.get("lower_circuit_limit"), q.get("upper_circuit_limit")
                    if lo and hi:
                        CIRCUITS[sym] = [round(float(lo), 2), round(float(hi), 2)]
                    if str(q.get("last_trade_time") or "")[:10] == datetime.datetime.now(IST).date().isoformat():
                        TRADED_TODAY.add(sym)
                    pc = (q.get("ohlc") or {}).get("close")
                    if pc and pc > 0:
                        PREV_CLOSE[sym] = round(float(pc), 2)
        except Exception as e:
            print(f"::warning::Quote batch of {len(batch)} symbols failed twice ({type(e).__name__}) - those prices are missing this round")
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

    family = get_family()
    symbols = sorted(set(get_symbols()) | {s for s, c in family.items() if f"NSE:{s}" in c})
    print(f"  Tracked symbols: {len(symbols)} (incl. {len(family)} family holdings)")

    try:
        prices = fetch_prices_kite(kite, symbols)
    except Exception as e:
        print(f"  ERROR fetching quotes: {e}. Preserving existing file.")
        return

    prices.update(fetch_family_prices(kite, family, prices))
    print(f"  Fetched {len(prices)}/{len(symbols)} prices.")
    for sym in list(prices.keys())[:5]:
        print(f"    {sym}: ₹{prices[sym]}")

    if len(prices) == 0:
        print("  No prices fetched — preserving existing file.")
        return
    # An exchange holiday missing from scripts/market_session.py: Kite still answers, with the
    # last session's trades. Don't publish those as today's prices (the dip alerts would read
    # them as today's moves).
    if now_ist.time() >= datetime.time(9, 17) and len(TRADED_TODAY) < 0.2 * len(prices):
        print(f"::warning::Only {len(TRADED_TODAY)} of {len(prices)} quotes traded today - an exchange holiday missing "
              "from scripts/market_session.py? Preserving existing file.")
        return

    output = {
        "updated_at": now_ist.strftime("%Y-%m-%d %H:%M IST"),
        # Kite only writes during live market hours (see the is_market_hours
        # guard above), so the prices always belong to today's session.
        # Declaring it explicitly lets the frontend compare DATA recency
        # against the scan baseline rather than file write times — the
        # baseline can legitimately be written later in the evening while
        # still containing the PREVIOUS day's closes, if NSE's bhavcopy
        # for today hadn't published yet when the scan ran.
        "data_date": now_ist.strftime("%Y-%m-%d"),
        "market_open": True,
        "in_market_hours": True,
        "source": "Kite Connect (licensed, live)",
        "count": len(prices),
        "prices": prices,
        # previous session's close, for today's % change (cycle-dip alerts)
        "prev_close": {k: v for k, v in PREV_CLOSE.items() if k in prices},
        # today's circuit band, so order links never carry a limit Kite would reject
        "circuits": {k: v for k, v in CIRCUITS.items() if k in prices},
    }
    os.makedirs("data", exist_ok=True)
    with open("data/prices_live.json", "w") as f:
        json.dump(output, f)
    print(f"  ✓ Written {len(prices)} live prices to data/prices_live.json")


if __name__ == "__main__":
    main()
