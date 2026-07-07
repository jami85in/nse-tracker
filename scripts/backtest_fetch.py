#!/usr/bin/env python3
"""
Incremental historical backtest data collector.

Runs on a schedule (every 15 min via GitHub Actions). Each run:
1. Reads data/backtest/progress.json to find the next un-fetched date chunk
2. Calls the Cloudflare Worker (nse-backtest) for that chunk's raw daily closes
3. Merges the result into data/backtest/raw_closes/<SYMBOL>.json (one file
   per symbol, append-only, never overwritten wholesale)
4. Updates progress.json so the next run picks up where this one left off

Designed to be safely interruptible and resumable: if a run fails partway,
the next scheduled run just retries the same chunk (progress.json is only
advanced on success).
"""
import json, os, sys, time, datetime, urllib.request, urllib.error

WORKER_URL = os.environ.get("BACKTEST_WORKER_URL", "").rstrip("/")
CHUNK_DAYS = int(os.environ.get("BACKTEST_CHUNK_DAYS", "28"))  # calendar days per chunk
START_DATE = os.environ.get("BACKTEST_START_DATE", "2019-01-01")  # how far back to go
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
    """Call the Cloudflare Worker for one date-range chunk."""
    symbols_param = ",".join(symbols)
    url = (f"{WORKER_URL}/backtest?start={start_str}&end={end_str}"
           f"&symbols={urllib.parse.quote(symbols_param)}")
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


def main():
    if not WORKER_URL:
        print("ERROR: BACKTEST_WORKER_URL not set. Add it as a repo secret or workflow env var.")
        sys.exit(1)

    symbols = get_symbol_universe()
    if not symbols:
        print("ERROR: no symbol universe found (data/backtest/symbols.json empty and "
              "data/scan_latest.json has no candidates yet). Seed symbols.json manually "
              "with the full Nifty 500 list to proceed.")
        sys.exit(1)
    print(f"Tracking {len(symbols)} symbols.")

    progress = load_json(PROGRESS_PATH, {
        "start_date": START_DATE,
        "next_chunk_start": START_DATE,
        "chunks_completed": 0,
        "last_run": None,
        "done": False,
    })

    if progress.get("done"):
        print("Backtest data collection already complete through today. Nothing to do.")
        print("(Delete or edit progress.json's 'done' flag to extend the range.)")
        return

    today = datetime.date.today()
    chunk_start = datetime.date.fromisoformat(progress["next_chunk_start"])
    chunk_end = min(chunk_start + datetime.timedelta(days=CHUNK_DAYS - 1), today - datetime.timedelta(days=1))

    if chunk_start > today - datetime.timedelta(days=1):
        progress["done"] = True
        progress["last_run"] = datetime.datetime.now().isoformat()
        save_json(PROGRESS_PATH, progress)
        print(f"Reached today's date. Marking collection complete.")
        return

    cs, ce = chunk_start.isoformat(), chunk_end.isoformat()
    print(f"Fetching chunk {cs} -> {ce} for {len(symbols)} symbols...")

    try:
        result = fetch_chunk(cs, ce, symbols)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        print(f"Chunk fetch FAILED: {e}. Will retry same chunk next run (progress not advanced).")
        sys.exit(0)  # exit cleanly — don't fail the whole Action, just skip this run

    if result.get("error"):
        print(f"Worker returned an error: {result['error']}. Will retry same chunk next run.")
        sys.exit(0)

    closes = result.get("closes", {})
    updated_syms = merge_closes(closes)
    print(f"  fetched={result.get('trading_days_fetched')}, "
          f"failed={result.get('trading_days_failed')}, "
          f"symbols_with_data_this_chunk={len(updated_syms)}")

    # Only advance progress on a successful chunk — this is what makes the
    # whole pipeline resumable across failures/restarts.
    progress["next_chunk_start"] = (chunk_end + datetime.timedelta(days=1)).isoformat()
    progress["chunks_completed"] = progress.get("chunks_completed", 0) + 1
    progress["last_run"] = datetime.datetime.now().isoformat()
    save_json(PROGRESS_PATH, progress)

    total_days_est = (today - datetime.date.fromisoformat(START_DATE)).days
    done_days = (chunk_end - datetime.date.fromisoformat(START_DATE)).days
    pct = round(done_days / total_days_est * 100, 1) if total_days_est > 0 else 100
    print(f"Progress: {pct}% ({progress['chunks_completed']} chunks done, "
          f"next chunk starts {progress['next_chunk_start']})")


if __name__ == "__main__":
    import urllib.parse
    main()
