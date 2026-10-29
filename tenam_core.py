#!/usr/bin/env python3
"""
TEN·AM Desk - core library
==========================
Shared 1:1 by the paper trader (tenam_trader.py) and the backtester (tenam_web.py),
so a backtest trade and a paper trade are computed by exactly the same code.

  * config (two markets: BTC on Bybit, NQ on IBKR futures/CFD)
  * free market data: Bybit v5 (BTC/ETH 1m + funding), Yahoo Finance (NQ=F/ES=F 1m,
    live, ~30 days), Dukascopy (Nasdaq-100 / S&P-500 CFD 1m, years of history)
  * New-York session clock (DST aware), session levels, draw-on-liquidity targets
  * the 10:00 NY key-open state machine (Powell "10am" model, mechanised)
  * fee models: Bybit maker/taker + funding, IBKR futures per contract, IBKR CFD
  * statistics + Discord webhooks (per market + errors)
"""
from __future__ import annotations

import copy
import csv
import json
import logging
import lzma
import math
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

import numpy as np
import requests

VERSION = "1.0.1"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(DATA_DIR, "logs")
DB_PATH = os.path.join(DATA_DIR, "tenam.db")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
NEWS_CSV = os.path.join(BASE_DIR, "news.csv")
os.makedirs(LOG_DIR, exist_ok=True)

MIN, HOUR, DAY = 60_000, 3_600_000, 86_400_000
MARKETS = ("btc", "nq")
NaN = float("nan")

# ============================================================================ config
DEFAULT_CONFIG = {
    "display_timezone": "Europe/Berlin",
    "session_timezone": "America/New_York",
    "markets": {
        "btc": {
            "enabled": True, "label": "BTCUSDT", "title": "Bitcoin perpetual", "venue": "BYBIT",
            "feed": {"type": "bybit", "symbol": "BTCUSDT", "category": "linear"},
            "pair": {"type": "bybit", "symbol": "ETHUSDT", "category": "linear", "label": "ETHUSDT"},
            "backtest_feed": None, "backtest_pair": None,
            "day_start_hour": 0, "closed_weekends": False, "trade_weekdays_only": True,
            "account": {"start_balance": 100.0, "risk_pct": 2.0, "compound": True, "max_leverage": 50},
            "fee_profile": "bybit",
            "fee_profiles": {
                "bybit": {"label": "Bybit non-VIP linear",
                          "fees": {"model": "bybit", "maker": 0.0002, "taker": 0.00055,
                                   "include_funding": True, "slippage_pct": 0.01, "fee_in_risk": True},
                          "instrument": {"qty_step": 0.001, "min_qty": 0.001, "min_notional": 5.0,
                                         "tick_size": 0.1, "point_value": 1.0, "unit": "BTC"}}},
            "rules": {"manip_min_pct": 0.06, "wick_tol_pct": 0.03, "min_sl_pct": 0.15, "max_sl_pct": 1.2,
                      "rb_min_sl_pct": 0.08, "fixed_sl_pct": 0.25, "sl_buffer_pct": 0.01,
                      "expansion_pct": 5.0, "min_corr": 0.5, "zone_pct": 0.03, "max_target_pct": 3.0},
        },
        "nq": {
            "enabled": True, "label": "NQ1!", "title": "Nasdaq-100 E-mini", "venue": "IBKR",
            "feed": {"type": "yahoo", "symbol": "NQ=F"},
            "pair": {"type": "yahoo", "symbol": "ES=F", "label": "ES1!"},
            "backtest_feed": {"type": "dukascopy", "symbol": "USATECHIDXUSD", "price_range": [3000, 90000]},
            "backtest_pair": {"type": "dukascopy", "symbol": "USA500IDXUSD", "price_range": [500, 20000]},
            "day_start_hour": 18, "closed_weekends": True, "trade_weekdays_only": True,
            "account": {"start_balance": 5000.0, "risk_pct": 2.0, "compound": True, "max_leverage": 30},
            "fee_profile": "ibkr_mnq",
            "fee_profiles": {
                "ibkr_mnq": {"label": "IBKR Micro NQ (MNQ) futures",
                             "fees": {"model": "futures", "commission_per_contract_side": 0.62,
                                      "slippage_ticks": 1, "fee_in_risk": True},
                             "instrument": {"qty_step": 1, "min_qty": 1, "min_notional": 0, "tick_size": 0.25,
                                            "point_value": 2.0, "unit": "MNQ"}},
                "ibkr_nq": {"label": "IBKR E-mini NQ futures",
                            "fees": {"model": "futures", "commission_per_contract_side": 2.25,
                                     "slippage_ticks": 1, "fee_in_risk": True},
                            "instrument": {"qty_step": 1, "min_qty": 1, "min_notional": 0, "tick_size": 0.25,
                                           "point_value": 20.0, "unit": "NQ"}},
                "ibkr_cfd": {"label": "IBKR US Tech 100 index CFD",
                             "fees": {"model": "cfd", "spread_pts": 1.0, "commission_pct_side": 0.005,
                                      "commission_min": 1.0, "commission_per_lot_side": 0.0,
                                      "swap_long_pct_year": -6.5, "swap_short_pct_year": -1.5,
                                      "slippage_pts": 0.5, "fee_in_risk": True},
                             "instrument": {"qty_step": 0.1, "min_qty": 0.1, "min_notional": 0, "tick_size": 0.25,
                                            "point_value": 1.0, "unit": "CFD"}}},
            "rules": {"manip_min_pct": 0.03, "wick_tol_pct": 0.012, "min_sl_pct": 0.08, "max_sl_pct": 0.6,
                      "rb_min_sl_pct": 0.03, "fixed_sl_pct": 0.08, "sl_buffer_pct": 0.004,
                      "expansion_pct": 2.4, "min_corr": 0.6, "zone_pct": 0.015, "max_target_pct": 1.5},
        },
    },
    "strategy": {
        "key_open": "10:00", "ny_open": "09:30", "entry_window_end": "11:30", "flatten": "15:50",
        "asia": ["20:00", "00:00"], "london": ["02:00", "05:00"], "news_time": "08:30",
        "confirm_closes": 2, "confirm_5m": True, "require_clean": True,
        "entry_mode": "limit", "rb_tf": 1, "rb_entry": "body", "rb_wick_frac": 0.35, "rb_valid_bars": 10,
        "fill_through_ticks": 0,
        "sl_mode": "rr_target", "rr_target": 5.0, "rr_min": 3.0, "fib_level": 0.5,
        "tp_mode": "dol", "tp_rr": 5.0, "swing_left": 2, "swing_right": 2,
        "be_trigger_r": 2.0, "be_offset_r": 0.05,
        "max_trades": 2, "stop_after_win": True, "second_risk_mult": 0.5,
        "both_hit": "SL",
        "filters": {"fomc": True, "expansion": True, "expansion_rel": 2.5, "correlation": True,
                    "no_news_monday": True, "news_day": False, "day_before_nfp": False},
        "bias": {"judas": False, "midnight": False, "pair_confirm": False, "require_sweep": False},
    },
    "engine": {"poll_seconds": 10, "yahoo_poll_seconds": 30, "pair_wait_s": 120, "history_days": 30,
               "chart_days": 4, "max_catchup_days": 7},
    "backtest": {"default_days": 365, "max_days": 1095, "lab_days": 180},
    "web": {"host": "0.0.0.0", "port": 8757, "password": ""},
    "discord": {"errors": "", "btc": "", "nq": "", "notify_start": True, "notify_setups": True,
                "notify_orders": True, "notify_trades": True, "notify_daily": True},
}


def deep_merge(base: dict, over: dict) -> dict:
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config() -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            deep_merge(cfg, json.load(f))
    else:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(CONFIG_TEMPLATE, f, indent=2)
    return cfg


# config.json only holds YOUR overrides - everything else comes from DEFAULT_CONFIG (see config.example.json)
CONFIG_TEMPLATE = {
    "discord": {"errors": "", "btc": "", "nq": "", "notify_setups": True, "notify_orders": True,
                "notify_trades": True, "notify_daily": True},
    "web": {"port": 8757, "password": ""},
    "markets": {"btc": {"enabled": True, "account": {"start_balance": 100.0, "risk_pct": 2.0}},
                "nq": {"enabled": True, "fee_profile": "ibkr_mnq",
                       "account": {"start_balance": 5000.0, "risk_pct": 2.0}}},
}


def market_cfg(cfg: dict, mkt: str, overrides: dict | None = None) -> dict:
    """Resolved market config: fees/instrument from the fee profile, P = strategy+rules."""
    M = copy.deepcopy(cfg["markets"][mkt])
    M["key"] = mkt
    ov = copy.deepcopy(overrides or {})
    prof = ov.pop("fee_profile", None) or M.get("fee_profile")
    profs = M.get("fee_profiles", {})
    if prof not in profs:
        prof = next(iter(profs))
    M["fee_profile"] = prof
    M["fees"] = copy.deepcopy(profs[prof]["fees"])
    M["instrument"] = copy.deepcopy(profs[prof]["instrument"])
    M["fee_label"] = profs[prof].get("label", prof)
    P = copy.deepcopy(cfg["strategy"])
    deep_merge(P, M.get("rules", {}))
    deep_merge(P, M.get("strategy_override", {}))
    deep_merge(P, ov.pop("P", {}))
    M["P"] = P
    deep_merge(M["fees"], ov.pop("fees", {}))
    deep_merge(M["account"], ov.pop("account", {}))
    M["tz"] = cfg["session_timezone"]
    M["display_tz"] = cfg["display_timezone"]
    return M


def public_config(cfg: dict) -> dict:
    c = copy.deepcopy(cfg)
    for k in ("errors", "btc", "nq"):
        c["discord"][k] = "set" if cfg["discord"].get(k) else ""
    c["web"]["password"] = "set" if cfg["web"].get("password") else ""
    return c


def hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def now_ms() -> int:
    return int(time.time() * 1000)


def dn2str(dn: int) -> str:
    return (date(1970, 1, 1) + timedelta(days=int(dn))).isoformat()


def str2dn(s: str) -> int:
    return (date.fromisoformat(s) - date(1970, 1, 1)).days


# ============================================================================ logging + discord
class DiscordHandler(logging.Handler):
    """Posts ERROR/CRITICAL records to the error webhook (deduplicated for 5 min)."""

    def __init__(self, url: str, source: str):
        super().__init__(level=logging.ERROR)
        self.url, self.source = url, source
        self._recent: dict = {}
        self._lock = threading.Lock()

    def emit(self, record):
        if not self.url:
            return
        try:
            msg = record.getMessage()
            key = (record.name, msg[:200])
            now = time.time()
            with self._lock:
                if now - self._recent.get(key, 0) < 300:
                    return
                self._recent[key] = now
            tb = "".join(traceback.format_exception(*record.exc_info)) if record.exc_info else ""
            body = msg + ("\n\n" + tb if tb else "")
            if len(body) > 1700:
                body = body[:500] + "\n...\n" + body[-1150:]
            text = (f"**TEN·AM {self.source} | {record.levelname}**  "
                    f"`{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`\n```{body}```")
            threading.Thread(target=_discord_post, args=(self.url, text), daemon=True).start()
        except Exception:
            pass


def _discord_post(url, text):
    try:
        requests.post(url, json={"content": text[:1990], "username": "TEN·AM Desk"}, timeout=10)
    except Exception:
        pass


def discord_send(cfg, channel, text):
    url = cfg["discord"].get(channel)
    if url:
        threading.Thread(target=_discord_post, args=(url, text), daemon=True).start()


def setup_logging(source: str, cfg: dict) -> logging.Logger:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(os.path.join(LOG_DIR, f"{source}.log"), maxBytes=1_000_000,
                             backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    root.addHandler(DiscordHandler(cfg["discord"].get("errors", ""), source))
    for noisy in ("urllib3", "werkzeug", "waitress"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    lg = logging.getLogger(source)
    sys.excepthook = lambda t, v, tb: lg.critical("Uncaught exception", exc_info=(t, v, tb))
    threading.excepthook = lambda a: lg.critical(
        f"Uncaught exception in thread {a.thread.name if a.thread else '?'}",
        exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    return lg


# ============================================================================ sqlite
SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, mkt TEXT, day TEXT, side INTEGER, entry_ts INTEGER, exit_ts INTEGER,
  entry REAL, exit REAL, sl REAL, sl0 REAL, tp REAL, qty REAL, notional REAL, leverage REAL, risk_amt REAL,
  fee_entry REAL, fee_exit REAL, funding REAL, pnl_gross REAL, pnl_net REAL, r REAL, rr REAL,
  mae_r REAL, mfe_r REAL, reason TEXT, equity_after REAL, entry_type TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS days (mkt TEXT, day TEXT, v TEXT, PRIMARY KEY(mkt, day));
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS duka_days (key TEXT, day TEXT, n INTEGER, PRIMARY KEY(key, day));
"""
TRADE_COLS = ["mkt", "day", "side", "entry_ts", "exit_ts", "entry", "exit", "sl", "sl0", "tp", "qty", "notional",
              "leverage", "risk_amt", "fee_entry", "fee_exit", "funding", "pnl_gross", "pnl_net", "r", "rr",
              "mae_r", "mfe_r", "reason", "equity_after", "entry_type", "note"]


def db_connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    return con


def kv_get(con, k, default=None):
    row = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return json.loads(row[0]) if row else default


def kv_set(con, k, v):
    con.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, json.dumps(clean_json(v))))
    con.commit()


def insert_trade(con, t: dict) -> int:
    cur = con.execute(f"INSERT INTO trades({','.join(TRADE_COLS)}) VALUES({','.join('?' * len(TRADE_COLS))})",
                      [t.get(c) for c in TRADE_COLS])
    con.commit()
    return cur.lastrowid


def load_trades(con, mkt) -> list:
    cur = con.execute(f"SELECT id,{','.join(TRADE_COLS)} FROM trades WHERE mkt=? ORDER BY exit_ts, id", (mkt,))
    return [dict(zip(["id"] + TRADE_COLS, r)) for r in cur.fetchall()]


def save_day(con, mkt, rec):
    con.execute("INSERT OR REPLACE INTO days(mkt,day,v) VALUES(?,?,?)", (mkt, rec["date"], json.dumps(clean_json(rec))))
    con.commit()


def load_days(con, mkt, limit=4000) -> list:
    rows = con.execute("SELECT v FROM days WHERE mkt=? ORDER BY day DESC LIMIT ?", (mkt, limit)).fetchall()
    return [json.loads(r[0]) for r in reversed(rows)]


def clean_json(o):
    if isinstance(o, dict):
        return {str(k): clean_json(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [clean_json(v) for v in o]
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return None if (math.isnan(f) or math.isinf(f)) else f
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    return o


# ============================================================================ market data feeds
UA = "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"


def feed_key(f: dict) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", f"{f['type']}_{f['symbol']}")


class BybitFeed:
    """Bybit v5 public REST, 1m klines + funding (no API keys)."""
    base = "https://api.bybit.com"

    def __init__(self, f):
        self.f, self.sym, self.cat = f, f["symbol"], f.get("category", "linear")
        self.key = feed_key(f)
        self.s = requests.Session()
        self.s.headers["User-Agent"] = f"TENAM/{VERSION}"
        self.last_latency_ms = None

    def _get(self, path, params):
        err = None
        for attempt in range(4):
            try:
                t0 = time.time()
                r = self.s.get(self.base + path, params=params, timeout=15)
                self.last_latency_ms = int((time.time() - t0) * 1000)
                r.raise_for_status()
                j = r.json()
                if j.get("retCode") != 0:
                    raise RuntimeError(f"Bybit {path} retCode={j.get('retCode')} {j.get('retMsg')}")
                return j["result"]
            except Exception as e:
                err = e
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"Bybit request failed {path} {params}: {err}")

    def klines(self, start=None, end=None, limit=1000):
        p = {"category": self.cat, "symbol": self.sym, "interval": "1", "limit": limit}
        if start is not None:
            p["start"] = int(start)
        if end is not None:
            p["end"] = int(end)
        rows = self._get("/v5/market/kline", p).get("list", [])
        out = [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in rows]
        out.sort()
        return out

    def range(self, start, end, progress=None):
        res, cur_end, total = {}, int(end), max(1, int(end) - int(start))
        while True:
            rows = self.klines(start=start, end=cur_end)
            if not rows:
                break
            for r in rows:
                res[r[0]] = r
            first = rows[0][0]
            if progress:
                progress(min(1.0, (end - first) / total))
            if first <= start or len(rows) < 1000:
                break
            cur_end = first - 1
            time.sleep(0.04)
        return [res[k] for k in sorted(res) if start <= k <= end]

    def latest(self):
        rows = self.klines(limit=6)
        price = rows[-1][4] if rows else None
        return rows, price, (rows[-1] if rows else None)

    def funding_range(self, start, end):
        res, cur_end = {}, int(end)
        while True:
            rows = self._get("/v5/market/funding/history",
                             {"category": self.cat, "symbol": self.sym, "startTime": int(start),
                              "endTime": cur_end, "limit": 200}).get("list", [])
            if not rows:
                break
            for r in rows:
                res[int(r["fundingRateTimestamp"])] = float(r["fundingRate"])
            oldest = min(int(r["fundingRateTimestamp"]) for r in rows)
            if oldest <= start or len(rows) < 200:
                break
            cur_end = oldest - 1
            time.sleep(0.04)
        return res


class RateLimited(RuntimeError):
    """Data source refuses requests for a while (HTTP 429) - not a bug, just wait."""


try:  # Yahoo blocks python-requests' TLS fingerprint -> impersonate a real Chrome if possible
    from curl_cffi import requests as _creq
except Exception:  # pragma: no cover
    _creq = None


class YahooFeed:
    """Yahoo Finance v8 chart API, 1m bars (CME futures are ~10 min delayed, ~30 days of 1m history).

    * uses curl_cffi (Chrome TLS fingerprint) + Yahoo cookie/crumb like yfinance does
    * on HTTP 429 a cooldown is shared by ALL Yahoo feeds (NQ=F and ES=F) and RateLimited is raised
      immediately instead of hammering the API (the caller just retries later)
    """
    hosts = ("https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com")
    _cool_until = 0.0
    _cool_n = 0
    _gate = threading.Lock()
    _last_req = 0.0

    def __init__(self, f):
        self.f, self.sym, self.key = f, f["symbol"], feed_key(f)
        self.last_latency_ms = None
        self.last_meta = {}
        self._new_session()

    # ------------------------------------------------------------------ session / cookies
    def _new_session(self):
        if _creq is not None:
            self.s = _creq.Session(impersonate="chrome")
        else:
            self.s = requests.Session()
            self.s.headers.update({"User-Agent": UA, "Accept": "application/json,text/plain,*/*",
                                   "Accept-Language": "en-US,en;q=0.9"})
        self.crumb, self.primed = None, False

    def _prime(self):
        self.primed = True
        try:
            self.s.get("https://fc.yahoo.com", timeout=10, allow_redirects=True)   # sets the A3 cookie (404 is fine)
        except Exception:
            pass
        try:
            r = self.s.get("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=10)
            txt = (r.text or "").strip()
            if r.status_code == 200 and txt and "<" not in txt and len(txt) < 40:
                self.crumb = txt
        except Exception:
            pass

    @classmethod
    def cooldown_left(cls):
        return max(0.0, cls._cool_until - time.time())

    @classmethod
    def _rate_limited(cls):
        cls._cool_n = min(cls._cool_n + 1, 6)
        cls._cool_until = time.time() + min(900, 30 * 2 ** (cls._cool_n - 1))   # 30s,60s,...,15min

    def _throttle(self):
        with YahooFeed._gate:                      # max ~1 request / 1.2 s across all Yahoo feeds
            wait = 1.2 - (time.time() - YahooFeed._last_req)
            if wait > 0:
                time.sleep(wait)
            YahooFeed._last_req = time.time()

    # ------------------------------------------------------------------ requests
    def chart(self, p1_ms, p2_ms):
        if self.cooldown_left() > 0:
            raise RateLimited(f"Yahoo cooldown active ({self.cooldown_left():.0f}s left)")
        if not self.primed:
            self._prime()
        err = None
        for attempt in range(3):
            host = self.hosts[attempt % 2]
            params = {"period1": int(p1_ms // 1000), "period2": int(p2_ms // 1000),
                      "interval": "1m", "includePrePost": "true"}
            if self.crumb:
                params["crumb"] = self.crumb
            try:
                self._throttle()
                t0 = time.time()
                r = self.s.get(f"{host}/v8/finance/chart/{self.sym}", params=params, timeout=20)
                self.last_latency_ms = int((time.time() - t0) * 1000)
                if r.status_code == 429:
                    self._rate_limited()
                    self._new_session()            # fresh cookies for the next try
                    raise RateLimited(f"Yahoo rate limit (429) on {self.sym} - pausing Yahoo for "
                                      f"{self.cooldown_left():.0f}s")
                if r.status_code in (401, 403):
                    self._new_session()
                    self._prime()
                    raise RuntimeError(f"Yahoo {r.status_code} (cookie/crumb refreshed)")
                r.raise_for_status()
                YahooFeed._cool_n = 0
                res = r.json()["chart"]["result"]
                if not res:
                    return []
                res = res[0]
                self.last_meta = res.get("meta", {})
                ts = res.get("timestamp") or []
                q = res["indicators"]["quote"][0]
                out = []
                for k, t in enumerate(ts):
                    o, h, l, c = q["open"][k], q["high"][k], q["low"][k], q["close"][k]
                    if None in (o, h, l, c):
                        continue
                    out.append((int(t) * 1000 // MIN * MIN, float(o), float(h), float(l), float(c),
                                float((q.get("volume") or [0] * len(ts))[k] or 0)))
                return out
            except RateLimited:
                raise
            except Exception as e:
                err = e
                time.sleep(2.0 * (attempt + 1))
        raise RuntimeError(f"Yahoo request failed {self.sym}: {err}")

    def range(self, start, end, progress=None, sink=None):
        """Downloads in 6-day chunks; with `sink` every chunk is stored immediately (resumable)."""
        start = max(int(start), now_ms() - 29 * DAY)
        res, cur, total = {}, start, max(1, end - start)
        while cur < end:
            nxt = min(end, cur + 6 * DAY)
            rows = [r for r in self.chart(cur, nxt) if cur <= r[0] <= nxt]
            if sink:
                sink(rows)
            else:
                for r in rows:
                    res[r[0]] = r
            cur = nxt
            if progress:
                progress(min(1.0, (cur - start) / total))
        return [res[k] for k in sorted(res) if start <= k <= end]

    def latest(self):
        rows = self.chart(now_ms() - 3 * HOUR, now_ms() + MIN)
        price = self.last_meta.get("regularMarketPrice") or (rows[-1][4] if rows else None)
        return rows, price, (rows[-1] if rows else None)


class DukascopyFeed:
    """Dukascopy public datafeed, BID 1m candles per UTC day (LZMA .bi5). Free, years of history."""
    base = "https://datafeed.dukascopy.com/datafeed"

    def __init__(self, f):
        self.f, self.sym, self.key = f, f["symbol"], feed_key(f)
        self.rng = f.get("price_range", [1, 1e9])
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.last_latency_ms = None
        self.div = None

    def day(self, d: date):
        url = f"{self.base}/{self.sym}/{d.year:04d}/{d.month - 1:02d}/{d.day:02d}/BID_candles_min_1.bi5"
        err = None
        for attempt in range(4):
            try:
                t0 = time.time()
                r = self.s.get(url, timeout=30)
                self.last_latency_ms = int((time.time() - t0) * 1000)
                if r.status_code == 404:
                    return []
                r.raise_for_status()
                base = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)
                return self.decode(r.content, base)
            except Exception as e:
                err = e
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"Dukascopy request failed {url}: {err}")

    def decode(self, raw, base_ms):
        if not raw:
            return []
        data = lzma.decompress(raw)
        n = len(data) // 24
        if n == 0:
            return []
        buf = data[:n * 24]
        ints = np.frombuffer(buf, dtype=">i4").reshape(n, 6).astype(np.float64)
        flts = np.frombuffer(buf, dtype=">f4").reshape(n, 6).astype(np.float64)
        t = np.frombuffer(buf, dtype=">u4").reshape(n, 6)[:, 0].astype(np.int64)
        tmul = 1 if t.max() > 2 * 86400 else 1000          # seconds (normal) or ms
        vol = flts[:, 5]
        orders = ((0, 1, 2, 3), (0, 3, 2, 1), (0, 1, 3, 2), (0, 2, 3, 1))  # (O,C,L,H) is the documented one
        for P in (ints[:, 1:5], flts[:, 1:5]):
            for io, ic, il, ih in orders:
                o, c, l, h = P[:, io], P[:, ic], P[:, il], P[:, ih]
                good = (h >= np.maximum(o, c) - 1e-9) & (l <= np.minimum(o, c) + 1e-9) & (l > 0)
                if good.mean() < 0.97:
                    continue
                med = float(np.median(c[good]))
                divs = [self.div] if self.div else [10 ** k for k in range(0, 7)]
                for dv in divs:
                    if self.rng[0] <= med / dv <= self.rng[1]:
                        self.div = dv
                        keep = good & ~((vol <= 0) & (o == h) & (h == l) & (l == c))
                        out = []
                        for k in np.flatnonzero(keep):
                            out.append((int(base_ms + t[k] * tmul), o[k] / dv, h[k] / dv, l[k] / dv,
                                        c[k] / dv, float(vol[k])))
                        return out
        raise ValueError(f"Dukascopy {self.sym}: could not decode candle file (unexpected format/price range)")


def make_feed(f):
    if not f:
        return None
    t = f["type"]
    if t == "bybit":
        return BybitFeed(f)
    if t == "yahoo":
        return YahooFeed(f)
    if t == "dukascopy":
        return DukascopyFeed(f)
    raise ValueError(f"unknown feed type {t}")


class CandleStore:
    """sqlite cache of CLOSED 1m candles per feed."""

    def __init__(self, con, feed):
        self.con, self.feed = con, feed
        self.t = f"candles_{feed.key}"
        con.execute(f"CREATE TABLE IF NOT EXISTS {self.t} (ts INTEGER PRIMARY KEY, o REAL, h REAL, l REAL, c REAL, v REAL)")
        con.commit()

    def insert(self, rows):
        lim = now_ms()
        rows = [r for r in rows if r[0] + MIN <= lim]
        if rows:
            self.con.executemany(f"INSERT OR REPLACE INTO {self.t} VALUES(?,?,?,?,?,?)", rows)
            self.con.commit()
        return len(rows)

    def bounds(self):
        return self.con.execute(f"SELECT MIN(ts), MAX(ts) FROM {self.t}").fetchone()

    def load(self, start, end):
        rows = self.con.execute(f"SELECT ts,o,h,l,c FROM {self.t} WHERE ts>=? AND ts<=? ORDER BY ts",
                                (int(start), int(end))).fetchall()
        if not rows:
            e = np.array([], float)
            return np.array([], np.int64), e, e, e, e
        a = np.array(rows, float)
        return a[:, 0].astype(np.int64), a[:, 1], a[:, 2], a[:, 3], a[:, 4]

    def last_ts(self):
        return self.bounds()[1]

    def ensure(self, start, end, progress=None):
        end = min(int(end), now_ms())
        start = int(start)
        fd = self.feed
        if isinstance(fd, DukascopyFeed):
            return self._ensure_duka(start, end, progress)
        if isinstance(fd, YahooFeed):
            start = max(start, now_ms() - 29 * DAY)
        mn, mx = self.bounds()
        segs = []
        if mn is None:
            segs.append((start, end))
        else:
            if start < mn - MIN:
                segs.append((start, mn - 1))
            if end > mx + MIN:
                segs.append((mx, end))
            if isinstance(fd, BybitFeed):   # crypto trades 24/7 -> fill holes
                ts = [r[0] for r in self.con.execute(f"SELECT ts FROM {self.t} WHERE ts>=? AND ts<=? ORDER BY ts",
                                                     (start, end))]
                for a, b in zip(ts, ts[1:]):
                    if b - a > 10 * MIN and len(segs) < 60:
                        segs.append((a, b))
        for k, (s, e) in enumerate(segs):
            def pg(x, k=k):
                if progress:
                    progress((k + x) / len(segs))
            if isinstance(fd, YahooFeed):
                fd.range(s, e, progress=pg, sink=self.insert)
            else:
                self.insert(fd.range(s, e, progress=pg))

    def _ensure_duka(self, start, end, progress):
        d0 = datetime.fromtimestamp(start / 1000, timezone.utc).date()
        d1 = datetime.fromtimestamp(end / 1000, timezone.utc).date()
        today = datetime.now(timezone.utc).date()
        have = {r[0]: r[1] for r in self.con.execute("SELECT day, n FROM duka_days WHERE key=?", (self.feed.key,))}
        days, d = [], d0
        while d <= d1:
            k = d.isoformat()
            if d.weekday() != 5 and d < today and (k not in have or (have[k] == 0 and (today - d).days <= 3)):
                days.append(d)
            d += timedelta(days=1)
        for k, d in enumerate(days):
            rows = self.feed.day(d)
            self.insert(rows)
            self.con.execute("INSERT OR REPLACE INTO duka_days VALUES(?,?,?)", (self.feed.key, d.isoformat(), len(rows)))
            self.con.commit()
            if progress:
                progress((k + 1) / len(days))
            time.sleep(0.05)


class FundingStore:
    def __init__(self, con, feed):
        self.con, self.feed = con, feed
        self.t = f"funding_{feed.key}"
        con.execute(f"CREATE TABLE IF NOT EXISTS {self.t} (ts INTEGER PRIMARY KEY, rate REAL)")
        con.commit()

    def ensure(self, start, end):
        mn, mx = self.con.execute(f"SELECT MIN(ts), MAX(ts) FROM {self.t}").fetchone()
        segs = []
        if mn is None:
            segs.append((start, end))
        else:
            if start < mn - 8 * HOUR:
                segs.append((start, mn - 1))
            if end > mx + 8 * HOUR:
                segs.append((mx + 1, end))
        for s, e in segs:
            d = self.feed.funding_range(s, e)
            if d:
                self.con.executemany(f"INSERT OR REPLACE INTO {self.t} VALUES(?,?)", list(d.items()))
                self.con.commit()

    def load(self, start, end):
        rows = self.con.execute(f"SELECT ts, rate FROM {self.t} WHERE ts>=? AND ts<=?", (int(start), int(end))).fetchall()
        return {int(t): float(r) for t, r in rows}


def feeds_for(M, src):
    if src == "backtest" and M.get("backtest_feed"):
        return M["backtest_feed"], M.get("backtest_pair") or M.get("pair")
    return M["feed"], M.get("pair")


def load_data(con, M, start, end, src="paper", progress=None, download=True, feeds=None):
    """Download (cache) + load main/pair 1m candles and funding into a Data object."""
    fm, fp = feeds or feeds_for(M, src)
    main, pair = make_feed(fm), make_feed(fp)
    sm = CandleStore(con, main)
    sp = CandleStore(con, pair) if pair else None
    if download:
        sm.ensure(start, end, progress=(lambda x: progress("downloading " + fm["symbol"], x * 0.55)) if progress else None)
        if sp:
            sp.ensure(start, end, progress=(lambda x: progress("downloading " + fp["symbol"], 0.55 + x * 0.3)) if progress else None)
    funding = {}
    if M["fees"].get("model") == "bybit" and M["fees"].get("include_funding") and isinstance(main, BybitFeed):
        fs = FundingStore(con, main)
        if download:
            if progress:
                progress("downloading funding history", 0.86)
            fs.ensure(start, end)
        funding = fs.load(start, end)
    ts, o, h, l, c = sm.load(start, end)
    P = None
    if sp:
        pts, po, ph, pl, pc = sp.load(start, end)
        if pts.size:
            P = {"ts": pts, "o": po, "c": pc}
    return Data(ts, o, h, l, c, M["tz"], pair=P, funding=funding,
                feed={"main": fm, "pair": fp})


# ============================================================================ session clock
def local_info(ts, tz):
    ts = np.asarray(ts, np.int64)
    if ts.size == 0:
        z = np.array([], np.int64)
        return z, z, z, z
    hrs = ts // HOUR
    uh, inv = np.unique(hrs, return_inverse=True)
    zi = ZoneInfo(tz)
    offs = np.array([int(datetime.fromtimestamp(int(h) * 3600, tz=zi).utcoffset().total_seconds() * 1000)
                     for h in uh], np.int64)
    loc = ts + offs[inv]
    loc = np.maximum.accumulate(loc)
    dnum = loc // DAY
    mod = (loc % DAY) // MIN
    wd = (dnum + 3) % 7  # Monday = 0
    return loc, dnum, mod, wd


class Data:
    def __init__(self, ts, o, h, l, c, tz, pair=None, funding=None, feed=None):
        self.ts = np.asarray(ts, np.int64)
        self.o, self.h, self.l, self.c = (np.asarray(x, float) for x in (o, h, l, c))
        self.n = int(self.ts.size)
        self.loc, self.dnum, self.mod, self.wd = local_info(self.ts, tz)
        self.T, self.O, self.H, self.L, self.C = (x.tolist() for x in (self.ts, self.o, self.h, self.l, self.c))
        self.M, self.DN, self.WD = self.mod.tolist(), self.dnum.tolist(), self.wd.tolist()
        self.pair = pair
        self.funding = funding or {}
        self.feed = feed or {}

    def iloc(self, local_ms):
        return int(np.searchsorted(self.loc, local_ms, "left"))

    def win(self, a, b, upto=None):
        i0, i1 = self.iloc(a), self.iloc(b)
        if upto is not None:
            i1 = min(i1, upto + 1)
        return i0, max(i0, i1)

    def at(self, ts):
        return int(np.searchsorted(self.ts, ts, "left"))


# ============================================================================ economic calendar
NEWS_BUILTIN = {
    # BLS release schedules (bls.gov) - CPI / PPI / Employment Situation (NFP), 08:30 NY
    "CPI": "2024-01-11 2024-02-13 2024-03-12 2024-04-10 2024-05-15 2024-06-12 2024-07-11 2024-08-14 2024-09-11 "
           "2024-10-10 2024-11-13 2024-12-11 2025-01-15 2025-02-12 2025-03-12 2025-04-10 2025-05-13 2025-06-11 "
           "2025-07-15 2025-08-12 2025-09-11 2025-10-24 2025-12-18 2026-01-13 2026-02-13 2026-03-11 2026-04-10 "
           "2026-05-12 2026-06-10 2026-07-14 2026-08-12 2026-09-11 2026-10-14 2026-11-10 2026-12-10",
    "PPI": "2024-01-12 2024-02-16 2024-03-14 2024-04-11 2024-05-14 2024-06-13 2024-07-12 2024-08-13 2024-09-12 "
           "2024-10-11 2024-11-14 2024-12-12 2025-01-14 2025-02-13 2025-03-13 2025-04-11 2025-05-15 2025-06-12 "
           "2025-07-16 2025-08-14 2025-09-10 2025-11-25 2026-01-14 2026-01-30 2026-02-27 2026-03-18 2026-04-14 "
           "2026-05-13 2026-06-11 2026-07-15 2026-08-13 2026-09-10 2026-10-15 2026-11-13 2026-12-15",
    "NFP": "2024-01-05 2024-02-02 2024-03-08 2024-04-05 2024-05-03 2024-06-07 2024-07-05 2024-08-02 2024-09-06 "
           "2024-10-04 2024-11-01 2024-12-06 2025-01-10 2025-02-07 2025-03-07 2025-04-04 2025-05-02 2025-06-06 "
           "2025-07-03 2025-08-01 2025-09-05 2025-11-20 2025-12-16 2026-01-09 2026-02-11 2026-03-06 2026-04-03 "
           "2026-05-08 2026-06-05 2026-07-02 2026-08-07 2026-09-04 2026-10-02 2026-11-06 2026-12-04 2027-01-08 "
           "2027-02-05 2027-03-05 2027-04-02 2027-05-07 2027-06-04 2027-07-02 2027-08-06 2027-09-03 2027-10-08",
    # FOMC statement days (federalreserve.gov), 14:00 NY
    "FOMC": "2024-01-31 2024-03-20 2024-05-01 2024-06-12 2024-07-31 2024-09-18 2024-11-07 2024-12-18 2025-01-29 "
            "2025-03-19 2025-05-07 2025-06-18 2025-07-30 2025-09-17 2025-10-29 2025-12-10 2026-01-28 2026-03-18 "
            "2026-04-29 2026-06-17 2026-07-29 2026-09-16 2026-10-28 2026-12-09 2027-01-27 2027-03-17 2027-04-28 "
            "2027-06-09 2027-07-28 2027-09-15 2027-10-27 2027-12-08",
}


def load_calendar(extra=None) -> dict:
    cal = {}
    for ev, s in NEWS_BUILTIN.items():
        for d in s.split():
            cal.setdefault(d, set()).add(ev)
    if os.path.exists(NEWS_CSV):
        with open(NEWS_CSV, encoding="utf-8") as f:
            for row in csv.reader(f):
                if len(row) >= 2 and re.match(r"\d{4}-\d{2}-\d{2}", row[0].strip()):
                    d, ev = row[0].strip(), row[1].strip().upper()
                    if ev.startswith("-"):
                        cal.get(d, set()).discard(ev[1:])
                    else:
                        cal.setdefault(d, set()).add(ev)
    for d, evs in (extra or {}).items():
        cal.setdefault(d, set()).update(evs)
    return cal


def fetch_ff_week():
    """ForexFactory weekly calendar (free JSON mirror). Returns {date: {events}} for USD high impact."""
    r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", headers={"User-Agent": UA}, timeout=15)
    r.raise_for_status()
    out = {}
    for e in r.json():
        if e.get("country") != "USD" or e.get("impact") != "High":
            continue
        t = e.get("title", "").upper()
        ev = "CPI" if "CPI" in t else "PPI" if "PPI" in t else "NFP" if "NON-FARM" in t else \
            "FOMC" if ("FOMC" in t or "FEDERAL FUNDS" in t) else None
        if ev:
            d = datetime.fromisoformat(e["date"]).astimezone(ZoneInfo("America/New_York")).date().isoformat()
            out.setdefault(d, set()).add(ev)
    return out


# ============================================================================ costs + sizing
def round_tick(p, tick, mode="nearest"):
    k = p / tick
    k = math.floor(k + 1e-9) if mode == "down" else math.ceil(k - 1e-9) if mode == "up" else round(k)
    return round(k * tick, 8)


class Costs:
    def __init__(self, M):
        self.f, self.ins = M["fees"], M["instrument"]
        self.model, self.pv, self.tick = self.f["model"], float(self.ins["point_value"]), float(self.ins["tick_size"])

    def fee(self, qty, price, kind):
        f, notional = self.f, qty * price * self.pv
        if self.model == "bybit":
            return notional * (f["maker"] if kind == "maker" else f["taker"])
        if self.model == "cfd":
            com = max(notional * f.get("commission_pct_side", 0) / 100, f.get("commission_min", 0) if qty else 0)
            return qty * self.pv * f.get("spread_pts", 0) / 2 + com + qty * f.get("commission_per_lot_side", 0)
        if self.model == "futures":
            return qty * f["commission_per_contract_side"]
        return 0.0

    def slip(self, price):
        """adverse price slippage for stop / market fills (points)."""
        f = self.f
        if self.model == "bybit":
            return price * f.get("slippage_pct", 0) / 100
        if self.model == "cfd":
            return f.get("slippage_pts", 0)
        return f.get("slippage_ticks", 0) * self.tick

    def per_unit(self, entry, sl):
        return (self.fee(1, entry, "maker") + self.fee(1, sl, "taker")) + self.slip(sl) * self.pv

    def describe(self):
        f = self.f
        if self.model == "bybit":
            return f"maker {f['maker'] * 100:.3f}% · taker {f['taker'] * 100:.3f}%"
        if self.model == "cfd":
            return f"spread {f.get('spread_pts', 0)} pt · comm {f.get('commission_pct_side', 0)}%/side (min ${f.get('commission_min', 0)})"
        return f"${f['commission_per_contract_side']:.2f}/contract/side · {f.get('slippage_ticks', 0)} tick stop slip"


# ============================================================================ 10:00 key-open engine
DIRN = {1: "LONG", -1: "SHORT"}


class Engine:
    """
    Bar-by-bar (closed 1m candles) state machine of Powell's 10am model.

    per NY trading day:
      GATE      at 10:00: FOMC / no-news Monday / overnight expansion / pair correlation
      AWAIT     first move away from the 10:00 open >= manip_min (clean: opposite wick <= tol)
      MANIP     track the manipulation extreme; wait for N consecutive 1m closes back through
                the 10:00 open (+ a 5m close through it). New extreme resets the confirmation.
      ORDER     limit at the 10:00 open (retest) - or a rejection-block trigger (rb mode)
      POSITION  SL / TP (draw on liquidity) / break-even / flatten at 15:50
      after a loss: re-arm (needs a sweep of the 10am range), 2nd trade at 50% risk
    Every decision only uses bars <= the current bar; fills happen on later bars.
    """

    def __init__(self, M, state=None, calendar=None):
        self.M, self.P = M, M["P"]
        P = self.P
        self.acc, self.fees, self.ins = M["account"], M["fees"], M["instrument"]
        self.costs = Costs(M)
        self.k_min, self.ny_min = hm(P["key_open"]), hm(P["ny_open"])
        self.w_end, self.flat, self.news_min = hm(P["entry_window_end"]), hm(P["flatten"]), hm(P["news_time"])
        self.dsh = int(M.get("day_start_hour", 0))
        self.closed = {5, 6} if M.get("closed_weekends") else set()
        self.tradeclosed = {5, 6} if M.get("trade_weekdays_only", True) else set()
        self.cal = calendar if calendar is not None else load_calendar()
        self.st = state or self.new_state()
        self.trades_out, self.days_out, self.events = [], [], []

    # ------------------------------------------------------------------ state
    def new_state(self):
        b = float(self.acc["start_balance"])
        return {"equity": b, "start_balance": b, "last_ts": 0, "day": None, "last_key_dn": None}

    def _off(self):
        return self.dsh * HOUR - DAY if self.dsh > 9 else self.dsh * HOUR

    def _prev(self, dn):
        p = dn - 1
        while (p + 3) % 7 in self.closed:
            p -= 1
        return p

    def _ev(self, day, typ, t, px, msg):
        e = {"t": int(t), "type": typ, "px": None if px is None else float(px), "msg": msg}
        day["events"].append(e)
        self.events.append(dict(e, mkt=self.M["key"], date=day["date"]))

    # ------------------------------------------------------------------ main step
    def step(self, D, i):
        st = self.st
        t, m, dn = D.T[i], D.M[i], D.DN[i]
        st["last_ts"] = t
        day = st["day"]
        if day is not None and dn != day["dn"]:
            self._rollover(D, i)
            day = None
        if day is None:
            if self.k_min <= m < self.k_min + 5 and st.get("last_key_dn") != dn:
                st["last_key_dn"] = dn
                if D.WD[i] in self.tradeclosed:
                    return
                day = self._new_day(D, i)
                st["day"] = day
                if day["final"]:
                    return
            else:
                return
        if day["final"] and not day.get("pos"):
            return
        self._bar(D, i, day)

    def _bar(self, D, i, day):
        t, m, o, h, l, c = D.T[i], D.M[i], D.O[i], D.H[i], D.L[i], D.C[i]
        # running 5m bucket (aligned to clock)
        b = t // (5 * MIN)
        b5 = day.get("b5")
        if not b5 or b5["id"] != b:
            b5 = day["b5"] = {"id": b, "o": o, "h": h, "l": l, "c": c}
        else:
            b5["h"], b5["l"], b5["c"] = max(b5["h"], h), min(b5["l"], l), c
        closed5 = dict(b5) if (t // MIN + 1) % 5 == 0 else None
        ph = day["phase"]
        if ph == "position":
            self._manage(D, i, day)
        elif ph == "order":
            self._order(D, i, day)
        elif ph in ("await", "manip", "rbscan"):
            if m >= self.w_end:
                self._done(day, {"await": "NO_SETUP", "manip": "NO_CONFIRM", "rbscan": "NO_TRIGGER"}[ph], t,
                           "entry window closed")
            elif ph == "await":
                self._await(D, i, day, closed5)
            elif ph == "manip":
                self._manip(D, i, day, closed5)
            else:
                self._rbscan(D, i, day, closed5)
        day["rhi"] = h if day["rhi"] is None else max(day["rhi"], h)
        day["rlo"] = l if day["rlo"] is None else min(day["rlo"], l)

    # ------------------------------------------------------------------ day context
    def _new_day(self, D, i):
        P, dn, t = self.P, D.DN[i], D.T[i]
        dstr, o10, base = dn2str(dn), D.O[i], D.DN[i] * DAY
        cur_start = base + self._off()
        prev = self._prev(dn)
        pstart = prev * DAY + self._off()
        lv, show = [], {}

        def add(name, a, b):
            i0, i1 = D.win(a, b, i - 1)
            if i1 - i0 >= 3:
                jh = i0 + int(np.argmax(D.h[i0:i1]))
                jl = i0 + int(np.argmin(D.l[i0:i1]))
                lv.append([name + "H", D.H[jh], D.T[jh], 1])
                lv.append([name + "L", D.L[jl], D.T[jl], -1])
                show[name] = [D.H[jh], D.L[jl]]
            return i0, i1

        add("PD", pstart, pstart + (23 * HOUR if self.dsh > 9 else DAY))
        mon = dn - (dn + 3) % 7
        add("PW", (mon - 7) * DAY + self._off(), mon * DAY + self._off())
        a0, a1 = hm(P["asia"][0]), hm(P["asia"][1])
        add("ASIA", (dn - 1) * DAY + a0 * MIN if a0 > a1 else base + a0 * MIN, base + a1 * MIN)
        add("LON", base + hm(P["london"][0]) * MIN, base + hm(P["london"][1]) * MIN)
        on0, on1 = add("ON", cur_start, base + self.ny_min * MIN)
        add("NYO", base + self.ny_min * MIN, base + self.k_min * MIN)
        evs = self.cal.get(dstr, set())
        if evs & {"CPI", "PPI", "NFP"}:
            j = D.iloc(base + self.news_min * MIN)
            if j < i and D.M[j] - self.news_min < 3:
                lv.append(["DATAH", D.H[j], D.T[j], 1])
                lv.append(["DATAL", D.L[j], D.T[j], -1])
                show["DATA"] = [D.H[j], D.L[j]]
        mid = None
        j = D.iloc(base)
        if j < i and D.DN[j] == dn and D.M[j] < 5:
            mid = D.O[j]
        day = {"date": dstr, "dn": dn, "key_ts": t, "o10": o10, "mid": mid, "lv": lv, "show": show,
               "pstart": pstart, "phase": "await", "status": "WATCH", "reason": "", "final": False,
               "trades": 0, "wins": 0, "losses": 0, "risk_mult": 1.0, "rearm": False,
               "hi": None, "lo": None, "rhi": None, "rlo": None, "manip": None, "cnt": 0, "c1": False,
               "c5": False, "order": None, "pos": None, "b5": None, "events": [], "pnl": 0.0, "r": 0.0,
               "results": [], "attempts": []}
        day["gate"] = self._gate(D, i, dn, dstr, o10, on0, on1, base)
        self._ev(day, "KEY", t, o10, f"10:00 NY key open {o10:.2f}")
        if not day["gate"]["ok"]:
            self._ev(day, "GATE", t, None, f"day skipped - {day['gate']['reason']}")
            self._done(day, "SKIPPED", t, day["gate"]["reason"])
        else:
            self._ev(day, "GATE", t, None, "day gate passed - watching for manipulation")
        return day

    def _gate(self, D, i, dn, dstr, o10, on0, on1, base):
        P, F = self.P, self.P["filters"]
        ev = self.cal.get(dstr, set())
        wd = (dn + 3) % 7
        g = {"ok": True, "reason": "", "checks": [], "news": sorted(ev), "weekday": wd}

        def chk(name, ok, detail, on=True):
            g["checks"].append({"name": name, "pass": bool(ok), "detail": detail, "on": bool(on)})
            if on and not ok and g["ok"]:
                g["ok"], g["reason"] = False, name

        chk("FOMC day", "FOMC" not in ev, "FOMC 14:00 NY today" if "FOMC" in ev else "no FOMC", F["fomc"])
        majors = ev & {"CPI", "PPI", "NFP", "FOMC"}
        chk("No-news Monday", not (wd == 0 and not majors), "Monday without red-folder news" if wd == 0 and not majors
            else ("Monday + " + "/".join(sorted(majors)) if wd == 0 else "not Monday"), F["no_news_monday"])
        chk("Red-folder day", not (ev & {"CPI", "PPI", "NFP"}), "/".join(sorted(ev & {"CPI", "PPI", "NFP"})) or "none",
            F["news_day"])
        nxt = dn + 1
        while (nxt + 3) % 7 in {5, 6}:
            nxt += 1
        nfp_next = "NFP" in self.cal.get(dn2str(nxt), set())
        chk("Day before NFP", not nfp_next, "NFP next session" if nfp_next else "no", F["day_before_nfp"])
        # overnight expansion (Asia + London + pre-market), absolute and vs 20-day median
        pct = rel = None
        if on1 - on0 >= 30:
            pct = (float(D.h[on0:on1].max()) - float(D.l[on0:on1].min())) / o10 * 100
            hist, p = [], dn
            for _ in range(40):
                p = self._prev(p)
                a, b = D.win(p * DAY + self._off(), p * DAY + self.ny_min * MIN)
                if b - a >= 30:
                    lo_ = float(D.l[a:b].min())
                    hist.append((float(D.h[a:b].max()) - lo_) / lo_ * 100)
                if len(hist) >= 20:
                    break
            if len(hist) >= 5:
                rel = pct / float(np.median(hist))
        ok = pct is None or (pct <= P["expansion_pct"] and (rel is None or rel <= F["expansion_rel"]))
        chk("Overnight expansion", ok,
            "no overnight data" if pct is None else
            f"{pct:.2f}% range" + (f" · {rel:.1f}× 20d median" if rel else "") +
            f" (limit {P['expansion_pct']}% / {F['expansion_rel']}×)", F["expansion"])
        g["exp_pct"], g["exp_rel"] = pct, rel
        # NQ/ES (BTC/ETH) correlation of 1m returns 09:30-10:00
        corr = self._corr(D, base + self.ny_min * MIN, base + self.k_min * MIN, i - 1)
        chk("Pair correlation", corr is None or corr >= P["min_corr"],
            "no pair data" if corr is None else f"ρ = {corr:.2f} (min {P['min_corr']})", F["correlation"])
        g["corr"] = corr
        g["judas"] = self._judas(D, i, base)
        return g

    def _corr(self, D, a, b, upto):
        if D.pair is None:
            return None
        i0, i1 = D.win(a, b, upto)
        if i1 - i0 < 15:
            return None
        ts, c = D.ts[i0:i1], D.c[i0:i1]
        pts = D.pair["ts"]
        j = np.searchsorted(pts, ts)
        jj = np.minimum(j, pts.size - 1)
        ok = pts[jj] == ts
        if ok.sum() < 15:
            return None
        ra, rb = np.diff(np.log(c[ok])), np.diff(np.log(D.pair["c"][jj[ok]]))
        if ra.std() == 0 or rb.std() == 0:
            return None
        return float(np.corrcoef(ra, rb)[0, 1])

    def _judas(self, D, i, base):
        i0, i1 = D.win(base + self.ny_min * MIN, base + self.k_min * MIN, i - 1)
        if i1 - i0 < 3:
            return 0
        o = D.O[i0]
        thr = o * self.P["manip_min_pct"] / 100
        for j in range(i0, i1):
            up, dn = D.H[j] - o >= thr, o - D.L[j] >= thr
            if up and dn:
                return 0
            if up:
                return 1
            if dn:
                return -1
        return 0

    # ------------------------------------------------------------------ setup phases
    def _await(self, D, i, day, c5):
        P, t, h, l, o10 = self.P, D.T[i], D.H[i], D.L[i], day["o10"]
        thr, tol = o10 * P["manip_min_pct"] / 100, o10 * P["wick_tol_pct"] / 100
        day["hi"] = h if day["hi"] is None else max(day["hi"], h)
        day["lo"] = l if day["lo"] is None else min(day["lo"], l)
        if not day["rearm"]:
            up, dn = day["hi"] - o10 >= thr, o10 - day["lo"] >= thr
            if not (up or dn):
                return
            if up and dn:
                return self._done(day, "TWO_SIDED", t, "manipulated both sides of the 10am open in one candle")
            d = 1 if up else -1
            opp = (o10 - day["lo"]) if d == 1 else (day["hi"] - o10)
            clean = opp <= tol
            if P["require_clean"] and not clean:
                return self._done(day, "NOT_CLEAN", t, f"opposite wick {opp:.2f} > tolerance {tol:.2f} (chop)")
            ext = day["hi"] if d == 1 else day["lo"]
        else:
            up, dn = h > day["sweep_hi"] + tol, l < day["sweep_lo"] - tol
            if not (up or dn):
                return
            if up and dn:
                return self._done(day, "TWO_SIDED", t, "re-arm: both sides of the range swept in one candle")
            d, clean, ext = (1, True, h) if up else (-1, True, l)
        day["manip"] = {"dir": d, "ts": t, "ext": ext, "ext_ts": t, "clean": clean, "swept": None}
        day["phase"], day["cnt"], day["c1"], day["c5"] = "manip", 0, False, False
        self._ev(day, "MANIP", t, ext, f"manipulation {'UP' if d == 1 else 'DOWN'} from the 10am open"
                                       f"{' (range sweep)' if day['rearm'] else ''} → watching for {DIRN[-d]}")
        self._manip(D, i, day, c5)

    def _manip(self, D, i, day, c5):
        P, t, h, l, c, o10 = self.P, D.T[i], D.H[i], D.L[i], D.C[i], day["o10"]
        mp = day["manip"]
        d, s = mp["dir"], -mp["dir"]
        if (d == 1 and h > mp["ext"]) or (d == -1 and l < mp["ext"]):
            mp["ext"], mp["ext_ts"] = (h if d == 1 else l), t
            day["c1"] = day["c5"] = False
            day["cnt"] = 0
        beyond = c < o10 if s == -1 else c > o10
        day["cnt"] = day["cnt"] + 1 if beyond else 0
        if day["cnt"] >= P["confirm_closes"]:
            day["c1"] = True
        if c5 is not None and ((c5["c"] < o10) if s == -1 else (c5["c"] > o10)):
            day["c5"] = True
        if day["c1"] and (day["c5"] or not P["confirm_5m"]):
            self._confirm(D, i, day)

    def _confirm(self, D, i, day):
        P, B, t, o10 = self.P, self.P["bias"], D.T[i], day["o10"]
        mp = day["manip"]
        s = -mp["dir"]
        mp["confirm_ts"] = t
        mp["swept"] = self._swept(D, i, day)
        self._ev(day, "CONFIRM", t, D.C[i],
                 f"distribution confirmed ({P['confirm_closes']}×1m{' + 5m' if P['confirm_5m'] else ''} close "
                 f"{'below' if s == -1 else 'above'} 10am open) · {'swept ' + mp['swept'] if mp['swept'] else 'no sweep'}")
        why = None
        if B.get("require_sweep") and not mp["swept"]:
            why = "manipulation did not sweep liquidity"
        if not why and B.get("judas"):
            j = day["gate"]["judas"]
            if j == 0 or -j != s:
                why = f"Judas bias {'none' if j == 0 else DIRN[-j]} disagrees"
        if not why and B.get("midnight") and day["mid"] is not None:
            if (s == -1 and not o10 > day["mid"]) or (s == 1 and not o10 < day["mid"]):
                why = "10am open on wrong side of midnight open (premium/discount)"
        if not why and B.get("pair_confirm"):
            ps = self._pair_side(D, i, day)
            if ps is not None and ps != s:
                why = "SMT: pair did not confirm (still on the other side of its 10am open)"
        if why:
            return self._done(day, "FILTERED", t, why)
        mode = P["entry_mode"]
        if mode == "limit_then_rb":
            mode = "limit" if day["trades"] == 0 else "rb"
        if mode == "limit":
            plan, why = self._plan(D, i, day, s, o10, "limit")
            if plan is None:
                return self._done(day, "NO_PLAN", t, why)
            self._place(D, i, day, plan)
        else:
            day["phase"] = "rbscan"
            self._ev(day, "SCAN", t, None, f"scanning {P['rb_tf']}m rejection blocks at the 10am open")

    def _rbscan(self, D, i, day, c5):
        P, t = self.P, D.T[i]
        mp, o10 = day["manip"], day["o10"]
        s = -mp["dir"]
        if P["rb_tf"] == 5:
            if c5 is None:
                return
            o, h, l, c = c5["o"], c5["h"], c5["l"], c5["c"]
        else:
            o, h, l, c = D.O[i], D.H[i], D.L[i], D.C[i]
        if (s == -1 and c > mp["ext"]) or (s == 1 and c < mp["ext"]):
            return self._done(day, "INVALIDATED", t, "closed beyond the manipulation extreme")
        zone = o10 * P["zone_pct"] / 100
        rng = h - l
        if rng <= 0:
            return
        if s == -1:
            wick = h - max(o, c)
            ok = h >= o10 - zone and c < o and c < o10 and wick >= P["rb_wick_frac"] * rng
            top = max(o, c)
            ref = {"body": top, "ce": (h + top) / 2, "q25": top + 0.25 * wick}[P["rb_entry"]]
            ext = h
        else:
            wick = min(o, c) - l
            ok = l <= o10 + zone and c > o and c > o10 and wick >= P["rb_wick_frac"] * rng
            bot = min(o, c)
            ref = {"body": bot, "ce": (l + bot) / 2, "q25": bot - 0.25 * wick}[P["rb_entry"]]
            ext = l
        if not ok:
            return
        plan, why = self._plan(D, i, day, s, ref, "rb", rb_ext=ext)
        if plan is None:
            self._ev(day, "RB", t, ref, f"rejection block ignored: {why}")
            return
        plan["valid_until"] = t + P["rb_valid_bars"] * MIN
        self._place(D, i, day, plan)

    # ------------------------------------------------------------------ levels / targets
    def _swings(self, D, i, day):
        a, b = D.iloc(day["pstart"]), i + 1
        if b - a < 30:
            return []
        ts = D.ts[a:b]
        bid = ts // (5 * MIN)
        starts = np.flatnonzero(np.r_[True, bid[1:] != bid[:-1]])
        hh = np.maximum.reduceat(D.h[a:b], starts)
        ll = np.minimum.reduceat(D.l[a:b], starts)
        ends = np.r_[starts[1:], b - a] - 1 + a
        nb = len(starts) - (0 if (D.T[i] // MIN + 1) % 5 == 0 else 1)
        L, R = int(self.P["swing_left"]), int(self.P["swing_right"])
        out = []
        for j in range(L, nb - R):
            if hh[j] > hh[j - L:j].max() and hh[j] >= hh[j + 1:j + R + 1].max():
                out.append(["SWH", float(hh[j]), int(D.T[ends[j]]), 1])
            if ll[j] < ll[j - L:j].min() and ll[j] <= ll[j + 1:j + R + 1].min():
                out.append(["SWL", float(ll[j]), int(D.T[ends[j]]), -1])
        return out

    def _untaken(self, D, i, lev, upto=None):
        j0 = D.at(lev[2]) + 1
        j1 = (i if upto is None else upto) + 1
        if j1 <= j0:
            return True
        return float(D.l[j0:j1].min()) > lev[1] if lev[3] == -1 else float(D.h[j0:j1].max()) < lev[1]

    def _targets(self, D, i, day, s, entry):
        cand = [x for x in day["lv"] + self._swings(D, i, day) if x[3] == s]
        if day["mid"] is not None:
            cand.append(["MIDNIGHT", day["mid"], day["key_ts"] - MIN, s])
        out, mx = [], entry * self.P["max_target_pct"] / 100
        for lev in cand:
            dist = (lev[1] - entry) * s
            if 0 < dist <= mx and self._untaken(D, i, lev):
                out.append((dist, lev[0], lev[1]))
        out.sort()
        return out

    def _swept(self, D, i, day):
        mp, o10 = day["manip"], day["o10"]
        d, ext = mp["dir"], mp["ext"]
        ki = D.at(day["key_ts"])
        for lev in day["lv"] + [x for x in self._swings(D, ki - 1, day) if x[2] < day["key_ts"]]:
            if lev[3] != d:
                continue
            p = lev[1]
            inside = (o10 < p < ext) if d == 1 else (ext < p < o10)
            if inside and self._untaken(D, ki - 1, lev):
                return lev[0]
        return None

    def _pair_side(self, D, i, day):
        if D.pair is None:
            return None
        pts = D.pair["ts"]
        k = int(np.searchsorted(pts, day["key_ts"]))
        j = int(np.searchsorted(pts, D.T[i], "right")) - 1
        if k >= pts.size or j < k or pts[k] - day["key_ts"] > 5 * MIN:
            return None
        po, pc = float(D.pair["o"][k]), float(D.pair["c"][j])
        return 1 if pc > po else -1 if pc < po else None

    # ------------------------------------------------------------------ planning / sizing
    def _plan(self, D, i, day, s, entry, kind, rb_ext=None):
        P, tick = self.P, float(self.ins["tick_size"])
        mp = day["manip"]
        min_sl = entry * (P["rb_min_sl_pct"] if kind == "rb" else P["min_sl_pct"]) / 100
        max_sl = entry * P["max_sl_pct"] / 100
        buf = entry * P["sl_buffer_pct"] / 100
        rr_min = float(P["rr_min"])
        mode = "rb" if kind == "rb" else P["sl_mode"]
        dist = None
        if mode == "rb":
            dist = abs(rb_ext - entry) + buf
        elif mode == "manip":
            dist = abs(mp["ext"] - entry) + buf
        elif mode == "fib":
            j0 = D.at(mp["ext_ts"])
            if s == -1:
                lo = float(D.l[j0:i + 1].min())
                lvl = lo + P["fib_level"] * (mp["ext"] - lo)
                dist = lvl + buf - entry
            else:
                hi = float(D.h[j0:i + 1].max())
                lvl = hi - P["fib_level"] * (hi - mp["ext"])
                dist = entry - (lvl - buf)
        elif mode == "fixed":
            dist = entry * P["fixed_sl_pct"] / 100
        tname, tp = "R-multiple", None
        if P["tp_mode"] == "dol":
            tg = self._targets(D, i, day, s, entry)
            if mode == "rr_target":
                need = rr_min * min_sl
                pick = next((x for x in tg if x[0] >= need), None)
                if pick is None:
                    return None, "no untaken draw on liquidity far enough for 1:%g" % rr_min
                dist = max(min_sl, pick[0] / P["rr_target"])
            else:
                dist = max(dist, min_sl)
                pick = next((x for x in tg if x[0] >= rr_min * dist), None)
                if pick is None:
                    return None, "no untaken draw on liquidity at >= 1:%g" % rr_min
            tname, tp = pick[1], pick[2]
        else:
            dist = max(min_sl, dist if dist is not None else min_sl)
            tp = entry + s * P["tp_rr"] * dist
        dist = max(dist, min_sl)
        if dist > max_sl:
            return None, f"stop {dist / entry * 100:.2f}% wider than max {P['max_sl_pct']}%"
        sl = round_tick(entry - s * dist, tick, "down" if s == 1 else "up")
        tp = round_tick(tp, tick)
        dist = abs(entry - sl)
        rr = abs(tp - entry) / dist if dist else 0
        if rr < rr_min * 0.99:
            return None, f"R:R 1:{rr:.2f} below minimum 1:{rr_min:g}"
        sz, why = self._size(entry, sl, day["risk_mult"])
        if sz is None:
            return None, why
        return dict(sz, side=s, entry=float(entry), sl=sl, sl0=sl, tp=tp, dist=dist, rr=rr, target=tname,
                    kind=kind, swept=mp.get("swept"), placed_ts=int(D.T[i])), None

    def _size(self, entry, sl, mult):
        acc, ins, pv = self.acc, self.ins, self.costs.pv
        eq = self.st["equity"]
        if eq <= 0:
            return None, "account blown"
        base = eq if acc["compound"] else acc["start_balance"]
        risk = base * acc["risk_pct"] / 100 * mult
        per = abs(entry - sl) * pv + (self.costs.per_unit(entry, sl) if self.fees.get("fee_in_risk", True) else 0)
        qty = risk / per
        qty = min(qty, eq * acc["max_leverage"] / (entry * pv))
        step = float(ins["qty_step"])
        qty = round(math.floor(qty / step + 1e-9) * step, 8)
        if qty < ins["min_qty"] - 1e-12:
            return None, (f"size {qty:g} below min {ins['min_qty']} {ins.get('unit', '')} "
                          f"(risk ${risk:.2f} < ${per * ins['min_qty']:.2f} needed)")
        notional = qty * entry * pv
        if notional < ins.get("min_notional", 0):
            return None, f"notional ${notional:.2f} below minimum"
        return {"qty": qty, "notional": notional, "leverage": notional / eq, "risk_amt": qty * per,
                "risk_target": risk}, None

    def _place(self, D, i, day, plan):
        day["order"] = plan
        day["phase"] = "order"
        self._ev(day, "ORDER", D.T[i], plan["entry"],
                 f"{DIRN[plan['side']]} {plan['kind'].upper()} @ {plan['entry']:.2f} · SL {plan['sl']:.2f} · "
                 f"TP {plan['tp']:.2f} ({plan['target']}) · 1:{plan['rr']:.1f} · qty {plan['qty']:g} "
                 f"· risk ${plan['risk_amt']:.2f}")

    # ------------------------------------------------------------------ orders / positions
    def _order(self, D, i, day):
        P, t, m, o, h, l = self.P, D.T[i], D.M[i], D.O[i], D.H[i], D.L[i]
        od = day["order"]
        s, E = od["side"], od["entry"]
        if m >= self.w_end:
            day["order"] = None
            return self._done(day, "NOT_FILLED", t, "limit not filled inside the entry window")
        if od.get("valid_until") and t > od["valid_until"]:
            day["order"], day["phase"] = None, "rbscan"
            self._ev(day, "CANCEL", t, E, "rejection-block order expired → scanning again")
            return
        ft = P["fill_through_ticks"] * self.costs.tick
        if (s == 1 and l <= E - ft) or (s == -1 and h >= E + ft):
            px = min(E, o) if s == 1 else max(E, o)
            self._open(D, i, day, od, px)
            pos = day["pos"]
            if (s == 1 and l <= pos["sl"]) or (s == -1 and h >= pos["sl"]):   # worst case inside fill candle
                ex = o if ((s == 1 and o <= pos["sl"]) or (s == -1 and o >= pos["sl"])) else pos["sl"]
                self._close(D, i, day, ex, "SL", "taker")
            return
        if (s == 1 and h >= od["tp"]) or (s == -1 and l <= od["tp"]):
            day["order"] = None
            return self._done(day, "RAN_AWAY", t, "target hit before the retest - left behind")
        mp = day["manip"]
        if od["kind"] == "rb" and ((s == -1 and h > mp["ext"]) or (s == 1 and l < mp["ext"])):
            day["order"] = None
            return self._done(day, "INVALIDATED", t, "price went beyond the manipulation extreme")

    def _open(self, D, i, day, od, px):
        pos = dict(od)
        pos["entry"] = float(px)
        pos["dist"] = abs(px - pos["sl"])
        pos["entry_ts"] = int(D.T[i])
        pos["fee_entry"] = self.costs.fee(pos["qty"], px, "maker")
        pos.update(funding=0.0, mae=0.0, mfe=0.0, be=False, day=day["date"])
        day["pos"], day["order"], day["phase"] = pos, None, "position"
        day["attempts"].append({"side": pos["side"], "entry": px, "kind": pos["kind"]})
        self._ev(day, "FILL", D.T[i], px, f"FILLED {DIRN[pos['side']]} {pos['qty']:g} @ {px:.2f}")

    def _manage(self, D, i, day):
        P, t, m, o, h, l = self.P, D.T[i], D.M[i], D.O[i], D.H[i], D.L[i]
        pos = day["pos"]
        s = pos["side"]
        if t > pos["entry_ts"]:
            if self.fees["model"] == "bybit" and self.fees.get("include_funding") and t in D.funding:
                pos["funding"] += s * D.funding[t] * pos["qty"] * o
            if self.fees["model"] == "cfd" and m == 17 * 60:
                rate = self.fees.get("swap_long_pct_year" if s == 1 else "swap_short_pct_year", 0) / 100 / 360
                pos["funding"] -= rate * pos["qty"] * o * self.costs.pv
        if m >= self.flat:
            return self._close(D, i, day, o, "EOD", "taker")
        sl, tp = pos["sl"], pos["tp"]
        rsn = "BE" if pos["be"] else "SL"
        if (s == 1 and o <= sl) or (s == -1 and o >= sl):
            return self._close(D, i, day, o, rsn, "taker")
        if (s == 1 and o >= tp) or (s == -1 and o <= tp):
            return self._close(D, i, day, o, "TP", "maker")
        self._excursion(pos, h, l)
        hit_sl = l <= sl if s == 1 else h >= sl
        hit_tp = h >= tp if s == 1 else l <= tp
        if hit_sl and (not hit_tp or P["both_hit"] == "SL"):
            return self._close(D, i, day, sl, rsn, "taker")
        if hit_tp:
            return self._close(D, i, day, tp, "TP", "maker")
        if not pos["be"] and P["be_trigger_r"] > 0 and pos["mfe"] >= P["be_trigger_r"] * pos["dist"]:
            pos["sl"] = round_tick(pos["entry"] + s * P["be_offset_r"] * pos["dist"], self.costs.tick,
                                   "up" if s == 1 else "down")
            pos["be"] = True
            self._ev(day, "BE", t, pos["sl"], f"stop moved to break-even {pos['sl']:.2f}")

    @staticmethod
    def _excursion(pos, h, l):
        e, s = pos["entry"], pos["side"]
        pos["mfe"] = max(pos["mfe"], (h - e) if s == 1 else (e - l))
        pos["mae"] = min(pos["mae"], (l - e) if s == 1 else (e - h))

    def _close(self, D, i, day, px, reason, kind):
        pos, t, m = day["pos"], D.T[i], D.M[i]
        s, pv = pos["side"], self.costs.pv
        if reason != "EOD":
            self._excursion(pos, D.H[i], D.L[i])
        if kind == "taker":
            px = px - s * self.costs.slip(px)
        fee_exit = self.costs.fee(pos["qty"], px, kind)
        gross = s * (px - pos["entry"]) * pos["qty"] * pv
        net = gross - pos["fee_entry"] - fee_exit - pos["funding"]
        d0 = abs(pos["entry"] - pos["sl0"]) or pos["dist"]
        self.st["equity"] += net
        tr = {"mkt": self.M["key"], "day": day["date"], "side": s, "entry_ts": pos["entry_ts"],
              "exit_ts": int(t) if reason == "EOD" else int(t) + MIN - 1, "entry": pos["entry"], "exit": float(px),
              "sl": pos["sl"], "sl0": pos["sl0"], "tp": pos["tp"], "qty": pos["qty"], "notional": pos["notional"],
              "leverage": pos["leverage"], "risk_amt": pos["risk_amt"], "fee_entry": pos["fee_entry"],
              "fee_exit": fee_exit, "funding": pos["funding"], "pnl_gross": gross, "pnl_net": net,
              "r": net / pos["risk_amt"] if pos["risk_amt"] else 0.0, "rr": pos["rr"],
              "mae_r": pos["mae"] / d0 if d0 else 0, "mfe_r": pos["mfe"] / d0 if d0 else 0,
              "reason": reason, "equity_after": self.st["equity"], "entry_type": pos["kind"],
              "note": f"target {pos['target']}" + (f" · swept {pos['swept']}" if pos.get("swept") else "")}
        self.trades_out.append(tr)
        day["pos"] = None
        day["trades"] += 1
        day["pnl"] += net
        day["r"] += tr["r"]
        win = reason == "TP" or (reason == "EOD" and net > 0)
        loss = reason == "SL" or (reason == "EOD" and net <= 0)
        day["wins"] += int(win)
        day["losses"] += int(loss)
        day["results"].append({"reason": reason, "r": tr["r"], "pnl": net, "side": s})
        self._ev(day, "EXIT", t, px, f"{reason} {DIRN[s]} @ {px:.2f} · {net:+.2f} ({tr['r']:+.2f}R) · "
                                     f"equity {self.st['equity']:.2f}")
        P = self.P
        if day["final"]:
            return
        if (win and P["stop_after_win"]) or day["trades"] >= P["max_trades"] or m >= self.w_end:
            return self._done(day, "TRADED", t, "daily rules: done for the day")
        if loss:
            day["risk_mult"] = P["second_risk_mult"]
        day.update(phase="await", rearm=True, hi=None, lo=None, manip=None,
                   sweep_hi=max(day["rhi"] or D.H[i], D.H[i]), sweep_lo=min(day["rlo"] or D.L[i], D.L[i]))
        self._ev(day, "REARM", t, None, f"re-armed: next setup needs a sweep of the 10am range "
                                        f"({day['sweep_lo']:.2f} – {day['sweep_hi']:.2f})"
                                        f"{', 50% risk' if loss else ''}")

    def _done(self, day, status, t, why):
        day["status"] = "SKIPPED" if status == "SKIPPED" else ("TRADED" if day["trades"] else status)
        day["reason"] = why
        day["phase"] = "done"
        if status != "SKIPPED":
            self._ev(day, "DONE", t, None, f"{status}: {why}")
        if not day.get("pos") and not day.get("order"):
            self._finalize(day)

    def _finalize(self, day):
        if day["final"]:
            return
        day["final"] = True
        self.days_out.append(day_record(day))

    def _rollover(self, D, i):
        day = self.st["day"]
        if day.get("pos"):
            self._close(D, i, day, D.O[i], "EOD", "taker")
        day["order"] = None
        if not day["final"]:
            if day["phase"] != "done":
                day["status"] = "TRADED" if day["trades"] else "INCOMPLETE"
                day["reason"] = day["reason"] or "data ended / day rolled over"
            self._finalize(day)
        self.st["day"] = None


def day_record(day):
    keep = ("date", "key_ts", "o10", "mid", "show", "gate", "status", "reason", "trades", "wins", "losses",
            "pnl", "r", "results", "events", "attempts")
    rec = {k: copy.deepcopy(day.get(k)) for k in keep}
    mp = day.get("manip")
    rec["manip"] = None if not mp else {k: mp.get(k) for k in ("dir", "ts", "ext", "ext_ts", "clean", "swept",
                                                               "confirm_ts")}
    return rec


def live_day_view(day):
    """Current day incl. resting order/position for the dashboard."""
    if not day:
        return None
    rec = day_record(day)
    rec.update(phase=day["phase"], order=day.get("order"), pos=day.get("pos"), cnt=day.get("cnt"),
               c1=day.get("c1"), c5=day.get("c5"), rearm=day.get("rearm"), risk_mult=day.get("risk_mult"),
               final=day.get("final"))
    return rec


# ============================================================================ backtest
def backtest(M, D, start_ts, progress=None, calendar=None):
    eng = Engine(M, calendar=calendar)
    eng.st["started_at"] = int(start_ts)
    i0 = D.at(start_ts)
    n = D.n
    step = max(1, (n - i0) // 60)
    for i in range(i0, n):
        if progress and (i - i0) % step == 0:
            progress((i - i0) / max(1, n - i0))
        eng.step(D, i)
    for k, t in enumerate(eng.trades_out, 1):
        t["id"] = k
    return eng


# ============================================================================ statistics
def compute_stats(trades, start_balance, start_ms, end_ms, M, unrealized=0.0):
    tz = ZoneInfo(M["display_tz"])
    tr = sorted([t for t in trades if t.get("exit_ts")], key=lambda t: (t["exit_ts"], t.get("id", 0)))
    n = len(tr)
    eq = peak = float(start_balance)
    curve = [[int(start_ms), round(eq, 4)]]
    max_dd_pct = cum_r = peak_r = max_dd_r = 0.0
    gw = gl = fees = funding = notional = lev = dur = slp = 0.0
    wins, losses, bes = [], [], 0
    best_w = best_l = cur_w = cur_l = 0
    day_pnl, day_start_eq = {}, {}
    for t in tr:
        p = t["pnl_net"]
        dk = datetime.fromtimestamp(t["exit_ts"] / 1000, tz).date().isoformat()
        if dk not in day_start_eq:
            day_start_eq[dk] = eq
        day_pnl[dk] = day_pnl.get(dk, 0.0) + p
        eq += p
        curve.append([int(t["exit_ts"]), round(eq, 4)])
        peak = max(peak, eq)
        if peak > 0:
            max_dd_pct = max(max_dd_pct, (peak - eq) / peak * 100)
        cum_r += t["r"]
        peak_r = max(peak_r, cum_r)
        max_dd_r = max(max_dd_r, peak_r - cum_r)
        if t["reason"] == "BE":
            bes += 1
        if p > 0:
            wins.append(t); gw += p; cur_w += 1; cur_l = 0
        else:
            losses.append(t); gl += -p; cur_l += 1; cur_w = 0
        best_w, best_l = max(best_w, cur_w), max(best_l, cur_l)
        fees += t["fee_entry"] + t["fee_exit"]
        funding += t["funding"]
        notional += t["notional"]
        lev += t["leverage"]
        dur += t["exit_ts"] - t["entry_ts"]
        slp += abs(t["entry"] - t["sl0"]) / t["entry"] * 100
    days = max((end_ms - start_ms) / DAY, 0.0)
    net = eq - start_balance
    wr = len(wins) / n * 100 if n else None
    rs = np.array([t["r"] for t in tr]) if n else np.array([])
    avg_wr = float(np.mean([t["r"] for t in wins])) if wins else None
    avg_lr = float(np.mean([t["r"] for t in losses])) if losses else None
    cagr = None
    if days >= 1 and eq > 0:
        try:
            cagr = ((eq / start_balance) ** (365.0 / days) - 1) * 100
            cagr = None if cagr > 1e7 else cagr
        except OverflowError:
            cagr = None
    sharpe, max_daily_dd = None, 0.0
    for dk, p in day_pnl.items():
        if day_start_eq[dk] > 0:
            max_daily_dd = min(max_daily_dd, p / day_start_eq[dk] * 100)
    if days >= 2:
        d0 = datetime.fromtimestamp(start_ms / 1000, tz).date()
        d1 = datetime.fromtimestamp(end_ms / 1000, tz).date()
        e, rets, d = float(start_balance), [], d0
        while d <= d1:
            if d.weekday() < 5:
                p = day_pnl.get(d.isoformat(), 0.0)
                rets.append(p / e if e > 0 else 0.0)
                e += p
            d += timedelta(days=1)
        rets = np.array(rets)
        if rets.size > 1 and rets.std() > 0:
            sharpe = float(rets.mean() / rets.std() * math.sqrt(252))
    avg_rtr = (avg_wr / abs(avg_lr)) if (avg_wr is not None and avg_lr) else None
    kelly = (wr / 100 - (1 - wr / 100) / avg_rtr) * 100 if (wr is not None and avg_rtr) else None
    sqn = float(math.sqrt(n) * rs.mean() / rs.std()) if n > 1 and rs.std() > 1e-6 else None
    acc = M["account"]
    return {
        "trades": n, "wins": len(wins), "losses": len(losses), "breakevens": bes, "winrate": wr,
        "cum_r": float(rs.sum()) if n else 0.0, "max_dd_r": max_dd_r,
        "start_balance": start_balance, "account": eq + unrealized, "realized_equity": eq, "unrealized": unrealized,
        "risk_pct": acc["risk_pct"], "risk_usd": (eq if acc["compound"] else start_balance) * acc["risk_pct"] / 100,
        "fee_setup": Costs(M).describe(), "fee_label": M.get("fee_label"),
        "days": days, "net_pnl": net, "return_pct": net / start_balance * 100 if start_balance else None,
        "profit_factor": (gw / gl) if gl > 0 else (None if gw == 0 else float("inf")),
        "cagr": cagr, "avg_win": gw / len(wins) if wins else None, "avg_loss": -gl / len(losses) if losses else None,
        "avg_win_r": avg_wr, "avg_loss_r": avg_lr, "max_dd_pct": max_dd_pct,
        "max_consec_wins": best_w, "max_consec_losses": best_l, "cur_streak": cur_w if cur_w else -cur_l,
        "avg_rtr": avg_rtr, "avg_planned_rr": float(np.mean([t["rr"] for t in tr])) if n else None,
        "max_daily_dd": max_daily_dd, "fees_total": fees, "funding_total": funding,
        "avg_fees": fees / n if n else None, "avg_order": notional / n if n else None,
        "avg_leverage": lev / n if n else None, "avg_duration_min": dur / n / MIN if n else None,
        "avg_sl_pct": slp / n if n else None, "expectancy_r": float(rs.mean()) if n else None,
        "fees_per_r": fees / n / (np.mean([t["risk_amt"] for t in tr])) if n else None,
        "sqn": sqn, "sharpe": sharpe, "kelly": kelly, "trading_days": len(day_pnl), "equity_curve": curve,
    }


def day_summary(days):
    out = {"days": len(days), "by_status": {}, "setups": 0, "fills": 0, "skips": {}}
    for d in days:
        out["by_status"][d["status"]] = out["by_status"].get(d["status"], 0) + 1
        if d.get("manip") and d["manip"].get("confirm_ts"):
            out["setups"] += 1
        out["fills"] += len(d.get("attempts") or [])
        if d["status"] == "SKIPPED":
            out["skips"][d["reason"]] = out["skips"].get(d["reason"], 0) + 1
    return out
