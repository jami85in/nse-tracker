#!/usr/bin/env python3
"""
Runs the squeeze->blast cycle analysis over the accumulated historical data
in data/backtest/raw_closes/*.json, once collection is complete (or on
partial data, if you want an early look — it just analyzes whatever's there).

Mirrors the exact classify() logic in scan.py:
  SQUEEZE: BB width < 4.5%, Stoch K < 50, Stoch K > Stoch D (turning up)
  BLAST:   BB width > 1.15x its value 5 days ago, Stoch K > 75, price > SMA10
           (SMA10 substitutes for EMA10 here — close enough for cycle timing)

Writes data/backtest/report.json — a ranked per-stock breakdown of:
  - how many complete squeeze->blast cycles were found
  - average/stddev of cycle length (days)
  - average return per cycle
  - a regularity score (consistency of the gap between cycle starts)
  - a stability flag (does the pattern hold in the first half of the window
    vs the second half, or is it just noise from one period)
"""
import json, os, glob, statistics

RAW_DIR = "data/backtest/raw_closes"
REPORT_PATH = "data/backtest/report.json"


def compute_indicators(closes):
    n = len(closes)
    bb_width = [None] * n
    for i in range(19, n):
        window = closes[i - 19:i + 1]
        m = sum(window) / 20
        var = sum((x - m) ** 2 for x in window) / 20
        std = var ** 0.5
        bb_width[i] = ((m + 2 * std) - (m - 2 * std)) / m * 100 if m > 0 else None

    raw_k = [None] * n
    for i in range(13, n):
        window = closes[i - 13:i + 1]
        hi, lo = max(window), min(window)
        raw_k[i] = ((closes[i] - lo) / (hi - lo)) * 100 if hi > lo else 50

    stoch_k = [None] * n
    for i in range(15, n):
        if None in (raw_k[i-2], raw_k[i-1], raw_k[i]):
            continue
        stoch_k[i] = (raw_k[i-2] + raw_k[i-1] + raw_k[i]) / 3

    stoch_d = [None] * n
    for i in range(17, n):
        if None in (stoch_k[i-2], stoch_k[i-1], stoch_k[i]):
            continue
        stoch_d[i] = (stoch_k[i-2] + stoch_k[i-1] + stoch_k[i]) / 3

    return bb_width, stoch_k, stoch_d


def find_cycles(dates, closes, bb_width, stoch_k, stoch_d):
    n = len(closes)
    sma10 = [None] * n
    for i in range(9, n):
        sma10[i] = sum(closes[i-9:i+1]) / 10

    cycles = []
    in_squeeze, entry_idx = False, None
    for i in range(20, n):
        if bb_width[i] is None or stoch_k[i] is None or stoch_d[i] is None:
            continue
        bb5ago_idx = i - 5 if i - 5 >= 0 else i
        bb5ago = bb_width[bb5ago_idx] if bb_width[bb5ago_idx] is not None else bb_width[i]

        is_squeeze = bb_width[i] < 4.5 and stoch_k[i] < 50 and stoch_k[i] > stoch_d[i]
        is_blast = (bb5ago and bb_width[i] > bb5ago * 1.15 and stoch_k[i] > 75
                    and sma10[i] and closes[i] > sma10[i])

        if not in_squeeze and is_squeeze:
            in_squeeze, entry_idx = True, i
        elif in_squeeze and is_blast:
            ret = ((closes[i] - closes[entry_idx]) / closes[entry_idx]) * 100
            cycles.append({
                "entry_date": dates[entry_idx], "entry_price": round(closes[entry_idx], 2),
                "exit_date": dates[i], "exit_price": round(closes[i], 2),
                "cycle_days": i - entry_idx, "return_pct": round(ret, 2),
            })
            in_squeeze, entry_idx = False, None
    return cycles


def analyze_symbol(path):
    with open(path) as f:
        series = json.load(f)
    if len(series) < 40:
        return None
    series = sorted(series, key=lambda p: p["date"])
    dates = [p["date"] for p in series]
    closes = [p["close"] for p in series]

    bb_width, stoch_k, stoch_d = compute_indicators(closes)
    cycles = find_cycles(dates, closes, bb_width, stoch_k, stoch_d)
    if not cycles:
        return {"cycles_found": 0}

    cycle_days = [c["cycle_days"] for c in cycles]
    returns = [c["return_pct"] for c in cycles]
    gaps = []
    for i in range(1, len(cycles)):
        from datetime import date
        d1 = date.fromisoformat(cycles[i-1]["entry_date"])
        d2 = date.fromisoformat(cycles[i]["entry_date"])
        gaps.append((d2 - d1).days)

    mid = len(cycles) // 2
    first_half, second_half = cycles[:mid], cycles[mid:]
    fh_days = statistics.mean([c["cycle_days"] for c in first_half]) if first_half else None
    sh_days = statistics.mean([c["cycle_days"] for c in second_half]) if second_half else None
    stable = None
    if fh_days and sh_days:
        diff = abs(fh_days - sh_days) / max(fh_days, sh_days)
        stable = diff < 0.5

    gap_mean = statistics.mean(gaps) if gaps else None
    gap_std = statistics.stdev(gaps) if len(gaps) > 1 else None
    regularity = round(gap_mean / (gap_std + 1), 2) if gap_mean and gap_std is not None else None

    return {
        "cycles_found": len(cycles),
        "avg_cycle_days": round(statistics.mean(cycle_days)),
        "cycle_days_stddev": round(statistics.stdev(cycle_days), 1) if len(cycle_days) > 1 else None,
        "avg_return_pct": round(statistics.mean(returns), 2),
        "regularity_score": regularity,
        "sufficient_data": len(cycles) >= 5,
        "stable_across_halves": stable,
        "first_half_avg_days": round(fh_days) if fh_days else None,
        "second_half_avg_days": round(sh_days) if sh_days else None,
        "cycles": cycles,
    }


def main():
    files = sorted(glob.glob(os.path.join(RAW_DIR, "*.json")))
    print(f"Found {len(files)} symbol data files.")

    report = {}
    for path in files:
        symbol = os.path.splitext(os.path.basename(path))[0]
        try:
            result = analyze_symbol(path)
            if result:
                report[symbol] = result
        except Exception as e:
            print(f"  {symbol}: analysis error — {e}")

    # Rank by cycles_found (most data-rich first), among those with sufficient data
    ranked = sorted(
        [(sym, r) for sym, r in report.items() if r.get("cycles_found", 0) > 0],
        key=lambda kv: kv[1]["cycles_found"], reverse=True
    )

    os.makedirs("data/backtest", exist_ok=True)
    with open(REPORT_PATH, "w") as f:
        json.dump({
            "symbols_analyzed": len(report),
            "symbols_with_cycles": len(ranked),
            "symbols_sufficient_data": sum(1 for _, r in ranked if r.get("sufficient_data")),
            "report": report,
        }, f)

    print(f"Analyzed {len(report)} symbols, {len(ranked)} had at least one cycle, "
          f"{sum(1 for _, r in ranked if r.get('sufficient_data'))} have >=5 cycles.")
    print(f"Written to {REPORT_PATH}")


if __name__ == "__main__":
    main()
