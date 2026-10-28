#!/usr/bin/env python3
"""
TEN·AM Desk - dashboard + backtester + variant lab (port 8757). Runs in tmux session "TENweb".
Cloudflare-tunnel friendly: relative URLs, plain HTTP polling, no websockets, no-store API caching,
optional basic-auth password.
"""
import copy
import gzip
import json
import logging
import os
import threading
import time

from flask import Flask, Response, request, send_from_directory

import tenam_core as core

cfg = core.load_config()
log = core.setup_logging("web", cfg)
app = Flask(__name__, static_folder=None)
STATIC = os.path.join(core.BASE_DIR, "static")
JOB = {"running": False, "kind": None, "mkt": None, "stage": "idle", "pct": 0.0, "error": None,
       "started": None, "finished": None, "params": None}
LOCK = threading.Lock()
CACHE = {}

VARIANTS = [
    ("Powell baseline (your settings)", {}),
    ("No day filters", {"filters": {"fomc": False, "expansion": False, "correlation": False, "no_news_monday": False}}),
    ("No 5m close required", {"confirm_5m": False}),
    ("3 × 1m closes", {"confirm_closes": 3}),
    ("SL beyond manipulation extreme", {"sl_mode": "manip"}),
    ("SL above fib 0.5 of leg", {"sl_mode": "fib"}),
    ("Fixed 3R, fixed stop", {"tp_mode": "rr", "tp_rr": 3.0, "sl_mode": "fixed"}),
    ("Fixed 5R, fixed stop", {"tp_mode": "rr", "tp_rr": 5.0, "sl_mode": "fixed"}),
    ("No break-even", {"be_trigger_r": 0}),
    ("1m rejection-block entry", {"entry_mode": "rb", "rb_tf": 1}),
    ("5m rejection-block entry", {"entry_mode": "rb", "rb_tf": 5}),
    ("Limit, then RB re-entry", {"entry_mode": "limit_then_rb"}),
    ("Require liquidity sweep", {"bias": {"require_sweep": True}}),
    ("Judas-swing bias (09:30)", {"bias": {"judas": True}}),
    ("Midnight-open premium/discount", {"bias": {"midnight": True}}),
    ("SMT pair confirmation", {"bias": {"pair_confirm": True}}),
    ("Max 1 trade per day", {"max_trades": 1}),
    ("Entry window until 12:00", {"entry_window_end": "12:00"}),
]


def bt_path(mkt, kind="backtest"):
    return os.path.join(core.DATA_DIR, f"{kind}_{mkt}.json")


def read_json(path):
    try:
        st = os.stat(path)
        c = CACHE.get(path)
        if c and c[0] == st.st_mtime:
            return c[1]
        with open(path, encoding="utf-8") as f:
            v = json.load(f)
        CACHE[path] = (st.st_mtime, v)
        return v
    except FileNotFoundError:
        return None


# ------------------------------------------------------------------ helpers
def jresp(obj, status=200):
    data = json.dumps(core.clean_json(obj), separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
    if "gzip" in request.headers.get("Accept-Encoding", "") and len(data) > 4096:
        data = gzip.compress(data, 5)
        headers["Content-Encoding"] = "gzip"
        headers["Vary"] = "Accept-Encoding"
    return Response(data, status=status, headers=headers)


def mkt_arg():
    m = request.args.get("mkt", "btc")
    return m if m in core.MARKETS else "btc"


@app.before_request
def _auth():
    pw = cfg["web"].get("password")
    if not pw or request.path == "/healthz":
        return None
    a = request.authorization
    if not a or a.password != pw:
        return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="TEN-AM Desk"'})
    return None


@app.errorhandler(Exception)
def _err(e):
    code = getattr(e, "code", 500)
    if not isinstance(code, int):
        code = 500
    if code >= 500:
        log.exception(f"web error on {request.path}")
    return jresp({"error": str(e)}, code)


# ------------------------------------------------------------------ state builders
def paper_state(mkt):
    M = core.market_cfg(cfg, mkt)
    con = core.db_connect()
    try:
        st = core.kv_get(con, f"paper_state_{mkt}") or {}
        live = core.kv_get(con, f"paper_live_{mkt}") or {}
        trades = core.load_trades(con, mkt)
        days = core.load_days(con, mkt, 400)
    finally:
        con.close()
    now = core.now_ms()
    day = st.get("day")
    view = core.live_day_view(day)
    pos = (day or {}).get("pos")
    unreal = live.get("unrealized") or 0.0 if pos else 0.0
    bal = st.get("start_balance", M["account"]["start_balance"])
    stats = core.compute_stats(trades, bal, st.get("started_at", now), now, M, unreal)
    curve = stats.pop("equity_curve")
    online = bool(live.get("last_tick")) and now - live["last_tick"] < 120_000
    engine = "RUNNING" if online else ("STALE" if live.get("last_tick") else "OFFLINE")
    if view and not (days and days[-1]["date"] == view["date"]):
        days = days + [view]
    return {"src": "paper", "mkt": mkt, "meta": {"start_ms": st.get("started_at"), "end_ms": now, "online": online},
            "engine": engine, "stats": stats, "equity_curve": curve, "trades": trades, "days": days, "today": view,
            "positions": [pos] if pos else [], "orders": [day["order"]] if day and day.get("order") else [],
            "live": live, "day_summary": core.day_summary([d for d in days if d.get("final", True)]),
            "market": public_market(M), "config": core.public_config(cfg), "server_time": now}


def public_market(M):
    return {k: M.get(k) for k in ("key", "label", "title", "venue", "feed", "pair", "backtest_feed", "backtest_pair",
                                  "account", "fees", "instrument", "fee_profile", "fee_label", "P", "day_start_hour")} | \
        {"fee_profiles": {k: v.get("label", k) for k, v in M.get("fee_profiles", {}).items()},
         "fee_setup": core.Costs(M).describe()}


def backtest_state(mkt):
    r = read_json(bt_path(mkt))
    if not r:
        M = core.market_cfg(cfg, mkt)
        return {"src": "backtest", "mkt": mkt, "empty": True, "job": JOB, "market": public_market(M),
                "config": core.public_config(cfg), "server_time": core.now_ms()}
    out = dict(r)
    out["job"] = JOB
    out["server_time"] = core.now_ms()
    return out


# ------------------------------------------------------------------ backtest job
def build_overrides(body, M):
    """Translate the dashboard form into market_cfg overrides."""
    ov = {"P": {}, "fees": {}, "account": {}}
    P = ov["P"]
    acc = ov["account"]
    if body.get("balance") not in (None, ""):
        acc["start_balance"] = max(10.0, float(body["balance"]))
    if body.get("risk") not in (None, ""):
        acc["risk_pct"] = max(0.05, min(float(body["risk"]), 20.0))
    if body.get("fee_profile") in M.get("fee_profiles", {}):
        ov["fee_profile"] = body["fee_profile"]
    for k in ("maker", "taker"):
        if body.get(k) not in (None, ""):
            ov["fees"][k] = float(body[k]) / 100
    for k in ("commission_per_contract_side", "spread_pts", "commission_pct_side", "slippage_pct",
              "slippage_ticks", "slippage_pts"):
        if body.get(k) not in (None, ""):
            ov["fees"][k] = float(body[k])
    if "funding" in body:
        ov["fees"]["include_funding"] = bool(body["funding"])
    for k in ("entry_mode", "sl_mode", "tp_mode", "entry_window_end"):
        if body.get(k):
            P[k] = str(body[k])
    for k in ("rr_target", "rr_min", "tp_rr", "be_trigger_r", "fib_level"):
        if body.get(k) not in (None, ""):
            P[k] = float(body[k])
    for k in ("confirm_closes", "max_trades", "rb_tf"):
        if body.get(k) not in (None, ""):
            P[k] = int(body[k])
    if "confirm_5m" in body:
        P["confirm_5m"] = bool(body["confirm_5m"])
    if isinstance(body.get("filters"), dict):
        P["filters"] = {k: bool(v) for k, v in body["filters"].items()}
    if isinstance(body.get("bias"), dict):
        P["bias"] = {k: bool(v) for k, v in body["bias"].items()}
    return ov


def _progress(base, span):
    def f(stage, x):
        JOB.update(stage=stage, pct=round(base + x * span, 1))
    return f


def _prepare(mkt, params, ov):
    M = core.market_cfg(cfg, mkt, copy.deepcopy(ov))
    days = int(params["days"])
    src = params.get("source") or "auto"
    feeds = core.feeds_for(M, "backtest")
    if src == "yahoo" or (feeds[0]["type"] == "dukascopy" and src == "live_feed"):
        feeds = (M["feed"], M.get("pair"))
        days = min(days, 28)
    if feeds[0]["type"] == "dukascopy":
        end = (core.now_ms() // core.DAY) * core.DAY - 1
    else:
        end = (core.now_ms() // core.MIN) * core.MIN - 1
    start = end - days * core.DAY
    warm = start - 30 * core.DAY
    con = core.db_connect()
    try:
        D = core.load_data(con, M, warm, end, src="backtest", progress=_progress(0, 70), feeds=feeds)
    finally:
        con.close()
    if D.n < 2000:
        raise RuntimeError(f"not enough data ({D.n} bars) - check the data feed / internet connection")
    return M, D, start, end, feeds, days


def _result(mkt, M, D, eng, start, end, feeds, params, t0, days):
    pos = (eng.st.get("day") or {}).get("pos")
    unreal = 0.0
    if pos:
        c = core.Costs(M)
        unreal = pos["side"] * (D.C[-1] - pos["entry"]) * pos["qty"] * c.pv - pos["fee_entry"] - pos["funding"]
    stats = core.compute_stats(eng.trades_out, M["account"]["start_balance"], start, end, M, unreal)
    curve = stats.pop("equity_curve")
    days_rec = list(eng.days_out)
    view = core.live_day_view(eng.st.get("day"))
    if view and not view.get("final"):
        days_rec.append(view)
    run_at = core.now_ms()
    return {"src": "backtest", "mkt": mkt,
            "meta": {"start_ms": start, "end_ms": end, "days": days, "params": params, "run_at": run_at,
                     "runtime_s": round(time.time() - t0, 1), "bars": D.n, "feeds": {"main": feeds[0], "pair": feeds[1]},
                     "online": True},
            "engine": "BACKTEST", "stats": stats, "equity_curve": curve, "trades": eng.trades_out, "days": days_rec,
            "today": view, "positions": [pos] if pos else [], "orders": [],
            "live": {"price": D.C[-1], "last_bar": int(D.T[-1]), "feed": feeds[0], "pair_feed": feeds[1]},
            "day_summary": core.day_summary(eng.days_out), "market": public_market(M),
            "config": core.public_config(cfg)}


def _run_backtest(mkt, params, ov):
    t0 = time.time()
    try:
        M, D, start, end, feeds, days = _prepare(mkt, params, ov)
        JOB.update(stage="simulating bar by bar", pct=72)
        eng = core.backtest(M, D, start, progress=lambda x: JOB.update(pct=round(72 + x * 25, 1)))
        res = _result(mkt, M, D, eng, start, end, feeds, params, t0, days)
        with open(bt_path(mkt), "w", encoding="utf-8") as f:
            json.dump(core.clean_json(res), f, separators=(",", ":"))
        JOB.update(stage="done", pct=100, finished=core.now_ms())
        s = res["stats"]
        log.info(f"Backtest {mkt} done: {days}d, {s['trades']} trades, net ${s['net_pnl']:+.2f}, "
                 f"{s['cum_r']:+.2f}R, {time.time() - t0:.1f}s")
    except Exception as e:
        JOB.update(stage="error", error=str(e))
        log.exception(f"backtest {mkt} failed")
    finally:
        JOB["running"] = False


def _run_lab(mkt, params, ov):
    t0 = time.time()
    try:
        M0, D, start, end, feeds, days = _prepare(mkt, params, ov)
        cal = core.load_calendar()
        rows = []
        for k, (name, var) in enumerate(VARIANTS):
            JOB.update(stage=f"variant {k + 1}/{len(VARIANTS)}: {name}", pct=round(72 + 27 * k / len(VARIANTS), 1))
            ov2 = copy.deepcopy(ov)
            core.deep_merge(ov2.setdefault("P", {}), copy.deepcopy(var))
            M = core.market_cfg(cfg, mkt, ov2)
            eng = core.backtest(M, D, start, calendar=cal)
            s = core.compute_stats(eng.trades_out, M["account"]["start_balance"], start, end, M)
            curve = s.pop("equity_curve")
            half = start + (end - start) // 2
            r1 = sum(t["r"] for t in eng.trades_out if t["exit_ts"] < half)
            r2 = sum(t["r"] for t in eng.trades_out if t["exit_ts"] >= half)
            step = max(1, len(curve) // 60)
            rows.append({"name": name, "variant": var, "trades": s["trades"], "winrate": s["winrate"],
                         "net_pnl": s["net_pnl"], "return_pct": s["return_pct"], "cum_r": s["cum_r"],
                         "profit_factor": s["profit_factor"], "max_dd_pct": s["max_dd_pct"], "max_dd_r": s["max_dd_r"],
                         "expectancy_r": s["expectancy_r"], "avg_planned_rr": s["avg_planned_rr"],
                         "fees_total": s["fees_total"], "sqn": s["sqn"], "r_first_half": r1, "r_second_half": r2,
                         "spark": [p[1] for p in curve[::step]] + [curve[-1][1]]})
        res = {"mkt": mkt, "run_at": core.now_ms(), "days": days, "params": params, "rows": rows,
               "start_ms": start, "end_ms": end, "runtime_s": round(time.time() - t0, 1)}
        with open(bt_path(mkt, "lab"), "w", encoding="utf-8") as f:
            json.dump(core.clean_json(res), f, separators=(",", ":"))
        JOB.update(stage="done", pct=100, finished=core.now_ms())
        log.info(f"Variant lab {mkt} done: {len(rows)} variants, {time.time() - t0:.1f}s")
    except Exception as e:
        JOB.update(stage="error", error=str(e))
        log.exception(f"variant lab {mkt} failed")
    finally:
        JOB["running"] = False


def start_job(kind, target):
    body = request.get_json(silent=True) or {}
    mkt = body.get("mkt") if body.get("mkt") in core.MARKETS else "btc"
    M = core.market_cfg(cfg, mkt)
    mx = cfg["backtest"]["max_days"]
    default = cfg["backtest"]["lab_days" if kind == "lab" else "default_days"]
    try:
        days = max(7, min(int(body.get("days") or default), mx))
    except (TypeError, ValueError):
        days = default
    params = dict(body, days=days, mkt=mkt)
    ov = build_overrides(body, M)
    with LOCK:
        if JOB["running"]:
            return jresp({"error": f"a {JOB['kind']} is already running", "job": JOB}, 409)
        JOB.update(running=True, kind=kind, mkt=mkt, stage="starting", pct=0, error=None, started=core.now_ms(),
                   params=params)
    threading.Thread(target=target, args=(mkt, params, ov), daemon=True, name=kind).start()
    log.info(f"{kind} started: {mkt} {days}d")
    return jresp({"job": JOB})


# ------------------------------------------------------------------ routes
@app.route("/")
def index():
    resp = send_from_directory(STATIC, "index.html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/static/<path:name>")
def static_files(name):
    resp = send_from_directory(STATIC, name)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/healthz")
def healthz():
    return {"ok": True, "version": core.VERSION}


@app.route("/api/state")
def api_state():
    mkt = mkt_arg()
    st = backtest_state(mkt) if request.args.get("src") == "backtest" else paper_state(mkt)
    if request.args.get("lite"):
        st = {k: v for k, v in st.items() if k not in ("trades", "days", "equity_curve")}
    return jresp(st)


@app.route("/api/chart")
def api_chart():
    """1m candles + pair closes + day records for a window. ?mkt&src&day=YYYY-MM-DD"""
    mkt, src = mkt_arg(), request.args.get("src", "paper")
    M = core.market_cfg(cfg, mkt)
    want = request.args.get("day")
    if src == "backtest":
        r = read_json(bt_path(mkt)) or {}
        feeds = ((r.get("meta") or {}).get("feeds") or {})
        fm, fp = feeds.get("main"), feeds.get("pair")
        days = r.get("days") or []
        trades = r.get("trades") or []
        end_ref = (r.get("meta") or {}).get("end_ms") or core.now_ms()
        if not fm:
            return jresp({"t": []})
    else:
        fm, fp = core.feeds_for(M, "paper")
        con = core.db_connect()
        try:
            days = core.load_days(con, mkt, 30)
            trades = core.load_trades(con, mkt)
            st = core.kv_get(con, f"paper_state_{mkt}") or {}
        finally:
            con.close()
        view = core.live_day_view(st.get("day"))
        if view and not (days and days[-1]["date"] == view["date"]):
            days = days + [view]
        end_ref = core.now_ms()
    if want:
        dn = core.str2dn(want)
        a = (dn - 1) * core.DAY + 12 * core.HOUR
        b = (dn + 1) * core.DAY + 6 * core.HOUR
    else:
        b = end_ref + core.HOUR
        a = b - cfg["engine"]["chart_days"] * core.DAY
    con = core.db_connect()
    try:
        sm = core.CandleStore(con, core.make_feed(fm))
        ts, o, h, l, c = sm.load(a - 6 * core.HOUR, b + 6 * core.HOUR)
        out = {"t": (ts // 1000).tolist(), "o": o.round(3).tolist(), "h": h.round(3).tolist(),
               "l": l.round(3).tolist(), "c": c.round(3).tolist()}
        if fp:
            sp = core.CandleStore(con, core.make_feed(fp))
            pts, _, _, _, pc = sp.load(a - 6 * core.HOUR, b + 6 * core.HOUR)
            out["pt"], out["pc"] = (pts // 1000).tolist(), pc.round(3).tolist()
    finally:
        con.close()
    lo_ms, hi_ms = int(ts[0]) if ts.size else a, int(ts[-1]) + core.MIN if ts.size else b
    out["days"] = [d for d in days if d.get("key_ts") and lo_ms - core.DAY <= d["key_ts"] <= hi_ms + core.DAY]
    out["trades"] = [t for t in trades if lo_ms <= t["entry_ts"] <= hi_ms]
    out["feed"], out["pair_feed"] = fm, fp
    return jresp(out)


@app.route("/api/logs")
def api_logs():
    src = "web" if request.args.get("src") == "web" else "trader"
    n = min(int(request.args.get("n", 250)), 2000)
    path = os.path.join(core.LOG_DIR, f"{src}.log")
    lines = []
    if os.path.exists(path):
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 400_000))
            lines = f.read().decode("utf-8", "replace").splitlines()[-n:]
    return jresp({"lines": lines})


@app.route("/api/job")
def api_job():
    return jresp({"job": JOB})


@app.route("/api/backtest", methods=["POST"])
def api_backtest():
    return start_job("backtest", _run_backtest)


@app.route("/api/lab", methods=["GET", "POST"])
def api_lab():
    if request.method == "GET":
        return jresp({"lab": read_json(bt_path(mkt_arg(), "lab")), "job": JOB})
    return start_job("lab", _run_lab)


def main():
    host, port = cfg["web"]["host"], int(cfg["web"]["port"])
    log.info(f"TEN·AM dashboard on http://{host}:{port}")
    try:
        from waitress import serve
        serve(app, host=host, port=port, threads=8, ident="TENAM")
    except ImportError:
        app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
