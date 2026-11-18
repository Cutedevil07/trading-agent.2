"""Backtest: the agent's daily rules replayed over about two years of past prices.

What it uses, the same way the live agent does: the chart score (tech_score), the setups that can be seen
in daily bars (pullback, 20-day breakout, rally short, 20-day breakdown), the stop and 2x target sizes,
the time machine gate, the day-trade close rule, the 1R stop move, the hold limits, the risk limits and sizing.

What it can't use: past headlines and past company numbers (so news and company parts count as neutral),
opening-range trades (they need 5-minute bars, which only go back 60 days) and options.
Signals are read at a day's close and filled at the next day's open. When a day's bar touches both the stop
and the target, the stop counts first. The universe is today's list, so companies that vanished are missing.
"""
import datetime as dt
import math

from core import RULES, FUTURES, tech_score, replay, yahoo_chart, to_ny, epoch_to_utc, log, stop_distance

KINDS = ("stock", "etf", "tsx", "crypto", "future")
HOLD = {"stock": 5, "etf": 5, "tsx": 5, "crypto": 10, "future": 5}
SLIP = {"stock": 0.001, "etf": 0.001, "tsx": 0.001, "crypto": 0.008, "future": 0.0002}
GROUP = {"stock": "equity", "etf": "equity", "tsx": "equity", "crypto": "crypto", "future": "future"}

# The variants compared. "now" is the rule set the agent ran before these fixes.
BASE = {"cautious_bar": 80, "rescale": False, "exits": "fixed", "stop_atr": 0.5, "day_rule": True, "gate": True,
        "risk": None, "halt": True, "target_r": 2.0, "off": ()}
VARIANTS = {
    "live rules": {"stop_atr": RULES["stop_atr"], "off": RULES["setups_off"]},
    "live rules at 1% risk": {"stop_atr": RULES["stop_atr"], "off": RULES["setups_off"], "risk": 0.01, "halt": False},
    "old rules": {"stop_atr": 0.5, "off": ()},
}


# ---------------------------------------------------------------- data
def load(symbols, rng="2y"):
    """{symbol: {"kind", "yahoo", "bars"}} with each bar's day in New York (UTC for crypto)."""
    out = {}
    for sym, kind, ysym, mult in symbols:
        ch = yahoo_chart(ysym, rng, "1d")
        if not ch or len(ch["bars"]) < 260:
            continue
        bars = []
        for b in ch["bars"]:
            u = epoch_to_utc(b["t"])
            day = (u if kind == "crypto" else to_ny(u)).date()
            if bars and bars[-1]["d"] == day:
                bars[-1] = dict(b, d=day)
            else:
                bars.append(dict(b, d=day))
        out[sym] = {"kind": kind, "yahoo": ysym, "mult": mult, "bars": bars}
    return out


class Series:
    """Indicators for every day of one symbol, matching core.daily_view with the day's bar complete."""
    def __init__(self, bars):
        self.b = bars
        n = len(bars)
        c = [x["c"] for x in bars]
        self.idx = {x["d"]: i for i, x in enumerate(bars)}
        self.cum = [0.0]
        for v in c:
            self.cum.append(self.cum[-1] + v)
        self.rsi = [None] * n
        if n > 15:
            g = l = 0.0
            for i in range(1, 15):
                ch = c[i] - c[i - 1]
                g += max(ch, 0.0)
                l += max(-ch, 0.0)
            ag, al = g / 14, l / 14
            self.rsi[14] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
            for i in range(15, n):
                ch = c[i] - c[i - 1]
                ag = (ag * 13 + max(ch, 0.0)) / 14
                al = (al * 13 + max(-ch, 0.0)) / 14
                self.rsi[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
        self.tr = [0.0] + [max(b["h"] - b["l"], abs(b["h"] - a["c"]), abs(b["l"] - a["c"])) for a, b in zip(bars[:-1], bars[1:])]

    def sma(self, i, n, back=0):
        j = i - back
        if j + 1 < n:
            return None
        return (self.cum[j + 1] - self.cum[j + 1 - n]) / n

    def dv(self, i):
        if i < 30:
            return None
        b = self.b
        t, p, prev = b[i], b[i]["c"], b[i - 1]["c"]
        hist = b[max(0, i - 20):i]
        avg_vol = sum(x["v"] for x in hist) / max(1, len(hist))
        trs = self.tr[max(1, i - 14):i]
        atr = sum(trs) / 14 if len(trs) >= 14 else None
        high20, low20 = max(x["h"] for x in hist), min(x["l"] for x in hist)
        return {
            "price": p, "prevClose": prev, "movePct": (p / prev - 1) * 100 if prev else 0.0,
            "dayHigh": t["h"], "dayLow": t["l"], "upperHalf": p >= (t["h"] + t["l"]) / 2,
            "sma20": self.sma(i, 20), "sma50": self.sma(i, 50), "sma200": self.sma(i, 200),
            "sma50_5ago": self.sma(i, 50, 5) if i + 1 > 55 else None,
            "rsi": self.rsi[i], "atrPct": (atr / p * 100) if atr and p else None,
            "high20": high20, "low20": low20, "newHigh20": t["h"] > high20, "newLow20": t["l"] < low20,
            "volPace": (t["v"] / avg_vol) if avg_vol > 0 else 0.0, "avgDollarVol": avg_vol * p, "i": i,
        }

    def ohlc(self, i):
        return [[x["o"], x["h"], x["l"], x["c"]] for x in self.b[max(0, i - 100):i]]


# ---------------------------------------------------------------- the agent's rules, from daily bars
def regime_stocks(dv):
    if not dv or not dv["sma200"]:
        return "cautious"
    if dv["price"] < dv["sma200"] or dv["movePct"] < -2:
        return "risk-off"
    if dv["price"] > dv["sma50"] and dv["price"] > dv["sma200"]:
        return "risk-on"
    return "cautious"


def regime_crypto(dv):
    if not dv or not dv["sma200"] or dv["movePct"] < -6 or dv["price"] < dv["sma200"]:
        return "risk-off"
    return "risk-on" if dv["price"] > dv["sma50"] else "cautious"


def score(kind, side, dv, regime, v):
    tech = tech_score(dv, side)
    news = 12 if side == "long" else 13                 # no past headlines: neutral, as the live agent scores "no news"
    favour = {"long": "risk-on", "short": "risk-off"}[side]
    macro = 15 if regime == favour else 8 if regime in ("cautious", "neutral") else 0
    micro = min(15, 5 + (5 if dv["volPace"] >= 1.5 else 0) + 5)
    if kind in ("stock", "tsx") or not v["rescale"]:
        fund = 9                                         # no past company numbers: the live default
    else:
        fund = round(15 * (tech + macro + micro) / 60)   # funds, coins, futures have no company numbers at all
    total = tech + news + fund + macro + micro
    if regime == favour:
        need = RULES["score_min"]
    elif regime == "neutral" or regime == "cautious":
        need = 75 if regime == "neutral" else v["cautious_bar"]
    else:
        need = RULES["score_min_against"]
    return total, need


def setup(dv, side):
    mv = dv["movePct"]
    if side == "long":
        if mv > RULES["max_move_today_pct"]:
            return None
        if dv["newHigh20"] and dv["volPace"] >= 1.5 and dv["price"] <= dv["high20"] * 1.03:
            return "breakout"
        if (dv["sma50"] and dv["sma50_5ago"] and dv["price"] > dv["sma50"] and dv["sma50"] > dv["sma50_5ago"]
                and dv["sma20"] and abs(dv["price"] / dv["sma20"] - 1) <= 0.02 and dv["rsi"] and 40 <= dv["rsi"] <= 60 and mv > 0):
            return "pullback"
    else:
        if mv < -RULES["max_move_today_pct"]:
            return None
        if dv["newLow20"] and dv["volPace"] >= 1.5 and dv["price"] >= dv["low20"] * 0.97:
            return "breakdown"
        if (dv["sma50"] and dv["sma50_5ago"] and dv["price"] < dv["sma50"] and dv["sma50"] < dv["sma50_5ago"]
                and dv["sma20"] and abs(dv["price"] / dv["sma20"] - 1) <= 0.02 and dv["rsi"] and 40 <= dv["rsi"] <= 60 and mv < 0):
            return "rally_short"
    return None


def stop_dist(kind, dv, mult=0.5):
    return stop_distance(kind, dv["atrPct"], mult)


# ---------------------------------------------------------------- the simulation
def simulate(data, variant, start, end, equity0=10000.0, replays=1000):
    v = dict(BASE, **(variant if isinstance(variant, dict) else VARIANTS[variant]))
    risk_pct = v["risk"] or RULES["risk_pct"]
    ser = {s: Series(x["bars"]) for s, x in data.items()}
    days = sorted({b["d"] for x in data.values() for b in x["bars"] if start <= b["d"] <= end})
    us_days = sorted({b["d"] for b in data["SPY"]["bars"]}) if "SPY" in data else days
    cash, eq, peak, maxdd = equity0, equity0, equity0, 0.0
    pos, pending, trades, curve = {}, [], [], []
    halted = False
    day_start, week_start, week_key, week_entries = eq, eq, None, 0
    for d in days:
        wk = d.isocalendar()[:2]
        if wk != week_key:
            week_key, week_start, week_entries = wk, eq, 0
        day_start = eq
        entries_today = 0
        # 1) yesterday's signals fill at today's open
        for o in pending:
            x, s = data[o["sym"]], ser[o["sym"]]
            i = s.idx.get(d)
            if i is None or o["sym"] in pos:
                continue
            op = x["bars"][i]["o"]
            side, k = o["side"], o["kind"]
            fill = op * (1 + SLIP[k]) if side == "long" else op * (1 - SLIP[k])
            dist = o["dist"]
            stop = fill * (1 - dist) if side == "long" else fill * (1 + dist)
            target = fill * (1 + v["target_r"] * dist) if side == "long" else fill * (1 - v["target_r"] * dist)
            risk_cash = risk_pct * eq
            mult = x["mult"] or 1.0
            if k == "future":
                units = math.floor(risk_cash / (dist * fill * mult))
                margin = units * fill * mult * FUTURES[o["sym"]][2]
                if units < 1 or margin > RULES["future_margin_cap_pct"] * 0.7 * eq:
                    continue
                cost = margin
            else:
                sleeve = 0.3 * eq if k in ("crypto", "tsx") else 0.7 * eq
                cap = (RULES["crypto_cap_pct"] if k == "crypto" else RULES["equity_cap_pct"]) * sleeve
                value = min(risk_cash / dist, cap, cash * 0.98)
                if k in ("stock", "etf", "tsx"):
                    units = math.floor(value / fill)
                    if units < 1:
                        continue
                else:
                    units = value / fill
                cost = units * fill * (1.5 if side == "short" else 1.0)
            if cost > cash or units <= 0:
                continue
            cash -= cost
            pos[o["sym"]] = {"sym": o["sym"], "kind": k, "side": side, "setup": o["setup"], "entry": fill, "stop": stop,
                             "init": stop, "target": target, "units": units, "orig": units, "cost": cost, "mult": mult,
                             "d0": d, "held": 0, "scaled": False, "hi": fill, "lo": fill, "realised": 0.0, "score": o["score"]}
            entries_today += 1
            week_entries += 1
        pending = []
        # 2) manage what is held, with today's bar
        for sym in list(pos):
            p = pos[sym]
            i = ser[sym].idx.get(d)
            if i is None:
                continue
            b = data[sym]["bars"][i]
            first = p["d0"] == d
            if not first:
                p["held"] += 1
            exit_px, why = None, None
            sgn = 1 if p["side"] == "long" else -1
            r1 = abs(p["entry"] - p["init"])
            if v["exits"] == "scale" and not p["scaled"]:
                one_r = p["entry"] + sgn * r1
                if (sgn > 0 and b["h"] >= one_r) or (sgn < 0 and b["l"] <= one_r):
                    if not ((sgn > 0 and b["l"] <= p["stop"]) or (sgn < 0 and b["h"] >= p["stop"])):
                        half = p["units"] / 2 if p["kind"] == "crypto" else math.floor(p["units"] / 2)
                        if half > 0:
                            p["realised"] += _close_value(p, half, one_r)
                            p["units"] -= half
                        p["scaled"], p["stop"], p["target"] = True, p["entry"], None
            if sgn > 0:
                if not first and b["o"] <= p["stop"]:
                    exit_px, why = b["o"], "stop"
                elif b["l"] <= p["stop"]:
                    exit_px, why = p["stop"], "stop"
                elif p["target"] and b["h"] >= p["target"]:
                    exit_px, why = (b["o"] if not first and b["o"] >= p["target"] else p["target"]), "target"
            else:
                if not first and b["o"] >= p["stop"]:
                    exit_px, why = b["o"], "stop"
                elif b["h"] >= p["stop"]:
                    exit_px, why = p["stop"], "stop"
                elif p["target"] and b["l"] <= p["target"]:
                    exit_px, why = (b["o"] if not first and b["o"] <= p["target"] else p["target"]), "target"
            if exit_px is None and first and v["day_rule"] and p["kind"] in ("stock", "etf", "tsx"):
                dv = ser[sym].dv(i)
                ok = dv and dv["sma20"] and ((b["c"] > p["entry"] and b["c"] > dv["sma20"]) if sgn > 0 else
                                            (b["c"] < p["entry"] and b["c"] < dv["sma20"]))
                if not ok:
                    exit_px, why = b["c"], "day-trade rule"
            if exit_px is None and p["held"] > HOLD[p["kind"]]:
                exit_px, why = b["c"], "time"
            if exit_px is None:
                p["hi"], p["lo"] = max(p["hi"], b["h"]), min(p["lo"], b["l"])
                if v["exits"] == "scale" and p["scaled"]:
                    dv = ser[sym].dv(i)
                    atr = (dv["atrPct"] or 2.0) / 100 * b["c"] if dv else r1
                    trail = (p["hi"] - 2 * atr) if sgn > 0 else (p["lo"] + 2 * atr)
                    p["stop"] = max(p["stop"], trail) if sgn > 0 else min(p["stop"], trail)
                elif (b["c"] - p["entry"]) * sgn >= r1 and (p["stop"] - p["entry"]) * sgn < 0:
                    p["stop"] = p["entry"]
                continue
            px = exit_px * (1 - SLIP[p["kind"]]) if sgn > 0 else exit_px * (1 + SLIP[p["kind"]])
            got = _close_value(p, p["units"], px) + p["realised"]
            cash += _back_cash(p) + got
            pnl = got - _cost_basis(p)
            days_held = max(1, (d - p["d0"]).days)
            if sgn < 0 and p["kind"] in ("stock", "etf", "tsx"):
                pnl -= p["orig"] * p["entry"] * RULES["borrow_rate"] * days_held / 365
            risk0 = r1 * p["orig"] * p["mult"]
            trades.append({"sym": sym, "kind": p["kind"], "side": p["side"], "setup": p["setup"], "in": p["d0"].isoformat(),
                           "out": d.isoformat(), "pnl": round(pnl, 2), "r": round(pnl / risk0, 3) if risk0 else 0.0, "why": why})
            del pos[sym]
        # 3) the day's close: value, limits
        eq = cash + sum(_mark(p, data[p["sym"]]["bars"][ser[p["sym"]].idx[d]]["c"] if d in ser[p["sym"]].idx else p["entry"])
                        for p in pos.values())
        peak = max(peak, eq)
        maxdd = max(maxdd, (peak - eq) / peak)
        curve.append((d.isoformat(), round(eq, 2)))
        if eq <= equity0 * RULES["halt_pct"] and v["halt"]:
            halted = True
        if halted or (v["halt"] and (eq <= day_start * (1 - RULES["day_loss_pct"]) or eq <= week_start * (1 - RULES["week_loss_pct"]))):
            continue
        # 4) tonight's signals, for tomorrow's open
        spy = ser.get("SPY")
        btc = ser.get("BTC-USD")
        rs = regime_stocks(spy.dv(spy.idx[d])) if spy and d in spy.idx else "cautious"
        rc = regime_crypto(btc.dv(btc.idx[d])) if btc and d in btc.idx else "risk-off"
        cands = []
        for sym, x in data.items():
            k = x["kind"]
            i = ser[sym].idx.get(d)
            if i is None or sym in pos:
                continue
            if k != "crypto" and d not in us_days:
                continue
            dv = ser[sym].dv(i)
            if not dv or dv["price"] < 5 and k not in ("crypto", "future"):
                continue
            if k in ("stock", "etf", "tsx") and dv["avgDollarVol"] < (5e6 if k == "tsx" else 20e6):
                continue
            if k == "crypto" and dv["avgDollarVol"] < 5e6:
                continue
            regime = rc if k == "crypto" else ("neutral" if k == "future" and sym not in ("MES", "MNQ", "MYM", "M2K") else rs)
            for side in (("long",) if k in ("crypto", "tsx") else ("long", "short")):
                st = setup(dv, side)
                if not st or st in v.get("off", ()):
                    continue
                total, need = score(k, side, dv, regime, v)
                if total >= need:
                    cands.append((total, sym, side, st, dv, k))
        cands.sort(key=lambda c: -c[0])
        counts = {}
        for p in pos.values():
            counts[GROUP[p["kind"]]] = counts.get(GROUP[p["kind"]], 0) + 1
        open_risk = sum(abs(p["entry"] - p["stop"]) * p["units"] * p["mult"] for p in pos.values())
        n_new = 0
        for total, sym, side, st, dv, k in cands:
            if n_new + entries_today >= RULES["entries_per_day"] or n_new + week_entries >= RULES["entries_per_week"]:
                break
            g = GROUP[k]
            if counts.get(g, 0) >= RULES["max_pos"][g]:
                continue
            dist = stop_dist(k, dv, v["stop_atr"])
            price = dv["price"]
            stp = price * (1 - dist) if side == "long" else price * (1 + dist)
            tgt = price * (1 + v["target_r"] * dist) if side == "long" else price * (1 - v["target_r"] * dist)
            sleeve = (0.3 if k in ("crypto", "tsx") else 0.7) * eq
            cap = (RULES["crypto_cap_pct"] if k == "crypto" else RULES["equity_cap_pct"]) * sleeve
            est = risk_pct * eq if k == "future" else min(risk_pct * eq, cap * dist)
            if open_risk + est > RULES["max_open_risk_pct"] * eq + 1e-6:
                continue
            rp = replay({"bars": ser[sym].ohlc(dv["i"]), "entry": price, "stop": stp, "target": tgt, "side": side, "replays": replays,
                         "maxDays": {"crypto": 10, "future": 3}.get(k, 2), "costPct": {"crypto": 0.016, "future": 0.001}.get(k, 0.002)})
            if v["gate"] and not rp.get("favourable"):
                continue
            pending.append({"sym": sym, "side": side, "setup": st, "dist": dist, "kind": k, "score": total})
            counts[g] = counts.get(g, 0) + 1
            open_risk += est
            n_new += 1
    return {"trades": trades, "curve": curve, "maxdd": maxdd, "end": eq, "start": equity0, "halted": halted}


def _cost_basis(p):
    if p["kind"] == "future":
        return 0.0
    return p["orig"] * p["entry"]


def _back_cash(p):
    """Collateral and margin come back when a position closes."""
    if p["kind"] == "future":
        return p["cost"]
    if p["side"] == "short":
        return p["cost"] - p["orig"] * p["entry"]
    return 0.0


def _close_value(p, units, px):
    if p["kind"] == "future":
        return (px - p["entry"]) * units * p["mult"] * (1 if p["side"] == "long" else -1)
    if p["side"] == "long":
        return units * px
    return units * (2 * p["entry"] - px)


def _mark(p, px):
    if p["kind"] == "future":
        return p["cost"] + (px - p["entry"]) * p["units"] * p["mult"] * (1 if p["side"] == "long" else -1) + p["realised"]
    if p["side"] == "long":
        return p["units"] * px + p["realised"]
    return p["cost"] - p["orig"] * p["entry"] + p["units"] * (2 * p["entry"] - px) + p["realised"]


def summarise(res, label=""):
    t = res["trades"]
    n = len(t)
    wins = sum(1 for x in t if x["pnl"] > 0)
    rs = [x["r"] for x in t]
    by = {}
    for x in t:
        b = by.setdefault(x["setup"], {"n": 0, "wins": 0, "r": 0.0})
        b["n"] += 1
        b["wins"] += 1 if x["pnl"] > 0 else 0
        b["r"] = round(b["r"] + x["r"], 2)
    why = {}
    for x in t:
        why[x["why"]] = why.get(x["why"], 0) + 1
    kinds = {}
    for x in t:
        b = kinds.setdefault(x["kind"], {"n": 0, "r": 0.0})
        b["n"] += 1
        b["r"] = round(b["r"] + x["r"], 2)
    half = len(res["curve"]) // 2
    first = [x for x in t if res["curve"] and x["in"] <= res["curve"][half][0]] if half else []
    second = [x for x in t if x not in first]
    return {"variant": label, "trades": n, "winRate": round(wins / n * 100, 1) if n else 0.0,
            "avgR": round(sum(rs) / n, 3) if n else 0.0, "totalR": round(sum(rs), 2),
            "returnPct": round((res["end"] / res["start"] - 1) * 100, 2), "maxDrawdownPct": round(res["maxdd"] * 100, 2),
            "halted": res["halted"], "bySetup": by, "byKind": kinds, "exits": why,
            "firstHalfAvgR": round(sum(x["r"] for x in first) / len(first), 3) if first else 0.0,
            "secondHalfAvgR": round(sum(x["r"] for x in second) / len(second), 3) if second else 0.0}


# ---------------------------------------------------------------- what to test, and running it
CRYPTO = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "DOT", "LTC", "BCH", "XLM", "NEAR", "ATOM",
          "UNI", "ETC", "HBAR", "FIL", "AAVE"]


def build_universe(state, cfg, n_stocks=250, n_etfs=50, liquid=None):
    """Today's most traded US stocks and funds, the TSX list, the top coins and the micro futures."""
    from core import stock_universe, yahoo_spark_daily, daily_view
    import tempfile
    out = [("SPY", "etf", "SPY", None), ("BTC-USD", "crypto", "BTC-USD", None)]
    if liquid is None:
        work = tempfile.mkdtemp()
        syms = [s for s, _name in stock_universe(work) if s.isalpha() and len(s) <= 5]
        spark = yahoo_spark_daily(syms, None, "1mo", pause=0.15)
        rows = []
        for s, ch in spark.items():
            b = ch["bars"]
            if len(b) < 10 or b[-1]["c"] < 5:
                continue
            m = ch.get("meta") or {}
            rows.append((float(m.get("regularMarketVolume") or 0) * b[-1]["c"], s))
        liquid = [s for _, s in sorted(rows, reverse=True)]
    etfs = set(state.get("etfUniverse") or []) | set(cfg.get("etfs") or [])
    stocks = [s for s in liquid if s not in etfs][:n_stocks]
    out += [(s, "stock", s, None) for s in stocks]
    out += [(s, "etf", s, None) for s in list(state.get("etfUniverse") or cfg.get("etfs") or [])[:n_etfs] if s != "SPY"]
    out += [(s + ".TO", "tsx", s + ".TO", None) for s in list(cfg.get("tsx", [])) + list(cfg.get("tsx_etfs", []))]
    out += [(c + "-USD", "crypto", c + "-USD", None) for c in CRYPTO if c != "BTC"]
    out += [(code, "future", y, mult) for code, (y, mult, mfrac, name) in FUTURES.items() if code not in ("MBT", "MET")]
    return out


def run(state, cfg, years=2, variants=None, data=None, log_each=True):
    """Every variant over the same past prices. Returns {"summary": [...], "curves": {...}, "asOf": ...}."""
    import time
    t0 = time.time()
    if data is None:
        data = load(build_universe(state, cfg), rng="%dy" % (years + 1))
    if "SPY" not in data:
        return {"error": "no S&P 500 fund prices"}
    spy_days = [b["d"] for b in data["SPY"]["bars"]]
    end = spy_days[-1]
    start = max(spy_days[210], end - dt.timedelta(days=int(365.25 * years)))
    out = {"asOf": end.isoformat(), "from": start.isoformat(), "symbols": len(data),
           "kinds": {k: sum(1 for x in data.values() if x["kind"] == k) for k in KINDS}, "summary": [], "curves": {}}
    for name in (variants or list(VARIANTS)):
        res = simulate(data, name, start, end)
        sm = summarise(res, name)
        out["summary"].append(sm)
        step = max(1, len(res["curve"]) // 120)
        out["curves"][name] = res["curve"][::step] + ([res["curve"][-1]] if res["curve"] and len(res["curve"]) % step != 1 else [])
        out.setdefault("trades", {})[name] = res["trades"][-40:]
        if log_each:
            log("Backtest %-18s %4d trades, win %5.1f%%, avg %+.3fR, return %+.1f%%, worst drop %.1f%%" % (
                name, sm["trades"], sm["winRate"], sm["avgR"], sm["returnPct"], sm["maxDrawdownPct"]))
    out["seconds"] = round(time.time() - t0)
    return out


if __name__ == "__main__":
    import json
    import sys
    st = json.load(open(sys.argv[1])) if len(sys.argv) > 1 else {}
    cf = json.load(open(sys.argv[2])) if len(sys.argv) > 2 else {}
    res = run(st, cf)
    json.dump(res, open(sys.argv[3] if len(sys.argv) > 3 else "backtest.json", "w"), indent=1, default=str)
