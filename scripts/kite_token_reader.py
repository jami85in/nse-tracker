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
    (IST) — a signal that the daily manual refresh hasn't happened yet.
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
    generated_date = token_data.get("generated_date")

    ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    today_ist = datetime.datetime.now(ist).date().isoformat()
    is_stale = (generated_date != today_ist)

    return access_token, is_stale


if __name__ == "__main__":
    token, stale = get_kite_token()
    if token is None:
        print("No token available (check SECRETS_REPO / SECRETS_REPO_PAT env vars).")
    else:
        print(f"Token found. Stale (not from today): {stale}")
        print(f"Token (first 8 chars): {token[:8]}...")
