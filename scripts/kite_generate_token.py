#!/usr/bin/env python3
"""
Daily Kite Connect access-token generator — writes to a PRIVATE repo.

SEBI regulations require every broker's API access token to expire daily.
This is the "get a fresh token" step you run once each morning, taking
under a minute.

This version writes the generated token to a SEPARATE PRIVATE repo (not
this public nse-tracker repo), so the live, sensitive access token is
never exposed publicly. The public repo's scan.py/prices.py fetch the
token at runtime via the GitHub API using a read-only PAT.

Required environment variables:
  KITE_API_KEY       — your Kite Connect API key
  KITE_API_SECRET    — your Kite Connect API secret
  SECRETS_REPO_PAT   — a GitHub Personal Access Token with write access
                        to the private secrets repo (see setup notes below)
  SECRETS_REPO       — "yourusername/nse-tracker-secrets" (the private repo)

HOW TO USE (once a day, ideally before market open):

1. Run with no arguments to get today's login URL:
       python3 kite_generate_token.py

2. Open that URL, log in to Zerodha (+2FA if prompted). You'll land on a
   URL like:
       https://127.0.0.1/?request_token=XXXXX&action=login&status=success
   (The page itself errors out since 127.0.0.1 isn't a real server —
   that's expected. Just grab request_token from the address bar.)

3. Run again with that token:
       python3 kite_generate_token.py XXXXX

4. This exchanges it for an access_token and commits it directly to the
   PRIVATE repo via the GitHub API (no local git operations needed) —
   the public repo's workflows will read it from there at runtime.
"""
import json, os, sys, datetime, base64, urllib.request, urllib.error
from kiteconnect import KiteConnect

API_KEY = os.environ.get("KITE_API_KEY", "")
API_SECRET = os.environ.get("KITE_API_SECRET", "")
SECRETS_REPO_PAT = os.environ.get("SECRETS_REPO_PAT", "")
SECRETS_REPO = os.environ.get("SECRETS_REPO", "")  # e.g. "jami85in/nse-tracker-secrets"
TOKEN_FILE_PATH = "kite_token.json"  # path WITHIN the private repo

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def github_api_request(method, url, token, body=None):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "kite-token-refresher",
    }
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()
        return {"_error": True, "status": e.code, "body": body_text}


def commit_token_to_private_repo(token_data):
    """Write kite_token.json into the private secrets repo via GitHub's
    Contents API — no local git clone needed, just two small API calls."""
    api_base = f"https://api.github.com/repos/{SECRETS_REPO}/contents/{TOKEN_FILE_PATH}"

    # Step 1: check if the file already exists (need its SHA to update it)
    existing = github_api_request("GET", api_base, SECRETS_REPO_PAT)
    sha = existing.get("sha") if not existing.get("_error") else None

    content_b64 = base64.b64encode(json.dumps(token_data).encode()).decode()
    payload = {
        "message": f"kite: token refreshed {token_data['generated_at']}",
        "content": content_b64,
    }
    if sha:
        payload["sha"] = sha

    result = github_api_request("PUT", api_base, SECRETS_REPO_PAT, payload)
    if result.get("_error"):
        print(f"ERROR writing to private repo: HTTP {result['status']}")
        print(f"  {result['body']}")
        print()
        print("Common causes:")
        print("  - SECRETS_REPO_PAT doesn't have 'contents: write' permission on the repo")
        print("  - SECRETS_REPO value is wrong (should be 'username/repo-name')")
        print("  - The private repo doesn't exist yet — create it first (empty is fine)")
        sys.exit(1)
    print(f"Token committed to private repo: {SECRETS_REPO}/{TOKEN_FILE_PATH}")


def main():
    if not API_KEY or not API_SECRET:
        print("ERROR: KITE_API_KEY and KITE_API_SECRET must be set.")
        sys.exit(1)

    kite = KiteConnect(api_key=API_KEY)

    if len(sys.argv) < 2:
        print("=" * 70)
        print("STEP 1: Open this URL in your browser and log in to Zerodha:")
        print()
        print(kite.login_url())
        print()
        print("After logging in, you'll land on a URL like:")
        print("  https://127.0.0.1/?request_token=XXXXX&action=login&status=success")
        print("(The page itself may show a browser error — that's fine, ignore it.)")
        print()
        print("Copy the value after 'request_token=' and run:")
        print(f"  python3 {sys.argv[0]} <request_token>")
        print("=" * 70)
        return

    if not SECRETS_REPO_PAT or not SECRETS_REPO:
        print("ERROR: SECRETS_REPO_PAT and SECRETS_REPO must be set to commit the token")
        print("to your private repo. See setup notes in this script's docstring.")
        sys.exit(1)

    request_token = sys.argv[1].strip()
    print("Exchanging request_token for an access_token...")

    try:
        data = kite.generate_session(request_token, api_secret=API_SECRET)
    except Exception as e:
        print(f"ERROR generating session: {e}")
        print("Usually means the request_token expired or was mistyped — start over from step 1.")
        sys.exit(1)

    access_token = data["access_token"]
    now_ist = datetime.datetime.now(IST)

    token_data = {
        "access_token": access_token,
        "generated_at": now_ist.strftime("%Y-%m-%d %H:%M IST"),
        "generated_date": now_ist.date().isoformat(),
        "user_id": data.get("user_id"),
        "user_name": data.get("user_name"),
    }

    commit_token_to_private_repo(token_data)
    print(f"SUCCESS. Logged in as: {data.get('user_name')} ({data.get('user_id')})")
    print("This token is valid until it expires per SEBI rules (typically ~market")
    print("close or early the next morning) — repeat this process tomorrow.")


if __name__ == "__main__":
    main()
