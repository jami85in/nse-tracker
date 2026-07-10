"""
build_symbols_meta.py — build/refresh data/symbols_meta.json, the metadata that
powers the scan's universe-quality filters (ETF exclusion, market cap ≥ ₹1000cr,
avg volume ≥ 10000 shares).

symbols_meta.json shape: { "SYMBOL": {"is_etf": bool,
                                       "avg_volume": float,      # rolling ~20d
                                       "market_cap_cr": float}}  # optional

Data sources (only what's reliably reachable from GitHub Actions):
  • is_etf      — Kite instruments dump (authoritative: instrument_type / name).
  • avg_volume  — Kite quote() gives each symbol's traded volume TODAY. We keep a
                  rolling exponential average in the meta file, so running this
                  daily converges to a true ~20-day average with only a handful
                  of batched API calls (no 2000+ historical requests).
  • market_cap  — Kite does NOT expose market cap. If you drop a CSV at
                  data/market_cap_cr.csv with rows "SYMBOL,MARKET_CAP_CR"
                  (from screener/Tickertape/NSE, refreshed occasionally), it's
                  merged in. Symbols without a market-cap value are simply NOT
                  cap-filtered (the scan only excludes when it KNOWS a value is
                  below the threshold), so partial data is safe and useful.

Safe by design: a symbol missing volume/cap data is never dropped on that basis.
"""
import os, sys, json, csv, datetime

META_PATH   = "data/symbols_meta.json"
MCAP_CSV    = "data/market_cap_cr.csv"
SYMBOLS_ALL = "data/backtest/symbols_all.json"
API_KEY     = os.environ.get("KITE_API_KEY", "")
ROLL_DAYS   = int(os.environ.get("META_VOLUME_ROLL_DAYS", "20"))  # EMA horizon
BATCH       = 400  # Kite quote() supports up to 500/call

ETF_NAME_MARKERS = ("ETF", "BEES", "LIQUID", "GOLD", "SILVER", "BHARATBOND", "GILT")


def load_json(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default


def get_symbols():
    syms = load_json(SYMBOLS_ALL, [])
    if not syms:
        print("ERROR: symbols_all.json missing/empty."); sys.exit(1)
    return syms


def mark_etfs(kite, symbols):
    """Authoritative ETF flags from Kite's NSE instrument dump."""
    flags = {}
    try:
        instruments = kite.instruments("NSE")
    except Exception as e:
        print(f"  instruments() failed ({e}); ETF flags left to the scan heuristic.")
        return flags
    by_sym = {i.get("tradingsymbol"): i for i in instruments}
    for s in symbols:
        info = by_sym.get(s)
        if not info:
            continue
        itype = (info.get("instrument_type") or "").upper()
        name = (info.get("name") or "").upper()
        seg = (info.get("segment") or "").upper()
        is_etf = (itype == "ETF") or ("ETF" in name) or \
                 (any(m in s.upper() for m in ETF_NAME_MARKERS) and "EQ" not in seg)
        flags[s] = bool(is_etf)
    return flags


def fetch_today_volume(kite, symbols):
    """Today's traded volume per symbol via batched quote()."""
    vol = {}
    for i in range(0, len(symbols), BATCH):
        batch = [f"NSE:{s}" for s in symbols[i:i + BATCH]]
        try:
            q = kite.quote(batch)
        except Exception as e:
            print(f"  quote batch {i//BATCH} failed ({e}); skipping.")
            continue
        for key, data in q.items():
            sym = key.split(":", 1)[-1]
            v = data.get("volume")
            if v is not None:
                vol[sym] = float(v)
    return vol


def merge_market_cap(meta):
    """Merge market_cap_cr from an optional committed CSV (SYMBOL,MCAP_CR)."""
    if not os.path.exists(MCAP_CSV):
        print(f"  {MCAP_CSV} not present — market-cap filter stays inactive "
              f"(safe: nothing excluded on market-cap basis).")
        return 0
    n = 0
    with open(MCAP_CSV) as f:
        for row in csv.reader(f):
            if len(row) < 2:
                continue
            sym = row[0].strip().upper()
            if sym in ("SYMBOL", ""):
                continue
            try:
                mcap = float(str(row[1]).replace(",", "").strip())
            except ValueError:
                continue
            meta.setdefault(sym, {})["market_cap_cr"] = mcap
            n += 1
    print(f"  merged market cap for {n} symbols from {MCAP_CSV}.")
    return n


def main():
    from kiteconnect import KiteConnect
    from kite_token_reader import get_kite_token

    symbols = get_symbols()
    meta = load_json(META_PATH, {})
    print(f"Building metadata for {len(symbols)} symbols "
          f"({len(meta)} already have entries).")

    access_token, is_stale = get_kite_token()
    kite = None
    if access_token and not is_stale:
        kite = KiteConnect(api_key=API_KEY)
        kite.set_access_token(access_token)
    else:
        print("  No fresh Kite token — updating market cap from CSV only, "
              "preserving existing volume/ETF data.")

    if kite:
        # ETF flags (authoritative)
        flags = mark_etfs(kite, symbols)
        for s, f in flags.items():
            meta.setdefault(s, {})["is_etf"] = f
        print(f"  ETF flags set for {len(flags)} symbols.")

        # Rolling average volume (EMA so a daily run converges to ~ROLL_DAYS avg)
        today_vol = fetch_today_volume(kite, symbols)
        alpha = 2.0 / (ROLL_DAYS + 1)
        updated = 0
        for s, v in today_vol.items():
            e = meta.setdefault(s, {})
            prev = e.get("avg_volume")
            e["avg_volume"] = round(v if prev is None else (alpha * v + (1 - alpha) * prev), 1)
            updated += 1
        print(f"  rolling avg_volume updated for {updated} symbols.")

    merge_market_cap(meta)

    meta["_meta"] = {"generated": datetime.datetime.now().isoformat(),
                     "roll_days": ROLL_DAYS,
                     "symbols_with_volume": sum(1 for k, v in meta.items()
                                                if isinstance(v, dict) and v.get("avg_volume")),
                     "symbols_with_mcap": sum(1 for k, v in meta.items()
                                              if isinstance(v, dict) and v.get("market_cap_cr"))}
    os.makedirs("data", exist_ok=True)
    with open(META_PATH, "w") as f:
        json.dump(meta, f)
    print(f"Wrote {META_PATH}: {meta['_meta']}")


if __name__ == "__main__":
    main()
