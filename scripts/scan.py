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
NIFTY_500_SAMPLE = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "SBIN", "AXISBANK",
    "KOTAKBANK", "BAJFINANCE", "BHARTIARTL", "ITC", "LT", "MARUTI", "TITAN",
    "SUNPHARMA", "ULTRACEMCO", "NTPC", "POWERGRID", "TATASTEEL", "TATAMOTORS",
    "IDFCFIRSTB", "BANKBARODA", "PNB", "CANBK", "FEDERALBNK", "INDUSINDBK",
    "ADANIPORTS", "ADANIENT", "JSWSTEEL", "HINDALCO", "COALINDIA", "ONGC",
    "BPCL", "GRASIM", "DRREDDY", "CIPLA", "DIVISLAB", "APOLLOHOSP",
    "BAJAJFINSV", "HDFCLIFE", "SBILIFE", "HEROMOTOCO", "EICHERMOT", "TVSMOTOR",
    "M&M", "WIPRO", "TECHM", "HCLTECH", "LTIM", "ASIANPAINT", "NESTLEIND",
    "BRITANNIA", "DABUR", "GODREJCP", "PIDILITIND", "SIEMENS", "ABB",
    "HAVELLS", "DLF", "GODREJPROP", "VEDANTA", "HINDZINC", "AMBUJACEM",
    "ACC", "SHREECEM", "IDEA", "ZOMATO", "PAYTM", "NYKAA", "TRENT",
    "VOLTAS", "PERSISTENT", "MPHASIS", "LUPIN", "AUROPHARMA", "BIOCON",
    "PFC", "RECLTD", "IRFC", "IRCTC", "INDIGO", "BEL", "HAL", "MAZDOCK",
]

NSE_BASE = "https://www.nseindia.com"
NSE_API_HIST = "https://www.nseindia.com/api/historical/cm/equity?symbol={symbol}&series=[%22EQ%22]&from={frm}&to={to}"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

MIN_PREDICTED_RETURN = 5.0  # % — hard floor for squeeze candidates


# ── NSE session (cookies required) ───────────────────────────────────────
def get_nse_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
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
    df["bb_mid"] = df["close"].rolling(20).mean()
    df["bb_std"] = df["close"].rolling(20).std()
    df["bb_upper"] = df["bb_mid"] + 2 * df["bb_std"]
    df["bb_lower"] = df["bb_mid"] - 2 * df["bb_std"]
    df["bb_width_pct"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"] * 100

    df["ema10"] = df["close"].ewm(span=10, adjust=False).mean()
    df["ema30"] = df["close"].ewm(span=30, adjust=False).mean()

    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    rsi_min = df["rsi"].rolling(14).min()
    rsi_max = df["rsi"].rolling(14).max()
    stoch = (df["rsi"] - rsi_min) / (rsi_max - rsi_min).replace(0, np.nan) * 100
    df["stoch_k"] = stoch.rolling(3).mean()
    df["stoch_d"] = df["stoch_k"].rolling(3).mean()

    df["pivot"] = (df["high"].shift(1) + df["low"].shift(1) + df["close"].shift(1)) / 3
    df["r1"] = 2 * df["pivot"] - df["low"].shift(1)
    df["r2"] = df["pivot"] + (df["high"].shift(1) - df["low"].shift(1))
    df["s1"] = 2 * df["pivot"] - df["high"].shift(1)
    df["s2"] = df["pivot"] - (df["high"].shift(1) - df["low"].shift(1))

    return df


@dataclass
class Candidate:
    symbol: str
    price: float
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


def classify(symbol: str, df: pd.DataFrame):
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

    predicted_return = round((r1 - price) / price * 100, 2) if price else 0

    is_squeeze = (
        bb_width < 3.0
        and bb_width <= bb_width_5d * 1.05
        and stoch_k < 45
        and stoch_k > stoch_d
        and predicted_return >= MIN_PREDICTED_RETURN
    )

    is_blast = (
        bb_width > bb_width_5d * 1.15
        and stoch_k > 75
        and price > ema10
    )

    if is_squeeze:
        return Candidate(symbol, price, bb_width, bb_width_5d, stoch_k, stoch_d,
                          ema10, ema30, r1, r2, s1, predicted_return, "SQUEEZE")
    if is_blast:
        gain_proxy = round((price - ema30) / ema30 * 100, 2) if ema30 else 0
        return Candidate(symbol, price, bb_width, bb_width_5d, stoch_k, stoch_d,
                          ema10, ema30, r1, r2, s1, gain_proxy, "BLAST")
    return None


# ── Claude call: ONLY for the shortlist, ONLY for reasoning/ranking ──────
def get_claude_commentary(candidates):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {"items": {c.symbol: _fallback_commentary(c) for c in candidates}, "market_mood": "NEUTRAL"}

    payload_rows = "\n".join(
        f"{c.symbol}: price={c.price}, bb_width={c.bb_width}%, bb_width_5d_ago={c.bb_width_5d_ago}%, "
        f"stoch_k={c.stoch_k}, stoch_d={c.stoch_d}, ema10={c.ema10}, ema30={c.ema30}, "
        f"R1={c.pivot_r1}, R2={c.pivot_r2}, S1={c.pivot_s1}, predicted_return={c.predicted_return}%, "
        f"setup={c.setup}"
        for c in candidates
    )

    prompt = f"""You are ranking pre-screened NSE stock setups. All technical filtering
(Bollinger Band squeeze detection, Stochastic RSI, EMA crossover, pivot
levels) has ALREADY been done with hard numeric thresholds. Do NOT
second-guess the math. Your job is ONLY to:
1. Assign confidence "HIGH" or "MEDIUM" based on how cleanly each setup
   meets the criteria (tighter BB + stronger stoch turn = HIGH)
2. Write one concise 12-word reason per stock referencing the actual numbers
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
                "max_tokens": 800,
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
        reason = f"Stoch K at {c.stoch_k:.0f} overbought, BB expanded {c.bb_width:.1f}% vs {c.bb_width_5d_ago:.1f}%"
    return {"confidence": conf, "reason": reason}


# ── Main pipeline ──────────────────────────────────────────────────────────
def run():
    session = get_nse_session()
    all_candidates = []

    for symbol in NIFTY_500_SAMPLE:
        df = fetch_history(session, symbol)
        if df is None:
            continue
        df = add_indicators(df)
        cand = classify(symbol, df)
        if cand:
            all_candidates.append(cand)
        time.sleep(0.25)

    squeeze_sorted = sorted(
        [c for c in all_candidates if c.setup == "SQUEEZE"],
        key=lambda c: c.predicted_return, reverse=True
    )[:6]
    blast_sorted = sorted(
        [c for c in all_candidates if c.setup == "BLAST"],
        key=lambda c: c.stoch_k, reverse=True
    )[:4]
    shortlist = squeeze_sorted + blast_sorted

    commentary = get_claude_commentary(shortlist) if shortlist else {"items": {}, "market_mood": "NEUTRAL"}
    items = commentary.get("items", commentary)

    def to_dict(c: Candidate) -> dict:
        extra = items.get(c.symbol, _fallback_commentary(c))
        base = asdict(c)
        base["confidence"] = extra.get("confidence", "MEDIUM")
        base["reason"] = extra.get("reason", "")
        return base

    output = {
        "scan_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M IST"),
        "market_mood": commentary.get("market_mood", "NEUTRAL"),
        "squeeze_stocks": [to_dict(c) for c in squeeze_sorted],
        "blast_stocks": [to_dict(c) for c in blast_sorted],
        "scanned_count": len(NIFTY_500_SAMPLE),
        "candidates_found": len(all_candidates),
    }

    os.makedirs("data", exist_ok=True)
    with open("data/scan_latest.json", "w") as f:
        json.dump(output, f, indent=2)

    os.makedirs("data/history", exist_ok=True)
    hist_path = f"data/history/scan_{datetime.date.today().isoformat()}.json"
    with open(hist_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"Scan complete: {len(squeeze_sorted)} squeeze, {len(blast_sorted)} blast candidates written.")


if __name__ == "__main__":
    run()
