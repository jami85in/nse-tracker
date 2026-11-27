#!/usr/bin/env python3
"""
watchdog.py — the checks nothing else makes, as phone alerts (no AI involved).

Runs from .github/workflows/watchdog.yml: every hour from the kite-login worker's 15-minute
Cloudflare cron (dependable), plus GitHub's own two-hourly schedule (often dropped: 2 of 12
slots ran on 27 Nov 2026). Public repo: free Actions minutes. Reads small files only:
  - ledger/digest.json in the private repo (written by ledger_watch.py there)
  - market/minute_progress.json in the private repo (1-minute backfill)
  - agents/handoff/last_run.txt in the private repo (evening check-in)
  - kite_token.json in the private repo - only whether today's login is done
  - data/scan_latest.json and data/prices_live.json in this repo
Checks:
  1. New ledger issues (shares missing from Zerodha, a sale no trade explains,
     no closing price, ...) — named by account, stock and a short reason; never
     quantities or prices.
  2. The ledger not rebuilt on a trading day by 21:30 IST.
  3. The 1-minute history backfill making no progress for 24 hours while months remain.
  4. No scan since the previous session's close, on a trading day by 09:30 IST.
  5. Live prices more than 20 minutes old during the session while logged in (a dead price
     loop can't send notify.py's own "not updating" notice).
  6. No HUF Kite login 15 minutes after the open.
  7. No evening check-in for the last session by 10 hours after its close.
  8. The NSE calendar copies in agents.html / app.src.html out of step with market_session.py.
With --morning (called in every round of the live price loop; acts from 08:45 IST,
once a day): one summary before the open — login done, scan ran,
ledger current, open ledger items, watchdog alive, 1-minute backfill — once a day.
Each alert is sent once (state in the private repo, notify/watchdog_state.json).
The first run only records what is open, and sends one summary.
notify.py (inside the price loop) also alerts live-price and login problems; checks 5-6
cover the case where the loop itself is not running.
"""
import datetime as dt
import json
import os
import re
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


def _ist_at(s):
    """'2026-11-27 15:28 IST' (or ISO) -> aware IST datetime, else None."""
    s = str(s or "").replace(" IST", "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            return dt.datetime.strptime(s[:16], fmt).replace(tzinfo=IST)
        except ValueError:
            pass
    return None


def prev_session_close(now):
    """Close of the most recent session that ended before today's open (or before now, if
    today has no session)."""
    s = session(now.date())
    return last_close((s[0] if s else now) - dt.timedelta(minutes=1))


def scan_ok(scan, now):
    """(ok, label): a scan ran after the previous session's close. Scans follow the 19:00 IST
    and 04:45 IST (Tue-Sat) OHLC refresh, so on a Monday or after a holiday the newest scan
    is from an earlier date - comparing its date with today raised a false alarm (review
    27 Nov 2026). Known gap: after a weekend special session (Muhurat on a Sunday) no refresh
    runs before Monday's open, so Monday reports the scan missing."""
    raw = str((scan or {}).get("scan_time") or "")
    at, prev = _ist_at(raw), prev_session_close(now)
    return (at is not None and prev is not None and at > prev), (raw[:16] or "never")


def checks(now, digest, progress, scan, state, prices=None, login_ok=None, checkin_day=None, cal_drift=None,
           login_at=None):
    """-> list of (key, title, body, priority). Pure: no network, for tests.
    login_ok: True / False / None (unknown); login_at: when today's login was saved (IST) or None.
    checkin_day: the last checked IST date ('' when never), None when unreadable.
    cal_drift: list of calendar mismatches, None to skip."""
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
    # 4. no scan since the previous session's close
    if sess_today and now.hour * 60 + now.minute >= 9 * 60 + 30:
        ok, st = scan_ok(scan, now)
        if not ok:
            out.append((f"scan-missing|{today}", "Daily scan missing",
                        f"No scan since the last session's close (last ran {st}). Check the scan workflow.", 4))
    # 5. live prices stopped during the session while logged in
    # (not in the first 20 minutes after a late login: the loop needs a round or two - notify.py does the same)
    late_login = login_at is not None and now - login_at < dt.timedelta(minutes=20)
    if sess_today and login_ok and not late_login and sess_today[0] + dt.timedelta(minutes=30) <= now <= sess_today[1]:
        upd = _ist_at((prices or {}).get("updated_at"))
        if upd is None or now - upd > dt.timedelta(minutes=20):
            out.append((f"prices-stale|{today}", "Live prices stopped",
                        f"Live prices last updated {upd.strftime('%H:%M') + ' IST' if upd else 'never'}. Either Kite is "
                        f"not answering or the price loop stopped: the login worker starts a new loop within 15 "
                        f"minutes if none is running. If a run shows as in progress but prices don't move, cancel "
                        f"it in GitHub Actions ('NSE Live Price Update (Kite Connect)').", 4))
    # 6. no HUF login after the open
    if sess_today and login_ok is False and sess_today[0] + dt.timedelta(minutes=15) <= now <= sess_today[1]:
        out.append((f"login-missing|{today}", "Kite login missing",
                    "No HUF Kite login today: no live prices, trade sync or buy-at-close alerts until you log in.", 4))
    # 7. evening check-in missing for the last session
    lc = last_close(now)
    if checkin_day is not None and lc and now >= lc + dt.timedelta(hours=10) and checkin_day < lc.date().isoformat():
        out.append((f"checkin-missed|{lc.date()}", "Evening check-in didn't run",
                    f"No Claude check-in for {lc:%d %b}. Check 'Evening ledger check-in (Claude)' in the private "
                    f"repo's Actions.", 3))
    # 8. calendar copies out of step
    if cal_drift:
        out.append((f"calendar-drift|{'; '.join(cal_drift)}", "NSE calendar copies differ",
                    "Holiday / special-session lists differ from scripts/market_session.py: " + "; ".join(cal_drift)
                    + ". Update them together.", 3))
    return out


def calendar_drift(today, root=None):
    """Future holiday and special-session dates in agents.html and app.src.html vs
    scripts/market_session.py -> list of short mismatch notes ([] when in step)."""
    import market_session as M
    root = root or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    t = today.isoformat()
    want_s = {d for d in M.SPECIAL_SESSIONS if d >= t}
    want_h = {d for d in M.HOLIDAYS if d >= t}
    lists = (("agents.html", r"const HOLIDAYS = new Set\(\[([^\]]*)\]\)", r"const SPECIAL = \{([^}]*)\}"),
             ("app.src.html", r"const NSE_HOLIDAYS = \{([^}]*)\}", r"const NSE_SPECIAL_SESSIONS = \{([^}]*)\}"))
    notes = []
    for name, hol_re, sp_re in lists:
        try:
            text = open(os.path.join(root, name), encoding="utf-8").read()
        except OSError:
            continue
        mh, ms = re.search(hol_re, text), re.search(sp_re, text)
        if not mh or not ms:
            notes.append(f"{name}: list not found")
            continue
        dates = lambda s: {d for d in re.findall(r'"(\d{4}-\d{2}-\d{2})"', s) if d >= t}
        got_s = dates(ms.group(1))
        got_h = dates(mh.group(1)) - set(M.SPECIAL_SESSIONS)
        diff = sorted((got_h ^ want_h) | (got_s ^ want_s))
        if diff:
            notes.append(f"{name} ({', '.join(diff[:4])})")
    return notes


def last_close(now):
    """Close (IST datetime) of the most recent session that has ended by `now`."""
    d = now.date()
    for _ in range(15):
        s = session(d)
        if s and s[1] <= now:
            return s[1]
        d -= dt.timedelta(days=1)
    return None


def morning_lines(now, digest, progress, scan, state, login_ok):
    """-> (title, [lines]) for the summary before the open. Pure: no network, for tests."""
    todo, fine = [], []
    if login_ok is False:
        todo.append("Log in to Kite (HUF): no valid login for today yet.")
    elif login_ok:
        fine.append("HUF login done")
    ok, st = scan_ok(scan, now)
    (fine.append("scan ran") if ok
     else todo.append(f"Daily scan missing (none since the last close; last ran {st}) - check the scan workflow."))
    lc = last_close(now)
    try:
        gen_at = dt.datetime.fromisoformat(str((digest or {}).get("generated_at")).replace("Z", "+00:00")).astimezone(IST)
    except Exception:
        gen_at = None
    if gen_at is None or (lc and gen_at < lc):
        todo.append("Ledger not rebuilt since the last close - check the Trade Ledger workflow.")
    else:
        fine.append("ledger current")
    acts = [f"{a.get('short') or aid} {i.get('symbol')}" for aid, a in sorted((digest or {}).get("accounts", {}).items())
            for i in a.get("issues", []) if i.get("level") == "act"]
    known = (f"Known ledger items: {', '.join(acts[:4])}" + (f" and {len(acts) - 4} more" if len(acts) > 4 else "")
             + " (alerted before).") if acts else None
    try:
        chk = dt.datetime.fromisoformat(state.get("checked_at"))
        (fine.append("watchdog alive") if now - chk <= dt.timedelta(hours=5)
         else todo.append("Watchdog has not run for 5+ hours - check the Watchdog workflow."))
    except Exception:
        todo.append("Watchdog has not run yet.")
    if progress:
        done = len(progress.get("months_done") or [])
        total = months_total(progress.get("archive_from") or "")
        if total and done < total:
            fine.append(f"1-minute backfill {done}/{total}")
        elif total:
            fine.append("1-minute backfill complete")
    title = "Ready for the open" if not todo else todo[0].split(":")[0].split(" - ")[0]
    lines = todo + (["OK: " + ", ".join(fine)] if fine else []) + ([known] if known else [])
    return title, lines


def morning(priv, topic, now):
    """Once per trading day, at the start of the price loop: one summary before the open."""
    if not session(now.date()):
        print("watchdog: no session today - no morning summary")
        return 0
    # from 08:45 (after the 06:40 scan and the early logins) until the close; called every round
    if not dt.time(8, 45) <= now.time() <= dt.time(15, 30):
        return 0
    try:   # a GitHub API blip just skips this round (called every round; a failure here would count
        #     against the price loop's alert-script failure notice - review 27 Nov 2026)
        state, sha = priv.get_json(STATE, None)
        state = state or {}
        key = f"morning|{now.date()}"
        if key in state.get("morning_sent", []):
            print("watchdog: morning summary already sent today")
            return 0
        digest, _ = priv.get_json("ledger/digest.json", None)
        progress, _ = priv.get_json("market/minute_progress.json", None)
    except Exception as e:
        print(f"::warning::watchdog: private files unreadable this round ({type(e).__name__})")
        return 0
    try:
        scan = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "scan_latest.json")))
    except Exception:
        scan = None
    try:
        from kite_token_reader import get_kite_token      # reads the token file; only its freshness is used here
        tok, stale = get_kite_token()
        login_ok = bool(tok) and not stale
    except Exception:
        login_ok = None
    title, lines = morning_lines(now, digest, progress, scan, state, login_ok)
    if topic:
        try:
            send(topic, title, "\n".join(lines), 4 if title != "Ready for the open" else 2)
        except Exception as e:
            print(f"::warning::ntfy send failed ({type(e).__name__})")
            return 0
    state["morning_sent"] = (state.get("morning_sent") or [])[-6:] + [key]
    try:
        priv.put_json(STATE, state, "watchdog: morning", sha=sha, retries=1)
    except Exception as e:
        print(f"::warning::watchdog state not saved ({type(e).__name__})")
    print(f"watchdog: morning summary sent ({len(lines)} line(s))")
    return 0


def main():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    priv = PrivateRepo()
    if not priv.ok:
        print("::warning::SECRETS_REPO / SECRETS_REPO_PAT not set - nothing checked")
        return 0
    now = dt.datetime.now(IST)
    if "--morning" in sys.argv:
        return morning(priv, topic, now)
    digest, _ = priv.get_json("ledger/digest.json", None)
    progress, _ = priv.get_json("market/minute_progress.json", None)
    try:
        scan = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "scan_latest.json")))
    except Exception:
        scan = None
    try:
        prices = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "prices_live.json")))
    except Exception:
        prices = None
    login_ok = login_at = None
    try:   # only whether / when today's login was saved - the token itself is not used here
        from kite_token_reader import token_is_stale
        tok_file, _ = priv.get_json("kite_token.json", None)
        if tok_file:
            login_ok = not token_is_stale(tok_file)
            login_at = _ist_at(tok_file.get("generated_at"))
        del tok_file
    except Exception:
        login_ok = login_at = None
    try:
        raw, _ = priv.get_bytes("agents/handoff/last_run.txt")
        checkin_day = "" if raw is None else raw.decode("utf-8", "replace").strip()   # missing = never ran
    except Exception:
        checkin_day = None
    try:
        cal_drift = calendar_drift(now.date())
    except Exception as e:
        print(f"::warning::calendar check failed ({type(e).__name__})")
        cal_drift = None
    state, sha = priv.get_json(STATE, None)
    first = state is None
    state = state or {}
    found = checks(now, digest, progress, scan, state, prices, login_ok, checkin_day, cal_drift, login_at)
    sent = state.setdefault("sent", {})
    keys = {k for k, *_ in found}
    new = [f for f in found if f[0] not in sent]
    # an issue that went away and comes back is alerted again - but an input that couldn't be read
    # this run proves nothing: keep its open alerts, or a GitHub hiccup re-sends them next hour
    unknown = set()
    if login_ok is None or prices is None:
        unknown |= {"prices-stale", "login-missing"}
    if checkin_day is None:
        unknown.add("checkin-missed")
    if cal_drift is None:
        unknown.add("calendar-drift")
    for k in list(sent):
        if k not in keys and k.split("|")[0] not in unknown:
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
