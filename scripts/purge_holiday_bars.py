#!/usr/bin/env python3
"""
purge_holiday_bars.py — remove fake bars on NSE holidays from the price archive.

Why: the price worker asks NSE for a bhavcopy on every weekday. On an
exchange holiday it gets the previous session's file back, so every stock
gained a "trading day" identical to the day before — about 70 of them since
2020 (Republic Day, Diwali Balipratipada, Christmas, 10 Nov 2026 ...). Those
copies count as extra sessions in the Ranges, Cycles and scan maths: a flat
day that never happened, and a second chance for the previous day's high or
low to fill a tested order.

How a fake day is recognised: a date on which at least 90% of stocks (and at
least MIN_SYMBOLS of them) have a bar identical in open, high, low and close
to their previous bar. A real session never looks like that market-wide; a
single untraded stock does, and is left alone. Dates in market_session.HOLIDAYS
are removed too.

Idempotent and cheap: run after every daily OHLC refresh. Prints only dates
and counts.
"""
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from market_session import HOLIDAYS  # noqa: E402
except Exception:                        # keep working even if the calendar module moves
    HOLIDAYS = {}

DIRS = [d for d in os.environ.get("PURGE_DIRS", "data/backtest/raw_ohlc,data/backtest/raw_ohlc_hist").split(",") if d]
SHARE = 0.9
MIN_SYMBOLS = int(os.environ.get("PURGE_MIN_SYMBOLS", "100"))
KEYS = ("open", "high", "low", "close")


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else None
    except Exception:
        return None


def fake_dates(folder):
    seen, same = collections.Counter(), collections.Counter()
    for name in os.listdir(folder):
        if not name.endswith(".json"):
            continue
        bars = load(os.path.join(folder, name))
        if not bars:
            continue
        for a, b in zip(bars, bars[1:]):
            d = b.get("date")
            if not d:
                continue
            seen[d] += 1
            if all(a.get(k) == b.get(k) for k in KEYS):
                same[d] += 1
    found = {d for d, n in seen.items() if n >= MIN_SYMBOLS and same[d] >= SHARE * n}
    return found | {d for d in HOLIDAYS if d in seen}


def purge(folder):
    if not os.path.isdir(folder):
        return 0, 0, []
    bad = fake_dates(folder)
    if not bad:
        return 0, 0, []
    files = removed = 0
    for name in os.listdir(folder):
        if not name.endswith(".json"):
            continue
        path = os.path.join(folder, name)
        bars = load(path)
        if not bars:
            continue
        keep = [b for b in bars if b.get("date") not in bad]
        if len(keep) != len(bars):
            removed += len(bars) - len(keep)
            files += 1
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(keep, f)                  # same format as backtest_fetch_ohlc.py
            os.replace(tmp, path)
    return files, removed, sorted(bad)


def main():
    for folder in DIRS:
        files, removed, bad = purge(folder)
        if bad:
            shown = ", ".join(bad[-6:]) + (f" (+{len(bad) - 6} earlier)" if len(bad) > 6 else "")
            print(f"{folder}: {len(bad)} holiday date(s) found [{shown}] — "
                  f"removed {removed} bars from {files} files")
        else:
            print(f"{folder}: no holiday bars")
    return 0


if __name__ == "__main__":
    sys.exit(main())
