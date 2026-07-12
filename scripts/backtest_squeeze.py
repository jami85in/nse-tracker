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

# ── Execution realism ──────────────────────────────────────────────────
# You only see the signal AFTER the close, so you cannot fill at that close —
# entry is modelled at the NEXT day's open. Friction is modelled so expectancy
# reflects what actually leaves the account, not a frictionless ideal.
SLIPPAGE_PCT = float(os.environ.get("BT_SLIPPAGE_PCT", "0.15")) / 100.0   # each side
# Round-trip taxes/fees as a fraction of trade value. Default ≈ Zerodha
# delivery: STT 0.20% (0.10% buy + 0.10% sell) + stamp 0.015% (buy) +
# exchange/SEBI/GST ≈ 0.007% -> ~0.222% round trip, zero brokerage. Add
# brokerage via BT_BROKERAGE_PCT (per side) for a full-service broker.
COST_ROUND_TRIP = float(os.environ.get("BT_COST_ROUND_TRIP_PCT", "0.222")) / 100.0
COST_ROUND_TRIP += 2 * (float(os.environ.get("BT_BROKERAGE_PCT", "0.0")) / 100.0)
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
    opens  = df["open"].values if "open" in df else df["close"].values
    dates  = df["date"].astype(str).values
    bbw = df["bb_width_pct"].values; bbw5 = df["bb_width_5d_min"].values
    sk = df["stoch_k"].values; sd = df["stoch_d"].values
    e10 = df["ema10"].values; e30 = df["ema30"].values
    n = len(df)

    out = []
    i = 20
    while i < n - 2:  # need bar i+1 to exist for the next-open entry
        if (not np.isnan(bbw[i]) and not np.isnan(sk[i]) and not np.isnan(sd[i])
                and not np.isnan(bbw5[i])
                and bbw[i] < BB_THRESHOLD
                and bbw[i] <= bbw5[i] * 1.6
                and sk[i] < 50 and sk[i] > sd[i]):
            # Enter at the NEXT day's open (the earliest achievable fill).
            entry_raw = opens[i + 1]
            if not (entry_raw > 0):
                i += 1; continue
            entry_fill = entry_raw * (1 + SLIPPAGE_PCT)          # pay up on the buy
            entry_date = dates[i]                                # signal date
            tgt = entry_fill * (1 + TARGET_PCT)
            stp = entry_fill * (1 - STOP_PCT)

            result, exit_raw, exit_idx = "TIMEOUT", None, None
            last_j = min(i + 1 + MAX_HOLD, n - 1)
            for j in range(i + 1, last_j + 1):
                # Stop checked first (conservative). Gap-through fills at the
                # open, which for a stop is WORSE than the stop price — models
                # the real cost of gap-downs.
                if lows[j] <= stp:
                    exit_raw = min(stp, opens[j]); result = "STOP"; exit_idx = j; break
                if highs[j] >= tgt:
                    exit_raw = max(tgt, opens[j]); result = "TARGET"; exit_idx = j; break
            if result == "TIMEOUT":
                exit_idx = last_j
                exit_raw = closes[exit_idx]

            exit_fill = exit_raw * (1 - SLIPPAGE_PCT)            # give up on the sell
            gross = (exit_raw - entry_raw) / entry_raw * 100     # frictionless, next-open
            net = ((exit_fill - entry_fill) / entry_fill - COST_ROUND_TRIP) * 100
            hold = exit_idx - (i + 1)

            # Benchmark: buy the SAME stock at the same next-open and just hold
            # MAX_HOLD days (net of the same friction). Isolates whether the
            # target/stop logic adds anything over passive holding.
            bh_idx = min(i + 1 + MAX_HOLD, n - 1)
            bh_exit_fill = closes[bh_idx] * (1 - SLIPPAGE_PCT)
            bh_net = ((bh_exit_fill - entry_fill) / entry_fill - COST_ROUND_TRIP) * 100

            resolved = (result != "TIMEOUT") or (i + 1 + MAX_HOLD < n)
            if entry_after is None or entry_date > entry_after:
                out.append({
                    "symbol": symbol,
                    "entry_date": entry_date,
                    "result": result,
                    "ret": round(float(net), 3),        # NET is the headline return now
                    "gross": round(float(gross), 3),    # frictionless next-open, for comparison
                    "bh_net": round(float(bh_net), 3),  # buy-and-hold benchmark, net
                    "hold": int(hold),
                    "bb_width": round(float(bbw[i]), 3),
                    "trend_conflict": bool((entry_raw < e10[i]) and (entry_raw < e30[i])),
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
        net = sum(t["ret"] for t in ts) / n
        gross = sum(t.get("gross", t["ret"]) for t in ts) / n
        bh = sum(t.get("bh_net", 0) for t in ts) / n
        return {"trades": n, "target_pct": round(tg/n*100, 1),
                "stop_pct": round(st/n*100, 1), "timeout_pct": round(tm/n*100, 1),
                "expectancy_pct": round(net, 3),            # NET (after costs+slippage)
                "gross_pct": round(gross, 3),               # frictionless next-open
                "buyhold_net_pct": round(bh, 3),            # buy-and-hold same period, net
                "edge_vs_buyhold": round(net - bh, 3)}      # does the exit logic add value?
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
    param_sig = f"bb{BB_THRESHOLD}_rsi14_stoch14_nextopen_slip{SLIPPAGE_PCT}_cost{COST_ROUND_TRIP}_tgt{TARGET_PCT}_stp{STOP_PCT}_hold{MAX_HOLD}"
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
