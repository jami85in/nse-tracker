#!/usr/bin/env python3
"""
Fetches sector-membership lists (which stocks belong to which sectoral
index) via the Worker's /sectors endpoint, and writes
data/backtest/sector_membership.json — a simple {symbol: [sector names]}
map (a stock can belong to more than one sectoral index, e.g. a bank that's
in both Nifty Bank and Nifty Private Bank).

This is a light, infrequent job — sector composition only changes at
semi-annual rebalances — so it just runs once and refreshes occasionally,
not on the 5-min collector schedule.
"""
import json, os, urllib.request

WORKER_URL = os.environ.get("BACKTEST_WORKER_URL", "").rstrip("/")
OUT_PATH = "data/backtest/sector_membership.json"


def main():
    if not WORKER_URL:
        print("ERROR: BACKTEST_WORKER_URL not set.")
        return

    url = f"{WORKER_URL}/sectors"
    req = urllib.request.Request(url, headers={"User-Agent": "nse-tracker-sectors/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"ERROR fetching sector lists: {e}")
        return

    sectors = data.get("sectors", {})
    print(f"Fetched {len(sectors)} sector lists:")
    for name, syms in sectors.items():
        print(f"  {name}: {len(syms)} stocks")

    # Invert to symbol -> [sectors]
    membership = {}
    for sector_name, symbols in sectors.items():
        for sym in symbols:
            membership.setdefault(sym, []).append(sector_name)

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump({
            "sector_lists": sectors,       # sector -> [symbols], for reference
            "symbol_to_sectors": membership,  # symbol -> [sectors], for lookup
        }, f)
    print(f"\nWritten {OUT_PATH}: {len(membership)} symbols mapped to at least one sector.")


if __name__ == "__main__":
    main()
