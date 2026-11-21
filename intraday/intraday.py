#!/usr/bin/env python3
"""今日盤中｜自動更新（不經 LLM）
- 累計成交金額：mis.twse.com.tw mis_ohlc_TSE.txt（每分鐘成交金額，百萬元）→ 今日累計；
  前一交易日：TWSE MI_5MINS（5 秒累計成交金額）→ 每分鐘。
- 盤中廣度：mis getStockInfo（150 檔/次，批次）全上市櫃普通股；
  20MA = 前 19 日收盤（/workspace/market-signals/cache/prices.json）+ 即時價；
  均價線 = 以快照間成交量增量 × 區間均價估算的當日 VWAP（開盤前沒啟動時用 Yahoo 5 分 K 回補）。
"""
import json, os, sys, time, math, datetime as dt, subprocess, traceback
from pathlib import Path
import requests

BASE = Path(__file__).resolve().parent
PUB = BASE / "public"; ST = BASE / "state"
PRICES = Path("/workspace/market-signals/cache/prices.json")
UA = {"User-Agent": "Mozilla/5.0 (intraday-dashboard; personal use; low-rate)"}
S = requests.Session(); S.headers.update(UA)
BATCH = 100; BATCH_GAP = 0.3; WORKERS = 2; SNAP_DEADLINE = 40; BREADTH_EVERY = 45; VALUE_EVERY = 60
FRESH_MIN = 0.30; COVER_MIN = 0.90   # 廣度：本輪新報價 ≥30% 且（新報價＋沿用上一輪）≥90% 才記點
import threading
from concurrent.futures import ThreadPoolExecutor
_TL = threading.local()
def _sess():
    s = getattr(_TL, "s", None)
    if s is None: s = _TL.s = requests.Session(); s.headers.update(UA)
    return s

def now(): return dt.datetime.now()
def log(*a):
    with open(BASE / "intraday.log", "a") as f: print(now().strftime("%F %T"), *a, file=f)

def jget(url, timeout=20):
    r = S.get(url, timeout=timeout); r.raise_for_status()
    return json.loads(r.text.strip())

def load_json(p, d=None):
    try: return json.loads(Path(p).read_text())
    except Exception: return d
def save_json(p, obj):
    p = Path(p); tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":"))); tmp.replace(p)

# ---------- 成交金額 ----------
def today_value_curve():
    d = jget("https://mis.twse.com.tw/stock/data/mis_ohlc_TSE.txt")
    key = d.get("staticObj", {}).get("key", "")  # tse_YYYYMMDD
    date = key.split("_")[-1]
    cum = 0.0; pts = []
    for x in d.get("ohlcArray", []):
        cum += float(x["s"]) / 100.0  # 百萬 → 億
        ts = x["ts"]; tl = min(ts[:2] + ":" + ts[2:4], "13:30")
        if pts and pts[-1][0] == tl: pts[-1] = [tl, round(cum, 2), float(x["c"])]
        else: pts.append([tl, round(cum, 2), float(x["c"])])
    tz = float(d["staticObj"].get("tz", 0)) / 1e8
    return date, pts, tz

def prev_value_curve(prev_date):
    p = ST / f"mi5_{prev_date}.json"
    c = load_json(p)
    if c: return c
    d = jget(f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_5MINS?date={prev_date}&response=json")
    if d.get("stat") != "OK": return None
    out = {}
    for row in d["data"]:
        t = row[0][:5]; v = float(row[7].replace(",", "")) / 100.0
        out[t] = round(v, 2)  # 最後一筆=該分鐘末
    pts = [[k, v] for k, v in sorted(out.items())]
    save_json(p, pts); return pts

def prev_trading_date(today):
    """往前找 MI_5MINS 有資料的最近交易日（成功即快取）。"""
    d = dt.datetime.strptime(today, "%Y%m%d").date()
    for i in range(1, 12):
        x = d - dt.timedelta(days=i)
        if x.weekday() >= 5: continue
        ds = x.strftime("%Y%m%d")
        if (ST / f"mi5_{ds}.json").exists(): return ds
        try:
            if prev_value_curve(ds): return ds
        except Exception as e:
            log("prev probe fail", ds, repr(e)[:120])
        time.sleep(3)
    return None

# ---------- 廣度 ----------
def universe(today):
    raw = load_json(PRICES, {})
    uni = {}
    for k, rows in raw.items():
        m, code = k.split("|", 1)
        if not (len(code) == 4 and code.isdigit() and not code.startswith("00")): continue
        closes = [v for d, v in rows if d.replace("-", "") < today]
        if len(closes) < 19: continue
        uni[("tse" if m == "twse" else "otc") + "_" + code] = sum(closes[-19:])
    return uni

def pick_price(x):
    for v in (x.get("z"), (x.get("trade") or {}).get("z")):
        try:
            f = float(v)
            if f > 0: return f
        except Exception: pass
    try:
        a = float(x["a"].split("_")[0]); b = float(x["b"].split("_")[0])
        if a > 0 and b > 0: return (a + b) / 2
    except Exception: pass
    return None

def _snap_batch(chunk, t_start):
    """單批 getStockInfo；失敗重試一次（拆半），逾總時限即放棄。"""
    out = {}
    def once(ks, to):
        ch = "|".join(f"{k}.tw" for k in ks)
        r = _sess().get(f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={ch}&json=1&delay=0", timeout=to)
        r.raise_for_status(); d = json.loads(r.text.strip())
        for x in d.get("msgArray", []):
            p = pick_price(x)
            try: v = float(x.get("v", 0))
            except Exception: v = 0.0
            out[f"{x.get('ex')}_{x.get('c')}"] = {"p": p, "v": v, "o": x.get("o"), "h": x.get("h"), "l": x.get("l"), "y": fnum(x.get("y")), "n": x.get("n", "")}
    try:
        once(chunk, (4, 8))
    except Exception as e:
        log("snapshot batch fail", chunk[0], repr(e)[:120])
        for half in (chunk[:len(chunk) // 2], chunk[len(chunk) // 2:]):
            if time.time() - t_start > SNAP_DEADLINE: break
            time.sleep(1)
            try: once(half, (4, 8))
            except Exception as e2: log("snapshot retry fail", half[0], repr(e2)[:120])
    time.sleep(BATCH_GAP)
    return out

def snapshot(keys):
    out = {}; t_start = time.time()
    chunks = [keys[i:i + BATCH] for i in range(0, len(keys), BATCH)]
    def job(ch):
        if time.time() - t_start > SNAP_DEADLINE: return {}
        return _snap_batch(ch, t_start)
    with ThreadPoolExecutor(WORKERS) as ex:
        for r in ex.map(job, chunks): out.update(r)
    if len(out) < len(keys): log("snapshot partial", len(out), "/", len(keys), f"{time.time() - t_start:.0f}s")
    return out

def fnum(s):
    try:
        f = float(s); return f if f > 0 else None
    except Exception: return None

def update_vwap(state, snap):
    vw = state.setdefault("vw", {})
    for k, s in snap.items():
        p, v = s["p"], s["v"]
        if p is None or v <= 0: continue
        st = vw.get(k)
        if st is None:
            o, h, l = fnum(s["o"]), fnum(s["h"]), fnum(s["l"])
            tp = (o + h + l + p) / 4 if (o and h and l) else p
            vw[k] = {"pv": tp * v, "cv": v, "lv": v, "lp": p}
            continue
        dv = v - st["lv"]
        if dv > 0:
            st["pv"] += dv * (p + st["lp"]) / 2; st["cv"] += dv
        st["lv"] = v; st["lp"] = p

def breadth(state, snap, uni, carry=None):
    """carry：上一輪已知報價（本輪沒抓到的股票沿用）。"""
    n = a20 = av = 0
    for k, s19 in uni.items():
        s = snap.get(k)
        if (not s or s.get("p") is None) and carry: s = carry.get(k)
        if not s or s["p"] is None: continue
        p = s["p"]; n += 1
        if p > (s19 + p) / 20: a20 += 1
        st = state.get("vw", {}).get(k)
        if st and st["cv"] > 0 and p > st["pv"] / st["cv"]: av += 1
    if n == 0: return None
    return {"n": n, "ma20": round(100 * a20 / n, 1), "vwap": round(100 * av / n, 1)}

def yahoo_backfill(state, uni, today, interval="1m", merge=False, set_vw=True):
    """啟動時已開盤：用 Yahoo 5 分 K 回補今日廣度曲線與 VWAP 狀態（Yahoo 約延遲 20 分）。"""
    try:
        import yfinance as yf, pandas as pd
    except ImportError:
        log("no yfinance"); return
    keys = list(uni); tick = {k: k[4:] + (".TW" if k.startswith("tse") else ".TWO") for k in keys}
    bars = {}
    for i in range(0, len(keys), 100):
        chunk = keys[i:i + 100]
        try:
            df = yf.download(" ".join(tick[k] for k in chunk), period="1d", interval=interval, group_by="ticker",
                             auto_adjust=False, threads=True, progress=False)
        except Exception as e:
            log("yf fail", repr(e)[:200]); continue
        for k in chunk:
            t = tick[k]
            try:
                sub = df[t].dropna(subset=["Close"])
                if sub.empty: continue
                idx = sub.index.tz_convert("Asia/Taipei") if sub.index.tz is not None else sub.index
                if idx[-1].strftime("%Y%m%d") != today: continue
                bars[k] = [(ix.strftime("%H:%M"), float(r.High), float(r.Low), float(r.Close), float(r.Volume))
                           for ix, r in zip(idx, sub.itertuples())]
            except Exception: continue
        time.sleep(1)
    if not bars: return None
    step = 1 if interval == "1m" else 5
    times = sorted({b[0] for v in bars.values() for b in v})
    series = []
    cum = {k: [0.0, 0.0, None] for k in bars}
    ptr = {k: 0 for k in bars}
    for t in times:
        n = a20 = av = 0
        for k, bl in bars.items():
            c = cum[k]
            while ptr[k] < len(bl) and bl[ptr[k]][0] <= t:
                _, h, l, cl, vol = bl[ptr[k]]; c[0] += vol * (h + l + cl) / 3; c[1] += vol; c[2] = cl; ptr[k] += 1
            if c[2] is None: continue
            p = c[2]; n += 1
            if p > (uni[k] + p) / 20: a20 += 1
            if c[1] > 0 and p > c[0] / c[1]: av += 1
        if n:
            # K 棒標籤是起始時間，換成結束時間
            hh, mm = map(int, t.split(":")); m2 = hh * 60 + mm + step
            series.append([f"{m2 // 60:02d}:{m2 % 60:02d}", round(100 * a20 / n, 1), round(100 * av / n, 1), n])
    series = [s for s in series if "09:02" <= s[0] <= "13:30"]  # 09:01 只有一根 K，均價線失真
    if merge: merge_breadth(state, series)
    else: state["breadth"] = series
    if set_vw:
        vw = state.setdefault("vw", {})
        for k, c in cum.items():
            if c[1] > 0: vw[k] = {"pv": c[0] / 1000.0, "cv": c[1] / 1000.0, "lv": None, "lp": c[2], "yb": 1}
    state["backfill"] = f"缺漏分鐘以 Yahoo {step} 分 K 回補（至 {series[-1][0] if series else '-'}）"
    log("backfill", interval, len(bars), state["backfill"])
    return series

def merge_breadth(state, series):
    """即時點優先，Yahoo 回補只填空缺分鐘。"""
    live = {x[0]: x for x in state.get("breadth", [])}
    for x in series:
        if x[0] not in live: live[x[0]] = x
    state["breadth"] = [live[t] for t in sorted(live)]

def breadth_gaps(state, upto):
    ts = [tm_(x[0]) for x in state.get("breadth", []) if x[0] <= upto]
    prev = 9 * 60
    for t in ts + [tm_(upto)]:
        if t - prev > 2: return True
        prev = t
    return False

def tm_(s): h, m = s.split(":"); return int(h) * 60 + int(m)

# ---------- 台幣 ----------
def fx_twd(today):
    """USD/TWD（Yahoo Finance TWD=X，1 分 K；前收＝前一日日線收盤）。"""
    import yfinance as yf
    t = yf.Ticker("TWD=X")
    df = t.history(period="1d", interval="1m")
    pts = []
    if not df.empty:
        idx = df.index.tz_convert("Asia/Taipei")
        for ix, c in zip(idx, df["Close"]):
            if ix.strftime("%Y%m%d") != today or c != c: continue
            pts.append([ix.strftime("%H:%M"), round(float(c), 4)])
    dd = t.history(period="7d", interval="1d")
    prev = None
    if not dd.empty:
        di = dd.index.tz_convert("Asia/Taipei")
        older = [float(c) for ix, c in zip(di, dd["Close"]) if ix.strftime("%Y%m%d") < today and c == c]
        prev = round(older[-1], 4) if older else None
    if not pts: return None
    cur = pts[-1][1]
    return {"pts": pts, "prev": prev, "cur": cur, "t": pts[-1][0],
            "chg": round(cur - prev, 4) if prev else None,
            "pct": round((cur / prev - 1) * 100, 2) if prev else None, "src": "Yahoo Finance TWD=X"}

def fix_lv(state, snap):
    """Yahoo 回補後第一筆 mis 快照：用 mis 量差補上 Yahoo 延遲那段。"""
    for k, st in state.get("vw", {}).items():
        if st.get("lv") is None and k in snap and snap[k]["p"] is not None:
            v = snap[k]["v"]; p = snap[k]["p"]
            dv = v - st["cv"]
            if dv > 0: st["pv"] += dv * (p + st["lp"]) / 2; st["cv"] += dv
            st["lv"] = v; st["lp"] = p

def do_heat(state, today, snap):
    try:
        import series
        tl = min(now(), now().replace(hour=13, minute=30)).strftime("%H:%M")
        if hm() >= 9 * 60: series.add_point(state, tl, snap)
        series.write_intraday(state, today)
    except Exception as e:
        log("series err", repr(e)[:300])
    try:
        import heat
        qc = state.setdefault("qc", {})
        qc.update({k: v for k, v in (snap or {}).items() if isinstance(v, dict) and v.get("p") is not None})
        merged = {**qc, **{k: v for k, v in (snap or {}).items() if isinstance(v, dict) and v.get("p") is not None}}
        if merged:
            state["heat"] = heat.build(S, today, merged, state.get("vw", {}))
        heat.write_hist(today)
    except Exception as e:
        log("heat err", repr(e)[:300])

# ---------- 輸出 ----------

def attach_ma_arr(state, today):
    """Attach 多空頭排列比例 from MIS last quotes + cached prior closes. Never raise."""
    try:
        import sys
        sig = "/workspace/market-signals"
        if sig not in sys.path:
            sys.path.insert(0, sig)
        import ma_arrangement
        qc = state.get("qc") or {}
        if not qc:
            return None
        at = now().strftime("%H:%M")
        # after close, label the last known snapshot time if we have one
        ts = state.get("ts") or {}
        if isinstance(ts, dict) and ts.get("t"):
            last_t = ts["t"][-1]
            if hm() >= 13 * 60 + 30:
                at = last_t
        payload = ma_arrangement.live_from_qc(qc, today, at=at)
        rec = ma_arrangement.hist_rec(at, payload.get("markets") or {})
        hist = list(state.get("ma_arr_hist") or [])
        if not hist or hist[-1][0] != at:
            hist.append(rec)
        state["ma_arr_hist"] = hist
        payload["hist"] = hist
        state["ma_arr"] = payload
        return payload
    except Exception as e:
        log("ma_arr fail", repr(e)[:200])
        return state.get("ma_arr")

def write_out(state, today, pts, prev_pts, tz, force=False):
    prev_map = dict((t, v) for t, v in (prev_pts or []))
    cur_t = pts[-1][0] if pts else None
    cur_v = pts[-1][1] if pts else None
    pv = prev_map.get(cur_t) if cur_t else None
    if pv is None and cur_t and prev_pts:
        earlier = [v for t, v in prev_pts if t <= cur_t]; pv = earlier[-1] if earlier else None
    data = {
        "date": today, "prev_date": state.get("prev_date"),
        "updated": now().strftime("%H:%M"), "updated_full": now().strftime("%Y-%m-%d %H:%M:%S"),
        "value": {"today": [[t, v] for t, v, _ in pts], "prev": prev_pts or [],
                  "cur": cur_v, "cur_t": cur_t, "tz": round(tz, 1),
                  "vs_prev_pct": round((cur_v / pv - 1) * 100, 1) if (cur_v and pv) else None},
        "breadth": state.get("breadth", []), "universe": state.get("uni_n"),
        "notes": state.get("backfill"),
    }
    if state.get("twd"): data["twd"] = state["twd"]
    if state.get("heat"):
        data["heat"] = state["heat"]
        save_json(PUB / "data.json", data)
        if hm() >= 13 * 60 + 30 or force:
            save_json(PUB / "heat_close.json", {"date": today, **state["heat"]})
    try:
        ma = attach_ma_arr(state, today)
        if ma:
            data["ma_arr"] = ma
            save_json(ST / f"day_{today}.json", state)
    except Exception as e:
        log("ma_arr attach fail", repr(e)[:200])
    save_json(PUB / "data.json", data)
    html = (BASE / "template.html").read_text().replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False))
    (PUB / "index.html.tmp").write_text(html); (PUB / "index.html.tmp").replace(PUB / "index.html")
    pub = BASE / "publish.sh"
    global _PUBP
    if pub.exists():
        if force:
            try: subprocess.run(["bash", str(pub)], timeout=120, capture_output=True, env={**os.environ, "FORCE": "1"})
            except Exception as e: log("publish fail", repr(e)[:200])
        else:
            # 盤中不阻塞主迴圈：上一次發布還沒結束就跳過
            if _PUBP is not None and _PUBP.poll() is None:
                if time.time() - _PUBP_T > 150: _PUBP.kill(); log("publish killed (stuck)")
                return
            _PUBP = subprocess.Popen(["bash", str(pub)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=dict(os.environ))
            globals()["_PUBP_T"] = time.time()

_PUBP = None; _PUBP_T = 0
# ---------- 主流程 ----------
def hm(t=None): t = t or now(); return t.hour * 60 + t.minute

def run_day():
    today = now().strftime("%Y%m%d")
    sp = ST / f"day_{today}.json"
    state = load_json(sp, {}) or {}
    uni = universe(today); state["uni_n"] = len(uni)
    prev = state.get("prev_date") or prev_trading_date(today); state["prev_date"] = prev
    if not os.environ.get("GHA"):  # GitHub Actions 版不跑（盤後序列仍由 box 產生）
        try:
            subprocess.run([sys.executable, str(BASE / "signals_series.py")], timeout=300, capture_output=True)
        except Exception as e: log("signals_series err", repr(e)[:200])
    def _ohlc():
        try:
            import series; series.daily_ohlc(list(uni), prev or today)
        except Exception as e: log("ohlc err", repr(e)[:200])
    if os.environ.get("GHA"): threading.Thread(target=_ohlc, daemon=True).start()  # 不擋盤中迴圈
    else: _ohlc()
    prev_pts = None
    last_vc = None
    last_b = 0; first_snap = True; last_gap = 0; gap_th = None; gap_res = {}; did_bf = False
    while hm() <= 13 * 60 + 36:
        t0 = time.time()
        try:
            try:
                date, pts, tz = today_value_curve(); last_vc = (date, pts, tz); state["vc"] = [date, pts, tz]
            except Exception as e:
                if not last_vc and (state.get("vc") or [None])[0] == today: last_vc = tuple(state["vc"])
                if not last_vc: raise
                log("value curve fail, reuse last", repr(e)[:120]); date, pts, tz = last_vc
            if date != today:
                if hm() > 9 * 60 + 15: log("not a trading day", date); return
                time.sleep(60); continue
            if prev_pts is None and prev:
                try: prev_pts = prev_value_curve(prev)
                except Exception as e: log("prev fail", repr(e)[:150])
            try:
                tw = fx_twd(today)
                if tw: state["twd"] = tw
            except Exception as e: log("twd err", repr(e)[:150])
            if hm() >= 9 * 60 and time.time() - last_b >= BREADTH_EVERY:
                if not did_bf and hm() > 9 * 60 + 10:
                    did_bf = True
                    yahoo_backfill(state, uni, today, "1m", merge=True, set_vw=not state.get("vw"))
                    if not state.get("ts"):
                        try:
                            import series; series.yahoo_intraday(state, list(uni), today)
                        except Exception as e: log("yintra err", repr(e)[:200])
                    last_gap = time.time()
                # 每 10 分鐘：若廣度有缺漏分鐘，背景用 Yahoo 1 分 K 回補（Yahoo 約延遲 20 分）
                if gap_res.get("series"): merge_breadth(state, gap_res.pop("series")); state["backfill"] = "缺漏分鐘以 Yahoo 1 分 K 回補"
                if time.time() - last_gap > 600 and (gap_th is None or not gap_th.is_alive()):
                    upto = (now() - dt.timedelta(minutes=22)).strftime("%H:%M")
                    if upto > "09:05" and breadth_gaps(state, min(upto, "13:30")):
                        last_gap = time.time()
                        def _gap():
                            tmp = {"breadth": []}
                            gap_res["series"] = yahoo_backfill(tmp, uni, today, "1m", merge=False, set_vw=False)
                        gap_th = threading.Thread(target=_gap, daemon=True); gap_th.start()
                last_b = time.time(); _t1 = time.time()
                snap = snapshot(list(uni)); _t2 = time.time()
                if first_snap: fix_lv(state, snap); first_snap = False
                update_vwap(state, snap)
                carry = dict(state.get("qc") or {})
                fresh = sum(1 for k in uni if k in snap and snap[k].get("p") is not None)
                b = breadth(state, snap, uni, carry)
                do_heat(state, today, snap); _t3 = time.time()
                if b and (fresh < FRESH_MIN * len(uni) or b["n"] < COVER_MIN * len(uni)):
                    log("low coverage, skip breadth", fresh, b["n"]); b = None
                if b:
                    b["fresh"] = fresh
                    tl = min(now(), now().replace(hour=13, minute=30)).strftime("%H:%M")
                    br = [x for x in state.get("breadth", []) if x[0] < tl]
                    br.append([tl, b["ma20"], b["vwap"], b["n"]]); state["breadth"] = br
                save_json(sp, state)
                try: date, pts, tz = today_value_curve(); last_vc = (date, pts, tz)
                except Exception as e: log("value curve fail", repr(e)[:120])
                write_out(state, today, pts, prev_pts, tz)
                log(f"timing pre {_t1 - t0:.0f}s snap {_t2 - _t1:.0f}s heat {_t3 - _t2:.0f}s out {time.time() - _t3:.0f}s fresh {fresh}")
            else:
                write_out(state, today, pts, prev_pts, tz)
        except Exception as e:
            log("loop err", repr(e)[:300])
        time.sleep(max(5, VALUE_EVERY - (time.time() - t0)))
    # 收盤後：收盤價確認一次；廣度缺漏分鐘用 Yahoo 1 分 K 補齊
    try:
        if state.get("breadth"): yahoo_backfill(state, uni, today, "1m", merge=True, set_vw=False)
    except Exception as e: log("close gap fill err", repr(e)[:150])
    try:
        date, pts, tz = today_value_curve()
        snap = snapshot(list(uni)); update_vwap(state, snap); b = breadth(state, snap, uni, state.get("qc")); do_heat(state, today, snap)
        if b:
            br = [x for x in state.get("breadth", []) if x[0] < "13:30"]; br.append(["13:30", b["ma20"], b["vwap"], b["n"]]); state["breadth"] = br
        save_json(sp, state); write_out(state, today, pts, prev_pts, tz, force=True)
    except Exception as e: log("close err", repr(e)[:200])

def close_backfill():
    """收盤後補整天：Yahoo 5 分 K 重建廣度 + 最後一次 mis 快照，FORCE 發布。"""
    today = now().strftime("%Y%m%d"); sp = ST / f"day_{today}.json"
    uni = universe(today); prev = prev_trading_date(today)
    state = {"uni_n": len(uni), "prev_date": prev}
    date, pts, tz = today_value_curve()
    if date != today: log("close_backfill: not trading day", date); return
    prev_pts = prev_value_curve(prev) if prev else None
    yahoo_backfill(state, uni, today)
    snap = snapshot(list(uni)); fix_lv(state, snap); update_vwap(state, snap)
    b = breadth(state, snap, uni); do_heat(state, today, snap)
    if b:
        br = [x for x in state.get("breadth", []) if x[0] < "13:30"]; br.append(["13:30", b["ma20"], b["vwap"], b["n"]]); state["breadth"] = br
    state["backfill"] = "收盤後以 Yahoo 5 分 K 回補全日，13:30 為證交所收盤快照"
    save_json(sp, state); write_out(state, today, pts, prev_pts, tz, force=True)

def main():
    if "--close" in sys.argv: close_backfill(); return
    once = "--once" in sys.argv
    if "--gha" in sys.argv:  # GitHub Actions：等到開盤窗→跑一整天→結束；過了 13:36 或週末直接結束
        while True:
            n = now()
            if n.weekday() >= 5 or hm(n) > 13 * 60 + 36: log("gha: outside window, exit"); return
            if hm(n) >= 8 * 60 + 55:
                log("run_day start (gha)"); run_day(); log("run_day end (gha)"); return
            time.sleep(20)
    done = None
    while True:
        n = now()
        if n.weekday() < 5 and 8 * 60 + 55 <= hm(n) <= 13 * 60 + 36 and done != n.date():
            log("run_day start"); run_day(); log("run_day end"); done = n.date()
            if once: return
        elif once:
            return
        time.sleep(30)

if __name__ == "__main__":
    main()
