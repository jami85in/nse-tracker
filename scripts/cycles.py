"""
cycles.py — find stocks with a repeating yearly price cycle (e.g. "bottoms in
March, rallies into September, slides into the next March") and write the
data behind the dashboard's Cycles tab.

OUTPUT
  data/cycles/summary.json    one row per qualifying stock (market cap filter)
  data/cycles/<SYMBOL>.json   per-stock detail: monthly returns by year,
                              month-of-year stats, cycle history, window table

HOW A "CYCLE" IS FOUND
  1. Daily bars are cleaned: phantom holiday bars (identical OHLC to the
     previous bar) are dropped, and splits/bonuses are back-adjusted by
     detecting overnight gaps that match a standard ratio (1:1 bonus = 0.5,
     1:2 split = 0.5, 1:5 = 0.2 ...). Unexplained big gaps are reported as
     warnings rather than silently "fixed".
  2. Month-end closes give a year x month table of returns.
  3. Every seasonal window "buy at the end of month E, sell at the end of
     month X" (hold 3-9 months, may cross the year end) is tested across all
     years. A window is scored on how often it worked (hit rate), its median
     return, and its EDGE: median return minus the stock's median return over
     ANY window of the same length. Edge is what separates a real seasonal
     cycle from a stock that simply went up every year.
  4. The off-season (end of X -> next end of E) is checked too: a genuine
     cycle has a down leg, not just an up leg.
  5. For each year the actual trough/peak around the windows are recorded,
     so the dashboard can show "2024: low 19 Mar at 972 -> high 10 Oct at
     1,826 (+88%), then fell 37% to the next low".

CAVEATS (also surfaced in the UI)
  The OHLC history starts Sep 2020, so each stock has only ~5-6 cycles. With
  that few, some windows will look perfect by chance — the grade thresholds
  below require both consistency AND edge AND a down leg, but treat the
  output as a shortlist to research, not a signal to trade blindly.

MARKET CAP
  Read from data/market_caps.json (fetch_market_caps.py), then
  data/symbols_meta.json market_cap_cr, then data/market_cap_cr.csv.
  If none of those has any data, falls back to a liquidity proxy (top N by
  average daily traded value) and marks summary.mcap_source = "proxy".
"""
import os, sys, json, csv, glob, math, datetime, statistics as st

RAW_DIR = os.environ.get("CYCLES_RAW_DIR", "data/backtest/raw_ohlc")
# Older history (pre-Sep-2020) collected separately by ohlc_extend_history.yml.
# Merged in by date when present; harmless when the folder doesn't exist.
HIST_DIR = os.environ.get("CYCLES_HIST_DIR", "data/backtest/raw_ohlc_hist")
OUT_DIR = os.environ.get("CYCLES_OUT_DIR", "data/cycles")
MIN_MCAP_CR = float(os.environ.get("CYCLES_MIN_MCAP_CR", "10000"))
PROXY_TOP_N = int(os.environ.get("CYCLES_PROXY_TOP_N", "450"))
MIN_YEARS = int(os.environ.get("CYCLES_MIN_YEARS", "4"))
HOLD_MIN, HOLD_MAX = 3, 9

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Overnight price ratios produced by common corporate actions.
#   bonus a:b  -> b/(a+b)   (1:1 0.5, 2:1 0.333, 1:2 0.667, 3:1 0.25, 4:1 0.2)
#   split 1:k  -> 1/k       (2, 2.5, 4, 5, 10)
CA_FACTORS = [1/2, 1/3, 2/3, 1/4, 1/5, 2/5, 1/10, 1/6, 1/8, 3/4, 4/5]
CA_TOL = 0.035          # |observed/expected - 1| must be within 3.5% ...
CA_TOL_DEEP = 0.08      # ... or 8% for ratios <= 0.5: NSE circuit limits make a
                        # genuine one-day fall of 50%+ effectively impossible, so a
                        # gap that deep near 1/2, 1/3, 1/4 ... is a bonus/split even
                        # if the stock also moved a few % that day (AARTIDRUGS 3:1, 2020)
CA_MAX_RATIO = 0.82     # only gaps deeper than -18% are considered
MAX_ADJ_GAP_DAYS = 7    # bars further apart than this are a DATA HOLE (e.g. weeks in
                        # the trade-to-trade segment), not consecutive sessions — a price
                        # change across a hole must never be mistaken for a split


# ── data loading / cleaning ─────────────────────────────────────────────────
def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def clean_bars(raw):
    """Drop malformed and phantom (repeated) bars; return list of dicts."""
    out = []
    for b in raw or []:
        try:
            o, h, l, c = (float(b[k]) for k in ("open", "high", "low", "close"))
        except (KeyError, TypeError, ValueError):
            continue
        if min(o, h, l, c) <= 0:
            continue
        bar = {"date": b["date"], "o": o, "h": h, "l": l, "c": c}
        if out and all(bar[k] == out[-1][k] for k in ("o", "h", "l", "c")):
            continue  # phantom holiday bar = copy of the previous session
        if out and bar["date"] <= out[-1]["date"]:
            continue
        out.append(bar)
    return out


def match_factor(ratio):
    best = None
    for f in CA_FACTORS:
        tol = CA_TOL_DEEP if f <= 0.5 else CA_TOL
        err = abs(ratio / f - 1)
        if err <= tol and (best is None or err < best[1]):
            best = (f, err)
    return best[0] if best else None


def day_gap(d1, d2):
    return (datetime.date.fromisoformat(d2) - datetime.date.fromisoformat(d1)).days


def adjust_corporate_actions(bars):
    """Back-adjust prices before split/bonus gaps. Returns (bars, events, warnings)."""
    n = len(bars)
    factor_at = [1.0] * n
    events, warnings = [], []
    for i in range(1, n):
        prev_c = bars[i - 1]["c"]
        r_open = bars[i]["o"] / prev_c
        r_close = bars[i]["c"] / prev_c
        gap = day_gap(bars[i - 1]["date"], bars[i]["date"])
        if gap > MAX_ADJ_GAP_DAYS:
            if gap > 20:
                warnings.append(f"No price data from {bars[i - 1]['date']} to {bars[i]['date']} "
                                f"({gap} days, likely trade-to-trade/suspended) — windows spanning it are less reliable")
            continue
        if r_open > CA_MAX_RATIO and r_close > CA_MAX_RATIO:
            continue
        f = match_factor(r_open) or match_factor(r_close)
        if f:
            factor_at[i] = f
            events.append({"date": bars[i]["date"], "factor": round(f, 4)})
        elif r_close < 0.7:
            warnings.append(f"Unexplained {round((r_close - 1) * 100)}% gap on "
                            f"{bars[i]['date']} (demerger or crash?) — cycles "
                            f"spanning this date may be distorted")
    # cumulative factor applied to all bars BEFORE each event
    mult = 1.0
    adj = [None] * n
    for i in range(n - 1, -1, -1):
        b = bars[i]
        adj[i] = {"date": b["date"], "o": b["o"] * mult, "h": b["h"] * mult,
                  "l": b["l"] * mult, "c": b["c"] * mult}
        mult *= factor_at[i]
    return adj, events, warnings


# ── monthly structure ───────────────────────────────────────────────────────
def month_key(d):
    return d[:7]


def next_month(ym, k=1):
    y, m = int(ym[:4]), int(ym[5:7])
    t = y * 12 + (m - 1) + k
    return f"{t // 12:04d}-{t % 12 + 1:02d}"


def build_months(bars):
    """Return ordered dict ym -> {close, high, low, high_date, low_date, last_date}."""
    months = {}
    for b in bars:
        ym = month_key(b["date"])
        m = months.get(ym)
        if m is None:
            m = months[ym] = {"close": b["c"], "high": b["h"], "low": b["l"],
                              "high_date": b["date"], "low_date": b["date"],
                              "first_date": b["date"], "last_date": b["date"]}
        m["close"] = b["c"]
        m["last_date"] = b["date"]
        if b["h"] > m["high"]:
            m["high"], m["high_date"] = b["h"], b["date"]
        if b["l"] < m["low"]:
            m["low"], m["low_date"] = b["l"], b["date"]
    return months


def month_complete(ym, last_date):
    """A month counts as complete once the data has moved past it."""
    return month_key(last_date) > ym


def window_return(months, end_entry_ym, length):
    """Close at end of entry month -> close at end of entry+length."""
    a = months.get(end_entry_ym)
    b = months.get(next_month(end_entry_ym, length))
    if not a or not b:
        return None
    return b["close"] / a["close"] - 1


# ── window search ───────────────────────────────────────────────────────────
def pct(x):
    return None if x is None else round(x * 100, 1)


def bench_window(bench, end_entry_ym, length):
    """Compounded equal-weight universe return over the same window."""
    if not bench:
        return None
    acc = 1.0
    for k in range(1, length + 1):
        r = bench.get(next_month(end_entry_ym, k))
        if r is None:
            return None
        acc *= 1 + r
    return acc - 1


def analyse_windows(months, complete, bench=None):
    """Test every (entry month, hold length). Return best window + table."""
    yms = [ym for ym in months if ym in complete]
    if not yms:
        return None, []
    # baseline: median return of ANY window of each length (drift)
    base = {}
    for L in range(HOLD_MIN, HOLD_MAX + 1):
        rs = [window_return(months, ym, L) for ym in yms
              if next_month(ym, L) in complete]
        rs = [r for r in rs if r is not None]
        base[L] = st.median(rs) if len(rs) >= 12 else None

    table = []
    for E in range(1, 13):
        for L in range(HOLD_MIN, HOLD_MAX + 1):
            per_year = []
            for ym in yms:
                if int(ym[5:7]) != E:
                    continue
                if next_month(ym, L) not in complete:
                    continue
                r = window_return(months, ym, L)
                if r is not None:
                    per_year.append((int(ym[:4]), r, bench_window(bench, ym, L)))
            n = len(per_year)
            if n < MIN_YEARS or base.get(L) is None:
                continue
            rets = [r for _, r, _ in per_year]
            exc = [r - b for _, r, b in per_year if b is not None]
            hit = sum(r > 0 for r in rets) / n
            beat = sum(x > 0 for x in exc) / len(exc) if exc else None
            exc_med = st.median(exc) if exc else None
            med = st.median(rets)
            edge = med - base[L]
            # off-season: end of exit month -> end of next entry month
            off = []
            for y, _, _ in per_year:
                ex = next_month(f"{y:04d}-{E:02d}", L)
                if next_month(ex, 12 - L) in complete:
                    r = window_return(months, ex, 12 - L)
                    if r is not None:
                        off.append(r)
            off_med = st.median(off) if len(off) >= 3 else None
            off_down = (sum(r < 0 for r in off) / len(off)) if len(off) >= 3 else None
            # rank on what the stock did BEYOND the market and its own drift
            core = min(edge, exc_med) if exc_med is not None else edge
            consistency = min(hit, beat) if beat is not None else hit
            score = core * max(0.0, (consistency - 0.5) * 2)
            if off_med is not None and off_med < 0:
                score *= 1.25          # reward a genuine down leg
            table.append({
                "entry": E, "exit": (E + L - 1) % 12 + 1, "hold": L, "n": n,
                "hit": round(hit, 3), "median": pct(med), "mean": pct(st.mean(rets)),
                "worst": pct(min(rets)), "best": pct(max(rets)),
                "edge": pct(edge), "baseline": pct(base[L]),
                "off_median": pct(off_med), "off_down_rate": None if off_down is None else round(off_down, 3),
                "beat_rate": None if beat is None else round(beat, 3),
                "excess_median": pct(exc_med),
                "score": round(score * 100, 2),
                "by_year": [{"year": y, "ret": pct(r), "market": pct(b)} for y, r, b in per_year],
            })
    if not table:
        return None, []
    table.sort(key=lambda w: w["score"], reverse=True)
    return table[0], table


def circ(a, b):
    return min((a - b) % 12, (b - a) % 12)


def select_best(table, timing):
    """The top-scoring window can sit a month or two off the turning points
    you'd see on a chart (because part of the move is market-wide). If a
    window scores within 25% of the best, earns the same grade, AND lines up
    better with the usual low/high months, prefer it — it's the one you can
    actually act on."""
    best = table[0]
    top_grade = grade(best)
    if timing and timing["low"]["within_1m"] >= 3 and timing["high"]["within_1m"] >= 3:
        lo, hi = timing["low"]["month"], timing["high"]["month"]
        close_enough = [w for w in table if w["score"] >= 0.75 * best["score"] and w["score"] > 0
                        and grade(w) == top_grade]
        close_enough.sort(key=lambda w: (circ(w["entry"], lo) + circ(w["exit"], hi), -w["score"]))
        if close_enough:
            best = close_enough[0]
    best["walk_forward"] = walk_forward(table)
    alts = [w for w in table if w is not best][:5]
    return best, alts


def walk_forward(table):
    """Honesty check: for every window, hold back its most recent completed
    year, pick the best window using only the earlier years, then report how
    that window did in the held-back year. With only ~6 cycles of history
    this is the closest thing to an out-of-sample test."""
    best, best_score = None, None
    for w in table:
        yrs = sorted(w["by_year"], key=lambda r: r["year"])
        if len(yrs) < MIN_YEARS:
            continue
        prior, test = yrs[:-1], yrs[-1]
        rets = [r["ret"] for r in prior]
        exc = [r["ret"] - r["market"] for r in prior if r.get("market") is not None]
        hit = sum(x > 0 for x in rets) / len(rets)
        beat = sum(x > 0 for x in exc) / len(exc) if exc else hit
        core = st.median(exc) if exc else st.median(rets)
        sc = core * max(0.0, (min(hit, beat) - 0.5) * 2)
        if best_score is None or sc > best_score:
            best, best_score = (w, test, prior[-1]["year"]), sc
    if not best:
        return None
    w, test, last_fit = best
    return {"fit_years": f"to {last_fit}", "test_year": test["year"],
            "entry": w["entry"], "exit": w["exit"], "hold": w["hold"],
            "ret": test["ret"], "market": test.get("market"),
            "worked": test["ret"] > 0 and (test.get("market") is None or test["ret"] > test["market"])}


def grade(w):
    if not w:
        return "NONE"
    n, hit, med, edge = w["n"], w["hit"], w["median"], w["edge"]
    off = w["off_median"]
    beat = w.get("beat_rate") if w.get("beat_rate") is not None else hit
    exc = w.get("excess_median") if w.get("excess_median") is not None else edge
    if (n >= 5 and hit >= 0.83 and beat >= 0.8 and med >= 12 and edge >= 8
            and exc >= 8 and off is not None and off <= 0):
        return "STRONG"
    if hit >= 0.75 and beat >= 0.67 and med >= 8 and edge >= 4 and exc >= 4:
        return "MODERATE"
    if hit >= 0.6 and edge > 0 and exc > 0:
        return "WEAK"
    return "NONE"


# ── per-year cycle history (actual troughs / peaks) ─────────────────────────
def band(center_month, width_before, width_after):
    return [((center_month - 1 + k) % 12) + 1 for k in range(-width_before, width_after + 1)]


def extreme_in(months, year, month_list, kind, after_date=None):
    """Lowest low / highest high in the given calendar months, starting in
    `year` and moving forward through the list (handles year wrap)."""
    best = None
    y = year
    prev = None
    for m in month_list:
        if prev is not None and m < prev:
            y += 1
        prev = m
        rec = months.get(f"{y:04d}-{m:02d}")
        if not rec:
            continue
        if kind == "low":
            if after_date and rec["low_date"] < after_date:
                continue
            if best is None or rec["low"] < best[0]:
                best = (rec["low"], rec["low_date"])
        else:
            if after_date and rec["high_date"] < after_date:
                continue
            if best is None or rec["high"] > best[0]:
                best = (rec["high"], rec["high_date"])
    return best


def cycle_history(months, w, last_date, low_month=None, high_month=None):
    """For each year: the trough around the usual low month, the peak around
    the usual high month, the rally between them, then the decline into the
    next year's trough. Uses the calendar timing (what you see on a chart)
    when it is available, otherwise the best window's entry/exit months."""
    if not w:
        return []
    E, L = w["entry"], w["hold"]
    lo_c = low_month or E
    hi_c = high_month or w["exit"]
    span_len = (hi_c - lo_c) % 12 or 12
    trough_band = band(lo_c, 1, 1)
    years = sorted({int(ym[:4]) for ym in months})
    rows = []
    for y in years:
        tr = extreme_in(months, y, trough_band, "low")
        if not tr:
            continue
        peak_months = [((trough_band[0] - 1 + k) % 12) + 1 for k in range(0, span_len + 3)]
        pk = extreme_in(months, y, peak_months, "high", after_date=tr[1])
        row = {"year": y, "trough_date": tr[1], "trough": round(tr[0], 2)}
        if pk and pk[1] > tr[1]:
            row.update({"peak_date": pk[1], "peak": round(pk[0], 2),
                        "rally": pct(pk[0] / tr[0] - 1)})
            ntr = extreme_in(months, y + 1, trough_band, "low", after_date=pk[1])
            if ntr:
                row.update({"next_trough_date": ntr[1], "next_trough": round(ntr[0], 2),
                            "decline": pct(ntr[0] / pk[0] - 1)})
        wr = window_return(months, f"{y:04d}-{E:02d}", L)
        exit_ym = next_month(f"{y:04d}-{E:02d}", L)
        if wr is not None and month_complete(exit_ym, last_date):
            row["window_return"] = pct(wr)
        rows.append(row)
    return rows


def peak_trough_timing(months, complete_years):
    """Calendar-year view the user sees on a chart: month of the yearly low
    and month of the highest high AFTER that low."""
    rows = []
    for y in complete_years:
        recs = [(m, months.get(f"{y:04d}-{m:02d}")) for m in range(1, 13)]
        recs = [(m, r) for m, r in recs if r]
        if len(recs) < 12:
            continue
        lo_m, lo = min(recs, key=lambda t: t[1]["low"])
        after = [(m, r) for m, r in recs if m >= lo_m]
        hi_m, hi = max(after, key=lambda t: t[1]["high"])
        rows.append({"year": y, "low_month": lo_m, "low_date": lo["low_date"],
                     "low": round(lo["low"], 2), "high_month": hi_m,
                     "high_date": hi["high_date"], "high": round(hi["high"], 2),
                     "rise": pct(hi["high"] / lo["low"] - 1)})
    if not rows:
        return rows, None

    def consistency(key):
        best = None
        for c in range(1, 13):
            near = sum(1 for r in rows if min((r[key] - c) % 12, (c - r[key]) % 12) <= 1)
            if best is None or near > best[1]:
                best = (c, near)
        return {"month": best[0], "within_1m": best[1], "years": len(rows)}

    return rows, {"low": consistency("low_month"), "high": consistency("high_month")}


# ── current status ──────────────────────────────────────────────────────────
def current_status(w, months, bars):
    if not w:
        return {"phase": "NONE"}
    last = bars[-1]
    cur_m = int(last["date"][5:7])
    E, X, L = w["entry"], w["exit"], w["hold"]
    to_entry = (E - cur_m) % 12
    since_entry = (cur_m - E) % 12  # months after the entry month
    if to_entry == 0:
        phase, note = "BUY_WINDOW", f"Entry month — typically buy by end-{MONTHS[E-1]}"
    elif to_entry == 1:
        phase, note = "BUY_SOON", f"Buy window opens next month ({MONTHS[E-1]})"
    elif 1 <= since_entry < L:
        phase, note = "IN_SEASON", f"Seasonal up-leg — usually sell by end-{MONTHS[X-1]}"
    elif since_entry == L:
        phase, note = "SELL_WINDOW", f"Exit month — seasonal up-leg usually ends in {MONTHS[X-1]}"
    else:
        phase, note = "OFF_SEASON", f"Seasonal down-leg — buy window in {to_entry} month(s) ({MONTHS[E-1]})"
    # this cycle's return since the entry month close
    entry_ym = None
    y, m = int(last["date"][:4]), cur_m
    for k in range(0, 12):
        ym = next_month(f"{y:04d}-{m:02d}", -k)
        if int(ym[5:7]) == E and ym != month_key(last["date"]):
            entry_ym = ym
            break
    cyc_ret = None
    if entry_ym and months.get(entry_ym) and phase in ("IN_SEASON", "SELL_WINDOW"):
        cyc_ret = pct(last["c"] / months[entry_ym]["close"] - 1)
    return {"phase": phase, "note": note, "months_to_entry": to_entry,
            "cycle_return": cyc_ret}


# ── market cap ──────────────────────────────────────────────────────────────
def load_market_caps(meta):
    caps, source = {}, None
    mc = load_json("data/market_caps.json", {}) or {}
    for s, v in (mc.get("caps") or {}).items():
        if isinstance(v, dict) and v.get("mcap_cr"):
            caps[s] = float(v["mcap_cr"])
    if caps:
        source = "market_caps.json"
    for s, v in (meta or {}).items():
        if s not in caps and isinstance(v, dict) and v.get("market_cap_cr"):
            caps[s] = float(v["market_cap_cr"])
            source = source or "symbols_meta.json"
    if os.path.exists("data/market_cap_cr.csv"):
        with open("data/market_cap_cr.csv") as f:
            for row in csv.reader(f):
                if len(row) >= 2 and row[0].strip().upper() != "SYMBOL":
                    try:
                        caps.setdefault(row[0].strip(), float(row[1]))
                        source = source or "market_cap_cr.csv"
                    except ValueError:
                        pass
    return caps, source


# ── main ────────────────────────────────────────────────────────────────────
def load_history(sym, path):
    """Current bars plus any older bars from HIST_DIR, merged by date."""
    raw = load_json(path, []) or []
    hp = os.path.join(HIST_DIR, f"{sym}.json")
    if os.path.exists(hp):
        by_date = {b.get("date"): b for b in (load_json(hp, []) or []) if b.get("date")}
        for b in raw:
            if b.get("date"):
                by_date[b["date"]] = b
        raw = [by_date[d] for d in sorted(by_date)]
    return raw


def prepare_symbol(raw):
    bars = clean_bars(raw)
    if len(bars) < 250 * MIN_YEARS:
        return None
    bars, events, warnings = adjust_corporate_actions(bars)
    months = build_months(bars)
    return {"bars": bars, "events": events, "warnings": warnings, "months": months}


def build_benchmark(prepared):
    """Equal-weight average monthly return of the whole universe (each
    stock's move capped at +/-50% so one outlier can't swing it)."""
    acc = {}
    for p in prepared.values():
        m = p["months"]
        keys = sorted(m)
        for a, b in zip(keys, keys[1:]):
            if next_month(a) != b:
                continue
            r = max(-0.5, min(0.5, m[b]["close"] / m[a]["close"] - 1))
            acc.setdefault(b, []).append(r)
    return {ym: sum(v) / len(v) for ym, v in acc.items() if len(v) >= 20}


def analyse_symbol(sym, prep, bench):
    bars, events, warnings, months = prep["bars"], prep["events"], prep["warnings"], prep["months"]
    last_date = bars[-1]["date"]
    complete = {ym for ym in months if month_complete(ym, last_date)}
    best, table = analyse_windows(months, complete, bench)
    if not best:
        return None
    years = sorted({int(ym[:4]) for ym in complete})
    full_years = [y for y in years if all(f"{y:04d}-{m:02d}" in complete for m in range(1, 13))]
    timing_rows, timing = peak_trough_timing(months, full_years)
    best, alternatives = select_best(table, timing)
    g = grade(best)

    # month-of-year stats
    month_rets = {}
    ordered = sorted(months)
    for a, b in zip(ordered, ordered[1:]):
        if b not in complete or next_month(a) != b:
            continue
        month_rets.setdefault(int(b[5:7]), []).append((int(b[:4]), months[b]["close"] / months[a]["close"] - 1))
    month_stats = []
    for m in range(1, 13):
        rs = [r for _, r in month_rets.get(m, [])]
        month_stats.append({
            "month": m, "n": len(rs),
            "avg": pct(st.mean(rs)) if rs else None,
            "median": pct(st.median(rs)) if rs else None,
            "up_rate": round(sum(r > 0 for r in rs) / len(rs), 3) if rs else None,
        })
    heat = {}
    for m, lst in month_rets.items():
        for y, r in lst:
            heat.setdefault(str(y), [None] * 12)[m - 1] = pct(r)
    # current (partial) month
    cur = month_key(last_date)
    prev = next_month(cur, -1)
    if cur in months and prev in months:
        heat.setdefault(cur[:4], [None] * 12)[int(cur[5:7]) - 1] = pct(months[cur]["close"] / months[prev]["close"] - 1)

    status = current_status(best, months, bars)
    cycles = cycle_history(months, best, last_date,
                           timing["low"]["month"] if timing and timing["low"]["within_1m"] >= 3 else None,
                           timing["high"]["month"] if timing and timing["high"]["within_1m"] >= 3 else None)
    last_close = bars[-1]["c"]
    hi52 = max(b["h"] for b in bars[-250:])
    lo52 = min(b["l"] for b in bars[-250:])

    # indicative zone if the usual decline repeats from the latest peak
    declines = [c["decline"] for c in cycles if c.get("decline") is not None]
    rallies = [c["rally"] for c in cycles if c.get("rally") is not None]
    last_peak = next((c for c in reversed(cycles) if c.get("peak")), None)
    zone = None
    if declines and last_peak and status["phase"] in ("OFF_SEASON", "BUY_SOON", "BUY_WINDOW"):
        md = st.median(declines) / 100
        zone = round(last_peak["peak"] * (1 + md), 2)

    detail = {
        "symbol": sym, "as_of": last_date, "last_close": round(last_close, 2),
        "grade": g, "best": best,
        "alternatives": alternatives,
        "status": status, "cycles": cycles,
        "median_rally": pct(st.median(rallies) / 100) if rallies else None,
        "median_decline": pct(st.median(declines) / 100) if declines else None,
        "indicative_buy_level": zone,
        "timing": timing, "timing_rows": timing_rows,
        "month_stats": month_stats, "heatmap": heat,
        "corporate_actions": events, "warnings": warnings,
        "hi_52w": round(hi52, 2), "lo_52w": round(lo52, 2),
        "first_date": bars[0]["date"],
    }
    summary = {
        "symbol": sym, "grade": g, "score": best["score"],
        "entry": best["entry"], "exit": best["exit"], "hold": best["hold"],
        "n": best["n"], "hit": best["hit"], "median": best["median"],
        "worst": best["worst"], "edge": best["edge"], "off_median": best["off_median"],
        "beat_rate": best.get("beat_rate"), "excess_median": best.get("excess_median"),
        "wf_worked": (best.get("walk_forward") or {}).get("worked"),
        "wf_ret": (best.get("walk_forward") or {}).get("ret"),
        "phase": status["phase"], "months_to_entry": status["months_to_entry"],
        "cycle_return": status.get("cycle_return"),
        "last_close": round(last_close, 2), "as_of": last_date,
        "pct_from_52w_high": pct(last_close / hi52 - 1),
        "median_rally": detail["median_rally"], "median_decline": detail["median_decline"],
        "indicative_buy_level": zone,
        "low_month": timing["low"]["month"] if timing else None,
        "low_consistency": f'{timing["low"]["within_1m"]}/{timing["low"]["years"]}' if timing else None,
        "high_month": timing["high"]["month"] if timing else None,
        "high_consistency": f'{timing["high"]["within_1m"]}/{timing["high"]["years"]}' if timing else None,
        "warn": bool(warnings),
    }
    return summary, detail


def main():
    meta = load_json("data/symbols_meta.json", {}) or {}
    caps, cap_source = load_market_caps(meta)
    files = sorted(glob.glob(os.path.join(RAW_DIR, "*.json")))
    print(f"{len(files)} OHLC files; market caps from {cap_source or 'NONE'} ({len(caps)} symbols)")

    # universe
    universe, mcap_used = [], {}
    if caps:
        for p in files:
            s = os.path.basename(p)[:-5]
            if meta.get(s, {}).get("is_etf"):
                continue
            if caps.get(s, 0) >= MIN_MCAP_CR:
                universe.append(p)
                mcap_used[s] = caps[s]
        mcap_source = cap_source
    else:
        # liquidity proxy: avg volume x last close, top N
        ranked = []
        for p in files:
            s = os.path.basename(p)[:-5]
            m = meta.get(s, {})
            if m.get("is_etf") or not m.get("avg_volume"):
                continue
            raw = load_json(p, [])
            if not raw:
                continue
            ranked.append((m["avg_volume"] * float(raw[-1]["close"]), p, s))
        ranked.sort(reverse=True)
        universe = [p for _, p, _ in ranked[:PROXY_TOP_N]]
        mcap_source = "proxy"
        print(f"  no market-cap data — using top {PROXY_TOP_N} by traded value as a proxy")

    os.makedirs(OUT_DIR, exist_ok=True)
    prepared = {}
    for p in universe:
        s = os.path.basename(p)[:-5]
        try:
            prep = prepare_symbol(load_history(s, p))
        except Exception as e:
            print(f"  {s}: prepare error {e}")
            continue
        if prep:
            prepared[s] = prep
    bench = build_benchmark(prepared)
    print(f"  {len(prepared)} stocks with >= {MIN_YEARS}y history; benchmark months: {len(bench)}")
    rows, written = [], set()
    for s, prep in prepared.items():
        try:
            res = analyse_symbol(s, prep, bench)
        except Exception as e:
            print(f"  {s}: error {e}")
            continue
        if not res:
            continue
        summary, detail = res
        summary["mcap_cr"] = round(mcap_used[s]) if s in mcap_used else None
        detail["mcap_cr"] = summary["mcap_cr"]
        with open(os.path.join(OUT_DIR, f"{s}.json"), "w") as f:
            json.dump(detail, f, separators=(",", ":"))
        written.add(f"{s}.json")
        rows.append(summary)

    # remove stale detail files for stocks that dropped out
    for p in glob.glob(os.path.join(OUT_DIR, "*.json")):
        b = os.path.basename(p)
        if b != "summary.json" and b not in written:
            os.remove(p)

    order = {"STRONG": 0, "MODERATE": 1, "WEAK": 2, "NONE": 3}
    rows.sort(key=lambda r: (order[r["grade"]], -r["score"]))
    out = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of": max((r["as_of"] for r in rows), default=None),
        "min_mcap_cr": MIN_MCAP_CR, "mcap_source": mcap_source,
        "history_from": min((p["bars"][0]["date"] for p in prepared.values()), default=None),
        "benchmark": "equal-weight average of all stocks in this universe",
        "counts": {g: sum(r["grade"] == g for r in rows) for g in order},
        # How often the window picked from earlier years also worked in the
        # latest year, by grade — the honest measure of how much to trust a grade.
        "validation": {g: {"held": sum(1 for r in rows if r["grade"] == g and r["wf_worked"] is True),
                           "tested": sum(1 for r in rows if r["grade"] == g and r["wf_worked"] is not None)}
                       for g in order},
        "stocks": rows,
    }
    with open(os.path.join(OUT_DIR, "summary.json"), "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"wrote {len(rows)} stocks -> {OUT_DIR}/summary.json  {out['counts']}")


if __name__ == "__main__":
    main()
