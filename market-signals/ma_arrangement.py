#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compute 多空頭排列家數比例 from official TWSE / TPEx daily closes.

Short MA arrangement: MA5 / MA10 / MA20
Long MA arrangement:  MA10 / MA20 / MA60
Bull = short > mid > long; bear = the reverse.
Ratio = count / stocks with enough history, for 上市櫃 / 上市 / 上櫃.

This is an observation item only (no fixed threshold).

Usage:
  /workspace/market-signals/.venv/bin/python /workspace/market-signals/ma_arrangement.py
  Options: --date YYYY-MM-DD  --history 20  --backfill 80  --no-fetch
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import fetch

DIR = Path(__file__).resolve().parent
OUT = DIR / "out" / "ma_arrangement.json"
DAY_CACHE = DIR / "cache" / "daily_closes"
LIST_CACHE = DIR / "cache" / "company_lists.json"
PRIOR_PATH = DIR / "cache" / "ma_prior.json"
_SERIES_CACHE: tuple[str, dict] | None = None
TPE = timezone(timedelta(hours=8))
SLEEP = 0.9
SHORT = (5, 10, 20)
LONG = (10, 20, 60)
MARKETS = ("all", "twse", "tpex")
MKT_NAME = {"all": "上市櫃", "twse": "上市", "tpex": "上櫃"}


def log(*a):
    print("[ma_arrangement]", *a, file=sys.stderr)


def now_tpe() -> datetime:
    return datetime.now(TPE)


def day_path(d: date) -> Path:
    DAY_CACHE.mkdir(parents=True, exist_ok=True)
    return DAY_CACHE / f"{d.isoformat()}.json"


def load_day(d: date) -> Optional[dict[str, dict[str, float]]]:
    p = day_path(d)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    out: dict[str, dict[str, float]] = {"twse": {}, "tpex": {}}
    for mkt in ("twse", "tpex"):
        src = raw.get(mkt) or {}
        if isinstance(src, dict):
            for code, px in src.items():
                if str(code).startswith("_"):
                    continue
                v = fetch.parse_num(px)
                if v is not None:
                    out[mkt][str(code).strip()] = v
    if not out["twse"] and not out["tpex"]:
        return None
    return out


def save_day(d: date, data: dict[str, dict[str, float]]) -> None:
    blob = {
        "date": d.isoformat(),
        "twse": {c: data.get("twse", {}).get(c) for c in sorted(data.get("twse") or {})},
        "tpex": {c: data.get("tpex", {}).get(c) for c in sorted(data.get("tpex") or {})},
    }
    # drop Nones
    for mkt in ("twse", "tpex"):
        blob[mkt] = {c: v for c, v in blob[mkt].items() if v is not None}
    p = day_path(d)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(blob, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(p)


def is_blocked(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(s in msg for s in ("403", "429", "451", "waf", "blocked", "forbidden", "too many requests"))


def clean_closes(raw: dict) -> dict[str, float]:
    out: dict[str, float] = {}
    for code, px in (raw or {}).items():
        if str(code).startswith("_"):
            continue
        v = px if isinstance(px, (int, float)) else fetch.parse_num(px)
        if v is not None:
            out[str(code).strip()] = float(v)
    return out


def fetch_official_day(d: date) -> dict[str, dict[str, float]]:
    """Fetch one day's official closes. Raises on failure. Caller sleeps between markets."""
    twse = clean_closes(fetch._twse_closes(d))
    time.sleep(SLEEP)
    tpex = clean_closes(fetch._tpex_closes(d))
    if not twse and not tpex:
        raise RuntimeError(f"no closes for {d.isoformat()}")
    return {"twse": twse, "tpex": tpex}


def seed_from_prices(needed: list[date]) -> int:
    """Fill missing daily-close cache files from prices.json. Returns days written."""
    missing = [d for d in needed if load_day(d) is None]
    if not missing:
        return 0
    log(f"seeding {len(missing)} day file(s) from prices.json")
    cache = fetch.load_price_cache()
    want = {d for d in missing}
    bucket: dict[date, dict[str, dict[str, float]]] = {d: {"twse": {}, "tpex": {}} for d in missing}
    for (mkt, code), rows in cache.items():
        if mkt not in ("twse", "tpex"):
            continue
        for dt, px in rows:
            if dt in want and px is not None:
                bucket[dt][mkt][code] = float(px)
    written = 0
    for d in missing:
        data = bucket[d]
        if data["twse"] or data["tpex"]:
            save_day(d, data)
            written += 1
    log(f"seeded {written} day file(s)")
    return written


def trading_calendar(end: date, backfill: int) -> list[date]:
    """Official TWSE days through the last FMTQIK date, plus `end` if it is later."""
    months = max(6, (backfill // 15) + 2)
    days = fetch.trading_days_upto(end, months_back=months)
    if not days:
        # fallback: weekdays ending at end
        d = end
        out: list[date] = []
        while len(out) < backfill:
            if d.weekday() < 5:
                out.append(d)
            d -= timedelta(days=1)
        return sorted(out)[-backfill:]
    if end not in days and end > days[-1]:
        days.append(end)
    return [d for d in days if d <= end]


def discover_trade_date(forced: Optional[date] = None, no_fetch: bool = False) -> date:
    if forced:
        return forced
    today = fetch.today_tpe()
    if no_fetch:
        for i in range(14):
            d = today - timedelta(days=i)
            if load_day(d):
                return d
        cal = trading_calendar(today, 20)
        return cal[-1] if cal else today
    last_err: Optional[Exception] = None
    for i in range(7):
        d = today - timedelta(days=i)
        cached = load_day(d)
        if cached and cached.get("twse") and cached.get("tpex") and i == 0:
            return d
        try:
            data = fetch_official_day(d)
            save_day(d, data)
            log(f"official closes {d.isoformat()} twse={len(data['twse'])} tpex={len(data['tpex'])}")
            time.sleep(SLEEP)
            return d
        except Exception as e:
            last_err = e
            if is_blocked(e):
                raise RuntimeError(f"blocked fetching official closes for {d.isoformat()}: {e}") from e
            log(f"no official closes for {d.isoformat()}: {e}")
            time.sleep(SLEEP)
    if last_err:
        log(f"falling back; last error: {last_err}")
    cal = trading_calendar(today, 20)
    return cal[-1] if cal else today


def load_universe() -> dict[str, set[str]]:
    cached = fetch.cache_load_json(LIST_CACHE) or {}
    if (
        isinstance(cached, dict)
        and cached.get("twse")
        and cached.get("tpex")
        and cached.get("as_of") == fetch.today_tpe().isoformat()
    ):
        return {"twse": set(cached["twse"]), "tpex": set(cached["tpex"])}
    log("loading official company lists")
    try:
        raw = fetch.load_company_lists()
        codes = raw.get("codes") or raw
        lists = {
            "twse": {c for c in (codes.get("twse") or set()) if fetch.is_common_stock_code(c)},
            "tpex": {c for c in (codes.get("tpex") or set()) if fetch.is_common_stock_code(c)},
        }
    except Exception as e:
        log(f"company lists failed: {e}")
        lists = {"twse": set(), "tpex": set()}
    if lists["twse"] and lists["tpex"]:
        fetch.cache_save_json(
            LIST_CACHE,
            {
                "as_of": fetch.today_tpe().isoformat(),
                "twse": sorted(lists["twse"]),
                "tpex": sorted(lists["tpex"]),
            },
        )
        log(f"company lists twse={len(lists['twse'])} tpex={len(lists['tpex'])}")
        return lists
    return lists


def fill_missing_days(needed: list[date], no_fetch: bool) -> list[date]:
    """Ensure cache has each needed day. Stops and raises if blocked."""
    have: list[date] = []
    missing = []
    for d in needed:
        if load_day(d) is not None:
            have.append(d)
        else:
            missing.append(d)
    if not missing:
        return have
    if no_fetch:
        log(f"missing {len(missing)} day(s) and --no-fetch set; skipping them")
        return have
    log(f"fetching {len(missing)} missing official day(s)")
    for i, d in enumerate(missing):
        try:
            data = fetch_official_day(d)
            save_day(d, data)
            have.append(d)
            log(f"fetched {d.isoformat()} twse={len(data['twse'])} tpex={len(data['tpex'])} ({i+1}/{len(missing)})")
            time.sleep(SLEEP)
        except Exception as e:
            if is_blocked(e):
                raise RuntimeError(f"blocked fetching official closes for {d.isoformat()}: {e}") from e
            log(f"skip {d.isoformat()}: {e}")
            time.sleep(SLEEP)
    return sorted(set(have))


def build_series(
    days: list[date], universe: dict[str, set[str]]
) -> dict[tuple[str, str], list[tuple[date, float]]]:
    """Per-stock close series in date order, common stocks only."""
    series: dict[tuple[str, str], list[tuple[date, float]]] = {}
    use_list = bool(universe.get("twse") or universe.get("tpex"))
    for d in days:
        data = load_day(d)
        if not data:
            continue
        for mkt in ("twse", "tpex"):
            allowed = universe.get(mkt) or set()
            for code, px in (data.get(mkt) or {}).items():
                if not fetch.is_common_stock_code(code):
                    continue
                if use_list and allowed and code not in allowed:
                    continue
                series.setdefault((mkt, code), []).append((d, px))
    return series


def sma(vals: list[float], n: int) -> Optional[float]:
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def arrangement(ma_s: Optional[float], ma_m: Optional[float], ma_l: Optional[float]) -> Optional[str]:
    if ma_s is None or ma_m is None or ma_l is None:
        return None
    if ma_s > ma_m > ma_l:
        return "bull"
    if ma_s < ma_m < ma_l:
        return "bear"
    return "none"


def ratios_for_day(
    series: dict[tuple[str, str], list[tuple[date, float]]],
    d: date,
    market: str,
) -> dict[str, Any]:
    short_u = short_bull = short_bear = 0
    long_u = long_bull = long_bear = 0
    for (mkt, _code), rows in series.items():
        if market != "all" and mkt != market:
            continue
        # closes on or before d
        vals = [px for dt, px in rows if dt <= d]
        if len(vals) >= SHORT[-1]:
            a, b, c = (sma(vals, n) for n in SHORT)
            kind = arrangement(a, b, c)
            short_u += 1
            if kind == "bull":
                short_bull += 1
            elif kind == "bear":
                short_bear += 1
        if len(vals) >= LONG[-1]:
            a, b, c = (sma(vals, n) for n in LONG)
            kind = arrangement(a, b, c)
            long_u += 1
            if kind == "bull":
                long_bull += 1
            elif kind == "bear":
                long_bear += 1

    def pack(n: int, u: int) -> dict[str, Any]:
        pct = round(n / u * 100, 2) if u else None
        return {"n": n, "universe": u, "pct": pct}

    return {
        "name": MKT_NAME[market],
        "short_bull": pack(short_bull, short_u),
        "short_bear": pack(short_bear, short_u),
        "long_bull": pack(long_bull, long_u),
        "long_bear": pack(long_bear, long_u),
    }


def hist_point(ratios: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in ("short_bull", "short_bear", "long_bull", "long_bear"):
        out[k] = (ratios.get(k) or {}).get("pct")
    out["short_n"] = (ratios.get("short_bull") or {}).get("universe")
    out["long_n"] = (ratios.get("long_bull") or {}).get("universe")
    return out



def save_prior(series: dict[tuple[str, str], list[tuple[date, float]]], through: date) -> None:
    rows: dict[str, list[list]] = {}
    for (mkt, code), pts in series.items():
        rows[f"{mkt}|{code}"] = [[d.isoformat(), px] for d, px in pts if d <= through][-80:]
    blob = {
        "through": through.isoformat(),
        "updated": now_tpe().isoformat(timespec="seconds"),
        "rows": rows,
    }
    PRIOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PRIOR_PATH.with_suffix(PRIOR_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(blob, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(PRIOR_PATH)
    log(f"wrote prior cache through {through.isoformat()} stocks={len(rows)}")


def parse_session(today) -> date:
    if isinstance(today, datetime):
        return today.date()
    if isinstance(today, date):
        return today
    s = str(today).strip()
    if len(s) == 8 and s.isdigit():
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    return date.fromisoformat(s[:10])


def series_for_session(session: date) -> dict[tuple[str, str], list[tuple[date, float]]]:
    """Closes strictly before `session`, so today's live quote can be appended."""
    global _SERIES_CACHE
    key = session.isoformat()
    if _SERIES_CACHE and _SERIES_CACHE[0] == key:
        return _SERIES_CACHE[1]

    def from_days(days: list[date]) -> dict[tuple[str, str], list[tuple[date, float]]]:
        return build_series([d for d in days if d < session], {"twse": set(), "tpex": set()})

    cal = trading_calendar(session, 80)
    prev_days = [d for d in cal if d < session]
    prev = prev_days[-1] if prev_days else session - timedelta(days=1)

    raw = None
    if PRIOR_PATH.exists():
        try:
            raw = json.loads(PRIOR_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = None
    if isinstance(raw, dict) and raw.get("through") and raw.get("rows"):
        try:
            through = date.fromisoformat(str(raw["through"]))
        except ValueError:
            through = None
        if through is not None and through >= prev:
            series: dict[tuple[str, str], list[tuple[date, float]]] = {}
            for k, pts in raw["rows"].items():
                if "|" not in k:
                    continue
                mkt, code = k.split("|", 1)
                rows = []
                for rec in pts or []:
                    if not rec:
                        continue
                    try:
                        dt = date.fromisoformat(str(rec[0]))
                        px = float(rec[1])
                    except (TypeError, ValueError):
                        continue
                    if dt < session:
                        rows.append((dt, px))
                if rows:
                    series[(mkt, code)] = rows
            if series:
                _SERIES_CACHE = (key, series)
                return series

    series = from_days(cal if cal else prev_days)
    if series:
        save_prior(series, prev)
    _SERIES_CACHE = (key, series)
    return series


def qc_prices(qc: dict) -> dict[tuple[str, str], float]:
    out: dict[tuple[str, str], float] = {}
    for k, v in (qc or {}).items():
        if not isinstance(v, dict) or v.get("p") is None:
            continue
        if k.startswith("tse_"):
            mkt, code = "twse", k[4:]
        elif k.startswith("otc_"):
            mkt, code = "tpex", k[4:]
        else:
            continue
        if not fetch.is_common_stock_code(code):
            continue
        try:
            out[(mkt, code)] = float(v["p"])
        except (TypeError, ValueError):
            continue
    return out


def live_from_qc(qc: dict, today, at: Optional[str] = None) -> dict[str, Any]:
    """Fast live ratios: cached prior closes + MIS last price as today's close."""
    session = parse_session(today)
    series = {k: list(v) for k, v in series_for_session(session).items()}
    for key, px in qc_prices(qc).items():
        series.setdefault(key, []).append((session, px))
    markets = {mkt: ratios_for_day(series, session, mkt) for mkt in MARKETS}
    return {
        "as_of": session.isoformat(),
        "at": at,
        "src": "盤中即時價＋先前收盤自算",
        "src_short": "即時價自算",
        "unit": "%",
        "markets": markets,
    }


def hist_rec(at: str, markets: dict) -> list:
    a = markets.get("all") or {}
    def pct(k):
        v = (a.get(k) or {}).get("pct")
        return v
    return [at, pct("short_bull"), pct("short_bear"), pct("long_bull"), pct("long_bear")]

def compute(trade: Optional[date] = None, history: int = 20, backfill: int = 80, no_fetch: bool = False) -> dict[str, Any]:
    trade = discover_trade_date(trade, no_fetch=no_fetch)
    cal = trading_calendar(trade, backfill)
    # need backfill days ending at trade
    if trade not in cal:
        cal.append(trade)
        cal = sorted(set(cal))
    needed = cal[-backfill:] if len(cal) >= backfill else cal
    log(f"trade {trade.isoformat()} window {needed[0].isoformat()}–{needed[-1].isoformat()} ({len(needed)} days)")
    seed_from_prices(needed)
    have = fill_missing_days(needed, no_fetch=no_fetch)
    if trade not in have and not no_fetch:
        raise RuntimeError(f"no closes for trade date {trade.isoformat()}")
    if not have:
        raise RuntimeError("no daily closes available")
    universe = load_universe()
    series = build_series(have, universe)
    log(f"series twse={sum(1 for m,_ in series if m=='twse')} tpex={sum(1 for m,_ in series if m=='tpex')}")

    # history dates: last `history` days that we actually have
    hist_days = [d for d in have if d <= trade][-history:]
    history_rows = []
    today_markets: dict[str, Any] = {}
    for d in hist_days:
        row: dict[str, Any] = {"date": d.isoformat()}
        for mkt in MARKETS:
            r = ratios_for_day(series, d, mkt)
            row[mkt] = hist_point(r)
            if d == trade:
                today_markets[mkt] = r
        history_rows.append(row)
    if trade not in hist_days:
        # still compute today if series has earlier data only
        for mkt in MARKETS:
            today_markets[mkt] = ratios_for_day(series, trade, mkt)

    try:
        save_prior(series, trade)
    except Exception as e:
        log(f"save_prior failed: {e}")

    doc = {
        "title": "多空頭排列比例",
        "as_of": trade.isoformat(),
        "updated": now_tpe().isoformat(timespec="seconds"),
        "unit": "%",
        "definition": {
            "short": list(SHORT),
            "long": list(LONG),
            "bull": "短均 > 中均 > 長均",
            "bear": "短均 < 中均 < 長均",
            "note": "觀察項，與近日比較，無固定門檻",
        },
        "source": "證交所 MI_INDEX ALLBUT0999／櫃買 dailyQuotes 普通股收盤自算",
        "markets": today_markets,
        "history": history_rows,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(OUT.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(OUT)
    return doc


def summarize(doc: dict[str, Any]) -> None:
    as_of = doc.get("as_of")
    print(f"資料日 {as_of}")
    for mkt in MARKETS:
        r = (doc.get("markets") or {}).get(mkt) or {}
        bits = []
        for key, label in (
            ("short_bull", "短均多頭"),
            ("short_bear", "短均空頭"),
            ("long_bull", "長均多頭"),
            ("long_bear", "長均空頭"),
        ):
            x = r.get(key) or {}
            pct, n, u = x.get("pct"), x.get("n"), x.get("universe")
            bits.append(f"{label} {pct if pct is not None else '—'}%（{n}/{u}）")
        print(f"  {r.get('name', mkt)}：" + "，".join(bits))
    hist = doc.get("history") or []
    print(f"歷史 {len(hist)} 日，已寫入 {OUT}")
    # last 5 days 上市櫃 trend
    print("近 5 日上市櫃：")
    for row in hist[-5:]:
        a = row.get("all") or {}
        print(
            f"  {row.get('date')}  短多 {a.get('short_bull')}  短空 {a.get('short_bear')}  "
            f"長多 {a.get('long_bull')}  長空 {a.get('long_bear')}"
        )


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="多空頭排列家數比例（官方收盤自算）")
    ap.add_argument("--date", help="YYYY-MM-DD，預設最近已公布收盤日")
    ap.add_argument("--history", type=int, default=20, help="比例歷史天數（預設 20）")
    ap.add_argument("--backfill", type=int, default=80, help="價格回補交易日數（預設 80）")
    ap.add_argument("--no-fetch", action="store_true", help="只用磁碟快取，不向交易所要資料")
    ap.add_argument("--live", action="store_true", help="用盤中 state qc 即時價算一次並印出")
    ap.add_argument("--qc", help="day_YYYYMMDD.json 路徑（--live 用，預設今日 state）")
    a = ap.parse_args(argv)
    if a.live:
        from pathlib import Path as _P
        today = fetch.today_tpe()
        qc_path = _P(a.qc) if a.qc else _P("/workspace/intraday/state") / f"day_{today.strftime('%Y%m%d')}.json"
        if not qc_path.exists():
            log(f"no qc file {qc_path}")
            return 1
        raw = json.loads(qc_path.read_text(encoding="utf-8"))
        qc = raw.get("qc") or {}
        at = None
        ts = raw.get("ts") or {}
        if isinstance(ts, dict) and ts.get("t"):
            at = ts["t"][-1]
        session = raw.get("date")
        if not session:
            stem = qc_path.stem  # day_YYYYMMDD
            session = stem.split("_")[-1] if "_" in stem else today
        payload = live_from_qc(qc, session, at=at)
        # shape like official for summarize
        doc = {"as_of": payload["as_of"], "markets": payload["markets"], "history": [], "updated": payload.get("at")}
        print(f"即時 {payload.get('as_of')} {payload.get('at') or ''} {payload.get('src')}")
        summarize(doc)
        return 0
    trade = fetch.parse_date_any(a.date) if a.date else None
    try:
        doc = compute(trade=trade, history=a.history, backfill=a.backfill, no_fetch=a.no_fetch)
    except Exception as e:
        log("FAILED:", e)
        traceback.print_exc()
        return 1
    summarize(doc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
