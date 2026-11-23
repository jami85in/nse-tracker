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
  3. Cycle dips: a STRONG seasonal-cycle stock that is in its up-season, is
     down 2% or more today, and still has at least 8% to go to its seasonal
     target (the Cycles tab's median seasonal gain on top of this cycle's
     entry-month close): a priority-4 heads-up during the day, and the
     priority-5 "buy at the close" alert from 15:10 IST (the backtest buys at
     the close, so the paper record uses the last price at/after 15:10).
     Backtest (Sep 2020 - Nov 2026, daily bars, today's STRONG list and
     medians, so in-sample): without the 8% rule 79.5% reached +2% after
     charges within 5 trading days (misses avg -4.8%, avg trade +0.60%); with
     it 79.7% (misses avg -4.5%, avg trade +0.68%) on 9,158 of 12,517
     signals. The rule mainly trims the misses (stocks already at or past
     their target); results are similar for any threshold from 0% to 20%.
     Without hindsight (medians/grades from earlier years only) the edge is
     about half: ~74-77% hit, +0.3-0.5% a trade; 2026 alone 74%, +0.28%
     (review 24 Nov 2026). Also logged to the private repo as a paper
     record, so the idea can be judged on new data before real money goes in.

Phone setup: install "ntfy" (Play Store), tap +, subscribe to the topic name
saved as the NTFY_TOPIC secret. Action alerts use priority 5 (sound + long
vibration); heads-ups use priority 4.

PRIVACY: this runs in the PUBLIC repo, so it prints nothing about alerts,
holdings or orders, only failures by type. Notification text travels via
ntfy.sh under the private topic name.
"""
import datetime as dt, json, os, secrets, sys, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from private_repo import PrivateRepo

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DIP_PCT = -2.0
DIP_GRADES = ("STRONG",)
DIP_MIN_ROOM_PCT = 8.0     # cycle dips: the seasonal target must be at least this far above the price
GTT_NEAR_PCT = 1.0
COST_PCT = 0.23
ACCOUNTS = [  # (id, label, token file, API-key env var)
    ("huf", "HUF", "kite_token.json", "KITE_API_KEY"),
    ("wife", "Wife", "tokens/wife.json", "KITE_API_KEY_WIFE"),
    ("mother", "Mother", "tokens/mother.json", "KITE_API_KEY_MOTHER"),
]


def rupees(v):
    return f"₹{v:,.2f}"


def send(topic, title, body, priority=5, tags="chart_with_upwards_trend", click=None, order_url=None):
    """Publish with ntfy's JSON API (UTF-8 titles, emoji and ₹ work)."""
    msg = {"topic": topic, "title": title, "message": body, "priority": priority, "tags": [tags]}
    if click:
        msg["click"] = click
    if order_url:     # a button on the alert: opens Kite's order screen, filled in (see order links below)
        msg["actions"] = [{"action": "view", "label": "Review order in Kite", "url": order_url, "clear": True}]
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
            # Sell link: the swing position (not shares from older rounds), on the symbol and
            # exchange Kite knows the holding by (SME "-SM", BSE-only scrips).
            q_ = int(min(sw.get("qty") or h.get("qty") or 0, h.get("qty") or 0))
            k_sym = h.get("zerodha_symbol") or sym
            k_ex = h.get("zerodha_exchange") or "NSE"

            def spec(px):
                o = _order(k_sym, "SELL", q_, px, exchange=k_ex) if q_ >= 1 else None
                return ({"account": aid, "client": a.get("client"), "who": label, "label": f"Sell {sym} ({label})",
                         "orders": [o]} if o else None)
            if p >= sw["target"]:
                out.append((f"swing-t:{aid}:{sym}:{sw['target']}:{since}", f"🎯 {sym} swing target reached ({label})",
                            f"Now {rupees(p)}. Target and exit are on the dashboard's Swing levels.", 5, "dart", spec(p)))
            elif sw.get("stop") and p <= sw["stop"]:
                # exit: a limit 0.5% under the price so the order fills while the price is falling
                out.append((f"swing-s:{aid}:{sym}:{sw['stop']}:{since}", f"🛑 {sym} at swing exit level ({label})",
                            f"Now {rupees(p)}. Target and exit are on the dashboard's Swing levels.", 5, "warning",
                            spec(p * 0.995)))
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
                            "Kite has sent the order — check in Kite that it went through.", 5, "white_check_mark"))
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
                for i, trig in enumerate(g["triggers"] or []):
                    if not trig:
                        continue
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


def cycle_target(s, p):
    """Seasonal target of an in-season cycle stock, as on the Cycles tab: this
    cycle's entry-month close x (1 + the median seasonal gain). The entry-month
    close comes from the weekly summary: last close / (1 + this cycle so far).
    Returns (target, % above p), or None when it can't be worked out safely."""
    med, cr, lc = s.get("median"), s.get("cycle_return"), s.get("last_close")
    if not isinstance(med, (int, float)) or not isinstance(lc, (int, float)) or lc <= 0 or not p or p <= 0:
        return None
    if isinstance(cr, (int, float)) and cr > -100:
        entry_close = lc / (1 + cr / 100)
    elif s.get("phase") == "BUY_WINDOW":
        entry_close = lc            # summary dated inside the entry month: its last close stands in
    else:
        return None
    if not 0.82 <= p / lc <= 1.22:  # split/bonus (even 1:4) since the weekly summary: not comparable
        return None
    t = entry_close * (1 + med / 100)
    return t, (t / p - 1) * 100


def dip_alerts(cycles, prices, prev, month, now, buy=None):
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
        ct = cycle_target(s, p)
        if not ct or ct[1] < DIP_MIN_ROOM_PCT:   # too close to (or past) its seasonal target, or unknown
            continue
        # A 1:5 or 1:10 bonus since the weekly summary (live/weekly ratio ~0.833 / ~0.909) would
        # leave the target that much too high: skip when the stock only passes if no bonus happened.
        r_ = p / s["last_close"]
        if any(abs(r_ / f - 1) <= 0.015 and (ct[0] * f / p - 1) * 100 < DIP_MIN_ROOM_PCT for f in (5 / 6, 10 / 11)):
            continue
        season = f"Median seasonal target {rupees(ct[0])} (+{ct[1]:.0f}% from here)."
        tgt = p * (1 + (2.0 + COST_PCT) / 100)
        if closing:
            papers.append({"symbol": sym, "change_pct": round(chg, 2), "price": p, "prev_close": pc, "target": round(tgt, 2),
                           "cycle_target": round(ct[0], 2), "room_pct": round(ct[1], 1)})
            out.append((f"dip-close:{sym}", f"📉 {sym} −{abs(chg):.1f}% near the close · STRONG cycle",
                        f"Now {rupees(p)} (prev close {rupees(pc)}). {season} Plan: buy before the close, sell at {rupees(tgt)} (+2% after charges), else exit by day 5. Backtest: 74–80% reached +2% within 5 days (74% without hindsight); misses avg −4.5%.{_no_buy_note(buy, p)}", 5, "chart_with_downwards_trend", _buy_spec(buy, sym, p)))
        else:
            out.append((f"dip-early:{sym}", f"👀 {sym} −{abs(chg):.1f}% · STRONG cycle in season",
                        f"Now {rupees(p)}. {season} Heads-up only: the tested rule buys at the close if it's still down 2%+; you'll get a buzz after 3:10 pm if so.", 4, "eyes"))
    return out, papers


# ── One-tap order links ──────────────────────────────────────────────────────
# Actionable alerts (swing target/exit, buy-at-close) carry a "Review order in Kite"
# button. The order details (stock, side, quantity, limit price) are saved in the
# PRIVATE repo (notify/links-<date>.json) under a random id; the alert carries only
# https://kite-login.jami85in.workers.dev/o/<date>/<id>. The kite-login worker turns
# that into Kite's own basket screen for the account: you check and edit it there,
# and nothing is placed until you confirm in Kite. Links work on the alert's day only.
# Several buy-at-close stocks in one round also get one basket link for all of them.
# Settings (private repo, notify/order_links.json; defaults below):
#   enabled      false turns the buttons off
#   buy_account  which account's Kite the buy links open (huf / wife / mother)
#   buy_amount   rupees per stock for buy links: quantity = amount / price, rounded down
#   product      CNC (delivery)
ORDER_LINK = "https://kite-login.jami85in.workers.dev/o"
LINKS_CFG_PATH = "notify/order_links.json"
LINKS_DEFAULT = {"enabled": True, "buy_account": "huf", "buy_amount": 25000, "product": "CNC"}


_INSTR = None


def _instruments():
    """{"EXCH:SYMBOL": (tick_size, lot_size)} from Kite's public instrument lists (NSE, BSE),
    fetched once a day per runner and cached in RUNNER_TEMP. {} if unreachable: _tick then
    falls back to NSE's price bands and quantities aren't rounded to lots."""
    global _INSTR
    if _INSTR is not None:
        return _INSTR
    import csv, io, time
    today = dt.datetime.now(IST).date().isoformat()
    cache = os.path.join(os.environ.get("RUNNER_TEMP") or "/tmp", f"kite-instruments-{today}.json")
    try:
        got = json.load(open(cache))
        # a failed download is retried at most once an hour (each round is a new process)
        if not got.get("_failed_at") or time.time() - got["_failed_at"] < 3600:
            _INSTR = got
            return _INSTR
    except (OSError, ValueError, AttributeError):
        pass
    out = {}
    for ex in ("NSE", "BSE"):
        try:
            req = urllib.request.Request(f"https://api.kite.trade/instruments/{ex}",
                                         headers={"X-Kite-Version": "3", "User-Agent": "nse-tracker-notify"})
            with urllib.request.urlopen(req, timeout=20) as r:
                for row in csv.DictReader(io.StringIO(r.read().decode("utf-8", "replace"))):
                    if row.get("segment") == ex and row.get("instrument_type") == "EQ":
                        out[f"{ex}:{row['tradingsymbol']}"] = (float(row.get("tick_size") or 0), int(float(row.get("lot_size") or 1)))
        except Exception as e:
            print(f"::warning::Kite instrument list not read ({type(e).__name__}) - tick from price bands")
    _INSTR = out
    try:
        json.dump(out if out else {"_failed_at": time.time()}, open(cache, "w"))
    except OSError:
        pass
    return _INSTR


def _tick(p, side, tick=None):
    """A limit price on the stock's tick grid. Kite's instrument list gives the tick in
    force (NSE resets it monthly from the last month-end close); without it, NSE's
    price bands from 15 Apr 2025 (NSE/CMTR/67133): 0.01 below 250, 0.05 to 1,000,
    0.10 to 5,000, 0.50 to 10,000, 1 to 20,000, 5 above, using the larger tick near
    a band edge (a whole multiple of the smaller). Buys round up, sells round down."""
    import math
    p = float(p)
    t = tick if tick and tick > 0 else (0.01 if p < 245 else 0.05 if p < 980 else 0.10 if p < 4900
                                        else 0.50 if p < 9800 else 1.0 if p < 19600 else 5.0)
    n = p / t
    n = math.ceil(n - 1e-9) if side == "BUY" else math.floor(n + 1e-9)
    return round(n * t, 2)


def _order(sym, side, qty, price, exchange="NSE", product="CNC"):
    """One LIMIT order, or None when the quantity is under one share (or one lot for SME stocks)."""
    tick, lot = (_instruments().get(f"{exchange}:{sym}") or (None, 1))[:2]
    q = int(qty) // lot * lot if lot and lot > 1 else int(qty)
    if q < 1:
        return None
    return {"exchange": exchange, "tradingsymbol": sym, "transaction_type": side, "order_type": "LIMIT",
            "quantity": q, "price": _tick(price, side, tick), "product": product, "variety": "regular",
            "validity": "DAY", "tag": "nsetrk"}       # Kite: tag max 8 characters


def _buy_spec(buy, sym, p):
    """Buy-at-close link: quantity = the configured amount / today's price."""
    if not buy or not p:
        return None
    q = int(float(buy.get("amount") or 0) // float(p))
    # limit 0.3% over the price, so the order fills if the stock ticks up into the close
    o = _order(sym, "BUY", q, p * 1.003, product=buy.get("product") or "CNC") if q >= 1 else None
    return {"account": buy["account"], "client": buy.get("client"), "who": buy.get("who"),
            "label": f"Buy {sym} at the close ({buy.get('who')})", "orders": [o]} if o else None


def _no_buy_note(buy, p):
    """Why a buy-at-close alert has no order button, when the amount is the reason."""
    amt = float((buy or {}).get("amount") or 0)
    return f" No order button: the ₹{amt:,.0f} buy amount is less than one share." if buy and p and amt < float(p) else ""


def _links_config(priv):
    """(links on?, buy settings or None)."""
    try:
        cfg, _ = priv.get_json(LINKS_CFG_PATH, None)
        cfg = {**LINKS_DEFAULT, **(cfg if isinstance(cfg, dict) else {})}
        if not cfg.get("enabled"):
            return False, None
        accts, _ = priv.get_json("ledger/accounts.json", {"accounts": []})
        a = next((x for x in (accts or {}).get("accounts", []) if x.get("id") == cfg["buy_account"]), {})
        return True, {"account": cfg["buy_account"], "client": a.get("client"), "who": a.get("short") or cfg["buy_account"],
                      "amount": cfg.get("buy_amount"), "product": cfg.get("product") or "CNC"}
    except Exception as e:
        print(f"::warning::order-link settings not read ({type(e).__name__})")
        return False, None


def _save_links(priv, today, now, alerts):
    """Save this round's order details privately; returns {alert key: link}."""
    ids = {a[0]: secrets.token_hex(12) for a in alerts if len(a) > 5 and a[5] and a[5].get("orders")}
    if not ids:
        return {}
    try:
        path = f"notify/links-{today}.json"
        data, sha = priv.get_json(path, {})
        data = data if isinstance(data, dict) else {}
        for a in alerts:
            if a[0] in ids:
                data[ids[a[0]]] = dict(a[5], made=now.strftime("%H:%M"))
        priv.put_json(path, data, f"notify links {today}", sha)
    except Exception as e:
        print(f"::warning::order links not saved this round ({type(e).__name__})")
        return {}
    return {k: f"{ORDER_LINK}/{today}/{v}" for k, v in ids.items()}


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
    """Trading days from 08:45 IST (in practice from the price loop's first round, ~09:10,
    or earlier if a login started it): one buzz per account per day while today's
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
    links_on, buy_cfg = _links_config(priv)

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
            a, papers = dip_alerts(cycles, prices, prev, now.month, now, buy_cfg)
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
            specs = [a[5] for a in group if len(a) > 5 and a[5]]
            basket = None             # one basket link for the whole group (same account, up to 10)
            if specs and len({s_["account"] for s_ in specs}) == 1:
                orders_ = [o for s_ in specs for o in s_["orders"]]
                basket = dict(specs[0], orders=orders_[:10],
                              label=(f"First 10 of {len(orders_)}: " if len(orders_) > 10 else f"{len(orders_)}: ") + words)
            short = ""                # say when the basket holds fewer stocks than the alert lists
            if basket and len(basket["orders"]) < len(group):
                short = (f" Order button: {len(basket['orders'])} of {len(group)}"
                         + (" (Kite takes 10 at a time)." if len(group) > 10 else " (the rest cost more than the buy amount per share)."))
            new = [a for a in new if a[0].split(":", 1)[0] != kind] + [(
                f"{kind}:batch", f"{icon} {len(group)} {words}", ", ".join(syms) + "." + tail + short,
                max(a[3] for a in group), group[0][4], basket)]
            batch_keys[f"{kind}:batch"] = [a[0] for a in group]
    # 2-4 buy-at-close stocks in one round: also one basket link that fills in all of them
    singles = [a for a in new if a[0].startswith("dip-close:") and not a[0].endswith(":batch") and len(a) > 5 and a[5]]
    if len(singles) >= 2 and len({a[5]["account"] for a in singles}) == 1:
        syms_ = sorted(a[5]["orders"][0]["tradingsymbol"] for a in singles)
        new.append((f"dip-basket:{'+'.join(syms_)}", f"🧺 Buy-at-close basket: {len(singles)} stocks",
                    "One tap opens Kite with all of them filled in. Check quantities and prices, then place.",
                    5, "shopping_cart", dict(singles[0][5], label=f"Buy at the close: {', '.join(syms_)}",
                                             orders=[a[5]["orders"][0] for a in singles][:10])))
    links = _save_links(priv, today, now, new) if links_on else {}
    new = [a for a in new if not a[0].startswith("dip-basket:") or a[0] in links]   # its only point is the button
    delivered = []
    for a_ in new:
        key, title, body, prio, tags = a_[:5]
        try:
            send(topic, title, body, prio, tags,
                 click=LOGIN_URL if key.startswith("login:") else "https://jami85in.github.io/nse-tracker/",
                 order_url=links.get(key))
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
