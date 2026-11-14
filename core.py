"""Shared tools for the trading agent: clock, free data sources, judgement and paper-trading maths.

Standard-library Python only, except the optional news model (transformers), which is used when
installed and replaced by word rules when not.
"""
import base64
import contextlib
import datetime as dt
import html
import importlib.util
import io
import json
import os
import random
import re
import shutil
import statistics
import sys
import tempfile
import time
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
START_CAD = 10000.0

# ---------------------------------------------------------------- the rules
RULES = {
    "risk_pct": 0.01,              # risk 1% of the account per trade
    "max_open_risk_pct": 0.06,     # all open trades together may risk at most 6%
    "score_min": 70,               # when the market mood favours the trade's direction
    "score_min_against": 80,       # when it does not
    "max_move_today_pct": 10.0,    # never chase a move already over 10% today
    "entries_per_day": 4, "entries_per_week": 12,
    "max_pos": {"equity": 6, "crypto": 3, "option": 3, "future": 2},
    "equity_cap_pct": 0.25,        # one stock or ETF position: at most 25% of the USD sleeve
    "crypto_cap_pct": 0.40,        # one coin: at most 40% of the crypto sleeve
    "option_cap_pct": 0.05,        # one options position: at most 5% of the account in premium
    "future_margin_cap_pct": 0.30, # one futures position: margin at most 30% of the USD sleeve
    "short_collateral": 0.5,       # extra cash set aside on a short (Reg T 150%)
    "day_loss_pct": 0.02, "week_loss_pct": 0.05, "halt_pct": 0.85,
    "hold_days": {"stock": 5, "etf": 5, "crypto": 10, "option": 10, "future": 5},
    "fx_cost": 0.015, "slip": {"stock": 0.001, "etf": 0.001, "crypto": 0.008, "future": 0.0002},
    "borrow_rate": 0.05,           # yearly cost of borrowing shares to short
    "option_fee": 1.0, "future_fee": 1.25,   # USD per contract per side
    "option_stop": 0.5, "option_target": 1.0, "option_min_dte": 14, "option_max_dte": 45, "option_exit_dte": 5,
    "option_score": 80,            # a signal this strong may use options instead of shares
    "alert_max": 8, "alert_days": 5, "alerts_per_run": 3,
    "paper_days_goal": 20,
}
LONG_SETUPS = ("pullback", "breakout", "news", "earnings")
SHORT_SETUPS = ("rally_short", "breakdown", "news_short", "earnings_short")
SETUPS = LONG_SETUPS + SHORT_SETUPS
SETUP_NAMES = {"pullback": "Pullback in an uptrend", "breakout": "Breakout", "news": "Good-news move",
               "earnings": "Strong earnings", "rally_short": "Short a rally in a downtrend",
               "breakdown": "Breakdown (short)", "news_short": "Bad-news move (short)",
               "earnings_short": "Weak earnings (short)"}

# Micro futures: Yahoo symbol, dollars per point, approximate margin as a share of the contract's value
FUTURES = {
    "MES": ("ES=F", 5.0, 0.07, "S&P 500"), "MNQ": ("NQ=F", 2.0, 0.08, "Nasdaq 100"),
    "MYM": ("YM=F", 0.5, 0.07, "Dow"), "M2K": ("RTY=F", 5.0, 0.08, "Russell 2000"),
    "MGC": ("GC=F", 10.0, 0.08, "gold"), "SIL": ("SI=F", 1000.0, 0.10, "silver"),
    "MCL": ("CL=F", 100.0, 0.12, "crude oil"), "MBT": ("BTC=F", 0.1, 0.40, "bitcoin"),
    "MET": ("ETH=F", 0.1, 0.45, "ether"), "M6E": ("6E=F", 12500.0, 0.03, "euro"),
}
INDEX_FUTURES = {"MES", "MNQ", "MYM", "M2K"}
CRYPTO_FUTURES = {"MBT", "MET"}

LOG = []
CALLS = {"n": 0, "fail": 0}


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


def epoch_to_utc(t):
    return dt.datetime(1970, 1, 1) + dt.timedelta(seconds=t)


def utc_to_epoch(u):
    return (u - dt.datetime(1970, 1, 1)).total_seconds()


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


def prev_trading_day(d):
    d -= dt.timedelta(days=1)
    while not is_trading_day(d):
        d -= dt.timedelta(days=1)
    return d


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
            CALLS["fail"] += 1
            return e.code, body
        except Exception as e:
            err = e
            if i + 1 < tries:
                time.sleep(2)
    CALLS["fail"] += 1
    raise ConnectionError(str(err)[:160])


def get_json(url, headers=None, timeout=25, tries=2):
    try:
        s, b = http(url, headers=headers, timeout=timeout, tries=tries)
    except ConnectionError as e:
        return None, str(e)
    if s != 200:
        return None, "HTTP %s" % s
    try:
        return json.loads(b), None
    except ValueError:
        return None, "not JSON"


# ---------------------------------------------------------------- Yahoo (free, unofficial)
_YCACHE = {}


def ysym(sym):
    """Yahoo writes share classes with a dash: BRK.B -> BRK-B."""
    return sym.replace(".", "-") if not sym.endswith(("=F", "=X")) else sym


def yahoo_chart(sym, rng, interval, now=None):
    key = (sym, rng, interval)
    if key in _YCACHE:
        return _YCACHE[key]
    q = urllib.parse.urlencode({"range": rng, "interval": interval, "includePrePost": "false"})
    out = None
    for host in ("query2", "query1"):
        url = "https://%s.finance.yahoo.com/v8/finance/chart/%s?%s" % (host, urllib.parse.quote(ysym(sym)), q)
        d, err = get_json(url, {"Accept": "application/json"})
        time.sleep(0.25)
        if d and (d.get("chart") or {}).get("result"):
            out = _parse_chart(d["chart"]["result"][0], now)
            break
    _YCACHE[key] = out
    return out


def _parse_chart(r, now=None):
    ts = r.get("timestamp") or []
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    cols = {k: q.get(k) or [None] * len(ts) for k in ("open", "high", "low", "close", "volume")}
    cut = utc_to_epoch(now) if now else None
    bars = []
    for i, t in enumerate(ts):
        o, h, l, c = cols["open"][i], cols["high"][i], cols["low"][i], cols["close"][i]
        if None in (o, h, l, c) or (cut is not None and t > cut):
            continue
        bars.append({"t": t, "o": float(o), "h": float(h), "l": float(l), "c": float(c),
                     "v": float(cols["volume"][i] or 0)})
    m = r.get("meta") or {}
    price = m.get("regularMarketPrice")
    if cut is not None and bars:
        price = bars[-1]["c"]
    return {"bars": bars, "meta": m, "price": float(price) if price else (bars[-1]["c"] if bars else None)}


def yahoo_spark_daily(symbols, now=None, rng="1y", pause=0.25):
    """Daily closes for up to 20 symbols per request: the cheap way to screen thousands of charts."""
    out = {}
    symbols = list(dict.fromkeys(symbols))
    for i in range(0, len(symbols), 20):
        chunk = symbols[i:i + 20]
        q = urllib.parse.urlencode({"symbols": ",".join(ysym(s) for s in chunk), "range": rng, "interval": "1d"})
        d = None
        for host in ("query2", "query1"):
            d, err = get_json("https://%s.finance.yahoo.com/v7/finance/spark?%s" % (host, q), {"Accept": "application/json"})
            time.sleep(pause)
            if d and (d.get("spark") or {}).get("result"):
                break
        back = {ysym(s): s for s in chunk}
        for r in ((d or {}).get("spark") or {}).get("result") or []:
            resp = (r.get("response") or [None])[0]
            if not resp:
                continue
            m = resp.get("meta") or {}
            ts = resp.get("timestamp") or []
            closes = (((resp.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
            cut = utc_to_epoch(now) if now else None
            bars = [{"t": t, "o": c, "h": c, "l": c, "c": c, "v": 0.0} for t, c in zip(ts, closes)
                    if c is not None and (cut is None or t <= cut)]
            if not bars:
                continue
            if cut is None and m.get("regularMarketDayHigh"):
                bars[-1].update({"h": float(m["regularMarketDayHigh"]), "l": float(m["regularMarketDayLow"])})
            price = bars[-1]["c"] if cut is not None else float(m.get("regularMarketPrice") or bars[-1]["c"])
            out[back.get(r.get("symbol"), r.get("symbol"))] = {"bars": bars, "meta": m, "price": price}
    return out


def bar_day(t, utc_days=False):
    u = epoch_to_utc(t)
    return (u if utc_days else to_ny(u)).date()


def sma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


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


def daily_view(chart, today, utc_days=False, frac_elapsed=1.0):
    """Indicators from daily bars. The last bar is today's (partial during the session)."""
    if not chart or len(chart["bars"]) < 30:
        return None
    bars = [b for b in chart["bars"] if bar_day(b["t"], utc_days) <= today]
    if len(bars) < 30:
        return None
    price = chart["price"] or bars[-1]["c"]
    is_today = bar_day(bars[-1]["t"], utc_days) == today
    hist = bars[:-1] if is_today else bars
    t_bar = bars[-1] if is_today else None
    closes = [b["c"] for b in hist] + [price]
    prev_close = hist[-1]["c"]
    day_high = max(t_bar["h"], price) if t_bar else price
    day_low = min(t_bar["l"], price) if t_bar else price
    vol_today = t_bar["v"] if t_bar else 0.0
    avg_vol = sum(b["v"] for b in hist[-20:]) / max(1, len(hist[-20:]))
    pace = (vol_today / max(frac_elapsed, 0.1)) / avg_vol if avg_vol > 0 else 0.0
    trs = [max(b["h"] - b["l"], abs(b["h"] - a["c"]), abs(b["l"] - a["c"])) for a, b in zip(hist[:-1], hist[1:])]
    atr = sum(trs[-14:]) / 14 if len(trs) >= 14 else None
    high20 = max(b["h"] for b in hist[-20:])
    low20 = min(b["l"] for b in hist[-20:])
    meta = chart.get("meta") or {}
    return {
        "price": price, "prevClose": prev_close,
        "movePct": (price / prev_close - 1) * 100 if prev_close else 0.0,
        "dayHigh": day_high, "dayLow": day_low,
        "upperHalf": price >= (day_high + day_low) / 2,
        "sma20": sma(closes, 20), "sma50": sma(closes, 50), "sma200": sma(closes, 200),
        "sma50_5ago": sma(closes[:-5], 50) if len(closes) > 55 else None,
        "rsi": rsi14(closes), "atrPct": (atr / price * 100) if atr and price else None,
        "high20": high20, "low20": low20,
        "newHigh20": day_high > high20, "newLow20": day_low < low20,
        "volPace": pace, "avgVol": avg_vol,
        "dollarVol": float(meta.get("regularMarketVolume") or vol_today or 0) * price,
        "ohlc": [[b["o"], b["h"], b["l"], b["c"]] for b in hist[-100:]],
    }


def tech_score(dv, side):
    """0-30: the chart's trend, momentum and volume, read for a buy (long) or a short."""
    if not dv:
        return 0
    p, s20, s50, s50o, s200, rsi = dv["price"], dv["sma20"], dv["sma50"], dv["sma50_5ago"], dv["sma200"], dv["rsi"]
    t = 0
    if side == "long":
        t += 6 if s50 and p > s50 else 0
        t += 4 if s50 and s50o and s50 > s50o else 0
        t += 5 if s200 and p > s200 else 0
        t += 5 if rsi and 45 <= rsi <= 70 else 0
        t += 5 if dv["price"] >= dv["high20"] * 0.97 else 0
    else:
        t += 6 if s50 and p < s50 else 0
        t += 4 if s50 and s50o and s50 < s50o else 0
        t += 5 if s200 and p < s200 else 0
        t += 5 if rsi and 30 <= rsi <= 55 else 0
        t += 5 if dv["price"] <= dv["low20"] * 1.03 else 0
    t += 5 if dv["volPace"] >= 1.2 else 0
    return t


# ---------------------------------------------------------------- universes (all of them)
LEVERAGED = re.compile(r"\b(2x|3x|4x|5x|1\.5x|2\.0x|ultra|ultrapro|inverse|bear|bull|short|leveraged|daily|-1x|-2x|-3x|"
                       r"etn|volatility|vix|option income|covered call|buffer)\b", re.I)


def stock_universe(work):
    """Every US-listed common stock, ordinary share and ADR (Nasdaq Trader's lists, via the pack)."""
    code, data, tail = pack("scan.py", ["universe", "--json"], work)
    try:
        lines = open(os.path.join(work, "trader", "universe.txt")).read().splitlines()
    except OSError:
        log("Stock list unavailable: %s" % tail[-120:])
        return []
    out = []
    for line in lines:
        if "|" in line:
            sym, name = line.split("|", 1)
            out.append((sym.strip(), name.strip()))
    return out


def etf_universe():
    """Every US-listed ETF, minus leveraged, inverse, volatility and option-income funds."""
    out = []
    for url, sym_i, etf_i in (("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", 0, 6),
                              ("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt", 0, 4)):
        try:
            s, b = http(url)
        except ConnectionError:
            continue
        if s != 200:
            continue
        for line in b.decode("latin-1").splitlines()[1:]:
            f = line.split("|")
            if len(f) <= etf_i or f[etf_i] != "Y":
                continue
            sym, name = f[sym_i], f[1]
            if not re.match(r"^[A-Z]{1,5}$", sym) or LEVERAGED.search(name):
                continue
            out.append((sym, name))
    return out


CG_EXCLUDE = {
    # stablecoins and cash-like tokens
    "usdt", "usdc", "dai", "usde", "fdusd", "pyusd", "usds", "tusd", "usdd", "busd", "usd1", "rlusd", "susde",
    "usdg", "usdy", "usyc", "usdf", "bfusd", "usdgo", "ousd", "gho", "ustb", "usd0", "eurc", "eursafo", "buidl",
    "jaaa", "figr_heloc", "usdtb", "usdx", "frax", "lusd", "crvusd",
    # wrapped, staked and gold-backed copies
    "wbtc", "weth", "steth", "wsteth", "weeth", "cbbtc", "reth", "bsc-usd", "wbeth", "jitosol", "lbtc", "xaut", "paxg",
    "meth", "ezeth", "rseth", "solvbtc", "bnsol", "msol", "jupsol", "clbtc", "tbtc",
    # meme coins and exchange tokens
    "doge", "shib", "pepe", "trump", "bonk", "wif", "floki", "pump", "pengu", "fartcoin", "spx", "brett",
    "leo", "okb", "bgb", "kcs", "gt", "htx", "wbt", "cro", "mnt", "nexo",
}
YAHOO_CRYPTO = {"HYPE": "HYPE32196-USD", "SUI": "SUI20947-USD", "TAO": "TAO22974-USD", "APT": "APT21794-USD",
                "UNI": "UNI7083-USD", "TON": "TON11419-USD", "ARB": "ARB11841-USD", "OP": "OP-USD",
                "STX": "STX4847-USD", "RENDER": "RENDER-USD", "INJ": "INJ-USD", "MNT": "MNT27075-USD",
                "ENA": "ENA-USD", "ONDO": "ONDO-USD", "WLD": "WLD-USD", "POL": "POL28321-USD",
                "JUP": "JUP29210-USD", "ZRO": "ZRO26997-USD", "KAS": "KAS-USD", "ICP": "ICP-USD", "AAVE": "AAVE-USD"}
CRYPTO_FALLBACK = ["BTC", "ETH", "XRP", "BNB", "SOL", "TRX", "ADA", "LINK", "XLM", "LTC", "AVAX", "DOT", "BCH", "NEAR"]


def crypto_yahoo(sym):
    return YAHOO_CRYPTO.get(sym, sym + "-USD")


def crypto_universe():
    """The 100 largest coins (CoinGecko), minus stablecoins, wrapped copies, meme coins and exchange tokens."""
    url = "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=100&page=1"
    d, err = get_json(url, {"Accept": "application/json"})
    if not isinstance(d, list) or not d:
        log("CoinGecko unavailable (%s): using the fixed coin list" % err)
        return CRYPTO_FALLBACK, "fixed list"
    out = []
    for c in d:
        s = str(c.get("symbol", "")).lower()
        if s in CG_EXCLUDE or not re.match(r"^[a-z0-9]{2,10}$", s):
            continue
        out.append(s.upper())
    return out or CRYPTO_FALLBACK, "CoinGecko top 100"


# ---------------------------------------------------------------- Nasdaq (free public pages)
NASDAQ_H = {"User-Agent": BROWSER_UA, "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}


def _num(s):
    s = str(s if s is not None else "")
    neg = "(" in s and ")" in s and re.search(r"\(\s*\$?[\d,.]+\s*\)", s)
    m = re.search(r"-?[\d,]+(\.\d+)?", s)
    if not m:
        return None
    try:
        v = float(m.group(0).replace(",", ""))
    except ValueError:
        return None
    return -abs(v) if neg else v


def nasdaq_earnings(day):
    url = "https://api.nasdaq.com/api/calendar/earnings?date=%s" % day.isoformat()
    d, err = get_json(url, NASDAQ_H, timeout=20)
    time.sleep(0.3)
    if not d or "data" not in d:
        return None
    out = {}
    for r in (d.get("data") or {}).get("rows") or []:
        sym = str(r.get("symbol", "")).upper().strip()
        if not sym:
            continue
        timing = str(r.get("time", ""))
        out[sym] = {"timing": "before-open" if "pre" in timing else ("after-close" if "after" in timing else "unknown"),
                    "eps": _num(r.get("eps")) if r.get("eps") not in (None, "", "N/A") else None,
                    "epsForecast": _num(r.get("epsForecast")) if r.get("epsForecast") not in (None, "", "N/A") else None}
    return out


def nasdaq_surprise(sym):
    """The last reported quarter: profit per share against what analysts expected."""
    d, err = get_json("https://api.nasdaq.com/api/company/%s/earnings-surprise" % sym, NASDAQ_H)
    time.sleep(0.3)
    rows = (((d or {}).get("data") or {}).get("earningsSurpriseTable") or {}).get("rows") or []
    if not rows:
        return None
    r = rows[0]
    try:
        m, dd, y = (int(x) for x in str(r.get("dateReported", "")).split("/"))
        reported = dt.date(y, m, dd).isoformat()
    except ValueError:
        reported = ""
    return {"reported": reported, "eps": _num(r.get("eps")), "consensus": _num(r.get("consensusForecast")),
            "surprisePct": _num(r.get("percentageSurprise")), "quarter": r.get("fiscalQtrEnd")}


def _table(d, name):
    t = (d or {}).get(name) or {}
    rows = {str(r.get("value1", "")).strip(): r for r in t.get("rows") or []}
    return rows


def nasdaq_fundamentals(sym):
    """Sales and profit growth, debt, free cash flow and analysts' 1-year target, from Nasdaq's public pages."""
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
    fin, err = get_json("https://api.nasdaq.com/api/company/%s/financials?frequency=2" % sym, NASDAQ_H)
    time.sleep(0.3)
    fd = (fin or {}).get("data") or {}
    bs, cf = _table(fd, "balanceSheetTable"), _table(fd, "cashFlowTable")
    liab = _num((bs.get("Total Liabilities") or {}).get("value2"))
    eq = _num((bs.get("Total Equity") or {}).get("value2"))
    debt = sum(x for x in (_num((bs.get("Long-Term Debt") or {}).get("value2")),
                           _num((bs.get("Short-Term Debt / Current Portion of Long-Term Debt") or {}).get("value2"))) if x)
    ocf = _num((cf.get("Net Cash Flow-Operating") or {}).get("value2"))
    capex = _num((cf.get("Capital Expenditures") or {}).get("value2"))
    out["debtToEquity"] = round(debt / eq, 2) if eq and eq > 0 else (None if eq is None else 99.0)
    out["freeCashFlow"] = (ocf + capex) if ocf is not None and capex is not None else ocf
    return out


def fundamental_score(f, price):
    """0-15 from real numbers: growth (5), profit (4), valuation and analysts (3), balance sheet (3)."""
    if not f or (f.get("revGrowthPct") is None and f.get("ttmEps") is None):
        return None, "not available"
    g = f.get("revGrowthPct")
    growth = 0 if g is None else (5 if g >= 15 else 4 if g >= 5 else 2 if g >= 0 else 0)
    profit = 0
    if f.get("lastEps") is not None and f["lastEps"] > 0:
        profit += 2
        eg = f.get("epsGrowthPct")
        profit += 2 if eg is not None and eg >= 10 else 1 if eg is not None and eg >= 0 else 0
    val = 0
    pe = price / f["ttmEps"] if f.get("ttmEps") and f["ttmEps"] > 0 and price else None
    if pe is not None:
        val += 2 if pe <= 35 else 1 if pe <= 60 else 0
    tgt = f.get("target1y")
    if tgt and price:
        val += 1 if tgt >= price * 1.05 else 0
    bal = 0
    de = f.get("debtToEquity")
    bal += 2 if de is not None and de <= 1.0 else 1 if de is not None and de <= 2.5 else 0
    bal += 1 if (f.get("freeCashFlow") or 0) > 0 else 0
    parts = []
    if g is not None:
        parts.append("sales %+.0f%%" % g)
    if pe is not None:
        parts.append("P/E %.0f" % pe)
    if de is not None:
        parts.append("debt/equity %.1f" % de)
    if tgt and price:
        parts.append("target %+.0f%%" % ((tgt / price - 1) * 100))
    return min(15, growth + profit + min(3, val) + min(3, bal)), ", ".join(parts)


def nasdaq_option_chain(sym, d0, d1, money="at"):
    """Calls and puts with bid, ask and open interest for expiries between d0 and d1."""
    q = urllib.parse.urlencode({"assetclass": "stocks", "limit": 400, "fromdate": d0.isoformat(),
                                "todate": d1.isoformat(), "money": money, "type": "all"})
    d, err = get_json("https://api.nasdaq.com/api/quote/%s/option-chain?%s" % (sym, q), NASDAQ_H)
    time.sleep(0.3)
    rows = ((((d or {}).get("data") or {}).get("table") or {}).get("rows")) or []
    out, exp = [], None
    for r in rows:
        if r.get("expirygroup"):
            try:
                exp = dt.datetime.strptime(r["expirygroup"].strip(), "%B %d, %Y").date()
            except ValueError:
                exp = None
            continue
        if not exp or not r.get("strike"):
            continue

        def leg(p):
            return {"bid": _num(r.get(p + "_Bid")) or 0.0, "ask": _num(r.get(p + "_Ask")) or 0.0,
                    "last": _num(r.get(p + "_Last")) or 0.0, "oi": _num(r.get(p + "_Openinterest")) or 0.0,
                    "vol": _num(r.get(p + "_Volume")) or 0.0}
        out.append({"expiry": exp, "strike": _num(r["strike"]), "call": leg("c"), "put": leg("p")})
    return out


# ---------------------------------------------------------------- SEC earnings releases (needs a contact email)
_SEC_TICKERS = {}


def sec_get(url, contact, accept="application/json"):
    return http(url, {"User-Agent": "trading-agent research %s" % contact, "Accept": accept}, timeout=30, tries=2)


def sec_cik(sym, contact):
    if not _SEC_TICKERS:
        s, b = sec_get("https://www.sec.gov/files/company_tickers.json", contact)
        if s != 200:
            return None
        for v in json.loads(b).values():
            _SEC_TICKERS[str(v.get("ticker", "")).upper()] = int(v["cik_str"])
        time.sleep(0.2)
    return _SEC_TICKERS.get(sym.upper().replace("-", "."))


def html_text(raw):
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def sec_earnings_release(sym, since, contact):
    """The company's own earnings press release (8-K item 2.02, exhibit 99.1) filed on or after `since`."""
    cik = sec_cik(sym, contact)
    if not cik:
        return None
    s, b = sec_get("https://data.sec.gov/submissions/CIK%010d.json" % cik, contact)
    time.sleep(0.2)
    if s != 200:
        return None
    rec = (json.loads(b).get("filings") or {}).get("recent") or {}
    for i, form in enumerate(rec.get("form") or []):
        if form != "8-K" or "2.02" not in str((rec.get("items") or [""] * (i + 1))[i]):
            continue
        filed = rec["filingDate"][i]
        if filed < since:
            break
        acc = rec["accessionNumber"][i].replace("-", "")
        base = "https://www.sec.gov/Archives/edgar/data/%d/%s/" % (cik, acc)
        s, b = sec_get(base + "index.json", contact)
        time.sleep(0.2)
        if s != 200:
            return None
        items = ((json.loads(b).get("directory") or {}).get("item")) or []
        docs = [it["name"] for it in items if str(it.get("name", "")).lower().endswith((".htm", ".html"))]
        ex = [n for n in docs if re.search(r"ex-?99", n, re.I)] or [n for n in docs if n != rec["primaryDocument"][i]]
        if not ex:
            return None
        s, b = sec_get(base + ex[0], contact, "text/html")
        if s != 200:
            return None
        return {"filed": filed, "url": base + ex[0], "text": html_text(b.decode("utf-8", "replace"))[:200000]}
    return None


GUIDE_UP = re.compile(r"\b(rais(e|es|ed|ing)|increas(e|es|ed|ing)|lift(s|ed|ing)?|boost(s|ed)?)\b[^.]{0,80}"
                      r"\b(guidance|outlook|forecast|full[- ]year)", re.I)
GUIDE_DOWN = re.compile(r"\b(lower(s|ed|ing)?|cut(s|ting)?|reduc(e|es|ed|ing)|withdr(aw|ew|awn))\b[^.]{0,80}"
                        r"\b(guidance|outlook|forecast|full[- ]year)", re.I)


def read_release(text):
    """Read an earnings press release: the tone of the sentences about results and outlook, and guidance."""
    sents = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text)
    key = [s.strip() for s in sents if 40 <= len(s) <= 400 and re.search(
        r"revenue|sales|earnings per share|\bEPS\b|guidance|outlook|expect|forecast|margin|record|growth|declin|demand",
        s, re.I)][:40]
    scores = classify_texts(key) if key else {}
    tone = sum(p - n for p, n in scores.values()) / len(scores) if scores else 0.0
    guide = "raised" if GUIDE_UP.search(text) else "lowered" if GUIDE_DOWN.search(text) else ""
    best = max(scores.items(), key=lambda kv: kv[1][0] - kv[1][1])[0] if scores else ""
    worst = min(scores.items(), key=lambda kv: kv[1][0] - kv[1][1])[0] if scores else ""
    return {"tone": round(tone, 2), "guidance": guide, "sentences": len(key), "best": best[:160], "worst": worst[:160]}


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


# ---------------------------------------------------------------- replay and order check
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
    """10,000 replays of the next few days from the market's own past days, for a long or a short."""
    side = p.get("side", "long")
    entry, stop, target = float(p["entry"]), float(p["stop"]), float(p["target"])
    if side == "long" and not (stop < entry < target):
        return {"error": "need stop < entry < target"}
    if side == "short" and not (target < entry < stop):
        return {"error": "need target < entry < stop"}
    pool, kind = _pool(p["bars"])
    if len(pool) < 40:
        return {"error": "need at least 40 days of prices, got %d" % len(pool)}
    n, days = int(p.get("replays", 10000)), int(p.get("maxDays", 5))
    risk = abs(entry - stop)
    cost_r = float(p.get("costPct", 0.002)) * entry / risk
    rng = random.Random(int(p.get("seed", 19)))
    counts = {"target": 0, "stop": 0, "time": 0}
    rs = []
    for _ in range(n):
        px, how, exit_px = entry, "time", None
        for _d in range(days):
            ro, rh, rl, rc = pool[rng.randrange(len(pool))]
            o, h, l, c = px * ro, px * rh, px * rl, px * rc
            if side == "long":
                if o <= stop:
                    how, exit_px = "stop", o
                elif o >= target:
                    how, exit_px = "target", o
                elif l <= stop:
                    how, exit_px = "stop", stop
                elif h >= target:
                    how, exit_px = "target", target
            else:
                if o >= stop:
                    how, exit_px = "stop", o
                elif o <= target:
                    how, exit_px = "target", o
                elif h >= stop:
                    how, exit_px = "stop", stop
                elif l <= target:
                    how, exit_px = "target", target
            if exit_px is not None:
                break
            px = c
        if exit_px is None:
            exit_px = px
        counts[how] += 1
        move = (exit_px - entry) if side == "long" else (entry - exit_px)
        rs.append(move / risk - cost_r)
    mean_r = round(sum(rs) / len(rs), 3)
    p_stop = round(counts["stop"] / n, 3)
    return {"replays": n, "daysHeld": days, "poolDays": len(pool), "data": kind,
            "pTarget": round(counts["target"] / n, 3), "pStop": p_stop, "pTime": round(counts["time"] / n, 3),
            "meanR": mean_r, "favourable": mean_r >= 0.05 and p_stop <= 0.60}


def order_check(o):
    """The risk manager's pass/fail card. Every number is worked out before; this only judges."""
    rules = []

    def add(name, ok, detail):
        rules.append({"rule": name, "ok": bool(ok), "detail": detail})

    def g(k, d=0.0):
        return float(o.get(k, d) or 0)
    rep = o.get("replay") or {}
    risk, loss = g("riskCad"), g("lossAtStopCad")
    day_used = max(0.0, g("dayStartEquityCad") - g("equityCad"))
    add("Buying allowed now", o.get("allowed"), o.get("blockReason") or "risk gate open")
    add("Allowed market", o.get("universeOk", True), str(o.get("symbol", "")))
    add("Not already held", not o.get("alreadyHeld"), "one position per symbol and direction")
    add("Score high enough", g("score") >= g("scoreMin", 70), "%g of %g needed" % (g("score"), g("scoreMin", 70)))
    add("Setup valid", o.get("setup") in SETUPS and not o.get("setupDisabled"), str(o.get("setup")))
    add("No earnings block", not o.get("earningsBlocked"), "no report due, no result against the trade")
    add("Not chasing", g("moveWithTradePct") <= RULES["max_move_today_pct"],
        "moved %.1f%% today in the trade's direction, limit 10%%" % g("moveWithTradePct"))
    add("Stop on the right side", o.get("stopOk"), "stop %s, entry %s" % (o.get("stop"), o.get("entry")))
    add("Reward is 2x risk", g("rewardRisk") >= 1.99, "%.2f to 1" % g("rewardRisk"))
    add("Loss at stop fits budget", loss <= 1.5 * risk, "CAD %.2f at the stop with costs, budget %.2f" % (loss, risk))
    add("Size within limit", 0 < g("sizeCad") <= g("sizeCapCad"), "CAD %.2f, limit %.2f" % (g("sizeCad"), g("sizeCapCad")))
    add("Cash or margin available", g("cashNeededCad") <= g("cashFreeCad"),
        "needs CAD %.2f, free %.2f" % (g("cashNeededCad"), g("cashFreeCad")))
    add("Position count", g("positionsOfKind") < g("maxPositionsOfKind"),
        "%d of %d" % (g("positionsOfKind"), g("maxPositionsOfKind")))
    add("Trades today and this week", g("entriesToday") < RULES["entries_per_day"]
        and g("entriesThisWeek") < RULES["entries_per_week"],
        "%d today, %d this week" % (g("entriesToday"), g("entriesThisWeek")))
    add("Daily loss limit", day_used + loss <= RULES["day_loss_pct"] * g("dayStartEquityCad"),
        "CAD %.2f lost today + %.2f at stop, limit %.2f" % (day_used, loss, RULES["day_loss_pct"] * g("dayStartEquityCad")))
    add("Total open risk", g("openRiskCad") + loss <= RULES["max_open_risk_pct"] * g("equityCad"),
        "CAD %.2f open + %.2f, limit %.2f" % (g("openRiskCad"), loss, RULES["max_open_risk_pct"] * g("equityCad")))
    add("Time machine favourable", rep.get("favourable"),
        "avg %+.2fR, stop first %.0f%%" % (float(rep.get("meanR") or 0), 100 * float(rep.get("pStop") if rep.get("pStop") is not None else 1)))
    failed = [r["rule"] for r in rules if not r["ok"]]
    return {"verdict": "BLOCK" if failed else "PASS", "rules": rules, "failed": failed}


# ---------------------------------------------------------------- reading the news
POS_WORDS = re.compile(r"\b(beat|beats|tops|topped|raises|raised|lifts|upgrade[sd]?|record|wins|won|awarded|contract|"
                       r"approval|approved|clears|partnership|buyback|surges?|soars?|jumps?|rall(y|ies)|outperform|"
                       r"strong demand|guidance (raise|hike))\b", re.I)
NEG_WORDS = re.compile(r"\b(miss|misses|missed|cuts?|downgrade[sd]?|lawsuits?|sued|sues|probes?|investigations?|"
                       r"recalls?|halt(ed)?|offering|plunges?|slumps?|tumbles?|sinks?|warns?|warning|bankruptcy|"
                       r"fraud|delay(s|ed)?|layoffs?|resigns?|subpoena|short seller|downbeat|settlement|fined|ban(s|ned)?)\b", re.I)
SERIOUS = re.compile(r"\b(lawsuits?|sued|probes?|investigations?|recalls?|halt(ed)?|bankruptcy|fraud|subpoena|fined|"
                     r"short seller|resigns?|ban(s|ned)?)\b", re.I)
CATALYST_TAGS = {"earnings", "analyst", "deal", "regulatory"}
NEWS_MODEL_ID = "mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis"
NEWS_MODEL_REV = "ae0eab9ad336d7d548e0efe394b07c04bcaf6e91"
_NEWS_MODEL = None


def about(title, sym, aliases, name=None):
    """True when a headline names this company or coin (Yahoo's feeds mix in other stories)."""
    if re.search(r"\(%s\)" % re.escape(sym), title) or (len(sym) >= 3 and re.search(r"\b%s\b" % re.escape(sym), title)):
        return True
    alias = aliases.get(sym)
    if alias and re.search(r"\b(%s)" % alias, title, re.I):
        return True
    if name:
        word = re.sub(r"[^A-Za-z&' -]", " ", name).split()
        word = [w for w in word if w.lower() not in ("the", "inc", "corp", "co", "class", "holdings", "group", "common",
                                                    "stock", "shares", "ordinary", "company", "plc", "ltd", "limited")]
        if word and len(word[0]) >= 4 and re.search(r"\b%s\b" % re.escape(word[0]), title, re.I):
            return True
    return False


def news_model():
    """A free, open-source finance-news sentiment model (Apache-2.0), run on GitHub's computer."""
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
    texts = list(texts)
    out = {}
    model = news_model() if texts else False
    if model:
        try:
            res = model(texts, truncation=True, max_length=256, batch_size=16)
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


def read_news(items, sym, aliases=None, feed=None, scores=None, name=None):
    """Judge the last 12 hours of headlines about one company or coin. 12 of 25 means no news."""
    feed = feed or sym
    rel = [it for it in items if feed in (it.get("touches") or [it.get("ticker")])
           and about(str(it.get("title", "")), sym, aliases or {}, name)]
    rel.sort(key=lambda it: it.get("time") or "", reverse=True)
    rel = rel[:6]
    scores = scores if scores is not None else classify_texts([news_text(it) for it in rel])
    pos = neg = serious = 0
    net = 0.0
    catalyst = bad_catalyst = False
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
            if n >= 0.7 and (tags & CATALYST_TAGS or SERIOUS.search(title) or re.search(r"miss|downgrade|cut|warn", title, re.I)):
                bad_catalyst = True
    score = int(round(max(0, min(25, 12 + 5 * max(-2.4, min(2.6, net))))))
    return {"score": score, "pos": pos, "neg": neg, "serious": serious, "catalyst": catalyst,
            "badCatalyst": bad_catalyst, "headlines": [str(it.get("title", "")) for it in rel[:3]],
            "brokenLong": pos == 0 and (neg >= 2 or serious >= 1),
            "brokenShort": neg == 0 and pos >= 2}


# ---------------------------------------------------------------- state storage
def empty_state(start=START_CAD):
    return {
        "schema": 2,
        "fund": {"phase": "paper", "startEquityCad": start, "startDate": "", "usd": 0.0, "cad": start,
                 "equityCad": start, "peakEquityCad": start, "maxDrawdownPct": 0.0, "benchmarkCad": start,
                 "benchmarkSet": False, "tradingDays": 0, "lastTradingDay": "", "dayDate": "",
                 "dayStartEquityCad": start, "entriesToday": 0, "weekStart": "", "weekStartEquityCad": start,
                 "entriesThisWeek": 0, "halted": False, "haltReason": "", "ruleBreaks": 0,
                 "stats": {"closed": 0, "wins": 0, "totalR": 0.0,
                           "bySetup": {k: {"n": 0, "wins": 0, "r": 0.0, "disabled": False} for k in SETUPS}}},
        "lessons": [], "positions": {}, "trades": [], "nav": [], "journal": [],
        "alerts": {}, "earnings": {}, "congress": {}, "runs": {}, "fundamentals": {},
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
        else:
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
    """Post the alert as a comment on one issue assigned to the owner, so GitHub notifies them."""
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
            intro = ("Every trade, stop-loss, profit target, price alert and halt from the trading agent is posted "
                     "here as a comment, plus a short summary after each market day. Paper trading only.")
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


def r2(x):
    return round(float(x) + 1e-12, 2)


def money(x):
    return ("-$" if x < 0 else "$") + "{:,.2f}".format(abs(x))


def load_config():
    with open(os.path.join(HERE, "config.json")) as f:
        return json.load(f)
