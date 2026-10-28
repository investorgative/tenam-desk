# Build prompt / specification (TEN·AM Desk v1.0)

Use this to rebuild or extend the project with any coding model. It describes what exists in this repo.

---
Build a Python project for a Raspberry Pi that **paper-trades (live forward test) and backtests** the
"10am key open" strategy of the YouTuber Powell trades on two markets, with an IQ-Trader-style web dashboard.

**Markets**
* BTC: Bybit BTCUSDT linear perp, 1m candles from Bybit v5 public API (live + history), ETHUSDT as SMT/correlation
  pair. Costs: maker 0.02 % (limit entry, TP), taker 0.055 % (SL, BE, flatten), 0.01 % stop slippage, real funding
  history. Account $100, 2 % risk, max 50× leverage, qty step 0.001.
* NQ: broker IBKR. Live data Yahoo NQ=F + ES=F 1m (free, ~10 min delayed). Backtest data Dukascopy
  USATECHIDXUSD + USA500IDXUSD 1m (free, LZMA .bi5 day files, auto-detect field order and price divisor) or Yahoo
  (≤ 28 days). Fee profiles: IBKR MNQ $0.62/contract/side ($2/pt), IBKR NQ $2.25 ($20/pt), IBKR US Tech 100 CFD
  0.005 % (min $1) + 1 pt spread; 1 tick stop slippage. Account $5,000, 2 % risk.

**Strategy engine (shared 1:1 by paper trader and backtester, bar-by-bar on closed 1m candles, no lookahead)**
All times America/New_York (DST aware). Per trading day Mon–Fri:
1. Gate at 10:00: skip FOMC day, Monday without CPI/PPI/NFP/FOMC, overnight range (day start → 09:30) > 2.4 % NQ /
   5 % BTC or > 2.5× its 20-day median, 1m-return correlation with the pair 09:30–10:00 < 0.6 (0.5 BTC). Optional:
   skip CPI/PPI/NFP days, day before NFP. Built-in BLS/Fed calendar 2024–2027 + news.csv + ForexFactory week JSON.
2. Key open O10 = open of the 10:00 bar.
3. Manipulation: first excursion from O10 ≥ 0.03 % NQ / 0.06 % BTC; clean if the opposite excursion ≤ 0.012 % /
   0.03 %, else no trade (both sides in one bar = TWO_SIDED).
4. Confirmation: 2 consecutive 1m closes beyond O10 in the distribution direction AND a clock-aligned 5m close beyond
   O10; a new manipulation extreme resets both.
5. Entry: limit at O10 (fills on touch; gap-through at the open; SL checked inside the fill candle, worst case).
   Alternatives: rejection-block trigger on 1m/5m (wick ≥ 35 % of range, closes back through, near O10; entry body /
   CE / 25 %, stop beyond wick, valid 10 bars), or limit first then RB after a loss. TP touched before fill = RAN_AWAY.
6. Target: nearest untaken liquidity beyond entry (PD/PW/Asia 20–00/London 02–05/overnight/09:30–10:00 highs-lows,
   confirmed 5m swing pivots 2/2, 08:30 data candle on news days, midnight open), max 1.5 % NQ / 3 % BTC away.
   Stop modes: rr_target (dist = target ÷ 5, floor 0.08 % NQ / 0.15 % BTC — default), fib 0.5 of the leg,
   beyond manipulation extreme, fixed %. Max stop 0.6 % / 1.2 %. Min RR 1:3. Alternative fixed-R target.
7. Management: BE at +2 R (offset 0.05 R), flatten 15:50, SL wins if SL and TP in one candle, entry window to 11:30.
8. Daily rules: max 2 trades, stop after a win, after a loss re-arm (new manipulation = sweep of the range since 10:00
   ± tolerance) with 50 % risk; BE counts as scratch.
Sizing: risk ÷ (stop × point value + round-trip costs per unit), floor to qty step, leverage cap, minimum size check.

**Processes**
* `tenam_trader.py` in tmux "TEN": polls both markets (Bybit 10 s, Yahoo 30 s), caches candles in sqlite, feeds new
  closed bars into the persisted engine state (catch-up after restarts = replay of missed candles), stores trades/day
  records, Discord webhook per market (setups, orders, fills, exits, daily) + error webhook for every ERROR.
* `tenam_web.py` in tmux "TENweb", Flask/waitress on port 8757, Cloudflare friendly (relative URLs, polling,
  no-store, optional basic auth). APIs: state, chart (per day), logs, backtest job, variant lab (18 variants,
  first/second-half R).

**Dashboard** (dark, mono, cyan/magenta/yellow): header with BTC/NQ toggle, ticker, price vs 10am open, engine
status, data delay, countdown to 10:00 NY / entry window, Berlin + NY clocks, PAPER/BACKTEST/LIVE🔒 (live header
staged, not implemented), ticker tape. Sections: overview (hero + equity curve + all KPIs: trades, W/L, WR, cum R,
max DD R/%, account, risk, fee setup, days, net PnL, PF, CAGR, avg win/loss, streaks, avg RtR, planned RR, max daily
DD, total/avg fees, fees in R, funding, avg order, leverage, time in position, SL size, expectancy, SQN, Sharpe,
Kelly), live chart (1/3/5/15m, 10am-open & midnight-open segments, session levels, setup markers, trades, price lines
for position/order, day picker, SMT pane = % from each instrument's 10am open), strategy status (day gate checklist,
6-step 10am pipeline + event feed, levels & context), positions & resting orders, trade log (click → chart day),
day log of every session with outcome/reason, journal (year/month/4-week default/week/day drilldown, CSV export),
backtester (all parameters + filters + bias switches, default 365 days), variant lab, system log.
