#!/usr/bin/env python3
"""
minute_bars.py — a private archive of 1-minute candles for EVERY NSE stock
Zerodha lists, for intraday analysis and backtests (daily open/high/low/close
can't show whether a stock fell 2% before or after rising 2%; minute bars can).

Source: Kite's historical-data API with the HUF login (paid Connect plan).
Kite allows 3 historical requests a second (no daily cap; we use 2.2 to leave
room for the ranges job), serves at most 60
days of minute candles per request, and has minute data back to about 2015.

Storage: release files in the PRIVATE repo (licensed data stays private; a git
repo should stay under ~1 GB, but GitHub doesn't cap the total size of release
files, only 2 GiB per file and 1,000 files per release).
  release "minute-YYYY-MM"  ->  minute-YYYY-MM-DD.parquet   one file per day
  release "minute-history"  ->  minute-hist-YYYY-MM.parquet one file per month
Columns: symbol (str), time (timestamp, IST, minute start), open, high, low,
close (float64), volume (int64). Parquet with zstd compression.

Modes
  archive            today's candles for all NSE stocks (after the close), plus
                     any of the last 30 trading days still missing
  history [budget]   resumable backfill, month by month, oldest first, from
                     Feb 2015 to the day before the daily archive started;
                     stops after `budget` minutes (default 320) and records
                     progress in market/minute_progress.json (private repo)

Prices: Kite adjusts past candles for splits/bonuses as of the fetch date, so
history files are adjusted to their fetch date (parquet metadata "fetched_on")
while daily files keep that day's prices; re-adjust before comparing across a
corporate action.
Universe: Kite's NSE instrument list, equity type, excluding bonds/G-secs/
T-bills. Delisted stocks aren't in today's list, so the history has
survivorship bias for them.
Prints counts only.
"""
import datetime as dt, io, json, os, re, sys, threading, time, urllib.error, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from private_repo import PrivateRepo

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
RATE = 2.2                     # requests per second: under Kite's 3/s, leaving room for the ranges job
WORKERS = 3
HIST_START = dt.date(2015, 2, 1)
PROGRESS = "market/minute_progress.json"
# Debt and other non-equity series that Kite also types as "EQ" on NSE
NON_EQUITY = re.compile(r"-(N\d|Y\d|Z\d|GB|GS|SG|TB|D\d|P\d|NB|IV|RR|MF)$")


# ── Kite ─────────────────────────────────────────────────────────────────────
def kite_client():
    from kiteconnect import KiteConnect
    from kite_token_reader import get_kite_token
    token, stale = get_kite_token(allow_overnight=True)   # yesterday's login works until ~06:00
    key = os.environ.get("KITE_API_KEY", "")
    if not token or stale or not key:
        return None
    k = KiteConnect(api_key=key, timeout=30)
    k.set_access_token(token)
    return k


def universe(kite):
    """{tradingsymbol: instrument_token} for NSE equities."""
    out = {}
    for i in kite.instruments("NSE"):
        s = i.get("tradingsymbol", "")
        if i.get("segment") == "NSE" and i.get("instrument_type") == "EQ" and s and not NON_EQUITY.search(s):
            out[s] = i["instrument_token"]
    return out


class Limiter:
    def __init__(self, rate):
        self.gap, self.next, self.lock = 1.0 / rate, 0.0, threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next)
            self.next = t + self.gap
        time.sleep(max(0.0, t - now))


def fetch_all(kite, toks, frm, to, deadline=None, sink=None, batch=200):
    """Minute candles for every symbol between two dates (inclusive, <= 60
    days), fetched in batches of `batch` stocks. Each finished batch goes to
    `sink` (a ParquetSink) and is then dropped, so a whole month for ~2,700
    stocks never sits in memory at once.
    Returns (stocks_with_data, failed_symbols, stopped_early)."""
    from concurrent.futures import ThreadPoolExecutor
    lim, failed, stop, got = Limiter(RATE), [], threading.Event(), 0

    def one(item, res):
        sym, tok = item
        if stop.is_set():
            return
        for attempt in range(4):
            if deadline and time.monotonic() > deadline:
                stop.set()
                return
            lim.wait()
            try:
                c = kite.historical_data(tok, dt.datetime.combine(frm, dt.time(0, 0)),
                                         dt.datetime.combine(to, dt.time(23, 59)), "minute")
                if c:
                    res[sym] = c
                return
            except Exception as e:
                name = type(e).__name__
                if "Token" in name:                 # login expired: no point continuing
                    stop.set()
                    failed.append(sym)
                    return
                if name in ("PermissionException", "InputException"):
                    return                          # not retryable: Kite has no candles to give for this stock
                time.sleep(2.0 * (attempt + 1))     # 429 / network: back off and retry
        failed.append(sym)

    items = sorted(toks.items())
    with ThreadPoolExecutor(WORKERS) as ex:
        for i in range(0, len(items), batch):
            res = {}
            list(ex.map(lambda it: one(it, res), items[i:i + batch]))
            if stop.is_set():
                break
            got += len(res)
            if sink is not None and res:
                sink.add(res)
    return got, failed, stop.is_set()


# ── Parquet ──────────────────────────────────────────────────────────────────
class ParquetSink:
    """Builds one Parquet file in memory, one row group per batch of stocks."""
    COLS = ("symbol", "time", "open", "high", "low", "close", "volume")

    def __init__(self):
        import pyarrow as pa, pyarrow.parquet as pq
        self.pa = pa
        self.schema = pa.schema([("symbol", pa.dictionary(pa.int32(), pa.string())),
                                 ("time", pa.timestamp("ms", tz="Asia/Kolkata")),
                                 ("open", pa.float64()), ("high", pa.float64()), ("low", pa.float64()),
                                 ("close", pa.float64()), ("volume", pa.int64())],
                                metadata={b"fetched_on": dt.datetime.now(IST).date().isoformat().encode(),
                                          b"note": b"Kite adjusts past candles for splits/bonuses as of fetched_on; "
                                                   b"daily files keep that day's prices. Re-adjust with corporate actions before comparing."})
        self.buf = io.BytesIO()
        self.w = pq.ParquetWriter(self.buf, self.schema, compression="zstd", compression_level=9)
        self.rows = 0

    def add(self, res):
        pa, cols = self.pa, {k: [] for k in self.COLS}
        for s in sorted(res):
            for c in res[s]:
                t = c["date"]
                if not isinstance(t, dt.datetime):
                    t = dt.datetime.fromisoformat(str(t))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=IST)
                cols["symbol"].append(s)
                cols["time"].append(t)
                for f in ("open", "high", "low", "close"):
                    cols[f].append(float(c[f]))
                try:
                    v = int(c.get("volume") or 0)
                except (TypeError, ValueError, OverflowError):   # NaN / junk in old candles
                    v = None
                # Kite's older candles occasionally carry a corrupt volume beyond 64 bits
                # (Mar 2015 crashed the backfill): store it as missing, keep the prices.
                cols["volume"].append(v if v is not None and -2**63 <= v < 2**63 else None)
        if not cols["symbol"]:
            return
        arr = [pa.array(cols["symbol"], type=pa.string()).dictionary_encode().cast(self.schema.field("symbol").type),
               pa.array(cols["time"], type=self.schema.field("time").type)] + \
              [pa.array(cols[f], type=pa.float64()) for f in ("open", "high", "low", "close")] + \
              [pa.array(cols["volume"], type=pa.int64())]
        self.w.write_table(pa.Table.from_arrays(arr, schema=self.schema))
        self.rows += len(cols["symbol"])

    def close(self):
        self.w.close()
        return self.buf.getvalue()


def to_parquet(res):
    sink = ParquetSink()
    sink.add(res)
    return sink.close(), sink.rows


# ── Releases in the private repo ─────────────────────────────────────────────
class Releases:
    def __init__(self, priv):
        self.repo, self.hdr = priv.repo, dict(priv.hdr)

    def _call(self, method, url, data=None, headers=None):
        h = dict(self.hdr, **(headers or {}))
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        with urllib.request.urlopen(req, timeout=900) as r:
            body = r.read()
            return json.loads(body) if body else None

    def release(self, tag, title):
        try:
            return self._call("GET", f"https://api.github.com/repos/{self.repo}/releases/tags/{tag}")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
        body = json.dumps({"tag_name": tag, "name": title, "body": "1-minute candles, NSE equities (Kite). Parquet, zstd.",
                           "draft": False, "prerelease": True}).encode()
        return self._call("POST", f"https://api.github.com/repos/{self.repo}/releases", body,
                          {"Content-Type": "application/json"})

    def assets(self, rel):
        out, page = [], 1
        while True:
            got = self._call("GET", f"https://api.github.com/repos/{self.repo}/releases/{rel['id']}/assets?per_page=100&page={page}")
            out += got or []
            if not got or len(got) < 100:
                return out
            page += 1

    def has(self, rel, name):
        return any(a["name"] == name for a in self.assets(rel))

    def upload(self, rel, name, data):
        for a in self.assets(rel):
            if a["name"] == name:                  # replace a partial or earlier copy
                self._call("DELETE", f"https://api.github.com/repos/{self.repo}/releases/assets/{a['id']}")
        url = f"https://uploads.github.com/repos/{self.repo}/releases/{rel['id']}/assets?name={name}"
        return self._call("POST", url, data, {"Content-Type": "application/octet-stream"})


def too_many_failed(failed, toks):
    """Transient failures (429s, network) above ~1% of stocks: don't save the file as
    complete; the next run fetches it again. A few stragglers are accepted (and
    counted in the progress file) so one bad symbol can't block the backfill."""
    return len(failed) > max(10, len(toks) // 100)


# ── modes ────────────────────────────────────────────────────────────────────
def month_end(m0):
    return dt.date(m0.year + (m0.month == 12), m0.month % 12 + 1, 1) - dt.timedelta(days=1)


def months(start, end):
    m = dt.date(start.year, start.month, 1)
    while m <= end:
        yield m, min(end, month_end(m))
        m = month_end(m) + dt.timedelta(days=1)


def archive(kite, priv, day=None):
    """Save today's file, and any of the last 30 days that are missing (a day
    without a login, or a run that never happened). Kite serves 60 days of
    minute candles, so missed days can still be fetched."""
    import market_session
    today = day or dt.datetime.now(IST).date()
    rels = Releases(priv)
    prog, sha = priv.get_json(PROGRESS, None)
    prog = prog or {}
    start = max(dt.date.fromisoformat(prog["archive_from"]), today - dt.timedelta(days=30)) \
        if prog.get("archive_from") else today
    toks, fetched_days = None, 0
    no_session = set(prog.get("no_session", []))
    for k in range((today - start).days + 1):
        d = start + dt.timedelta(days=k)
        ses = market_session.session(d)
        if not ses or d.isoformat() in no_session or (d == today and dt.datetime.now(IST) < ses[1] and day is None):
            continue
        if fetched_days >= 3:                     # at most 3 days per run (~21 min each); later runs catch up
            print("Catch-up limit reached for this run; the next run continues.")
            break
        rel = rels.release(f"minute-{d:%Y-%m}", f"Minute candles {d:%b %Y}")
        name = f"minute-{d.isoformat()}.parquet"
        if rels.has(rel, name) and not (d == today and os.environ.get("FORCE") == "1"):
            continue
        toks = toks or universe(kite)
        # Probe one liquid stock first: a holiday missing from the calendar costs
        # one request, not a ~2,700-request pass repeated for 30 days.
        probe = next((toks[s_] for s_ in ("RELIANCE", "HDFCBANK", "TCS", "INFY") if s_ in toks), None)
        if probe is not None:
            try:
                if not kite.historical_data(probe, dt.datetime.combine(d, dt.time(0, 0)),
                                            dt.datetime.combine(d, dt.time(23, 59)), "minute"):
                    print(f"{d}: no trading (not in the holiday calendar) — noted, skipped from now on.")
                    no_session.add(d.isoformat())
                    prog["no_session"] = sorted(no_session)
                    prog_now, sha = priv.get_json(PROGRESS, None)
                    prog_now = prog_now or {}
                    prog_now["no_session"] = prog["no_session"]
                    priv.put_json(PROGRESS, prog_now, f"minute bars: {d} had no trading", sha)
                    continue
            except Exception as e:
                if "Token" in type(e).__name__:
                    print("Kite login expired — stopping."); break
        sink = ParquetSink()
        got, failed, stopped = fetch_all(kite, toks, d, d, sink=sink)
        data = sink.close()
        fetched_days += 1
        if stopped:
            print(f"{d}: stopped early with {got} of {len(toks)} stocks (login expired?) — not saved; the next run retries.")
            break
        if not got:
            print(f"{d}: no candles")
            continue
        if too_many_failed(failed, toks):
            print(f"{d}: {len(failed)} stocks failed after retries (rate limit / network) — not saved; the next run retries.")
            continue
        rels.upload(rel, name, data)
        print(f"{d}: saved {sink.rows:,} candles for {got} of {len(toks)} stocks ({len(failed)} failed), {len(data)/1e6:.1f} MB.")
        if not prog.get("archive_from"):
            prog_now, sha = priv.get_json(PROGRESS, None)
            prog_now = prog_now or {}
            prog_now["archive_from"] = prog["archive_from"] = d.isoformat()   # the history backfill stops the day before
            priv.put_json(PROGRESS, prog_now, "minute bars: daily archive started", sha)


def history(kite, priv, budget_min=320):
    deadline = time.monotonic() + budget_min * 60
    prog, _ = priv.get_json(PROGRESS, None)
    prog = prog or {}
    if prog.get("archive_from"):
        end = dt.date.fromisoformat(prog["archive_from"]) - dt.timedelta(days=1)
    else:   # daily archive not started yet: only whole months, so no part-month is marked done
        t = dt.datetime.now(IST).date()
        end = dt.date(t.year, t.month, 1) - dt.timedelta(days=1)
    done = set(prog.get("months_done", []))
    rels = Releases(priv)
    rel = rels.release("minute-history", "Minute candles history")
    toks = universe(kite)
    did, last_dur = 0, 30 * 60
    for m0, m1 in months(HIST_START, end):
        key = f"{m0:%Y-%m}"
        if key in done:
            continue
        if time.monotonic() > deadline - max(35 * 60, 1.3 * last_dur):   # don't start a month we can't finish
            break
        t_month = time.monotonic()
        sink = ParquetSink()
        got, failed, stopped = fetch_all(kite, toks, m0, m1, deadline, sink=sink)
        data = sink.close()
        if stopped:
            print(f"{key}: stopped before finishing (time budget or login expired); redone next run.")
            break
        if too_many_failed(failed, toks):
            print(f"{key}: {len(failed)} stocks failed after retries (rate limit / network) — not marked done; redone next run.")
            break
        if got:
            rels.upload(rel, f"minute-hist-{key}.parquet", data)
            print(f"{key}: {sink.rows:,} candles, {got} stocks with data, {len(failed)} failed, {len(data)/1e6:.0f} MB")
        else:
            print(f"{key}: no candles (before the data starts)")
        done.add(key)
        last_dur = time.monotonic() - t_month
        prog, sha = priv.get_json(PROGRESS, None)   # re-read: the daily archive may have written it
        prog = prog or {}
        prog["months_done"] = sorted(done)
        if failed:
            prog.setdefault("failed", {})[key] = len(failed)
        prog["updated_at"] = dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
        priv.put_json(PROGRESS, prog, f"minute history: {key}", sha)
        did += 1
    remaining = sum(1 for m0, _ in months(HIST_START, end) if f"{m0:%Y-%m}" not in done)
    print(f"Done {did} month(s) this run; {remaining} to go.")
    return remaining


def main():
    priv = PrivateRepo()
    kite = kite_client()
    mode = sys.argv[1] if len(sys.argv) > 1 else "archive"
    left = None
    if not priv.ok or kite is None:
        print("No private-repo access or no fresh Kite login today: nothing to do.")
    elif mode == "archive":
        archive(kite, priv)
    elif mode == "history":
        # Today's archive comes first: GitHub keeps only one waiting run per
        # concurrency group, so a history run started around the close could
        # otherwise push the day's archive out of the queue.
        t0 = time.monotonic()
        try:
            import market_session
            now = dt.datetime.now(IST)
            ses = market_session.session(now.date())
            if (ses and now >= ses[1]) or now.time() < dt.time(5, 40):   # overnight: the last session
                archive(kite, priv)
        except Exception as e:
            print(f"::warning::today's archive check failed ({type(e).__name__})")
        budget = int(sys.argv[2]) if len(sys.argv) > 2 else 320
        left = history(kite, priv, max(0, budget - int((time.monotonic() - t0) // 60)))
    out = os.environ.get("GITHUB_OUTPUT")
    if out and left is not None:
        with open(out, "a") as f:
            f.write(f"remaining={left}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
