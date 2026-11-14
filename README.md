# $100 Trading Agent

An automatic **paper-trading** agent (pretend money) for US stocks, ETFs and crypto.
It runs by itself on GitHub's free computers, around the clock, with no AI and no cost:

- every 15 minutes while US markets can be open, Monday to Friday
- every 2 hours, all week, for crypto and news

Each run checks prices, stop-losses, profit targets, news on what it holds and price
alerts. It looks for new stock trades about every 30 minutes from 10:15 AM to 2:30 PM
New York time and for crypto every 2 hours. At 3:20 PM it closes day trades and
reviews the day.

**Page:** https://cutedevil07.github.io/trading-agent.2/ (add it to your iPhone home screen)

It never places a real order. There is no broker code in this repository.

## On your iPhone

1. Install **ntfy** from the App Store (free). Tap **+**, subscribe to your topic name.
2. On github.com, in this repository: **Settings → Secrets and variables → Actions →
   New repository secret**. Name `NTFY_TOPIC`, value: the same topic name.
3. Open the page in Safari, tap Share → **Add to Home Screen**.

You get an alert when the agent buys or sells, a stop or target is hit, an alert
price is reached or buying is halted, plus a one-line summary after each market day.

## Running it by hand

GitHub app or github.com → this repository → **Actions → agent → Run workflow**.
Type `run` for a normal run or `selftest` to check every data source and send a test alert.

## Stopping it

**Actions → agent → ⋯ → Disable workflow.** Enable it again the same way.

## What it uses (all free)

| what | source |
| --- | --- |
| stock, ETF, crypto and USD/CAD prices, 5-minute and daily bars | Yahoo Finance (unofficial, delayed) |
| headlines | Yahoo Finance RSS, read by the pack's news tool |
| opening-range breakouts, entry timing, exit plan, 10,000-replay time machine, risk check | [Day Trader skill pack](https://github.com/Jakeschincariol/day-trader-skill) by Jake Schincariol, MIT, commit `3d0f4ef`, vendored in `pack/` |
| top-10 coins | CoinGecko (falls back to a fixed list) |
| earnings dates and results | Nasdaq's public calendar (if unreachable, stocks are never held overnight) |
| Congress trade reports | US House Clerk |
| alerts | ntfy.sh |

## Files

| file | what |
| --- | --- |
| `agent.py` | the agent: one run per call, standard-library Python |
| `config.json` | what it may trade, and the names used to match headlines |
| `docs/state.json` | everything it knows: cash, holdings, trades, history (written by each run) |
| `docs/index.html` | the page |
| `.github/workflows/agent.yml` | the schedule |
| `pack/` | the seven Day Trader pack tools it uses |

The rules are listed in full on the page, under "The eleven tools and their rules".

Paper results use real market prices and modelled costs. Not financial advice.
