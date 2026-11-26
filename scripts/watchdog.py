#!/usr/bin/env python3
"""
watchdog.py — the checks nothing else makes, as phone alerts (no AI involved).

Runs every two hours from .github/workflows/watchdog.yml (public repo: free
Actions minutes). Reads small files only:
  - ledger/digest.json in the private repo (written by ledger_watch.py there)
  - market/minute_progress.json in the private repo (1-minute backfill)
  - data/scan_latest.json in this repo
Checks:
  1. New ledger issues (shares missing from Zerodha, a sale no trade explains,
     no closing price, ...) — named by account, stock and a short reason; never
     quantities or prices.
  2. The ledger not rebuilt on a trading day by 21:30 IST.
  3. The 1-minute history backfill making no progress for 24 hours while months remain.
  4. The daily scan missing on a trading day by 09:30 IST.
Each alert is sent once (state in the private repo, notify/watchdog_state.json).
The first run only records what is open, and sends one summary.
Live-price, login and price-loop problems are already alerted by notify.py.
"""
import datetime as dt
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from market_session import session  # noqa: E402
from private_repo import PrivateRepo  # noqa: E402

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
STATE = "notify/watchdog_state.json"
HIST_FIRST = (2015, 2)          # first month of the 1-minute backfill
DASH = "https://jami85in.github.io/nse-tracker/"


def send(topic, title, body, priority=3):
    msg = {"topic": topic, "title": title, "message": body, "priority": priority, "tags": ["mag"], "click": DASH}
    req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(msg).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status


def months_total(archive_from):
    """Months the backfill must cover: Feb 2015 up to the month before the daily archive starts."""
    try:
        a = dt.date.fromisoformat(archive_from)
    except Exception:
        return None
    y, m = (a.year, a.month - 1) if a.month > 1 else (a.year - 1, 12)
    if a.day > 1:                       # the archive starts mid-month: that month is in the history too
        y, m = a.year, a.month
    return (y - HIST_FIRST[0]) * 12 + (m - HIST_FIRST[1]) + 1


def checks(now, digest, progress, scan, state):
    """-> list of (key, title, body, priority). Pure: no network, for tests."""
    out = []
    today = now.date()
    sess_today = session(today)
    # 1. ledger issues
    for aid, a in sorted((digest or {}).get("accounts", {}).items()):
        for i in a.get("issues", []):
            if i.get("level") == "info":
                continue
            key = f"ledger|{aid}|{i.get('symbol')}|{i.get('reason')}"
            out.append((key, "Ledger needs a look",
                        f"{a.get('short') or aid} · {i.get('symbol') or '—'}: {i.get('reason')}",
                        4 if i.get("level") == "act" else 3))
    # 2. ledger not rebuilt on a trading day
    if sess_today and now.hour * 60 + now.minute >= 21 * 60 + 30:
        gen = (digest or {}).get("generated_at") or ""
        try:
            gen_at = dt.datetime.fromisoformat(gen.replace("Z", "+00:00")).astimezone(IST)
        except Exception:
            gen_at = None
        # rebuilds during the session (trade syncs) don't count: the after-close one captures holdings
        if gen_at is None or gen_at < sess_today[1]:
            out.append((f"ledger-stale|{today}", "Ledger not rebuilt today",
                        "No ledger rebuild since the close. Check the Trade Ledger workflow in the private repo.", 4))
    # 3. 1-minute backfill stalled
    if progress:
        done = len(progress.get("months_done") or [])
        total = months_total(progress.get("archive_from") or "")
        m = state.setdefault("minute", {})
        if m.get("done") != done:
            m.update({"done": done, "since": now.isoformat(timespec="minutes")})
        elif total and done < total:
            try:
                since = dt.datetime.fromisoformat(m["since"])
            except Exception:
                since = now
            if now - since >= dt.timedelta(hours=24):
                out.append((f"minute-stall|{today}", "1-minute backfill stalled",
                            f"No new month for 24 hours ({done} of {total} done). Is the HUF Kite login done? "
                            f"Check the minute history workflow.", 3))
    # 4. daily scan missing
    if sess_today and now.hour * 60 + now.minute >= 9 * 60 + 30:
        st = str((scan or {}).get("scan_time") or "")[:10]
        if st != today.isoformat():
            out.append((f"scan-missing|{today}", "Daily scan missing",
                        f"The scan last ran {st or 'never'}. Check the scan workflow.", 4))
    return out


def main():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    priv = PrivateRepo()
    if not priv.ok:
        print("::warning::SECRETS_REPO / SECRETS_REPO_PAT not set - nothing checked")
        return 0
    now = dt.datetime.now(IST)
    digest, _ = priv.get_json("ledger/digest.json", None)
    progress, _ = priv.get_json("market/minute_progress.json", None)
    try:
        scan = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "scan_latest.json")))
    except Exception:
        scan = None
    state, sha = priv.get_json(STATE, None)
    first = state is None
    state = state or {}
    found = checks(now, digest, progress, scan, state)
    sent = state.setdefault("sent", {})
    keys = {k for k, *_ in found}
    new = [f for f in found if f[0] not in sent]
    # an issue that went away and comes back is alerted again
    for k in list(sent):
        if k not in keys:
            sent.pop(k)
    if first and new:
        body = f"Watching the ledger, the 1-minute backfill and the daily scan. Open now: {len(new)} item(s), e.g. " + \
               "; ".join(b.rstrip(".") for _, _, b, _ in new[:3]) + "."
        msgs = [("Watchdog is on", body, 3)]
    elif len(new) > 3:
        by = {}
        for _, t, b, p in new:
            by.setdefault(t, []).append((b, p))
        msgs = [(t, "\n".join(b for b, _ in items[:8]) + (f"\n… and {len(items) - 8} more" if len(items) > 8 else ""),
                 max(p for _, p in items)) for t, items in by.items()]
    else:
        msgs = [(t, b, p) for _, t, b, p in new]
    n_ok = 0
    for t, b, p in msgs:
        if not topic:
            break
        try:
            send(topic, t, b, p)
            n_ok += 1
        except Exception as e:
            print(f"::warning::ntfy send failed ({type(e).__name__})")
    if n_ok == len(msgs) or not topic:
        for k, *_ in new:
            sent[k] = now.isoformat(timespec="minutes")
    state["checked_at"] = now.isoformat(timespec="minutes")
    try:
        priv.put_json(STATE, state, "watchdog: state", sha=sha, retries=1)
    except Exception as e:
        print(f"::warning::watchdog state not saved ({type(e).__name__})")
    print(f"watchdog: {len(found)} open, {len(new)} new, {n_ok} alert(s) sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
