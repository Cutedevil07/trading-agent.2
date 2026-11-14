#!/usr/bin/env python3
"""$100 Trading Agent - the Claude-free edition.

A paper-trading agent (pretend money only) that runs on its own, twice each
weekday, with no AI and no paid services. It uses free public data (Yahoo
Finance, Nasdaq, CoinGecko, the House Clerk), the open-source Day Trader skill
pack (vendored in pack/, MIT, commit 3d0f4ef), and fixed written rules.

Standard-library Python 3.8+ only, so it runs on an iPhone (a-Shell), a
laptop or GitHub Actions.

    python3 agent.py run                 # one run (OPEN before 13:00 New York, CLOSE after)
    python3 agent.py run --dry-run       # do everything, save nothing, send nothing
    python3 agent.py selftest            # check every data source and the alert
    python3 agent.py run --state FILE    # keep state in a local file instead of GitHub

It never places a real order and has no way to: there is no broker code here.
"""
import argparse
import base64
import contextlib
import datetime as dt
import importlib.util
import io
import json
import math
import os
import random
import re
import shutil
import statistics
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PACK = os.path.join(HERE, "pack")
PACK_COMMIT = "3d0f4ef6986f4984b7c6bd46f7ff4f87744c3abd"
UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/18.0 Safari/605.1.15")
STATE_PATH = "docs/state.json"

# ---------------------------------------------------------------- the rules
RULES = {
    "risk_pct": 0.01,             # risk 1% of the account per trade
    "score_min": 70, "score_min_cautious": 80,
    "max_up_today_pct": 10.0,     # never chase
    "entries_per_day": 2, "entries_per_week": 6,
    "max_stock_positions": 3, "max_crypto_positions": 2,
    "stock_cap_pct": 0.40, "crypto_cap_pct": 0.60,
    "day_loss_pct": 0.02, "week_loss_pct": 0.05, "halt_equity": 85.0,
    "hold_days_stock": 5, "hold_days_crypto": 10,
    "fx_cost": 0.015, "stock_slip": 0.001, "crypto_slip": 0.008,
    "alert_max": 6, "alert_days": 5, "alerts_per_run": 3,
    "paper_days_goal": 20, "max_drawdown_goal": 15.0,
}
SETUPS = ("pullback", "breakout", "news")

LOG = []
CALLS = {"n": 0}


def log(*a):
    line = " ".join(str(x) for x in a)
    LOG.append(line)
    print(line, flush=True)


# ---------------------------------------------------------------- time (no tz files needed)
def utcnow():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _nth_sunday(y, m, n):
    d = dt.date(y, m, 1)
    first = d + dt.timedelta(days=(6 - d.weekday()) % 7)
    return first + dt.timedelta(weeks=n - 1)


def ny_offset_hours(utc):
    start = dt.datetime.combine(_nth_sunday(utc.year, 3, 2), dt.time(7, 0))   # 2:00 EST
    end = dt.datetime.combine(_nth_sunday(utc.year, 11, 1), dt.time(6, 0))    # 2:00 EDT
    return -4 if start <= utc < end else -5


def to_ny(utc):
    return utc + dt.timedelta(hours=ny_offset_hours(utc))


def from_ny(local):
    guess = local + dt.timedelta(hours=5)
    return local - dt.timedelta(hours=ny_offset_hours(guess))


def iso(utc):
    return utc.replace(microsecond=0).isoformat() + "Z"


def parse_iso(s):
    if not s:
        return None
    s = str(s).replace("Z", "")
    if "+" in s[10:]:
        s = s[:10] + s[10:].split("+")[0]
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        return None


HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
    "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
    "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}
EARLY_CLOSE = {"2026-11-27", "2026-12-24", "2027-11-26"}
FOMC_DAYS = {"2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29",
             "2026-09-16", "2026-10-28", "2026-12-09"}


def is_trading_day(d):
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS


def trading_days_between(d0, d1):
    """Trading days after d0 up to and including d1."""
    n, d = 0, d0
    while d < d1:
        d += dt.timedelta(days=1)
        if is_trading_day(d):
            n += 1
    return n


def add_trading_days(d, n):
    while n > 0:
        d += dt.timedelta(days=1)
        if is_trading_day(d):
            n -= 1
    return d


def next_trading_day(d):
    return add_trading_days(d, 1)


# ---------------------------------------------------------------- http
def http(url, headers=None, data=None, method=None, timeout=25, tries=2):
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    err = None
    for i in range(tries):
        CALLS["n"] += 1
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            body = e.read()
            if e.code in (429, 500, 502, 503, 504) and i + 1 < tries:
                time.sleep(4)
                continue
            return e.code, body
        except Exception as e:  # network error
            err = e
            if i + 1 < tries:
                time.sleep(2)
    raise ConnectionError(str(err)[:160])


def get_json(url, headers=None, timeout=25):
    try:
        s, b = http(url, headers=headers, timeout=timeout)
    except ConnectionError as e:
        return None, str(e)
    if s != 200:
        return None, "HTTP %s" % s
    try:
        return json.loads(b), None
    except ValueError:
        return None, "not JSON"


# ---------------------------------------------------------------- Yahoo
_YCACHE = {}


def yahoo_chart(sym, rng, interval, now=None):
    key = (sym, rng, interval)
    if key in _YCACHE:
        return _YCACHE[key]
    q = urllib.parse.urlencode({"range": rng, "interval": interval, "includePrePost": "false"})
    out = None
    for host in ("query2", "query1"):
        url = "https://%s.finance.yahoo.com/v8/finance/chart/%s?%s" % (host, urllib.parse.quote(sym), q)
        d, err = get_json(url, {"Accept": "application/json"})
        time.sleep(0.3)
        if d and (d.get("chart") or {}).get("result"):
            out = _parse_chart(d["chart"]["result"][0], now)
            break
    _YCACHE[key] = out
    return out


def yahoo_spark_daily(symbols, now=None):
    """Daily closes for up to 20 symbols per request (a cheap first screen)."""
    out = {}
    for i in range(0, len(symbols), 20):
        chunk = symbols[i:i + 20]
        q = urllib.parse.urlencode({"symbols": ",".join(chunk), "range": "1y", "interval": "1d"})
        d = None
        for host in ("query2", "query1"):
            d, err = get_json("https://%s.finance.yahoo.com/v7/finance/spark?%s" % (host, q), {"Accept": "application/json"})
            time.sleep(0.3)
            if d and (d.get("spark") or {}).get("result"):
                break
        for r in ((d or {}).get("spark") or {}).get("result") or []:
            resp = (r.get("response") or [None])[0]
            if not resp:
                continue
            m = resp.get("meta") or {}
            ts = resp.get("timestamp") or []
            closes = (((resp.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
            cut = (now - dt.datetime(1970, 1, 1)).total_seconds() if now else None
            bars = [{"t": t, "o": c, "h": c, "l": c, "c": c, "v": 0.0} for t, c in zip(ts, closes)
                    if c is not None and (cut is None or t <= cut)]
            if not bars:
                continue
            if cut is None and m.get("regularMarketDayHigh"):
                bars[-1].update({"h": float(m["regularMarketDayHigh"]), "l": float(m["regularMarketDayLow"])})
            price = bars[-1]["c"] if cut is not None else float(m.get("regularMarketPrice") or bars[-1]["c"])
            out[r.get("symbol")] = {"bars": bars, "meta": m, "price": price}
    return out


def _parse_chart(r, now=None):
    ts = r.get("timestamp") or []
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    cols = {k: q.get(k) or [None] * len(ts) for k in ("open", "high", "low", "close", "volume")}
    cut = (now - dt.datetime(1970, 1, 1)).total_seconds() if now else None
    bars = []
    for i, t in enumerate(ts):
        o, h, l, c = cols["open"][i], cols["high"][i], cols["low"][i], cols["close"][i]
        if None in (o, h, l, c) or (cut is not None and t > cut):
            continue
        bars.append({"t": t, "o": float(o), "h": float(h), "l": float(l), "c": float(c),
                     "v": float(cols["volume"][i] or 0)})
    m = r.get("meta") or {}
    price = m.get("regularMarketPrice")
    if cut is not None and bars:      # replaying a past moment: the last bar is the price
        price = bars[-1]["c"]
    return {"bars": bars, "meta": m, "price": float(price) if price else (bars[-1]["c"] if bars else None)}


def bar_day(t, crypto=False):
    u = dt.datetime(1970, 1, 1) + dt.timedelta(seconds=t)
    return (u if crypto else to_ny(u)).date()


def daily_view(chart, today, crypto=False, frac_elapsed=1.0):
    """Indicators from daily bars. The last bar is today's (partial during the session)."""
    if not chart or len(chart["bars"]) < 30:
        return None
    bars = [b for b in chart["bars"] if bar_day(b["t"], crypto) <= today]
    if len(bars) < 30:
        return None
    price = chart["price"] or bars[-1]["c"]
    is_today = bar_day(bars[-1]["t"], crypto) == today
    hist = bars[:-1] if is_today else bars
    t_bar = bars[-1] if is_today else None
    closes = [b["c"] for b in hist] + [price]
    prev_close = hist[-1]["c"]
    day_high = max(t_bar["h"], price) if t_bar else price
    day_low = min(t_bar["l"], price) if t_bar else price
    vol_today = t_bar["v"] if t_bar else 0.0
    avg_vol = sum(b["v"] for b in hist[-20:]) / max(1, len(hist[-20:]))
    pace = (vol_today / max(frac_elapsed, 0.1)) / avg_vol if avg_vol > 0 else 0.0

    def sma(xs, n):
        return sum(xs[-n:]) / n if len(xs) >= n else None
    trs = [max(b["h"] - b["l"], abs(b["h"] - a["c"]), abs(b["l"] - a["c"])) for a, b in zip(hist[:-1], hist[1:])]
    atr = sum(trs[-14:]) / 14 if len(trs) >= 14 else None
    return {
        "price": price, "prevClose": prev_close,
        "upTodayPct": (price / prev_close - 1) * 100 if prev_close else 0.0,
        "dayHigh": day_high, "dayLow": day_low,
        "upperHalf": price >= (day_high + day_low) / 2,
        "sma20": sma(closes, 20), "sma50": sma(closes, 50), "sma200": sma(closes, 200),
        "sma50_5ago": sma(closes[:-5], 50) if len(closes) > 55 else None,
        "rsi": rsi14(closes), "atrPct": (atr / price * 100) if atr and price else None,
        "high20": max(b["h"] for b in hist[-20:]), "low20": min(b["l"] for b in hist[-20:]),
        "newHigh20": day_high > max(b["h"] for b in hist[-20:]),
        "volPace": pace, "ohlc": [[b["o"], b["h"], b["l"], b["c"]] for b in hist[-100:]],
    }


def rsi14(closes, n=14):
    if len(closes) < n + 1:
        return None
    g = l = 0.0
    for i in range(1, n + 1):
        ch = closes[i] - closes[i - 1]
        g += max(ch, 0.0)
        l += max(-ch, 0.0)
    ag, al = g / n, l / n
    for i in range(n + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(ch, 0.0)) / n
        al = (al * (n - 1) + max(-ch, 0.0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


# ---------------------------------------------------------------- optional free sources
CG_EXCLUDE = {
    "usdt", "usdc", "dai", "usde", "fdusd", "pyusd", "usds", "tusd", "usdd", "busd", "usd1", "rlusd", "susde",
    "wbtc", "weth", "steth", "wsteth", "weeth", "cbbtc", "reth", "bsc-usd", "wbeth", "jitosol", "lbtc",
    "doge", "shib", "pepe", "trump", "bonk", "wif", "floki", "leo", "figr_heloc",
}
CRYPTO_FALLBACK = ["BTC", "ETH", "XRP", "BNB", "SOL", "TRX", "ADA"]


def top_coins(now):
    url = "https://api.coingecko.com/api/v3/coins/markets?vs_currency=cad&order=market_cap_desc&per_page=15&page=1"
    d, err = get_json(url, {"Accept": "application/json"})
    if not d or not isinstance(d, list):
        log("CoinGecko unavailable (%s): using the fixed coin list" % err)
        return CRYPTO_FALLBACK, "fixed list"
    top = [c for c in d if c.get("market_cap_rank") and c["market_cap_rank"] <= 10]
    syms = [str(c.get("symbol", "")).upper() for c in top if str(c.get("symbol", "")).lower() not in CG_EXCLUDE]
    return syms or CRYPTO_FALLBACK, "CoinGecko"


def _money(s):
    if s in (None, "", "N/A", "--"):
        return None
    s = str(s).replace("$", "").replace(",", "").strip()
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def nasdaq_earnings(day):
    url = "https://api.nasdaq.com/api/calendar/earnings?date=%s" % day.isoformat()
    d, err = get_json(url, {"User-Agent": BROWSER_UA, "Accept": "application/json, text/plain, */*",
                            "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}, timeout=20)
    time.sleep(0.4)
    if not d or "data" not in d:
        return None
    rows = (d.get("data") or {}).get("rows") or []
    out = {}
    for r in rows:
        sym = str(r.get("symbol", "")).upper().strip()
        if not sym:
            continue
        timing = str(r.get("time", ""))
        out[sym] = {
            "timing": "before-open" if "pre" in timing else ("after-close" if "after" in timing else "unknown"),
            "eps": _money(r.get("eps")), "epsForecast": _money(r.get("epsForecast")),
            "surprise": r.get("surprise"),
        }
    return out


# ---------------------------------------------------------------- the Day Trader pack, in-process
def pack(tool, argv, cwd):
    path = os.path.join(PACK, tool)
    name = "dts_%s_%d" % (tool.replace(".", "_"), time.time_ns())
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    out, err = io.StringIO(), io.StringIO()
    old = os.getcwd()
    os.chdir(cwd)
    code = None
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            spec.loader.exec_module(mod)
            try:
                code = mod.main(argv)
            except SystemExit as e:
                code = e.code
    except Exception as e:
        err.write("%s: %s" % (type(e).__name__, e))
        code = 99
    finally:
        os.chdir(old)
    text = out.getvalue()
    data = None
    if "--json" in argv:
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    return code, data, (text + err.getvalue())[-600:]


# ---------------------------------------------------------------- replay and order check (from the Claude version)
def _pool(bars):
    if bars and not isinstance(bars[0], (list, tuple)):
        closes = [float(c) for c in bars]
        rets = [abs(closes[i] / closes[i - 1] - 1) for i in range(1, len(closes))]
        k = 0.5 * statistics.median(rets) if rets else 0.0
        return [(1.0, max(pc, c) * (1 + k) / pc, min(pc, c) * (1 - k) / pc, c / pc)
                for pc, c in zip(closes[:-1], closes[1:])], "closes"
    out = []
    for i in range(1, len(bars)):
        pc = float(bars[i - 1][3])
        o, h, l, c = (float(v) for v in bars[i][:4])
        if pc > 0 and l <= min(o, c) and h >= max(o, c):
            out.append((o / pc, h / pc, l / pc, c / pc))
    return out, "ohlc"


def replay(p):
    entry, stop, target = float(p["entry"]), float(p["stop"]), float(p["target"])
    if not (stop < entry < target):
        return {"error": "need stop < entry < target"}
    pool, kind = _pool(p["bars"])
    if len(pool) < 40:
        return {"error": "need at least 40 days of prices, got %d" % len(pool)}
    n, days = int(p.get("replays", 10000)), int(p.get("maxDays", 5))
    risk = entry - stop
    cost_r = float(p.get("costPct", 0.002)) * entry / risk
    rng = random.Random(int(p.get("seed", 19)))
    counts = {"target": 0, "stop": 0, "time": 0}
    rs = []
    for _ in range(n):
        px, how, exit_px = entry, "time", None
        for _d in range(days):
            ro, rh, rl, rc = pool[rng.randrange(len(pool))]
            o, h, l, c = px * ro, px * rh, px * rl, px * rc
            if o <= stop:
                how, exit_px = "stop", o
            elif o >= target:
                how, exit_px = "target", o
            elif l <= stop:
                how, exit_px = "stop", stop
            elif h >= target:
                how, exit_px = "target", target
            if exit_px is not None:
                break
            px = c
        if exit_px is None:
            exit_px = px
        counts[how] += 1
        rs.append((exit_px - entry) / risk - cost_r)
    rs.sort()
    mean_r = round(sum(rs) / len(rs), 3)
    p_stop = round(counts["stop"] / n, 3)
    return {"replays": n, "daysHeld": days, "poolDays": len(pool), "data": kind,
            "pTarget": round(counts["target"] / n, 3), "pStop": p_stop, "pTime": round(counts["time"] / n, 3),
            "meanR": mean_r, "favourable": mean_r >= 0.05 and p_stop <= 0.60}


def order_check(o):
    rules = []

    def add(name, ok, detail):
        rules.append({"rule": name, "ok": bool(ok), "detail": detail})

    def g(k, d=0.0):
        return float(o.get(k, d) or 0)
    entry, stop, target = g("entry"), g("stop"), g("target")
    value, risk, cost = g("valueCad"), g("riskCad"), g("costPct", 0.002)
    loss = value * (entry - stop) / entry + value * cost if entry > 0 else 9e9
    rr = (target - entry) / (entry - stop) if entry > stop else 0
    rep = o.get("replay") or {}
    day_used = max(0.0, g("dayStartEquityCad") - g("equityCad"))
    add("Buying allowed now", o.get("buyingAllowed"), o.get("blockReason") or "risk gate open")
    add("Allowed market", o.get("universeOk"), str(o.get("symbol", "")))
    add("Not already held", not o.get("alreadyHeld"), "one position per symbol")
    add("Score high enough", g("score") >= g("scoreMin", 70), "%g of %g needed" % (g("score"), g("scoreMin", 70)))
    add("Setup valid", o.get("setup") in SETUPS and not o.get("setupDisabled"), str(o.get("setup")))
    add("No earnings block", not o.get("earningsBlocked"), "report not due, no recent miss")
    add("Not chasing", g("upTodayPct") <= 10, "up %.1f%% today, limit 10%%" % g("upTodayPct"))
    add("Stop below entry", 0 < stop < entry, "stop %.4g, entry %.4g" % (stop, entry))
    add("Reward is 2x risk", rr >= 1.99, "%.2f to 1" % rr)
    add("Loss at stop fits budget", loss <= 1.5 * risk, "CAD %.2f at stop with costs, budget %.2f" % (loss, risk))
    add("Order size", 5 <= value <= g("sleeveCapCad"), "CAD %.2f, allowed 5 to %.2f" % (value, g("sleeveCapCad")))
    add("Cash available", value * (1 + cost / 2) <= g("sleeveCashCad"),
        "needs CAD %.2f, has %.2f" % (value * (1 + cost / 2), g("sleeveCashCad")))
    add("Position count", g("positionsInSleeve") < g("maxPositionsInSleeve"),
        "%d of %d" % (g("positionsInSleeve"), g("maxPositionsInSleeve")))
    add("Trades today and this week", g("entriesToday") < 2 and g("entriesThisWeek") < 6,
        "%d today, %d this week" % (g("entriesToday"), g("entriesThisWeek")))
    add("Daily loss limit", day_used + loss <= 0.02 * g("dayStartEquityCad"),
        "CAD %.2f lost today + %.2f at stop, limit %.2f" % (day_used, loss, 0.02 * g("dayStartEquityCad")))
    add("Time machine favourable", rep.get("favourable"),
        "avg %+.2fR, stop first %.0f%%" % (float(rep.get("meanR", 0) or 0), 100 * float(rep.get("pStop", 1) or 1)))
    failed = [r["rule"] for r in rules if not r["ok"]]
    return {"verdict": "BLOCK" if failed else "PASS", "rules": rules, "failed": failed,
            "lossAtStopCad": round(loss, 2), "rewardRisk": round(rr, 2)}


# ---------------------------------------------------------------- news reading (rules instead of judgment)
POS_WORDS = re.compile(r"\b(beat|beats|tops|topped|raises|raised|lifts|upgrade[sd]?|record|wins|won|awarded|contract|"
                       r"approval|approved|clears|partnership|buyback|surges?|soars?|jumps?|rall(y|ies)|outperform|"
                       r"strong demand|guidance (raise|hike))\b", re.I)
NEG_WORDS = re.compile(r"\b(miss|misses|missed|cuts?|downgrade[sd]?|lawsuits?|sued|sues|probes?|investigations?|"
                       r"recalls?|halt(ed)?|offering|plunges?|slumps?|tumbles?|sinks?|warns?|warning|bankruptcy|"
                       r"fraud|delay(s|ed)?|layoffs?|resigns?|subpoena|short seller|downbeat|settlement|fined|ban(s|ned)?)\b", re.I)
SERIOUS = re.compile(r"\b(lawsuits?|sued|probes?|investigations?|recalls?|halt(ed)?|bankruptcy|fraud|subpoena|fined|"
                     r"short seller|resigns?|ban(s|ned)?)\b", re.I)
CATALYST_TAGS = {"earnings", "analyst", "deal", "regulatory"}


def about(title, sym, aliases):
    """True when a headline names this company or coin (Yahoo's feeds mix in other stories)."""
    if re.search(r"\(%s\)" % re.escape(sym), title) or (len(sym) >= 3 and re.search(r"\b%s\b" % re.escape(sym), title)):
        return True
    alias = aliases.get(sym)
    return bool(alias and re.search(r"\b(%s)" % alias, title, re.I))


NEWS_MODEL_ID = "mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis"
NEWS_MODEL_REV = "ae0eab9ad336d7d548e0efe394b07c04bcaf6e91"
_NEWS_MODEL = None


def news_model():
    """A free, open-source finance-news sentiment model (Apache-2.0), run locally on GitHub's computer.
    Falls back to word rules where it is not installed (for example on a phone)."""
    global _NEWS_MODEL
    if _NEWS_MODEL is None:
        try:
            os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
            os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
            from transformers import pipeline
            _NEWS_MODEL = pipeline("text-classification", model=NEWS_MODEL_ID, revision=NEWS_MODEL_REV,
                                   top_k=None, device=-1)
            log("News model loaded")
        except Exception as e:
            log("News model not available (%s): using word rules" % str(e)[:80])
            _NEWS_MODEL = False
    return _NEWS_MODEL


def classify_texts(texts):
    """{text: (positive, negative)} probabilities from the model, or from word rules."""
    out = {}
    model = news_model() if texts else False
    if model:
        try:
            res = model(list(texts), truncation=True, max_length=256, batch_size=16)
            for t, r in zip(texts, res):
                sc = {d["label"].lower(): d["score"] for d in r}
                out[t] = (sc.get("positive", 0.0), sc.get("negative", 0.0))
            return out
        except Exception as e:
            log("News model failed (%s): using word rules" % str(e)[:80])
    for t in texts:
        head = t.split(" || ")[0]
        p, n = bool(POS_WORDS.search(head)), bool(NEG_WORDS.search(head))
        out[t] = (0.8 if p and not n else 0.1, 0.8 if n and not p else 0.1)
    return out


def news_text(it):
    title = str(it.get("title", "")).strip()
    summ = re.sub(r"\s+", " ", str(it.get("summary") or "")).strip()
    return title + (" || " + summ[:240] if summ else "")


def read_news(items, sym, aliases=None, feed=None, scores=None):
    """Judge the last 12 hours of headlines about one company or coin."""
    feed = feed or sym
    rel = []
    for it in items:
        if feed not in (it.get("touches") or [it.get("ticker")]):
            continue
        if about(str(it.get("title", "")), sym, aliases or {}):
            rel.append(it)
    rel.sort(key=lambda it: it.get("time") or "", reverse=True)
    rel = rel[:6]
    scores = scores if scores is not None else classify_texts([news_text(it) for it in rel])
    pos = neg = serious = 0
    net = 0.0
    catalyst = False
    for it in rel:
        title = str(it.get("title", ""))
        p, n = scores.get(news_text(it), (0.0, 0.0))
        net += p - n
        tags = set(it.get("tags") or [])
        if p >= 0.6:
            pos += 1
            if p >= 0.7 and (tags & CATALYST_TAGS or re.search(r"contract|approv|guidance|beat|upgrade|record|deal", title, re.I)):
                catalyst = True
        elif n >= 0.6:
            neg += 1
            if SERIOUS.search(title) or tags & {"legal", "halt", "risk"}:
                serious += 1
    score = int(round(max(0, min(25, 12 + 5 * max(-2.4, min(2.6, net))))))
    return {"score": score, "pos": pos, "neg": neg, "serious": serious, "catalyst": catalyst,
            "headlines": [str(it.get("title", "")) for it in rel[:3]],
            "broken": pos == 0 and (neg >= 2 or serious >= 1)}


# ---------------------------------------------------------------- fundamentals (Nasdaq, free)
NASDAQ_H = {"User-Agent": BROWSER_UA, "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}


def _num(s):
    m = re.search(r"-?[\d,]+(\.\d+)?", str(s or "").replace("(", "-").replace(")", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def nasdaq_fundamentals(sym):
    """Quarterly revenue and EPS by fiscal year, and analysts' 1-year target, from Nasdaq's public pages."""
    d, err = get_json("https://api.nasdaq.com/api/company/%s/revenue?limit=1" % sym, NASDAQ_H)
    time.sleep(0.3)
    if not d or not (d.get("data") or {}).get("revenueTable"):
        return None
    rows = d["data"]["revenueTable"].get("rows") or []
    quarters, month, eps_points = [], None, []
    for r in rows:
        label = str(r.get("value1", "")).strip()
        if label and label.lower() not in ("revenue", "eps", "dividends"):
            month = label
            continue
        if label.lower() == "revenue" and month:
            cur, prev = _num(r.get("value2")), _num(r.get("value3"))
            if cur is not None:
                quarters.append({"month": month, "rev": cur, "revPrev": prev})
        if label.lower() == "eps":
            for k in ("value2", "value3", "value4"):
                v = str(r.get(k) or "")
                m = re.search(r"\((\d{2})/(\d{2})/(\d{4})\)", v)
                if m and _num(v.split("(")[0]) is not None:
                    eps_points.append((dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2))), _num(v.split("(")[0])))
    eps_points.sort()
    out = {"fetchedOn": utcnow().date().isoformat()}
    if quarters:
        q = quarters[-1]
        out["revGrowthPct"] = round((q["rev"] / q["revPrev"] - 1) * 100, 1) if q.get("revPrev") else None
    if len(eps_points) >= 4:
        out["ttmEps"] = round(sum(e for _, e in eps_points[-4:]), 2)
        last = eps_points[-1]
        year_ago = [e for dte, e in eps_points if 350 <= (last[0] - dte).days <= 380]
        out["lastEps"] = last[1]
        out["epsGrowthPct"] = round((last[1] / year_ago[0] - 1) * 100, 1) if year_ago and year_ago[0] > 0 else None
    s, err = get_json("https://api.nasdaq.com/api/quote/%s/summary?assetclass=stocks" % sym, NASDAQ_H)
    time.sleep(0.3)
    sd = ((s or {}).get("data") or {}).get("summaryData") or {}
    out["target1y"] = _num((sd.get("OneYrTarget") or {}).get("value"))
    out["sector"] = (sd.get("Sector") or {}).get("value")
    return out


def fundamental_score(f, price):
    """0-15 from real numbers: growth (6), profit (5), valuation and analysts (4). None when not available."""
    if not f or (f.get("revGrowthPct") is None and f.get("ttmEps") is None):
        return None, "not available"
    g = f.get("revGrowthPct")
    growth = 0 if g is None else (6 if g >= 15 else 4 if g >= 5 else 2 if g >= 0 else 0)
    profit = 0
    if f.get("lastEps") is not None and f["lastEps"] > 0:
        profit += 2
        eg = f.get("epsGrowthPct")
        profit += 3 if eg is not None and eg >= 10 else 1 if eg is not None and eg >= 0 else 0
    val = 0
    pe = price / f["ttmEps"] if f.get("ttmEps") and f["ttmEps"] > 0 and price else None
    if pe is not None:
        val += 2 if pe <= 35 else 1 if pe <= 60 else 0
    tgt = f.get("target1y")
    if tgt and price:
        up = (tgt / price - 1) * 100
        val += 2 if up >= 10 else 1 if up >= 0 else 0
    parts = []
    if g is not None:
        parts.append("sales %+.0f%%" % g)
    if pe is not None:
        parts.append("P/E %.0f" % pe)
    if tgt and price:
        parts.append("target %+.0f%%" % ((tgt / price - 1) * 100))
    return min(15, growth + profit + min(4, val)), ", ".join(parts)


def ensure_pdftotext():
    """The Congress reports are PDFs. On GitHub's computer, install the free reader once when needed."""
    if shutil.which("pdftotext"):
        return True
    if os.environ.get("AGENT_RUNNER") != "github" or not shutil.which("sudo"):
        return False
    import subprocess
    for cmd in (["sudo", "apt-get", "install", "-y", "-qq", "poppler-utils"],
                ["sudo", "apt-get", "update", "-qq"], ["sudo", "apt-get", "install", "-y", "-qq", "poppler-utils"]):
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        except Exception:
            pass
        if shutil.which("pdftotext"):
            return True
    return bool(shutil.which("pdftotext"))


# ---------------------------------------------------------------- state storage
def empty_state(start=100.0):
    return {
        "schema": 1,
        "fund": {"phase": "paper", "startEquityCad": start, "stocksCashCad": start * 0.7, "stocksCashUsd": 0.0,
                 "cryptoCashCad": start * 0.3, "equityCad": start, "peakEquityCad": start, "maxDrawdownPct": 0.0,
                 "benchmarkCad": start, "benchmarkSet": False, "tradingDays": 0, "lastTradingDay": "",
                 "dayDate": "", "dayStartEquityCad": start, "entriesToday": 0, "weekStart": "",
                 "weekStartEquityCad": start, "entriesThisWeek": 0, "halted": False, "haltReason": "",
                 "ruleBreaks": 0, "stats": {"closed": 0, "wins": 0, "totalR": 0.0,
                                            "bySetup": {k: {"n": 0, "wins": 0, "r": 0.0, "disabled": False} for k in SETUPS}}},
        "lessons": [], "positions": {}, "trades": [], "nav": [], "journal": [],
        "alerts": {}, "earnings": {}, "congress": {}, "runs": {},
    }


class LocalStore:
    def __init__(self, path):
        self.path = path

    def load(self):
        if not os.path.exists(self.path):
            return empty_state(), None
        with open(self.path) as f:
            return json.load(f), "local"

    def save(self, state, version, message):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=1, sort_keys=True)
        os.replace(tmp, self.path)
        return True, "saved to %s" % self.path


class GitHubStore:
    def __init__(self, repo, token, path=STATE_PATH, branch="main"):
        self.repo, self.token, self.path, self.branch = repo, token, path, branch

    def _h(self):
        return {"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "trading-agent"}

    def load(self):
        url = "https://api.github.com/repos/%s/contents/%s?ref=%s" % (self.repo, self.path, self.branch)
        s, b = http(url, self._h())
        if s == 404:
            return empty_state(), None
        if s != 200:
            raise RuntimeError("GitHub read failed: HTTP %s %s" % (s, b[:120]))
        d = json.loads(b)
        if d.get("content"):
            raw = base64.b64decode(d["content"])
        else:  # over 1 MB: fetch the blob
            s2, raw = http(d["git_url"], dict(self._h(), Accept="application/vnd.github.raw"))
        return json.loads(raw), d["sha"]

    def save(self, state, version, message):
        body = {"message": message, "branch": self.branch,
                "content": base64.b64encode(json.dumps(state, indent=1, sort_keys=True).encode()).decode()}
        if version:
            body["sha"] = version
        url = "https://api.github.com/repos/%s/contents/%s" % (self.repo, self.path)
        s, b = http(url, dict(self._h(), **{"Content-Type": "application/json"}),
                    data=json.dumps(body).encode(), method="PUT", tries=1)
        if s in (200, 201):
            return True, "saved to GitHub"
        if s in (409, 422):
            return False, "someone else saved first (HTTP %s): this run was discarded" % s
        return False, "GitHub save failed: HTTP %s %s" % (s, b[:160])


ALERT_ISSUE = "Trading agent alerts"


def github_alert(title, body):
    """Post the alert as a comment on one issue assigned to the owner: GitHub then notifies them
    (GitHub app push notification and email) with no setup at all."""
    repo, token = os.environ.get("AGENT_GITHUB_REPO"), os.environ.get("AGENT_GITHUB_TOKEN")
    owner = os.environ.get("AGENT_GITHUB_OWNER") or (repo or "/").split("/")[0]
    if not repo or not token:
        return False
    h = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "trading-agent", "Content-Type": "application/json"}
    base = "https://api.github.com/repos/%s/issues" % repo
    try:
        s, b = http(base + "?state=open&per_page=100", h)
        num = None
        if s == 200:
            num = next((i["number"] for i in json.loads(b)
                        if i.get("title") == ALERT_ISSUE and "pull_request" not in i), None)
        if not num:
            intro = ("Every buy, sell, stop-loss, profit target, price alert and halt from the $100 Trading Agent "
                     "is posted here as a comment, plus a short summary after each market day. "
                     "GitHub sends you a notification for each one. Paper trading only.")
            s, b = http(base, h, data=json.dumps({"title": ALERT_ISSUE, "body": intro, "assignees": [owner]}).encode(),
                        method="POST", tries=1)
            if s not in (200, 201):
                log("GitHub alert issue could not be created: HTTP %s %s" % (s, b[:120]))
                return False
            num = json.loads(b)["number"]
        s, b = http("%s/%d/comments" % (base, num), h, data=json.dumps({"body": "**%s**\n\n%s" % (title, body)}).encode(),
                    method="POST", tries=1)
        log("GitHub alert %s" % ("sent" if s in (200, 201) else "failed: HTTP %s" % s))
        return s in (200, 201)
    except (ConnectionError, ValueError, KeyError) as e:
        log("GitHub alert failed: %s" % e)
        return False


def notify(topic, title, body, dry):
    if dry:
        log("[alert not sent] %s: %s" % (title, body))
        return
    github_alert(title, body)
    if not topic:
        return
    try:
        s, _ = http("https://ntfy.sh/" + urllib.parse.quote(topic), {"Title": title.encode("ascii", "ignore").decode(),
                    "Tags": "chart_with_upwards_trend"}, data=body.encode(), method="POST", tries=2)
        log("phone alert sent (%s)" % s)
    except ConnectionError as e:
        log("phone alert failed: %s" % e)


# ---------------------------------------------------------------- helpers
def r2(x):
    return round(float(x) + 1e-12, 2)


def money(x):
    return ("-$" if x < 0 else "$") + "%.2f" % abs(x)


def load_config():
    with open(os.path.join(HERE, "config.json")) as f:
        return json.load(f)


class Run:
    """One run of the agent. Mirrors the 12 steps of the Claude version."""

    def __init__(self, state, cfg, now, dry, mode=None):
        self.s, self.cfg, self.now, self.dry = state, cfg, now, dry
        self.f = state["fund"]
        self.ny = to_ny(now)
        self.today = self.ny.date()
        self.market_day = is_trading_day(self.today)
        self.type = mode or pick_mode(state, self.ny)
        hm = self.ny.hour * 60 + self.ny.minute
        mins = lambda key: (now - (parse_iso(self.f.get(key)) or dt.datetime(2000, 1, 1))).total_seconds() / 60
        # stocks: a fresh scan about every 10 minutes from 9:50 AM (opening range done) to 2:55 PM New York
        self.scan_stocks = self.type == "OPEN" or (self.market_day and 9 * 60 + 50 <= hm <= 14 * 60 + 55
                                                   and mins("lastStockScan") >= 9)
        # crypto trades all day, every day: a fresh scan about every 30 minutes
        self.scan_crypto = self.type in ("OPEN", "CLOSE") or mins("lastCryptoScan") >= 28
        self.replay_now = None          # set when replaying a past moment (tests)
        self.events = []                # things worth an alert
        self.blocked, self.cands = [], []
        self.notes = []
        self.work = tempfile.mkdtemp(prefix="agent-")
        os.makedirs(os.path.join(self.work, "trader"), exist_ok=True)
        self.packmode = False
        self.pack_test = ""
        self.daily = {}
        self.news = {}
        self.scanned = False
        self.earn_known = bool(self.f.get("earningsCheckedOn") == self.today.isoformat() and self.f.get("earningsKnown"))

    # -- prices
    def ny_arg(self):
        return self.ny.strftime("%Y-%m-%d %H:%M") if self.replay_now else None

    def session_frac(self):
        o = dt.datetime.combine(self.today, dt.time(9, 30))
        c = dt.datetime.combine(self.today, dt.time(13 if self.today.isoformat() in EARLY_CLOSE else 16, 0))
        return max(0.0, min(1.0, (self.ny - o).total_seconds() / (c - o).total_seconds()))

    def quote(self, sym, crypto=False):
        ch = yahoo_chart(sym, "5d", "5m", self.replay_now)
        if not ch or not ch["price"]:
            return None, None
        return ch["price"], ch

    def dview(self, sym, crypto=False):
        if sym not in self.daily:
            ch = yahoo_chart(sym, "1y", "1d", self.replay_now)
            frac = 1.0 if crypto else (self.session_frac() if self.market_day else 1.0)
            self.daily[sym] = daily_view(ch, self.now.date() if crypto else self.today, crypto, frac)
        return self.daily[sym]

    def ysym(self, p):
        return p["symbol"] + "-CAD" if p.get("kind") == "crypto" else p["symbol"]

    # -- step 0
    def bookkeeping(self):
        f = self.f
        if f.get("dayDate") != self.today.isoformat():
            f["dayDate"], f["dayStartEquityCad"], f["entriesToday"] = self.today.isoformat(), f["equityCad"], 0
        monday = (self.today - dt.timedelta(days=self.today.weekday())).isoformat()
        if f.get("weekStart") != monday:
            f["weekStart"], f["weekStartEquityCad"], f["entriesThisWeek"] = monday, f["equityCad"], 0

    # -- step 1
    def mark(self):
        f = self.f
        fx, _ = self.quote("CAD=X")
        spy, _ = self.quote("SPY")
        btc, _ = self.quote("BTC-CAD", True)
        if not fx or not spy or not btc:
            raise RuntimeError("prices could not be fetched (USD/CAD %s, SPY %s, BTC %s)" % (fx, spy, btc))
        self.fx, self.spy, self.btc = fx, spy, btc
        f["usdCad"] = round(fx, 5)
        if not f.get("benchmarkSet"):
            f["stocksCashUsd"] = f.get("stocksCashCad", 0) * (1 - RULES["fx_cost"]) / fx
            f["stocksCashCad"] = 0.0
            f["benchmarkSpyUnits"] = 70 * (1 - RULES["fx_cost"]) / fx * 0.999 / spy
            f["benchmarkBtcUnits"] = 30 * 0.992 / btc
            f["benchmarkSet"] = True
        for sym, p in self.s["positions"].items():
            px, _ = self.quote(self.ysym(p), p.get("kind") == "crypto")
            if px:
                p["lastPrice"] = px
            self.value_position(p)
        self.recompute()

    def value_position(self, p):
        rate = self.fx if p.get("currency") == "USD" else 1.0
        p["valueCad"] = r2(p["units"] * p["lastPrice"] * rate)
        p["pnlPct"] = round((p["valueCad"] / p["costCad"] - 1) * 100, 2) if p.get("costCad") else 0.0

    def recompute(self):
        f = self.f
        pos = sum(p["units"] * p["lastPrice"] * (self.fx if p.get("currency") == "USD" else 1.0)
                  for p in self.s["positions"].values())
        f["equityCad"] = r2(f.get("stocksCashCad", 0) + f.get("stocksCashUsd", 0) * self.fx + f.get("cryptoCashCad", 0) + pos)
        f["benchmarkCad"] = r2(f["benchmarkSpyUnits"] * self.spy * self.fx + f["benchmarkBtcUnits"] * self.btc)

    def sleeve(self, name):
        f = self.f
        cash = (f.get("stocksCashUsd", 0) * self.fx + f.get("stocksCashCad", 0)) if name == "stocks" else f.get("cryptoCashCad", 0)
        held = sum(p["valueCad"] for p in self.s["positions"].values() if p.get("sleeve") == name)
        n = sum(1 for p in self.s["positions"].values() if p.get("sleeve") == name)
        return cash, cash + held, n

    # -- selling
    def sell(self, sym, price, reason, when=None):
        p = self.s["positions"].pop(sym)
        crypto = p.get("kind") == "crypto"
        slip = RULES["crypto_slip"] if crypto else RULES["stock_slip"]
        proceeds = p["units"] * price * (1 - slip)
        if crypto:
            self.f["cryptoCashCad"] = self.f.get("cryptoCashCad", 0) + proceeds
            proceeds_cad = proceeds
        else:
            self.f["stocksCashUsd"] = self.f.get("stocksCashUsd", 0) + proceeds
            proceeds_cad = proceeds * self.fx
        pnl = proceeds_cad - p["costCad"]
        r = pnl / p["riskCad"] if p.get("riskCad") else 0.0
        when = when or self.ny
        tid = "%s-%s-SELL" % (when.strftime("%Y%m%d-%H%M"), sym)
        self.s["trades"].append({
            "id": tid, "date": when.strftime("%Y-%m-%d %H:%M"), "symbol": sym, "side": "SELL", "units": p["units"],
            "price": round(price, 6), "currency": p.get("currency"), "valueCad": r2(proceeds_cad),
            "costsCad": r2(p["units"] * price * slip * (1 if crypto else self.fx)), "setup": p.get("setup"),
            "reason": reason, "pnlCad": r2(pnl), "r": round(r, 2), "closedOn": self.today.isoformat()})
        self.events.append("Sold %s (%s): %s%s" % (sym, reason, "+" if pnl >= 0 else "", money(pnl)))
        log("SELL %s at %.4f: %s, P&L %s (%.2fR)" % (sym, price, reason, money(pnl), r))

    # -- step 2
    def exits(self):
        for sym in list(self.s["positions"]):
            p = self.s["positions"][sym]
            crypto = p.get("kind") == "crypto"
            if not crypto and not self.market_day and p.get("lastChecked"):
                continue  # stock market closed: nothing moved
            since = parse_iso(p.get("lastChecked") or p.get("entryTime")) or self.now
            _, ch = self.quote(self.ysym(p), crypto)
            done = False
            if ch:
                cut = (since - dt.datetime(1970, 1, 1)).total_seconds()
                for b in ch["bars"]:
                    if b["t"] + 300 <= cut:
                        continue
                    when = to_ny(dt.datetime(1970, 1, 1) + dt.timedelta(seconds=b["t"] + 300))
                    stop, target = p["stopPrice"], p.get("targetPrice") or 9e18
                    if b["o"] <= stop:   # opened below the stop: a stop order fills at the open
                        self.sell(sym, b["o"], "stop-loss hit", when); done = True
                    elif b["l"] <= stop:
                        self.sell(sym, stop, "stop-loss hit", when); done = True
                    elif b["h"] >= target:
                        self.sell(sym, target, "take-profit hit", when); done = True
                    if done:
                        break
            if done:
                continue
            px = p["lastPrice"]
            held_days = ((self.now.date() - dt.date.fromisoformat(p["entryDate"])).days if crypto
                         else trading_days_between(dt.date.fromisoformat(p["entryDate"]), self.today))
            limit = RULES["hold_days_crypto"] if crypto else RULES["hold_days_stock"]
            reason = None
            if held_days > limit:
                reason = "held the maximum %d days" % limit
            elif self.news.get(sym, {}).get("broken"):
                reason = "bad news broke the reason for buying: %s" % (self.news[sym]["headlines"] or [""])[0][:60]
            elif not crypto and self.type == "CLOSE" and self.market_day:
                dv = self.dview(sym)
                bought_today = p["entryDate"] == self.today.isoformat()
                trend_ok = dv and dv["sma20"] and px > dv["sma20"]
                if bought_today and not (px > p["entryPrice"] and trend_ok):
                    reason = "day trade closed: not in profit with the trend intact"
                elif p.get("kind") == "stock":
                    e = self.s["earnings"].get(sym)
                    nxt = next_trading_day(self.today).isoformat()
                    if not self.earn_known and not e:
                        reason = "earnings date unknown: no overnight hold"
                    elif e and e.get("status") == "upcoming" and (
                            (e.get("reportDate") == self.today.isoformat() and e.get("timing") != "before-open")
                            or (e.get("reportDate") == nxt and e.get("timing") != "after-close")):
                        reason = "company reports before the next open"
            if reason:
                self.sell(sym, px, reason)
                continue
            init_stop = p.get("initialStop", p["stopPrice"])
            if px - p["entryPrice"] >= p["entryPrice"] - init_stop and p["stopPrice"] < p["entryPrice"]:
                p["stopPrice"] = p["entryPrice"]
                log("%s: up 1R, stop raised to the buy price %.4f" % (sym, p["entryPrice"]))
            p["lastChecked"] = iso(self.now)
            self.value_position(p)
        self.recompute()

    # -- step 3
    def regimes(self):
        spy = self.dview("SPY")
        if spy and spy["sma200"]:
            if spy["price"] < spy["sma200"] or spy["upTodayPct"] < -2:
                self.stock_regime = "risk-off"
            elif spy["price"] > spy["sma50"] and spy["price"] > spy["sma200"]:
                self.stock_regime = "risk-on"
            else:
                self.stock_regime = "cautious"
        else:
            self.stock_regime = "cautious"
        btc = self.dview("BTC-CAD", True)
        _, bch = self.quote("BTC-CAD", True)
        chg24 = 0.0
        if bch and bch["bars"]:
            t_end = bch["bars"][-1]["t"]
            old = [b for b in bch["bars"] if b["t"] <= t_end - 86400]
            if old:
                chg24 = (bch["price"] / old[-1]["c"] - 1) * 100
        self.btc24 = chg24
        if not btc or not btc["sma200"] or chg24 < -6 or btc["price"] < btc["sma200"]:
            self.crypto_regime = "risk-off"
        elif btc["price"] > btc["sma50"]:
            self.crypto_regime = "risk-on"
        else:
            self.crypto_regime = "cautious"

    def gate(self):
        f = self.f
        why = []
        if f["equityCad"] <= RULES["halt_equity"] and not f.get("halted"):
            f["halted"], f["haltReason"] = True, "value fell to %s, at or below the $85 limit" % money(f["equityCad"])
            self.events.append("HALTED: " + f["haltReason"])
        if f.get("halted"):
            why.append("halted")
        if f["equityCad"] <= f["dayStartEquityCad"] * (1 - RULES["day_loss_pct"]):
            why.append("down 2% today")
        if f["equityCad"] <= f["weekStartEquityCad"] * (1 - RULES["week_loss_pct"]):
            why.append("down 5% this week")
        if f["entriesToday"] >= RULES["entries_per_day"]:
            why.append("2 buys already today")
        if f["entriesThisWeek"] >= RULES["entries_per_week"]:
            why.append("6 buys already this week")
        self.block_reason = ", ".join(why)
        self.stocks_ok = (not why and self.market_day and self.scan_stocks and self.type != "CLOSE"
                          and self.stock_regime != "risk-off")
        fomc_close = self.today.isoformat() in FOMC_DAYS and self.ny.hour >= 14
        self.crypto_ok = not why and self.scan_crypto and self.crypto_regime != "risk-off" and not fomc_close
        if fomc_close:
            self.notes.append("Fed decision day: no crypto buys this afternoon")

    # -- step 4
    def congress(self):
        f = self.f
        if not self.market_day or self.ny.hour < 9 or f.get("lastCongressCheck") == self.today.isoformat():
            return
        since = (self.today - dt.timedelta(days=7)).isoformat()
        code, data, tail = pack("ptr.py", ["index", "--since", since, "--json"], self.work)
        if not isinstance(data, list):
            log("Congress check unavailable: %s" % tail[-120:])
            return
        readable = ensure_pdftotext()
        seen = set(self.s.get("congressSeen", []))
        new = [r for r in data if r.get("doc_id") not in seen][:8]
        read = 0
        for r in new:
            member = r.get("name", "").replace("Hon. ", "")
            report = None
            if readable:
                argv = ["fetch", str(r["doc_id"]), "--year", str(r.get("year") or self.today.year),
                        "--filed", r.get("filed") or "", "--json"]
                code, report, tail = pack("ptr.py", argv, self.work)
            rows = (report or {}).get("rows") or []
            stock_rows = [x for x in rows if x.get("ticker") and x.get("asset_type") in (None, "ST", "OP")]
            if report is not None:
                read += 1
            for i, x in enumerate(stock_rows[:12]):
                cid = "%s-%s-%s-%d" % (r.get("filed"), x["ticker"], r.get("last", ""), i)
                kind = {"P": "Purchase", "S": "Sale", "S (partial)": "Sale"}.get(x.get("type"), x.get("type_name", "").title())
                self.s["congress"][cid] = {
                    "member": member, "chamber": "House", "symbol": x["ticker"], "type": kind,
                    "amount": x.get("amount") or "", "tradeDate": x.get("date") or "", "filedDate": r.get("filed"),
                    "url": r.get("url"), "note": ("options, " if x.get("asset_type") == "OP" else "") +
                    ("%s days to report" % x["days_to_disclose"] if x.get("days_to_disclose") is not None else "")}
            if not stock_rows:
                self.s["congress"]["%s-%s" % (r.get("filed"), r.get("doc_id"))] = {
                    "member": member, "chamber": "House", "symbol": "", "type": "Filed", "amount": "",
                    "tradeDate": "", "filedDate": r.get("filed"), "url": r.get("url"),
                    "note": "no stock trades read from this report" if report is not None else "open the link to read it"}
            seen.add(r.get("doc_id"))
        self.s["congressSeen"] = sorted(seen)[-300:]
        keep = sorted(self.s["congress"], key=lambda k: self.s["congress"][k].get("filedDate") or "", reverse=True)[:40]
        self.s["congress"] = {k: self.s["congress"][k] for k in keep}
        f["lastCongressCheck"] = self.today.isoformat()
        log("Congress tracker: %d new House reports, %d read" % (len(new), read))

    # -- step 5
    def check_alerts(self):
        hits = []
        for aid, a in self.s["alerts"].items():
            if a.get("status") != "waiting":
                continue
            if a.get("expiresOn") and a["expiresOn"] < self.today.isoformat():
                a["status"] = "expired"
                continue
            crypto = a.get("currency") == "CAD"
            px, _ = self.quote(a["symbol"] + ("-CAD" if crypto else ""), crypto)
            if px and ((a["direction"] == "above" and px >= a["level"]) or (a["direction"] == "below" and px <= a["level"])):
                a["status"], a["hitOn"] = "hit", self.today.isoformat()
                hits.append(a["symbol"])
                self.events.append("Alert hit: %s %s %s" % (a["symbol"], a["direction"], a["level"]))
        return hits

    # -- step 8
    def earnings(self, symbols):
        if not self.market_day:
            return
        f = self.f
        if f.get("earningsCheckedOn") == self.today.isoformat():
            self.earn_known = bool(f.get("earningsKnown"))
            return
        days, d = [], self.today
        back = d
        for _ in range(2):
            back = back - dt.timedelta(days=1)
            while not is_trading_day(back):
                back -= dt.timedelta(days=1)
            days.append(back)
        days.append(d)
        fwd = d
        for _ in range(3):
            fwd = next_trading_day(fwd)
            days.append(fwd)
        cal, ok = {}, 0
        for day in days:
            rows = nasdaq_earnings(day)
            if rows is None:
                continue
            ok += 1
            for sym, r in rows.items():
                cal.setdefault(sym, []).append((day, r))
        self.earn_known = ok >= 4
        f["earningsCheckedOn"], f["earningsKnown"] = self.today.isoformat(), self.earn_known
        if not self.earn_known:
            log("Earnings calendar unavailable: stocks will not be held overnight today")
            return
        watch = set(symbols) | {s for s, p in self.s["positions"].items() if p.get("kind") == "stock"}
        for sym in watch:
            for day, r in cal.get(sym, []):
                e = self.s["earnings"].get(sym, {})
                if day >= self.today:
                    e.update({"symbol": sym, "reportDate": day.isoformat(), "timing": r["timing"],
                              "status": "upcoming", "verdict": "", "updatedOn": self.today.isoformat(),
                              "note": "reports %s" % r["timing"].replace("-", " ") if r["timing"] != "unknown" else "report date from Nasdaq"})
                else:
                    eps, est = r.get("eps"), r.get("epsForecast")
                    if eps is None or est is None:
                        verdict, note = "", "reported; results not in the free data"
                    elif eps >= est * 1.02 if est > 0 else eps > est:
                        verdict, note = "beat", "profit per share %.2f vs %.2f expected" % (eps, est)
                    elif eps < est:
                        verdict, note = "miss", "profit per share %.2f vs %.2f expected" % (eps, est)
                    else:
                        verdict, note = "mixed", "profit per share %.2f, about as expected (%.2f)" % (eps, est)
                    e.update({"symbol": sym, "reportDate": day.isoformat(), "timing": r["timing"], "status": "reported",
                              "verdict": verdict, "note": note, "updatedOn": self.today.isoformat(),
                              "blockedUntil": add_trading_days(day, 5).isoformat() if verdict == "miss" else ""})
                self.s["earnings"][sym] = e
        old = (self.today - dt.timedelta(days=7)).isoformat()
        for sym in list(self.s["earnings"]):
            e = self.s["earnings"][sym]
            if (e.get("reportDate") or "") < old and sym not in self.s["positions"]:
                del self.s["earnings"][sym]

    def earnings_block(self, sym, etf):
        if etf:
            return False
        e = self.s["earnings"].get(sym)
        if not e:
            return False
        if e.get("blockedUntil") and e["blockedUntil"] >= self.today.isoformat():
            return True
        if e.get("status") == "upcoming":
            rd = dt.date.fromisoformat(e["reportDate"])
            return trading_days_between(self.today, rd) <= 2
        return False

    # -- scoring
    def score(self, sym, dv, news, crypto, etf):
        tech = 0
        if dv["sma50"] and dv["price"] > dv["sma50"]:
            tech += 6
        if dv["sma50"] and dv["sma50_5ago"] and dv["sma50"] > dv["sma50_5ago"]:
            tech += 4
        if dv["sma200"] and dv["price"] > dv["sma200"]:
            tech += 5
        if dv["rsi"] and 45 <= dv["rsi"] <= 70:
            tech += 5
        if dv["volPace"] >= 1.2:
            tech += 5
        if dv["price"] >= dv["high20"] * 0.97:
            tech += 5
        regime = self.crypto_regime if crypto else self.stock_regime
        macro = {"risk-on": 15, "cautious": 8}.get(regime, 0)
        fund = 9  # ETFs and coins have no company accounts: neutral
        if not crypto and not etf and news.get("full"):
            fs, why = self.fundamental(sym, dv["price"])
            if fs is not None:
                fund = fs
        micro = 5 + (5 if dv["volPace"] >= 1.5 else 0)
        if not crypto:
            micro += self.congress_bonus(sym)
        if crypto or etf:
            micro += 5
        elif self.earn_known:
            e = self.s["earnings"].get(sym)
            soon = e and e.get("status") == "upcoming" and trading_days_between(self.today, dt.date.fromisoformat(e["reportDate"])) <= 5
            micro += 0 if soon else 5
        else:
            micro += 2
        total = tech + news["score"] + fund + macro + min(15, micro)
        return total, {"technical": tech, "news": news["score"], "fundamental": fund, "macro": macro, "micro": min(15, micro)}

    def fundamental(self, sym, price):
        cache = self.s.setdefault("fundamentals", {})
        f = cache.get(sym)
        fresh = f and f.get("fetchedOn") and (utcnow().date() - dt.date.fromisoformat(f["fetchedOn"])).days < 7
        if not fresh:
            got = nasdaq_fundamentals(sym)
            if got:
                cache[sym] = f = got
            elif f is None:
                return None, "not available"
        return fundamental_score(f, price)

    def congress_bonus(self, sym):
        """Members of Congress who bought this stock in the last 45 days: up to 3 points, never a reason alone."""
        cut = (self.today - dt.timedelta(days=45)).isoformat()
        buyers = {c.get("member") for c in self.s.get("congress", {}).values()
                  if c.get("symbol") == sym and c.get("type") == "Purchase" and (c.get("tradeDate") or "") >= cut}
        return min(3, len(buyers))

    def setup_for(self, dv, news, orb):
        if dv["upTodayPct"] > RULES["max_up_today_pct"]:
            return None, "up more than 10% today"
        if orb:
            return "breakout", "opening-range breakout"
        if dv["newHigh20"] and dv["volPace"] >= 1.5 and dv["price"] <= dv["high20"] * 1.03:
            return "breakout", "new 20-day high on heavy volume"
        if news["catalyst"] and 2 <= dv["upTodayPct"] <= 10 and dv["upperHalf"]:
            return "news", "real news and holding its gains"
        if (dv["sma50"] and dv["sma50_5ago"] and dv["price"] > dv["sma50"] and dv["sma50"] > dv["sma50_5ago"]
                and dv["sma20"] and abs(dv["price"] / dv["sma20"] - 1) <= 0.02 and dv["rsi"] and 40 <= dv["rsi"] <= 60
                and dv["upTodayPct"] > 0):
            return "pullback", "pullback in an uptrend"
        return None, "no clean setup yet"

    # -- steps 6, 7, 9, 10
    def hunt(self, alert_hits):
        cfg = self.cfg
        stocks = list(cfg["stocks"])
        etfs = set(cfg["etfs"])
        shortlist = []
        if self.stocks_ok:
            self.f["lastStockScan"] = iso(self.now)
            self.scanned = True
            universe = stocks + sorted(etfs)
            for sym in alert_hits:
                if sym in universe:
                    universe.remove(sym)
                    universe.insert(0, sym)
            # pre-screen on daily closes (a few batch requests), full bars only for the best 20
            spark = yahoo_spark_daily(universe, self.replay_now)
            self.names = {s: (c["meta"].get("shortName") or c["meta"].get("longName") or s) for s, c in spark.items()}
            pre = []
            for sym in universe:
                dv = daily_view(spark.get(sym), self.today, False, 1.0) if sym in spark else self.dview(sym)
                if not dv or dv["price"] < 5:
                    continue
                t, _ = self.score(sym, dv, {"score": 12}, False, sym in etfs)
                pre.append((t + (5 if sym in alert_hits else 0), sym))
            pre.sort(reverse=True)
            top = [s for _, s in pre[:20]]
            orb = {}
            if self.packmode and top:
                argv = ["orb", "--symbols", ",".join(top), "--json", "--no-write", "--max-confirm", "20", "--quiet"]
                if self.ny_arg():
                    argv += ["--now", self.ny_arg()]
                code, data, tail = pack("scan.py", argv, self.work)
                if data:
                    orb = {h["symbol"]: h for h in data.get("hits", [])}
                    log("Chart scanner: %d of %d charts broke their opening range" % (len(orb), len(top)))
                else:
                    log("Chart scanner unavailable: %s" % tail[-150:].strip())
            shortlist = [s for s in top if s in orb][:4]
            for s in top:
                if len(shortlist) >= 4:
                    break
                if s not in shortlist:
                    shortlist.append(s)
            self.orb = orb
        coins = []
        if self.crypto_ok:
            self.f["lastCryptoScan"] = iso(self.now)
            self.scanned = True
            syms, src = top_coins(self.now)
            held = [s for s, p in self.s["positions"].items() if p.get("kind") == "crypto"]
            pre = []
            for c in syms:
                if c in held:
                    continue
                dv = self.dview(c + "-CAD", True)
                if not dv:
                    continue
                t, _ = self.score(c, dv, {"score": 12}, True, False)
                pre.append((t, c))
            pre.sort(reverse=True)
            coins = [c for _, c in pre[:2]]
        # news (step 7) for the shortlist (holdings were read before the exits)
        self.fetch_news(shortlist + [c + "-USD" for c in coins])
        # earnings (step 8)
        self.earnings(shortlist)
        cands = []
        for sym in shortlist:
            dv = self.dview(sym)
            n = dict(self.news.get(sym, {"score": 12, "catalyst": False, "pos": 0, "neg": 0}), full=True)
            total, parts = self.score(sym, dv, n, False, sym in etfs)
            cands.append({"symbol": sym, "kind": "etf" if sym in etfs else "stock", "dv": dv, "news": n,
                          "score": total, "parts": parts})
        for c in coins:
            dv = self.dview(c + "-CAD", True)
            n = self.news.get(c, {"score": 12, "catalyst": False, "pos": 0, "neg": 0})
            total, parts = self.score(c, dv, n, True, False)
            cands.append({"symbol": c, "kind": "crypto", "dv": dv, "news": n, "score": total, "parts": parts})
        cands.sort(key=lambda c: -c["score"])
        for c in cands:
            self.try_buy(c)
        self.set_alerts(cands)

    def fetch_news(self, tickers):
        tickers = [t for t in tickers if (t[:-4] if t.endswith("-USD") else t) not in self.news]
        if not tickers:
            return
        argv = ["--tickers"] + tickers + ["--hours", "12", "--no-sec", "--no-moves", "--all", "--dry-run", "--json"]
        if self.replay_now:
            argv += ["--now", iso(self.now)]
        code, data, tail = pack("news.py", argv, self.work)
        if data is None:
            log("News check unavailable: %s" % tail[-120:].strip())
            return
        items = data.get("items", [])
        aliases = self.cfg.get("names", {})
        texts = []   # one model pass over every relevant headline
        for t in tickers:
            sym = t[:-4] if t.endswith("-USD") else t
            for it in items:
                if t in (it.get("touches") or [it.get("ticker")]) and about(str(it.get("title", "")), sym, aliases):
                    texts.append(news_text(it))
        scores = classify_texts(sorted(set(texts)))
        for t in tickers:
            sym = t[:-4] if t.endswith("-USD") else t
            self.news[sym] = read_news(items, sym, aliases, feed=t, scores=scores)

    def try_buy(self, c):
        f, sym, dv, crypto = self.f, c["symbol"], c["dv"], c["kind"] == "crypto"
        etf = c["kind"] == "etf"
        sleeve = "crypto" if crypto else "stocks"
        regime = self.crypto_regime if crypto else self.stock_regime
        need = RULES["score_min_cautious"] if regime == "cautious" else RULES["score_min"]
        orb_hit = None
        if not crypto and sym in getattr(self, "orb", {}) and self.packmode:
            argv = ["check", sym, "--write", "--json"] + (["--now", self.ny_arg()] if self.ny_arg() else [])
            code, data, tail = pack("entry.py", argv, self.work)
            if data and data.get("status") == "triggered" and dv["price"] <= data["trigger"]["price"] * 1.01:
                orb_hit = data
        setup, why = self.setup_for(dv, c["news"], orb_hit)
        verdict = "%d/100, %s" % (c["score"], why)
        if c["score"] < need:
            verdict = "%d/100, below the %d needed" % (c["score"], need)
        self.cands.append({"symbol": sym, "score": c["score"], "verdict": verdict[:80]})
        if c["score"] < need or not setup:
            return
        if f["stats"]["bySetup"][setup]["disabled"]:
            self.blocked.append({"symbol": sym, "failed": ["Setup switched off"]})
            return
        price = dv["price"]
        risk_cad = RULES["risk_pct"] * f["equityCad"]
        replay_src, rep = "daily", None
        stop = target = None
        if orb_hit:
            argv = ["plan", "--target", "2R", "--entry", "%.2f" % price, "--write", "--json"] + \
                   (["--now", self.ny_arg()] if self.ny_arg() else [])
            code, plan, tail = pack("exit.py", argv, self.work)
            if plan and plan.get("stop") and plan.get("target"):
                stop, target = plan["stop"], plan["target"]
                bar = self.ny - dt.timedelta(minutes=1)
                bar = bar.replace(minute=bar.minute - bar.minute % 5, second=0, microsecond=0)
                etime = bar.strftime("%I:%M %p").lstrip("0")
                argv = ["run", "--symbol", sym, "--date", self.today.isoformat(), "--entry", "%.2f" % price,
                        "--stop", "%.2f" % stop, "--target", "%.2f" % target, "--entry-time", etime,
                        "--setup", "orb", "--until", "15:55", "--json"] + (["--now", self.ny_arg()] if self.ny_arg() else [])
                code, tm, tail = pack("timemachine.py", argv, self.work)
                if tm and tm.get("mc"):
                    pt, ps = tm["mc"]["p_target"], tm["mc"]["p_stop"]
                    v = str(tm.get("verdict", ""))
                    rep = {"pTarget": pt, "pStop": ps, "meanR": tm["mc"].get("avg_R"),
                           "favourable": (pt / (pt + ps) >= 0.40 if pt + ps > 0 else False) and "toward the stop" not in v,
                           "source": "pack-5m"}
                    replay_src = "pack-5m"
        if stop is None:
            atr = dv["atrPct"] or (4.0 if crypto else 2.0)
            d = min(0.15, max(0.04, atr / 100)) if crypto else min(0.08, max(0.015, atr / 200))
            stop, target = price * (1 - d), price * (1 + 2 * d)
        d = (price - stop) / price
        if rep is None:
            bars = dv["ohlc"]
            res = replay({"bars": bars, "entry": price, "stop": stop, "target": target,
                          "maxDays": 10 if crypto else 2, "costPct": 0.016 if crypto else 0.002})
            rep = {"pTarget": res.get("pTarget"), "pStop": res.get("pStop"), "meanR": res.get("meanR"),
                   "favourable": bool(res.get("favourable")), "source": "daily"}
        cash, sleeve_val, npos = self.sleeve(sleeve)
        cap = sleeve_val * (RULES["crypto_cap_pct"] if crypto else RULES["stock_cap_pct"])
        value = min(risk_cad / d if d > 0 else 0, cap, cash / (1 + (0.016 if crypto else 0.002) / 2))
        order = {"side": "BUY", "symbol": sym, "buyingAllowed": self.crypto_ok if crypto else self.stocks_ok,
                 "blockReason": self.block_reason, "universeOk": True, "alreadyHeld": sym in self.s["positions"],
                 "score": c["score"], "scoreMin": need, "setup": setup, "setupDisabled": False,
                 "earningsBlocked": (not crypto) and self.earnings_block(sym, etf), "upTodayPct": dv["upTodayPct"],
                 "entry": price, "stop": stop, "target": target, "valueCad": value, "riskCad": risk_cad,
                 "costPct": 0.016 if crypto else 0.002, "equityCad": f["equityCad"],
                 "dayStartEquityCad": f["dayStartEquityCad"], "sleeveCashCad": cash, "sleeveCapCad": cap,
                 "positionsInSleeve": npos, "maxPositionsInSleeve": RULES["max_crypto_positions" if crypto else "max_stock_positions"],
                 "entriesToday": f["entriesToday"], "entriesThisWeek": f["entriesThisWeek"], "replay": rep}
        chk = order_check(order)
        failed, passed = list(chk["failed"]), sum(1 for r in chk["rules"] if r["ok"])
        if not crypto and self.packmode:
            self.pack_risk(sym, value / self.fx, price, stop, target, setup, failed)
            passed += 0 if "Pack risk check" in failed else 1
        if failed:
            self.blocked.append({"symbol": sym, "failed": failed})
            log("BLOCK %s: %s" % (sym, ", ".join(failed)))
            return
        self.buy(sym, c, setup, price, stop, target, value, risk_cad, rep, passed)

    def pack_risk(self, sym, usd, price, stop, target, setup, failed):
        f = self.f
        _, sleeve_val, _ = self.sleeve("stocks")
        cfg = {"budget": round(sleeve_val / self.fx, 2), "cash_buffer_pct": 2, "max_position_pct": 40,
               "max_order_usd": round(0.4 * sleeve_val / self.fx, 2), "min_order_usd": 3.5,
               "max_loss_per_trade_usd": round(1.5 * RULES["risk_pct"] * f["equityCad"] / self.fx, 2),
               "max_daily_loss_usd": round(0.02 * f["dayStartEquityCad"] / self.fx, 2), "max_trades_per_day": 2,
               "max_orders_per_day": 6, "min_reward_risk": 2.0, "require_stop": True, "no_entries_before": "09:45",
               "no_entries_after": "15:00", "flat_by": "15:55", "fractional": True, "broker": "alpaca", "paper": True,
               "blocked": [], "allow_options": False, "allow_margin": False, "allow_short": False, "pdt_guard": False,
               "sec_user_agent": ""}
        orders = {"created_by": "trading-agent", "status": "proposed", "paper": True, "orders": [{
            "symbol": sym, "side": "buy", "notional": "%.2f" % usd, "type": "market", "time_in_force": "day",
            "why": "paper", "plan": {"entry": round(price, 2), "stop": round(stop, 2), "target": round(target, 2),
                                     "setup": setup, "flat_by": "15:55"}}]}
        with open(os.path.join(self.work, "trader", "config.json"), "w") as fh:
            json.dump(cfg, fh)
        with open(os.path.join(self.work, "trader", "orders.json"), "w") as fh:
            json.dump(orders, fh)
        argv = ["--json", "--no-write"] + (["--now", self.ny_arg()] if self.ny_arg() else [])
        code, data, tail = pack("check.py", argv, self.work)
        res = (data or {}).get("results") or []
        if not res or res[0].get("verdict") != "PASS":
            reasons = (res[0].get("reasons") if res else None) or [tail[-100:].strip() or "no answer"]
            failed.append("Pack risk check")
            log("pack risk check: %s" % "; ".join(str(r) for r in reasons)[:200])

    def buy(self, sym, c, setup, price, stop, target, value, risk_cad, rep, passed):
        f, crypto = self.f, c["kind"] == "crypto"
        slip = RULES["crypto_slip"] if crypto else RULES["stock_slip"]
        fill = price * (1 + slip)
        stop_f, target_f = fill * stop / price, fill * target / price
        if crypto:
            units = value / fill
            f["cryptoCashCad"] -= value
        else:
            usd = value / self.fx
            units = usd / fill
            f["stocksCashUsd"] -= usd
        dv = c["dv"]
        p = {"symbol": sym, "name": getattr(self, "names", {}).get(sym, sym), "kind": c["kind"], "sleeve": "crypto" if crypto else "stocks",
             "currency": "CAD" if crypto else "USD", "units": units, "entryPrice": fill,
             "entryDate": (self.now.date() if crypto else self.today).isoformat(), "entryTime": iso(self.now),
             "costCad": r2(value), "riskCad": r2(risk_cad), "stopPrice": stop_f, "initialStop": stop_f,
             "targetPrice": target_f, "setup": setup, "lastPrice": price, "score": c["score"],
             "thesis": ("%s, score %d" % ({"breakout": "Breakout", "pullback": "Pullback in an uptrend",
                                            "news": "News move holding gains"}[setup], c["score"]))[:120],
             "lastChecked": iso(self.now), "dayLowAtEntry": dv["dayLow"], "dayHighAtEntry": dv["dayHigh"]}
        self.s["positions"][sym] = p
        self.value_position(p)
        f["entriesToday"] += 1
        f["entriesThisWeek"] += 1
        tid = "%s-%s-BUY" % (self.ny.strftime("%Y%m%d-%H%M"), sym)
        self.s["trades"].append({
            "id": tid, "date": self.ny.strftime("%Y-%m-%d %H:%M"), "symbol": sym, "side": "BUY", "units": units,
            "price": round(fill, 6), "currency": p["currency"], "valueCad": r2(value), "costsCad": r2(value * slip / (1 + slip)),
            "setup": setup, "reason": "%s, score %d" % (setup, c["score"]),
            "check": {"verdict": "PASS", "passed": passed, "failed": []},
            "replay": {"pTarget": rep.get("pTarget"), "pStop": rep.get("pStop"), "meanR": rep.get("meanR"),
                       "source": rep.get("source")}})
        self.events.append("Bought %s %s (%s, score %d)" % (money(value), sym, setup, c["score"]))
        log("BUY %s: %s at %.4f, stop %.4f, target %.4f" % (sym, money(value), fill, stop_f, target_f))
        self.recompute()

    def set_alerts(self, cands):
        waiting = [a for a in self.s["alerts"].values() if a.get("status") == "waiting"]
        added = 0
        for c in cands:
            if added >= RULES["alerts_per_run"] or len(waiting) + added >= RULES["alert_max"]:
                break
            sym, dv = c["symbol"], c["dv"]
            if c["score"] < 60 or sym in self.s["positions"] or any(a["symbol"] == sym for a in waiting):
                continue
            if c["kind"] == "stock" and self.earnings_block(sym, False):
                continue
            if any(x["symbol"] == sym and x["side"] == "BUY" and x["date"].startswith(self.today.isoformat())
                   for x in self.s["trades"]):
                continue
            if dv["price"] < dv["high20"]:
                level, direction, reason = dv["high20"], "above", "a breakout would start above here"
            elif dv["sma20"] and dv["price"] > dv["sma20"] * 1.01:
                level, direction, reason = dv["sma20"] * 1.01, "below", "a pullback entry would start here"
            else:
                continue
            aid = "%s-%s" % (sym, self.today.strftime("%Y%m%d"))
            self.s["alerts"][aid] = {"symbol": sym, "level": round(level, 4), "direction": direction,
                                     "currency": "CAD" if c["kind"] == "crypto" else "USD", "reason": reason,
                                     "createdOn": self.today.isoformat(),
                                     "expiresOn": add_trading_days(self.today, RULES["alert_days"]).isoformat(),
                                     "status": "waiting", "hitOn": ""}
            added += 1
        for aid in [k for k, a in self.s["alerts"].items() if a.get("status") != "waiting"
                    and (a.get("hitOn") or a.get("expiresOn") or "") < (self.today - dt.timedelta(days=10)).isoformat()]:
            del self.s["alerts"][aid]

    # -- step 11
    def review(self):
        st = self.f["stats"]
        for t in self.s["trades"]:
            if t.get("side") != "SELL" or t.get("reviewed"):
                continue
            t["reviewed"] = True
            r, setup = float(t.get("r", 0)), t.get("setup") or "breakout"
            st["closed"] += 1
            st["wins"] += 1 if t.get("pnlCad", 0) > 0 else 0
            st["totalR"] = round(st["totalR"] + r, 3)
            b = st["bySetup"].setdefault(setup, {"n": 0, "wins": 0, "r": 0.0, "disabled": False})
            b["n"] += 1
            b["wins"] += 1 if t.get("pnlCad", 0) > 0 else 0
            b["r"] = round(b["r"] + r, 3)
            if b["n"] >= 8 and b["r"] < 0 and not b["disabled"]:
                b["disabled"] = True
                self.events.append("Setup switched off: %s lost money over %d trades" % (setup, b["n"]))
            reason = t.get("reason", "")
            if "stop" in reason:
                lesson = "%s %s: stopped out (%+.2fR). Wait for a cleaner entry before buying this setup again." % (t["symbol"], setup, r)
            elif "take-profit" in reason:
                lesson = "%s %s: target reached (%+.2fR). The plan worked as written." % (t["symbol"], setup, r)
            else:
                lesson = "%s %s: closed by rule (%s), %+.2fR." % (t["symbol"], setup, reason[:40], r)
            self.s["lessons"] = ([lesson[:140]] + self.s["lessons"])[:12]

    # -- the whole run
    def go(self):
        f = self.f
        log("Run %s %s (New York %s), market %s" % (self.today, self.type, self.ny.strftime("%H:%M"),
                                                    "open day" if self.market_day else "closed today"))
        self.pack_probe()
        self.bookkeeping()
        self.mark()
        if self.market_day and not self.replay_now:
            spy_ch = yahoo_chart("SPY", "5d", "5m")
            if spy_ch and spy_ch["bars"] and bar_day(spy_ch["bars"][-1]["t"]) != self.today:
                self.market_day = False
                log("SPY has no bars today: treating the stock market as closed")
        held = [s if p.get("kind") != "crypto" else s + "-USD" for s, p in self.s["positions"].items()
                if p.get("kind") == "crypto" or self.market_day]
        self.fetch_news(held)
        held_stocks = [s for s, p in self.s["positions"].items() if p.get("kind") == "stock"]
        if held_stocks and self.market_day:
            self.earnings(held_stocks)
        self.exits()
        self.regimes()
        self.gate()
        self.congress()
        hits = self.check_alerts()
        if self.stocks_ok or self.crypto_ok:
            self.hunt(hits)
        else:
            if self.type != "WATCH" or self.scan_stocks or self.scan_crypto:
                log("No buying this run: %s" % (self.block_reason or "market closed or risk-off"))
            held = [s for s, p in self.s["positions"].items() if p.get("kind") == "stock"]
            if held:
                self.earnings(held)
        self.review()
        self.record()

    def pack_probe(self):
        res = []
        for host in ("query1", "query2"):
            url = "https://%s.finance.yahoo.com/v8/finance/chart/SPY?range=1d&interval=5m" % host
            try:
                s, _ = http(url, {"Accept": "application/json"}, tries=1)
            except ConnectionError as e:
                s = "error " + str(e)[:40]
            res.append("%s %s" % (host, s))
        self.packmode = any(r.endswith(" 200") for r in res) and os.path.exists(os.path.join(PACK, "scan.py"))
        self.pack_test = "; ".join(res)
        log("Pack data test: %s -> %s" % (self.pack_test, "pack" if self.packmode else "basic"))

    def record(self):
        f, s = self.f, self.s
        for p in s["positions"].values():
            self.value_position(p)
        self.recompute()
        cash = f.get("stocksCashCad", 0) + f.get("stocksCashUsd", 0) * self.fx + f.get("cryptoCashCad", 0)
        problems = []
        for k in ("stocksCashCad", "stocksCashUsd", "cryptoCashCad"):
            if f.get(k, 0) < -0.005:
                problems.append("%s is negative" % k)
        if abs(cash + sum(p["valueCad"] for p in s["positions"].values()) - f["equityCad"]) > 0.02:
            problems.append("value does not add up")
        if f["entriesToday"] > RULES["entries_per_day"] or f["entriesThisWeek"] > RULES["entries_per_week"]:
            problems.append("too many buys")
        if problems:
            f["ruleBreaks"] = f.get("ruleBreaks", 0) + 1
            self.events.append("Bookkeeping problem: " + ", ".join(problems))
        f["peakEquityCad"] = max(f.get("peakEquityCad", f["equityCad"]), f["equityCad"])
        dd = (1 - f["equityCad"] / f["peakEquityCad"]) * 100 if f["peakEquityCad"] else 0
        f["maxDrawdownPct"] = round(max(f.get("maxDrawdownPct", 0), dd), 2)
        if self.type == "CLOSE" and self.market_day and f.get("lastTradingDay") != self.today.isoformat():
            f["tradingDays"] = f.get("tradingDays", 0) + 1
            f["lastTradingDay"] = self.today.isoformat()
            if f["tradingDays"] == RULES["paper_days_goal"]:
                self.events.append("Paper test complete: 20 trading days. Open the page and decide about real money.")
        nav = [n for n in s["nav"] if n.get("date") != self.today.isoformat()]
        nav.append({"date": self.today.isoformat(), "equityCad": f["equityCad"], "benchmarkCad": f["benchmarkCad"],
                    "cashCad": r2(cash)})
        s["nav"] = nav[-400:]
        s["trades"] = s["trades"][-300:]
        summary = self.summary()
        regime = self.stock_regime if self.market_day else self.crypto_regime
        j = next((x for x in s["journal"] if x.get("date") == self.today.isoformat()), None)
        if not j:
            j = {"date": self.today.isoformat(), "summary": ""}
            s["journal"].append(j)
        label = {"OPEN": "Morning", "CLOSE": "Close", "WATCH": self.ny.strftime("%I:%M %p").lstrip("0")}[self.type]
        if self.type != "WATCH" or any(e.startswith(("Bought", "Sold", "HALTED")) for e in self.events):
            text = (j["summary"] + " " if j.get("summary") else "") + "%s: %s" % (label, summary)
            if len(text) > 1200:  # keep the newest part
                text = "..." + text[-1200:].split(". ", 1)[-1]
            j["summary"] = text
        if self.scanned or self.type != "WATCH":
            j.update({"topCandidates": self.cands[:6], "blocked": self.blocked[:6]})
        j.update({"regime": regime, "dataMode": "pack" if self.packmode else "connectors",
                  "packTest": self.pack_test, "dataCalls": CALLS["n"]})
        s["journal"] = s["journal"][-30:]
        f["lastRun"] = iso(self.now)
        f["lastRunNote"] = ("Pack data test: %s. " % ("ok" if self.packmode else "failed")) + summary[:200]
        f["lastRunBy"] = os.environ.get("AGENT_RUNNER", "manual")
        if self.type != "WATCH":
            s["runs"]["%s %s" % (self.today, self.type)] = {"at": iso(self.now), "by": f["lastRunBy"]}
            s["runs"] = dict(sorted(s["runs"].items())[-20:])
        s["log"] = LOG[-80:]
        shutil.rmtree(self.work, ignore_errors=True)

    def summary(self):
        f = self.f
        parts = []
        spy = self.daily.get("SPY")
        if self.market_day and spy:
            parts.append("The S&P 500 fund was %s %.1f%% today; market mood %s." % (
                "up" if spy["upTodayPct"] >= 0 else "down", abs(spy["upTodayPct"]), self.stock_regime))
        elif not self.market_day:
            parts.append("US stock market closed today.")
        parts.append("Bitcoin %s %.1f%% in 24 hours (%s)." % ("up" if self.btc24 >= 0 else "down", abs(self.btc24),
                                                              self.crypto_regime))
        trades = [e for e in self.events if e.startswith(("Bought", "Sold"))]
        if trades:
            parts.append(" ".join(e + "." for e in trades))
        elif self.blocked:
            parts.append("No trades: %s was the closest but failed %s." % (
                self.blocked[0]["symbol"], ", ".join(self.blocked[0]["failed"][:2]).lower()))
        elif not (self.stocks_ok or self.crypto_ok):
            parts.append("No buying allowed this run (%s)." % (self.block_reason or "market closed or risk-off"))
        else:
            parts.append("No trades: nothing scored high enough with a clean setup.")
        parts.extend(self.notes)
        parts.append("Value %s vs buy-and-hold %s." % (money(f["equityCad"]), money(f["benchmarkCad"])))
        text = " ".join(parts)
        words = text.split()
        return " ".join(words[:90])


# ---------------------------------------------------------------- commands
def make_store(a):
    if a.state:
        return LocalStore(a.state)
    repo = os.environ.get("AGENT_GITHUB_REPO") or os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("AGENT_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        sys.exit("No state store: pass --state FILE, or set AGENT_GITHUB_REPO and AGENT_GITHUB_TOKEN.")
    return GitHubStore(repo, token)


def pick_mode(state, ny):
    """OPEN once each market morning, CLOSE once each market afternoon, WATCH otherwise."""
    d, hm, runs = ny.date(), ny.hour * 60 + ny.minute, state.get("runs", {})
    if is_trading_day(d):
        if 10 * 60 + 15 <= hm < 12 * 60 and "%s OPEN" % d not in runs:
            return "OPEN"
        if 15 * 60 + 15 <= hm < 16 * 60 + 30 and "%s CLOSE" % d not in runs:
            return "CLOSE"
    return "WATCH"


def cmd_run(a):
    now = parse_iso(a.now) if a.now else utcnow()
    cfg = load_config()
    topic = os.environ.get("AGENT_NTFY_TOPIC") or os.environ.get("NTFY_TOPIC") or ""
    store = make_store(a)
    try:
        state, version = store.load()
    except Exception as e:
        log("Could not read the agent's records: %s" % e)
        notify(topic, "Trading agent: run failed", "Could not read its records: %s" % str(e)[:150], a.dry_run)
        return 1
    run = Run(state, cfg, now, a.dry_run, a.mode)
    if a.now:
        run.replay_now = now
    slot = "%s %s" % (run.today, run.type)
    try:
        run.go()
    except Exception as e:
        log("Run failed, nothing changed: %s" % e)
        log(traceback.format_exc()[-800:])
        notify(topic, "Trading agent: run failed", "%s run failed and changed nothing: %s" % (run.type, str(e)[:150]), a.dry_run)
        return 1
    f = state["fund"]
    if a.dry_run:
        log("Dry run: nothing saved. Value %s vs buy-and-hold %s." % (money(f["equityCad"]), money(f["benchmarkCad"])))
        return 0
    last_saved = parse_iso(f.get("lastSaved")) or dt.datetime(2000, 1, 1)
    if run.type == "WATCH" and not run.events and (now - last_saved).total_seconds() < 1800:
        log("Quiet check: nothing changed worth saving. Value %s." % money(f["equityCad"]))
        return 0
    f["lastSaved"] = iso(now)
    ok, msg = store.save(state, version, "Agent %s %s" % (slot, now.strftime("%H:%MZ")))
    log(msg)
    if not ok:
        notify(topic, "Trading agent: not saved", msg, False)
        return 1
    tail = " | Value %s vs buy-and-hold %s" % (money(f["equityCad"]), money(f["benchmarkCad"]))
    if run.events:
        notify(topic, "Trading agent", " | ".join(run.events)[:400] + tail, False)
    elif run.type == "CLOSE" and cfg.get("daily_summary", True):
        notify(topic, "Trading agent: day done", run.summary()[:400], False)
    return 0


def cmd_selftest(a):
    topic = os.environ.get("AGENT_NTFY_TOPIC") or os.environ.get("NTFY_TOPIC") or ""
    rows = []

    def check(name, fn):
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, str(e)[:80]
        rows.append((name, ok, detail))
        print("%-26s %s  %s" % (name, "OK  " if ok else "FAIL", detail), flush=True)

    def yahoo(sym, rng="1d", iv="5m"):
        def f():
            ch = yahoo_chart(sym, rng, iv)
            return bool(ch and ch["price"]), ("price %.4f" % ch["price"]) if ch and ch["price"] else "no data"
        return f
    check("Yahoo stocks (SPY)", yahoo("SPY"))
    check("Yahoo crypto (BTC-CAD)", yahoo("BTC-CAD"))
    check("Yahoo USD/CAD", yahoo("CAD=X"))
    check("Yahoo news feed", lambda: (lambda s: (s[0] == 200, "HTTP %s" % s[0]))(
        http("https://feeds.finance.yahoo.com/rss/2.0/headline?s=AAPL&region=US&lang=en-US")))
    check("CoinGecko (optional)", lambda: (lambda r: (r[1] == "CoinGecko", ", ".join(r[0][:5])))(top_coins(utcnow())))
    check("Nasdaq earnings (optional)", lambda: (lambda r: (r is not None, "%d companies" % len(r or {})))(
        nasdaq_earnings(next_trading_day(to_ny(utcnow()).date()))))
    check("House Clerk (Congress)", lambda: (lambda s: (s[0] == 200, "HTTP %s" % s[0]))(
        http("https://disclosures-clerk.house.gov/public_disc/financial-pdfs/%d/FD.zip".replace("/FD", "FD")
             % to_ny(utcnow()).year, method="HEAD")))
    if a.state or os.environ.get("AGENT_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN"):
        def gh():
            st, v = make_store(a).load()
            return True, "records read, value %s" % money(st["fund"]["equityCad"])
        check("Agent records (GitHub)", gh)
    else:
        rows.append(("Agent records (GitHub)", False, "no token set"))
        print("Agent records (GitHub)     FAIL  no token set")
    def pk():
        code, data, tail = pack("timemachine.py", ["--help"], tempfile.gettempdir())
        return code in (0, None), "loaded" if code in (0, None) else tail[-60:]
    check("Day Trader pack", pk)
    must = [r for r in rows if "optional" not in r[0]]
    good = all(r[1] for r in must)
    msg = "Self-test %s: %d of %d checks passed." % ("passed" if good else "had problems", sum(r[1] for r in rows), len(rows))
    print(msg)
    notify(topic, "Trading agent self-test", msg + " " + "; ".join("%s %s" % (r[0], "ok" if r[1] else "FAIL") for r in rows), a.dry_run)
    return 0 if good else 1


def cmd_probe(a):
    """Developer check: print what the free sources return from this computer."""
    def show(label, url, headers=None, n=1500):
        try:
            s, b = http(url, headers=headers, timeout=25, tries=1)
            print("== %s HTTP %s\n%s\n" % (label, s, b[:n].decode("utf-8", "replace")), flush=True)
        except ConnectionError as e:
            print("== %s ERROR %s\n" % (label, e), flush=True)
    nh = NASDAQ_H
    show("option chain", "https://api.nasdaq.com/api/quote/AAPL/option-chain?assetclass=stocks&limit=20&money=at&type=all", nh, 3000)
    show("earnings surprise", "https://api.nasdaq.com/api/company/AAPL/earnings-surprise", nh, 1500)
    s, b = http("https://api.nasdaq.com/api/company/AAPL/financials?frequency=2", nh)
    try:
        d = json.loads(b)["data"]
        for tab in ("balanceSheetTable", "cashFlowTable", "financialRatiosTable"):
            print("== %s" % tab, [r.get("value1") for r in (d.get(tab) or {}).get("rows", [])][:40], flush=True)
        print("== income rows", [r.get("value1") for r in d["incomeStatementTable"]["rows"]], flush=True)
    except Exception as e:
        print("financials parse error", e)
    for syms in (["ES=F", "NQ=F", "YM=F", "RTY=F", "GC=F", "SI=F", "CL=F", "BTC=F", "ETH=F", "6E=F", "MES=F", "MBT=F"],
                 ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "HYPE32196-USD", "LINK-USD", "SUI20947-USD", "AVAX-USD"]):
        got = yahoo_spark_daily(syms)
        print("== spark", {k: (round(v["price"], 2), len(v["bars"])) for k, v in got.items()}, "missing", [x for x in syms if x not in got], flush=True)
    d, err = get_json("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=100&page=1")
    print("== coingecko top100", (len(d) if isinstance(d, list) else err), [c["symbol"] for c in (d or [])[:100]] if isinstance(d, list) else "", flush=True)
    work = tempfile.mkdtemp()
    os.makedirs(os.path.join(work, "trader"), exist_ok=True)
    code, data, tail = pack("scan.py", ["universe", "--json"], work)
    print("== universe", code, data if data else tail[-300:], flush=True)
    try:
        lines = open(os.path.join(work, "trader", "universe.txt")).read().splitlines()
        print("universe lines", len(lines), lines[:5])
    except Exception as e:
        print("universe read error", e)
    s, b = http("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt")
    etf = [l for l in b.decode("latin-1").splitlines() if l.count("|") > 6 and l.split("|")[6] == "Y"]
    s2, b2 = http("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt")
    etf2 = [l for l in b2.decode("latin-1").splitlines() if l.count("|") > 5 and l.split("|")[4] == "Y"]
    print("== ETFs nasdaq", len(etf), etf[:2], "other", len(etf2), etf2[:2], flush=True)
    show("sec tickers (no contact)", "https://www.sec.gov/files/company_tickers.json", {"User-Agent": "trading-agent"}, 200)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="$100 Trading Agent, Claude-free edition (paper trading only)")
    sub = p.add_subparsers(dest="cmd")
    for name in ("run", "selftest"):
        sp = sub.add_parser(name)
        sp.add_argument("--state", help="keep the records in this local file instead of GitHub")
        sp.add_argument("--dry-run", action="store_true", help="save nothing and send no alert")
        if name == "run":
            sp.add_argument("--now", help="replay a past moment, UTC ISO time (for testing)")
            sp.add_argument("--mode", choices=["OPEN", "CLOSE", "WATCH"], help="force the run type (testing)")
    sub.add_parser("probe")
    a = p.parse_args(argv)
    if a.cmd == "probe":
        return cmd_probe(a)
    if a.cmd == "run":
        return cmd_run(a)
    if a.cmd == "selftest":
        return cmd_selftest(a)
    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
