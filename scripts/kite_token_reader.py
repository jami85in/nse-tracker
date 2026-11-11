#!/usr/bin/env python3
"""
Shared helper: fetch today's Kite access token from the private secrets
repo at runtime. Import this from scan.py / prices.py.

Required environment variables (set as GitHub Actions secrets on the
PUBLIC repo — read-only access to the private repo, never write):
  SECRETS_REPO_PAT   — GitHub PAT with read access to the private repo
  SECRETS_REPO       — "yourusername/nse-tracker-secrets"
  KITE_API_KEY       — needed alongside the access_token to auth with Kite
"""
import json, os, base64, urllib.request, urllib.error, datetime

TOKEN_FILE_PATH = "kite_token.json"


def get_kite_token():
    """
    Returns (access_token, is_stale) or (None, None) if unavailable.
    is_stale=True if the token was generated on a date earlier than today
    (IST), or before Zerodha's ~06:00 IST daily reset when it is now past it —
    a signal that today's login hasn't happened yet.
    """
    secrets_repo = os.environ.get("SECRETS_REPO", "")
    pat = os.environ.get("SECRETS_REPO_PAT", "")
    if not secrets_repo or not pat:
        return None, None

    url = f"https://api.github.com/repos/{secrets_repo}/contents/{TOKEN_FILE_PATH}"
    headers = {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "nse-tracker-kite-reader",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            envelope = json.loads(resp.read().decode())
        token_data = json.loads(base64.b64decode(envelope["content"]).decode())
    except (urllib.error.HTTPError, urllib.error.URLError, KeyError, json.JSONDecodeError):
        return None, None

    access_token = token_data.get("access_token")
    return access_token, token_is_stale(token_data)


# Zerodha cancels every Kite access token in its daily reset at about 06:00 IST,
# so a token from a login between midnight and 06:00 carries today's date but
# stops working that morning (11 Nov 2026: a 02:10 login looked "fresh" all day
# while every price request was refused).
KITE_RESET = datetime.time(6, 0)


def token_is_stale(token_data, now=None):
    """True if the token can't be today's working token: generated on an earlier
    date, or before today's ~06:00 IST reset when it is now past it."""
    ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    now = now or datetime.datetime.now(ist)
    if token_data.get("generated_date") != now.date().isoformat():
        return True
    try:
        hm = str(token_data.get("generated_at", ""))[11:16]          # "YYYY-MM-DD HH:MM IST"
        made = datetime.time(int(hm[:2]), int(hm[3:5]))
    except (ValueError, IndexError):
        return False                                                # older files: date check only
    return made < KITE_RESET <= now.time()


if __name__ == "__main__":
    token, stale = get_kite_token()
    if token is None:
        print("No token available (check SECRETS_REPO / SECRETS_REPO_PAT env vars).")
    else:
        print(f"Token found. Stale (not from today): {stale}")
        print(f"Token (first 8 chars): {token[:8]}...")
