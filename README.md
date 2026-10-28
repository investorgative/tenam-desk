# TEN·AM Desk — Powell's 10am key-open model · paper trader + backtester

Forward-test (paper) and backtest of the **10:00 New-York key-open strategy** (YouTuber "Powell trades")
on **NQ** (IBKR cost model) and **BTC** (Bybit), with an IQ-Trader-style dashboard on port **8757**.
Paper trader and backtester share **one engine** (`tenam_core.Engine`) — a backtest trade and a paper
trade are computed by exactly the same code, fed with the same closed 1m candles.

| File | Purpose |
|---|---|
| `tenam_core.py` | data feeds, NY session clock, levels, the 10am state machine, fees, sizing, stats, Discord |
| `tenam_trader.py` | paper trader for both markets (tmux session **TEN**) |
| `tenam_web.py` | dashboard + backtester + variant lab, port **8757** (tmux session **TENweb**) |
| `static/index.html` | the dashboard (BTC ⇄ NQ toggle in the header) |
| `config.json` | **your** overrides only (created on first start) · all defaults: `config.example.json` |
| `news.csv` | optional extra/corrected CPI/PPI/NFP/FOMC dates |
| `STRATEGY.md` | the strategy as extracted from 17 transcripts + every rule we mechanised + inconsistencies |
| `PROMPT.md` | full build spec / prompt (reusable for another model) |

---
## 1. Setup on the Raspberry Pi (Raspberry Pi OS Bookworm 64-bit)

```bash
# 1) clone (private repo → use a GitHub personal access token as password, or SSH)
cd ~
git clone https://github.com/investorgative/tenam-desk.git
cd tenam-desk

# 2) install (python venv, tmux, chart library)
chmod +x setup.sh && ./setup.sh

# 3) first start creates config.json - stop, edit, start again
./start.sh && sleep 5 && ./stop.sh
nano config.json
```

Put your three Discord webhooks into `config.json`:

```json
"discord": {
  "errors": "https://discord.com/api/webhooks/...   (every ERROR from both processes)",
  "btc":    "https://discord.com/api/webhooks/...   (BTC setups / orders / fills / exits / day summary)",
  "nq":     "https://discord.com/api/webhooks/...   (NQ  setups / orders / fills / exits / day summary)",
  "notify_setups": true, "notify_orders": true, "notify_trades": true, "notify_daily": true
}
```

Discord: *Server settings → Integrations → Webhooks → New webhook → Copy URL* (one per channel).

```bash
# 4) start both tmux sessions
./start.sh
```

Dashboard: `http://<pi-ip>:8757` (on the Pi: `http://localhost:8757`).

### Daily use
```bash
tmux attach -t TEN        # watch the paper trader  (detach: Ctrl+B, then D)
tmux attach -t TENweb     # watch the web server
./stop.sh && ./start.sh   # restart (after editing config.json)
./update.sh               # git pull newest version + restart (keeps config.json and data/)
./venv/bin/python tenam_trader.py --reset nq     # wipe one paper account (stop first): btc | nq | all
```
Autostart after reboot: `crontab -e` →
`@reboot sleep 40 && /home/<user>/tenam-desk/start.sh`

### Cloudflare tunnel
Point your tunnel at `http://localhost:8757` (relative URLs, plain HTTP polling, no websockets,
API responses `no-store`). The page is public then — set `"web": {"password": "..."}` in `config.json`
(browser login, any username) or protect it with Cloudflare Access.

---
## 2. Markets, data and costs

| | BTC | NQ |
|---|---|---|
| live (paper) data | Bybit BTCUSDT 1m (real-time) | Yahoo **NQ=F** 1m (CME, ≈10 min delayed) |
| SMT / correlation pair | Bybit ETHUSDT | Yahoo ES=F |
| backtest data | Bybit (any length, cached) | **Dukascopy** Nasdaq-100 CFD 1m (years, free) + S&P 500 CFD · or Yahoo NQ=F (≤ 28 days) |
| broker / costs | Bybit non-VIP: maker 0.020 %, taker 0.055 %, real funding history, 0.01 % stop slippage | **IBKR** MNQ $0.62/contract/side (IBKR $0.25 + CME + NFA) · NQ $2.25 · US Tech 100 CFD 0.005 % (min $1) + spread — choose in `fee_profile` |
| default account | $100 · 2 % | $5,000 · 2 % (MNQ needs ≈ $40+ risk per contract at a 20-pt stop) |

The 10am limit entry is a **resting limit** → Bybit maker fee. TP = limit (maker), SL / break-even /
15:50 flatten = stop/market (taker + slippage). Position size = risk ÷ (stop distance × point value + round-trip costs),
so a full stop-out costs ≈ −1 R **after** fees.

*Delayed NQ feed:* the paper engine simulates fills from the candles (same as the backtester), so
delay does not change results — only the Discord alerts arrive ~10 min late. The header shows the delay.
*Dukascopy* is the cash-index CFD, not the future: prices differ by the basis, intraday structure is the same.
IBKR fees are configurable; please double-check them against your IBKR commission page (tiered vs fixed).

---
## 3. The engine (mechanised 10am model)
See `STRATEGY.md` for the derivation. In short, per NY trading day (Mon–Fri):

1. **Day gate @ 10:00** — skip FOMC days, no-news Mondays, an overnight session that already expanded
   (> 2.4 % NQ / 5 % BTC, or > 2.5× its 20-day median), poor NQ/ES (BTC/ETH) correlation 09:30–10:00.
2. **Key open** — open of the 10:00 NY 1m candle (= the 4H candle open on CME charts).
3. **Manipulation** — first move away from the 10am open ≥ `manip_min_pct`; the opposite side must have
   "no wick" (≤ `wick_tol_pct`), otherwise the day is chop.
4. **Distribution** — `confirm_closes` (2) consecutive 1m closes back through the open **and** a 5m close
   through it. A new manipulation extreme resets this.
5. **Entry** — limit at the 10am open (retest) · or a rejection-block trigger (`entry_mode`).
6. **Stop / target** — target = nearest *untaken* draw on liquidity (session/PD/PW highs-lows, 5m swings,
   data candle, midnight open); stop = target ÷ 5 but at least `min_sl_pct` (Powell's "how big can my stop be
   for a 1:5"); other modes: fib 0.5, beyond manipulation, fixed. Min RR 1:3.
7. **Management** — break-even at +2 R, flatten 15:50 NY, worst-case SL if SL and TP hit in one candle.
8. **Daily rules** — max 2 trades, stop after a win, after a loss re-arm (needs a sweep of the 10am range) at 50 % risk.

Everything is a switch in the backtester. The **Variant Lab** runs 18 variants on the same data and shows
first-half vs second-half R, so you can see whether a rule carries an edge or just fits noise.

No-lookahead: indicators/levels use only bars ≤ the current bar, liquidity must be untaken at decision time,
fills happen on later bars, swings must be confirmed. Live-incremental processing was verified to give
byte-identical trades to a single full backtest.

---
## 4. Troubleshooting
* *Dashboard error / no data*: `tmux attach -t TEN` — the first start downloads 30 days of 1m data per feed.
* *Dukascopy decode error*: the format is auto-detected; if it still fails run the NQ backtest with source
  "Yahoo NQ=F" and send me the error from the WEB log.
* *Yahoo 429*: raise `engine.yahoo_poll_seconds` in `config.json` (default 30 s).
* Live execution (IBKR TWS/Gateway, Bybit) is staged in the header (LIVE 🔒) but **not implemented**.
