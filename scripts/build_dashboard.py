#!/usr/bin/env python3
"""
build_dashboard.py — turn the editable dashboard source (app.src.html) into
the page GitHub Pages serves (index.html).

Why: the source keeps its React code as JSX in a <script type="text/babel">
block and ships Babel Standalone (3.1 MB) so the browser compiles it on
every visit. Measured on 12 Nov 2026 at phone-class CPU speed, that took
about 5 seconds before anything appeared; precompiled, the same page shows
in about 0.4 s and the file drops from 3.6 MB to 0.5 MB.

So: edit app.src.html (it still runs on its own, Babel included, for
testing), and this script compiles the JSX once with esbuild, removes the
Babel bundle and writes index.html. The "Build dashboard" workflow runs it
on every change to app.src.html.

Safety checks before index.html is replaced: esbuild must succeed, the
output must keep BUILD_VERSION and the root element, must not still contain
a text/babel block or any Babel script (inline or from a CDN), and must be
under 1.5 MB. Any failure leaves the current index.html untouched (exit 1).
The inline Babel bundle is found by the "Babel Standalone" banner near the
top of its <script> block; if that banner ever changes the bundle stays in,
and the "text/babel" / size checks fail the build rather than ship it.

index.html is generated: don't upload it by hand — edit app.src.html.

Usage: python scripts/build_dashboard.py [src] [out]   (defaults: app.src.html index.html)
Needs node + npx (esbuild is fetched by npx, pinned below).
"""
import os
import re
import subprocess
import sys
import tempfile

ESBUILD = os.environ.get("ESBUILD", "esbuild@0.24.0")
MAX_BYTES = 1_500_000


def fail(msg):
    print(f"::error::{msg}")
    sys.exit(1)


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "app.src.html"
    out = sys.argv[2] if len(sys.argv) > 2 else "index.html"
    html = open(src, encoding="utf-8").read()

    scripts = list(re.finditer(r"<script([^>]*)>(.*?)</script>", html, re.S))
    babel_lib = [m for m in scripts if "Babel Standalone" in m.group(2)[:400]]
    jsx = [m for m in scripts if "text/babel" in m.group(1)]
    if len(jsx) != 1:
        fail(f"expected exactly one <script type=\"text/babel\"> block, found {len(jsx)}")
    if len(babel_lib) > 1:
        fail("found more than one Babel Standalone block")

    with tempfile.TemporaryDirectory() as tmp:
        src_js = os.path.join(tmp, "app.jsx")
        out_js = os.path.join(tmp, "app.js")
        open(src_js, "w", encoding="utf-8").write(jsx[0].group(2))
        cmd = ["npx", "--yes", ESBUILD, src_js, "--loader:.jsx=jsx", "--jsx=transform",
               "--target=es2018", "--charset=utf8", f"--outfile={out_js}", "--log-level=warning"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            fail("esbuild failed:\n" + (r.stderr or r.stdout)[-3000:])
        if r.stderr.strip():
            print(r.stderr.strip()[-2000:])
        js = open(out_js, encoding="utf-8").read()

    if "</script" in js.lower():
        js = re.sub(r"</(script)", r"<\\/\1", js, flags=re.I)   # never end the inline block early

    # Splice back to front so earlier offsets stay valid.
    page = html
    edits = [(jsx[0].start(), jsx[0].end(), "<script>\n" + js + "</script>")]
    if babel_lib:
        edits.append((babel_lib[0].start(), babel_lib[0].end(), ""))
    for start, end, text in sorted(edits, reverse=True):
        page = page[:start] + text + page[end:]
    page = page.replace("<!-- built from app.src.html -->\n", "")
    page = page.replace("<head>", "<head>\n<!-- built from app.src.html by scripts/build_dashboard.py — edit that file, not this one -->", 1)

    ver = re.search(r'BUILD_VERSION\s*=\s*"([^"]+)"', page)
    checks = [
        (ver is not None, "BUILD_VERSION missing from the output"),
        ('id="root"' in page or "id='root'" in page, "root element missing from the output"),
        ("text/babel" not in page, "a text/babel block is still present"),
        (not re.search(r"<script[^>]+src=[^>]*babel", page, re.I), "a Babel <script src> is still present"),
        (len(page.encode()) < MAX_BYTES, f"output is {len(page.encode()):,} bytes (limit {MAX_BYTES:,})"),
    ]
    for ok, msg in checks:
        if not ok:
            fail(msg)

    tmp_out = out + ".tmp"
    open(tmp_out, "w", encoding="utf-8").write(page)
    os.replace(tmp_out, out)
    print(f"Built {out} from {src}: build {ver.group(1)}, {len(html.encode()):,} -> {len(page.encode()):,} bytes")


if __name__ == "__main__":
    main()
