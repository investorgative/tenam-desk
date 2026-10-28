#!/usr/bin/env python3
"""
TEN·AM Desk - live paper trader (forward test) for BTC (Bybit) and NQ (Yahoo NQ=F, IBKR cost model).
Runs in tmux session "TEN".  Usage:
    python tenam_trader.py                 # run both enabled markets
    python tenam_trader.py --reset         # wipe both paper accounts (stop the trader first)
    python tenam_trader.py --reset btc     # wipe one market

The paper engine is the backtest engine fed with newly closed 1m candles: every fill, stop and
target is simulated from candle data exactly like in the backtester (incl. after a restart:
missed candles are replayed - "catch-up").
"""
import argparse
import logging
import signal
import time

import tenam_core as core

EMOJI = {"GATE": "🚦", "MANIP": "🪤", "CONFIRM": "✅", "ORDER": "📌", "FILL": "🎯", "CANCEL": "✖️", "EXIT": "🏁",
         "BE": "🛡️", "REARM": "🔁", "DONE": "📒", "KEY": "🟡", "SCAN": "🔎", "RB": "🔎"}
NOTIFY = {"GATE": "notify_setups", "MANIP": "notify_setups", "CONFIRM": "notify_setups", "KEY": "notify_setups",
          "SCAN": "notify_setups", "ORDER": "notify_orders", "CANCEL": "notify_orders", "FILL": "notify_orders",
          "EXIT": "notify_trades", "BE": "notify_trades", "REARM": "notify_trades", "DONE": "notify_daily"}


class MarketRunner:
    def __init__(self, cfg, mkt, con):
        self.cfg, self.mkt, self.con = cfg, mkt, con
        self.M = core.market_cfg(cfg, mkt)
        self.log = logging.getLogger(f"trader.{mkt}")
        fm, fp = core.feeds_for(self.M, "paper")
        self.fm, self.fp = fm, fp
        self.main, self.pair = core.make_feed(fm), core.make_feed(fp)
        self.sm = core.CandleStore(con, self.main)
        self.sp = core.CandleStore(con, self.pair) if self.pair else None
        self.fs = core.FundingStore(con, self.main) if isinstance(self.main, core.BybitFeed) else None
        self.poll_s = cfg["engine"]["yahoo_poll_seconds"] if fm["type"] == "yahoo" else cfg["engine"]["poll_seconds"]
        self.next_poll = 0
        self.last_funding = 0
        self.errors = 0
        self.ticks = 0
        self.boot = time.time()
        self.price = None
        self.forming = None
        self.state = core.kv_get(con, f"paper_state_{mkt}")

    # ------------------------------------------------------------------ setup
    def bootstrap(self):
        E = self.cfg["engine"]
        now = core.now_ms()
        self.log.info(f"[{self.mkt}] bootstrapping {self.fm['symbol']} + {self.fp['symbol'] if self.fp else '-'} "
                      f"({E['history_days']}d of 1m history)")
        self.sm.ensure(now - E["history_days"] * core.DAY, now)
        if self.sp:
            self.sp.ensure(now - E["history_days"] * core.DAY, now)
        self._funding(force=True)
        if not self.state:
            eng = core.Engine(self.M)
            self.state = eng.st
            self.state["started_at"] = now
            self.state["last_ts"] = int(self.sm.last_ts() or now - core.MIN)
            self.save()
            self.log.info(f"[{self.mkt}] new paper account ${self.state['equity']:.2f} - forward test starts now")
        else:
            self.log.info(f"[{self.mkt}] resuming paper account, equity ${self.state['equity']:.2f}, "
                          f"last processed bar {time.strftime('%Y-%m-%d %H:%M', time.gmtime(self.state['last_ts'] / 1000))} UTC")
        if self.cfg["discord"].get("notify_start"):
            core.discord_send(self.cfg, self.mkt, f"**TEN·AM {self.M['label']} paper trader started** · "
                                                  f"equity ${self.state['equity']:.2f} · {self.M['fee_label']}")

    def save(self):
        core.kv_set(self.con, f"paper_state_{self.mkt}", self.state)

    def _funding(self, force=False):
        if not self.fs or not self.M["fees"].get("include_funding"):
            return
        if force or time.time() - self.last_funding > 1800:
            now = core.now_ms()
            self.fs.ensure(now - self.cfg["engine"]["history_days"] * core.DAY, now)
            self.last_funding = time.time()

    # ------------------------------------------------------------------ polling
    def poll(self):
        now = core.now_ms()
        rows, price, forming = self.main.latest()
        self.sm.insert(rows)
        if self.sp:
            self.sp.insert(self.pair.latest()[0])
        last = self.sm.last_ts() or 0
        if last and now - last > 15 * core.MIN and rows and rows[0][0] - last > core.MIN:
            self.sm.ensure(last, now)            # gap (network / restart)
            if self.sp:
                self.sp.ensure(last, now)
        self._funding()
        self.price, self.forming = price, forming
        self.process()
        self.ticks += 1
        self.write_live()

    def process(self):
        E, st = self.cfg["engine"], self.state
        now = core.now_ms()
        mx = self.sm.last_ts()
        if not mx or mx <= st["last_ts"]:
            return
        if now - st["last_ts"] > E["max_catchup_days"] * core.DAY:
            self.log.warning(f"[{self.mkt}] gap longer than {E['max_catchup_days']}d - skipping ahead")
            st["last_ts"] = now - E["max_catchup_days"] * core.DAY
        upto = mx
        if self.sp:
            pl = self.sp.last_ts() or 0
            if pl < mx and now - (mx + core.MIN) < E["pair_wait_s"] * 1000:
                upto = max(st["last_ts"], pl)          # wait for the pair candle (correlation / SMT)
        if upto <= st["last_ts"]:
            return
        D = core.load_data(self.con, self.M, st["last_ts"] - E["history_days"] * core.DAY, upto, src="paper",
                           download=False, feeds=(self.fm, self.fp))
        extra = {d: set(v) for d, v in (core.kv_get(self.con, "news_ff") or {}).items()}
        eng = core.Engine(self.M, state=st, calendar=core.load_calendar(extra))
        catchup = now - upto > 5 * core.MIN
        i0 = D.at(st["last_ts"] + 1)
        for i in range(i0, D.n):
            eng.step(D, i)
        for t in eng.trades_out:
            t["id"] = core.insert_trade(self.con, t)
        for d in eng.days_out:
            core.save_day(self.con, self.mkt, d)
        self.state = eng.st
        self.save()
        for e in eng.events:
            self.notify(e, catchup)

    def notify(self, e, catchup):
        msg = f"[{self.M['label']}] {e['type']} {e['msg']}"
        if e["type"] in ("EXIT", "FILL", "ORDER", "CONFIRM"):
            self.log.info(msg)
        else:
            self.log.info(msg)
        flag = NOTIFY.get(e["type"])
        if flag and self.cfg["discord"].get(flag):
            ny = time.strftime("%H:%M", time.gmtime((e["t"] + self._ny_off(e["t"])) / 1000))
            core.discord_send(self.cfg, self.mkt, f"{EMOJI.get(e['type'], '•')} **{self.M['label']}** `{e['date']} {ny} NY` "
                                                  f"{e['msg']}{'  _(catch-up)_' if catchup else ''}")

    @staticmethod
    def _ny_off(t):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return int(datetime.fromtimestamp(t / 1000, ZoneInfo("America/New_York")).utcoffset().total_seconds() * 1000)

    def write_live(self):
        now = core.now_ms()
        st = self.state
        day = st.get("day") or {}
        pos = day.get("pos")
        unreal = 0.0
        if pos and self.price:
            c = core.Costs(self.M)
            unreal = (pos["side"] * (self.price - pos["entry"]) * pos["qty"] * c.pv - pos["fee_entry"]
                      - pos["funding"] - c.fee(pos["qty"], self.price, "taker"))
        last = self.sm.last_ts() or 0
        core.kv_set(self.con, f"paper_live_{self.mkt}", {
            "price": self.price, "forming": list(self.forming) if self.forming else None, "last_tick": now,
            "last_bar": last, "delay_s": (now - last - core.MIN) / 1000 if last else None,
            "latency_ms": self.main.last_latency_ms, "uptime_s": int(time.time() - self.boot),
            "errors": self.errors, "ticks": self.ticks, "unrealized": unreal, "equity": st["equity"],
            "feed": self.fm, "pair_feed": self.fp})


class Trader:
    def __init__(self, cfg):
        self.cfg = cfg
        self.log = logging.getLogger("trader")
        self.con = core.db_connect()
        self.runners = [MarketRunner(cfg, m, self.con) for m in core.MARKETS if cfg["markets"][m].get("enabled")]
        self.running = True
        self.last_ff = 0

    def news(self):
        if time.time() - self.last_ff < 6 * 3600:
            return
        self.last_ff = time.time()
        try:
            ff = core.fetch_ff_week()
            old = core.kv_get(self.con, "news_ff") or {}
            old.update({d: sorted(v) for d, v in ff.items()})
            core.kv_set(self.con, "news_ff", old)
            self.log.info(f"ForexFactory calendar: {sum(len(v) for v in ff.values())} USD red-folder events this week")
        except Exception as e:
            self.log.warning(f"ForexFactory calendar unavailable ({e}) - using built-in calendar")

    def run(self):
        for r in self.runners:
            for attempt in range(5):
                try:
                    r.bootstrap()
                    break
                except Exception:
                    self.log.exception(f"[{r.mkt}] bootstrap failed (attempt {attempt + 1}/5)")
                    time.sleep(20)
        while self.running:
            self.news()
            for r in self.runners:
                if time.time() < r.next_poll:
                    continue
                r.next_poll = time.time() + r.poll_s
                try:
                    r.poll()
                except Exception:
                    r.errors += 1
                    self.log.exception(f"[{r.mkt}] poll failed")
                    r.next_poll = time.time() + max(15, r.poll_s)
            time.sleep(1)
        self.log.info("Paper trader stopped.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", nargs="?", const="all", help="wipe paper account(s): all | btc | nq")
    args = ap.parse_args()
    cfg = core.load_config()
    log = core.setup_logging("trader", cfg)
    if args.reset:
        con = core.db_connect()
        for m in (core.MARKETS if args.reset == "all" else [args.reset]):
            con.execute("DELETE FROM trades WHERE mkt=?", (m,))
            con.execute("DELETE FROM days WHERE mkt=?", (m,))
            con.execute("DELETE FROM kv WHERE k IN (?,?)", (f"paper_state_{m}", f"paper_live_{m}"))
            log.info(f"Paper account {m} reset.")
        con.commit()
        return
    t = Trader(cfg)

    def stop(*_):
        t.running = False
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    t.run()


if __name__ == "__main__":
    main()
