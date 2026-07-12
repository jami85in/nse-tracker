"""
fetch_shares_outstanding.py — build/refresh data/shares_outstanding.json, the
per-symbol shares-outstanding cache that market cap is computed from.

WHY THIS IS SEPARATE FROM THE DAILY build_symbols_meta.py RUN
---------------------------------------------------------------
Shares outstanding barely changes day to day (only on buybacks, further
issues, bonuses/splits). So it doesn't need fetching daily — it needs
fetching ONCE per symbol, then occasional refreshes (weekly/monthly is
plenty). This script does that slow, resumable backfill; build_symbols_meta.py
then just multiplies the cached shares_outstanding by the day's price to get
market_cap_cr, with zero extra network calls.

HOW IT FETCHES
--------------
Goes through worker.js's new /marketcap endpoint (NOT Kite — Kite doesn't
expose shares outstanding — and NOT the plain bhavcopy archive, which also
doesn't carry it). That endpoint hits NSE's LIVE quote-equity API, which
needs a session-cookie warmup to get past NSE's anti-bot check — a genuinely
different mechanism from the static-file endpoints this worker already
proxies for OHLC/indices/symbols. This is new, less-tested surface area; if
it turns out NSE blocks the Worker's IP for the live API even with cookies,
this script will simply keep failing gracefully (errors logged, cache
untouched, filter stays inactive) — the manual CSV route
(data/market_cap_cr.csv) remains the fallback either way.

RESUMABLE: processes symbols missing from the cache in small batches
(worker-side cap: 20/call), respects a time budget, and saves progress after
every batch — a killed/timed-out run picks up where it left off next time.
"""
import os, sys, json, time, urllib.request, urllib.error, urllib.parse

CACHE_PATH = "data/shares_outstanding.json"
SYMBOLS_ALL = "data/backtest/symbols_all.json"
WORKER_URL = os.environ.get("BACKTEST_WORKER_URL", "").rstrip("/")
BATCH_SIZE = 20  # matches the worker's per-call cap
TIME_BUDGET_SECONDS = int(os.environ.get("SHARES_TIME_BUDGET_SECONDS", "420"))
REFRESH_ALL = os.environ.get("SHARES_REFRESH_ALL", "").lower() == "true"


def load_json(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f)


def fetch_batch(symbols):
    url = f"{WORKER_URL}/marketcap?symbols={urllib.parse.quote(','.join(symbols))}"
    req = urllib.request.Request(url, headers={"User-Agent": "nse-tracker-meta/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        return {"error": str(e)}


def main():
    if not WORKER_URL:
        print("ERROR: BACKTEST_WORKER_URL not set."); sys.exit(1)

    universe = load_json(SYMBOLS_ALL, [])
    if not universe:
        print("ERROR: symbols_all.json missing/empty."); sys.exit(1)

    cache = load_json(CACHE_PATH, {})
    if REFRESH_ALL:
        todo = list(universe)
        print(f"SHARES_REFRESH_ALL=true — refreshing all {len(todo)} symbols.")
    else:
        todo = [s for s in universe if s not in cache]
        print(f"{len(cache)} symbols already cached. {len(todo)} remaining.")

    if not todo:
        print("Nothing to do."); return

    start = time.time()
    fetched, failed = 0, 0
    batches_done = 0
    for i in range(0, len(todo), BATCH_SIZE):
        if time.time() - start > TIME_BUDGET_SECONDS:
            print(f"Time budget ({TIME_BUDGET_SECONDS}s) reached after "
                  f"{batches_done} batch(es); stopping — will resume next run.")
            break
        batch = todo[i:i + BATCH_SIZE]
        result = fetch_batch(batch)
        batches_done += 1

        if "error" in result and "data" not in result:
            print(f"  batch {batches_done}: request failed ({result['error']}); "
                  f"symbols left uncached for this batch, will retry next run.")
            failed += len(batch)
            # Keep going — a transient failure on one batch shouldn't stop
            # the whole run — but don't hammer NSE if something's
            # structurally wrong (e.g. cookie warmup broken).
            if failed >= 3 * BATCH_SIZE and fetched == 0:
                print("  3+ consecutive failed batches with zero successes — "
                      "likely a structural issue (session cookie / NSE blocking "
                      "the Worker). Stopping early rather than retrying "
                      "hundreds of times.")
                break
            time.sleep(3)
            continue

        for sym, info in result.get("data", {}).items():
            if info.get("issuedSize"):
                cache[sym] = {"issued_size": info["issuedSize"],
                              "fetched_price": info.get("price")}
                fetched += 1
        for sym, err in result.get("errors", {}).items():
            print(f"    {sym}: {err}")
            failed += 1

        save_json(CACHE_PATH, cache)  # persist after every batch, not just at the end
        print(f"  batch {batches_done}: {result.get('count', 0)} ok, "
              f"{result.get('error_count', 0)} failed "
              f"(running total cached: {len(cache)})")

    print(f"\nDone this run: {fetched} newly cached, {failed} failed. "
          f"Total cached: {len(cache)}/{len(universe)}.")


if __name__ == "__main__":
    main()
