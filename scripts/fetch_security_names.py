"""
fetch_security_names.py — build/refresh data/nse_security_names.json, the
STABLE, authoritative NSE-sourced signal for ETF detection.

WHY THIS EXISTS (see chat for the full story): Kite's own instrument_type
classification has repeatedly missed real ETFs (SBINMID150, BANK10ADD,
AARTISURF, MOBANK10, INFRA all had Kite is_etf=False despite being genuine
ETFs) — it's Kite's own secondary/derived tagging, not authoritative. This
script instead pulls NSE's own securities master (EQUITY_L.csv, via
worker.js's /security_names endpoint, a plain static archive file — NOT the
anti-bot-protected live API that blocked /marketcap). A genuine ETF's
OFFICIAL LISTED NAME always says "ETF" (e.g. MOBANK10's real NSE name is
"Motilal Oswal BSE Top 10 Banks ETF"), even when neither the ticker nor
Kite's classification gives any hint.

WHY SEPARATE FROM THE DAILY build_symbols_meta.py RUN: this data changes
only when new securities list/delist — essentially never day to day. It
should NOT be re-derived by (or vulnerable to being reset by) the daily
Kite-driven price/volume refresh. Runs weekly, writes its own file, which
build_symbols_meta.py then reads (read-only) and merges in.
"""
import os, sys, json, urllib.request, urllib.error

OUT_PATH = "data/nse_security_names.json"
WORKER_URL = os.environ.get("BACKTEST_WORKER_URL", "").rstrip("/")


def main():
    if not WORKER_URL:
        print("ERROR: BACKTEST_WORKER_URL not set."); sys.exit(1)

    url = f"{WORKER_URL}/security_names"
    req = urllib.request.Request(url, headers={"User-Agent": "nse-tracker-meta/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            result = json.loads(r.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        print(f"ERROR: fetch failed ({e}). Leaving any existing {OUT_PATH} untouched "
              f"— safe degradation, is_etf_nse just stays absent for now.")
        sys.exit(1)

    if "error" in result:
        print(f"ERROR from worker: {result['error']}. Leaving existing data untouched.")
        sys.exit(1)

    names = result.get("names", {})
    if not names or len(names) < 1000:  # sanity floor — NSE has 2000+ listed EQ securities
        print(f"ERROR: got only {len(names)} names — suspiciously low, likely a bad/partial "
              f"fetch. Refusing to overwrite existing data with this.")
        sys.exit(1)

    is_etf_nse = {sym: ("ETF" in name.upper()) for sym, name in names.items()}
    n_etf = sum(1 for v in is_etf_nse.values() if v)

    os.makedirs("data", exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump({"names": names, "is_etf_nse": is_etf_nse, "count": len(names),
                   "etf_count": n_etf}, f)
    print(f"Wrote {OUT_PATH}: {len(names)} securities, {n_etf} flagged as ETF by official name.")


if __name__ == "__main__":
    main()
