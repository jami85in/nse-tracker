"""
backtest_squeeze.py — squeeze-strategy trade simulator (resumable / incremental)

Replicates scan.py's exact long-squeeze detection and simulates each signal
forward with fixed exit rules, across all stocks in data/backtest/raw_ohlc/.
Produces:
  data/backtest/squeeze_trades.json   — every simulated trade (the baseline)
  data/backtest/squeeze_report.json   — aggregate + year + trend-conflict stats

WHY IT'S INCREMENTAL
--------------------
Re-deriving 6 years of trades from scratch each run is wasteful once a baseline
exists. On re-run we keep every previously-stored trade whose entry_date is
older than a safe replay cutoff, and only re-simulate the recent tail. The
cutoff = (previous last_ohlc_date − REPLAY_BUFFER_DAYS). The buffer exists
because a trade entered in the final ~20 trading days of the previous data may
have been resolved with truncated forward data; re-simulating that tail lets
those trades resolve properly against the newly-appended bars. Everything
before the cutoff is immutable and simply carried forward. So each run only
does real work on "today → last time" — exactly the delta.

Signal (long squeeze), identical to scan.py detect logic:
    bb_width < BB_THRESHOLD
    and bb_width <= bb_width_5d_min * 1.6      (still near recent squeeze low)
    and stoch_k < 50
    and stoch_k > stoch_d                       (turning up)

Trades are stored at BB_THRESHOLD = 4.5 (the production threshold). Any tighter
threshold ≤4.5 can be evaluated later by simply filtering on the stored
bb_width — no re-run needed. Each trade also records trend_conflict (price
below BOTH EMAs at entry), which the analysis showed is the real edge divider.

Exit rules (fixed): +TARGET_PCT target, −STOP_PCT stop, MAX_HOLD-day timeout.
Entry at the signal-day close. Stop is checked before target on any bar that
spans both (conservative). No overlapping trades on the same stock.
"""
import json, glob, os, datetime
import numpy as np
import pandas as pd

OHLC_DIR   = "data/backtest/raw_ohlc"
TRADES_OUT = "data/backtest/squeeze_trades.json"
REPORT_OUT = "data/backtest/squeeze_report.json"

BB_THRESHOLD = 4.5     # store everything qualifying at the production threshold
TARGET_PCT   = 0.04
STOP_PCT     = 0.05
MAX_HOLD     = 20      # trading days
REPLAY_BUFFER_DAYS = 45   # calendar days of tail to re-simulate on incremental runs


def add_indicators(df):
    df = df.copy()
    df["bb_mid"] = df["close"].rolling(20).mean()
    df["bb_std"] = df["close"].rolling(20).std()
    df["bb_upper"] = df["bb_mid"] + 2 * df["bb_std"]
    df["bb_lower"] = df["bb_mid"] - 2 * df["bb_std"]
    df["bb_width_pct"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"] * 100

    df["ema10"] = df["close"].ewm(span=10, adjust=False).mean()
    df["ema30"] = df["close"].ewm(span=30, adjust=False).mean()

    # SIGNAL Stoch RSI — must match scan.py's signal stoch exactly: RSI 14
    # (simple rolling mean), Stochastic 14, %K 3, %D 3. This is the money
    # signal the strategy trades on; the 40/60 chart stoch is display-only in
    # scan.py and plays no part in the backtest.
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))
    df.loc[(loss == 0) & (gain > 0), "rsi"] = 100.0
    df.loc[(loss == 0) & (gain == 0), "rsi"] = 50.0
    rsi_min = df["rsi"].rolling(14).min()
    rsi_max = df["rsi"].rolling(14).max()
    stoch = (df["rsi"] - rsi_min) / (rsi_max - rsi_min).replace(0, np.nan) * 100
    stoch = stoch.where(rsi_max != rsi_min, 100.0)
    df["stoch_k"] = stoch.rolling(3).mean()
    df["stoch_d"] = df["stoch_k"].rolling(3).mean()
    df["bb_width_5d_min"] = df["bb_width_pct"].rolling(5).min()
    return df


def simulate_stock(symbol, raw, entry_after=None):
    """Return list of trades for one stock. If entry_after (YYYY-MM-DD) is set,
    only emit trades whose entry_date > entry_after (used for incremental runs)."""
    if not raw or len(raw) < 60:
        return []
    df = pd.DataFrame(raw)
    if "high" not in df or "low" not in df:
        return []
    df = df.dropna(subset=["close", "high", "low"]).reset_index(drop=True)
    if len(df) < 60:
        return []
    df = add_indicators(df)

    closes = df["close"].values; highs = df["high"].values; lows = df["low"].values
    dates  = df["date"].astype(str).values
    bbw = df["bb_width_pct"].values; bbw5 = df["bb_width_5d_min"].values
    sk = df["stoch_k"].values; sd = df["stoch_d"].values
    e10 = df["ema10"].values; e30 = df["ema30"].values
    n = len(df)

    out = []
    i = 20
    while i < n - 1:
        if (not np.isnan(bbw[i]) and not np.isnan(sk[i]) and not np.isnan(sd[i])
                and not np.isnan(bbw5[i])
                and bbw[i] < BB_THRESHOLD
                and bbw[i] <= bbw5[i] * 1.6
                and sk[i] < 50 and sk[i] > sd[i]):
            entry = closes[i]
            if entry <= 0:
                i += 1; continue
            entry_date = dates[i]
            tgt = entry * (1 + TARGET_PCT); stp = entry * (1 - STOP_PCT)
            result, ret, hold = "TIMEOUT", None, MAX_HOLD
            exit_idx = min(i + MAX_HOLD, n - 1)
            for j in range(i + 1, min(i + MAX_HOLD + 1, n)):
                if lows[j] <= stp:
                    result, ret, hold, exit_idx = "STOP", -STOP_PCT * 100, j - i, j; break
                if highs[j] >= tgt:
                    result, ret, hold, exit_idx = "TARGET", TARGET_PCT * 100, j - i, j; break
            if result == "TIMEOUT":
                ret = (closes[exit_idx] - entry) / entry * 100
                hold = exit_idx - i
            # Was there enough forward data to actually resolve this trade?
            resolved = (result != "TIMEOUT") or (i + MAX_HOLD < n)
            if entry_after is None or entry_date > entry_after:
                out.append({
                    "symbol": symbol,
                    "entry_date": entry_date,
                    "result": result,
                    "ret": round(float(ret), 3),
                    "hold": int(hold),
                    "bb_width": round(float(bbw[i]), 3),
                    "trend_conflict": bool((entry < e10[i]) and (entry < e30[i])),
                    "resolved": bool(resolved),
                })
            i = exit_idx + 1
        else:
            i += 1
    return out


def build_report(trades):
    def block(ts):
        n = len(ts)
        if n == 0:
            return {"trades": 0}
        tg = sum(1 for t in ts if t["result"] == "TARGET")
        st = sum(1 for t in ts if t["result"] == "STOP")
        tm = sum(1 for t in ts if t["result"] == "TIMEOUT")
        exp = sum(t["ret"] for t in ts) / n
        return {"trades": n, "target_pct": round(tg/n*100, 1),
                "stop_pct": round(st/n*100, 1), "timeout_pct": round(tm/n*100, 1),
                "expectancy_pct": round(exp, 3)}
    by_year = {}
    for t in trades:
        by_year.setdefault(t["entry_date"][:4], []).append(t)
    aligned = [t for t in trades if not t["trend_conflict"]]
    conflict = [t for t in trades if t["trend_conflict"]]
    return {
        "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "params": {"bb_threshold": BB_THRESHOLD, "target_pct": TARGET_PCT*100,
                   "stop_pct": STOP_PCT*100, "max_hold_days": MAX_HOLD},
        "overall": block(trades),
        "by_year": {yr: block(by_year[yr]) for yr in sorted(by_year)},
        "trend_aligned": block(aligned),
        "trend_conflict": block(conflict),
    }


def main():
    files = sorted(glob.glob(os.path.join(OHLC_DIR, "*.json")))
    if not files:
        print(f"No OHLC files in {OHLC_DIR}"); return

    # Determine current data frontier
    def file_last_date(path):
        try:
            d = json.load(open(path))
            return str(d[-1]["date"]) if d else ""
        except Exception:
            return ""
    cur_last_date = max((file_last_date(f) for f in files), default="")

    prior = None
    if os.path.exists(TRADES_OUT):
        try:
            prior = json.load(open(TRADES_OUT))
        except Exception:
            prior = None

    # Signature of the strategy params. If it changed since the cached run
    # (e.g. the Stoch RSI was realigned to 40/60), every cached trade was
    # computed under a different rule and is invalid — force a full rebuild
    # rather than mixing old and new trades.
    param_sig = f"bb{BB_THRESHOLD}_rsi14_stoch14_k3_d3_tgt{TARGET_PCT}_stp{STOP_PCT}_hold{MAX_HOLD}"
    if prior and prior.get("param_sig") != param_sig:
        print(f"Strategy params changed (was {prior.get('param_sig')}, now {param_sig}); "
              f"discarding cache and rebuilding from scratch.")
        prior = None

    if prior and prior.get("trades") is not None and prior.get("last_ohlc_date"):
        # ── Incremental: keep old trades before the replay cutoff, redo the tail
        cutoff_dt = (datetime.date.fromisoformat(prior["last_ohlc_date"])
                     - datetime.timedelta(days=REPLAY_BUFFER_DAYS))
        cutoff = cutoff_dt.isoformat()
        kept = [t for t in prior["trades"] if t["entry_date"] < cutoff]
        print(f"Incremental run. Prior data to {prior['last_ohlc_date']}, "
              f"replay cutoff {cutoff}. Kept {len(kept)} immutable trades; "
              f"re-simulating everything on/after {cutoff}.")
        new_trades = []
        for f in files:
            sym = os.path.splitext(os.path.basename(f))[0]
            try:
                raw = json.load(open(f))
            except Exception:
                continue
            # entry_after = day before cutoff, so trades ON the cutoff are re-made
            new_trades += simulate_stock(sym, raw, entry_after=_prev_day(cutoff))
        trades = kept + new_trades
    else:
        print(f"Full baseline run over {len(files)} stocks (no prior cache).")
        trades = []
        for f in files:
            sym = os.path.splitext(os.path.basename(f))[0]
            try:
                raw = json.load(open(f))
            except Exception:
                continue
            trades += simulate_stock(sym, raw)

    # Dedupe by (symbol, entry_date) — last write wins
    seen = {}
    for t in trades:
        seen[(t["symbol"], t["entry_date"])] = t
    trades = sorted(seen.values(), key=lambda t: (t["entry_date"], t["symbol"]))

    payload = {
        "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "last_ohlc_date": cur_last_date,
        "param_sig": param_sig,
        "params": {"bb_threshold": BB_THRESHOLD, "target_pct": TARGET_PCT*100,
                   "stop_pct": STOP_PCT*100, "max_hold_days": MAX_HOLD,
                   "rsi_length": 14, "stoch_length": 14, "stoch_k": 3, "stoch_d": 3},
        "count": len(trades),
        "trades": trades,
    }
    os.makedirs(OHLC_DIR, exist_ok=True)
    with open(TRADES_OUT, "w") as f:
        json.dump(payload, f)
    with open(REPORT_OUT, "w") as f:
        json.dump(build_report(trades), f, indent=2)
    print(f"Wrote {len(trades)} trades -> {TRADES_OUT}")
    print(f"Wrote summary -> {REPORT_OUT}")


def _prev_day(iso):
    return (datetime.date.fromisoformat(iso) - datetime.timedelta(days=1)).isoformat()


if __name__ == "__main__":
    main()
