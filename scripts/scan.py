"""
NSE Squeeze & Blast Scanner — GitHub Actions edition
======================================================
Runs on a schedule via GitHub Actions. Does ALL the heavy lifting
(data fetch + indicator math) locally with free libraries.
Claude API is called ONLY ONCE per run, on a pre-filtered shortlist
of ~5-10 candidates, to add reasoning/ranking — not raw data fetching.

This cuts Claude API cost by ~95% vs. calling Claude with web_search
for every scan, because:
  1. No web_search tool tokens (search results are verbose & costly)
  2. No need to scan hundreds of stocks via the model — math is done in numpy/pandas
  3. Claude only sees ~10 rows of clean numeric data, not raw HTML/search results

Outputs: data/scan_latest.json  (committed back to repo by the Action)
The React frontend reads this static JSON via raw.githubusercontent.com
or GitHub Pages — zero API cost for the frontend.
"""

import json
import os
import time
import datetime
from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd
import requests

# ── Config ────────────────────────────────────────────────────────────────
# Stock universe is fetched dynamically from NSE's official Nifty 500
# constituent list at the start of each run (see fetch_nifty500_universe()
# below) — this widens coverage from ~84 hand-picked names to the full
# ~500, which matters directly for SQUEEZE detection: on any given day
# only a small fraction of stocks sit in a genuinely tight consolidation,
# so a larger universe meaningfully increases the odds of catching one.
# It also self-corrects for delisted/renamed symbols automatically (the
# hardcoded list below had several: TATAMOTORS' 2025 demerger, LTIM,
# VEDANTA and ZOMATO/ETERNAL ticker changes all showed up as "possibly
# delisted" errors in earlier runs) since NSE's own list is always current.
#
# FALLBACK_STOCK_LIST below is used ONLY if the live NSE fetch fails for
# any reason (network issue, NSE API change, etc.) — so the scan never
# hard-fails to zero stocks, it just falls back to a smaller known-good set.
FALLBACK_STOCK_LIST = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "SBIN", "AXISBANK",
    "KOTAKBANK", "BAJFINANCE", "BHARTIARTL", "ITC", "LT", "MARUTI", "TITAN",
    "SUNPHARMA", "ULTRACEMCO", "NTPC", "POWERGRID", "TATASTEEL",
    "IDFCFIRSTB", "BANKBARODA", "PNB", "CANBK", "FEDERALBNK", "INDUSINDBK",
    "ADANIPORTS", "ADANIENT", "JSWSTEEL", "HINDALCO", "COALINDIA", "ONGC",
    "BPCL", "GRASIM", "DRREDDY", "CIPLA", "DIVISLAB", "APOLLOHOSP",
    "BAJAJFINSV", "HDFCLIFE", "SBILIFE", "HEROMOTOCO", "EICHERMOT", "TVSMOTOR",
    "M&M", "WIPRO", "TECHM", "HCLTECH", "ASIANPAINT", "NESTLEIND",
    "BRITANNIA", "DABUR", "GODREJCP", "PIDILITIND", "SIEMENS", "ABB",
    "HAVELLS", "DLF", "GODREJPROP", "HINDZINC", "AMBUJACEM",
    "ACC", "SHREECEM", "IDEA", "PAYTM", "NYKAA", "TRENT",
    "VOLTAS", "PERSISTENT", "MPHASIS", "LUPIN", "AUROPHARMA", "BIOCON",
    "PFC", "RECLTD", "IRFC", "IRCTC", "INDIGO", "BEL", "HAL", "MAZDOCK",
]

NSE_NIFTY500_CSV_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"

NSE_BASE = "https://www.nseindia.com"
NSE_API_HIST = "https://www.nseindia.com/api/historical/cm/equity?symbol={symbol}&series=[%22EQ%22]&from={frm}&to={to}"
NSE_API_HOLIDAYS = "https://www.nseindia.com/api/holiday-master?type=trading"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

MIN_PREDICTED_RETURN = 5.0  # % — hard floor for squeeze candidates


def fetch_nifty500_universe(session: requests.Session) -> list:
    """
    Fetch the live, official Nifty 500 constituent list from NSE's own
    archive CSV. This is the full ~500-stock universe rather than the
    ~84-stock curated sample — directly relevant to SQUEEZE detection,
    since on any given day only a small fraction of stocks sit in a
    genuinely tight consolidation; a larger universe meaningfully
    improves the odds of catching one. It also self-corrects for
    delisted/renamed symbols (e.g. demergers, ticker changes) since
    NSE's own list is always current, unlike a hardcoded snapshot.

    Falls back to FALLBACK_STOCK_LIST if the live fetch fails for any
    reason, so a network hiccup never reduces the scan to zero stocks.
    """
    try:
        r = session.get(NSE_NIFTY500_CSV_URL, timeout=15)
        r.raise_for_status()
        # CSV columns: Company Name,Industry,Symbol,Series,ISIN Code
        lines = r.text.strip().splitlines()
        if len(lines) < 100:  # sanity check — a real Nifty 500 CSV has ~501 lines
            raise ValueError(f"CSV too short ({len(lines)} lines) — likely not the real file")
        symbols = []
        for line in lines[1:]:  # skip header row
            parts = line.split(",")
            if len(parts) >= 3:
                sym = parts[2].strip()
                if sym:
                    symbols.append(sym)
        if len(symbols) < 100:
            raise ValueError(f"Parsed only {len(symbols)} symbols — likely a parsing issue")
        print(f"Fetched live Nifty 500 universe: {len(symbols)} symbols")
        return symbols
    except Exception as e:
        print(f"Live Nifty 500 fetch failed ({e}), using {len(FALLBACK_STOCK_LIST)}-stock fallback list")
        return FALLBACK_STOCK_LIST

# ── NSE trading holiday calendar ─────────────────────────────────────────
# Primary source is always the live NSE API (NSE_API_HOLIDAYS above) since
# the exchange occasionally amends its calendar after announcement. This
# hardcoded list is ONLY a fallback for the rare case that API call fails
# (network hiccup, NSE site change, etc.) — without it, a failed holiday
# check could silently let the workflow run on a real holiday (wasting the
# Claude API call) or, worse, skip a real trading day entirely.
# Source: official NSE 2026 holiday circular, cross-checked against
# angelone.in/nse-holidays (verified weekday-correct as of this writing).
NSE_HOLIDAYS_2026_FALLBACK = {
    "2026-01-15": "Municipal Corporation Election (Maharashtra)",
    "2026-01-26": "Republic Day",
    "2026-02-15": "Mahashivratri",
    "2026-03-03": "Holi",
    "2026-03-21": "Id-Ul-Fitr (Ramadan Eid)",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-08-15": "Independence Day",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-08": "Diwali Laxmi Pujan",
    "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}

# Special sessions that run on an otherwise-non-trading day (the reverse
# case): Muhurat Trading is a short, symbolic session held on Diwali
# Laxmi Pujan even when that date falls on a weekend. For 2026, Diwali
# Laxmi Pujan / Muhurat Trading falls on Sunday, November 8 — confirmed
# across NSE/BSE holiday circulars and major broker calendars (Zerodha,
# Groww, etc.) as of this writing. Exact session timing is announced by
# NSE closer to the date and is typically a ~1 hour evening window, so a
# scan triggered on this date will still run scan.py's normal logic
# against whatever prices are available at run time.
NSE_SPECIAL_SESSIONS_2026_FALLBACK = {
    "2026-11-08": "Diwali Laxmi Pujan — Muhurat Trading (special session, Sunday)",
}


def fetch_nse_holiday_calendar(session: requests.Session) -> dict | None:
    """
    Pull NSE's live official trading-holiday list. Returns a dict of
    {YYYY-MM-DD: description} or None if the call fails for any reason
    (caller should fall back to the hardcoded list below).
    """
    try:
        r = session.get(NSE_API_HOLIDAYS, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        fo = data.get("FO") or data.get("CM") or []
        holidays = {}
        for item in fo:
            raw_date = item.get("tradingDate")
            desc = item.get("description", "")
            if not raw_date:
                continue
            try:
                # NSE returns dates like "26-Jan-2026"
                dt = datetime.datetime.strptime(raw_date, "%d-%b-%Y").date()
                holidays[dt.isoformat()] = desc
            except ValueError:
                continue
        return holidays if holidays else None
    except Exception:
        return None


def is_trading_day(check_date: datetime.date, session: requests.Session = None) -> tuple[bool, str]:
    """
    Returns (is_open, reason). Checks, in order:
    1. Live NSE holiday API (authoritative, current) for a closure on this date
    2. Hardcoded fallback list if the live call fails
    3. Special-session override (a trading day that would otherwise be
       closed, e.g. a weekend Muhurat Trading session)
    4. Plain weekday/weekend check as the final baseline
    """
    date_str = check_date.isoformat()

    holidays = None
    if session is not None:
        holidays = fetch_nse_holiday_calendar(session)
    source = "live NSE calendar"
    if holidays is None:
        holidays = NSE_HOLIDAYS_2026_FALLBACK
        source = "fallback calendar (live API unavailable)"

    special_sessions = NSE_SPECIAL_SESSIONS_2026_FALLBACK

    if date_str in special_sessions:
        return True, f"Special trading session: {special_sessions[date_str]}"

    if date_str in holidays:
        return False, f"NSE holiday ({source}): {holidays[date_str]}"

    if check_date.weekday() >= 5:  # Saturday=5, Sunday=6
        return False, "Weekend"

    return True, f"Regular trading day ({source})"


# ── NSE session (cookies required) ───────────────────────────────────────
def get_nse_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    # Hitting the homepage first sets the cookies NSE's API requires
    s.get(NSE_BASE, timeout=10)
    time.sleep(0.3)
    return s


def fetch_history_nse(session: requests.Session, symbol: str, days: int = 90):
    """Primary source: NSE official historical API."""
    try:
        to_date = datetime.date.today()
        frm_date = to_date - datetime.timedelta(days=days)
        url = NSE_API_HIST.format(
            symbol=symbol,
            frm=frm_date.strftime("%d-%m-%Y"),
            to=to_date.strftime("%d-%m-%Y"),
        )
        r = session.get(url, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json().get("data", [])
        if not data:
            return None
        df = pd.DataFrame(data)
        df = df.rename(columns={
            "CH_TIMESTAMP": "date", "CH_OPENING_PRICE": "open",
            "CH_TRADE_HIGH_PRICE": "high", "CH_TRADE_LOW_PRICE": "low",
            "CH_CLOSING_PRICE": "close", "CH_TOT_TRADED_QTY": "volume",
        })
        cols = ["date", "open", "high", "low", "close", "volume"]
        df = df[[c for c in cols if c in df.columns]]
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df.dropna()
    except Exception:
        return None


def fetch_history_yfinance(symbol: str, days: int = 90):
    """Fallback source if NSE blocks the runner IP (common on cloud IPs)."""
    try:
        import yfinance as yf
        ticker = f"{symbol}.NS"
        df = yf.download(ticker, period=f"{days}d", interval="1d", progress=False)
        if df.empty:
            return None
        df = df.reset_index()
        df.columns = [c.lower() if isinstance(c, str) else c[0].lower() for c in df.columns]
        return df[["date", "open", "high", "low", "close", "volume"]].dropna()
    except Exception:
        return None


def fetch_history(session: requests.Session, symbol: str):
    df = fetch_history_nse(session, symbol)
    if df is None or len(df) < 35:
        df = fetch_history_yfinance(symbol)
    return df


# ── Indicator math (pure pandas/numpy — zero API cost) ───────────────────
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    # Bollinger Bands (20, 2)
    df["bb_mid"] = df["close"].rolling(20).mean()
    df["bb_std"] = df["close"].rolling(20).std()
    df["bb_upper"] = df["bb_mid"] + 2 * df["bb_std"]
    df["bb_lower"] = df["bb_mid"] - 2 * df["bb_std"]
    df["bb_width_pct"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"] * 100

    # EMA 10 / 30
    df["ema10"] = df["close"].ewm(span=10, adjust=False).mean()
    df["ema30"] = df["close"].ewm(span=30, adjust=False).mean()

    # RSI(14) -> Stochastic of RSI -> %K smooth 3 -> %D smooth 3
    # (matching the chart's "Stoch RSI 40 60 3 3" where 40/60 are the
    # user's custom oversold/overbought reference lines, not the lookback)
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    # When loss is 0 over the lookback (price only went up — a strong
    # uptrend), RSI should correctly read 100, not NaN. Computing rs as
    # gain/loss and replacing 0-loss with NaN silently breaks RSI for
    # exactly the strong-trend stocks we most want to flag as BLAST, so
    # handle that case explicitly instead of dividing by a NaN denominator.
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))
    df.loc[(loss == 0) & (gain > 0), "rsi"] = 100.0
    df.loc[(loss == 0) & (gain == 0), "rsi"] = 50.0  # flat/no movement at all

    rsi_min = df["rsi"].rolling(14).min()
    rsi_max = df["rsi"].rolling(14).max()
    stoch = (df["rsi"] - rsi_min) / (rsi_max - rsi_min).replace(0, np.nan) * 100
    # Same fix at the stochastic level: if RSI has been pinned at its
    # rolling max for the whole window (rsi_max == rsi_min), that's a
    # maximally overbought/oversold condition, not an undefined one.
    stoch = stoch.where(rsi_max != rsi_min, 100.0)
    df["stoch_k"] = stoch.rolling(3).mean()
    df["stoch_d"] = df["stoch_k"].rolling(3).mean()

    # Traditional pivots (prior day H/L/C)
    df["pivot"] = (df["high"].shift(1) + df["low"].shift(1) + df["close"].shift(1)) / 3
    df["r1"] = 2 * df["pivot"] - df["low"].shift(1)
    df["r2"] = df["pivot"] + (df["high"].shift(1) - df["low"].shift(1))
    df["s1"] = 2 * df["pivot"] - df["high"].shift(1)
    df["s2"] = df["pivot"] - (df["high"].shift(1) - df["low"].shift(1))

    # Average True Range (14) — used to estimate breakout magnitude for
    # SQUEEZE targets. During an active squeeze, the daily H-L range (and
    # therefore the traditional pivot R1, which is derived from just the
    # prior day's range) compresses toward near-zero — that's the whole
    # point of a squeeze. Using R1 as the upside target for a SQUEEZE
    # signal is mathematically self-defeating: it asks the target to be
    # ≥5% above price while simultaneously requiring the recent range be
    # tight enough to produce a sub-3% BB width, which are structurally
    # close to incompatible. ATR instead measures typical daily movement
    # over a longer 14-day window, which still reflects the stock's
    # normal volatility/momentum character even while today's range has
    # tightened — making it a more realistic basis for "how far could
    # this move once it breaks out" than the compressed pivot range.
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr14"] = tr.rolling(14).mean()

    return df


@dataclass
class Candidate:
    symbol: str
    price: float                    # current/live price at this scan
    bb_width: float
    bb_width_5d_ago: float
    stoch_k: float
    stoch_d: float
    ema10: float
    ema30: float
    pivot_r1: float
    pivot_r2: float
    pivot_s1: float
    predicted_return: float
    setup: str  # "SQUEEZE" or "BLAST"

    # --- SQUEEZE (entry) fields ---
    # entry_price/entry_date: the price+date this squeeze was first flagged
    # (set once, on first detection, then held fixed by the ledger even as
    # the stock is re-scanned on later days — see update_ledger()).
    entry_price: float = None
    entry_date: str = None
    # target_price: estimated upside if the move plays out to pivot R1 —
    # the "how much could this make me" answer at the moment of entry.
    target_price: float = None
    target_return_pct: float = None

    # --- BLAST (exit) fields ---
    # blast_entry_price/blast_entry_date: price+date the SQUEEZE ended and
    # this move actually started (found by walking back through bb_width
    # history — see find_blast_entry()). This is when you *would* have
    # bought if you'd acted on the original squeeze signal.
    blast_entry_price: float = None
    blast_entry_date: str = None
    # exit_price/exit_date: price+date THIS blast/exit signal fired —
    # i.e. when the tool told you to consider selling. Distinct from
    # `price`/today's date once a BLAST entry persists across multiple
    # scans in its 10-day retention window.
    exit_price: float = None
    exit_date: str = None
    holding_period_days: int = None   # calendar days, blast_entry_date -> exit_date
    return_since_entry: float = None  # % gained, blast_entry_price -> exit_price


def find_blast_entry(df: pd.DataFrame, lookback: int = 20):
    """
    Walk backwards from the latest bar to find where the current blast began:
    the most recent local minimum in bb_width_pct before it started expanding
    toward today's value. That bar's close is treated as the squeeze-end /
    entry price the move launched from. The latest bar's own date is treated
    as the exit date (the day this blast/exit signal fired).

    Returns (entry_price, entry_date, exit_date, holding_period_days)
    or (None, None, None, None) if no clear point found.
    """
    window = df.tail(lookback + 1).reset_index(drop=True)
    if len(window) < 5 or window["bb_width_pct"].isna().all():
        return None, None, None, None

    # Find the index of the minimum BB width in the lookback window —
    # that's the tightest point of the squeeze, i.e. where the blast started.
    widths = window["bb_width_pct"]
    min_idx = widths.idxmin()

    # Guard: if the minimum is the very last bar, there's no real squeeze-to-
    # blast transition yet (still tightening) — not a usable entry point.
    if min_idx >= len(window) - 2:
        return None, None, None, None

    entry_row = window.iloc[min_idx]
    exit_row = window.iloc[-1]

    entry_price = float(entry_row["close"])
    entry_dt = entry_row["date"]
    exit_dt = exit_row["date"]

    entry_date = entry_dt.strftime("%Y-%m-%d") if hasattr(entry_dt, "strftime") else str(entry_dt)
    exit_date = exit_dt.strftime("%Y-%m-%d") if hasattr(exit_dt, "strftime") else str(exit_dt)

    try:
        holding_period_days = (pd.to_datetime(exit_dt) - pd.to_datetime(entry_dt)).days
    except Exception:
        holding_period_days = None

    return entry_price, entry_date, exit_date, holding_period_days


def diagnose(symbol: str, df: pd.DataFrame, scan_date: str = None) -> dict:
    """
    Like classify(), but ALWAYS returns a full condition breakdown —
    which checks passed/failed and by how much — rather than only
    returning something when every condition lines up. This answers
    "where does this stock currently stand relative to a SQUEEZE/BLAST
    signal, even if it hasn't fully triggered yet" — useful for spotting
    near-miss opportunities the strict classify() filter would otherwise
    hide entirely.
    """
    if len(df) < 35:
        return {"symbol": symbol, "status": "insufficient_history"}
    last = df.iloc[-1]
    prev5 = df.iloc[-6] if len(df) >= 6 else df.iloc[0]

    if pd.isna(last["bb_width_pct"]) or pd.isna(last["stoch_k"]):
        return {"symbol": symbol, "status": "no_data"}

    price = float(last["close"])
    bb_width = float(last["bb_width_pct"])
    stoch_k = float(last["stoch_k"])
    stoch_d = float(last["stoch_d"])
    ema10 = float(last["ema10"])
    atr14 = float(last["atr14"]) if not pd.isna(last["atr14"]) else None
    bb_width_5d_min = float(df["bb_width_pct"].iloc[-5:].min()) if len(df) >= 5 else bb_width

    atr_return = round(((price + 3 * atr14) - price) / price * 100, 2) if atr14 and atr14 > 0 else 0

    checks = {
        "bb_tight": bb_width < 3.0,
        "bb_near_recent_low": bb_width <= bb_width_5d_min * 1.6,
        "stoch_oversold_zone": stoch_k < 45,
        "stoch_turning_up": stoch_k > stoch_d,
        "target_return_ok": atr_return >= MIN_PREDICTED_RETURN,
    }
    passed = sum(checks.values())

    return {
        "symbol": symbol,
        "date": scan_date or (last["date"].strftime("%Y-%m-%d") if hasattr(last["date"], "strftime") else str(last["date"])),
        "price": round(price, 2),
        "bb_width": round(bb_width, 2),
        "bb_width_5d_min": round(bb_width_5d_min, 2),
        "stoch_k": round(stoch_k, 1),
        "stoch_d": round(stoch_d, 1),
        "atr_target_return_pct": atr_return,
        "checks": checks,
        "checks_passed": f"{passed}/5",
        "would_be_squeeze": passed == 5,
        "would_be_watchlist": (passed < 5 and bb_width < 8.0 and stoch_k < 50 and stoch_k > stoch_d),
    }


def classify(symbol: str, df: pd.DataFrame, scan_date: str = None):
    if len(df) < 35:
        return None
    last = df.iloc[-1]
    prev5 = df.iloc[-6] if len(df) >= 6 else df.iloc[0]

    if pd.isna(last["bb_width_pct"]) or pd.isna(last["stoch_k"]):
        return None

    price = float(last["close"])
    bb_width = float(last["bb_width_pct"])
    bb_width_5d = float(prev5["bb_width_pct"]) if not pd.isna(prev5["bb_width_pct"]) else bb_width
    stoch_k = float(last["stoch_k"])
    stoch_d = float(last["stoch_d"])
    ema10 = float(last["ema10"])
    ema30 = float(last["ema30"])
    r1, r2, s1 = float(last["r1"]), float(last["r2"]), float(last["s1"])
    atr14 = float(last["atr14"]) if not pd.isna(last["atr14"]) else None

    # ATR-projected breakout target: a squeeze release typically expands
    # to several multiples of the recent ATR. 3x ATR is the standard,
    # widely-used projection for a volatility-expansion breakout target
    # (this is the conventional multiplier in most squeeze/breakout
    # trading methodologies — more aggressive multipliers like 4-5x
    # overstate the target on lower-volatility names). This is a
    # genuine improvement over the previous pivot-R1-based target, which
    # was structurally self-defeating for a squeeze signal: pivot R1 is
    # derived from the prior day's H-L range, which is mechanically
    # compressed during an active squeeze — asking that compressed-range
    # figure to simultaneously clear a 5% bar made the SQUEEZE condition
    # close to impossible to satisfy. ATR instead reflects the stock's
    # broader volatility character. Falls back to pivot R1 if ATR is
    # unavailable (e.g. insufficient history) so the field is never blank.
    if atr14 and atr14 > 0:
        atr_target_price = price + (3 * atr14)
        atr_predicted_return = round((atr_target_price - price) / price * 100, 2) if price else 0
    else:
        atr_target_price = r1
        atr_predicted_return = round((r1 - price) / price * 100, 2) if price else 0

    # predicted_return retained as the pivot-R1-based figure for BLAST's
    # existing use and for display alongside the ATR target on SQUEEZE
    # cards — kept separate so existing BLAST logic is untouched.
    predicted_return = round((r1 - price) / price * 100, 2) if price else 0

    last_date = last["date"]
    last_date_str = last_date.strftime("%Y-%m-%d") if hasattr(last_date, "strftime") else str(last_date)
    if scan_date is None:
        scan_date = last_date_str

    # SQUEEZE (entry): tight bands, stoch turning up from <45.
    #
    # "Not yet (significantly) expanding" is checked against the band's
    # own MINIMUM over the last 5 days, with generous headroom (1.6x) —
    # this widens the valid detection window to the realistic few-day
    # zone around the squeeze's tightest point. The original same-day
    # check (today's width vs exactly 5 days ago) meant bb_width
    # contracting and stoch_k turning up rarely lined up on the identical
    # single bar, since stoch_k typically crosses above stoch_d about a
    # day AFTER bb_width has already ticked up from its absolute low —
    # they're lagging indicators tracking the same regime change with
    # slightly different timing, not independent confirmations expected
    # to land on the same bar. Verified via backtest: under the original
    # same-day-only logic, SQUEEZE never fired across 84 simulated
    # stocks with realistic NSE volatility, even when several had a
    # textbook squeeze-then-breakout pattern scripted into the data.
    bb_width_5d_min = float(df["bb_width_pct"].iloc[-5:].min()) if len(df) >= 5 else bb_width
    is_squeeze = (
        bb_width < 3.0
        and bb_width <= bb_width_5d_min * 1.6  # still close to the recent squeeze low
        and stoch_k < 45
        and stoch_k > stoch_d  # turning up
        and atr_predicted_return >= MIN_PREDICTED_RETURN
    )

    # WATCHLIST (forming, not yet confirmed): a deliberately looser tier
    # for stocks that are visibly tightening and turning up, but haven't
    # cleared the strict SQUEEZE bar yet. Diagnostic runs across the full
    # 502-stock Nifty 500 universe showed the closest real candidates
    # consistently sitting around 5-10% BB width when every other SQUEEZE
    # condition (stoch turning up, target return) was already satisfied —
    # this tier surfaces exactly that population, so a stock can be seen
    # "forming" before it actually triggers. WATCHLIST_BB_WIDTH_MAX is
    # set above the strict 3.0% threshold but well below the typical
    # already-blasting range (BLAST stocks in practice run 9-20%+ wide),
    # keeping this tier meaningfully selective rather than just "most of
    # the market." A stock only ever shows here if it does NOT already
    # qualify as SQUEEZE — this is a strictly lower tier, not an
    # alternative path to the same signal.
    WATCHLIST_BB_WIDTH_MAX = 8.0
    is_watchlist = (
        not is_squeeze
        and bb_width < WATCHLIST_BB_WIDTH_MAX
        and stoch_k < 50
        and stoch_k > stoch_d  # turning up, same directional requirement as SQUEEZE
    )

    # BLAST (exit): bands expanded, stoch overbought, price above EMA10
    is_blast = (
        bb_width > bb_width_5d * 1.15  # expanded vs 5 days ago
        and stoch_k > 75
        and price > ema10
    )

    if is_squeeze:
        # target_price: ATR-projected breakout target — the realistic
        # "how far could this move" estimate at the moment of entry,
        # independent of today's compressed range. predicted_return
        # field carries the same ATR-based % for consistency with the
        # MIN_PREDICTED_RETURN gate above.
        return Candidate(
            symbol, price, bb_width, bb_width_5d, stoch_k, stoch_d,
            ema10, ema30, r1, r2, s1, atr_predicted_return, "SQUEEZE",
            entry_price=round(price, 2), entry_date=scan_date,
            target_price=round(atr_target_price, 2), target_return_pct=atr_predicted_return,
        )

    if is_watchlist:
        # Same field shape as SQUEEZE (entry_price/date, target_price)
        # so a stock's watchlist record and its eventual SQUEEZE record
        # are directly comparable — the frontend uses this to show
        # "first spotted forming on {date} at {price}" when a stock
        # later graduates to a real entry signal.
        return Candidate(
            symbol, price, bb_width, bb_width_5d, stoch_k, stoch_d,
            ema10, ema30, r1, r2, s1, atr_predicted_return, "WATCHLIST",
            entry_price=round(price, 2), entry_date=scan_date,
            target_price=round(atr_target_price, 2), target_return_pct=atr_predicted_return,
        )

    if is_blast:
        blast_entry_price, blast_entry_date, _exit_date_unused, holding_period_days = find_blast_entry(df)
        if blast_entry_price and blast_entry_price > 0:
            return_since_entry = round((price - blast_entry_price) / blast_entry_price * 100, 2)
        else:
            # Fallback if we couldn't pin down a clean squeeze-start bar —
            # use EMA30 as a rough proxy so the field is never just blank.
            blast_entry_price = ema30
            blast_entry_date = None
            holding_period_days = None
            return_since_entry = round((price - ema30) / ema30 * 100, 2) if ema30 else 0
        return Candidate(
            symbol, price, bb_width, bb_width_5d, stoch_k, stoch_d,
            ema10, ema30, r1, r2, s1, return_since_entry, "BLAST",
            blast_entry_price=round(blast_entry_price, 2) if blast_entry_price else None,
            blast_entry_date=blast_entry_date,
            exit_price=round(price, 2), exit_date=scan_date,
            holding_period_days=holding_period_days,
            return_since_entry=return_since_entry,
        )
    return None


# ── Claude call: ONLY for the shortlist, ONLY for reasoning/ranking ──────
def get_claude_commentary(candidates):
    """
    Single, cheap Claude call: no web_search tool, no raw data fetch.
    Input is ~10 rows of pre-computed numbers (a few hundred tokens).
    Output is short reasoning text + a confidence rank per stock.
    This is the ONLY Claude API spend in the whole pipeline.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        # Allow the pipeline to run (and commit data) even without a key,
        # falling back to rule-based confidence so CI doesn't hard-fail.
        return {"items": {c.symbol: _fallback_commentary(c) for c in candidates}, "market_mood": "NEUTRAL"}

    payload_rows = "\n".join(
        f"{c.symbol}: price={c.price}, bb_width={c.bb_width}%, bb_width_5d_ago={c.bb_width_5d_ago}%, "
        f"stoch_k={c.stoch_k}, stoch_d={c.stoch_d}, ema10={c.ema10}, ema30={c.ema30}, "
        f"R1={c.pivot_r1}, R2={c.pivot_r2}, S1={c.pivot_s1}, predicted_return={c.predicted_return}%, "
        f"setup={c.setup}"
        + (f", entry_price={c.entry_price} (on {c.entry_date}), exit_date={c.exit_date}, "
           f"holding_period_days={c.holding_period_days}, return_since_entry={c.return_since_entry}%"
           if c.setup == "BLAST" else "")
        for c in candidates
    )

    prompt = f"""You are ranking pre-screened NSE stock setups. All technical filtering
(Bollinger Band squeeze detection, Stochastic RSI, EMA crossover, pivot
levels) has ALREADY been done with hard numeric thresholds. Do NOT
second-guess the math. Your job is ONLY to:
1. Assign confidence "HIGH" or "MEDIUM" based on how cleanly each setup
   meets the criteria (tighter BB + stronger stoch turn = HIGH)
2. Write one concise 12-word reason per stock referencing the actual numbers.
   For BLAST stocks, reference the return_since_entry and holding_period_days
   figures (e.g. "up 8.2% over 6 trading days since squeeze entry") since
   that's the realized performance so far.
3. Give an overall one-line market_mood (BULLISH/NEUTRAL/BEARISH) based on
   how many squeeze vs blast setups are present

Data:
{payload_rows}

Return ONLY this JSON, no markdown:
{{"items": {{"SYMBOL": {{"confidence": "HIGH|MEDIUM", "reason": "..."}}, ...}}, "market_mood": "..."}}"""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 800,  # small — no search tool, no raw data
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        text = "".join(b.get("text", "") for b in resp.json().get("content", []))
        clean = text.strip().strip("`")
        if clean.startswith("json"):
            clean = clean[4:]
        result = json.loads(clean)
        return result
    except Exception as e:
        print(f"Claude commentary call failed, using fallback: {e}")
        return {"items": {c.symbol: _fallback_commentary(c) for c in candidates}, "market_mood": "NEUTRAL"}


def _fallback_commentary(c: Candidate) -> dict:
    if c.setup == "SQUEEZE":
        conf = "HIGH" if c.bb_width < 1.8 and c.predicted_return >= 6 else "MEDIUM"
        reason = f"BB width {c.bb_width}%, Stoch K/D turning up at {c.stoch_k:.0f}, target R1 {c.pivot_r1:.1f}"
    else:
        conf = "HIGH" if c.stoch_k > 85 else "MEDIUM"
        ret = c.return_since_entry if c.return_since_entry is not None else c.predicted_return
        hold = f" over {c.holding_period_days}d" if c.holding_period_days is not None else ""
        reason = f"Up {ret:.1f}%{hold} since entry at {c.entry_price}, stoch_k {c.stoch_k:.0f} overbought"
    return {"confidence": conf, "reason": reason}


# ── Persistent position ledger ──────────────────────────────────────────────
# Problem this solves: a plain per-scan snapshot only shows stocks that meet
# the narrow numeric thresholds *today*. A stock flagged SQUEEZE yesterday
# can drift just outside the tight "BB width < 3% and still contracting"
# condition today (often *because* the move is starting — the whole point)
# and silently vanish from the dashboard with no record it was ever flagged.
# That's wrong for a tool meant to track "I told you to watch this until I
# tell you to exit." This ledger makes that lifecycle explicit and durable
# across scans, independent of the per-scan classify() snapshot.
LEDGER_PATH = "data/active_positions.json"
BLAST_RETENTION_DAYS = 10  # keep an exit signal visible for this many days, then drop it
WATCHLIST_RETENTION_DAYS = 7  # drop a forming-but-unconfirmed watchlist entry after this many days


def load_ledger() -> dict:
    if os.path.exists(LEDGER_PATH):
        try:
            with open(LEDGER_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_ledger(ledger: dict):
    os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
    with open(LEDGER_PATH, "w") as f:
        json.dump(ledger, f, indent=2)


def update_ledger(ledger: dict, all_candidates: list, scan_date: str) -> dict:
    """
    Merge this scan's fresh classify() results into the durable ledger.

    Rules:
    - New SQUEEZE detection -> add to ledger as SQUEEZE, record first_seen.
    - Existing SQUEEZE entry, still SQUEEZE today -> refresh its numbers.
    - Existing SQUEEZE entry, no longer meets SQUEEZE numerically today but
      hasn't hit BLAST either -> KEEP as SQUEEZE, just don't refresh numbers
      (the position is still considered "open" until an actual exit fires).
    - Existing SQUEEZE entry that now meets BLAST -> flip to BLAST, record
      blast_triggered_date.
    - New BLAST detection with no prior SQUEEZE on record (e.g. first scan
      after deploying this feature, or a move that started before tracking
      began) -> add directly as BLAST so it's not silently dropped either.
    - Existing BLAST entry -> refresh numbers each scan while still
      numerically BLAST; once it's been BLAST_RETENTION_DAYS since
      blast_triggered_date, drop it from the ledger entirely.
    """
    today = pd.to_datetime(scan_date)
    fresh_by_symbol = {c.symbol: c for c in all_candidates}

    # 1. Update/insert from this scan's fresh results
    for symbol, cand in fresh_by_symbol.items():
        existing = ledger.get(symbol)
        cand_dict = asdict(cand)
        cand_dict["last_seen"] = scan_date

        if cand.setup == "SQUEEZE":
            if existing and existing.get("status") == "BLAST":
                # Already exited and currently in its retention window —
                # don't let a fresh SQUEEZE read resurrect/overwrite that;
                # leave the BLAST record alone, it'll age out on its own.
                continue
            # Promotion tracking: if this stock was previously sitting on
            # the WATCHLIST tier and has now genuinely earned a SQUEEZE
            # signal, preserve when it was first spotted forming — this
            # is the "graduated from watchlist" case the dashboard
            # highlights, since a stock that was visibly tightening
            # before it confirmed is a stronger, more legible setup than
            # one that appears with no lead-up.
            was_watchlist = existing and existing.get("status") == "WATCHLIST"
            cand_dict["status"] = "SQUEEZE"
            cand_dict["first_seen"] = existing.get("first_seen", scan_date) if existing else scan_date
            if was_watchlist:
                cand_dict["watchlist_first_seen"] = existing.get("watchlist_first_seen", existing.get("first_seen"))
                cand_dict["watchlist_first_price"] = existing.get("watchlist_first_price", existing.get("price"))
                cand_dict["promoted_from_watchlist"] = True
                cand_dict["promoted_date"] = scan_date
            elif existing and existing.get("promoted_from_watchlist"):
                # already-promoted stock, still SQUEEZE on a later scan —
                # keep carrying the promotion record forward
                cand_dict["watchlist_first_seen"] = existing.get("watchlist_first_seen")
                cand_dict["watchlist_first_price"] = existing.get("watchlist_first_price")
                cand_dict["promoted_from_watchlist"] = True
                cand_dict["promoted_date"] = existing.get("promoted_date")
            ledger[symbol] = cand_dict

        elif cand.setup == "WATCHLIST":
            if existing and existing.get("status") in ("SQUEEZE", "BLAST"):
                # Never downgrade — a stock that already earned SQUEEZE or
                # is mid-BLAST stays there; a softer WATCHLIST read on a
                # later scan doesn't overwrite real progress.
                continue
            cand_dict["status"] = "WATCHLIST"
            if not existing:
                cand_dict["first_seen"] = scan_date
                cand_dict["watchlist_first_seen"] = scan_date
                cand_dict["watchlist_first_price"] = cand_dict["price"]
            else:
                # Still forming — keep the original watchlist spot date/
                # price fixed, refresh current numbers
                cand_dict["first_seen"] = existing.get("first_seen", scan_date)
                cand_dict["watchlist_first_seen"] = existing.get("watchlist_first_seen", scan_date)
                cand_dict["watchlist_first_price"] = existing.get("watchlist_first_price", cand_dict["price"])
            ledger[symbol] = cand_dict

        elif cand.setup == "BLAST":
            cand_dict["status"] = "BLAST"
            if existing and existing.get("status") == "BLAST":
                # Still blasting — refresh price/exit_date/numbers each
                # scan, but keep the ORIGINAL blast_entry_price/date and
                # the original trigger date fixed from when this exit
                # signal first fired, not today's find_blast_entry() guess.
                cand_dict["first_seen"] = existing.get("first_seen", scan_date)
                cand_dict["blast_triggered_date"] = existing.get("blast_triggered_date", scan_date)
                cand_dict["blast_entry_price"] = existing.get("blast_entry_price", cand_dict.get("blast_entry_price"))
                cand_dict["blast_entry_date"] = existing.get("blast_entry_date", cand_dict.get("blast_entry_date"))
                # original_squeeze_entry_price/date: if this stock was
                # tracked as a SQUEEZE before flipping, those are already
                # carried on the existing record from the flip below — keep
                # them as-is rather than letting them be overwritten.
                if "original_squeeze_entry_price" in existing:
                    cand_dict["original_squeeze_entry_price"] = existing["original_squeeze_entry_price"]
                    cand_dict["original_squeeze_entry_date"] = existing["original_squeeze_entry_date"]
            else:
                # Either was SQUEEZE and just flipped, or appearing fresh
                cand_dict["first_seen"] = existing.get("first_seen", scan_date) if existing else scan_date
                cand_dict["blast_triggered_date"] = scan_date
                if existing and existing.get("status") == "SQUEEZE":
                    # Preserve the original entry signal (what you'd have
                    # bought at) distinct from blast_entry_price (the
                    # squeeze-end price found via BB-width lookback, which
                    # can differ slightly from the exact day the dashboard
                    # first flagged it).
                    cand_dict["original_squeeze_entry_price"] = existing.get("entry_price")
                    cand_dict["original_squeeze_entry_date"] = existing.get("entry_date")
            ledger[symbol] = cand_dict

    # 2. Anything in the ledger NOT in today's fresh results: leave SQUEEZE
    #    entries untouched (per your instruction — stays visible until an
    #    actual exit fires), age out BLAST entries past retention, and age
    #    out WATCHLIST entries that stopped forming (didn't promote, didn't
    #    stay tight) — unlike SQUEEZE, a WATCHLIST entry was never a
    #    confirmed signal, so there's no "open position" obligation to keep
    #    showing it indefinitely once it's no longer actually forming.
    to_drop = []
    for symbol, entry in ledger.items():
        if symbol in fresh_by_symbol:
            continue  # already handled above
        if entry.get("status") == "BLAST":
            trigger_date = entry.get("blast_triggered_date")
            if trigger_date:
                age_days = (today - pd.to_datetime(trigger_date)).days
                if age_days >= BLAST_RETENTION_DAYS:
                    to_drop.append(symbol)
        elif entry.get("status") == "WATCHLIST":
            spotted_date = entry.get("watchlist_first_seen") or entry.get("first_seen")
            if spotted_date:
                age_days = (today - pd.to_datetime(spotted_date)).days
                if age_days >= WATCHLIST_RETENTION_DAYS:
                    to_drop.append(symbol)
        # SQUEEZE entries not refreshed today are intentionally left as-is.

    for symbol in to_drop:
        del ledger[symbol]

    return ledger


def scan_all_symbols(session: requests.Session, universe: list, as_of_date: str = None, histories: dict = None):
    """
    Run classify() across the given symbol universe. If as_of_date is
    given, each symbol's price history is truncated to bars on or before
    that date before classifying — this lets a backfill run reconstruct
    what a scan would have shown on a past trading day, using the same
    classify() logic as a live run. histories, if provided, is a
    {symbol: DataFrame} cache to avoid re-fetching from NSE for every
    backfilled day (fetch once, slice many times).
    """
    all_candidates = []
    fetched_histories = histories if histories is not None else {}

    for symbol in universe:
        if symbol in fetched_histories:
            df = fetched_histories[symbol]
        else:
            df = fetch_history(session, symbol)
            if histories is not None:
                fetched_histories[symbol] = df
            time.sleep(0.25)  # be polite to NSE — only matters on real fetches

        if df is None:
            continue

        df_slice = df
        if as_of_date:
            cutoff = pd.to_datetime(as_of_date)
            df_slice = df[pd.to_datetime(df["date"]) <= cutoff]
            if len(df_slice) < 35:
                continue

        df_ind = add_indicators(df_slice)
        cand = classify(symbol, df_ind, scan_date=as_of_date)
        if cand:
            all_candidates.append(cand)

    return all_candidates, fetched_histories


def run(backfill_days: int = 0, allow_weekend: bool = False):
    session = get_nse_session()

    today = datetime.date.today()
    is_open, reason = is_trading_day(today, session)
    print(f"Market day check for {today.isoformat()}: {'OPEN' if is_open else 'CLOSED'} — {reason}")

    if not is_open and not allow_weekend and backfill_days == 0:
        # Write a lightweight status file so the frontend can show
        # "market closed today" instead of silently keeping stale data
        # with no explanation. No NSE data fetch, no Claude call — zero
        # cost on non-trading days.
        os.makedirs("data", exist_ok=True)
        status_path = "data/market_status.json"
        with open(status_path, "w") as f:
            json.dump({
                "date": today.isoformat(),
                "market_open": False,
                "reason": reason,
                "checked_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M IST"),
            }, f, indent=2)
        print(f"Skipping scan — {reason}. No data fetched, no Claude API call made.")
        return

    # Record today's market-open status regardless of backfill/weekend
    # override, so the frontend status always reflects the real calendar.
    os.makedirs("data", exist_ok=True)
    with open("data/market_status.json", "w") as f:
        json.dump({
            "date": today.isoformat(),
            "market_open": is_open,
            "reason": reason,
            "checked_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M IST"),
        }, f, indent=2)

    # Fetch the live Nifty 500 universe once per run (falls back to the
    # smaller hardcoded list if NSE's CSV is unreachable). Widening from
    # ~84 to ~500 stocks directly improves SQUEEZE detection odds, since
    # only a small fraction of any universe sits in a tight consolidation
    # on a given day.
    universe = fetch_nifty500_universe(session)

    ledger = load_ledger()

    if backfill_days > 0:
        # Reconstruct the last N trading days sequentially, feeding each
        # day's classify() results into update_ledger() in chronological
        # order — exactly as if a normal scan had run on each of those
        # days. Fetch each symbol's history once, slice per day, to avoid
        # N x the NSE API load.
        print(f"Backfill mode: reconstructing the last {backfill_days} trading day(s)...")
        histories = {}
        candidate_dates = []
        check_date = today
        while len(candidate_dates) < backfill_days and (today - check_date).days < 14:
            check_date -= datetime.timedelta(days=1)
            is_open_check, _ = is_trading_day(check_date, session)
            if is_open_check:
                candidate_dates.append(check_date)
        candidate_dates.reverse()  # chronological order, oldest first

        last_candidates = []
        for d in candidate_dates:
            d_str = d.isoformat()
            print(f"  Reconstructing {d_str}...")
            day_candidates, histories = scan_all_symbols(session, universe, as_of_date=d_str, histories=histories)
            ledger = update_ledger(ledger, day_candidates, d_str)
            last_candidates = day_candidates
        all_candidates = last_candidates
        scan_date = candidate_dates[-1].isoformat() if candidate_dates else today.isoformat()
    else:
        scan_date = today.isoformat()
        all_candidates, _ = scan_all_symbols(session, universe, as_of_date=scan_date)
        ledger = update_ledger(ledger, all_candidates, scan_date)


    # Build the dashboard view FROM THE LEDGER, not just today's fresh
    # candidates — this is what makes SQUEEZE entries persist until a real
    # exit fires, and BLAST entries stay visible for their retention window.
    ledger_squeeze = [v for v in ledger.values() if v.get("status") == "SQUEEZE"]
    ledger_blast = [v for v in ledger.values() if v.get("status") == "BLAST"]
    ledger_watchlist = [v for v in ledger.values() if v.get("status") == "WATCHLIST"]

    # SQUEEZE: show EVERY open position, not a capped top-N. The whole
    # point of the ledger is "you can't have an exit without an entry" —
    # any stock that triggered an entry signal and hasn't hit a real
    # BLAST/exit yet is still an open, live position and must stay on
    # the dashboard regardless of how many there are. Capping this list
    # would silently hide genuinely open positions, which defeats the
    # purpose of tracking them at all. Sorted by predicted return only
    # for display ordering (best opportunities first), not to exclude
    # anything.
    squeeze_sorted = sorted(
        ledger_squeeze, key=lambda v: v.get("predicted_return", 0) or 0, reverse=True
    )

    # BLAST: still capped, since this list is naturally self-bounding by
    # the 10-day retention window (BLAST_RETENTION_DAYS) rather than by
    # an arbitrary count, and a cap here just keeps the "best/most
    # recent exit signals" view focused. Raised from 4 to 10 so it's
    # less likely to hide a real signal while the SQUEEZE-side fix is
    # the priority — adjust BLAST_RETENTION_DAYS if you want a tighter
    # or looser natural bound instead of a count-based cap.
    # BLAST: uncapped — every open exit signal is shown. Naturally
    # self-bounding by BLAST_RETENTION_DAYS (10-day auto-drop).
    blast_sorted = sorted(
        ledger_blast,
        key=lambda v: (v.get("return_since_entry") if v.get("return_since_entry") is not None else v.get("stoch_k", 0)) or 0,
        reverse=True
    )

    # WATCHLIST: uncapped — show all forming setups, sorted by BB width
    # ascending (tightest = closest to a real signal, shown first).
    watchlist_sorted = sorted(
        ledger_watchlist, key=lambda v: v.get("bb_width", 999) or 999
    )

    # Only ask Claude about entries that don't already have stored
    # commentary from a previous scan — keeps the API call minimal even
    # though the ledger can now hold more symbols across multiple days.
    def needs_commentary(entry):
        return not entry.get("reason")

    field_names = set(Candidate.__dataclass_fields__.keys())

    def dict_to_candidate(v: dict):
        kwargs = {k: v.get(k) for k in field_names if k in v}
        try:
            return Candidate(**kwargs)
        except TypeError:
            return None

    shortlist_dicts = [v for v in (squeeze_sorted + blast_sorted) if needs_commentary(v)]
    # Cap how many NEW (never-commented) entries get sent to Claude in one
    # call, purely as a cost-control safety net — this is independent of
    # the dashboard display, which still shows every open SQUEEZE/BLAST
    # position regardless of this cap. Entries beyond this cap simply get
    # the free, local _fallback_commentary() reasoning instead of a
    # Claude-generated one for this scan; they'll get genuine Claude
    # commentary on a later scan once the backlog clears (each needs it
    # only once, thanks to needs_commentary() re-use).
    MAX_NEW_COMMENTARY_PER_SCAN = 15
    shortlist_dicts = shortlist_dicts[:MAX_NEW_COMMENTARY_PER_SCAN]
    shortlist_candidates = [c for c in (dict_to_candidate(v) for v in shortlist_dicts) if c is not None]

    commentary = get_claude_commentary(shortlist_candidates) if shortlist_candidates else {"items": {}, "market_mood": None}
    items = commentary.get("items", commentary)

    def apply_commentary(entry: dict) -> dict:
        entry = dict(entry)
        symbol = entry["symbol"]
        if symbol in items:
            extra = items[symbol]
            entry["confidence"] = extra.get("confidence", entry.get("confidence", "MEDIUM"))
            entry["reason"] = extra.get("reason", entry.get("reason", ""))
        elif not entry.get("reason"):
            cand = dict_to_candidate(entry)
            if cand:
                fb = _fallback_commentary(cand)
                entry["confidence"] = fb["confidence"]
                entry["reason"] = fb["reason"]
        # persist the (possibly updated) commentary back into the ledger
        # entry so future scans can re-use it without re-asking Claude
        if symbol in ledger:
            ledger[symbol]["confidence"] = entry.get("confidence")
            ledger[symbol]["reason"] = entry.get("reason")
        return entry

    squeeze_sorted = [apply_commentary(v) for v in squeeze_sorted]
    blast_sorted = [apply_commentary(v) for v in blast_sorted]

    # WATCHLIST gets free, rule-based reasons only — no Claude call, by
    # design, since this is a high-volume "keep an eye on it" tier rather
    # than a confirmed signal worth spending API budget on.
    def watchlist_reason(entry: dict) -> str:
        bbw = entry.get("bb_width", 0) or 0
        sk = entry.get("stoch_k", 0) or 0
        return f"BB width {bbw}% tightening, stoch_k {sk:.0f} turning up — not yet confirmed (needs <3.0% width)"

    for v in watchlist_sorted:
        if not v.get("reason"):
            v["reason"] = watchlist_reason(v)
        v["confidence"] = "WATCHING"

    save_ledger(ledger)  # persist commentary + lifecycle state for next scan

    # Overall market mood: only Claude-generated when we actually called
    # Claude this run; otherwise derive a simple rule-based mood so we don't
    # show a stale/misleading label on calls that skipped the API entirely.
    market_mood = commentary.get("market_mood")
    if not market_mood:
        if len(ledger_squeeze) > len(ledger_blast):
            market_mood = "BULLISH"
        elif len(ledger_blast) > len(ledger_squeeze):
            market_mood = "NEUTRAL"
        else:
            market_mood = "NEUTRAL"

    # Count how many of today's SQUEEZE entries are fresh graduations
    # from the watchlist — directly answers "is the watchlist actually
    # producing real hits."
    promoted_today = [v for v in squeeze_sorted if v.get("promoted_date") == scan_date]

    output = {
        "scan_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M IST"),
        "market_mood": market_mood,
        "squeeze_stocks": squeeze_sorted,
        "blast_stocks": blast_sorted,
        "watchlist_stocks": watchlist_sorted,
        "scanned_count": len(universe),
        "candidates_found": len(all_candidates),
        "ledger_squeeze_total": len(ledger_squeeze),
        "ledger_blast_total": len(ledger_blast),
        "ledger_watchlist_total": len(ledger_watchlist),
        "promoted_from_watchlist_today": len(promoted_today),
    }

    os.makedirs("data", exist_ok=True)
    with open("data/scan_latest.json", "w") as f:
        json.dump(output, f, indent=2)

    # Keep a dated history for backtesting prediction accuracy later
    os.makedirs("data/history", exist_ok=True)
    hist_path = f"data/history/scan_{datetime.date.today().isoformat()}.json"
    with open(hist_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"Scan complete: {len(squeeze_sorted)} squeeze shown (ledger total {len(ledger_squeeze)}), "
          f"{len(blast_sorted)} blast shown (ledger total {len(ledger_blast)}), "
          f"{len(watchlist_sorted)} watchlist shown (ledger total {len(ledger_watchlist)}). "
          f"{len(promoted_today)} promoted from watchlist today. "
          f"Claude called for {len(shortlist_candidates)} new/changed entries.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="NSE Squeeze & Blast Scanner")
    parser.add_argument(
        "--backfill-days", type=int, default=0,
        help="Reconstruct the last N trading days into the ledger (e.g. "
             "--backfill-days 2 on a weekend to see what Thu/Fri would "
             "have shown). Ignores the normal market-day skip check."
    )
    parser.add_argument(
        "--allow-weekend", action="store_true",
        help="Run a normal (non-backfill) scan even if today is not a "
             "trading day. Useful for testing; not for routine use since "
             "it spends a Claude API call against data that won't have "
             "moved since the last real trading day."
    )
    parser.add_argument(
        "--diagnose", action="store_true",
        help="Print a full near-miss breakdown for every stock in the "
             "universe (which of the 5 SQUEEZE conditions pass/fail and "
             "by how much) instead of running a normal scan. Free — does "
             "NOT call Claude or write to the ledger/dashboard. Useful "
             "for understanding 'how close' stocks are to a real signal "
             "when the strict scan comes back empty."
    )
    args = parser.parse_args()

    # Explicit, unambiguous confirmation of which mode actually ran — this
    # exists because of a real incident where the GitHub Actions UI's
    # diagnose toggle didn't pass through correctly and the script silently
    # ran a normal scan instead, which wasn't obvious from the log output
    # at the time. This line removes all doubt going forward.
    mode = "DIAGNOSE" if args.diagnose else (f"BACKFILL({args.backfill_days}d)" if args.backfill_days > 0 else "NORMAL SCAN")
    print(f"=== MODE: {mode} (diagnose={args.diagnose}, backfill_days={args.backfill_days}, allow_weekend={args.allow_weekend}) ===\n")

    if args.diagnose:
        sess = get_nse_session()
        diag_universe = fetch_nifty500_universe(sess)
        print(f"Diagnosing all {len(diag_universe)} symbols (no Claude call, no ledger write)...\n")
        results = []
        for sym in diag_universe:
            hist = fetch_history(sess, sym)
            if hist is None:
                continue
            hist_ind = add_indicators(hist)
            d = diagnose(sym, hist_ind)
            if "checks" in d:
                results.append(d)
            time.sleep(0.2)

        results.sort(key=lambda r: (r["checks_passed"], r["atr_target_return_pct"]), reverse=True)
        watchlist_count = sum(1 for r in results if r.get("would_be_watchlist"))
        squeeze_count = sum(1 for r in results if r["would_be_squeeze"])
        print(f"Would-be SQUEEZE: {squeeze_count} | Would-be WATCHLIST: {watchlist_count}\n")
        print(f"{'Symbol':14} {'Passed':8} {'Price':>10} {'BBwidth':>8} {'StochK':>7} {'StochD':>7} {'ATR target':>11}")
        for r in results[:25]:
            flag = " <-- SQUEEZE" if r["would_be_squeeze"] else (" <-- watchlist" if r.get("would_be_watchlist") else "")
            print(f"{r['symbol']:14} {r['checks_passed']:8} {r['price']:10.2f} {r['bb_width']:8.2f} "
                  f"{r['stoch_k']:7.1f} {r['stoch_d']:7.1f} {r['atr_target_return_pct']:10.2f}%{flag}")
    else:
        run(backfill_days=args.backfill_days, allow_weekend=args.allow_weekend)
