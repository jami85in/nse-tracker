"""
fetch_market_caps.py — build/refresh data/market_caps.json, used by
cycles.py for the "market cap >= Rs 10,000 crore" filter.

WHY YAHOO FINANCE
  Kite doesn't expose shares outstanding, and the worker's /marketcap route
  (fetch_shares_outstanding.py) has never produced data. yfinance reads
  Yahoo's quote data for NSE symbols ("<SYMBOL>.NS"), which GitHub Actions
  can reach. Market cap changes slowly relative to a Rs 10,000 cr cut-off,
  so a weekly refresh is plenty.

SCOPE
  Only symbols that could plausibly be >= Rs 10,000 cr are queried: non-ETF
  symbols whose average daily traded value (avg_volume x last close) is at
  least MCAP_MIN_TURNOVER_CR (default Rs 2 cr/day). Every Rs 10,000 cr stock
  trades far more than that, so this only skips the illiquid tail.

RESUMABLE
  Entries younger than MCAP_MAX_AGE_DAYS are skipped; progress is saved every
  25 symbols; a time budget stops the run cleanly. Failures are logged and
  leave the previous value in place.

OUTPUT
  {"updated_at": "...", "source": "yahoo",
   "caps": {"RELIANCE": {"mcap_cr": 1934567.0, "as_of": "2026-11-03"}, ...}}
"""
import os, json, time, datetime, glob

CACHE = "data/market_caps.json"
META = "data/symbols_meta.json"
RAW_DIR = "data/backtest/raw_ohlc"
MIN_TURNOVER_CR = float(os.environ.get("MCAP_MIN_TURNOVER_CR", "2"))
MAX_AGE_DAYS = int(os.environ.get("MCAP_MAX_AGE_DAYS", "6"))
TIME_BUDGET = int(os.environ.get("MCAP_TIME_BUDGET_SECONDS", "900"))


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save(cache):
    cache["updated_at"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(CACHE, "w") as f:
        json.dump(cache, f, separators=(",", ":"), sort_keys=True)


def candidates(meta):
    out = []
    for p in glob.glob(os.path.join(RAW_DIR, "*.json")):
        sym = os.path.basename(p)[:-5]
        m = meta.get(sym, {})
        if m.get("is_etf") or m.get("is_etf_nse"):
            continue
        bars = load_json(p, [])
        if not bars:
            continue
        try:
            close = float(bars[-1]["close"])
        except (KeyError, TypeError, ValueError):
            continue
        vol = m.get("avg_volume")
        if vol is None:
            out.append((0, sym))          # unknown liquidity: still try
            continue
        turnover_cr = vol * close / 1e7
        if turnover_cr >= MIN_TURNOVER_CR:
            out.append((turnover_cr, sym))
    out.sort(reverse=True)               # biggest first, so a cut-off run covers the most important names
    return [s for _, s in out]


def fetch_one(yf, sym):
    t = yf.Ticker(f"{sym}.NS")
    mc = None
    try:
        mc = t.fast_info.get("marketCap") if hasattr(t.fast_info, "get") else t.fast_info.market_cap
    except Exception:
        mc = None
    if not mc:
        try:
            mc = (t.info or {}).get("marketCap")
        except Exception:
            mc = None
    if mc and mc > 0:
        return round(mc / 1e7, 1)      # rupees -> crore
    return None


def main():
    import yfinance as yf
    meta = load_json(META, {}) or {}
    cache = load_json(CACHE, {}) or {}
    cache.setdefault("source", "yahoo")
    caps = cache.setdefault("caps", {})
    today = datetime.date.today()
    todo = []
    for sym in candidates(meta):
        rec = caps.get(sym)
        if rec and rec.get("as_of"):
            try:
                age = (today - datetime.date.fromisoformat(rec["as_of"])).days
                if age < MAX_AGE_DAYS:
                    continue
            except ValueError:
                pass
        todo.append(sym)
    print(f"{len(todo)} symbols to refresh (cache has {len(caps)})")
    start, ok, fail = time.time(), 0, 0
    for i, sym in enumerate(todo, 1):
        if time.time() - start > TIME_BUDGET:
            print(f"time budget reached after {i - 1} symbols — will resume next run")
            break
        try:
            v = fetch_one(yf, sym)
        except Exception as e:
            v = None
            print(f"  {sym}: {e}")
        if v:
            caps[sym] = {"mcap_cr": v, "as_of": today.isoformat()}
            ok += 1
        else:
            fail += 1
        if i % 25 == 0:
            save(cache)
            print(f"  {i}/{len(todo)}  ok={ok} fail={fail}")
        time.sleep(0.25)
    save(cache)
    big = sum(1 for v in caps.values() if v.get("mcap_cr", 0) >= 10000)
    print(f"done: ok={ok} fail={fail}; {len(caps)} cached, {big} at >= Rs 10,000 cr")
    if ok == 0 and fail > 0 and not caps:
        print("::warning::No market caps fetched — cycles.py will fall back to a liquidity proxy")


if __name__ == "__main__":
    main()
