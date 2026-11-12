#!/usr/bin/env python3
"""
Collects daily NSE index data (NIFTY 50, sectoral indices like NIFTY BANK,
NIFTY IT, NIFTY PHARMA, etc.) via the Cloudflare Worker's /indices endpoint.

This is what lets us answer "is this stock moving with or against the
broader market/sector" — the actual purpose is RELATIVE STRENGTH: compare
a stock's return over N days against its sector index's return over the
same N days. A stock beating its sector is showing real relative strength;
a stock merely riding a rising sector isn't necessarily special.

Same resumable multi-chunk design as the other collectors. Once the backfill
is done, every run is a DAILY refresh: it re-fetches from a week before the
newest stored day through today and merges. (Until 12 Nov 2026 a finished
backfill simply stopped — the index files froze at 7 Jul 2026 and the
dashboard's "vs Nifty (20d)" figures were four months old.)
"""
import json, os, sys, time, datetime, urllib.request, urllib.error, urllib.parse

WORKER_URL = os.environ.get("BACKTEST_WORKER_URL", "").rstrip("/")
CHUNK_DAYS = int(os.environ.get("INDEX_CHUNK_DAYS", "28"))
START_DATE = os.environ.get("INDEX_START_DATE", "2020-09-01")

DATA_DIR = "data/backtest"
INDEX_DIR = os.path.join(DATA_DIR, "raw_indices")
PROGRESS_PATH = os.path.join(DATA_DIR, "progress_indices.json")

# The indices we care about for relative-strength comparisons: the broad
# market benchmark plus the major NSE sectoral indices. Stock-to-sector
# mapping (which stock belongs to which sector) is a separate concern —
# this just collects the index price series themselves.
TRACKED_INDICES = [
    "Nifty 50", "Nifty Bank", "Nifty IT", "Nifty Auto", "Nifty Pharma",
    "Nifty FMCG", "Nifty Metal", "Nifty Realty", "Nifty Energy",
    "Nifty Media", "Nifty PSU Bank", "Nifty Private Bank",
    "Nifty Financial Services", "Nifty Healthcare Index",
    "Nifty Consumer Durables", "Nifty Oil & Gas", "Nifty Infrastructure",
]


def load_json(path, default):
    """Missing file -> default. A file that exists but won't parse is an error:
    defaulting to [] there would let the next merge overwrite years of history."""
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        print(f"::error::{path} is unreadable ({e}) — fix or restore it from git; not overwriting it.")
        sys.exit(1)


def save_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)          # a killed run never leaves a half-written file


def fetch_chunk(start_str, end_str):
    indices_param = ",".join(TRACKED_INDICES)
    url = (f"{WORKER_URL}/indices?start={start_str}&end={end_str}"
           f"&indices={urllib.parse.quote(indices_param)}")
    req = urllib.request.Request(url, headers={"User-Agent": "nse-tracker-backtest/1.0"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read().decode())


def merge_indices(chunk_indices):
    updated = set()
    for name, series in chunk_indices.items():
        safe_name = name.replace(" ", "_").replace("&", "and")
        path = os.path.join(INDEX_DIR, f"{safe_name}.json")
        existing = load_json(path, [])
        by_date = {pt["date"]: pt for pt in existing}
        for pt in series:
            by_date[pt["date"]] = pt
        merged = [by_date[d] for d in sorted(by_date.keys())]
        save_json(path, merged)
        updated.add(name)
    return updated


def process_one_chunk(progress):
    today = datetime.date.today()
    chunk_start = datetime.date.fromisoformat(progress["next_chunk_start"])
    chunk_end = min(chunk_start + datetime.timedelta(days=CHUNK_DAYS - 1), today - datetime.timedelta(days=1))

    if chunk_start > today - datetime.timedelta(days=1):
        progress["done"] = True
        progress["last_run"] = datetime.datetime.now().isoformat()
        print("Reached today's date. Index collection complete.")
        return "done"

    cs, ce = chunk_start.isoformat(), chunk_end.isoformat()
    print(f"Fetching index chunk {cs} -> {ce}...")

    try:
        result = fetch_chunk(cs, ce)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        print(f"  Chunk fetch FAILED: {e}. Will retry.")
        return "retry"

    if result.get("error"):
        print(f"  Worker error: {result['error']}. Will retry.")
        return "retry"

    indices = result.get("indices", {})
    trading_days_fetched = result.get("trading_days_fetched", 0)
    print(f"  fetched={trading_days_fetched}, failed={result.get('trading_days_failed',0)}, indices_in_response={len(indices)}")

    if trading_days_fetched == 0:
        progress["current_chunk_failures"] = progress.get("current_chunk_failures", 0) + 1
        if progress["current_chunk_failures"] >= 4:
            print(f"  Chunk failed 4x — skipping past it.")
            progress["next_chunk_start"] = (chunk_end + datetime.timedelta(days=1)).isoformat()
            progress["chunks_skipped"] = progress.get("chunks_skipped", 0) + 1
            progress["current_chunk_failures"] = 0
            return "skipped"
        return "retry"

    updated = merge_indices(indices)
    print(f"  merged {len(updated)} indices this chunk: {sorted(updated)}")

    progress["next_chunk_start"] = (chunk_end + datetime.timedelta(days=1)).isoformat()
    progress["chunks_completed"] = progress.get("chunks_completed", 0) + 1
    progress["current_chunk_failures"] = 0
    progress["last_run"] = datetime.datetime.now().isoformat()
    return "advanced"


def _newest_nifty():
    series = load_json(os.path.join(INDEX_DIR, "Nifty_50.json"), [])
    return series[-1].get("date", "") if series else ""


def daily_refresh(progress):
    """Bring every index up to today, in CHUNK_DAYS pieces, from a week before
    the contiguous frontier: the end of the last chunk that arrived with every
    chunk before it. A chunk that fails stops the run there, so a gap is
    retried next time instead of being jumped over. merge_indices dedupes by
    date, so overlap is harmless. Exits 1 when a chunk failed or Nifty 50 is
    still more than 4 weekdays old, so a stalled feed shows up as a failed run."""
    today = datetime.date.today()
    frontier = progress.get("daily_frontier") or _newest_nifty() or progress.get("next_chunk_start") or START_DATE
    lookback = int(os.environ.get("INDEX_DAILY_LOOKBACK_DAYS", "12"))
    start = min(today - datetime.timedelta(days=lookback),
                datetime.date.fromisoformat(frontier) - datetime.timedelta(days=7))
    budget_end = time.time() + int(os.environ.get("INDEX_TIME_BUDGET_SECONDS", "300"))
    print(f"Daily index refresh: {start} -> {today} (frontier {frontier})")
    s, merged, failed = start, set(), None
    while s <= today:
        if time.time() > budget_end:
            failed = f"time budget reached at {s}"
            break
        e = min(s + datetime.timedelta(days=CHUNK_DAYS - 1), today)
        ok = False
        for attempt in range(1, 4):
            try:
                result = fetch_chunk(s.isoformat(), e.isoformat())
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as ex:
                print(f"  {s}..{e} attempt {attempt}: fetch failed ({ex})")
                time.sleep(6)
                continue
            if result.get("error"):
                print(f"  {s}..{e} attempt {attempt}: worker error ({result['error']})")
                time.sleep(6)
                continue
            merged |= merge_indices(result.get("indices", {}))
            print(f"  {s}..{e}: {result.get('trading_days_fetched', 0)} day(s)")
            ok = True
            break
        if not ok:
            failed = f"chunk {s}..{e} failed 3 times"
            break
        # frontier = newest day actually stored, so an empty answer is retried next run
        progress["daily_frontier"] = _newest_nifty() or frontier
        s = e + datetime.timedelta(days=1)
        time.sleep(2)
    progress["last_daily_refresh"] = datetime.datetime.now().isoformat()
    progress["last_daily_range"] = f"{start}..{today}"
    print(f"Daily index refresh merged {len(merged)} indices.")
    newest = _newest_nifty()
    stale = not newest or sum(1 for k in range(1, (today - datetime.date.fromisoformat(newest)).days + 1)
                              if (today - datetime.timedelta(days=k)).weekday() < 5) > 4
    if failed or stale:
        print(f"::error::Index data not current — {failed or 'no new days'}; newest Nifty 50 bar {newest or 'none'}.")
        return False
    return True


def main():
    if not WORKER_URL:
        print("ERROR: BACKTEST_WORKER_URL not set.")
        sys.exit(1)

    print(f"Tracking {len(TRACKED_INDICES)} indices: {TRACKED_INDICES}")

    progress = load_json(PROGRESS_PATH, {
        "start_date": START_DATE, "next_chunk_start": START_DATE,
        "chunks_completed": 0, "chunks_skipped": 0,
        "current_chunk_failures": 0, "last_run": None, "done": False,
    })

    if progress.get("done"):
        ok = daily_refresh(progress)
        save_json(PROGRESS_PATH, progress)
        if not ok:
            sys.exit(1)               # the commit step still runs (if: always()) and saves what arrived
        return

    TIME_BUDGET_SECONDS = int(os.environ.get("INDEX_TIME_BUDGET_SECONDS", "300"))
    PAUSE_SECONDS = int(os.environ.get("INDEX_PAUSE_SECONDS", "5"))

    start_time = time.time()
    chunks_this_run = 0

    while True:
        if time.time() - start_time > TIME_BUDGET_SECONDS:
            print(f"\nTime budget reached after {chunks_this_run} chunk(s). Stopping.")
            break
        status = process_one_chunk(progress)
        save_json(PROGRESS_PATH, progress)
        if status == "done":
            break
        elif status in ("advanced", "skipped"):
            chunks_this_run += 1
        time.sleep(PAUSE_SECONDS)

    print(f"\nRun summary: {chunks_this_run} chunk(s) in {time.time()-start_time:.0f}s.")


if __name__ == "__main__":
    main()
