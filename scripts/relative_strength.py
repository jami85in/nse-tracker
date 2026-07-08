#!/usr/bin/env python3
"""
Computes relative-strength stats for every stock we have OHLC data for:
  - N-day return vs its sector index's N-day return (is it beating its own sector?)
  - N-day return vs Nifty 50's N-day return (is it beating the broad market?)
  - trailing correlation between the stock's daily returns and its sector's
    daily returns (is it moving WITH the sector, or independently?)

This directly answers "is the stock moving with or against the tide" —
your original ask. Writes data/backtest/relative_strength.json, read by the
dashboard to show "vs Sector" / "vs Nifty" badges on squeeze/blast/short
cards, and feeds into the conviction score.

Requires: data/backtest/raw_ohlc/*.json (stock prices), 
          data/backtest/raw_indices/*.json (index prices),
          data/backtest/sector_membership.json (symbol -> sector mapping)
"""
import json, os, glob, statistics

OHLC_DIR = "data/backtest/raw_ohlc"
INDEX_DIR = "data/backtest/raw_indices"
SECTOR_MAP_PATH = "data/backtest/sector_membership.json"
OUT_PATH = "data/backtest/relative_strength.json"

LOOKBACK_DAYS = 20  # trading days used for both the return comparison and correlation


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def index_filename_for(sector_name):
    return sector_name.replace(" ", "_").replace("&", "and") + ".json"


def n_day_return(closes, n):
    if len(closes) < n + 1:
        return None
    return (closes[-1] - closes[-1 - n]) / closes[-1 - n] * 100


def daily_returns(closes):
    return [(closes[i] - closes[i-1]) / closes[i-1] * 100 for i in range(1, len(closes))]


def correlation(a, b):
    n = min(len(a), len(b))
    if n < 5:
        return None
    a, b = a[-n:], b[-n:]
    try:
        return round(statistics.correlation(a, b), 3)
    except (statistics.StatisticsError, AttributeError):
        # statistics.correlation needs Python 3.10+; fall back to manual calc
        mean_a, mean_b = statistics.mean(a), statistics.mean(b)
        cov = sum((x-mean_a)*(y-mean_b) for x, y in zip(a, b))
        std_a = (sum((x-mean_a)**2 for x in a)) ** 0.5
        std_b = (sum((y-mean_b)**2 for y in b)) ** 0.5
        if std_a == 0 or std_b == 0:
            return None
        return round(cov / (std_a * std_b), 3)


def main():
    sector_data = load_json(SECTOR_MAP_PATH, {"symbol_to_sectors": {}})
    symbol_to_sectors = sector_data.get("symbol_to_sectors", {})

    # Load Nifty 50 as the market benchmark
    nifty_series = load_json(os.path.join(INDEX_DIR, "Nifty_50.json"), [])
    nifty_series = sorted(nifty_series, key=lambda p: p["date"])
    nifty_closes = [p["close"] for p in nifty_series]
    nifty_dates = [p["date"] for p in nifty_series]
    nifty_ret_20d = n_day_return(nifty_closes, LOOKBACK_DAYS)
    nifty_daily_rets = daily_returns(nifty_closes)
    print(f"Nifty 50: {len(nifty_series)} days loaded, 20d return {nifty_ret_20d}")

    # Pre-load all sector index series once
    sector_series_cache = {}

    def get_sector_series(sector_name):
        if sector_name not in sector_series_cache:
            path = os.path.join(INDEX_DIR, index_filename_for(sector_name))
            series = load_json(path, [])
            series = sorted(series, key=lambda p: p["date"])
            sector_series_cache[sector_name] = series
        return sector_series_cache[sector_name]

    ohlc_files = sorted(glob.glob(os.path.join(OHLC_DIR, "*.json")))
    print(f"Found {len(ohlc_files)} stock OHLC files.")

    results = {}
    for path in ohlc_files:
        symbol = os.path.splitext(os.path.basename(path))[0]
        series = load_json(path, [])
        if not series or len(series) < LOOKBACK_DAYS + 5:
            continue
        series = sorted(series, key=lambda p: p["date"])
        closes = [p["close"] for p in series if p.get("close")]
        if len(closes) < LOOKBACK_DAYS + 5:
            continue

        stock_ret_20d = n_day_return(closes, LOOKBACK_DAYS)
        stock_daily_rets = daily_returns(closes)

        vs_nifty = round(stock_ret_20d - nifty_ret_20d, 2) if stock_ret_20d is not None and nifty_ret_20d is not None else None
        corr_nifty = correlation(stock_daily_rets, nifty_daily_rets)

        sectors = symbol_to_sectors.get(symbol, [])
        sector_comparisons = []
        for sector_name in sectors:
            sec_series = get_sector_series(sector_name)
            if not sec_series:
                continue
            sec_closes = [p["close"] for p in sec_series]
            sec_ret_20d = n_day_return(sec_closes, LOOKBACK_DAYS)
            sec_daily_rets = daily_returns(sec_closes)
            if sec_ret_20d is None:
                continue
            vs_sector = round(stock_ret_20d - sec_ret_20d, 2) if stock_ret_20d is not None else None
            corr_sector = correlation(stock_daily_rets, sec_daily_rets)
            sector_comparisons.append({
                "sector": sector_name,
                "sector_return_20d": round(sec_ret_20d, 2),
                "vs_sector_pct": vs_sector,
                "correlation_with_sector": corr_sector,
            })

        results[symbol] = {
            "stock_return_20d": round(stock_ret_20d, 2) if stock_ret_20d is not None else None,
            "nifty_return_20d": round(nifty_ret_20d, 2) if nifty_ret_20d is not None else None,
            "vs_nifty_pct": vs_nifty,
            "correlation_with_nifty": corr_nifty,
            "sectors": sector_comparisons,
        }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump({
            "lookback_days": LOOKBACK_DAYS,
            "as_of": nifty_dates[-1] if nifty_dates else None,
            "results": results,
        }, f)

    print(f"\nComputed relative strength for {len(results)} symbols.")
    print(f"Written to {OUT_PATH}")


if __name__ == "__main__":
    main()
