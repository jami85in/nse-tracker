#!/usr/bin/env python3
"""
sync_repo.py — download the whole nse-tracker repo to your laptop, and push
local file changes back to GitHub in ONE atomic commit, from the command
line. No git installation required (talks to the GitHub REST API directly).

WHY THIS EXISTS
---------------
Manual copy/paste into GitHub's web editor is exactly what caused the mix-up
today — a workflow's YAML got pasted into a .py file's edit box, and a report
landed in the wrong folder. This script moves exact file bytes, so there's no
copy-paste step left to get wrong, and `upload` commits everything you list
in a single all-or-nothing commit (not one save-per-file).

RECOMMENDATION: now that you're on a laptop, plain git (see the bottom of
this file) is the sturdier long-term tool — full history, diffs, easy undo.
This script is the no-install bridge until/unless you set that up.

──────────────────────────────────────────────────────────────────────────
SETUP (one-time)
──────────────────────────────────────────────────────────────────────────
  pip install requests

  `download` needs no authentication (public repo).

  `upload` needs a GitHub Personal Access Token with write access:
    github.com -> Settings -> Developer settings -> Personal access tokens
    -> Fine-grained tokens -> New token
       Repository access: Only select repositories -> jami85in/nse-tracker
       Permissions: Contents -> Read and write
  Then set it as an environment variable (don't paste it into scripts,
  chats, or share it with anyone/anything — treat it like a password):
    macOS/Linux (bash/zsh):  export GITHUB_TOKEN=github_pat_xxxxxxxx
    Windows (PowerShell):    $env:GITHUB_TOKEN = "github_pat_xxxxxxxx"

──────────────────────────────────────────────────────────────────────────
USAGE
──────────────────────────────────────────────────────────────────────────
  Download the whole repo to ./nse-tracker:
    python sync_repo.py download

  Download to a specific folder:
    python sync_repo.py download --dest "C:\\Users\\you\\nse-tracker"

  Upload specific local files back to GitHub (one commit for all of them):
    python sync_repo.py upload scripts/scan.py index.html -m "update scan + frontend"

  Upload EVERY file under your local repo folder (mirrors local -> remote):
    python sync_repo.py upload --all --dest ./nse-tracker -m "full sync"

  Before uploading, ALWAYS sanity-check what you're about to push:
    python sync_repo.py upload scripts/scan.py --dry-run

──────────────────────────────────────────────────────────────────────────
EQUIVALENT WITH REAL GIT (recommended once installed)
──────────────────────────────────────────────────────────────────────────
    git clone https://github.com/jami85in/nse-tracker.git
    cd nse-tracker
    # ...edit files...
    git add -A
    git commit -m "your message"
    git push
"""
import argparse
import base64
import io
import json
import os
import sys
import tarfile
import urllib.error
import urllib.request

OWNER = "jami85in"
REPO = "nse-tracker"
BRANCH = "main"
API = f"https://api.github.com/repos/{OWNER}/{REPO}"


# ── download ────────────────────────────────────────────────────────────

def download(dest):
    url = f"https://codeload.github.com/{OWNER}/{REPO}/tar.gz/refs/heads/{BRANCH}"
    print(f"Downloading {OWNER}/{REPO}@{BRANCH} ...")
    try:
        with urllib.request.urlopen(url) as resp:
            data = resp.read()
    except urllib.error.URLError as e:
        print(f"ERROR: download failed ({e}). Check your internet connection.")
        sys.exit(1)
    print(f"Downloaded {len(data)/1e6:.1f} MB. Extracting to {dest} ...")
    os.makedirs(dest, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = tar.getmembers()
        # GitHub's tarball wraps everything in a "<repo>-<branch>/" folder —
        # strip that so files land directly in `dest`.
        clean = []
        for m in members:
            parts = m.name.split("/", 1)
            if len(parts) > 1 and parts[1]:
                m.name = parts[1]
                clean.append(m)
        tar.extractall(dest, members=clean)
    n_files = sum(1 for _, _, files in os.walk(dest) for _ in files)
    print(f"Done. {n_files} files written to: {os.path.abspath(dest)}")


# ── upload (GitHub Git Data API: one atomic multi-file commit) ───────────

def _headers(token):
    return {"Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def _request(method, path, token, payload=None):
    url = f"{API}/{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, headers={**_headers(token), "Content-Type": "application/json"},
                                  data=data, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"ERROR: GitHub API {method} {path} -> HTTP {e.code}\n{body}")
        sys.exit(1)


def upload(local_files, message, token, repo_root=None, dry_run=False):
    if not token:
        print("ERROR: no token found. Set the GITHUB_TOKEN environment variable "
              "(see the SETUP section at the top of this file) or pass --token.")
        sys.exit(1)
    if not local_files:
        print("ERROR: no files given. List files, or use --all with --dest."); sys.exit(1)

    missing = [f for f in local_files if not os.path.isfile(f)]
    if missing:
        print("ERROR: these local files don't exist:")
        for f in missing:
            print(f"  {f}")
        sys.exit(1)

    print(f"Preparing to commit {len(local_files)} file(s):")
    entries = []
    for local_path in local_files:
        rel_path = (os.path.relpath(local_path, repo_root) if repo_root else local_path).replace(os.sep, "/")
        size = os.path.getsize(local_path)
        print(f"  {rel_path}  ({size:,} bytes)")
        entries.append((local_path, rel_path))

    if dry_run:
        print("\n[dry run] Nothing was uploaded. Remove --dry-run to actually push.")
        return

    print("\nFetching current branch state...")
    ref = _request("GET", f"git/ref/heads/{BRANCH}", token)
    base_commit_sha = ref["object"]["sha"]
    base_commit = _request("GET", f"git/commits/{base_commit_sha}", token)
    base_tree_sha = base_commit["tree"]["sha"]

    print("Uploading file contents (creating blobs)...")
    tree_entries = []
    for local_path, rel_path in entries:
        with open(local_path, "rb") as f:
            content = f.read()
        b64 = base64.b64encode(content).decode()
        blob = _request("POST", "git/blobs", token, {"content": b64, "encoding": "base64"})
        tree_entries.append({"path": rel_path, "mode": "100644", "type": "blob", "sha": blob["sha"]})

    print("Building tree + commit...")
    new_tree = _request("POST", "git/trees", token,
                         {"base_tree": base_tree_sha, "tree": tree_entries})
    new_commit = _request("POST", "git/commits", token,
                           {"message": message, "tree": new_tree["sha"], "parents": [base_commit_sha]})

    print("Updating main branch (fast-forward)...")
    _request("PATCH", f"git/refs/heads/{BRANCH}", token, {"sha": new_commit["sha"]})

    print(f"\n✓ Pushed {len(tree_entries)} file(s) in one commit: {new_commit['sha'][:7]}")
    print(f"  \"{message}\"")
    print(f"  https://github.com/{OWNER}/{REPO}/commit/{new_commit['sha']}")


# ── CLI ────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Download/upload the nse-tracker repo without git.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("download", help="Download the whole repo to a local folder")
    d.add_argument("--dest", default="./nse-tracker", help="Local folder to download into")

    u = sub.add_parser("upload", help="Push local files back to GitHub in one commit")
    u.add_argument("files", nargs="*", help="Local file paths to upload")
    u.add_argument("--all", action="store_true", help="Upload every file under --dest")
    u.add_argument("--dest", default="./nse-tracker",
                   help="Local repo folder root (used to compute the GitHub path for each file)")
    u.add_argument("-m", "--message", default="update via sync_repo.py", help="Commit message")
    u.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"),
                   help="GitHub token (defaults to $GITHUB_TOKEN)")
    u.add_argument("--dry-run", action="store_true", help="Show what would be uploaded, without pushing")

    args = p.parse_args()

    if args.cmd == "download":
        download(args.dest)

    elif args.cmd == "upload":
        if args.all:
            files = []
            for root, dirs, names in os.walk(args.dest):
                dirs[:] = [dd for dd in dirs if dd != ".git"]
                for name in names:
                    files.append(os.path.join(root, name))
            upload(files, args.message, args.token, repo_root=args.dest, dry_run=args.dry_run)
        else:
            # Explicit file list: assumed to already be repo-relative paths
            # (run this from inside your downloaded repo folder, e.g.
            # `cd nse-tracker && python ../sync_repo.py upload scripts/scan.py`).
            upload(args.files, args.message, args.token, repo_root=None, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
