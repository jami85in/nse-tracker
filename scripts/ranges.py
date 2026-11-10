#!/usr/bin/env python3
"""
ranges.py — how far each stock typically moves in a day and in a week, and
TESTED short-term (1-3 day) and weekly plans for trading around a holding to
bring its cost down.

OUTPUT
  data/ranges/summary.json   your holdings + watch.txt: daily/weekly range stats,
                             the next orders' levels, and the best-tested plans
  data/ranges/more.json      the same for the scan / Cycles stocks (loaded by the
                             dashboard only when you ask for them)

THE TWO WEEKLY PLANS (both tested on every week of the stock's history)
  Buy the dip  — needs spare cash. Before the week starts, place a buy order
                 b% below last week's close. If it fills, place a sell order t%
                 above the fill price. Not sold by Friday -> sold at Friday's
                 close (so a trade never runs into the next week).
  Sell the rise — uses shares you already hold, no new money. Place a sell
                 order s% above last week's close. If it fills, place a buy-back
                 order t% below the sale price. Not bought back by Friday ->
                 bought back at Friday's close (you end the week with the same
                 number of shares, sometimes at a loss).
  Every trade pays ROUND_TRIP_COST (STT, exchange charges, stamp duty, GST, DP).
  Tax on the gain (short-term, 20%) is not deducted.

  Fills use daily bars, so the order of the high and the low WITHIN a day is
  unknown. To stay honest the exit is only checked from the NEXT day on (a
  same-day round trip would need intraday data), and a day that opens beyond
  the order price fills at the open.

  SHORT-TERM PLANS (refreshed every evening, trades last 1-3 days)
  The same two plans, but the order goes in for the next session relative to
  the latest close, and an unfilled target is closed out after HOLD days
  (1, 2 or 3 sessions after the fill) instead of on Friday. One trade at a
  time: a new order is only placed after the previous trade has closed.

  For each plan a grid of (entry %, target %) pairs is tested; the pair with
  the best expected return per week counts only if it also worked over the
  last 52 weeks (score = the lower of the two), so a level that only paid off
  years ago isn't recommended.

DATA
  data/backtest/raw_ohlc/<SYMBOL>.json (from Sep 2020), cleaned and adjusted
  for splits/bonuses with the same code as the Cycles tab (cycles.py).

UNIVERSE
  Your holdings (data/zerodha_holdings.json), data/ranges/watch.txt (one
  symbol per line, optional), the latest scan's squeeze / countertrend /
  watchlist stocks, and Cycles stocks graded STRONG or MODERATE.
"""
import datetime as dt
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cycles import clean_bars, adjust_corporate_actions, load_history, load_json  # noqa: E402

RAW_DIR = os.environ.get("RANGES_RAW_DIR", "data/backtest/raw_ohlc")
OUT = os.environ.get("RANGES_OUT", "data/ranges/summary.json")
OUT_MORE = os.environ.get("RANGES_OUT_MORE", "data/ranges/more.json")
WATCH = os.environ.get("RANGES_WATCH", "data/ranges/watch.txt")
HOLDINGS = os.environ.get("RANGES_HOLDINGS", "data/zerodha_holdings.json")
SCAN = os.environ.get("RANGES_SCAN", "data/scan_latest.json")
CYCLES = os.environ.get("RANGES_CYCLES", "data/cycles/summary.json")

ROUND_TRIP_COST = float(os.environ.get("RANGES_COST", "0.003"))   # 0.3% per buy+sell
ENTRY_GRID = [0.01, 0.015, 0.02, 0.025, 0.03, 0.04, 0.05, 0.06, 0.08]
TARGET_GRID = [0.01, 0.015, 0.02, 0.03, 0.04, 0.05, 0.06]
MIN_WEEKS = 100             # about two years of weekly history
RECENT = 52                 # "still works lately" check
MIN_FILLS, MIN_FILLS_RECENT = 15, 5
# A level is only worth showing if each trade earns enough to matter after
# charges: at least 0.25% on average over the whole history (and not a loss
# lately). Without this, levels with a tiny average and an occasional -40%
# trade (a stock that hit upper circuits after you sold) can look "positive".
MIN_AVG = float(os.environ.get("RANGES_MIN_AVG", "0.0025"))
PCTS = (10, 25, 50, 75, 90)


# ── helpers ────────────────────────────────────────────────────────────────
def pct(values, p):
    """Linear-interpolated percentile of a non-empty list (p in 0-100)."""
    v = sorted(values)
    if not v:
        return None
    k = (len(v) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def r4(x):
    return None if x is None else round(x, 4)


def pct_block(values):
    return {f"p{p}": r4(pct(values, p)) for p in PCTS} if values else None


def build_weeks(bars):
    """[{start, ref (the previous week's last close), days}] per ISO week."""
    groups = []
    for b in bars:
        y, w, _ = dt.date.fromisoformat(b["date"]).isocalendar()
        if not groups or groups[-1][0] != (y, w):
            groups.append(((y, w), []))
        groups[-1][1].append(b)
    out = []
    for (key_a, days_a), (key_b, days_b) in zip(groups, groups[1:]):
        gap = (dt.date.fromisoformat(days_b[0]["date"]) - dt.date.fromisoformat(days_a[-1]["date"])).days
        if gap > 10:            # a data hole, not consecutive weeks
            continue
        out.append({"start": days_b[0]["date"], "ref": days_a[-1]["c"], "days": days_b})
    # the latest week is still running unless it has reached Friday — leave it
    # out so a half week isn't scored as if Friday's close had come
    if out and dt.date.fromisoformat(out[-1]["days"][-1]["date"]).weekday() < 4:
        out.pop()
    return out


# ── the two weekly plans ───────────────────────────────────────────────────
def sim_buy_dip(weeks, entry, target):
    """Per week: None (no fill) or (return, ended_by_friday_close)."""
    res = []
    for w in weeks:
        buy_at = w["ref"] * (1 - entry)
        days = w["days"]
        fill_i = fill = None
        for i, d in enumerate(days):
            if d["l"] <= buy_at:
                fill_i, fill = i, min(d["o"], buy_at)
                break
        if fill_i is None:
            res.append(None)
            continue
        sell_at = fill * (1 + target)
        exit_px, timed_out = None, False
        for d in days[fill_i + 1:]:
            if d["h"] >= sell_at:
                exit_px = max(d["o"], sell_at)
                break
        if exit_px is None:
            exit_px, timed_out = days[-1]["c"], True
        res.append((exit_px / fill - 1 - ROUND_TRIP_COST, timed_out))
    return res


def sim_sell_rise(weeks, entry, target):
    """Sell held shares on a rise, buy them back lower. Return = cash made as a
    fraction of the sale value (positive = cost of the holding goes down)."""
    res = []
    for w in weeks:
        sell_at = w["ref"] * (1 + entry)
        days = w["days"]
        fill_i = fill = None
        for i, d in enumerate(days):
            if d["h"] >= sell_at:
                fill_i, fill = i, max(d["o"], sell_at)
                break
        if fill_i is None:
            res.append(None)
            continue
        buy_at = fill * (1 - target)
        back, timed_out = None, False
        for d in days[fill_i + 1:]:
            if d["l"] <= buy_at:
                back = min(d["o"], buy_at)
                break
        if back is None:
            back, timed_out = days[-1]["c"], True
        res.append(((fill - back) / fill - ROUND_TRIP_COST, timed_out))
    return res


def score(res, n_weeks):
    fills = [r for r in res if r is not None]
    if not fills:
        return {"fills": 0}
    rets = [r[0] for r in fills]
    return {
        "fills": len(fills),
        "fill_rate": r4(len(fills) / n_weeks),
        "win_rate": r4(sum(1 for x in rets if x > 0) / len(rets)),
        "avg": r4(sum(rets) / len(rets)),                     # per filled trade
        "per_week": r4(sum(rets) / n_weeks),                   # expected, counting weeks with no fill
        "target_hit": r4(sum(1 for r in fills if not r[1]) / len(fills)),
        "worst": r4(min(rets)),
    }


TOP_N = 3                       # plans kept per stock and plan type


def _brief(sc):
    return {k: sc.get(k) for k in ("fills", "win_rate", "avg", "per_week", "worst")}


def best_plan(weeks, sim):
    recent = weeks[-RECENT:]
    rows = []
    for e in ENTRY_GRID:
        for t in TARGET_GRID:
            full = score(sim(weeks, e, t), len(weeks))
            rec = score(sim(recent, e, t), len(recent))
            if full["fills"] < MIN_FILLS or rec["fills"] < MIN_FILLS_RECENT:
                continue
            if full["avg"] < MIN_AVG or rec["avg"] < 0:
                continue
            s = min(full["per_week"], rec["per_week"])
            rows.append({"entry": e, "target": t, "score": r4(s), "all": full, "last52": _brief(rec)})
    rows.sort(key=lambda r: (r["score"], r["all"]["win_rate"]), reverse=True)
    return rows[:TOP_N]


# ── short-term plans: next session's order, out within 1-3 sessions ───────
D_ENTRY_GRID = [0.005, 0.01, 0.015, 0.02, 0.03, 0.04]
D_TARGET_GRID = [0.005, 0.01, 0.015, 0.02, 0.03]
D_HOLD_GRID = [1, 2, 3]
D_RECENT = 250                  # about a year of sessions
D_MIN_FILLS, D_MIN_FILLS_RECENT = 30, 10


MAX_SESSION_GAP = 5             # calendar days; a longer gap is a data hole (suspension,
                                # trade-to-trade weeks), never a tradable overnight move


def _gap(a, b):
    return (dt.date.fromisoformat(b["date"]) - dt.date.fromisoformat(a["date"])).days


def sim_short(bars, entry, target, hold, side):
    """side 'buy': buy entry% below the previous close, sell target% above the
    fill; 'sell': sell entry% above, buy back target% below. Unfilled targets
    close at the close of the hold-th session after the fill. Returns
    (list of (return, timed_out), sessions covered)."""
    res, i, n = [], 1, len(bars)
    while i < n:
        if _gap(bars[i - 1], bars[i]) > MAX_SESSION_GAP:
            i += 1
            continue
        ref = bars[i - 1]["c"]
        d = bars[i]
        if side == "buy":
            px = ref * (1 - entry)
            hit = d["l"] <= px
            fill = min(d["o"], px) if hit else None
        else:
            px = ref * (1 + entry)
            hit = d["h"] >= px
            fill = max(d["o"], px) if hit else None
        if not hit:
            i += 1
            continue
        exit_px, timed_out, j, hole = None, False, i, False
        goal = fill * (1 + target) if side == "buy" else fill * (1 - target)
        for j in range(i + 1, min(i + hold, n - 1) + 1):
            b = bars[j]
            if _gap(bars[j - 1], b) > MAX_SESSION_GAP:
                hole = True
                break
            if side == "buy" and b["h"] >= goal:
                exit_px = max(b["o"], goal)
                break
            if side == "sell" and b["l"] <= goal:
                exit_px = min(b["o"], goal)
                break
        else:
            if i + hold > n - 1:    # the hold runs past the latest bar: outcome unknown yet
                break
            j = i + hold
            exit_px, timed_out = bars[j]["c"], True
        if hole:                    # the trade would straddle a data hole: leave it out
            i = j
            continue
        r = (exit_px / fill - 1) if side == "buy" else (fill - exit_px) / fill
        res.append((r - ROUND_TRIP_COST, timed_out))
        i = j + 1
    return res, max(1, n - 1)


def score_short(res, sessions):
    if not res:
        return {"fills": 0}
    rets = [r[0] for r in res]
    return {
        "fills": len(res),
        "fills_per_week": r4(len(res) / sessions * 5),
        "win_rate": r4(sum(1 for x in rets if x > 0) / len(rets)),
        "avg": r4(sum(rets) / len(rets)),
        "per_week": r4(sum(rets) / sessions * 5),          # expected, per 5 sessions
        "target_hit": r4(sum(1 for r in res if not r[1]) / len(res)),
        "worst": r4(min(rets)),
    }


def best_short(bars, side):
    recent = bars[-(D_RECENT + 1):]
    rows = []
    for e in D_ENTRY_GRID:
        for t in D_TARGET_GRID:
            for h in D_HOLD_GRID:
                full = score_short(*sim_short(bars, e, t, h, side))
                rec = score_short(*sim_short(recent, e, t, h, side))
                if full["fills"] < D_MIN_FILLS or rec["fills"] < D_MIN_FILLS_RECENT:
                    continue
                if full["avg"] < MIN_AVG or rec["avg"] < 0:
                    continue
                s = min(full["per_week"], rec["per_week"])
                rows.append({"entry": e, "target": t, "hold": h, "score": r4(s), "all": full, "last250": _brief(rec)})
    rows.sort(key=lambda r: (r["score"], r["all"]["win_rate"]), reverse=True)
    return rows[:TOP_N]


# ── per stock ──────────────────────────────────────────────────────────────
def analyse(sym, raw):
    bars = clean_bars(raw)
    if len(bars) < 60:
        return None
    bars, events, warnings = adjust_corporate_actions(bars)
    last = bars[-1]
    out = {"symbol": sym, "as_of": last["date"], "close": round(last["c"], 2),
           "history_from": bars[0]["date"], "days": len(bars)}

    # daily ranges vs the previous close
    dd = [(b["l"], b["h"], a["c"]) for a, b in zip(bars, bars[1:]) if _gap(a, b) <= MAX_SESSION_GAP]
    def daily(rows):
        return {"down": pct_block([1 - l / pc for l, h, pc in rows]),
                "up": pct_block([h / pc - 1 for l, h, pc in rows]),
                "range": pct_block([(h - l) / pc for l, h, pc in rows])}
    out["daily"] = {"all": daily(dd), "last250": daily(dd[-250:])}
    trs = [max(b["h"], a["c"]) - min(b["l"], a["c"]) for a, b in zip(bars[-15:], bars[-14:])]
    out["atr14_pct"] = r4(sum(trs) / len(trs) / last["c"]) if trs else None

    weeks = build_weeks(bars)
    out["weeks"] = len(weeks)
    def weekly(ws):
        return {"down": pct_block([1 - min(d["l"] for d in w["days"]) / w["ref"] for w in ws]),
                "up": pct_block([max(d["h"] for d in w["days"]) / w["ref"] - 1 for w in ws]),
                "range": pct_block([(max(d["h"] for d in w["days"]) - min(d["l"] for d in w["days"])) / w["ref"] for w in ws]),
                "change": pct_block([w["days"][-1]["c"] / w["ref"] - 1 for w in ws])}
    if weeks:
        out["weekly"] = {"all": weekly(weeks), "last52": weekly(weeks[-RECENT:])}
        drift = [w["days"][-1]["c"] / w["ref"] - 1 for w in weeks]
        out["avg_week_change"] = r4(sum(drift) / len(drift))
    # Short-term orders are for the next session, so they're set from the latest
    # close. Weekly orders go in before the week starts, so they're set from the
    # last COMPLETED week's close: on Friday (or later) that's today's close and
    # the levels are for next week; Monday-Thursday it's last Friday's close and
    # the levels are for the week already under way.
    c = last["c"]
    if dt.date.fromisoformat(last["date"]).weekday() >= 4:
        wref = {"close": round(c, 2), "date": last["date"], "for": "next week"}
    else:
        this_wk = dt.date.fromisoformat(last["date"]).isocalendar()[:2]
        prev = [b for b in bars if dt.date.fromisoformat(b["date"]).isocalendar()[:2] != this_wk]
        wref = {"close": round(prev[-1]["c"], 2), "date": prev[-1]["date"], "for": "this week"} if prev else None
    out["week_ref"] = wref
    wc = wref["close"] if wref else c
    if len(weeks) >= MIN_WEEKS:
        out["buy_dip"] = best_plan(weeks, sim_buy_dip)
        out["sell_rise"] = best_plan(weeks, sim_sell_rise)
        for key in ("buy_dip", "sell_rise"):
            for p in out[key]:
                if key == "buy_dip":
                    p["order"] = round(wc * (1 - p["entry"]), 2)
                    p["exit"] = round(p["order"] * (1 + p["target"]), 2)
                else:
                    p["order"] = round(wc * (1 + p["entry"]), 2)
                    p["exit"] = round(p["order"] * (1 - p["target"]), 2)
    else:
        out["note"] = f"only {len(weeks)} weeks of history — at least {MIN_WEEKS} needed to test a weekly plan"
    if len(bars) >= 2 * D_RECENT:
        out["short_buy"] = best_short(bars, "buy")
        out["short_sell"] = best_short(bars, "sell")
        for key, sign in (("short_buy", -1), ("short_sell", 1)):
            for p in out[key]:
                p["order"] = round(c * (1 + sign * p["entry"]), 2)
                p["exit"] = round(p["order"] * (1 - sign * p["target"]), 2)
    out["warnings"] = warnings[:3]
    if events:
        out["adjusted_for"] = events[-3:]
    return out


def universe():
    syms, why = [], {}

    def add(s, reason):
        s = re.sub(r"-(SM|ST|BE|BZ|EQ)$", "", str(s or "").strip().upper())
        if s and re.fullmatch(r"[A-Z0-9&\-]+", s):
            if s not in why:
                syms.append(s)
                why[s] = []
            if reason not in why[s]:
                why[s].append(reason)

    h = load_json(HOLDINGS, {}) or {}
    hold = h.get("holdings") or {}
    for s in (hold.keys() if isinstance(hold, dict) else
              [x.get("tradingsymbol") or x.get("symbol") if isinstance(x, dict) else x for x in hold]):
        add(s, "holding")
    if os.path.exists(WATCH):
        for line in open(WATCH, encoding="utf-8"):
            line = line.split("#")[0].strip()
            if line:
                add(line, "watch")
    scan = load_json(SCAN, {}) or {}
    for key, label in (("squeeze_stocks", "squeeze"), ("countertrend_stocks", "countertrend"), ("watchlist_stocks", "watchlist")):
        for x in scan.get(key) or []:
            add(x.get("symbol"), label)
    cyc = load_json(CYCLES, {}) or {}
    for x in cyc.get("stocks") or []:
        if x.get("grade") in ("STRONG", "MODERATE"):
            add(x.get("symbol"), "cycles")
    return syms, why


def main():
    syms, why = universe()
    stocks, missing = {}, []
    for s in syms:
        path = os.path.join(RAW_DIR, f"{s}.json")
        if not os.path.exists(path):
            missing.append(s)
            continue
        try:
            r = analyse(s, load_history(s, path))
        except Exception as e:                      # one bad file must not stop the rest
            print(f"  {s}: skipped ({type(e).__name__}: {e})")
            continue
        if r:
            r["why"] = why.get(s, [])
            stocks[s] = r
        else:
            missing.append(s)               # fewer than 60 sessions of history
    as_of = max((v["as_of"] for v in stocks.values()), default=None)
    mine = {s: v for s, v in stocks.items() if {"holding", "watch"} & set(v["why"])}
    more = {s: v for s, v in stocks.items() if s not in mine}
    out = {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of": as_of,
        "params": {"round_trip_cost": ROUND_TRIP_COST, "entry_grid": ENTRY_GRID, "target_grid": TARGET_GRID,
                   "short_entry_grid": D_ENTRY_GRID, "short_target_grid": D_TARGET_GRID, "short_hold_grid": D_HOLD_GRID,
                   "short_recent_sessions": D_RECENT,
                   "min_weeks": MIN_WEEKS, "min_avg": MIN_AVG, "recent_weeks": RECENT, "min_fills": MIN_FILLS,
                   "min_fills_recent": MIN_FILLS_RECENT},
        "missing": [{"symbol": s, "why": why.get(s, [])} for s in missing
                    if {"holding", "watch"} & set(why.get(s, []))],
        "more_count": len(more),
        "stocks": mine,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    with open(OUT_MORE, "w") as f:
        json.dump({"generated_at": out["generated_at"], "as_of": as_of, "stocks": more}, f, separators=(",", ":"))
    held = [s for s in stocks if "holding" in stocks[s]["why"]]
    print(f"ranges: {len(stocks)} stocks ({len(held)} holdings), {len(missing)} without price history, as of {as_of}")


if __name__ == "__main__":
    main()
