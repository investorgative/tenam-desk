# The 10am model — what Powell says, what the engine does, where he contradicts himself

Source: 17 YouTube transcripts (official "10am guide", key-opens & fib video, rejection-block videos,
entry-trigger video, "days not to trade", psychology / vlog videos up to Oct 2026).

## 1. Core idea (consistent across all videos)
* 10:00 NY is the open of a **4H candle** on CME charts. ~97 % of 4H candles have wicks on both sides →
  the first move after 10:00 is often the **manipulation** (wick), the opposite move the **distribution** (body).
* "Open-high-low-close" (bearish) / "open-low-high-close" (bullish): whatever price does first after the open,
  look for the opposite. Same logic at 09:30 (Judas swing), midnight open, weekly open.
* Bias = closest/strongest **draw on liquidity** (equal highs/lows, previous day/week H/L, data-candle H/L,
  unfilled gaps / new-week opening gap).
* Execution (official guide): mark the 10am open → manipulation into a PD array (gap, OB, high to sweep) with
  "no bottom wick" (2 pts on NQ = no wick) → **two 1m closes** back through the open + the **10:05 5m candle
  closing** through it (CISD) → **limit at the 10am open** → target the DOL.
* Stop: "subjective to volatility". Fib 0.5 of the leg protects the stop but may be too wide; alternative:
  size the stop from the target for **1:5** (target 100 pts → 20-pt stop). 20 pts = minimum in current volatility.
* Entry trigger alternative: lower-timeframe **rejection block** at the level (wick + candle closing back),
  entry at the body ("beginning") of the RB, stop beyond the wick. Fewer losses, but you get "left behind".
  "You can do both": limit first, RB trigger if the limit fails.
* No-trade conditions: previous session (Asia/London) expanded a lot (600–700 NQ pts) → NY accumulates;
  poor NQ/ES correlation; before red-folder news (CPI/PPI/NFP 08:30 — trade after the release); FOMC day
  (AM is "ass"); no-news Mondays; sometimes the day before NFP.
* Risk rules: two losses → off. Older rule: win → off; loss → one more trade at 50 % risk.

## 2. How the engine mechanises it (defaults, all switchable)
| Powell | Engine parameter |
|---|---|
| 10am open | open of the 10:00 NY 1m bar (DST aware) |
| manipulation "into a PDA" | excursion ≥ `manip_min_pct` (NQ 0.03 % ≈ 7.5 pts, BTC 0.06 %); sweep of a prior level is recorded and optional (`require_sweep`) |
| "no bottom wick" | opposite excursion ≤ `wick_tol_pct` (NQ 0.012 % ≈ 3 pts); otherwise day = NOT_CLEAN / TWO_SIDED |
| 2 × 1m close + 5m close | `confirm_closes` = 2, `confirm_5m` = true; new extreme resets |
| limit at 10am | `entry_mode` = limit; fills on touch, gap-through fills at the open |
| RB trigger | `entry_mode` = rb (1m or 5m), body / CE / 25 % entry, stop beyond wick |
| "do both" | `entry_mode` = limit_then_rb |
| DOL target | nearest untaken level: PD/PW/Asia/London/overnight/09:30-10:00 H-L, 5m swings, 08:30 data candle, midnight open |
| stop for 1:5 | `sl_mode` = rr_target (target ÷ 5, floor `min_sl_pct` NQ 0.08 % ≈ 20 pts) · alt. fib / manip / fixed |
| never < 1:3 | `rr_min` = 3 |
| break-even after reaction | `be_trigger_r` = 2 (configurable, 0 = off) |
| 2 losses → off / win → off / 50 % | `max_trades` 2, `stop_after_win`, `second_risk_mult` 0.5; re-arm needs a sweep of the 10am range |
| days not to trade | `filters`: fomc, no_news_monday, expansion (2.4 % NQ / 5 % BTC or 2.5× 20d median), correlation (ρ ≥ 0.6 NQ/ES, 0.5 BTC/ETH), news_day, day_before_nfp |
| Judas / midnight premium-discount / SMT | `bias`: judas, midnight, pair_confirm (off by default) |

What cannot be mechanised 1:1: his discretionary bias ("which leg is the real one"), "accumulation" recognition
beyond the expansion filter, intrabar SMT management, trailing to "newly formed lows". These are approximated or
left as switches — Powell himself says "people trade 10am too mechanically".

## 3. Changes over time and inconsistencies (why to be sceptical)
**Strategy changes**
1. **Stops**: older videos 10–15-pt static stops ("2–3 pt stops when I was broke", "10 was the absolute max");
   official guide: "10/15-pt stops are just giving money to the market", 20 pts minimum.
2. **RR**: 1:4–1:6 sweet spot, "lowest 1:4", "don't take < 1:3"; official guide: 1:5 on the main stack,
   1:10–1:20 on accounts without consistency rule (a 1:17 "yesterday"). RR is driven by prop-firm rules, not the setup.
3. **Daily rules**: win → stop / loss → one more at 50 % (2025) vs. "max two losses, 2–3 trades a day" vs.
   "two losses in a row → off" vs. "three trades per week".
4. **Timeframe**: rejection-block era = 1m triggers; Jan 2026 "stop obsessing over the 1m", 5m triggers;
   official guide back to 1m closes + 5m close; Oct 2026: 5m/15m rejection blocks recommended.
5. **The edge itself**: official guide — "weirdly consistent this year and last year", quants told him backtests are
   "weirdly high performing". Oct 2026 — the mechanical 10am entry "stopped being so consistent" after ~Sept 2026,
   and he reframes it as "entry mechanisms come and go — that's variance, not alpha decay". If an edge is
   "variance" whenever it stops working, the claim is unfalsifiable. Test it, don't believe it.

**Numbers that don't add up**
* "≈ 60 % win rate, average 1:7, profit factor 5.88": with 60 % and 1:7 the PF would be ≈ 10.5. Avg win $660 /
  avg loss $95 (≈ 7×) and PF 5.88 imply a win rate of ≈ 46 % — he admits BEs are counted as losses.
* Crypto memecoin: "2020, $2k → $100k" vs. "2021 I hit a crypto pump".
* Losses: "$13k over 9 months, made it back in one month" vs. "it took me a year to recover my losses".
* First 5-figure month: "12k month while studying for exams" vs. "May 2024" vs. "13k payout in June".
* Quit his job Oct 2023, but trading in a college booth in his final semester in Feb 2024.

**Incentives / survivorship**
* Results are prop-firm **payouts**, copy-traded across 10–20 accounts bought at 90 % off with his affiliate code.
  A payout stream from $840 of evals does **not** prove an edge: an eval is a cheap option — losses are capped at the
  fee, wins are paid out. Even a zero-edge, high-RR strategy produces regular payouts across 20 accounts. (VWL view:
  convex payoff + limited liability → positive EV for the trader even at zero market edge, financed by everyone who blows.)
* Sells a $315 course / mentorship, affiliate codes (Apex, Lucid), Discord. Example setups are openly cherry-picked.
* Strong statements without data: "NWOG gets filled ~100 % / 90 % same week", "95 % of the time we manipulate
  around midnight open", "97 % of 4H candles have both wicks" — the last one is trivially true and says nothing
  about direction.

**Bottom line**: the framework is coherent and mechanisable; the edge claims are not evidenced. The backtester and
the Variant Lab exist to test exactly that, with real costs.
