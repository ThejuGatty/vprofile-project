"""Offline tests for crypto.py. The price series here are generated (a seeded
random walk), not market data: they exist to test the logic.

    python3 -m unittest discover -s .claude/skills/trader-crypto/tests -v
"""
import contextlib
import importlib.util
import io
import json
import os
import random
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("crypto", os.path.join(HERE, "..", "crypto.py"))
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)

T0 = 1760000000 - 1760000000 % 86400   # a UTC midnight


def yahoo_doc(bars):
    return {"chart": {"result": [{
        "timestamp": [b[0] for b in bars],
        "indicators": {"quote": [{"open": [b[1] for b in bars], "high": [b[2] for b in bars],
                                  "low": [b[3] for b in bars], "close": [b[4] for b in bars]}]}}]}}


def walk_bars(n, step, start, vol, seed):
    rnd = random.Random(seed)
    out, p = [], start
    for i in range(n):
        o = p
        c = o * (1 + rnd.gauss(0, vol))
        h = max(o, c) * (1 + abs(rnd.gauss(0, vol / 2)))
        l = min(o, c) * (1 - abs(rnd.gauss(0, vol / 2)))
        out.append((T0 + i * step, o, h, l, c))
        p = c
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.root = os.path.join(self.dir, "trader", "crypto")
        self.off = os.path.join(self.dir, "off")
        os.makedirs(self.off)
        self.hourly = walk_bars(60 * 24, 3600, 100000.0, 0.006, 1)
        self.daily = walk_bars(120, 86400, 90000.0, 0.03, 2)
        for name, bars in (("BTC-1h", self.hourly), ("BTC-1d", self.daily)):
            with open(os.path.join(self.off, name + ".json"), "w") as fh:
                json.dump(yahoo_doc(bars), fh)
        last = self.hourly[-1][0] + 3600
        self.now = C.dt.datetime.fromtimestamp(last, C.dt.timezone.utc).strftime("%Y-%m-%d %H:%M")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def run_cli(self, *args):
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = C.main(["--root", self.root, "--offline", self.off, "--now", self.now] + list(args))
        return code, out.getvalue() + err.getvalue()


class Symbols(unittest.TestCase):
    def test_forms(self):
        for s in ("BTC", "btc", "BTC-USD", "BTC/USD", "BTCUSD", "BTCUSDT"):
            self.assertEqual(C.base_of(s), "BTC")
        self.assertEqual(C.base_of("DOGE"), "DOGE")
        self.assertEqual(C.pair_of("eth-usd"), "ETH/USD")
        with self.assertRaises(C.CryptoError):
            C.base_of("not a coin!")


class PlanAndSize(unittest.TestCase):
    cfg = dict(C.DEFAULTS)
    now = C.utc_now("2026-11-01 12:00")

    def test_plan_percent_stop_and_2r(self):
        p = C.make_plan("BTC", 100000.0, "2%", "2R", None, self.cfg, self.now)
        self.assertEqual(p["stop"], 98000.0)
        self.assertEqual(p["target"], 104000.0)
        self.assertEqual(p["r_multiple"], 2.0)
        self.assertEqual(p["pair"], "BTC/USD")

    def test_plan_refuses_bad_levels(self):
        with self.assertRaises(C.CryptoError):
            C.make_plan("BTC", 100.0, "101", "2R", None, self.cfg, self.now)
        with self.assertRaises(C.CryptoError):
            C.make_plan("BTC", 100.0, "0.1%", "2R", None, self.cfg, self.now)   # inside slippage
        with self.assertRaises(C.CryptoError):
            C.make_plan("BTC", 100.0, "2%", "99", None, self.cfg, self.now)

    def test_size_is_capped_by_max_loss(self):
        cfg = dict(self.cfg, max_order_usd=900, max_position_pct=100)
        p = C.make_plan("BTC", 100000.0, "2%", "3R", None, cfg, self.now)
        pf = {"cash": 1000.0, "holdings": {}}
        o, decider, _ = C.size_order(p, cfg, pf)
        self.assertEqual(decider, "max loss per trade")
        self.assertLessEqual(o["max_loss_usd"], self.cfg["max_loss_per_trade_usd"])
        self.assertEqual(o["side"], "buy")
        self.assertEqual(o["type"], "limit")
        self.assertEqual(o["time_in_force"], "gtc")

    def test_size_refuses_thin_reward_after_fees(self):
        p = C.make_plan("BTC", 100000.0, "2%", "2R", None, self.cfg, self.now)
        with self.assertRaises(C.CryptoError) as e:
            C.size_order(p, self.cfg, {"cash": 1000.0, "holdings": {}})
        self.assertIn("after fees", str(e.exception))

    def test_size_refuses_under_minimum(self):
        cfg = dict(self.cfg, max_loss_per_trade_usd=0.05)
        p = C.make_plan("BTC", 100000.0, "2%", "3R", None, cfg, self.now)
        with self.assertRaises(C.CryptoError):
            C.size_order(p, cfg, {"cash": 1000.0, "holdings": {}})


class Yes(unittest.TestCase):
    o = {"symbol": "BTC/USD", "side": "buy", "qty": 0.00012, "type": "limit",
         "time_in_force": "gtc", "limit_price": 100000.0}

    def test_yes(self):
        self.assertTrue(C.parse_yes("yes", self.o)[0])
        self.assertTrue(C.parse_yes("Yes, buy 0.00012 BTC", self.o)[0])

    def test_not_yes(self):
        for r in ("sure", "ok", "yes?", "yes to all", "yes but wait", "yes 0.5", ""):
            self.assertFalse(C.parse_yes(r, self.o)[0], r)
        self.assertFalse(C.parse_yes("yes ETH", self.o, ["BTC/USD", "ETH/USD"])[0])


class Flow(Base):
    def test_full_flow(self):
        code, out = self.run_cli("price", "BTC", "--write")
        self.assertEqual(code, 0, out)
        code, out = self.run_cli("plan", "BTC", "--stop", "2%", "--target", "3R")
        self.assertEqual(code, 0, out)
        code, out = self.run_cli("sim", "--hours", "24", "--paths", "500", "--seed", "3")
        self.assertEqual(code, 0, out)
        self.assertIn("target first", out)
        code, out = self.run_cli("size")
        self.assertEqual(code, 0, out)
        code, out = self.run_cli("ticket", "approve", "--reply", "yes")
        self.assertEqual(code, 1, out)          # not checked yet
        self.assertIn("NOT CHECKED", out)
        code, out = self.run_cli("check")
        self.assertEqual(code, 0, out)
        self.assertIn("PASS", out)
        code, out = self.run_cli("ticket", "approve", "--reply", "sure")
        self.assertEqual(code, 1, out)
        code, out = self.run_cli("ticket", "approve", "--reply", "yes")
        self.assertEqual(code, 0, out)
        order = json.load(open(os.path.join(self.root, "orders.json")))["orders"][0]
        code, out = self.run_cli("ticket", "result", "--broker-id", "x1",
                                 "--filled-qty", str(order["qty"]), "--filled-price", str(order["limit_price"]))
        self.assertEqual(code, 0, out)
        pf = json.load(open(os.path.join(self.root, "portfolio.json")))
        self.assertAlmostEqual(pf["holdings"]["BTC"], order["qty"])
        code, out = self.run_cli("ticket", "protect")
        self.assertEqual(code, 0, out)
        stop = json.load(open(os.path.join(self.root, "orders.json")))["orders"][0]
        self.assertEqual((stop["side"], stop["type"]), ("sell", "stop_limit"))
        self.assertLess(stop["limit_price"], stop["stop_price"])
        code, out = self.run_cli("check")
        self.assertEqual(code, 0, out)

    def test_blocks(self):
        self.run_cli("price", "BTC", "--write")
        self.run_cli("plan", "BTC", "--stop", "2%", "--target", "3R")
        self.run_cli("size")
        with open(os.path.join(self.root, "config.json"), "w") as fh:
            json.dump({"max_loss_per_trade_usd": 1, "coins": ["ETH"]}, fh)
        code, out = self.run_cli("check")
        self.assertEqual(code, 1, out)
        self.assertIn("max loss per trade", out)
        self.assertIn("not on your coins list", out)

    def test_no_short_and_live_guard(self):
        cfg = dict(C.DEFAULTS, paper=False)
        pf = {"cash": 1000.0, "holdings": {}}
        st = {"events": []}
        sell = {"symbol": "BTC/USD", "side": "sell", "qty": 0.1, "type": "market", "time_in_force": "gtc"}
        r = C.check_order(sell, cfg, None, pf, st, {}, C.utc_now("2026-11-01 12:00"))
        self.assertTrue(any("no short sales" in x for x in r))
        self.assertTrue(any("allow_live" in x for x in r))

    def test_watch_rings_once(self):
        self.run_cli("plan", "BTC", "--entry", "1", "--stop", "0.5", "--target", "2")
        price = self.hourly[-1][4]
        code, out = self.run_cli("watch")
        self.assertEqual(code, 0, out)
        self.assertIn("ALERT target", out)
        code, out = self.run_cli("watch")
        self.assertEqual(code, 1, out)
        self.assertIn("Nothing hit", out)
        self.assertGreater(price, 2)


class Sim(unittest.TestCase):
    def test_deterministic_and_sums_to_100(self):
        bars = walk_bars(500, 3600, 100.0, 0.01, 5)
        plan = {"entry": 100.0, "stop": 98.0, "target": 104.0}
        a = C.simulate(plan, bars, 24, 1000, 9, 0.25)
        b = C.simulate(plan, bars, 24, 1000, 9, 0.25)
        self.assertEqual(a, b)
        self.assertAlmostEqual(sum(a["pct"].values()), 100.0, places=0)
        self.assertGreater(a["history_runs"], 0)

    def test_stop_first_when_bar_touches_both(self):
        self.assertEqual(C.walk(100, 98, 102, [(1.05, 0.95, 1.0)])[0], "stop")


if __name__ == "__main__":
    unittest.main()
