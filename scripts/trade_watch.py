#!/usr/bin/env python3
"""
trade_watch.py — keeps the trade ledger current DURING the day.

Runs inside the price loop (every 10 minutes while the market is open). For
each Kite-connected account that has logged in today (HUF, and wife/mother
once their Kite apps are set up) it asks Kite for today's trades and, when
there is a trade the private repo hasn't seen yet, merges them into that
account's daily file there (ledger/daily/<today>.json for the HUF,
ledger/accounts/<name>/daily/<today>.json for the others): the same file, format and merge-by-trade-id
that the evening kite_capture.py uses, so the two never conflict. That commit
starts the private Trade Ledger workflow, and the dashboard shows the trade
about 10-15 minutes after it executes, not the next evening.

Only trades are written. The holdings snapshot stays with the evening capture:
during the day Kite still lists today's buys under positions, not holdings.

PRIVACY: this runs in the PUBLIC repo, whose logs anyone can read, so it
prints nothing about trades: no symbols, quantities, prices, counts or even
whether anything changed. Only a failure is reported, by its type.
"""
import base64, datetime as dt, hashlib, json, os, sys, time, urllib.error, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
STATE = os.path.join(os.environ.get("RUNNER_TEMP") or "/tmp", "trade_watch.sig")


def _clean(x):
    return {k: (v.isoformat() if isinstance(v, (dt.datetime, dt.date)) else v) for k, v in x.items()}


def _key(t):
    return (str(t.get("exchange")), str(t.get("trade_id")))


def signature(trades):
    return hashlib.sha256("|".join(sorted(":".join(_key(t)) for t in trades)).encode()).hexdigest()


class GitHub:
    """Minimal contents API client for the private repo."""

    def __init__(self, repo, pat):
        self.base = f"https://api.github.com/repos/{repo}/contents/"
        self.hdr = {"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json",
                    "User-Agent": "nse-tracker-trade-watch"}

    def get(self, path):
        """(parsed_json, sha), or (None, None) if the file doesn't exist yet."""
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + path, headers=self.hdr), timeout=30) as r:
                env = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, None
            raise
        return json.loads(base64.b64decode(env.get("content") or b"e30=").decode() or "{}"), env.get("sha")

    def put(self, path, obj, sha, message):
        body = {"message": message, "content": base64.b64encode(json.dumps(obj, indent=0).encode()).decode()}
        if sha:
            body["sha"] = sha
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers=self.hdr, method="PUT")
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status


def sync(trades, gh, today, now_str, daily_dir="ledger/daily"):
    """Merge `trades` into <daily_dir>/<today>.json in the private repo.
    Returns True if the file was written, False if it already had them all."""
    path = f"{daily_dir}/{today}.json"
    for attempt in range(3):
        cur, sha = gh.get(path)
        have = {_key(t): t for t in ((cur or {}).get("trades") or [])}
        new = [t for t in trades if _key(t) not in have]
        if not new:
            return False
        for t in trades:                     # refresh rows Kite re-sends, add the new ones
            have[_key(t)] = t
        doc = dict(cur or {})
        doc.update({"date": today, "captured_at": now_str, "trades": list(have.values())})
        try:
            gh.put(path, doc, sha, f"trades: sync during the session, {now_str}")
            return True
        except urllib.error.HTTPError as e:
            if e.code in (409, 422) and attempt < 2:   # the evening capture wrote at the same moment
                time.sleep(3)
                continue
            raise
    return False


# Kite-connected accounts: (name, token file in the private repo, API-key env var,
# folder for the day's trades in the private repo). An account is skipped
# until its key is set and it has logged in today.
ACCOUNTS = [
    ("huf",    "kite_token.json",    "KITE_API_KEY",        "ledger/daily"),
    ("wife",   "tokens/wife.json",   "KITE_API_KEY_WIFE",   "ledger/accounts/wife/daily"),
    ("mother", "tokens/mother.json", "KITE_API_KEY_MOTHER", "ledger/accounts/mother/daily"),
]


def watch(name, token_path, api_key, daily_dir, gh):
    from kite_token_reader import token_is_stale
    tok, _ = gh.get(token_path)
    if not tok or not tok.get("access_token") or token_is_stale(tok):
        return
    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(tok["access_token"])
    trades = [_clean(t) for t in (kite.trades() or [])]
    if not trades:
        return
    # No local "already synced" cache: sync() compares with what the private file
    # really holds, so a fill dropped by a concurrent ledger merge is written back
    # in the next round.
    now = dt.datetime.now(IST)
    sync(trades, gh, now.date().isoformat(), f"{now:%Y-%m-%d %H:%M} IST", daily_dir)


def main():
    repo, pat = os.environ.get("SECRETS_REPO", ""), os.environ.get("SECRETS_REPO_PAT", "")
    if not (repo and pat):
        return 0
    gh = GitHub(repo, pat)
    for name, token_path, key_env, daily_dir in ACCOUNTS:
        api_key = os.environ.get(key_env, "")
        if not api_key:
            continue
        try:
            watch(name, token_path, api_key, daily_dir, gh)
        except Exception as e:                # one account failing mustn't stop the others
            print(f"::warning::trade sync skipped one account this round ({type(e).__name__})")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:                    # never print the payload — only what kind of failure
        print(f"::warning::trade sync skipped this round ({type(e).__name__})")
        sys.exit(0)
