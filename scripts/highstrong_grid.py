"""
highstrong_grid.py — simulate GTT-style trades (limit target, market stop,
unlimited hold) restricted to HIGH-confidence + STRONG-conviction squeeze
signals only, across a target% x stop% grid, broken out by entry year.

FILTER (replicates scan.py exactly):
  Base squeeze:  bb_width < 4.5, bb_width <= bb_width_5d_min*1.6,
                 stoch_k < 50, stoch_k > stoch_d
  HIGH confidence (deterministic fallback formula from scan.py's
                 _fallback_commentary — see chat for why this, not the
                 live AI judgment, is what a backtest can use):
                   bb_width < 1.8  AND  predicted_return >= 6
                   where predicted_return = (pivot_r1 - price) / price * 100
  STRONG conviction (scan.py's score_conviction(), which subsumes
                 "not countertrend" — STRONG requires price > BOTH EMAs):
                   price > ema10 AND price > ema30
                   AND |stoch_k - stoch_d| >= 3.0

EXECUTION (GTT-accurate, same model validated earlier this session):
  Entry: next day's open, +0.15% slippage (market buy).
  Target: LIMIT sell — fills at exactly target price, or better if price
          gaps above it. No slippage.
  Stop:   MARKET sell — fills at min(stop, that day's open) to capture
          gap-through risk, MINUS 0.15% slippage.
  No time cap — held until target or stop actually fires. ~0.222% round-trip
  cost (Zerodha delivery: STT+stamp+exchange, zero brokerage) on every trade.
"""
import json, glob, os, sys, time
import numpy as np, pandas as pd

OHLC_DIR = "data/backtest/raw_ohlc"
BB_THRESHOLD = 4.5
SLIP = 0.0015
COST = 0.00222

TARGETS = [0.02, 0.025, 0.03, 0.034, 0.04, 0.045, 0.05]
STOPS = [0.05, 0.06, 0.07]


def add_ind(df):
    df = df.copy()
    df["bb_mid"] = df["close"].rolling(20).mean()
    df["bb_std"] = df["close"].rolling(20).std()
    df["bb_width_pct"] = (4 * df["bb_std"]) / df["bb_mid"] * 100
    df["ema10"] = df["close"].ewm(span=10, adjust=False).mean()
    df["ema30"] = df["close"].ewm(span=30, adjust=False).mean()
    d = df["close"].diff()
    g = d.clip(lower=0).rolling(14).mean(); l = (-d.clip(upper=0)).rolling(14).mean()
    rs = g / l.replace(0, np.nan); df["rsi"] = 100 - (100 / (1 + rs))
    df.loc[(l == 0) & (g > 0), "rsi"] = 100.0; df.loc[(l == 0) & (g == 0), "rsi"] = 50.0
    rmin = df["rsi"].rolling(14).min(); rmax = df["rsi"].rolling(14).max()
    st = (df["rsi"] - rmin) / (rmax - rmin).replace(0, np.nan) * 100
    st = st.where(rmax != rmin, 100.0)
    df["stoch_k"] = st.rolling(3).mean(); df["stoch_d"] = df["stoch_k"].rolling(3).mean()
    df["bb5"] = df["bb_width_pct"].rolling(5).min()
    # Standard pivot R1, needed for the HIGH-confidence predicted_return check
    prev_close = df["close"].shift(1); prev_high = df["high"].shift(1); prev_low = df["low"].shift(1)
    pivot = (prev_high + prev_low + prev_close) / 3
    df["r1"] = 2 * pivot - prev_low
    return df


def find_strong_signals(df):
    """Returns a boolean array: True where bar i is a STRONG-conviction
    squeeze signal (price fully above both EMAs + clear stoch crossover gap
    -- this already excludes countertrend by construction, per scan.py's
    score_conviction()). The separate HIGH-confidence fallback formula is
    dropped per the chat discussion: it's ~1000x rarer than what actually
    shows as HIGH on the live dashboard (which is normally an AI judgment,
    not this rigid formula), so it isn't a meaningful thing to backtest."""
    bbw = df["bb_width_pct"].values; bb5 = df["bb5"].values
    sk = df["stoch_k"].values; sd = df["stoch_d"].values
    e10 = df["ema10"].values; e30 = df["ema30"].values
    c = df["close"].values
    n = len(df)
    out = np.zeros(n, dtype=bool)
    for i in range(20, n):
        if np.isnan(bbw[i]) or np.isnan(sk[i]) or np.isnan(sd[i]) or np.isnan(bb5[i]):
            continue
        base = (bbw[i] < BB_THRESHOLD) and (bbw[i] <= bb5[i] * 1.6) and (sk[i] < 50) and (sk[i] > sd[i])
        if not base:
            continue
        price = c[i]
        is_strong = (price > e10[i]) and (price > e30[i]) and (abs(sk[i] - sd[i]) >= 3.0)
        out[i] = is_strong
    return out


def simulate_one(symbol, raw, target_pct, stop_pct):
    if not raw or len(raw) < 60:
        return []
    df = pd.DataFrame(raw)
    if "open" not in df:
        return []
    df = df.dropna(subset=["open", "close", "high", "low"]).reset_index(drop=True)
    if len(df) < 60:
        return []
    df = add_ind(df)
    signals = find_strong_signals(df)

    o = df["open"].values; c = df["close"].values; h = df["high"].values; l = df["low"].values
    dt = df["date"].astype(str).values
    n = len(df)

    out = []
    i = 0
    while i < n - 2:
        if not signals[i]:
            i += 1
            continue
        er = o[i + 1]
        if not (er > 0):
            i += 1
            continue
        ef = er * (1 + SLIP)
        stp = ef * (1 - stop_pct)
        tgt = ef * (1 + target_pct)
        result, xraw, xidx = "OPEN", None, None
        for j in range(i + 1, n):
            if l[j] <= stp:
                xraw = min(stp, o[j]); result = "STOP"; xidx = j; break
            if h[j] >= tgt:
                xraw = max(tgt, o[j]); result = "TARGET"; xidx = j; break
        entry_date = dt[i]
        if result == "OPEN":
            out.append({"symbol": symbol, "entry_date": entry_date, "result": "OPEN"})
            i = n
            continue
        exit_slip = SLIP if result == "STOP" else 0.0  # limit target: no slip
        xf = xraw * (1 - exit_slip)
        net = ((xf - ef) / ef - COST) * 100
        gross = (xraw - er) / er * 100
        hold = xidx - (i + 1)
        bh_exit = c[xidx] * (1 - SLIP)
        bh_net = ((bh_exit - ef) / ef - COST) * 100
        out.append({"symbol": symbol, "entry_date": entry_date, "result": result,
                    "net": round(float(net), 3), "gross": round(float(gross), 3),
                    "bh_net": round(float(bh_net), 3), "hold": int(hold)})
        i = xidx + 1
    return out


def run_grid(files, targets, stops, time_budget=None):
    start = time.time()
    results = {}  # (target,stop) -> list of trades
    for tp in targets:
        for sp in stops:
            trades = []
            for f in files:
                try:
                    raw = json.load(open(f))
                except Exception:
                    continue
                sym = os.path.basename(f).replace(".json", "")
                trades += simulate_one(sym, raw, tp, sp)
            results[(tp, sp)] = trades
            elapsed = time.time() - start
            n_resolved = sum(1 for t in trades if t["result"] in ("TARGET", "STOP"))
            print(f"  target={tp*100:.1f}% stop={sp*100:.0f}%: {n_resolved} resolved trades "
                  f"(+{sum(1 for t in trades if t['result']=='OPEN')} open) [{elapsed:.0f}s elapsed]")
            if time_budget and elapsed > time_budget:
                print(f"  time budget ({time_budget}s) reached — stopping grid early.")
                return results
    return results


def summarize_by_year(trades):
    """Returns {year: {n, target_pct, stop_pct, net, bh_net, edge, avg_hold}}
    plus an 'ALL' key for the full-period aggregate. OPEN (unresolved)
    trades are counted separately, excluded from the return stats."""
    resolved = [t for t in trades if t["result"] in ("TARGET", "STOP")]
    opened = [t for t in trades if t["result"] == "OPEN"]
    by_year = {}
    for t in resolved:
        by_year.setdefault(t["entry_date"][:4], []).append(t)

    def block(ts):
        n = len(ts)
        if n == 0:
            return {"n": 0}
        tg = sum(1 for t in ts if t["result"] == "TARGET")
        net = sum(t["net"] for t in ts) / n
        bh = sum(t["bh_net"] for t in ts) / n
        hold = sum(t["hold"] for t in ts) / n
        return {"n": n, "target_pct": round(tg/n*100, 1), "stop_pct": round((n-tg)/n*100, 1),
                "net": round(net, 3), "bh_net": round(bh, 3), "edge": round(net-bh, 3),
                "avg_hold": round(hold, 1)}

    out = {yr: block(by_year[yr]) for yr in sorted(by_year)}
    out["ALL"] = block(resolved)
    out["_open"] = len(opened)
    return out


RESULTS_PATH = "/tmp/highstrong_grid_results.json"


def load_results():
    try:
        return json.load(open(RESULTS_PATH))
    except Exception:
        return {}


def save_results(d):
    json.dump(d, open(RESULTS_PATH, "w"))


def run_remaining(files, targets, stops, max_combos_this_call=6):
    """Resumable: processes up to max_combos_this_call combos not already in
    the results file, saves after EACH combo (not just at the end), so a
    killed/interrupted run loses at most one in-progress combo."""
    results = load_results()
    all_combos = [(tp, sp) for tp in targets for sp in stops]
    todo = [c for c in all_combos if f"{c[0]}_{c[1]}" not in results]
    print(f"{len(results)}/{len(all_combos)} combos already done. {len(todo)} remaining.")
    if not todo:
        print("All combos complete.")
        return results

    done_this_call = 0
    for tp, sp in todo:
        if done_this_call >= max_combos_this_call:
            print(f"Hit this-call cap ({max_combos_this_call}); "
                  f"{len(todo) - done_this_call} combo(s) still remaining — run again to continue.")
            break
        t0 = time.time()
        trades = []
        for f in files:
            try:
                raw = json.load(open(f))
            except Exception:
                continue
            sym = os.path.basename(f).replace(".json", "")
            trades += simulate_one(sym, raw, tp, sp)
        summ = summarize_by_year(trades)
        results[f"{tp}_{sp}"] = {"target": tp, "stop": sp, "summary": summ}
        save_results(results)  # persist immediately after every combo
        done_this_call += 1
        print(f"  [{done_this_call}/{min(max_combos_this_call,len(todo))}] "
              f"target={tp*100:.1f}% stop={sp*100:.0f}%: "
              f"n={summ['ALL'].get('n',0)} net={summ['ALL'].get('net','—')}% "
              f"({time.time()-t0:.0f}s)")
    return results


if __name__ == "__main__":
    files = sorted(glob.glob(os.path.join(OHLC_DIR, "*.json")))
    print(f"Universe: {len(files)} files")
    t0 = time.time()
    probe = run_grid(files, [0.03], [0.05])
    t1 = time.time()
    trades = probe[(0.03, 0.05)]
    n_resolved = sum(1 for t in trades if t["result"] in ("TARGET", "STOP"))
    print(f"\nPROBE: 1 combo took {t1-t0:.1f}s, {n_resolved} resolved trades.")
    print(f"Estimated full 21-combo grid: ~{(t1-t0)*21/60:.1f} minutes")
    summ = summarize_by_year(trades)
    print("\nSample year breakdown for this probe combo (target=3%, stop=5%):")
    for yr, b in summ.items():
        if yr.startswith("_"): continue
        if b.get("n"):
            print(f"  {yr}: n={b['n']:>5} target={b['target_pct']}% net={b['net']:+.3f}% edge={b['edge']:+.3f}%")
