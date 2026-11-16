#!/usr/bin/env python3
"""
Which stocks the family accounts (other than the HUF) hold, so the live-price
job quotes them too.

The HUF's holdings come from Kite itself (fetch_zerodha_holdings.py). The
other family accounts have no Kite login here; their holdings are worked out
by the Trade Ledger in the private nse-tracker-secrets repo. This reads each
of those accounts' ledger (ledger/accounts/<id>/portfolio_ledger.json) with the
same read key the token reader uses, and writes data/family_holdings.json.

PRIVACY: like data/zerodha_holdings.json, this repo is public, so the file
holds only WHICH symbols are held and the names to quote them by — never the
account, quantity, cost or P&L.

Kept separate from zerodha_holdings.json on purpose: the dashboard treats a
symbol missing from that file as "sold" for the HUF.

Safe degradation: on any error the existing file is left as it is.
"""
import base64, datetime, json, os, re, sys, urllib.error, urllib.request

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
OUT_PATH = "data/family_holdings.json"
ISIN_RE = re.compile(r"^IN[A-Z0-9]{10}$")
SUFFIX_RE = re.compile(r"-(E\d|Z|X|T|B|XT|BE|SM|ST)$")


def _get(repo, pat, path):
    """A file from the private repo; the raw media type also serves files over 1 MB."""
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    hdr = {"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github.raw",
           "User-Agent": "nse-tracker-family-holdings"}
    with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=60) as r:
        return json.loads(r.read().decode())


def candidates(row):
    """Kite instruments to try, best first. Zerodha's own name for the stock
    (from the account's Console holdings statement) comes first, then the
    ledger's symbol, each on NSE and then BSE."""
    names = []
    for n in (row.get("zerodha_symbol"), row.get("symbol")):
        n = (n or "").strip().upper()
        if n and n not in names:
            names.append(n)
        b = SUFFIX_RE.sub("", n)
        if b and b not in names:
            names.append(b)
    first = (row.get("zerodha_exchange") or "NSE").upper()
    order = [first] + [x for x in ("NSE", "BSE") if x != first]
    return [f"{ex}:{n}" for n in names for ex in order]


def main():
    now = datetime.datetime.now(IST)
    print(f"=== Family holdings sync: {now.strftime('%Y-%m-%d %H:%M IST')} ===")
    repo, pat = os.environ.get("SECRETS_REPO", ""), os.environ.get("SECRETS_REPO_PAT", "")
    if not repo or not pat:
        print("  SECRETS_REPO / SECRETS_REPO_PAT not set — leaving the file as it is.")
        return
    try:
        accts = _get(repo, pat, "ledger/accounts.json").get("accounts", [])
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        print(f"  Could not read the accounts list ({e}) — leaving the file as it is.")
        return

    holdings, read_ok, read_fail = {}, 0, 0
    for a in accts:
        aid = a.get("id")
        if not aid or aid == "huf":          # the HUF is synced from Kite directly
            continue
        try:
            led = _get(repo, pat, f"ledger/accounts/{aid}/portfolio_ledger.json")
            read_ok += 1
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
            print(f"  One account's ledger could not be read ({type(e).__name__}).")
            read_fail += 1
            continue
        for h in led.get("holdings", []):
            sym = (h.get("symbol") or "").strip().upper()
            if not sym or (h.get("qty") or 0) <= 0 or ISIN_RE.match(sym) or h.get("unlisted"):
                continue                      # unlisted shares have no quote
            c = candidates(h)
            if sym in holdings:
                c = holdings[sym]["try"] + [x for x in c if x not in holdings[sym]["try"]]
            holdings[sym] = {"try": c}

    if read_fail and not read_ok:
        print("  No account could be read — leaving the file as it is.")
        return
    out = {
        "updated_at": now.strftime("%Y-%m-%d %H:%M IST"),
        "source": "family accounts' Trade Ledger holdings; symbols only (accounts, quantities and prices are kept private)",
        "count": len(holdings),
        "holdings": dict(sorted(holdings.items())),
    }
    os.makedirs("data", exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(out, f)
    print(f"  ✓ {len(holdings)} symbols written to {OUT_PATH} from {read_ok} account(s).")


if __name__ == "__main__":
    main()
