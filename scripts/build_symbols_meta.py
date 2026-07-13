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
  • market_cap  — computed daily as issued_size × today's price, using a
                  shares-outstanding cache built separately by
                  fetch_shares_outstanding.py (see that file for why it's a
                  separate, occasional job rather than a daily fetch — shares
                  outstanding barely changes day to day). If a symbol isn't in
                  that cache yet, an optional CSV at data/market_cap_cr.csv
                  (SYMBOL,MARKET_CAP_CR) is used as a fallback. Symbols with
                  neither source are simply NOT cap-filtered (the scan only
                  excludes when it KNOWS a value is below the threshold), so
                  partial data is always safe.

Safe by design: a symbol missing volume/cap data is never dropped on that basis.
"""
import os, sys, json, csv, datetime

META_PATH   = "data/symbols_meta.json"
MCAP_CSV    = "data/market_cap_cr.csv"
SHARES_CACHE_PATH = "data/shares_outstanding.json"
SYMBOLS_ALL = "data/backtest/symbols_all.json"
API_KEY     = os.environ.get("KITE_API_KEY", "")
ROLL_DAYS   = int(os.environ.get("META_VOLUME_ROLL_DAYS", "20"))  # EMA horizon
BATCH       = 400  # Kite quote() supports up to 500/call


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
        # Trust ONLY Kite's own instrument_type (exchange-sourced ground
        # truth) plus the full descriptive NAME field. A prior version also
        # matched generic words (GOLD/SILVER/GILT/LIQUID/ETF/BEES/...)
        # against the raw TICKER, which wrongly flagged real companies whose
        # symbol happens to contain one of those substrings — confirmed in
        # production: GOLDIAM, SKYGOLD, SHANTIGOLD (ticker contains "GOLD"),
        # PNBGILTS ("GILT"), and JETFREIGHT ("ETF" literally appears inside
        # "J-ETF-REIGHT"). The ticker is just a symbol; it proves nothing
        # about instrument type. NAME is the registered instrument name
        # from the exchange and is safe to substring-match.
        is_etf = (itype == "ETF") or ("ETF" in name)
        flags[s] = bool(is_etf)
    return flags


def fetch_today_quotes(kite, symbols):
    """Today's traded volume AND last price per symbol, via batched quote().
    Kite's quote() response already includes last_price alongside volume in
    the same payload — capturing both here is free (no extra API calls),
    and last_price is exactly what market-cap computation needs."""
    vol, price = {}, {}
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
            lp = data.get("last_price")
            if lp:
                price[sym] = float(lp)
    return vol, price


def compute_market_cap(meta, today_price):
    """market_cap_cr = issued_size × price / 1e7 (1 crore = 1e7), using the
    shares-outstanding cache built by fetch_shares_outstanding.py. Prefers
    today's live Kite price; falls back to the price that was captured
    alongside issued_size at cache-build time if today's isn't available
    (e.g. Kite token stale this run) — a slightly older price is still far
    better than no market-cap value at all for a slow-moving figure like
    this. Returns the count of symbols updated."""
    shares = load_json(SHARES_CACHE_PATH, {})
    if not shares:
        print(f"  {SHARES_CACHE_PATH} not present/empty — run "
              f"fetch_shares_outstanding.py first. Market cap stays inactive "
              f"for symbols not covered by the CSV fallback.")
        return 0
    n = 0
    for sym, s in shares.items():
        issued = s.get("issued_size")
        if not issued:
            continue
        price = today_price.get(sym) or s.get("fetched_price")
        if not price:
            continue
        mcap_cr = round(issued * price / 1e7, 2)
        meta.setdefault(sym, {})["market_cap_cr"] = mcap_cr
        n += 1
    print(f"  market_cap_cr computed for {n} symbols from shares_outstanding cache.")
    return n


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


def merge_nse_etf_names(meta):
    """Merge the STABLE, authoritative is_etf_nse signal (from NSE's own
    security names, fetched separately/infrequently by
    fetch_security_names.py — see that file's docstring). Read-only here:
    this daily run never re-derives or overwrites this signal, only reads
    it. Stored as a SEPARATE key (is_etf_nse) rather than overwriting
    is_etf, so it's always clear which source said what."""
    path = "data/nse_security_names.json"
    if not os.path.exists(path):
        print(f"  {path} not present — run fetch_security_names.py first. "
              f"is_etf_nse stays absent (safe: falls through to the other "
              f"ETF signals in scan.py's symbol_excluded()).")
        return 0
    try:
        data = json.load(open(path))
    except Exception as e:
        print(f"  {path} unreadable ({e}); skipping.")
        return 0
    is_etf_nse = data.get("is_etf_nse", {})
    n = 0
    for sym, flag in is_etf_nse.items():
        meta.setdefault(sym, {})["is_etf_nse"] = bool(flag)
        n += 1
    print(f"  merged is_etf_nse for {n} symbols from {path} "
          f"({sum(1 for v in is_etf_nse.values() if v)} flagged ETF).")
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

    today_price = {}
    if kite:
        # ETF flags (authoritative)
        flags = mark_etfs(kite, symbols)
        for s, f in flags.items():
            meta.setdefault(s, {})["is_etf"] = f
        print(f"  ETF flags set for {len(flags)} symbols.")

        # Rolling average volume (EMA so a daily run converges to ~ROLL_DAYS avg)
        # + today's price, captured together at no extra API cost.
        today_vol, today_price = fetch_today_quotes(kite, symbols)
        alpha = 2.0 / (ROLL_DAYS + 1)
        updated = 0
        for s, v in today_vol.items():
            e = meta.setdefault(s, {})
            prev = e.get("avg_volume")
            e["avg_volume"] = round(v if prev is None else (alpha * v + (1 - alpha) * prev), 1)
            updated += 1
        print(f"  rolling avg_volume updated for {updated} symbols.")

    # Stable, NSE-sourced ETF signal — read-only merge, never re-derived here.
    merge_nse_etf_names(meta)

    # CSV first as a baseline/fallback, THEN the computed (issued_size ×
    # price) value overwrites it where available — the computed source is
    # fresher and automated, the CSV just fills gaps ahead of it.
    merge_market_cap(meta)
    compute_market_cap(meta, today_price)

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
