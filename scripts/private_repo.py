#!/usr/bin/env python3
"""Small helper for reading/writing files in the PRIVATE repo (SECRETS_REPO)
through GitHub's contents API, with the SECRETS_REPO_PAT token. Used by the
public workflows for anything personal or licensed (trades, alerts state,
minute bars), so none of it lands in this public repo."""
import base64, json, os, time, urllib.error, urllib.request


class PrivateRepo:
    def __init__(self, repo=None, pat=None):
        self.repo = repo or os.environ.get("SECRETS_REPO", "")
        pat = pat or os.environ.get("SECRETS_REPO_PAT", "")
        self.ok = bool(self.repo and pat)
        self.base = f"https://api.github.com/repos/{self.repo}/contents/"
        self.hdr = {"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json",
                    "User-Agent": "nse-tracker"}

    def get_bytes(self, path):
        """(bytes, sha) or (None, None) if missing. Files over 1 MB come back
        without content from this endpoint, so fetch those raw."""
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + path, headers=self.hdr), timeout=60) as r:
                env = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, None
            raise
        if env.get("content"):
            return base64.b64decode(env["content"]), env.get("sha")
        raw = dict(self.hdr, Accept="application/vnd.github.raw")
        with urllib.request.urlopen(urllib.request.Request(self.base + path, headers=raw), timeout=120) as r:
            return r.read(), env.get("sha")

    def get_json(self, path, default=None):
        b, sha = self.get_bytes(path)
        if b is None:
            return default, None
        return json.loads(b.decode() or "null"), sha

    def put_bytes(self, path, data, message, sha=None, retries=3):
        for attempt in range(retries):
            if sha is None:
                _, sha = self.get_bytes(path)
            body = {"message": message, "content": base64.b64encode(data).decode()}
            if sha:
                body["sha"] = sha
            req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers=self.hdr, method="PUT")
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                if e.code in (409, 422) and attempt < retries - 1:
                    sha = None
                    time.sleep(3)
                    continue
                raise

    def put_json(self, path, obj, message, sha=None):
        return self.put_bytes(path, json.dumps(obj, indent=0).encode(), message, sha)
