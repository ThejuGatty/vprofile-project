#!/usr/bin/env python3
"""trader-crypto: the crypto add-on for the day-trader skill pack.

Spot crypto, long only, no leverage. Crypto trades 24/7, so there is no
session, no opening range and no "flat by close": days are UTC days, and a
plan carries a time stop (max_hours) instead.

Commands (all files under --root, default trader/crypto):

  price BTC ETH [--write]      latest price            -> prices.json
  scan [--coins BTC ETH ...]   daily setups             -> watchlist.json
  plan BTC --stop 3% --target 2R [--entry now|PRICE]    -> plan.json
  sim [--hours 24] [--paths 10000] [--seed 7]           odds before you risk a dollar
  size                         whole order from the plan -> orders.json
  check                        PASS / BLOCK per order    -> state/risk.json
  ticket show | approve --index N --reply "..." | result | protect | exit | log
  watch                        stop / target / time stop, each rings once

Data, no API key: Yahoo Finance chart JSON (BTC-USD), falling back to the
Coinbase Exchange public candles API. --offline DIR reads saved files instead:
<BASE>-1h.json and <BASE>-1d.json in Yahoo's chart format.

Nothing here talks to a broker. The order JSON is for Alpaca's crypto order
tool and is only sent by Claude after a PASS, the user's explicit yes and the
tool's own permission prompt.

Standard library only. Python 3.8+.
"""
import argparse
import datetime as dt
import hashlib
import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; trader-crypto/1.0; day-trader-skill add-on)"
YAHOO_HOSTS = ("query2", "query1")
COINBASE = "https://api.exchange.coinbase.com/products/%s-USD/candles?granularity=%d&start=%s&end=%s"
COINBASE_TICKER = "https://api.exchange.coinbase.com/products/%s-USD/ticker"
PAUSE = 0.5

DEFAULTS = {
    "account_usd": 1000,
    "cash_buffer_pct": 10,
    "max_position_pct": 50,
    "max_order_usd": 500,
    "min_order_usd": 10,
    "max_loss_per_trade_usd": 20,
    "max_daily_loss_usd": 60,
    "max_trades_per_day": 3,
    "max_orders_per_day": 10,
    "min_reward_risk": 1.5,
    "fee_pct": 0.25,
    "slippage_pct": 0.5,
    "max_price_age_min": 5,
    "max_entry_drift_pct": 1.0,
    "qty_decimals": 6,
    "max_hours": 24,
    "coins": ["BTC", "ETH", "SOL", "XRP", "DOGE", "LTC", "LINK", "AVAX", "DOT", "BCH", "UNI", "AAVE"],
    "blocked": [],
    "broker": "alpaca",
    "paper": True,
    "allow_live": False,
}

ORDER_FIELDS = ("symbol", "side", "qty", "notional", "type", "time_in_force",
                "limit_price", "stop_price", "client_order_id")
STRING_FIELDS = ("qty", "notional", "limit_price", "stop_price")
ORDER_TYPES = ("market", "limit", "stop_limit")
TIFS = ("gtc", "ioc")
YES_WORDS = ("yes", "y")
BATCH_WORDS = {"all", "every", "everything", "rest", "remaining", "them", "both", "batch", "each"}
DOUBT_WORDS = {"no", "not", "dont", "don't", "wait", "cancel", "but", "maybe", "later",
               "hold", "unless", "if"}


class CryptoError(Exception):
    pass


# ---------------------------------------------------------------- basics

def base_of(sym):
    """BTC, btc, BTC-USD, BTC/USD, BTCUSD -> BTC."""
    s = str(sym).strip().upper()
    s = re.sub(r"[-/]?USDT?$", "", s) if len(s) > 4 or "-" in s or "/" in s else s
    s = s.strip("-/")
    if not re.fullmatch(r"[A-Z0-9]{2,10}", s):
        raise CryptoError("not a crypto symbol: %r" % sym)
    return s


def pair_of(sym):
    return base_of(sym) + "/USD"


def utc_now(text=None):
    if text:
        try:
            return dt.datetime.strptime(text.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            raise CryptoError('--now wants "YYYY-MM-DD HH:MM" in UTC')
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def day_of(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%d")


def price_decimals(p):
    if p >= 100:
        return 2
    if p >= 1:
        return 4
    return 6


def round_down(x, d):
    f = 10 ** d
    return math.floor(x * f + 1e-9) / f


def round_up(x, d):
    f = 10 ** d
    return math.ceil(x * f - 1e-9) / f


def fmt_price(p):
    return "$" + format(p, ",.%df" % price_decimals(p))


def fmt_usd(v):
    return ("-$" if v < 0 else "$") + format(abs(v), ",.2f")


def num_str(v):
    f = float(v)
    if f == int(f):
        return str(int(f))
    return ("%.10f" % f).rstrip("0").rstrip(".")


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def load_config(root):
    cfg = dict(DEFAULTS)
    user = read_json(os.path.join(root, "config.json"), {}) or {}
    cfg.update(user)
    cfg["coins"] = [base_of(c) for c in cfg["coins"]]
    cfg["blocked"] = [base_of(c) for c in cfg["blocked"]]
    return cfg


# ---------------------------------------------------------------- data

def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def bars_from_yahoo(doc):
    try:
        res = doc["chart"]["result"][0]
    except (KeyError, IndexError, TypeError):
        raise CryptoError("unexpected chart data")
    q = res["indicators"]["quote"][0]
    out = []
    for i, t in enumerate(res.get("timestamp") or []):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        out.append((int(t), float(o), float(h), float(l), float(c)))
    return out


def _yahoo(base, rng, interval):
    last = None
    for n, host in enumerate(YAHOO_HOSTS):
        if n:
            time.sleep(PAUSE)
        url = "https://%s.finance.yahoo.com/v8/finance/chart/%s-USD?range=%s&interval=%s" % (
            host, urllib.parse.quote(base), rng, interval)
        try:
            return json.loads(_get(url))
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = e
    raise CryptoError("Yahoo: %s" % last)


def _coinbase(base, gran, days, now):
    out = {}
    end = now
    start_all = now - dt.timedelta(days=days)
    while end > start_all:
        start = max(start_all, end - dt.timedelta(seconds=gran * 299))
        url = COINBASE % (urllib.parse.quote(base), gran, iso(start), iso(end))
        rows = json.loads(_get(url))
        if not isinstance(rows, list):
            raise CryptoError("Coinbase: %s" % rows)
        for t, lo, hi, op, cl, _v in rows:
            out[int(t)] = (int(t), float(op), float(hi), float(lo), float(cl))
        end = start
        time.sleep(PAUSE)
    return [out[k] for k in sorted(out)]


def fetch_bars(base, interval, days, offline=None, now=None):
    """interval '1h' or '1d'. Returns [(unix, o, h, l, c)], oldest first."""
    if offline:
        path = os.path.join(offline, "%s-%s.json" % (base, interval))
        doc = read_json(path)
        if doc is None:
            raise CryptoError("offline file missing: %s" % path)
        bars = bars_from_yahoo(doc)
        if now is not None:
            bars = [b for b in bars if b[0] <= now.timestamp()]
        return bars
    rng = "%dd" % days if interval == "1h" else ("6mo" if days <= 180 else "1y")
    try:
        return bars_from_yahoo(_yahoo(base, rng, interval))
    except CryptoError as yerr:
        try:
            gran = 3600 if interval == "1h" else 86400
            return _coinbase(base, gran, days, now or utc_now())
        except (urllib.error.URLError, OSError, ValueError, CryptoError) as cerr:
            raise CryptoError("no data for %s: %s; Coinbase: %s" % (base, yerr, cerr))


def fetch_price(base, offline=None, now=None):
    """Returns (price, as_of datetime)."""
    if offline:
        bars = fetch_bars(base, "1h", 2, offline, now)
        if not bars:
            raise CryptoError("no offline bars for %s" % base)
        t, _o, _h, _l, c = bars[-1]
        as_of = dt.datetime.fromtimestamp(t + 3600, dt.timezone.utc)
        return c, (min(as_of, now) if now else as_of)
    try:
        doc = _yahoo(base, "1d", "5m")
        meta = doc["chart"]["result"][0]["meta"]
        return float(meta["regularMarketPrice"]), dt.datetime.fromtimestamp(
            int(meta["regularMarketTime"]), dt.timezone.utc)
    except (CryptoError, KeyError, IndexError, TypeError, ValueError) as yerr:
        try:
            d = json.loads(_get(COINBASE_TICKER % urllib.parse.quote(base)))
            t = dt.datetime.strptime(d["time"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
            return float(d["price"]), t
        except (urllib.error.URLError, OSError, ValueError, KeyError) as cerr:
            raise CryptoError("no price for %s: %s; Coinbase: %s" % (base, yerr, cerr))


# ---------------------------------------------------------------- indicators

def sma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def rsi(closes, n=14):
    if len(closes) <= n:
        return None
    gains = losses = 0.0
    for a, b in zip(closes[:n], closes[1:n + 1]):
        d = b - a
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for a, b in zip(closes[n:], closes[n + 1:]):
        d = b - a
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def atr(bars, n=14):
    if len(bars) <= n:
        return None
    trs = []
    for prev, b in zip(bars, bars[1:]):
        trs.append(max(b[2] - b[3], abs(b[2] - prev[4]), abs(b[3] - prev[4])))
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def setups(bars):
    closes = [b[4] for b in bars]
    if len(closes) < 22:
        return None
    c, pc = closes[-1], closes[-2]
    s20, s20p = sma(closes, 20), sma(closes[:-1], 20)
    r, rp = rsi(closes), rsi(closes[:-1])
    a = atr(bars)
    found = []
    if c > max(closes[-21:-1]):
        found.append("20-day closing high")
    if s20 and s20p and c > s20 and pc <= s20p:
        found.append("reclaimed 20-day average")
    if r is not None and r < 30:
        found.append("oversold")
    if rp is not None and rp < 30 and c > pc:
        found.append("oversold bounce")
    return {
        "price": c,
        "change_1d_pct": round((c / pc - 1) * 100, 2),
        "change_7d_pct": round((c / closes[-8] - 1) * 100, 2) if len(closes) >= 8 else None,
        "sma20": round(s20, 8) if s20 else None,
        "above_sma20": bool(s20 and c > s20),
        "rsi14": round(r, 1) if r is not None else None,
        "atr14": a,
        "atr14_pct": round(a / c * 100, 2) if a else None,
        "setups": found,
    }


# ---------------------------------------------------------------- price / scan

def cmd_price(a, cfg):
    now = utc_now(a.now)
    prices = read_json(os.path.join(a.root, "prices.json"), {"prices": {}}) or {"prices": {}}
    for sym in a.symbols:
        b = base_of(sym)
        p, t = fetch_price(b, a.offline, now)
        print("%s %s  (as of %s UTC, free delayed data)" % (pair_of(b), fmt_price(p), t.strftime("%Y-%m-%d %H:%M")))
        prices["prices"][b] = {"price": p, "as_of": iso(t)}
    if a.write:
        write_json(os.path.join(a.root, "prices.json"), prices)
        print("Wrote %s" % os.path.join(a.root, "prices.json"))
    return 0


def cmd_scan(a, cfg):
    now = utc_now(a.now)
    coins = [base_of(c) for c in (a.coins or cfg["coins"])]
    rows, errors = [], []
    for n, b in enumerate(coins):
        if n and not a.offline:
            time.sleep(PAUSE)
        try:
            bars = fetch_bars(b, "1d", 120, a.offline, now)
            s = setups(bars)
            if s is None:
                errors.append("%s: not enough history" % b)
                continue
            s["symbol"] = b
            s["pair"] = pair_of(b)
            rows.append(s)
        except CryptoError as e:
            errors.append(str(e))
    rows.sort(key=lambda r: (-len(r["setups"]), -(r["change_1d_pct"] or 0)))
    print("Crypto scan, %s UTC, daily bars (UTC days; today's bar is still forming)." % now.strftime("%Y-%m-%d %H:%M"))
    print("%-9s %14s %8s %8s %6s %6s  %s" % ("pair", "price", "1d %", "7d %", "RSI", "ATR%", "setups"))
    for r in rows:
        print("%-9s %14s %8s %8s %6s %6s  %s" % (
            r["pair"], fmt_price(r["price"]), r["change_1d_pct"], r["change_7d_pct"],
            r["rsi14"], r["atr14_pct"], ", ".join(r["setups"]) or "-"))
    for e in errors:
        print("skipped: %s" % e)
    write_json(os.path.join(a.root, "watchlist.json"), {"as_of": iso(now), "rows": rows})
    print("Wrote %s" % os.path.join(a.root, "watchlist.json"))
    return 0


# ---------------------------------------------------------------- plan

def make_plan(base, entry, stop_rule, target_rule, atr_value, cfg, now, max_hours=None):
    d = price_decimals(entry)
    rule = stop_rule.strip().lower()
    if rule.endswith("%"):
        pct = float(rule[:-1])
        if not 0 < pct < 50:
            raise CryptoError("stop percent must be between 0 and 50")
        stop = entry * (1 - pct / 100)
    elif rule.endswith("atr"):
        mult = float(rule[:-3] or 1)
        if not atr_value:
            raise CryptoError("no ATR available for an ATR stop")
        stop = entry - mult * atr_value
    else:
        stop = float(rule)
    stop = round_down(stop, d)
    if stop <= 0 or stop >= entry:
        raise CryptoError("the stop (%s) must be below the entry (%s)" % (fmt_price(stop), fmt_price(entry)))
    risk = entry - stop
    slip = cfg["slippage_pct"] / 100
    if risk < entry * slip:
        raise CryptoError("a stop %s below the entry is inside the %.2f%% slippage allowance; widen it"
                          % (fmt_price(risk), cfg["slippage_pct"]))
    t = target_rule.strip().lower()
    if t.endswith("r"):
        target = round_up(entry + float(t[:-1]) * risk, d)
    else:
        target = float(t)
    if target <= entry:
        raise CryptoError("the target (%s) must be above the entry" % fmt_price(target))
    plan = {
        "symbol": base, "pair": pair_of(base), "side": "long",
        "entry": round(entry, d), "stop": stop, "target": target,
        "risk_per_unit": round(risk, 10), "r_multiple": round((target - entry) / risk, 2),
        "stop_rule": stop_rule, "target_rule": target_rule,
        "stop_pct": round(risk / entry * 100, 3), "target_pct": round((target - entry) / entry * 100, 3),
        "max_hours": int(max_hours or cfg["max_hours"]),
        "created": iso(now),
    }
    plan["id"] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()[:10]
    return plan


def cmd_plan(a, cfg):
    now = utc_now(a.now)
    b = base_of(a.symbol)
    if a.entry in (None, "now"):
        entry, t = fetch_price(b, a.offline, now)
        src = "latest price, %s UTC" % t.strftime("%H:%M")
    else:
        entry, src = float(a.entry), "your price"
    atr_value = None
    if a.stop.lower().endswith("atr"):
        atr_value = atr(fetch_bars(b, "1d", 120, a.offline, now))
    plan = make_plan(b, entry, a.stop, a.target, atr_value, cfg, now, a.max_hours)
    print("Plan for %s (long, spot, no leverage)" % plan["pair"])
    print("  entry  %s  (%s)" % (fmt_price(plan["entry"]), src))
    print("  stop   %s  (%s, %.2f%% below)" % (fmt_price(plan["stop"]), a.stop, plan["stop_pct"]))
    print("  target %s  (%s, %.2f%% above, %.2fR)" % (fmt_price(plan["target"]), a.target,
                                                        plan["target_pct"], plan["r_multiple"]))
    print("  time stop: reassess after %d hours if neither has hit" % plan["max_hours"])
    write_json(os.path.join(a.root, "plan.json"), plan)
    print("Wrote %s. Next: sim, then size." % os.path.join(a.root, "plan.json"))
    return 0


def need_plan(root):
    plan = read_json(os.path.join(root, "plan.json"))
    if not plan:
        raise CryptoError("no plan yet: run `plan` first")
    return plan


# ---------------------------------------------------------------- sim

def rel_bars(bars):
    """Each hourly bar as ratios to the previous close: (h, l, c)."""
    out = []
    for prev, b in zip(bars, bars[1:]):
        pc = prev[4]
        if pc > 0:
            out.append((b[2] / pc, b[3] / pc, b[4] / pc))
    return out


def walk(entry, stop, target, steps):
    """steps: iterable of (h, l, c) ratios. Stop first when one bar touches both."""
    p = entry
    for h, l, c in steps:
        hi, lo = p * h, p * l
        if lo <= stop:
            return "stop", stop
        if hi >= target:
            return "target", target
        p *= c
    return "open", p


def simulate(plan, bars, hours, paths, seed, fee_pct):
    rb = rel_bars(bars)
    if len(rb) < hours + 24:
        raise CryptoError("not enough hourly history to simulate (%d bars)" % len(rb))
    entry, stop, target = plan["entry"], plan["stop"], plan["target"]
    risk = entry - stop
    fee_r = entry * fee_pct / 100 * 2 / risk
    rnd = random.Random(seed)
    counts = {"stop": 0, "target": 0, "open": 0}
    total_r = 0.0
    finals = []
    for _ in range(paths):
        steps = (rb[rnd.randrange(len(rb))] for _ in range(hours))
        res, px = walk(entry, stop, target, steps)
        counts[res] += 1
        total_r += (px - entry) / risk
        if res == "open":
            finals.append(px)
    hist = {"stop": 0, "target": 0, "open": 0}
    stop_f, target_f = stop / entry, target / entry
    for i in range(1, len(bars) - hours):
        e = bars[i][4]
        res, _ = walk(e, e * stop_f, e * target_f, rb[i:i + hours])
        hist[res] += 1
    avg_r = total_r / paths - fee_r
    finals.sort()
    return {
        "paths": paths, "hours": hours, "seed": seed, "bars": len(bars),
        "pct": {k: round(v * 100.0 / paths, 1) for k, v in counts.items()},
        "avg_r_after_fees": round(avg_r, 3), "fee_r": round(fee_r, 3),
        "open_median": finals[len(finals) // 2] if finals else None,
        "history": hist, "history_runs": sum(hist.values()),
    }


def verdict(avg_r):
    if avg_r >= 0.15:
        return "the sample leans your way, after fees"
    if avg_r <= -0.15:
        return "the sample leans against you, after fees"
    return "close to a coin flip after fees"


def cmd_sim(a, cfg):
    now = utc_now(a.now)
    plan = need_plan(a.root)
    hours = a.hours or plan["max_hours"]
    bars = fetch_bars(plan["symbol"], "1h", 60, a.offline, now)
    r = simulate(plan, bars, hours, a.paths, a.seed, cfg["fee_pct"])
    p = r["pct"]
    print("Time Machine for %s: entry %s, stop %s, target %s, next %d hours." % (
        plan["pair"], fmt_price(plan["entry"]), fmt_price(plan["stop"]), fmt_price(plan["target"]), hours))
    print("%d simulated paths, each hour drawn from the last %d hourly bars (seed %d):" % (
        r["paths"], r["bars"], r["seed"]))
    print("  target first %.1f%%   stop first %.1f%%   neither after %dh %.1f%%" % (
        p["target"], p["stop"], hours, p["open"]))
    if r["open_median"]:
        print("  median price of the open ones: %s" % fmt_price(r["open_median"]))
    print("  average R %.2f after fees (fees cost %.2fR round trip)" % (r["avg_r_after_fees"], r["fee_r"]))
    h = r["history"]
    print("The same stop and target from every past hour in the data (%d starts, overlapping):" % r["history_runs"])
    print("  target %d, stop %d, neither %d" % (h["target"], h["stop"], h["open"]))
    print("Verdict: %s. A simulation of the last 60 days, not a forecast." % verdict(r["avg_r_after_fees"]))
    if a.write:
        write_json(os.path.join(a.root, "sim.json"), dict(r, plan_id=plan["id"], as_of=iso(now)))
    return 0


# ---------------------------------------------------------------- size

def portfolio(root, cfg):
    p = read_json(os.path.join(root, "portfolio.json"))
    if p is None:
        p = {"cash": float(cfg["account_usd"]), "holdings": {}}
    p.setdefault("holdings", {})
    return p


def loss_per_unit(entry, stop, cfg):
    fill = stop * (1 - cfg["slippage_pct"] / 100)
    fee = cfg["fee_pct"] / 100
    return (entry - fill) + entry * fee + fill * fee


def reward_per_unit(entry, target, cfg):
    fee = cfg["fee_pct"] / 100
    return (target - entry) - entry * fee - target * fee


def stop_order(pair, qty, stop, cfg):
    d = price_decimals(stop)
    return {"symbol": pair, "side": "sell", "qty": qty, "type": "stop_limit", "time_in_force": "gtc",
            "stop_price": stop, "limit_price": round_down(stop * (1 - cfg["slippage_pct"] / 100), d)}


def size_order(plan, cfg, pf):
    entry, stop, target = plan["entry"], plan["stop"], plan["target"]
    lpu = loss_per_unit(entry, stop, cfg)
    fee = cfg["fee_pct"] / 100
    rr = reward_per_unit(entry, target, cfg) / lpu
    if rr < cfg["min_reward_risk"]:
        raise CryptoError("no order: after fees and slippage this plan pays %.2f to 1, under your minimum "
                          "%.2f. Re-plan with a further target (e.g. --target 3R) or a tighter stop"
                          % (rr, cfg["min_reward_risk"]))
    held_value = float(pf["holdings"].get(plan["symbol"], 0)) * entry
    usable_cash = pf["cash"] - cfg["account_usd"] * cfg["cash_buffer_pct"] / 100
    caps = {
        "max loss per trade": cfg["max_loss_per_trade_usd"] / lpu,
        "max order": cfg["max_order_usd"] / (entry * (1 + fee)),
        "position cap": max(0.0, cfg["account_usd"] * cfg["max_position_pct"] / 100 - held_value) / entry,
        "cash after buffer": max(0.0, usable_cash) / (entry * (1 + fee)),
    }
    decider = min(caps, key=caps.get)
    qty = round_down(caps[decider], cfg["qty_decimals"])
    if qty <= 0 or qty * entry < cfg["min_order_usd"]:
        raise CryptoError("no order: the %s allows only %s %s (%s), under the %s minimum" % (
            decider, num_str(qty), plan["symbol"], fmt_usd(qty * entry), fmt_usd(cfg["min_order_usd"])))
    max_loss = round(qty * lpu, 2)
    reward = round(qty * reward_per_unit(entry, target, cfg), 2)
    order = {
        "symbol": plan["pair"], "side": "buy", "qty": qty, "type": "limit", "time_in_force": "gtc",
        "limit_price": entry,
        "kind": "entry", "plan_id": plan["id"], "max_loss_usd": max_loss, "reward_usd": reward,
        "why": "%s limited by the %s" % (num_str(qty), decider),
    }
    return order, decider, caps


def cmd_size(a, cfg):
    plan = need_plan(a.root)
    pf = portfolio(a.root, cfg)
    order, decider, caps = size_order(plan, cfg, pf)
    q = order["qty"]
    print("Sizing %s at %s, stop %s, target %s" % (plan["pair"], fmt_price(plan["entry"]),
                                                   fmt_price(plan["stop"]), fmt_price(plan["target"])))
    for k, v in caps.items():
        print("  %-20s %s%s" % (k, num_str(round_down(v, cfg["qty_decimals"])), "   <- decides" if k == decider else ""))
    print("Order: buy %s %s, limit %s, good until canceled (%s)." % (
        num_str(q), plan["symbol"], fmt_price(plan["entry"]), fmt_usd(q * plan["entry"])))
    print("Loss if the stop fills at its limit, fees in: %s. Reward at the target, fees in: %s." % (
        fmt_usd(order["max_loss_usd"]), fmt_usd(order["reward_usd"])))
    so = stop_order(plan["pair"], q, plan["stop"], cfg)
    print("After it fills: `ticket protect` builds the stop, sell %s at stop %s / limit %s." % (
        num_str(q), fmt_price(so["stop_price"]), fmt_price(so["limit_price"])))
    print("Alpaca has no bracket orders for crypto, so the target is watched by `watch`.")
    write_json(os.path.join(a.root, "orders.json"), {"created": iso(utc_now(a.now)), "orders": [order]})
    print("Wrote %s. Next: check." % os.path.join(a.root, "orders.json"))
    return 0


# ---------------------------------------------------------------- risk

def fingerprint(order):
    core = {k: order[k] for k in ORDER_FIELDS if k in order and order[k] is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def broker_state(root):
    return read_json(os.path.join(root, "state", "broker.json"), {"events": []}) or {"events": []}


def today_entries(state, today):
    return [e for e in state["events"]
            if e.get("event") == "placed" and e.get("day") == today and e.get("kind") == "entry"]


def check_order(order, cfg, plan, pf, state, prices, now):
    reasons = []
    today = day_of(now)
    try:
        b = base_of(order.get("symbol", ""))
    except CryptoError as e:
        return ["bad symbol: %s" % e]
    side, typ, tif = order.get("side"), order.get("type"), order.get("time_in_force")
    qty = order.get("qty")
    if not cfg["paper"] and not cfg["allow_live"]:
        reasons.append('paper is off but allow_live is false: live trading needs both set on purpose')
    if b in cfg["blocked"]:
        reasons.append("%s is on your blocked list" % b)
    if side not in ("buy", "sell"):
        reasons.append("side must be buy or sell")
    if typ not in ORDER_TYPES:
        reasons.append("order type %r is not allowed (market, limit, stop_limit)" % typ)
    if tif not in TIFS:
        reasons.append("time in force must be gtc or ioc for crypto")
    if not isinstance(qty, (int, float)) or qty <= 0:
        reasons.append("qty must be a positive number")
        return reasons
    if typ in ("limit", "stop_limit") and not order.get("limit_price"):
        reasons.append("a %s order needs a limit price" % typ)
    if typ == "stop_limit" and not order.get("stop_price"):
        reasons.append("a stop_limit order needs a stop price")
    if order.get("notional") is not None:
        reasons.append("use qty, not notional, so the max loss can be checked")

    if side == "sell":
        held = float(pf["holdings"].get(b, 0))
        if qty > held + 1e-12:
            reasons.append("selling %s %s but you hold %s: no short sales" % (num_str(qty), b, num_str(held)))
        return reasons   # getting out is never blocked by a day limit

    if b not in cfg["coins"]:
        reasons.append("%s is not on your coins list in config.json" % b)
    if order.get("kind") != "entry" or not plan or plan.get("id") != order.get("plan_id"):
        reasons.append("a buy needs a stop: it must come from the current plan (run plan, then size)")
        return reasons
    entry, stop, target = plan["entry"], plan["stop"], plan["target"]
    q = prices.get(b) if prices else None
    if not q:
        reasons.append("no fresh price for %s: run `price %s --write`" % (b, b))
        ref = entry
    else:
        age = (now - parse_iso(q["as_of"])).total_seconds() / 60
        if age > cfg["max_price_age_min"]:
            reasons.append("the price for %s is %.0f minutes old (limit %s): run `price %s --write`" % (
                b, age, cfg["max_price_age_min"], b))
        ref = q["price"]
        drift = abs(ref / entry - 1) * 100
        if drift > cfg["max_entry_drift_pct"]:
            reasons.append("the price %s is %.2f%% from the plan's entry %s (limit %s%%): re-plan" % (
                fmt_price(ref), drift, fmt_price(entry), cfg["max_entry_drift_pct"]))
    if typ == "limit":
        worst_entry = float(order["limit_price"])   # a buy limit never fills above it
    else:
        worst_entry = max(entry, ref)
    loss = qty * loss_per_unit(worst_entry, stop, cfg)
    if loss > cfg["max_loss_per_trade_usd"] + 0.005:
        reasons.append("loss at the stop %s is over your max loss per trade %s" % (
            fmt_usd(loss), fmt_usd(cfg["max_loss_per_trade_usd"])))
    rr = reward_per_unit(entry, target, cfg) / loss_per_unit(entry, stop, cfg)
    if rr < cfg["min_reward_risk"]:
        reasons.append("reward:risk after fees is %.2f, under your minimum %.2f" % (rr, cfg["min_reward_risk"]))
    cost = qty * worst_entry * (1 + cfg["fee_pct"] / 100)
    if cost > cfg["max_order_usd"] + 0.005:
        reasons.append("order %s is over your max order %s" % (fmt_usd(cost), fmt_usd(cfg["max_order_usd"])))
    held_value = float(pf["holdings"].get(b, 0)) * worst_entry
    cap = cfg["account_usd"] * cfg["max_position_pct"] / 100
    if held_value + qty * worst_entry > cap + 0.005:
        reasons.append("the position would be %s, over your position cap %s" % (
            fmt_usd(held_value + qty * worst_entry), fmt_usd(cap)))
    usable = pf["cash"] - cfg["account_usd"] * cfg["cash_buffer_pct"] / 100
    if cost > usable + 0.005:
        reasons.append("cost %s is more than your cash after the buffer %s" % (fmt_usd(cost), fmt_usd(usable)))
    placed = today_entries(state, today)
    used = sum(e.get("max_loss_usd", 0) for e in placed)
    if used + loss > cfg["max_daily_loss_usd"] + 0.005:
        reasons.append("today's risk would be %s, over your daily loss limit %s" % (
            fmt_usd(used + loss), fmt_usd(cfg["max_daily_loss_usd"])))
    if len(placed) >= cfg["max_trades_per_day"]:
        reasons.append("already %d trades today (UTC), your limit is %d" % (len(placed), cfg["max_trades_per_day"]))
    all_today = [e for e in state["events"] if e.get("event") == "placed" and e.get("day") == today]
    if len(all_today) >= cfg["max_orders_per_day"]:
        reasons.append("already %d orders today, your limit is %d" % (len(all_today), cfg["max_orders_per_day"]))
    return reasons


def cmd_check(a, cfg):
    now = utc_now(a.now)
    orders = (read_json(os.path.join(a.root, "orders.json"), {}) or {}).get("orders", [])
    if not orders:
        raise CryptoError("no orders: run `size` (or `ticket protect` / `ticket exit`) first")
    plan = read_json(os.path.join(a.root, "plan.json"))
    pf = portfolio(a.root, cfg)
    state = broker_state(a.root)
    prices = (read_json(os.path.join(a.root, "prices.json"), {}) or {}).get("prices", {})
    rpath = os.path.join(a.root, "state", "risk.json")
    risk = read_json(rpath, {"verdicts": []}) or {"verdicts": []}
    blocked = 0
    for i, o in enumerate(orders, 1):
        reasons = check_order(o, cfg, plan, pf, state, prices, now)
        v = "BLOCK" if reasons else "PASS"
        blocked += bool(reasons)
        print("%d. %s  %s" % (i, v, plain(o)))
        for r in reasons:
            print("     - %s" % r)
        risk["verdicts"].append({"fp": fingerprint(o), "day": day_of(now), "time": iso(now),
                                 "verdict": v, "reasons": reasons})
    risk["verdicts"] = risk["verdicts"][-200:]
    write_json(rpath, risk)
    return 1 if blocked else 0


# ---------------------------------------------------------------- tickets

def plain(o):
    b = base_of(o["symbol"])
    s = "%s %s %s" % (o["side"], num_str(o["qty"]), b)
    if o["type"] == "market":
        s += " at market"
    elif o["type"] == "limit":
        s += " limit %s" % fmt_price(o["limit_price"])
    else:
        s += " stop %s, limit %s" % (fmt_price(o["stop_price"]), fmt_price(o["limit_price"]))
    return s


def tool_input(o):
    out = {}
    for k in ORDER_FIELDS:
        if k in o and o[k] is not None:
            out[k] = num_str(o[k]) if k in STRING_FIELDS else o[k]
    return out


def amount_numbers(o):
    return [float(o[k]) for k in ("qty", "limit_price", "stop_price") if o.get(k) is not None]


def parse_yes(reply, order, others=()):
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not a yes"
    words = re.findall(r"[a-z']+", raw.lower())
    if not words or words[0] not in YES_WORDS:
        return False, 'only a reply that starts with "yes" counts (not ok, sure, go, do it)'
    if BATCH_WORDS & set(words):
        return False, "a yes covers one order at a time"
    if DOUBT_WORDS & set(words[1:]):
        return False, "the reply has a condition or a doubt in it; ask again"
    sym = base_of(order["symbol"])
    named = set(re.findall(r"[A-Z]{2,10}", raw.upper())) & ({base_of(s) for s in others} - {sym})
    if named:
        return False, "the reply names %s, but this order is for %s" % (", ".join(sorted(named)), sym)
    nums = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", raw)]
    if nums:
        want = amount_numbers(order)
        if not any(abs(n - w) <= max(1e-9, abs(w) * 1e-9) for n in nums for w in want):
            return False, "the reply's numbers do not match this order (%s)" % plain(order)
    return True, "explicit yes for %s" % plain(order)


def status_of(o, risk, state, today):
    fp = fingerprint(o)
    v = [x for x in risk.get("verdicts", []) if x["fp"] == fp and x["day"] == today]
    if not v:
        return "NOT CHECKED", fp
    if v[-1]["verdict"] != "PASS":
        return "BLOCKED", fp
    ev = [e for e in state["events"] if e.get("fp") == fp]
    if any(e["event"] in ("placed", "filled") for e in ev):
        return "PLACED", fp
    if ev and ev[-1]["event"] == "yes":
        return "APPROVED", fp
    return "READY", fp


def _ticket_ctx(a):
    now = utc_now(a.now)
    orders = (read_json(os.path.join(a.root, "orders.json"), {}) or {}).get("orders", [])
    risk = read_json(os.path.join(a.root, "state", "risk.json"), {"verdicts": []}) or {"verdicts": []}
    return now, orders, risk, broker_state(a.root)


def _pick(orders, index):
    if not orders:
        raise CryptoError("no orders in orders.json")
    if not 1 <= index <= len(orders):
        raise CryptoError("there is no order %d (there are %d)" % (index, len(orders)))
    return orders[index - 1]


def _log(root, state, now, **ev):
    ev.update(time=iso(now), day=day_of(now))
    state["events"].append(ev)
    write_json(os.path.join(root, "state", "broker.json"), state)


def cmd_ticket(a, cfg):
    now, orders, risk, state = _ticket_ctx(a)
    today = day_of(now)
    acct = "PAPER" if cfg["paper"] else "LIVE"
    if a.action == "show":
        if not orders:
            print("No orders. Run size, protect or exit first.")
            return 0
        for i, o in enumerate(orders, 1):
            st, fp = status_of(o, risk, state, today)
            print("Ticket %d of %d  [%s]  %s account, %s" % (i, len(orders), st, acct, cfg["broker"]))
            print("  %s" % plain(o))
            if o.get("max_loss_usd") is not None:
                print("  loss at the stop, fees in: %s" % fmt_usd(o["max_loss_usd"]))
            print("  tool input: %s" % json.dumps(tool_input(o)))
            if st == "READY":
                print('  Ask: "Place this order? Reply yes or no."')
            elif st in ("NOT CHECKED", "BLOCKED"):
                print("  Not ready: run check.")
        return 0
    if a.action == "approve":
        o = _pick(orders, a.index)
        st, fp = status_of(o, risk, state, today)
        if st != "READY":
            print("Not recorded: ticket %d is %s, not READY." % (a.index, st))
            return 1
        ok, why = parse_yes(a.reply, o, [x["symbol"] for x in orders])
        if not ok:
            print("Not recorded: %s." % why)
            return 1
        _log(a.root, state, now, event="yes", fp=fp, reply=a.reply, symbol=o["symbol"], kind=o.get("kind"))
        print("Recorded your yes for %s." % plain(o))
        print("Tool input: %s" % json.dumps(tool_input(o)))
        return 0
    if a.action == "decline":
        o = _pick(orders, a.index)
        _log(a.root, state, now, event="no", fp=fingerprint(o), symbol=o["symbol"])
        print("Recorded no for %s." % plain(o))
        return 0
    if a.action == "result":
        o = _pick(orders, a.index)
        st, fp = status_of(o, risk, state, today)
        if a.status == "placed" and st != "APPROVED":
            raise CryptoError("ticket %d is %s: only an APPROVED ticket can be placed" % (a.index, st))
        ev = dict(event=a.status, fp=fp, symbol=o["symbol"], side=o["side"], kind=o.get("kind"),
                  broker_id=a.broker_id, max_loss_usd=o.get("max_loss_usd", 0) if o.get("kind") == "entry" else 0)
        _log(a.root, state, now, **ev)
        if a.filled_qty:
            apply_fill(a.root, cfg, o["symbol"], o["side"], a.filled_qty, a.filled_price)
            _log(a.root, state, now, event="filled", fp=fp, symbol=o["symbol"], side=o["side"],
                 qty=a.filled_qty, price=a.filled_price)
        print("Recorded %s for %s." % (a.status, plain(o)))
        if a.filled_qty and o["side"] == "buy":
            print("Next: `ticket protect` to build the stop order, then check, then your yes.")
        return 0
    if a.action == "fill":
        apply_fill(a.root, cfg, a.symbol, a.side, a.qty, a.price)
        _log(a.root, state, now, event="filled", symbol=pair_of(a.symbol), side=a.side, qty=a.qty, price=a.price)
        print("Recorded fill: %s %s %s at %s." % (a.side, num_str(a.qty), base_of(a.symbol), fmt_price(a.price)))
        return 0
    if a.action == "protect":
        plan = need_plan(a.root)
        held = float(portfolio(a.root, cfg)["holdings"].get(plan["symbol"], 0))
        qty = round_down(a.qty if a.qty else held, cfg["qty_decimals"])
        if qty <= 0:
            raise CryptoError("you hold no %s: record the fill first" % plan["symbol"])
        o = stop_order(plan["pair"], qty, plan["stop"], cfg)
        o.update(kind="stop", plan_id=plan["id"])
        write_json(os.path.join(a.root, "orders.json"), {"created": iso(now), "orders": [o]})
        print("Built the protective stop: %s. Next: check, then show it and ask for a yes." % plain(o))
        print("A stop-limit can be skipped if the price falls through the limit in one move; watch keeps an eye on it.")
        return 0
    if a.action == "exit":
        b = base_of(a.symbol)
        held = float(portfolio(a.root, cfg)["holdings"].get(b, 0))
        qty = round_down(a.qty if a.qty else held, cfg["qty_decimals"])
        if qty <= 0:
            raise CryptoError("you hold no %s" % b)
        o = {"symbol": pair_of(b), "side": "sell", "qty": qty, "type": "market", "time_in_force": "gtc",
             "kind": "exit"}
        if a.limit:
            o.update(type="limit", limit_price=a.limit)
        write_json(os.path.join(a.root, "orders.json"), {"created": iso(now), "orders": [o]})
        print("Built the exit: %s." % plain(o))
        print("If a protective stop is open for these coins, cancel it first (cancel_order_by_id, its own yes),")
        print("or the broker will say the coins are already reserved. Next: check, then your yes.")
        return 0
    if a.action == "log":
        for e in state["events"][-30:]:
            print("%s  %-7s %s %s" % (e["time"], e["event"], e.get("symbol", ""),
                                      " ".join("%s=%s" % (k, e[k]) for k in ("side", "qty", "price", "max_loss_usd", "reply")
                                               if e.get(k) not in (None, ""))))
        placed = today_entries(state, day_of(now))
        print("Today (UTC): %d trade(s), %s at risk of your %s daily limit." % (
            len(placed), fmt_usd(sum(e.get("max_loss_usd", 0) for e in placed)), fmt_usd(cfg["max_daily_loss_usd"])))
        return 0
    raise CryptoError("unknown ticket action")


def apply_fill(root, cfg, symbol, side, qty, price):
    if price is None:
        raise CryptoError("a fill needs --filled-price / --price")
    b = base_of(symbol)
    pf = portfolio(root, cfg)
    fee = cfg["fee_pct"] / 100
    held = float(pf["holdings"].get(b, 0))
    if side == "buy":
        pf["cash"] = round(pf["cash"] - qty * price * (1 + fee), 2)
        held += qty
    else:
        if qty > held + 1e-12:
            raise CryptoError("selling %s but you hold %s" % (num_str(qty), num_str(held)))
        pf["cash"] = round(pf["cash"] + qty * price * (1 - fee), 2)
        held -= qty
    pf["holdings"][b] = round(held, 10)
    if pf["holdings"][b] <= 0:
        del pf["holdings"][b]
    write_json(os.path.join(root, "portfolio.json"), pf)


# ---------------------------------------------------------------- watch

def cmd_watch(a, cfg):
    now = utc_now(a.now)
    plan = need_plan(a.root)
    p, t = fetch_price(plan["symbol"], a.offline, now)
    wpath = os.path.join(a.root, "state", "watch.json")
    st = read_json(wpath, {}) or {}
    rung = st.setdefault(plan["id"], [])
    alerts = []
    if p <= plan["stop"] and "stop" not in rung:
        alerts.append(("stop", "ALERT stop: %s is %s, at or below your stop %s. If the stop order did not fill, "
                               "`ticket exit --symbol %s` builds the sell, then check and your yes."
                       % (plan["pair"], fmt_price(p), fmt_price(plan["stop"]), plan["symbol"])))
    if p >= plan["target"] and "target" not in rung:
        alerts.append(("target", "ALERT target: %s is %s, at or above your target %s. To take it: cancel the "
                                 "protective stop, then `ticket exit --symbol %s`, check and your yes."
                       % (plan["pair"], fmt_price(p), fmt_price(plan["target"]), plan["symbol"])))
    hours = (now - parse_iso(plan["created"])).total_seconds() / 3600
    if hours >= plan["max_hours"] and "time" not in rung:
        alerts.append(("time", "ALERT time stop: %.0f hours since the plan (limit %d). Decide: exit or re-plan."
                       % (hours, plan["max_hours"])))
    for key, msg in alerts:
        rung.append(key)
        print(msg)
    write_json(wpath, st)
    if not alerts:
        print("Nothing hit. %s %s (as of %s UTC, delayed), stop %s, target %s." % (
            plan["pair"], fmt_price(p), t.strftime("%H:%M"), fmt_price(plan["stop"]), fmt_price(plan["target"])))
        return 1
    return 0


# ---------------------------------------------------------------- cli

def build_parser():
    ap = argparse.ArgumentParser(prog="crypto.py", description="Crypto add-on for the day-trader skills.")
    ap.add_argument("--root", default=os.path.join("trader", "crypto"))
    ap.add_argument("--now", help='pin the clock, "YYYY-MM-DD HH:MM" UTC')
    ap.add_argument("--offline", help="read saved chart files from this folder")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("price")
    p.add_argument("symbols", nargs="+")
    p.add_argument("--write", action="store_true")
    p = sub.add_parser("scan")
    p.add_argument("--coins", nargs="*")
    p = sub.add_parser("plan")
    p.add_argument("symbol")
    p.add_argument("--entry", default="now")
    p.add_argument("--stop", default="1.5atr", help="3%%, 1.5atr or a price")
    p.add_argument("--target", default="3R", help="3R or a price")
    p.add_argument("--max-hours", type=int)
    p = sub.add_parser("sim")
    p.add_argument("--hours", type=int)
    p.add_argument("--paths", type=int, default=10000)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--write", action="store_true")
    sub.add_parser("size")
    sub.add_parser("check")
    p = sub.add_parser("ticket")
    p.add_argument("action", choices=["show", "approve", "decline", "result", "fill", "protect", "exit", "log"])
    p.add_argument("--index", type=int, default=1)
    p.add_argument("--reply")
    p.add_argument("--status", choices=["placed", "failed", "canceled"], default="placed")
    p.add_argument("--broker-id")
    p.add_argument("--filled-qty", type=float)
    p.add_argument("--filled-price", type=float)
    p.add_argument("--symbol")
    p.add_argument("--side", choices=["buy", "sell"])
    p.add_argument("--qty", type=float)
    p.add_argument("--price", type=float)
    p.add_argument("--limit", type=float)
    sub.add_parser("watch")
    return ap


COMMANDS = {"price": cmd_price, "scan": cmd_scan, "plan": cmd_plan, "sim": cmd_sim, "size": cmd_size,
            "check": cmd_check, "ticket": cmd_ticket, "watch": cmd_watch}


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        cfg = load_config(a.root)
        return COMMANDS[a.cmd](a, cfg)
    except CryptoError as e:
        print("Error: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
