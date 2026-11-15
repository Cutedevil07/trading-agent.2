"""Real money.

- Crypto on Kraken: fully automatic. Each buy is protected at once by a stop-loss order resting at
  Kraken, so it works even between runs. Spot only (Kraken offers Canadians no margin or derivatives).
- Stocks and ETFs on Questrade: Questrade lets only partner companies place orders by program, so the
  agent writes each long trade here as a signal; Claude's Questrade connection sends it to the owner's
  Questrade app, and nothing happens unless they approve it there.

Safety rules that nothing else overrides:
- never uses more than its budget (config live.crypto.budgetCad, live.stocks.budgetCad), whatever is in the account
- risks 1% of the total cap per trade (about $10 on $1,000)
- refuses to trade if the Kraken key can withdraw money; it only needs to query and trade
- stops buying when real losses reach 15% of the budget
- never trades in replays, tests or dry runs
- every action is wrapped so a live problem can never break the paper agent
"""
import datetime as dt
import os
import time

from core import (KrakenError, add_trading_days, floor_dec, iso, kraken_cad_pairs, kraken_private, kraken_public,
                  log, money, parse_iso, r2)

CRYPTO_HOLD_DAYS = 10
AGENT_REF = 20261115   # every order the agent sends carries this tag, so it only ever manages its own coins


class LiveDesk:
    def __init__(self, run):
        self.run = run
        self.cfg = run.cfg.get("live") or {}
        self.s = run.s.setdefault("live", {})
        self.c = self.s.setdefault("crypto", {})
        for k, v in (("positions", {}), ("trades", []), ("untradable", []), ("pnlCad", 0.0)):
            self.c.setdefault(k, v)
        self.st = self.s.setdefault("stocks", {})
        for k, v in (("signals", []), ("exits", []), ("book", {})):
            self.st.setdefault(k, v)
        safe_run = not run.replay_now and not run.dry
        self.enabled = bool(self.cfg.get("enabled")) and safe_run
        self.key = os.environ.get("AGENT_KRAKEN_KEY", "").strip()
        self.secret = os.environ.get("AGENT_KRAKEN_SECRET", "").strip()
        self.crypto_on = self.enabled and bool(self.key and self.secret)
        self.test_only = (self.cfg.get("crypto") or {}).get("mode", "live") != "live"
        self.pairs = None
        self.bal = {}
        self.today = run.today.isoformat()

    # ---------------------------------------------------------- settings
    def cap(self):
        return float(self.cfg.get("capCad", 1000))

    def risk(self):
        return float(self.cfg.get("riskPct", 0.01)) * self.cap()

    def crypto_budget(self):
        return float((self.cfg.get("crypto") or {}).get("budgetCad", 300))

    def stock_budget(self):
        return float((self.cfg.get("stocks") or {}).get("budgetCad", 700))

    def event(self, text):
        self.run.events.append("REAL MONEY: " + text)
        log("LIVE " + text)

    def guard(self, name, fn, *a):
        try:
            return fn(*a)
        except KrakenError as e:
            msg = str(e)[:160]
            self.c["lastError"] = "%s: %s" % (name, msg)
            if self.c.get("lastErrorDay") != self.today:
                self.event("Kraken problem during %s: %s" % (name, msg))
                self.c["lastErrorDay"] = self.today
            else:
                log("LIVE Kraken problem during %s: %s" % (name, msg))
        except Exception as e:  # never let real-money code break the paper agent
            self.c["lastError"] = "%s: %s" % (name, str(e)[:160])
            log("LIVE error during %s: %s" % (name, e))
        return None

    # ---------------------------------------------------------- Kraken plumbing
    def k(self, method, **params):
        return kraken_private(method, params, self.key, self.secret)

    def load_pairs(self):
        if self.pairs is None:
            cache = self.c.get("pairs") or {}
            if cache.get("date") == self.today and cache.get("pairs"):
                self.pairs = cache["pairs"]
            else:
                self.pairs = kraken_cad_pairs()
                self.c["pairs"] = {"date": self.today, "pairs": self.pairs}
        return self.pairs

    def ticker(self, pair):
        r = kraken_public("Ticker", {"pair": pair})
        t = next(iter(r.values()))
        return float(t["b"][0]), float(t["a"][0]), float(t["c"][0])

    def asset_balance(self, sym):
        p = (self.pairs or {}).get(sym)
        return float(self.bal.get(p["asset"], 0)) if p else 0.0

    # ---------------------------------------------------------- each run
    def start(self):
        """Connect, check the key is safe, protect what is held, and bring the records in line with Kraken."""
        if not self.enabled:
            self.c["status"] = "real-money trading switched off" if not self.cfg.get("enabled") else self.c.get("status", "")
            return
        if not self.crypto_on:
            self.c["status"] = "waiting for Kraken API keys (repository secrets KRAKEN_API_KEY and KRAKEN_API_SECRET)"
            return
        self.guard("connect", self._connect)
        if self.c.get("status") != "connected":
            self.crypto_on = False
            return
        self.guard("pairs", self.load_pairs)
        if not self.pairs:
            self.crypto_on = False
            return
        self.guard("stop-loss check", self._reconcile)
        self.guard("orphan check", self._adopt_orphans)

    def _connect(self):
        self.bal = self.k("Balance")
        self.c["cashCad"] = r2(float(self.bal.get("ZCAD", 0)))
        if self.c.get("keyCheckedDay") != self.today:
            # A safe key cannot withdraw: Kraken answers "Permission denied" to a withdrawal question.
            try:
                self.k("WithdrawInfo", asset="XBT", key="agent-permission-check", amount="0.001")
                withdraw_ok = True
            except KrakenError as e:
                withdraw_ok = "permission denied" not in str(e).lower()
            if withdraw_ok:
                self.c["status"] = "refused: this Kraken key can withdraw money. Make a new key without withdrawal permission."
                self.event(self.c["status"])
                return
            if not self.c.get("keyChecked"):
                try:
                    self.k("AddOrder", pair="XBTCAD", type="buy", ordertype="market", volume="0.0001", validate="true")
                except KrakenError as e:
                    self.c["status"] = "refused: this Kraken key cannot place orders (%s). Add 'Create & Modify Orders'." % str(e)[:80]
                    self.event(self.c["status"])
                    return
                self.c["keyChecked"] = iso(self.run.now)
                self.event("Kraken connected. The key can trade and cannot withdraw. Cash available %s CAD." % money(self.c["cashCad"]))
            self.c["keyCheckedDay"] = self.today
        self.c["status"] = "connected"
        self.c["lastConnected"] = iso(self.run.now)

    def _reconcile(self):
        for sym in list(self.c["positions"]):
            p = self.c["positions"][sym]
            if p.get("stopTxid"):
                info = self.k("QueryOrders", txid=p["stopTxid"]).get(p["stopTxid"], {})
                status = info.get("status")
                if status == "closed":
                    vol = float(info.get("vol_exec") or 0)
                    cost, fee = float(info.get("cost") or 0), float(info.get("fee") or 0)
                    self._record_exit(sym, vol, cost - fee, float(info.get("price") or 0), fee, "stop-loss hit (resting at Kraken)",
                                      p["stopTxid"])
                    continue
                if status in ("canceled", "expired"):
                    p["stopTxid"] = None
            if not p.get("stopTxid"):
                self._place_stop(sym, p)

    def _adopt_orphans(self):
        """A buy the agent made but never recorded (for example if a run crashed right after it) gets protected.
        Only orders carrying the agent's own tag are considered: coins you bought yourself are never touched."""
        recorded = {tr.get("txid") for tr in self.c["trades"]} | set(self.c.get("adoptedTx", []))
        closed = self.k("ClosedOrders", userref=str(AGENT_REF), start=str(int(time.time()) - 3 * 86400)).get("closed", {})
        by_pair = {pr["pair"]: s for s, pr in self.pairs.items()}
        by_pair.update({pr["altname"]: s for s, pr in self.pairs.items()})
        for tx, o in closed.items():
            d = o.get("descr", {})
            if tx in recorded or d.get("type") != "buy" or o.get("status") != "closed" or float(o.get("vol_exec") or 0) <= 0:
                continue
            sym = by_pair.get(d.get("pair"))
            if not sym or sym in self.c["positions"]:
                continue
            pr = self.pairs[sym]
            vol = floor_dec(min(float(o["vol_exec"]), float(self.bal.get(pr["asset"], 0))), pr["lot"])
            self.c.setdefault("adoptedTx", []).append(tx)
            if vol <= 0 or vol < pr["ordermin"]:
                continue
            avg = float(o.get("price") or 0) or self.ticker(pr["pair"])[0]
            p = {"symbol": sym, "pair": pr["pair"], "volume": vol, "entryPrice": avg, "costCad": r2(float(o.get("cost") or vol * avg)),
                 "stopPrice": round(avg * 0.94, pr["pdec"]), "initialStop": round(avg * 0.94, pr["pdec"]),
                 "targetPrice": round(avg * 1.12, pr["pdec"]), "entryDate": self.today, "setup": "recovered",
                 "stopTxid": None, "lastPrice": avg, "buyTxid": tx}
            self.c["positions"][sym] = p
            self._place_stop(sym, p)
            self.event("Recovered an unrecorded agent buy of %s %s; it is now protected by a stop at %s." % (vol, sym, p["stopPrice"]))

    def _place_stop(self, sym, p):
        pr = self.pairs[sym]
        vol = floor_dec(min(p["volume"], float(self.bal.get(pr["asset"], p["volume"]))), pr["lot"])
        if vol <= 0 or vol < pr["ordermin"]:
            return
        price = "%.*f" % (pr["pdec"], p["stopPrice"])
        if self.test_only:
            log("LIVE test mode: would place stop-loss %s %s at %s" % (vol, sym, price))
            return
        r = self.k("AddOrder", pair=pr["pair"], type="sell", ordertype="stop-loss", price=price, volume="%.*f" % (pr["lot"], vol),
                   userref=str(AGENT_REF))
        p["stopTxid"] = (r.get("txid") or [None])[0]
        log("LIVE stop-loss placed for %s %s at %s (%s)" % (vol, sym, price, p["stopTxid"]))

    def manage(self):
        """Take profits, time limits, bad news and moving the stop to the entry once up by the risk."""
        if not self.crypto_on:
            return
        for sym in list(self.c["positions"]):
            self.guard("manage %s" % sym, self._manage_one, sym)

    def _manage_one(self, sym):
        p = self.c["positions"][sym]
        pr = self.pairs[sym]
        bid, ask, last = self.ticker(pr["pair"])
        p["lastPrice"] = bid
        p["valueCad"] = r2(p["volume"] * bid)
        p["pnlCad"] = r2(p["valueCad"] - p["costCad"])
        held = (dt.date.fromisoformat(self.today) - dt.date.fromisoformat(p["entryDate"])).days
        news = self.run.news.get(sym) or {}
        reason = None
        if bid >= p["targetPrice"]:
            reason = "take-profit reached"
        elif held > CRYPTO_HOLD_DAYS:
            reason = "held the maximum %d days" % CRYPTO_HOLD_DAYS
        elif news.get("brokenLong"):
            reason = "news broke the reason for the trade"
        if reason:
            self._exit(sym, reason)
            return
        risk = p["entryPrice"] - p.get("initialStop", p["stopPrice"])
        if risk > 0 and bid - p["entryPrice"] >= risk and p["stopPrice"] < p["entryPrice"]:
            if p.get("stopTxid") and not self.test_only:
                self.k("CancelOrder", txid=p["stopTxid"])
                p["stopTxid"] = None
            p["stopPrice"] = round(p["entryPrice"], pr["pdec"])
            self._place_stop(sym, p)
            log("LIVE %s up by its risk: stop moved to the entry price" % sym)

    def _exit(self, sym, reason):
        p = self.c["positions"][sym]
        pr = self.pairs[sym]
        if self.test_only:
            log("LIVE test mode: would sell %s (%s)" % (sym, reason))
            return
        if p.get("stopTxid"):
            try:
                self.k("CancelOrder", txid=p["stopTxid"])
            except KrakenError as e:
                info = self.k("QueryOrders", txid=p["stopTxid"]).get(p["stopTxid"], {})
                if info.get("status") == "closed":
                    self._record_exit(sym, float(info.get("vol_exec") or 0), float(info.get("cost") or 0) - float(info.get("fee") or 0),
                                      float(info.get("price") or 0), float(info.get("fee") or 0), "stop-loss hit (resting at Kraken)", p["stopTxid"])
                    return
                raise
            p["stopTxid"] = None
        bal = self.k("Balance")
        self.bal = bal
        vol = floor_dec(min(p["volume"], float(bal.get(pr["asset"], 0))), pr["lot"])
        if vol <= 0 or vol < pr["ordermin"]:
            del self.c["positions"][sym]
            return
        r = self.k("AddOrder", pair=pr["pair"], type="sell", ordertype="market", volume="%.*f" % (pr["lot"], vol), userref=str(AGENT_REF))
        tx = (r.get("txid") or [None])[0]
        time.sleep(2)
        info = self.k("QueryOrders", txid=tx).get(tx, {})
        cost, fee = float(info.get("cost") or 0), float(info.get("fee") or 0)
        self._record_exit(sym, float(info.get("vol_exec") or vol), cost - fee, float(info.get("price") or 0), fee, reason, tx)

    def _record_exit(self, sym, vol, proceeds, price, fee, reason, txid):
        p = self.c["positions"].pop(sym)
        pnl = proceeds - p["costCad"]
        self.c["pnlCad"] = r2(self.c.get("pnlCad", 0) + pnl)
        self.c["trades"].append({"date": self.run.ny.strftime("%Y-%m-%d %H:%M"), "symbol": sym, "side": "SELL", "volume": vol,
                                 "priceCad": price, "valueCad": r2(proceeds), "feeCad": r2(fee), "pnlCad": r2(pnl),
                                 "reason": reason, "txid": txid})
        self.c["trades"] = self.c["trades"][-200:]
        self.event("Sold %s on Kraken (%s): %s%s" % (sym, reason, "+" if pnl >= 0 else "", money(pnl)))

    def enter_crypto(self, sym, price, stop, target, setup, score):
        if not self.crypto_on:
            return
        self.guard("buy %s" % sym, self._enter_crypto, sym, price, stop, target, setup, score)

    def _enter_crypto(self, sym, price, stop, target, setup, score):
        c = self.c
        if c.get("halted") or sym in c["positions"] or sym in c["untradable"]:
            return
        pr = self.pairs.get(sym)
        if not pr:
            log("LIVE %s has no Canadian-dollar market on Kraken: paper only" % sym)
            return
        if sum(1 for t in c["trades"] if t["side"] == "BUY" and t["date"].startswith(self.today)) >= int(self.cfg.get("maxEntriesPerDay", 2)):
            log("LIVE daily limit of real buys reached")
            return
        d = (price - stop) / price
        if d <= 0:
            return
        bid, ask, last = self.ticker(pr["pair"])
        exposure = sum(p["costCad"] for p in c["positions"].values())
        budget = self.crypto_budget() + min(0.0, c.get("pnlCad", 0))     # losses shrink what it may use
        free = min(float(c.get("cashCad", 0)), budget - exposure)
        value = min(self.risk() / d, 0.40 * self.crypto_budget(), free * 0.98)
        minimum = max(pr["costmin"], pr["ordermin"] * ask, 10.0)
        if value < minimum:
            log("LIVE %s: %s is below the minimum order or no budget left (free %s)" % (sym, money(value), money(free)))
            return
        vol = floor_dec(value / ask, pr["lot"])
        if vol < pr["ordermin"]:
            return
        ref = AGENT_REF
        params = dict(pair=pr["pair"], type="buy", ordertype="market", volume="%.*f" % (pr["lot"], vol), oflags="fciq", userref=str(ref))
        if self.test_only:
            params["validate"] = "true"
            self.k("AddOrder", **params)
            self.event("Test order accepted by Kraken (not executed): buy %s %s, about %s" % (vol, sym, money(vol * ask)))
            return
        try:
            r = self.k("AddOrder", **params)
        except KrakenError as e:
            if any(w in str(e).lower() for w in ("permission", "restricted", "unknown asset pair", "not available")):
                c["untradable"].append(sym)
            raise
        tx = (r.get("txid") or [None])[0]
        time.sleep(2)
        info = self.k("QueryOrders", txid=tx).get(tx, {})
        filled = float(info.get("vol_exec") or 0)
        avg = float(info.get("price") or ask)
        cost = float(info.get("cost") or filled * avg) + float(info.get("fee") or 0)
        if filled <= 0:
            self.event("Buy order for %s sent to Kraken but not yet filled (%s); it will be checked next run." % (sym, tx))
            filled, cost = vol, vol * ask
        p = {"symbol": sym, "pair": pr["pair"], "volume": filled, "entryPrice": avg, "costCad": r2(cost),
             "stopPrice": round(avg * stop / price, pr["pdec"]), "initialStop": round(avg * stop / price, pr["pdec"]),
             "targetPrice": round(avg * target / price, pr["pdec"]), "entryDate": self.today, "entryTime": iso(self.run.now),
             "setup": setup, "score": score, "buyTxid": tx, "stopTxid": None, "lastPrice": avg, "userref": ref}
        c["positions"][sym] = p
        c["cashCad"] = r2(float(c.get("cashCad", 0)) - cost)
        self.bal[pr["asset"]] = str(float(self.bal.get(pr["asset"], 0)) + filled)
        c["trades"].append({"date": self.run.ny.strftime("%Y-%m-%d %H:%M"), "symbol": sym, "side": "BUY", "volume": filled,
                            "priceCad": avg, "valueCad": r2(cost), "feeCad": r2(float(info.get("fee") or 0)), "reason": setup, "txid": tx})
        self._place_stop(sym, p)
        self.event("Bought %s %s on Kraken for %s (stop %s, target %s CAD)" % (filled, sym, money(cost), p["stopPrice"], p["targetPrice"]))

    # ---------------------------------------------------------- stocks: signals for Questrade
    def stock_entry(self, sym, price, stop, target, setup, score, name):
        if not self.enabled:
            return
        d = (price - stop) / price
        if d <= 0:
            return
        value = min(self.risk() / d, 0.5 * self.stock_budget())
        sig = {"id": "%s-%s" % (self.run.ny.strftime("%Y%m%d-%H%M"), sym), "symbol": sym, "name": name, "side": "buy",
               "createdAt": iso(self.run.now), "validUntil": iso(self.run.now + dt.timedelta(minutes=50)),
               "entry": round(price, 4), "stop": round(stop, 4), "target": round(target, 4), "setup": setup, "score": score,
               "maxValueCad": r2(value), "riskCad": r2(self.risk()), "currency": "USD", "maxEntryPrice": round(price * 1.01, 4)}
        self.st["signals"] = (self.st["signals"] + [sig])[-20:]
        self.st["book"][sym] = {"entry": sig["entry"], "stop": sig["stop"], "target": sig["target"], "since": sig["createdAt"],
                                "signal": sig["id"]}
        self.event("Stock signal for Questrade: buy %s up to %s, stop %s, target %s (needs your approval in the Questrade app)"
                   % (sym, money(value), sig["stop"], sig["target"]))

    def stock_exit(self, sym, reason):
        if not self.enabled or sym not in self.st["book"]:
            return
        self.st["book"].pop(sym)
        if "stop-loss" in reason:
            return  # the stop order resting at Questrade handles it
        ex = {"id": "%s-%s-exit" % (self.run.ny.strftime("%Y%m%d-%H%M"), sym), "symbol": sym, "side": "sell", "reason": reason,
              "createdAt": iso(self.run.now)}
        self.st["exits"] = (self.st["exits"] + [ex])[-20:]
        self.event("Stock signal for Questrade: sell %s (%s)" % (sym, reason))

    def stock_stop(self, sym, new_stop):
        if self.enabled and sym in self.st["book"]:
            self.st["book"][sym]["stop"] = round(new_stop, 4)
            self.st["book"][sym]["stopChanged"] = iso(self.run.now)

    # ---------------------------------------------------------- end of run
    def finish(self):
        c = self.c
        if self.crypto_on:
            open_val = sum(p.get("valueCad", p["costCad"]) for p in c["positions"].values())
            open_pnl = sum(p.get("valueCad", p["costCad"]) - p["costCad"] for p in c["positions"].values())
            c["equityCad"] = r2(self.crypto_budget() + c.get("pnlCad", 0) + open_pnl)
            c["openValueCad"] = r2(open_val)
            if not c.get("halted") and c["equityCad"] <= 0.85 * self.crypto_budget():
                c["halted"] = True
                self.event("Real crypto trading halted: down 15%% (%s of %s). Existing stops stay in place."
                           % (money(c["equityCad"]), money(self.crypto_budget())))
        self.s["capCad"], self.s["riskCad"] = self.cap(), self.risk()
        self.s["cryptoBudgetCad"], self.s["stockBudgetCad"] = self.crypto_budget(), self.stock_budget()
        self.s["enabled"] = bool(self.cfg.get("enabled"))
        self.s["updated"] = iso(self.run.now)
