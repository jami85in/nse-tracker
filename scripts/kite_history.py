#!/usr/bin/env python3
"""
kite_history.py — daily price history from Kite for holdings the NSE price
archive doesn't cover, so the Ranges tab can test them too.

Why: data/backtest/raw_ohlc/ is built from NSE's main-board (EQ) bhavcopy,
so NSE SME (EMERGE, "-SM"/"-ST") holdings such as FLYSBS-SM have no history
there. Kite has their full daily candles, plus the two things that matter
most for an SME stock: the lot size (SME shares trade only in lots) and the
traded volume (thin trading decides whether a limit order actually fills).

Writes, for each such symbol (name without the -SM/-ST suffix):
  data/ranges/extra_ohlc/<SYMBOL>.json  [{date, open, high, low, close, volume}, ...]
  data/ranges/extra_meta.json           {SYMBOL: {tradingsymbol, exchange, lot_size,
                                          tick_size, first_date, last_date, bars, updated_at}}
Incremental: after the first run only the last few weeks are re-fetched.

Uses today's Kite login token (kite_token_reader). With no usable token it
changes nothing and exits 0, so the ranges job still runs on what it has.
Only symbols, dates and counts are printed — no quantities or prices.
"""
import datetime as dt
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kite_token_reader import get_kite_token  # noqa: E402

HOLDINGS = "data/zerodha_holdings.json"
WATCH = "data/ranges/watch.txt"
RAW_DIR = "data/backtest/raw_ohlc"
OUT_DIR = "data/ranges/extra_ohlc"
META = "data/ranges/extra_meta.json"
START = dt.date(2020, 9, 1)          # same start as the main archive
WINDOW_DAYS = 1900                   # Kite allows up to 2000 days of daily candles per call
REFETCH_DAYS = 21                    # re-read the last three weeks each run (late corrections)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SUFFIX = re.compile(r"-(SM|ST|BE|BZ|EQ)$")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(tmp, path)


def base(sym):
    return SUFFIX.sub("", str(sym or "").strip().upper())


def wanted():
    """(base symbol, Kite tradingsymbol or None, exchange) for holdings and watch.txt
    entries with no file in the main archive."""
    out, seen = [], set()
    h = load_json(HOLDINGS, {}) or {}
    hold = h.get("holdings") or {}
    items = hold.items() if isinstance(hold, dict) else [(x, {}) for x in hold]
    for ts, info in items:
        b = base(ts)
        if b and b not in seen:
            seen.add(b)
            out.append((b, str(ts).strip().upper(), (info or {}).get("exchange") or "NSE"))
    if os.path.exists(WATCH):
        for line in open(WATCH, encoding="utf-8"):
            s = line.split("#")[0].strip().upper()
            b = base(s)
            if b and b not in seen and re.fullmatch(r"[A-Z0-9&\-]+", b):
                seen.add(b)
                out.append((b, s if s != b else None, "NSE"))
    return [w for w in out if not os.path.exists(os.path.join(RAW_DIR, f"{w[0]}.json"))]


def find_instrument(index, b, ts, exch):
    """Exact tradingsymbol first; otherwise the base name, then -SM / -ST, on NSE then BSE."""
    tries = []
    if ts:
        tries += [(exch, ts), ("NSE", ts), ("BSE", ts)]
    for e in ("NSE", "BSE"):
        tries += [(e, b), (e, f"{b}-SM"), (e, f"{b}-ST")]
    for key in tries:
        if key in index:
            return index[key]
    return None


def candles(kite, token, start, end):
    bars = []
    s = start
    while s <= end:
        e = min(s + dt.timedelta(days=WINDOW_DAYS - 1), end)
        for attempt in range(3):
            try:
                rows = kite.historical_data(token, s, e, "day")
                break
            except Exception:                       # network blip or rate limit: back off
                if attempt == 2:
                    raise
                time.sleep(2 * (attempt + 1))
        for r in rows:
            d = r["date"]
            d = d.date() if hasattr(d, "date") else dt.date.fromisoformat(str(d)[:10])
            bars.append({"date": d.isoformat(), "open": float(r["open"]), "high": float(r["high"]),
                         "low": float(r["low"]), "close": float(r["close"]), "volume": int(r.get("volume") or 0)})
        s = e + dt.timedelta(days=1)
        time.sleep(0.4)                              # stay under Kite's 3 requests/second
    return bars


def main():
    todo = wanted()
    print(f"kite_history: {len(todo)} holding/watch symbol(s) not in the NSE archive: {', '.join(t[0] for t in todo) or 'none'}")
    if not todo:
        return 0
    api_key = os.environ.get("KITE_API_KEY", "")
    token, stale = get_kite_token()
    if not api_key or not token:
        print("kite_history: no Kite key/token available — keeping the existing files.")
        return 0
    if stale:
        print("kite_history: today's Kite login hasn't happened yet — trying the last token anyway.")
    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(token)

    exchanges = sorted({"NSE", "BSE"} | {t[2] for t in todo if t[2] in ("NSE", "BSE")})
    index = {}
    try:
        for e in exchanges:
            for r in kite.instruments(e):
                index[(e, str(r.get("tradingsymbol") or "").upper())] = r
    except Exception as ex:
        print(f"kite_history: couldn't read Kite's instrument list ({type(ex).__name__}: {ex}) — "
              "the login token has probably expired; keeping the existing files.")
        return 0

    meta = load_json(META, {}) or {}
    now = dt.datetime.now(IST)
    # Before the close, today's candle is still forming — stop at yesterday.
    today = now.date() if (now.hour, now.minute) >= (15, 40) else now.date() - dt.timedelta(days=1)
    done = 0
    for b, ts, exch in todo:
        ins = find_instrument(index, b, ts, exch)
        if not ins:
            print(f"  {b}: not found on Kite (NSE/BSE) — skipped")
            continue
        path = os.path.join(OUT_DIR, f"{b}.json")
        old = load_json(path, []) or []
        start = START
        if old:
            start = max(START, dt.date.fromisoformat(old[-1]["date"]) - dt.timedelta(days=REFETCH_DAYS))
        try:
            new = candles(kite, ins["instrument_token"], start, today)
        except Exception as ex:
            print(f"  {b}: history request failed ({type(ex).__name__}: {ex}) — kept what was there")
            continue
        by_date = {x["date"]: x for x in old}
        for x in new:
            by_date[x["date"]] = x
        bars = [by_date[d] for d in sorted(by_date)]
        if not bars:
            print(f"  {b}: Kite returned no candles — skipped")
            continue
        save_json(path, bars)
        meta[b] = {"tradingsymbol": ins["tradingsymbol"], "exchange": ins.get("exchange") or exch,
                   "lot_size": int(ins.get("lot_size") or 1), "tick_size": float(ins.get("tick_size") or 0.05),
                   "first_date": bars[0]["date"], "last_date": bars[-1]["date"], "bars": len(bars),
                   "updated_at": dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")}
        done += 1
        print(f"  {b}: {ins['exchange']}:{ins['tradingsymbol']} · {len(bars)} sessions "
              f"{bars[0]['date']} → {bars[-1]['date']} · lot {meta[b]['lot_size']}")
    save_json(META, meta)
    print(f"kite_history: updated {done} of {len(todo)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
