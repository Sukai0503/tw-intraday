"""分時序列（每 2 分鐘快照）與 Yahoo 日K（每日一次）。"""
import json, time, datetime as dt
from pathlib import Path
BASE = Path(__file__).resolve().parent; ST = BASE / "state"; PUB = BASE / "public"

def _sj(p, o):
    p = Path(p); tmp = p.with_suffix(".tmp"); tmp.write_text(json.dumps(o, ensure_ascii=False, separators=(",", ":"))); tmp.replace(p)

def add_point(state, tlabel, snap):
    ts = state.setdefault("ts", {"t": [], "p": {}, "y": {}})
    ts.setdefault("v", {})
    if ts["t"] and ts["t"][-1] == tlabel:
        ts["t"].pop()
        for dct in (ts["p"], ts["v"]):
            for k in dct:
                if len(dct[k]) > len(ts["t"]): dct[k].pop()
    n = len(ts["t"]); ts["t"].append(tlabel)
    for k, s in snap.items():
        if s.get("y"): ts["y"][k[4:]] = s["y"]
        arr = ts["p"].setdefault(k[4:], [])
        while len(arr) < n: arr.append(arr[-1] if arr else None)
        arr.append(s.get("p") if s.get("p") is not None else (arr[-1] if arr else None))
        va = ts["v"].setdefault(k[4:], [])
        while len(va) < n: va.append(va[-1] if va else 0)
        va.append(int(s.get("v") or (va[-1] if va else 0)))
    for dct in (ts["p"], ts["v"]):
        for arr in dct.values():
            while len(arr) < n + 1: arr.append(arr[-1] if arr else None)

def yahoo_intraday(state, keys, today):
    """用 Yahoo 5 分 K 建今日分時（晚啟動或收盤後補）。"""
    import yfinance as yf
    tick = {k: k[4:] + (".TW" if k.startswith("tse") else ".TWO") for k in keys}
    series = {}; times = set(); vser = {}
    for i in range(0, len(keys), 100):
        chunk = keys[i:i + 100]
        try:
            df = yf.download(" ".join(tick[k] for k in chunk), period="1d", interval="5m", group_by="ticker", auto_adjust=False, threads=True, progress=False)
        except Exception: continue
        for k in chunk:
            try:
                sub = df[tick[k]].dropna(subset=["Close"])
                idx = sub.index.tz_convert("Asia/Taipei")
                if sub.empty or idx[-1].strftime("%Y%m%d") != today: continue
                d = {}; dv = {}; cum = 0
                for ix, c, vv in zip(idx, sub["Close"], sub["Volume"]):
                    m = ix.hour * 60 + ix.minute + 5
                    if m > 810: m = 810
                    cum += float(vv) / 1000
                    t = f"{m // 60:02d}:{m % 60:02d}"; d[t] = round(float(c), 2); dv[t] = int(cum); times.add(t)
                series[k[4:]] = d; vser[k[4:]] = dv
            except Exception: continue
        time.sleep(1)
    tl = sorted(times)
    ts = state.setdefault("ts", {"t": [], "p": {}, "y": {}})
    old_t = ts["t"]; old_p = ts["p"]
    merged_t = sorted(set(tl) | set(old_t))
    newp = {}
    for code in set(series) | set(old_p):
        src = dict(zip(old_t, old_p.get(code, [])))
        src.update({t: v for t, v in series.get(code, {}).items() if t not in src})
        arr = []; last = None
        for t in merged_t:
            v = src.get(t); last = v if v is not None else last; arr.append(last)
        newp[code] = arr
    oldv = ts.get("v", {}); newv = {}
    for code in set(vser) | set(oldv):
        src = dict(zip(old_t, oldv.get(code, [])))
        src.update({t: v for t, v in vser.get(code, {}).items() if t not in src})
        arr = []; last = 0
        for t in merged_t:
            v = src.get(t); last = v if v is not None else last; arr.append(last)
        newv[code] = arr
    ts["t"] = merged_t; ts["p"] = newp; ts["v"] = newv

def write_intraday(state, today):
    ts = state.get("ts")
    if not ts: return
    _sj(PUB / "intraday.json", {"date": today, **ts})

def clean_ohlc(o, h, l, c):
    """台股漲跌幅 10%：高低價限制在前收 ±10.1%，且至少包住開收；離群值夾回。"""
    n = 0
    for i in range(len(c)):
        bh, bl = max(o[i], c[i]), min(o[i], c[i])
        if i > 0 and c[i - 1] > 0 and abs(c[i] / c[i - 1] - 1) < 0.105:
            pc = c[i - 1]
            nh, nl = min(h[i], pc * 1.101), max(l[i], pc * 0.899)
        else:
            nh, nl = h[i], l[i]
        nh, nl = max(nh, bh), min(nl, bl)
        if nh != h[i] or nl != l[i]: n += 1
        h[i], l[i] = round(nh, 2), round(nl, 2)
    return n

def clean_all():
    tot = 0
    for f in (PUB / "hist").glob("*.json"):
        d = json.loads(f.read_text()); n = clean_ohlc(d["o"], d["h"], d["l"], d["c"])
        if n: tot += n; _sj(f, d)
    return tot

def daily_ohlc(keys, today, force=False):
    """每檔 OHLCV → public/hist/<code>.json（每天一次）：日線近 260 日，另附 w（週K 近 260 週）、m（月K 近 120 月）。"""
    mark = ST / f".ohlc_{today}"
    if mark.exists() and not force: return False
    import yfinance as yf
    out = PUB / "hist"; out.mkdir(exist_ok=True)
    tick = {k: k[4:] + (".TW" if k.startswith("tse") else ".TWO") for k in keys}
    n = 0
    r2 = lambda s: [round(float(x), 2) for x in s]
    def pack(sub, fmt):
        o, h, l, c = r2(sub["Open"]), r2(sub["High"]), r2(sub["Low"]), r2(sub["Close"])
        clean_ohlc(o, h, l, c)
        return {"d": [ix.strftime(fmt) for ix in sub.index], "o": o, "h": h, "l": l, "c": c,
                "v": [int(float(x) / 1000) for x in sub["Volume"]]}
    def agg(sub, rule):
        g = sub.resample(rule).agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna(subset=["Close"])
        last = sub.index.to_series().resample(rule).last().dropna()
        g.index = last.loc[g.index].values if len(last) else g.index
        return g
    for i in range(0, len(keys), 100):
        chunk = keys[i:i + 100]
        try:
            df = yf.download(" ".join(tick[k] for k in chunk), period="10y", interval="1d", group_by="ticker", auto_adjust=False, threads=True, progress=False)
        except Exception: continue
        for k in chunk:
            try:
                full = df[tick[k]].dropna(subset=["Close"])
                if len(full) < 5: continue
                rec = pack(full.tail(260), "%Y%m%d")
                import pandas as pd
                w = agg(full, "W-FRI").tail(260); w.index = pd.to_datetime(w.index)
                m = agg(full, "ME").tail(120); m.index = pd.to_datetime(m.index)
                rec["w"] = pack(w, "%Y%m%d"); rec["m"] = pack(m, "%Y%m%d")
                _sj(out / f"{k[4:]}.json", rec)
                n += 1
            except Exception: continue
        time.sleep(1)
    if n > 1000: mark.touch()
    return n
