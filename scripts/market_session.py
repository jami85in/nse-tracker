#!/usr/bin/env python3
"""
market_session.py — when is NSE actually trading today?

One place for the trading calendar, used by prices_kite.py and the price-loop
workflow. Normal days are Monday–Friday 09:15–15:30 IST. Exchange special
sessions on other days (Diwali Muhurat trading, budget-day Saturdays, mock or
special live sessions) go in SPECIAL_SESSIONS; weekday exchange holidays go in
HOLIDAYS.

Why this exists: on Sunday 8 Nov 2026 (Muhurat trading, 14:20–15:30) the Kite
price loop started fine after the daily login, but prices_kite.py treated
every Saturday/Sunday as closed, so it fetched nothing and the dashboard kept
Friday's prices.

Add a line to SPECIAL_SESSIONS whenever NSE announces one. If a session is
missed, the price workflow can also be run by hand with force=true.

CLI (used by the workflow):
  python scripts/market_session.py end   -> epoch seconds the price loop should
                                             stop at today (session close + 5 min),
                                             or 0 if there is no session today
  python scripts/market_session.py info  -> human-readable summary
"""
import datetime, sys

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
REGULAR = ("09:15", "15:30")

# date: (open, close, label) — equity continuous-trading window, IST
SPECIAL_SESSIONS = {
    "2026-11-08": ("14:20", "15:30", "Diwali Muhurat trading (Samvat 2083)"),
}

# Weekday exchange holidays (NSE equity segment)
HOLIDAYS = {
    "2026-11-10": "Diwali Balipratipada",
    "2026-11-24": "Guru Nanak Jayanti",
    "2026-12-25": "Christmas",
}


def _hm(day, hm):
    h, m = map(int, hm.split(":"))
    return datetime.datetime(day.year, day.month, day.day, h, m, tzinfo=IST)


def session(day=None):
    """(open_dt, close_dt, label) for `day` (IST date), or None if NSE is closed."""
    day = day or datetime.datetime.now(IST).date()
    key = day.isoformat()
    if key in SPECIAL_SESSIONS:
        o, c, label = SPECIAL_SESSIONS[key]
        return _hm(day, o), _hm(day, c), label
    if day.weekday() >= 5 or key in HOLIDAYS:
        return None
    return _hm(day, REGULAR[0]), _hm(day, REGULAR[1]), "regular session"


def is_open(now=None):
    now = now or datetime.datetime.now(IST)
    s = session(now.date())
    return bool(s) and s[0] <= now <= s[1]


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "info"
    s = session()
    if cmd == "end":
        print(int((s[1] + datetime.timedelta(minutes=5)).timestamp()) if s else 0)
    else:
        print(f"{datetime.datetime.now(IST):%Y-%m-%d %H:%M IST}: "
              + (f"{s[2]} {s[0]:%H:%M}–{s[1]:%H:%M}" if s else "no trading session today"))
