#!/usr/bin/env python3
"""
sme_fetch_ohlc.py — collect NSE SME (EMERGE) OHLC into a SEPARATE dataset.

WHY SEPARATE, deliberately:
The main strategy is validated on ~1,800 main-board EQ stocks over ~6 years.
SME has zero validation behind it. Mixing SME symbols into the main universe
would silently contaminate signals whose numbers were earned on a different
population — so this writes to its own directory and its own symbol list, and
nothing in the existing scan reads it. The point is to accumulate enough
history to BACKTEST SME properly before any of it drives a real decision.

DATA SOURCE — no new dependency:
SME securities are in the SAME sec_bhavdata_full file the worker already
fetches every day, under series "SM" (SME) and "ST" (SME trade-to-trade).
They were simply discarded by a hardcoded EQ filter. So this adds no new
URL and no new failure mode; it reads data already being downloaded.

LIQUIDITY IS CAPTURED ON PURPOSE:
For main-board names liquidity is a footnote. For SME it is the whole
question: Bollinger width measures OBSERVED price variation, so a stock that
barely trades produces a very tight band and registers as a textbook squeeze
purely because nobody traded it. That is the same failure that made the only
5 qualifying HIGH+STRONG backtest trades all debt ETFs. Volume and turnover
are stored per bar so a liquidity floor can be applied before any signal is
trusted.
"""
import os
import sys
import json
import time
import datetime
import urllib.request
import urllib.error

WORKER = os.environ.get("NSE_WORKER_URL", "https://nse-backtest.jami85in.workers.dev")
OUT_DIR = "data/sme/raw_ohlc"
SYMBOLS_PATH = "data/sme/symbols_sme.json"
PROGRESS_PATH = "data/sme/progress_sme.json"
SERIES = "SM,ST"          # SME and SME trade-to-trade
CHUNK_DAYS = int(os.environ.get("SME_CHUNK_DAYS", "30"))
# Where a fresh collection starts (only used when there is no progress file,
# e.g. after the workflow's "reset" run), and how long one run may keep
# fetching chunks before it stops and commits; the next run carries on.
START_DATE = os.environ.get("SME_START_DATE", "")
TIME_BUDGET = int(os.environ.get("SME_TIME_BUDGET_SECONDS", "300"))
# 12 Nov 2026: before the worker was redeployed it ignored ?series=, so the
# files written here until then were main-board (EQ) copies. Bars written now
# carry a "series" field (SM/ST); a chunk without it is refused rather than
# saved, so that can't silently happen again.
SME_SERIES = {"SM", "ST"}


def http_get_json(url, timeout=120, retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "nse-tracker-sme/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
            wait = 2 ** attempt
            print(f"    attempt {attempt+1}/{retries} failed ({e}); retrying in {wait}s")
            time.sleep(wait)
    print(f"    giving up: {last}")
    return None


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f)


def fetch_sme_symbols():
    """Current SME universe, from the same bhavcopy the worker already reads."""
    url = f"{WORKER}/symbols?universe=all&series={SERIES}"
    data = http_get_json(url)
    if not data or "symbols" not in data:
        print(f"  Could not fetch SME symbol list: {data}")
        return None
    syms = sorted(data["symbols"])
    print(f"  SME universe: {len(syms)} symbols (as of {data.get('as_of')})")
    return syms


def fetch_chunk(start, end):
    url = (f"{WORKER}/backtest?start={start.isoformat()}&end={end.isoformat()}"
           f"&series={SERIES}")
    return http_get_json(url)


def merge_bars(existing, new_bars):
    """Merge by date, newer wins. Keeps the series sorted and de-duplicated."""
    by_date = {b["date"]: b for b in existing}
    for b in new_bars:
        by_date[b["date"]] = b
    return [by_date[d] for d in sorted(by_date)]


def main():
    print(f"=== SME OHLC collection ({datetime.datetime.now().isoformat(timespec='seconds')}) ===")
    os.makedirs(OUT_DIR, exist_ok=True)

    syms = fetch_sme_symbols()
    if syms:
        save_json(SYMBOLS_PATH, syms)
    else:
        syms = load_json(SYMBOLS_PATH, [])
        if not syms:
            print("  No SME symbol list available and none cached — aborting.")
            return 1
        print(f"  Using cached SME symbol list ({len(syms)} symbols)")

    prog = load_json(PROGRESS_PATH, {})
    today = datetime.date.today()
    if "next_start" not in prog:
        first = START_DATE or (today - datetime.timedelta(days=CHUNK_DAYS)).isoformat()
        prog["next_start"] = first
        print(f"  No progress file — starting a fresh collection from {first}")

    # Near the frontier, always re-read the last week: a day whose file wasn't
    # published yet at run time gets picked up next run (merging is by date).
    week_ago = (today - datetime.timedelta(days=7)).isoformat()
    if prog["next_start"] > week_ago:
        prog["next_start"] = week_ago

    t0 = time.time()
    chunks = 0
    while time.time() - t0 < TIME_BUDGET:
        start = datetime.date.fromisoformat(prog["next_start"])
        if start > today:
            print("  Already current — nothing more to fetch.")
            break
        end = min(start + datetime.timedelta(days=CHUNK_DAYS - 1), today)
        print(f"  Fetching {start} .. {end} (series {SERIES})")
        data = fetch_chunk(start, end)
        if not data or "ohlc" not in data:
            print(f"  Chunk failed: {str(data)[:300]}")
            save_json(PROGRESS_PATH, prog)
            return 1 if chunks == 0 else 0

        ohlc = data["ohlc"]
        sample = next((b[0] for b in ohlc.values() if b), None)
        if sample is not None and sample.get("series") not in SME_SERIES:
            print("  The worker returned bars without an SME series tag — it is not honouring "
                  "?series=SM,ST. Nothing saved.")
            return 1
        print(f"  Received {len(ohlc)} symbols, "
              f"{data.get('trading_days_fetched')} trading days "
              f"({data.get('trading_days_failed')} failed)")

        written = 0
        for sym, bars in ohlc.items():
            if not bars:
                continue
            # Sanitise the symbol so it can never escape the output directory.
            safe = "".join(ch for ch in sym if ch.isalnum() or ch in "-_&")
            if not safe:
                continue
            path = os.path.join(OUT_DIR, f"{safe}.json")
            merged = merge_bars(load_json(path, []), bars)
            save_json(path, merged)
            written += 1

        prog["next_start"] = (end + datetime.timedelta(days=1)).isoformat()
        prog["last_run"] = datetime.datetime.now().isoformat()
        prog["last_range"] = f"{start}..{end}"
        prog["symbols_count"] = len(syms)
        save_json(PROGRESS_PATH, prog)
        chunks += 1
        print(f"  Wrote {written} symbol files; next chunk starts {prog['next_start']}")
        time.sleep(2)

    print(f"  {chunks} chunk(s) this run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
