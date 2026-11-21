"""族群熱力圖資料：官方產業別（TWSE t187ap03_L / TPEx mopsfin_t187ap03_O）。"""
import json, time, datetime as dt
import themes
from pathlib import Path
BASE = Path(__file__).resolve().parent; ST = BASE / "state"; PUB = BASE / "public"
PRICES = Path("/workspace/market-signals/cache/prices.json")
IND = {"01":"水泥","02":"食品","03":"塑膠","04":"紡織纖維","05":"電機機械","06":"電器電纜","08":"玻璃陶瓷","09":"造紙",
"10":"鋼鐵","11":"橡膠","12":"汽車","14":"建材營造","15":"航運","16":"觀光餐旅","17":"金融保險","18":"貿易百貨","19":"綜合",
"20":"其他","21":"化學","22":"生技醫療","23":"油電燃氣","24":"半導體","25":"電腦及週邊","26":"光電","27":"通信網路",
"28":"電子零組件","29":"電子通路","30":"資訊服務","31":"其他電子","32":"文化創意","33":"農業科技","34":"電子商務",
"35":"綠能環保","36":"數位雲端","37":"運動休閒","38":"居家生活"}

def _lj(p, d=None):
    try: return json.loads(Path(p).read_text())
    except Exception: return d
def _sj(p, o): Path(p).write_text(json.dumps(o, ensure_ascii=False, separators=(",", ":")))

def groups(S, today):
    p = ST / f"groups2_{today}.json"
    g = _lj(p)
    if g: return g
    g = {}
    try:
        for r in S.get("https://openapi.twse.com.tw/v1/opendata/t187ap03_L", timeout=30).json():
            g["tse_" + r["公司代號"].strip()] = [IND.get(r.get("產業別", "").strip(), "其他"), r.get("公司簡稱", "").strip(), _n(r.get("已發行普通股數或TDR原股發行股數"))]
        time.sleep(2)
        for r in S.get("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O", timeout=30).json():
            g["otc_" + r["SecuritiesCompanyCode"].strip()] = [IND.get(r.get("SecuritiesIndustryCode", "").strip(), "其他"), r.get("CompanyAbbreviation", "").strip(), _n(r.get("IssueShares"))]
    except Exception:
        old = sorted(ST.glob("groups2_*.json"))
        return _lj(old[-1], {}) if old else {}
    if len(g) > 1500: _sj(p, g)
    return g

def closes(today, n=121):
    raw = _lj(PRICES, {}); out = {}
    for k, rows in raw.items():
        m, c = k.split("|", 1)
        if not (len(c) == 4 and c.isdigit() and not c.startswith("00")): continue
        rr = [(d.replace("-", ""), v) for d, v in rows if d.replace("-", "") < today][-n:]
        if len(rr) >= 11: out[("tse_" if m == "twse" else "otc_") + c] = rr
    return out

def _n(s):
    try: return float(str(s).replace(",", ""))
    except Exception: return 0.0

def inst_day(S, d):
    """回傳 {key:[外資股, 投信股]}；無資料回 None。"""
    p = ST / f"inst_{d}.json"
    c = _lj(p)
    if c: return c
    out = {}
    try:
        r = S.get(f"https://www.twse.com.tw/rwd/zh/fund/T86?date={d}&selectType=ALLBUT0999&response=json", timeout=30).json()
        if r.get("stat") != "OK": return None
        for x in r["data"]: out["tse_" + x[0].strip()] = [_n(x[4]) + _n(x[7]), _n(x[10])]
        time.sleep(2)
        roc = f"{int(d[:4]) - 1911}/{d[4:6]}/{d[6:]}"
        r = S.get(f"https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php?l=zh-tw&se=EW&t=D&d={roc}&o=json", timeout=30).json()
        t = (r.get("tables") or [{}])[0].get("data") or []
        if not t: return None
        for x in t: out["otc_" + x[0].strip()] = [_n(x[10]), _n(x[13])]
    except Exception:
        return None
    _sj(p, out); return out

_inst_try = {}
def inst3(S, today, days):
    """最近 3 個有資料的交易日（含今日若已公布）。"""
    cand = [today] + days[::-1]
    got = []
    for d in cand:
        if len(got) == 3: break
        if d == today and not (ST / f"inst_{d}.json").exists():
            if dt.datetime.now().hour < 15 or time.time() - _inst_try.get(d, 0) < 1800: continue
            _inst_try[d] = time.time()
        x = inst_day(S, d)
        if x: got.append((d, x))
        time.sleep(1)
    return got

def build(S, today, snap, vw):
    g = groups(S, today); cl = closes(today)
    days = sorted({d for rr in cl.values() for d, _ in rr})[-11:]
    ins = inst3(S, today, days[-5:])
    th = {}  # key -> [族群…]（同一檔可屬多個族群）
    _multi = getattr(themes, "themes_of", None)
    for k, s in snap.items():
        if k in g: th[k] = _multi(k[4:]) if _multi else [themes.theme(k[4:], g[k][0], s.get("p"), g[k][1])]
    for k in cl:
        if k in g and k not in th: th[k] = _multi(k[4:]) if _multi else [themes.theme(k[4:], g[k][0], None, g[k][1])]
    th = {k: [x for x in v if x] for k, v in th.items()}
    th = {k: v for k, v in th.items() if v}  # STRICT：只留參考截圖內的股票
    _all = {x for v in th.values() for x in v}
    _ord = getattr(themes, "ORDER", None)
    gname = [n for n in _ord if n in _all] if _ord else sorted(_all); gi = {n: i for i, n in enumerate(gname)}
    rows = []; agg = {}
    for k, s in snap.items():
        if k not in g or k not in cl or k not in th or s.get("p") is None: continue
        rr = cl[k]; p = s["p"]; y = s.get("y") or rr[-1][1]
        st = vw.get(k) or {}
        val = (st.get("pv", 0) * 1000 / 1e8) if st.get("cv") else s.get("v", 0) * p * 1000 / 1e8
        d1 = p / y - 1 if y else 0
        d3 = p / rr[-3][1] - 1 if len(rr) >= 3 else d1
        d10 = p / rr[-10][1] - 1 if len(rr) >= 10 else d3
        fi = sum(x[1].get(k, [0, 0])[0] for x in ins) * p / 1e8
        ti = sum(x[1].get(k, [0, 0])[1] for x in ins) * p / 1e8
        cap = (g[k][2] if len(g[k]) > 2 else 0) * p
        hi = _n(s.get("h")) or p; lo = _n(s.get("l")) or p
        for grp in th[k]:
            rows.append({"k": k, "cap": cap, "r": [k[4:], (g[k][1] or s.get("n", "")).rstrip("*"), gi[grp], round(val, 2), round(d1 * 100, 2), round(d3 * 100, 2),
                         round(d10 * 100, 2), k[:3], p, round(p - y, 2) if y else 0, hi, lo, g[k][0], 0, _n(s.get("o")) or p, int(s.get("v", 0))]})
            a = agg.setdefault(grp, {"n": 0, "val": 0, "d1": 0, "d3": 0, "d10": 0, "f": 0, "t": 0})
            a["n"] += 1; a["val"] += val; a["d1"] += d1; a["d3"] += d3; a["d10"] += d10; a["f"] += fi; a["t"] += ti
    # 龍頭：族群內市值最大（≥20 檔的族群取前 2）
    byg = {}
    for x in rows: byg.setdefault(x["r"][2], []).append(x)
    for lst in byg.values():
        lst.sort(key=lambda x: -(x["cap"] or x["r"][3] * 1e9))
        for x in lst[: 2 if len(lst) >= 20 else 1]: x["r"][13] = 1
    stocks = [x["r"] for x in rows]
    table = []
    for n, a in agg.items():
        c = a["n"]
        table.append({"g": n, "n": c, "val": round(a["val"], 1), "d1": round(a["d1"] / c * 100, 2), "d3": round(a["d3"] / c * 100, 2),
                      "d10": round(a["d10"] / c * 100, 2), "f3": round(a["f"], 1) if ins else None, "t3": round(a["t"], 1) if ins else None})
    table.sort(key=lambda r: -r["d1"])
    for i, r in enumerate(table): r["rank"] = i + 1
    rot_days = days[-9:] + [today]
    rot = {n: [] for n in agg}
    idx = {k: {d: i for i, (d, _) in enumerate(rr)} for k, rr in cl.items()}
    for d in rot_days:
        sums = {}
        for k, rr in cl.items():
            grps = [x for x in th.get(k, []) if x in agg]
            if not grps: continue
            if d == today:
                s = snap.get(k)
                if not s or s.get("p") is None or not s.get("y"): continue
                r1 = s["p"] / s["y"] - 1
            else:
                i = idx[k].get(d, -1)
                if i < 1: continue
                r1 = rr[i][1] / rr[i - 1][1] - 1
            for grp in grps:
                sm = sums.setdefault(grp, [0, 0]); sm[0] += r1; sm[1] += 1
        order = sorted(sums, key=lambda n: -sums[n][0] / sums[n][1])
        rk = {n: i + 1 for i, n in enumerate(order)}
        for n in rot: rot[n].append(rk.get(n))
    return {"groups": gname, "stocks": stocks, "table": table, "rot_days": rot_days, "rot": rot,
            "inst_days": [d for d, _ in ins], "ng": len(agg), "asof": time.strftime("%H:%M:%S"), "n_all": len(snap),
            "fields": "code,name,gi,val億,d1,d3,d10,mkt,price,chg,high,low,sub,leader,open,vol張"}

def write_hist(today):
    """點個股用的近 120 日收盤（每天一次）。"""
    p = PUB / "hist.json"
    old = _lj(p, {})
    if old.get("asof") == today: return False
    cl = closes(today, 120)
    days = sorted({d for rr in cl.values() for d, _ in rr})[-120:]
    h = {"asof": today, "days": days, "c": {}}
    for k, rr in cl.items():
        dd = dict(rr); h["c"][k[4:]] = [dd.get(d) for d in days]
    _sj(p, h); return True
