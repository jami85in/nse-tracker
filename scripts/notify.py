#!/usr/bin/env python3
"""
notify.py — buzzes your phone (ntfy app) when something needs action.

Runs every round of the price loop, after prices are fetched. Each alert is
sent ONCE per stock per day (state kept in the private repo, so a relay run
doesn't repeat it). Checks:

  1. Swing levels on recent buys: target reached / exit level reached
     (from each account's private ledger, the "swing" field).
  2. GTT orders in Kite, for each account that logged in today: price within
     1% of a trigger (heads-up), at/through a trigger, or triggered today.
     Regular (non-GTT) orders, which Kite lists for the current day only:
     filled, rejected, or a pending order within 0.5% of its price/trigger.
  3. Cycle dips: a STRONG seasonal-cycle stock that is in its up-season and
     is down 2% or more today: a priority-4 heads-up during the day, and the
     priority-5 "buy at the close" alert from 15:10 IST (the backtest buys at
     the close, so the paper record uses the last price at/after 15:10). Backtest (Sep 2020 - Nov 2026, daily bars):
     78% reached +2% after charges within 5 trading days; misses averaged
     -4.8%. Also logged to the private repo as a paper record, so the idea can
     be judged on new data before real money goes in.

Phone setup: install "ntfy" (Play Store), tap +, subscribe to the topic name
saved as the NTFY_TOPIC secret. Action alerts use priority 5 (sound + long
vibration); heads-ups use priority 4.

PRIVACY: this runs in the PUBLIC repo, so it prints nothing about alerts,
holdings or orders, only failures by type. Notification text travels via
ntfy.sh under the private topic name.
"""
import datetime as dt, json, os, sys, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from private_repo import PrivateRepo

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DIP_PCT = -2.0
DIP_GRADES = ("STRONG",)
GTT_NEAR_PCT = 1.0
COST_PCT = 0.23
ACCOUNTS = [  # (id, label, token file, API-key env var)
    ("huf", "HUF", "kite_token.json", "KITE_API_KEY"),
    ("wife", "Wife", "tokens/wife.json", "KITE_API_KEY_WIFE"),
    ("mother", "Mother", "tokens/mother.json", "KITE_API_KEY_MOTHER"),
]


def rupees(v):
    return f"₹{v:,.2f}"


def send(topic, title, body, priority=5, tags="chart_with_upwards_trend", click=None):
    """Publish with ntfy's JSON API (UTF-8 titles, emoji and ₹ work)."""
    msg = {"topic": topic, "title": title, "message": body, "priority": priority, "tags": [tags]}
    if click:
        msg["click"] = click
    req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(msg).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status


def in_season(s, month):
    since = (month - s["entry"]) % 12
    return 1 <= since <= (s.get("hold") or 0) - 1      # up-leg months, excluding the month it usually ends


def swing_alerts(priv, prices):
    out = []
    accts, _ = priv.get_json("ledger/accounts.json", {"accounts": []})
    for a in (accts or {}).get("accounts", []):
        aid = a.get("id")
        path = "ledger/portfolio_ledger.json" if aid == "huf" else f"ledger/accounts/{aid}/portfolio_ledger.json"
        led, _ = priv.get_json(path, {})
        label = a.get("short") or aid
        for h in (led or {}).get("holdings", []):
            sw, sym = h.get("swing"), h.get("symbol")
            p = prices.get(sym) or prices.get(f"{sym}-SM")
            if not sw or not p:
                continue
            # Keyed by the level and the buy date, not the day: a stock sitting above its target
            # buzzes once, and a later re-buy at the same price gets its own alert.
            # The text carries no level: target/exit are fixed % from the buy price, so
            # printing them would reveal the cost (levels are on the dashboard).
            since = sw.get("since", "")
            if p >= sw["target"]:
                out.append((f"swing-t:{aid}:{sym}:{sw['target']}:{since}", f"🎯 {sym} swing target reached ({label})",
                            f"Now {rupees(p)}. Target and exit are on the dashboard's Swing levels.", 5, "dart"))
            elif sw.get("stop") and p <= sw["stop"]:
                out.append((f"swing-s:{aid}:{sym}:{sw['stop']}:{since}", f"🛑 {sym} at swing exit level ({label})",
                            f"Now {rupees(p)}. Target and exit are on the dashboard's Swing levels.", 5, "warning"))
    return out


def _kite(priv, tok_path, key):
    from kite_token_reader import token_is_stale
    tok, _ = priv.get_json(tok_path, None)
    if not key or not tok or not tok.get("access_token") or token_is_stale(tok):
        return None
    from kiteconnect import KiteConnect
    k = KiteConnect(api_key=key)
    k.set_access_token(tok["access_token"])
    return k


def gtt_alerts(priv, prices, today):
    out, huf = [], None
    for aid, label, tok_path, key_env in ACCOUNTS:
        try:
            kite = _kite(priv, tok_path, os.environ.get(key_env, ""))
            if kite is None:
                continue
            if aid == "huf":
                huf = kite
            allg = kite.get_gtts() or []
            gtts = [g for g in allg if g.get("status") == "active" and g.get("condition")]
            fired = [g for g in allg if g.get("status") == "triggered" and str(g.get("updated_at", ""))[:10] == today]
            slim = [{"id": g.get("id"), "type": g.get("type"), "symbol": g["condition"].get("tradingsymbol"),
                     "exchange": g["condition"].get("exchange"), "triggers": g["condition"].get("trigger_values"),
                     "set_at_price": g["condition"].get("last_price"),
                     "orders": [{"side": o.get("transaction_type"), "qty": o.get("quantity"), "price": o.get("price")}
                                for o in g.get("orders", [])]} for g in gtts]
            gpath = "ledger/gtt.json" if aid == "huf" else f"ledger/accounts/{aid}/gtt.json"
            old, sha = priv.get_json(gpath, None)
            if not old or old.get("gtts") != slim or old.get("date") != today:
                priv.put_json(gpath, {"date": today, "updated_at": dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"),
                                      "gtts": slim}, f"gtt: {aid} {today}", sha)
            for g in fired:
                sym = (g.get("condition") or {}).get("tradingsymbol", "?")
                out.append((f"gtt-fired:{aid}:{g.get('id')}", f"✅ {sym} GTT triggered ({label})",
                            "Kite has placed the order — check it in Kite.", 5, "white_check_mark"))
            # GTTs on stocks the price job doesn't quote: one LTP call with the HUF login
            missing = sorted({f"{g['exchange'] or 'NSE'}:{g['symbol']}" for g in slim
                              if not (prices.get(g["symbol"]) or prices.get(f"{g['symbol']}-SM"))})
            if missing and (huf or aid != "huf"):
                q = huf or _kite(priv, "kite_token.json", os.environ.get("KITE_API_KEY", ""))
                if q is not None:
                    for k_, v in (q.ltp(missing) or {}).items():
                        if v.get("last_price"):
                            prices[k_.split(":", 1)[1]] = float(v["last_price"])
            for g in slim:
                p = prices.get(g["symbol"]) or prices.get(f"{g['symbol']}-SM")
                if not p:
                    continue
                base = g.get("set_at_price")
                trigs = [t_ for t_ in (g["triggers"] or []) if t_]
                for i, trig in enumerate(trigs):
                    o = g["orders"][i] if i < len(g["orders"]) else (g["orders"][0] if g["orders"] else {})
                    what = (o.get("side") or "order").lower()
                    gap = (p / trig - 1) * 100
                    # Two-leg (OCO) GTTs, trailing or not: the lower trigger is the stop-loss and
                    # fires at or below it, the higher one is the target and fires at or above it.
                    # A trailing stop moves up after the GTT is set, so the price at creation can't
                    # give the direction (that sent false "at its GTT trigger" alerts on 23 Nov 2026).
                    # A single GTT fires when the price crosses its trigger from where it was set.
                    if len(trigs) == 2:
                        crossed = p <= trig if trig == min(trigs) else p >= trig
                    else:
                        crossed = base is not None and ((base < trig <= p) or (base > trig >= p))
                    if crossed or abs(gap) <= 0.1:
                        out.append((f"gtt-hit:{aid}:{g['id']}:{i}", f"⚡ {g['symbol']} at its GTT trigger ({label})",
                                    f"Now {rupees(p)} ({what} GTT). Check the order in Kite.", 5, "zap"))
                    elif abs(gap) <= GTT_NEAR_PCT:
                        out.append((f"gtt-near:{aid}:{g['id']}:{i}", f"👀 {g['symbol']} near its GTT ({label})",
                                    f"Now {rupees(p)}, {abs(gap):.1f}% from the trigger ({what}).", 4, "eyes"))
        except Exception as e:                   # one account failing mustn't stop the others
            print(f"::warning::GTT check skipped for one account ({type(e).__name__})")
    return out


CLOSE_ROUND = dt.time(15, 10)    # the backtest buys at the close: the "buy" alert waits for the last rounds


OPEN_STATES = {"OPEN", "TRIGGER PENDING", "AMO REQ RECEIVED", "OPEN PENDING", "VALIDATION PENDING",
               "PUT ORDER REQ RECEIVED", "MODIFY PENDING", "MODIFY VALIDATION PENDING"}
ORDER_NEAR_PCT = 0.5


def order_alerts(priv, prices, today):
    """Regular (non-GTT) orders: Kite lists only TODAY's orders. Saves a slim
    private copy for the dashboard (ledger/orders.json or
    ledger/accounts/<id>/orders.json) and alerts when an order fills, is
    rejected, or a pending order's price/trigger is within 0.5%."""
    out = []
    for aid, label, tok_path, key_env in ACCOUNTS:
        try:
            kite = _kite(priv, tok_path, os.environ.get(key_env, ""))
            if kite is None:
                continue
            orders = [o for o in (kite.orders() or []) if o.get("exchange") in ("NSE", "BSE")]
            slim = [{"id": o.get("order_id"), "symbol": o.get("tradingsymbol"), "exchange": o.get("exchange"),
                     "side": o.get("transaction_type"), "type": o.get("order_type"), "product": o.get("product"),
                     "variety": o.get("variety"), "status": o.get("status"), "qty": o.get("quantity"),
                     "filled": o.get("filled_quantity"), "price": o.get("price"), "trigger": o.get("trigger_price"),
                     "avg": o.get("average_price"), "time": str(o.get("order_timestamp") or "")[:16],
                     "note": (o.get("status_message") or "")[:120] if o.get("status") == "REJECTED" else ""}
                    for o in orders]
            opath = "ledger/orders.json" if aid == "huf" else f"ledger/accounts/{aid}/orders.json"
            old, sha = priv.get_json(opath, None)
            if not old or old.get("orders") != slim or old.get("date") != today:
                priv.put_json(opath, {"date": today, "updated_at": dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"),
                                      "orders": slim}, f"orders: {aid} {today}", sha)
            for o in slim:
                side = (o["side"] or "").lower()
                if o["status"] == "COMPLETE":
                    out.append((f"ord-done:{aid}:{o['id']}", f"✅ {o['symbol']} {side} order filled ({label})",
                                "Executed — details in Kite.", 5, "white_check_mark"))
                elif o["status"] == "REJECTED":
                    # Kite's reason often carries margin amounts or quantities: kept in the private
                    # orders file for the dashboard, not sent through ntfy.
                    out.append((f"ord-rej:{aid}:{o['id']}", f"❌ {o['symbol']} {side} order rejected ({label})",
                                "See Kite (or the dashboard's Today's orders) for the reason.", 5, "x"))
                elif o["status"] in OPEN_STATES:
                    p = prices.get(o["symbol"]) or prices.get(f"{o['symbol']}-SM")
                    lvl = o["trigger"] or o["price"]
                    if p and lvl and o["type"] != "MARKET" and abs(p / lvl - 1) * 100 <= ORDER_NEAR_PCT:
                        what = "trigger" if o["trigger"] else "limit"
                        out.append((f"ord-near:{aid}:{o['id']}", f"👀 {o['symbol']} near its {side} order ({label})",
                                    f"Now {rupees(p)}, {abs(p / lvl - 1) * 100:.1f}% from the order's {what}.", 4, "eyes"))
        except Exception as e:
            print(f"::warning::order check skipped for one account ({type(e).__name__})")
    return out


MARKET_CLOSE = dt.time(15, 30)   # the loop runs a few minutes past the close: no "buy" alert then


def dip_alerts(cycles, prices, prev, month, now):
    out, papers = [], []
    if now.time() >= MARKET_CLOSE:
        return out, papers
    closing = now.time() >= CLOSE_ROUND
    for s in cycles:
        if s.get("grade") not in DIP_GRADES or not s.get("entry") or not in_season(s, month):
            continue
        sym = s["symbol"]
        p, pc = prices.get(sym), prev.get(sym)
        if not p or not pc:
            continue
        chg = (p / pc - 1) * 100
        if chg > DIP_PCT or chg < -15:          # below -15%: likely a split/bonus ex-date, not a real fall
            continue
        tgt = p * (1 + (2.0 + COST_PCT) / 100)
        if closing:
            papers.append({"symbol": sym, "change_pct": round(chg, 2), "price": p, "prev_close": pc, "target": round(tgt, 2)})
            out.append((f"dip-close:{sym}", f"📉 {sym} −{abs(chg):.1f}% near the close · STRONG cycle",
                        f"Now {rupees(p)} (prev close {rupees(pc)}). Plan: buy before the close, sell at {rupees(tgt)} (+2% after charges), else exit by day 5. Backtest: 78% reached +2% within 5 days; misses avg −4.8%.", 5, "chart_with_downwards_trend"))
        else:
            out.append((f"dip-early:{sym}", f"👀 {sym} −{abs(chg):.1f}% · STRONG cycle in season",
                        f"Now {rupees(p)}. Heads-up only: the tested rule buys at the close if it's still down 2%+; you'll get a buzz after 3:10 pm if so.", 4, "eyes"))
    return out, papers


LOGIN_URL = "https://kite-login.jami85in.workers.dev/"
LOCAL_SENT = os.path.join(os.environ.get("RUNNER_TEMP") or "/tmp", "notify-sent.json")


def _local_sent(today, add=None):
    """What this run already sent today, kept on the runner too, so a failed save
    of the state file can't make an alert repeat every 10 minutes."""
    try:
        got = json.load(open(LOCAL_SENT))
    except (OSError, ValueError):
        got = {}
    have = set(got.get(today, []))
    if add:
        have |= set(add)
        try:
            json.dump({today: sorted(have)}, open(LOCAL_SENT, "w"))
        except OSError:
            pass
    return have


def private_repo_reachable(priv, topic, now):
    """If GitHub refuses the private-repo token (expired or revoked), nothing
    else can work: say so once a day by ntfy (which needs no GitHub) and stop."""
    import urllib.error, urllib.request
    try:
        urllib.request.urlopen(urllib.request.Request(f"https://api.github.com/repos/{priv.repo}", headers=priv.hdr),
                               timeout=30).close()
        return True
    except urllib.error.HTTPError as e:
        if e.code not in (401, 403, 404):
            return True                           # GitHub hiccup: let the round try as usual
        code = e.code
    except Exception:
        return True
    print(f"::error::GitHub refuses SECRETS_REPO_PAT for the private repo (HTTP {code}): prices, trade sync "
          f"and alerts are stopped until it is renewed.")
    key = f"pat-broken:{code}"
    if topic and key not in _local_sent(now.date().isoformat()):
        try:
            send(topic, "🔧 NSE tracker stopped: GitHub token problem",
                 f"GitHub refused the private-repo token (HTTP {code}). Renew SECRETS_REPO_PAT in the nse-tracker repo "
                 f"settings; until then there are no prices, trade sync or alerts.", 5, "wrench",
                 click="https://github.com/settings/personal-access-tokens")
            _local_sent(now.date().isoformat(), [key])
        except Exception as e2:
            print(f"::warning::couldn't send the token alert ({type(e2).__name__})")
    return False
REMIND_FROM, REMIND_TO = dt.time(8, 45), dt.time(14, 30)


def login_reminders(priv, now):
    """Trading days from 08:45 IST: one buzz per account per day while today's
    Kite login is missing (HUF at priority 5 — prices and alerts depend on it;
    wife/mother at 4, only once their API key is set up)."""
    import market_session
    from kite_token_reader import token_is_stale
    if not (REMIND_FROM <= now.time() <= REMIND_TO) or not market_session.session(now.date()):
        return []
    out = []
    for aid, label, tok_path, key_env in ACCOUNTS:
        if not os.environ.get(key_env):
            continue
        tok, _ = priv.get_json(tok_path, None)
        if tok and tok.get("access_token") and not token_is_stale(tok, now):
            continue
        out.append((f"login:{aid}", f"🔑 Kite login needed today ({label})",
                    "Tap to open the login page." + (" Prices, alerts and the trade sync wait for this login."
                                                     if aid == "huf" else " Today's trades sync once logged in."),
                    5 if aid == "huf" else 4, "key"))
    return out


def main():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    priv = PrivateRepo()
    if not priv.ok:
        return 0
    now = dt.datetime.now(IST)
    today = now.date().isoformat()
    if not private_repo_reachable(priv, topic, now):
        return 0
    try:
        live = json.load(open("data/prices_live.json"))
    except (OSError, ValueError):
        live = {}
    fresh = live.get("data_date") == today       # price alerts only on today's prices
    prices = dict(live.get("prices", {})) if fresh else {}
    prev = live.get("prev_close", {}) if fresh else {}

    # GTT and order checks also refresh the private gtt/orders files the dashboard
    # shows, so they run without a topic and before the HUF login (wife/mother).
    checks = [("gtt", lambda: gtt_alerts(priv, prices, today)), ("orders", lambda: order_alerts(priv, prices, today))]
    if topic:
        checks.append(("login", lambda: login_reminders(priv, now)))
        if fresh:
            checks.insert(0, ("swing", lambda: swing_alerts(priv, prices)))
    alerts, papers = [], []
    for name, fn in checks:
        try:
            alerts += fn()
        except Exception as e:
            print(f"::warning::{name} check skipped this round ({type(e).__name__})")
    if not topic:
        return 0                                  # nowhere to send alerts (NTFY_TOPIC not set)
    dips_ok = False
    if fresh:
        try:
            cycles = json.load(open("data/cycles/summary.json")).get("stocks", [])
            a, papers = dip_alerts(cycles, prices, prev, now.month, now)
            alerts += a
            dips_ok = True
        except Exception as e:
            print(f"::warning::cycle-dip check skipped this round ({type(e).__name__})")

    state_path = f"notify/state-{today[:7]}.json"
    state, sha = priv.get_json(state_path, {})
    state = state or {}
    sent_today = set(state.get(today, [])) | _local_sent(today)
    levels, lsha = priv.get_json("notify/levels.json", {})   # swing level alerts: once per level, across months
    levels = levels or {}
    sticky = set(levels.get("sent", []))
    new = [a for a in alerts if a[0] not in sent_today and a[0] not in sticky]
    # More than 4 new alerts of one kind in a round (e.g. the first round after a
    # login finds the day's fills): one summary per kind instead of a burst.
    BATCH = {"dip-close": ("📉", "STRONG cycle stocks down 2%+ near the close — buy-at-close candidates",
                           " Sell target for each: +2.23% from your buy price."),
             "dip-early": ("👀", "STRONG cycle stocks down 2%+ today (heads-up)", ""),
             "swing-t": ("🎯", "swing targets reached", ""), "swing-s": ("🛑", "swing exit levels reached", ""),
             "gtt-fired": ("✅", "GTTs triggered", " Check the orders in Kite."),
             "gtt-hit": ("⚡", "stocks at their GTT trigger", " Check the orders in Kite."),
             "gtt-near": ("👀", "stocks near their GTT trigger", ""),
             "ord-done": ("✅", "orders filled", " Details in Kite."), "ord-rej": ("❌", "orders rejected", " See Kite."),
             "ord-near": ("👀", "stocks near their pending order", "")}
    batch_keys = {}
    for kind, (icon, words, tail) in BATCH.items():
        group = [a for a in new if a[0].split(":", 1)[0] == kind]
        if len(group) > 4:
            syms = []
            for a in group:                     # titles read "<icon> SYMBOL ..."
                s_ = a[1].split(" ")[1] if " " in a[1] else "?"
                if s_ not in syms:
                    syms.append(s_)
            new = [a for a in new if a[0].split(":", 1)[0] != kind] + [(
                f"{kind}:batch", f"{icon} {len(group)} {words}", ", ".join(syms) + "." + tail,
                max(a[3] for a in group), group[0][4])]
            batch_keys[f"{kind}:batch"] = [a[0] for a in group]
    delivered = []
    for key, title, body, prio, tags in new:
        try:
            send(topic, title, body, prio, tags,
                 click=LOGIN_URL if key.startswith("login:") else "https://jami85in.github.io/nse-tracker/")
            delivered.append(key)
        except Exception as e:
            print(f"::warning::a notification could not be sent ({type(e).__name__})")
    for bk_, members in batch_keys.items():
        if bk_ in delivered:
            delivered += members
    before = json.dumps(state, sort_keys=True)
    delivered = [d for d in delivered if not d.endswith(":batch")]   # the members are what's remembered
    _local_sent(today, delivered)
    if delivered:
        state[today] = sorted(sent_today | {d for d in delivered if not d.startswith("swing-")})
    new_levels = {d for d in delivered if d.startswith("swing-")}
    if new_levels:
        try:
            priv.put_json("notify/levels.json", {"sent": sorted(sticky | new_levels)}, f"notify levels {today}", lsha)
        except Exception as e:
            print(f"::warning::alert levels not saved ({type(e).__name__})")
    if fresh and dips_ok and CLOSE_ROUND <= now.time() < MARKET_CLOSE:   # a failed round keeps the last record
        # Paper record of the tested rule: rebuilt every round from 15:10, so it ends up
        # as the stocks still down 2%+ in the last round (the price is that round's, not
        # the official close).
        rebuilt = {pp["symbol"]: dict(pp, seen_at=now.strftime("%H:%M"), price_is="last 10-min round from 15:10")
                   for pp in papers}
        if rebuilt or today in state.get("dips", {}):
            state.setdefault("dips", {})[today] = rebuilt
    if json.dumps(state, sort_keys=True) != before:   # commit only when something changed
        try:
            priv.put_json(state_path, state, f"notify: {today}", sha)
        except Exception as e:
            print(f"::warning::alert state not saved ({type(e).__name__})")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"::warning::notifications skipped this round ({type(e).__name__})")
        sys.exit(0)
