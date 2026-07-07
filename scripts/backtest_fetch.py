#!/usr/bin/env python3
"""
Incremental historical backtest data collector.

Runs on a schedule (every 5 min via GitHub Actions — GitHub's documented
minimum interval for scheduled workflows). Each invocation processes
MULTIPLE date chunks in a loop (not just one), since a single chunk only
takes a few seconds — this lets one run comfortably work through many
chunks back-to-back within a time budget, rather than waiting on GitHub's
scheduler (which is best-effort and often delayed) for every single chunk.

Each chunk:
1. Reads data/backtest/progress.json to find the next un-fetched date chunk
2. Calls the Cloudflare Worker (nse-backtest) for that chunk's raw daily closes
3. Merges the result into data/backtest/raw_closes/<SYMBOL>.json (one file
   per symbol, append-only, never overwritten wholesale)
4. Updates progress.json so the next chunk (or the next scheduled run, if
   the time budget runs out) picks up where this one left off

progress.json is saved to disk after EVERY chunk within a run (not just at
the end), so even if the job is killed mid-run, nothing is lost — the
workflow's commit step then pushes everything gathered so far.
"""
import json, os, sys, time, datetime, urllib.request, urllib.error

WORKER_URL = os.environ.get("BACKTEST_WORKER_URL", "").rstrip("/")
CHUNK_DAYS = int(os.environ.get("BACKTEST_CHUNK_DAYS", "28"))  # calendar days per chunk
START_DATE = os.environ.get("BACKTEST_START_DATE", "2020-09-01")  # how far back to go
# NOTE: NSE's sec_bhavdata_full_DDMMYYYY.csv URL scheme (used by this
# pipeline, via the Cloudflare Worker) only exists from ~August 2020 onward.
# Earlier dates return nothing no matter how many times we retry — this was
# confirmed by an all-20-days-failed chunk when we tried Jan 2019. Don't set
# BACKTEST_START_DATE earlier than 2020-09-01 or every chunk in that range
# will fail permanently and stall the collector (see the retry-cap logic
# below, which now catches this case rather than looping forever).
DATA_DIR = "data/backtest"
RAW_DIR = os.path.join(DATA_DIR, "raw_closes")
PROGRESS_PATH = os.path.join(DATA_DIR, "progress.json")
SYMBOLS_PATH = os.path.join(DATA_DIR, "symbols.json")  # full Nifty 500 list to track


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
    """Load the tracked symbol list. Priority:
    1. data/backtest/symbols.json if already seeded (stable across runs —
       we don't want the universe shifting mid-collection)
    2. Fetch the full current Nifty 500 list via the Worker's /symbols
       endpoint (one-time cost, cached to symbols.json thereafter)
    3. Fall back to whatever's in scan_latest.json if the above fails
    """
    syms = load_json(SYMBOLS_PATH, None)
    if syms:
        return syms

    try:
        url = f"{WORKER_URL}/symbols"
        req = urllib.request.Request(url, headers={"User-Agent": "nse-tracker-backtest/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        if data.get("symbols"):
            universe = sorted(data["symbols"])
            save_json(SYMBOLS_PATH, universe)
            print(f"Seeded {len(universe)} symbols from Nifty 500 list via Worker.")
            return universe
    except Exception as e:
        print(f"Could not fetch full Nifty 500 list via Worker ({e}), falling back to scan_latest.json.")

    try:
        with open("data/scan_latest.json") as f:
            scan = json.load(f)
        universe = set()
        for section in ["squeeze_stocks", "blast_stocks", "watchlist_stocks",
                        "short_squeeze_stocks", "short_breakdown_stocks"]:
            for s in scan.get(section, []):
                universe.add(s["symbol"])
        universe = sorted(universe)
        save_json(SYMBOLS_PATH, universe)
        return universe
    except Exception:
        return []


def fetch_chunk(start_str, end_str, symbols):
    """Call the Cloudflare Worker for one date-range chunk. Always requests
    debug=1 so failures come with a diagnostic log automatically — no need
    for a separate manual test when something goes wrong."""
    symbols_param = ",".join(symbols)
    url = (f"{WORKER_URL}/backtest?start={start_str}&end={end_str}"
           f"&symbols={urllib.parse.quote(symbols_param)}&debug=1")
    req = urllib.request.Request(url, headers={"User-Agent": "nse-tracker-backtest/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def merge_closes(chunk_closes):
    """Merge one chunk's {SYMBOL: [{date, close}, ...]} into per-symbol files."""
    updated = set()
    for sym, series in chunk_closes.items():
        path = os.path.join(RAW_DIR, f"{sym}.json")
        existing = load_json(path, [])
        by_date = {pt["date"]: pt["close"] for pt in existing}
        for pt in series:
            by_date[pt["date"]] = pt["close"]  # last-write-wins, harmless if identical
        merged = [{"date": d, "close": c} for d, c in sorted(by_date.items())]
        save_json(path, merged)
        updated.add(sym)
    return updated


def process_one_chunk(symbols, progress):
    """
    Process exactly one chunk. Returns one of:
      "done"       — reached today's date, collection complete
      "advanced"   — chunk succeeded, progress moved forward
      "skipped"    — chunk failed 4x in a row, skipped past it
      "retry"      — chunk failed, will retry same chunk next call
      "stall"      — days fetched but 0 symbols matched, needs investigation
    Mutates `progress` in place; caller is responsible for saving it.
    """
    today = datetime.date.today()
    chunk_start = datetime.date.fromisoformat(progress["next_chunk_start"])
    chunk_end = min(chunk_start + datetime.timedelta(days=CHUNK_DAYS - 1), today - datetime.timedelta(days=1))

    if chunk_start > today - datetime.timedelta(days=1):
        progress["done"] = True
        progress["last_run"] = datetime.datetime.now().isoformat()
        print("Reached today's date. Marking collection complete.")
        return "done"

    cs, ce = chunk_start.isoformat(), chunk_end.isoformat()
    print(f"Fetching chunk {cs} -> {ce} for {len(symbols)} symbols...")

    try:
        result = fetch_chunk(cs, ce, symbols)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        print(f"  Chunk fetch FAILED: {e}. Will retry same chunk next call.")
        return "retry"

    if result.get("error"):
        print(f"  Worker returned an error: {result['error']}. Will retry same chunk next call.")
        return "retry"

    closes = result.get("closes", {})
    trading_days_fetched = result.get("trading_days_fetched", 0)
    trading_days_failed = result.get("trading_days_failed", 0)

    print(f"  Worker response: trading_days_fetched={trading_days_fetched}, "
          f"trading_days_failed={trading_days_failed}, "
          f"symbols_in_response={len(closes)}")

    if trading_days_fetched == 0:
        progress["current_chunk_failures"] = progress.get("current_chunk_failures", 0) + 1
        if progress["current_chunk_failures"] >= 4:
            print(f"  Chunk {cs} -> {ce} failed {progress['current_chunk_failures']}x in a row. "
                  f"Assuming this data genuinely doesn't exist on NSE — skipping past it.")
            progress["next_chunk_start"] = (chunk_end + datetime.timedelta(days=1)).isoformat()
            progress["chunks_skipped"] = progress.get("chunks_skipped", 0) + 1
            progress["current_chunk_failures"] = 0
            progress["last_run"] = datetime.datetime.now().isoformat()
            return "skipped"
        else:
            print(f"  WARNING: zero trading days fetched for {cs} -> {ce} "
                  f"(failure {progress['current_chunk_failures']}/4). Will retry.")
            progress["last_run"] = datetime.datetime.now().isoformat()
            if result.get("debug_log"):
                print(f"  Worker debug log: {result['debug_log'][:10]}")
            return "retry"

    updated_syms = merge_closes(closes)
    print(f"  merged data for {len(updated_syms)} symbols this chunk")

    if len(updated_syms) == 0:
        print(f"  WARNING: {trading_days_fetched} days fetched but 0 symbols merged — "
              f"possible symbol-matching mismatch. Not advancing; investigate.")
        return "stall"

    progress["next_chunk_start"] = (chunk_end + datetime.timedelta(days=1)).isoformat()
    progress["chunks_completed"] = progress.get("chunks_completed", 0) + 1
    progress["current_chunk_failures"] = 0
    progress["last_run"] = datetime.datetime.now().isoformat()

    total_days_est = (today - datetime.date.fromisoformat(START_DATE)).days
    done_days = (chunk_end - datetime.date.fromisoformat(START_DATE)).days
    pct = round(done_days / total_days_est * 100, 1) if total_days_est > 0 else 100
    print(f"  Progress: {pct}% ({progress['chunks_completed']} chunks done, "
          f"next chunk starts {progress['next_chunk_start']})")
    return "advanced"


def main():
    if not WORKER_URL:
        print("ERROR: BACKTEST_WORKER_URL not set. Add it as a repo secret or workflow env var.")
        sys.exit(1)

    symbols = get_symbol_universe()
    if not symbols:
        print("ERROR: no symbol universe found. Seed data/backtest/symbols.json with the "
              "full Nifty 500 list to proceed.")
        sys.exit(1)
    print(f"Tracking {len(symbols)} symbols.")

    progress = load_json(PROGRESS_PATH, {
        "start_date": START_DATE,
        "next_chunk_start": START_DATE,
        "chunks_completed": 0,
        "chunks_skipped": 0,
        "current_chunk_failures": 0,
        "last_run": None,
        "done": False,
    })

    if progress.get("done"):
        print("Backtest data collection already complete through today. Nothing to do.")
        print("(Delete or edit progress.json's 'done' flag to extend the range.)")
        return

    # Process multiple chunks per invocation instead of just one — each
    # chunk only takes a few seconds (mostly small polite delays talking to
    # NSE via the Worker), so a single ~10-minute GitHub Actions run can
    # comfortably work through many chunks back-to-back. This dramatically
    # speeds up total collection time versus waiting for GitHub's scheduler
    # to fire a fresh run for every single chunk (schedule intervals are
    # inherently best-effort and often delayed under load).
    #
    # TIME_BUDGET_SECONDS keeps us safely under typical Actions job limits
    # (well under the 6-hour hard cap, and under any shorter limits your
    # plan might have) with margin for the final commit step.
    TIME_BUDGET_SECONDS = int(os.environ.get("BACKTEST_TIME_BUDGET_SECONDS", "480"))  # 8 min
    PAUSE_BETWEEN_CHUNKS_SECONDS = int(os.environ.get("BACKTEST_PAUSE_SECONDS", "5"))

    start_time = time.time()
    chunks_this_run = 0
    consecutive_stalls = 0

    while True:
        elapsed = time.time() - start_time
        if elapsed > TIME_BUDGET_SECONDS:
            print(f"\nTime budget ({TIME_BUDGET_SECONDS}s) reached after {chunks_this_run} "
                  f"chunk(s) this run. Saving progress and stopping — the next scheduled "
                  f"run will continue from here.")
            break

        status = process_one_chunk(symbols, progress)
        save_json(PROGRESS_PATH, progress)  # save after EVERY chunk — nothing is ever lost

        if status == "done":
            break
        elif status == "advanced":
            chunks_this_run += 1
            consecutive_stalls = 0
        elif status == "skipped":
            chunks_this_run += 1
            consecutive_stalls = 0
        elif status == "stall":
            # Don't loop forever on a genuine data-integrity issue — bail
            # after a couple of stalls so a human can investigate.
            consecutive_stalls += 1
            if consecutive_stalls >= 2:
                print("Multiple consecutive stalls — stopping this run for investigation.")
                break
        elif status == "retry":
            # Transient failure — brief pause then try again, but don't
            # hammer the Worker in a tight loop if NSE/Worker is having a
            # bad moment. A few retries within THIS run is fine; if it
            # keeps failing, current_chunk_failures will eventually trigger
            # the skip logic inside process_one_chunk itself.
            pass

        time.sleep(PAUSE_BETWEEN_CHUNKS_SECONDS)

    print(f"\nRun summary: {chunks_this_run} chunk(s) processed in "
          f"{time.time() - start_time:.0f}s this invocation.")


if __name__ == "__main__":
    import urllib.parse
    main()
