# Trading Agent

An automatic **paper-trading** agent (pretend money: CAD 10,000) that studies the whole US market
and trades whatever passes its rules: long or short, shares or options, plus micro futures and crypto.
It runs by itself on GitHub's free computers at no cost:

- every 5 minutes while US markets can be open, Monday to Friday
- every 30 minutes, all week, for crypto, futures and news

**Page:** https://mxmedits.github.io/trading-agent.2/ (add it to your iPhone home screen).
**Alerts:** every trade, stop, target, earnings read and halt is posted to the issue
"Trading agent alerts", which GitHub sends to you in its app and by email.

It never places a real order. There is no broker code in this repository.

## What it studies

| market | how |
| --- | --- |
| every US-listed stock (about 6,000) | read each market morning before the open; the liquid ones (over $20M a day, $5 or more) ranked for buys and shorts; the best 100 rescanned every 10 minutes from 9:50 AM to 2:55 PM New York |
| ETFs | every US-listed ETF read weekly; the 300 most traded, without leveraged, inverse or volatility funds, join the daily screen |
| options | strong stock ideas (80+ points) may buy a call or a put instead of shares, 2 to 6 weeks out, near the money, liquid only |
| futures | micro S&P 500, Nasdaq, Dow, Russell, gold, silver, oil, bitcoin, ether, euro, long or short, when one contract fits the 1% risk |
| crypto | the 100 largest coins without stablecoins, wrapped copies, meme coins or exchange tokens, long only |

## Judgement

- **News:** each headline about the company or coin is read by the open-source finance-news model
  `mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis` (Apache-2.0), on GitHub's computer.
- **Fundamentals:** sales and profit growth, price/earnings, debt, free cash flow and analysts' targets (Nasdaq);
  for close calls, the numbers the company filed with the SEC take over (the pack's earnings analyst).
- **Earnings:** report dates (Nasdaq); after a report, profit against forecast, sales growth, and the tone and
  outlook of the company's own press release from the SEC.
- **Congress:** every new House trade report, read from the PDF.
- **Time machine:** 10,000 replays before every trade (the Day Trader pack's 5-minute replay for breakouts and breakdowns).
- **Sizing:** every stock or ETF buy is whole shares, sized by the pack's Budget Manager inside four limits
  (max loss, position cap, max order, cash); a stop too far away means no trade, not a smaller one.
- **Tickets:** the pack's Order Executor writes the plain ticket for each paper buy (shares, stop, target,
  max loss). It never records a yes and never contacts a broker.
- **Market clock and alerts:** the pack's NYSE clock double-checks the market is open before a stock trade,
  and a phone alert goes out when anything held moves a lot in a day (4% stocks, 6% crypto, 2.5% futures).

The rules are listed in full on the page under "How it decides".

## Real money

Set in `config.json` under `live`: at most **CAD 1,000** in total, about **$10 risked per trade**.
Switch every new real trade off at once with `"enabled": false` (edit the file in the GitHub app).

**Crypto on Kraken, fully automatic (budget $300).** Buys only (Kraken offers Canadians no margin or
derivatives). Every buy is protected at once by a stop-loss order resting at Kraken, so it works even
between runs. Real buying stops if real losses reach 15%. Every order carries the agent's own tag, so it
never sells coins you bought yourself. Only coins Kraken sells for Canadian dollars can be bought for
real (in November 2026: BTC, ETH, SOL, XRP, DOGE, PEPE, XDC); the paper book still trades the top 100.

To switch it on: in Kraken, Settings → API → create a key with only *Query Funds*, *Query Open Orders &
Trades*, *Query Closed Orders & Trades*, *Create & Modify Orders* and *Cancel/Close Orders* (never
*Withdraw*), then add two repository secrets on github.com: `KRAKEN_API_KEY` and `KRAKEN_API_SECRET`.
The agent checks the key each day and refuses to trade if it could withdraw money.

**Stocks and ETFs on Questrade, approved by you (budget $700).** Questrade lets only partner companies
place orders by program. The agent writes each long trade as a signal in `docs/state.json`
(`live.stocks`); Claude's Questrade connection sends it to your Questrade app, and nothing happens
unless you approve it there. Needs an active self-directed Questrade account with push-to-approve set up.

## One optional setting: reading companies' own earnings reports

The SEC asks every program to give a contact email. To let the agent read press releases from the SEC,
add a repository secret on github.com: **Settings → Secrets and variables → Actions → New repository secret**,
name `SEC_CONTACT`, value: your email. It is only sent to the SEC and never appears in the code.
It also turns on the SEC's filed numbers (sales, margins, profit per share) for close calls.
Without it, earnings are judged from Nasdaq's numbers and the earnings headlines.

## Running it by hand, or stopping it

GitHub app or github.com → this repository → **Actions → agent → Run workflow** (`run`, `selftest` or `probe`).
To stop it: **Actions → agent → ⋯ → Disable workflow**.

## Files

| file | what |
| --- | --- |
| `agent.py` | the trading logic: one run per call |
| `core.py` | clock, free data sources, judgement, paper-trading maths, storage, alerts |
| `config.json` | fallback lists and the names used to match headlines |
| `docs/state.json` | everything it knows: cash, holdings, trades, history (written by each run) |
| `docs/index.html` | the page (published from the `gh-pages` branch) |
| `.github/workflows/agent.yml` | the schedule |
| `requirements.txt` | the news model's libraries (installed on GitHub's computer) |
| `pack/` | all eleven [Day Trader skill pack](https://github.com/Jakeschincariol/day-trader-skill) tools (MIT, commit `3d0f4ef`) |

Data: Yahoo Finance (prices, headlines), Nasdaq (earnings, fundamentals, options), CoinGecko (coin list),
SEC (press releases), US House Clerk (Congress). All free, unofficial where noted, and delayed.
Paper results use real market prices and modelled costs. Not financial advice.
