"""
unlimited_hold_experiment.py — same squeeze signal, same GTT-accurate fills
(limit target / market stop with gap-through), but NO time-based exit. A
trade stays open until price actually touches the target or the stop,
however long that takes. Tests TARGET_PCT in {3%, 4%}, STOP_PCT fixed at 5%.

Trades still open at the end of the committed data (mostly recent entries)
are reported separately as OPEN — they have no realized P&L yet, so they're
excluded from expectancy, not force-closed at an arbitrary price.
"""
import json, glob, os
import numpy as np, pandas as pd

OHLC_DIR = "data/backtest/raw_ohlc"
BB_THRESHOLD = 4.5
STOP_PCT = 0.05
SLIP = 0.0015     # 0.15%/side, entry + stop (market fills)
COST = 0.00222    # round-trip STT/stamp/exchange, ~Zerodha delivery

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
    return df


def simulate(symbol, raw, target_pct):
    if not raw or len(raw) < 60: return []
    df = pd.DataFrame(raw)
    if "open" not in df: return []
    df = df.dropna(subset=["open", "close", "high", "low"]).reset_index(drop=True)
    if len(df) < 60: return []
    df = add_ind(df)
    o = df["open"].values; c = df["close"].values; h = df["high"].values; l = df["low"].values
    dt = df["date"].astype(str).values
    bbw = df["bb_width_pct"].values; bb5 = df["bb5"].values
    sk = df["stoch_k"].values; sd = df["stoch_d"].values
    e10 = df["ema10"].values; e30 = df["ema30"].values
    n = len(df)

    out = []
    i = 20
    while i < n - 2:
        if (not np.isnan(bbw[i]) and not np.isnan(sk[i]) and not np.isnan(sd[i])
                and not np.isnan(bb5[i]) and bbw[i] < BB_THRESHOLD
                and bbw[i] <= bb5[i] * 1.6 and sk[i] < 50 and sk[i] > sd[i]):
            er = o[i + 1]
            if not (er > 0): i += 1; continue
            ef = er * (1 + SLIP)
            stp = ef * (1 - STOP_PCT)
            tgt = ef * (1 + target_pct)
            result, xraw, xidx = "OPEN", None, None
            for j in range(i + 1, n):  # scan to end of available data — no time cap
                if l[j] <= stp:
                    xraw = min(stp, o[j]); result = "STOP"; xidx = j; break
                if h[j] >= tgt:
                    xraw = max(tgt, o[j]); result = "TARGET"; xidx = j; break
            entry_date = dt[i]
            trend_conflict = bool(er < e10[i] and er < e30[i])
            if result == "OPEN":
                # Never resolved within available data. Report unrealized
                # mark-to-last-close for visibility, but NOT counted in
                # expectancy (no realized P&L).
                last_close = c[n - 1]
                unreal_gross = (last_close - er) / er * 100
                out.append({"symbol": symbol, "entry_date": entry_date, "result": "OPEN",
                            "hold_so_far": (n - 1) - (i + 1), "unreal_gross": round(float(unreal_gross), 3),
                            "trend_conflict": trend_conflict})
                i = n  # can't take another position in this name — still holding
                continue
            xf = xraw * (1 - SLIP if result == "STOP" else 1.0)  # limit target: no exit slip
            net = ((xf - ef) / ef - COST) * 100
            gross = (xraw - er) / er * 100
            hold = xidx - (i + 1)
            # Buy & hold benchmark over the SAME realized duration.
            bh_exit = c[xidx] * (1 - SLIP)
            bh_net = ((bh_exit - ef) / ef - COST) * 100
            out.append({"symbol": symbol, "entry_date": entry_date, "result": result,
                        "net": round(float(net), 3), "gross": round(float(gross), 3),
                        "bh_net": round(float(bh_net), 3), "hold": int(hold),
                        "trend_conflict": trend_conflict})
            i = xidx + 1
        else:
            i += 1
    return out


def run_all(target_pct):
    files = sorted(glob.glob(os.path.join(OHLC_DIR, "*.json")))
    trades = []
    for f in files:
        try: raw = json.load(open(f))
        except Exception: continue
        sym = os.path.basename(f).replace(".json", "")
        trades += simulate(sym, raw, target_pct)
    return trades


def summarize(trades, label):
    resolved = [t for t in trades if t["result"] in ("TARGET", "STOP")]
    opened = [t for t in trades if t["result"] == "OPEN"]
    n = len(resolved)
    print(f"\n{'='*70}\n{label}\n{'='*70}")
    print(f"Total signals: {len(trades)}   Resolved: {n}   Still OPEN (unresolved): {len(opened)}")
    if n == 0:
        print("  no resolved trades."); return None
    tg = [t for t in resolved if t["result"] == "TARGET"]
    st = [t for t in resolved if t["result"] == "STOP"]
    net = sum(t["net"] for t in resolved) / n
    gross = sum(t["gross"] for t in resolved) / n
    bh = sum(t["bh_net"] for t in resolved) / n
    avg_hold = sum(t["hold"] for t in resolved) / n
    avg_hold_tg = sum(t["hold"] for t in tg) / len(tg) if tg else 0
    avg_hold_st = sum(t["hold"] for t in st) / len(st) if st else 0
    # Annualized-equivalent (net% per trade / avg holding days * 365) — lets
    # us compare capital efficiency against a fixed-horizon strategy fairly.
    ann = (net / avg_hold * 365) if avg_hold > 0 else 0
    bh_ann = (bh / avg_hold * 365) if avg_hold > 0 else 0
    print(f"  target hit: {len(tg)/n*100:.1f}%   stop hit: {len(st)/n*100:.1f}%")
    print(f"  avg hold (all resolved): {avg_hold:.1f} trading days"
          f"   (target-hit: {avg_hold_tg:.1f}d, stop-hit: {avg_hold_st:.1f}d)")
    print(f"  GROSS: {gross:+.3f}%/trade   NET: {net:+.3f}%/trade   B&H (same duration): {bh:+.3f}%/trade"
          f"   edge: {net-bh:+.3f}%/trade")
    print(f"  ANNUALIZED equivalent —  strategy: {ann:+.2f}%/yr    buy&hold: {bh_ann:+.2f}%/yr")

    ta = [t for t in resolved if not t["trend_conflict"]]
    tc = [t for t in resolved if t["trend_conflict"]]
    for name, grp in [("trend-aligned", ta), ("trend-conflict", tc)]:
        if grp:
            gn = sum(t["net"] for t in grp)/len(grp)
            gh = sum(t["hold"] for t in grp)/len(grp)
            print(f"    {name:15}: n={len(grp):>5}  net {gn:+.3f}%/trade  avg hold {gh:.1f}d")

    by_year = {}
    for t in resolved:
        by_year.setdefault(t["entry_date"][:4], []).append(t)
    print(f"  {'year':<6}{'n':>6}{'target%':>9}{'stop%':>8}{'net%':>9}{'B&H%':>9}{'avg hold':>10}")
    for yr in sorted(by_year):
        yt = by_year[yr]
        ntg = sum(1 for t in yt if t["result"]=="TARGET")
        yn = sum(t["net"] for t in yt)/len(yt)
        yb = sum(t["bh_net"] for t in yt)/len(yt)
        yh = sum(t["hold"] for t in yt)/len(yt)
        print(f"  {yr:<6}{len(yt):>6}{ntg/len(yt)*100:>8.1f}%{(len(yt)-ntg)/len(yt)*100:>7.1f}%{yn:>+8.3f}%{yb:>+8.3f}%{yh:>9.1f}d")

    if opened:
        avg_open_hold = sum(t["hold_so_far"] for t in opened)/len(opened)
        avg_unreal = sum(t["unreal_gross"] for t in opened)/len(opened)
        print(f"  Still-open positions: avg held so far {avg_open_hold:.0f}d, "
              f"avg unrealized (gross, mark-to-last-close) {avg_unreal:+.2f}%")
    return dict(n=n, net=net, bh=bh, avg_hold=avg_hold, ann=ann, bh_ann=bh_ann, opened=len(opened))


for tp in (0.03, 0.04):
    trades = run_all(tp)
    summarize(trades, f"UNLIMITED HOLD — target {tp*100:.0f}% / stop 5% — full universe, 2020 to date")
