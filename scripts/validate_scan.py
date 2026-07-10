"""
validate_scan.py — end-to-end validation of the scan pipeline BEFORE deploy.

Runs the real indicator + classify + output-serialisation path over a broad
sample of the committed universe (deliberately including short-history small-
caps that produce NaN indicators), then asserts:
  1. The output serialises AND strictly re-parses as JSON (allow_nan=False) —
     this is the exact check the browser does; it catches the "NaN is not valid
     JSON" crash.
  2. No NaN/Inf survives anywhere in the payload.
  3. The dual Stoch RSI is wired correctly: signal (14) and chart (40/60) are
     both present and genuinely different (e.g. UBL signal ~25 vs chart ~74).
  4. Every displayed candidate has the fields the frontend reads.
Exits non-zero on any failure so it can gate deploys.
"""
import sys, json, glob, os, math
import importlib.util
import pandas as pd

SCAN_PATH = os.environ.get("SCAN_PATH", "/tmp/scan.py")
spec = importlib.util.spec_from_file_location("scanmod", SCAN_PATH)
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)

failures = []
def check(cond, msg):
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        failures.append(msg)

OHLC_DIR = "data/backtest/raw_ohlc"
files = sorted(glob.glob(os.path.join(OHLC_DIR, "*.json")))
print(f"Universe available: {len(files)} symbols")

# Deliberately sample across the spectrum: known names + a slice of everything
# (small-caps with short history are where the NaN crash originated).
import random
random.seed(7)
must = ["UBL","SBIN","WALCHANNAG","PFIZER","SUNTV","RELIANCE","LODHA"]
sample = [f for f in files if os.path.basename(f).replace(".json","") in must]
sample += random.sample(files, min(400, len(files)))
sample = list(dict.fromkeys(sample))
print(f"Validating over {len(sample)} symbols (incl. {', '.join(must)})\n")

print("[1] Running add_indicators + classify over the sample...")
candidates = {"squeeze_stocks": [], "countertrend_stocks": [], "blast_stocks": [],
              "watchlist_stocks": [], "short_squeeze_stocks": [], "short_breakdown_stocks": []}
from dataclasses import asdict
errors = 0
nan_indicator_symbols = 0
for f in sample:
    sym = os.path.basename(f).replace(".json","")
    try:
        raw = json.load(open(f))
        df = pd.DataFrame(raw); df["date"] = pd.to_datetime(df["date"])
        for c in ["open","high","low","close"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        di = scan.add_indicators(df)
        if scan.indicators_look_broken(di):
            continue
        last = di.iloc[-1]
        if pd.isna(last.get("stoch_k_chart")):
            nan_indicator_symbols += 1  # expected for short-history names
        cand = scan.classify(sym, di, scan_date="2026-07-10")
        if cand and hasattr(cand, "setup"):
            d = asdict(cand)
            bucket = {"SQUEEZE":"squeeze_stocks","BLAST":"blast_stocks",
                      "WATCHLIST":"watchlist_stocks"}.get(d.get("setup"))
            if bucket:
                candidates[bucket].append(d)
    except Exception as e:
        errors += 1
        print(f"    ! {sym}: {type(e).__name__}: {e}")
check(errors == 0, f"classify ran without exceptions ({errors} errors)")
print(f"    (short-history symbols with NaN chart-stoch, expected: {nan_indicator_symbols})")
print(f"    candidates: squeeze={len(candidates['squeeze_stocks'])} "
      f"blast={len(candidates['blast_stocks'])} watchlist={len(candidates['watchlist_stocks'])}")

print("\n[2] Serialising output and strict-parsing as the browser would...")
output = {"scan_time": "2026-07-10 12:00 IST", "market_mood": "TEST", **candidates}
import io
buf = io.StringIO()
try:
    scan.dump_json_safe(output, buf)
    text = buf.getvalue()
    check(True, "dump_json_safe wrote without error")
except Exception as e:
    check(False, f"dump_json_safe raised: {e}")
    text = ""
check("NaN" not in text, "raw output contains no literal NaN token")
check("Infinity" not in text, "raw output contains no literal Infinity token")
try:
    reparsed = json.loads(text)  # strict: rejects NaN/Infinity
    check(True, "json.loads (strict) round-trips — browser will parse it")
except Exception as e:
    check(False, f"strict json.loads FAILED (dashboard would crash): {e}")

print("\n[3] Scanning payload for any non-finite numeric...")
def find_nonfinite(o, path=""):
    bad = []
    if isinstance(o, dict):
        for k,v in o.items(): bad += find_nonfinite(v, f"{path}.{k}")
    elif isinstance(o, list):
        for i,v in enumerate(o): bad += find_nonfinite(v, f"{path}[{i}]")
    elif isinstance(o, float) and not math.isfinite(o):
        bad.append(path)
    return bad
bad = find_nonfinite(json_safe_out := json.loads(text) if text else {})
check(len(bad) == 0, f"no non-finite floats in payload ({len(bad)} found)")

print("\n[4] Dual Stoch RSI wiring (signal 14 vs chart 40/60)...")
def stoch_for(sym):
    raw = json.load(open(os.path.join(OHLC_DIR, sym+".json")))
    df = pd.DataFrame(raw); df["date"]=pd.to_datetime(df["date"])
    for c in ["open","high","low","close"]: df[c]=pd.to_numeric(df[c],errors="coerce")
    last = scan.add_indicators(df).iloc[-1]
    return last["stoch_k"], last.get("stoch_k_chart")
sk_ubl, skc_ubl = stoch_for("UBL")
print(f"    UBL: signal K={sk_ubl:.1f}, chart K={skc_ubl:.1f}")
check(abs(skc_ubl - 74) < 8, "UBL chart stoch (~74) matches TradingView 73.03")
check(sk_ubl < 50, "UBL signal stoch (14) is the low/oversold value (<50)")
check(abs(sk_ubl - skc_ubl) > 15, "signal and chart stoch are genuinely different indicators")

print("\n[5] Required display fields present on candidates...")
req = ["symbol","price","bb_width","stoch_k","stoch_d","stoch_k_chart","stoch_d_chart",
       "ema10","ema30","target_price","trend_conflict"]
allc = candidates["squeeze_stocks"] + candidates["watchlist_stocks"]
missing_any = False
for d in allc[:50]:
    miss = [k for k in req if k not in d]
    if miss:
        missing_any = True; print(f"    ! {d.get('symbol')} missing {miss}")
check(not missing_any, "all sampled candidates carry the required display fields")

print("\n" + "="*56)
if failures:
    print(f"VALIDATION FAILED — {len(failures)} issue(s):")
    for f in failures: print("  - " + f)
    sys.exit(1)
print("VALIDATION PASSED — safe to generate/deploy.")
sys.exit(0)
