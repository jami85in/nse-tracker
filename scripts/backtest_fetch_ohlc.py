#!/usr/bin/env python3
"""
Incremental historical backtest data collector — OHLC version.

Upgraded from close-only to full OHLC (open, high, low, close) per day per
symbol, now that the Cloudflare Worker exposes those fields from the
bhavcopy. This enables a proper (not close-proxy) stop-loss simulation
later, since real stops trigger on intraday low, not just the close.

Also supports the FULL NSE equity universe (not just Nifty 500) via the
Worker's /symbols?universe=all endpoint — set BACKTEST_UNIVERSE=all to
track every EQ-series stock (~2000+), covering names like WALCHANNAG that
aren't Nifty 500 members.

Same resumable, multi-chunk-per-run design as before. Existing close-only
raw_closes/*.json files are left in place (harmless, superseded going
forward) — new data lands in raw_ohlc/*.json.
"""
import json, os, sys, time, datetime, urllib.request, urllib.error, urllib.parse

WORKER_URL = os.environ.get("BACKTEST_WORKER_URL", "").rstrip("/")
CHUNK_DAYS = int(os.environ.get("BACKTEST_CHUNK_DAYS", "28"))
START_DATE = os.environ.get("BACKTEST_START_DATE", "2020-09-01")
UNIVERSE = os.environ.get("BACKTEST_UNIVERSE", "nifty500")  # "nifty500" or "all"

DATA_DIR = "data/backtest"
OHLC_DIR = os.path.join(DATA_DIR, "raw_ohlc")
PROGRESS_PATH = os.path.join(DATA_DIR, "progress_ohlc.json")  # separate progress file — new collection pass
SYMBOLS_PATH = os.path.join(DATA_DIR, "symbols_all.json" if UNIVERSE == "all" else "symbols.json")


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


def get_symbol_universe():
    syms = load_json(SYMBOLS_PATH, None)
    if syms:
        return syms
    try:
        universe_param = "?universe=all&debug=1" if UNIVERSE == "all" else ""
        url = f"{WORKER_URL}/symbols{universe_param}"
        req = urllib.request.Request(url, headers={"User-Agent": "nse-tracker-backtest/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
        if data.get("error"):
            print(f"Worker returned an error fetching symbols: {data['error']}")
            if data.get("debug"):
                print(f"Worker debug log: {data['debug']}")
            return []
        if data.get("symbols"):
            universe = sorted(data["symbols"])
            save_json(SYMBOLS_PATH, universe)
            print(f"Seeded {len(universe)} symbols ({UNIVERSE} universe) via Worker, as_of={data.get('as_of')}")
            return universe
        print(f"Worker responded but had no 'symbols' field. Raw response: {json.dumps(data)[:500]}")
    except urllib.error.HTTPError as e:
        body = e.read().decode() if hasattr(e, "read") else ""
        print(f"HTTP error fetching symbol list: {e.code} {e.reason}. Body: {body[:500]}")
    except Exception as e:
        print(f"Could not fetch symbol list via Worker: {type(e).__name__}: {e}")

    if UNIVERSE == "all":
        print("Falling back to the Nifty 500 list so collection can still proceed "
              "(full-universe expansion can be retried once the /symbols?universe=all "
              "endpoint issue is fixed).")
        try:
            url = f"{WORKER_URL}/symbols"
            req = urllib.request.Request(url, headers={"User-Agent": "nse-tracker-backtest/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode())
            if data.get("symbols"):
                universe = sorted(data["symbols"])
                save_json(SYMBOLS_PATH, universe)
                print(f"Fallback: seeded {len(universe)} Nifty 500 symbols.")
                return universe
        except Exception as e2:
            print(f"Fallback also failed: {e2}")
    return []


def fetch_chunk(start_str, end_str, symbols):
    symbols_param = ",".join(symbols)
    url = (f"{WORKER_URL}/backtest?start={start_str}&end={end_str}"
           f"&symbols={urllib.parse.quote(symbols_param)}")
    req = urllib.request.Request(url, headers={"User-Agent": "nse-tracker-backtest/1.0"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read().decode())


def merge_ohlc(chunk_ohlc):
    updated = set()
    for sym, series in chunk_ohlc.items():
        path = os.path.join(OHLC_DIR, f"{sym}.json")
        existing = load_json(path, [])
        by_date = {pt["date"]: pt for pt in existing}
        for pt in series:
            by_date[pt["date"]] = pt
        merged = [by_date[d] for d in sorted(by_date.keys())]
        save_json(path, merged)
        updated.add(sym)
    return updated


def process_one_chunk(symbols, progress):
    today = datetime.date.today()
    chunk_start = datetime.date.fromisoformat(progress["next_chunk_start"])
    chunk_end = min(chunk_start + datetime.timedelta(days=CHUNK_DAYS - 1), today - datetime.timedelta(days=1))

    if chunk_start > today - datetime.timedelta(days=1):
        progress["done"] = True
        progress["last_run"] = datetime.datetime.now().isoformat()
        print("Reached today's date. OHLC collection complete.")
        return "done"

    cs, ce = chunk_start.isoformat(), chunk_end.isoformat()
    print(f"Fetching OHLC chunk {cs} -> {ce} for {len(symbols)} symbols...")

    try:
        result = fetch_chunk(cs, ce, symbols)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        print(f"  Chunk fetch FAILED: {e}. Will retry.")
        return "retry"

    if result.get("error"):
        print(f"  Worker error: {result['error']}. Will retry.")
        return "retry"

    ohlc = result.get("ohlc", {})
    trading_days_fetched = result.get("trading_days_fetched", 0)
    print(f"  fetched={trading_days_fetched}, failed={result.get('trading_days_failed',0)}, symbols_in_response={len(ohlc)}")

    if trading_days_fetched == 0:
        progress["current_chunk_failures"] = progress.get("current_chunk_failures", 0) + 1
        if progress["current_chunk_failures"] >= 4:
            print(f"  Chunk failed 4x — skipping past it (data likely doesn't exist for this range).")
            progress["next_chunk_start"] = (chunk_end + datetime.timedelta(days=1)).isoformat()
            progress["chunks_skipped"] = progress.get("chunks_skipped", 0) + 1
            progress["current_chunk_failures"] = 0
            return "skipped"
        return "retry"

    updated = merge_ohlc(ohlc)
    print(f"  merged OHLC for {len(updated)} symbols this chunk")

    progress["next_chunk_start"] = (chunk_end + datetime.timedelta(days=1)).isoformat()
    progress["chunks_completed"] = progress.get("chunks_completed", 0) + 1
    progress["current_chunk_failures"] = 0
    progress["last_run"] = datetime.datetime.now().isoformat()

    total_days_est = (today - datetime.date.fromisoformat(START_DATE)).days
    done_days = (chunk_end - datetime.date.fromisoformat(START_DATE)).days
    pct = round(done_days / total_days_est * 100, 1) if total_days_est > 0 else 100
    print(f"  Progress: {pct}% ({progress['chunks_completed']} chunks done)")
    return "advanced"


def main():
    if not WORKER_URL:
        print("ERROR: BACKTEST_WORKER_URL not set.")
        sys.exit(1)

    symbols = get_symbol_universe()
    if not symbols:
        print("ERROR: no symbol universe found.")
        sys.exit(1)
    print(f"Tracking {len(symbols)} symbols (universe={UNIVERSE}).")

    progress = load_json(PROGRESS_PATH, {
        "start_date": START_DATE, "next_chunk_start": START_DATE,
        "chunks_completed": 0, "chunks_skipped": 0,
        "current_chunk_failures": 0, "last_run": None, "done": False,
        "universe": UNIVERSE,
    })

    if progress.get("done"):
        print("OHLC collection already complete. Nothing to do.")
        return

    TIME_BUDGET_SECONDS = int(os.environ.get("BACKTEST_TIME_BUDGET_SECONDS", "480"))
    PAUSE_BETWEEN_CHUNKS_SECONDS = int(os.environ.get("BACKTEST_PAUSE_SECONDS", "5"))

    start_time = time.time()
    chunks_this_run = 0

    while True:
        if time.time() - start_time > TIME_BUDGET_SECONDS:
            print(f"\nTime budget reached after {chunks_this_run} chunk(s). Stopping — next run continues.")
            break
        status = process_one_chunk(symbols, progress)
        save_json(PROGRESS_PATH, progress)
        if status == "done":
            break
        elif status in ("advanced", "skipped"):
            chunks_this_run += 1
        time.sleep(PAUSE_BETWEEN_CHUNKS_SECONDS)

    print(f"\nRun summary: {chunks_this_run} chunk(s) processed in {time.time()-start_time:.0f}s.")


if __name__ == "__main__":
    main()
