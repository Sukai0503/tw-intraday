#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Daily Taiwan-market readings for the 非常態訊號儀表板.

Writes /workspace/market-signals/latest.json and appends to history.jsonl.

Usage:
  /workspace/market-signals/.venv/bin/python /workspace/market-signals/fetch.py
  /workspace/market-signals/.venv/bin/python /workspace/market-signals/fetch.py --date 2026-11-09
  /workspace/market-signals/.venv/bin/python /workspace/market-signals/fetch.py --skip-stock-stats
  /workspace/market-signals/.venv/bin/python /workspace/market-signals/fetch.py --stock-budget 180
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Optional

import requests

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None

DIR = Path("/workspace/market-signals")
CACHE = DIR / "cache"
LATEST_PATH = DIR / "latest.json"
HISTORY_PATH = DIR / "history.jsonl"
OBSERVATIONS_PATH = DIR / "observations.md"
TPE = timezone(timedelta(hours=8))

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
TIMEOUT = 18
RETRIES = 3
FLAT_EPS = 0.0005  # 0.05% MA change => flat

# Official / public sources
TWSE_RWD = "https://www.twse.com.tw/rwd/zh"
TWSE_OPENAPI = "https://openapi.twse.com.tw/v1/opendata"
TPEX_WWW = "https://www.tpex.org.tw/www/zh-tw"
TPEX_WEB = "https://www.tpex.org.tw/web"
TPEX_OPENAPI = "https://www.tpex.org.tw/openapi/v1"
TAIFEX_OPENAPI = "https://openapi.taifex.com.tw/v1"
MIS = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
WANTGOO_MARGIN = "https://www.wantgoo.com/stock/margin-trading/market-price/taiex"

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": UA,
        "Accept": "application/json,text/javascript,text/html,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        "Referer": "https://www.twse.com.tw/",
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_tpe() -> datetime:
    return datetime.now(TPE)


def today_tpe() -> date:
    return now_tpe().date()


def to_iso(d: date | datetime | str) -> str:
    if isinstance(d, datetime):
        return d.date().isoformat()
    if isinstance(d, date):
        return d.isoformat()
    s = str(d).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    if re.fullmatch(r"\d{8}", s):
        if int(s[:4]) >= 1911:
            return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
        # ROC yyyMMdd
        return roc_yyyymmdd_to_iso(s)
    if "/" in s:
        parts = s.split("/")
        if len(parts) == 3:
            y, m, dd = (int(p) for p in parts)
            if y < 1911:
                y += 1911
            return f"{y:04d}-{m:02d}-{dd:02d}"
    return s


def to_ymd(d: date | datetime | str) -> str:
    return to_iso(d).replace("-", "")


def to_roc_slash(d: date | datetime | str) -> str:
    iso = to_iso(d)
    y, m, dd = iso.split("-")
    return f"{int(y) - 1911}/{m}/{dd}"


def roc_yyyymmdd_to_iso(s: str) -> str:
    s = re.sub(r"\D", "", s)
    if len(s) == 7:  # 1151109
        y, m, d = int(s[:3]) + 1911, s[3:5], s[5:7]
        return f"{y:04d}-{m}-{d}"
    if len(s) == 8 and int(s[:4]) < 1911:  # unlikely
        y, m, d = int(s[:4]) + 1911, s[4:6], s[6:8]
        return f"{y:04d}-{m}-{d}"
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    raise ValueError(f"bad roc date: {s}")


def parse_date_any(s: Any) -> Optional[date]:
    if s is None:
        return None
    try:
        iso = to_iso(s)
        return date.fromisoformat(iso)
    except Exception:
        return None


def parse_num(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v)):
        return float(v)
    s = str(v).strip()
    if s in ("", "--", "---", "—", "－", "n/a", "N/A", "null", "None", "-"):
        return None
    s = s.replace(",", "").replace("%", "").replace("+", "")
    s = re.sub(r"<[^>]+>", "", s)
    s = s.strip()
    if s in ("", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def sign_from_html(v: Any) -> int:
    s = str(v or "")
    if "-" in s or "green" in s.lower() or "–" in s or "−" in s:
        # careful: color:green often means down on TWSE
        if "color:green" in s.lower() or (s.strip() in ("-", "–", "−")):
            return -1
    if "+" in s or "color:red" in s.lower():
        return 1
    return 0


def field(
    value: Any,
    source: Optional[str],
    *,
    unit: Optional[str] = None,
    as_of: Optional[str] = None,
    note: Optional[str] = None,
    method: Optional[str] = None,
    stale: bool = False,
    reason: Optional[str] = None,
    extra: Optional[dict] = None,
) -> dict:
    out: dict[str, Any] = {
        "value": value,
        "source": source,
        "as_of": as_of,
        "unit": unit,
        "note": note,
        "method": method,
        "stale": stale,
        "reason": reason,
    }
    if extra:
        out.update(extra)
    return out


def missing(reason: str, source: Optional[str] = None, *, as_of: Optional[str] = None) -> dict:
    return field(None, source, reason=reason, as_of=as_of)


def http_get(url: str, *, params: Optional[dict] = None, timeout: int = TIMEOUT, raw: bool = False) -> requests.Response:
    last_err: Exception | None = None
    for i in range(RETRIES):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            if r.status_code in (403, 429) and i < RETRIES - 1:
                time.sleep(1.2 * (i + 1))
                last_err = RuntimeError(f"HTTP {r.status_code}")
                continue
            return r
        except Exception as e:
            last_err = e
            time.sleep(0.6 * (i + 1))
    raise RuntimeError(f"GET failed {url}: {last_err}")


def get_json(url: str, *, params: Optional[dict] = None, timeout: int = TIMEOUT) -> Any:
    r = http_get(url, params=params, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} for {url}")
    ctype = (r.headers.get("content-type") or "").lower()
    text = r.text.lstrip("\ufeff")
    if "html" in ctype and not text.startswith(("{", "[")):
        raise RuntimeError(f"HTML (not JSON) from {url} ({r.status_code}, {len(r.content)} bytes)")
    try:
        return json.loads(text)
    except Exception as e:
        raise RuntimeError(f"JSON parse failed for {url}: {e}") from e


def twse_ok(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    stat = str(payload.get("stat") or "")
    return stat.upper() == "OK" or stat.lower() == "ok"


def tpex_ok(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    stat = str(payload.get("stat") or "")
    return stat.lower() == "ok"



def first_filled_table(payload: dict, require: Optional[str] = None) -> Optional[dict]:
    for tbl in payload.get("tables") or []:
        if not isinstance(tbl, dict):
            continue
        fields = tbl.get("fields") or []
        data = tbl.get("data") or []
        if not fields or not data:
            continue
        if require and not any(require in str(f) for f in fields):
            continue
        return tbl
    return None


def cache_path(*parts: str) -> Path:
    p = CACHE.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def cache_load_json(path: Path) -> Any:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def cache_save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def iter_months(start: date, end: date) -> list[date]:
    out = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(date(y, m, 1))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def ma(values: list[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def ma_direction(curr: Optional[float], prev: Optional[float]) -> Optional[str]:
    if curr is None or prev is None or prev == 0:
        return None
    chg = (curr - prev) / abs(prev)
    if abs(chg) < FLAT_EPS:
        return "flat"
    return "up" if chg > 0 else "down"


def position_vs(close: Optional[float], avg: Optional[float]) -> Optional[dict]:
    if close is None or avg is None or avg == 0:
        return None
    pct = (close / avg - 1.0) * 100.0
    return {
        "side": "above" if close >= avg else "below",
        "pct": round(pct, 2),
        "ma": round(avg, 2),
    }


def last_of_week(daily: list[tuple[date, float]]) -> list[tuple[date, float]]:
    """Weekly close = last trading day of each ISO week."""
    buckets: dict[tuple[int, int], tuple[date, float]] = {}
    for d, v in daily:
        key = d.isocalendar()[:2]
        buckets[key] = (d, v)
    return [buckets[k] for k in sorted(buckets)]


def last_of_month(daily: list[tuple[date, float]]) -> list[tuple[date, float]]:
    buckets: dict[tuple[int, int], tuple[date, float]] = {}
    for d, v in daily:
        buckets[(d.year, d.month)] = (d, v)
    return [buckets[k] for k in sorted(buckets)]


def is_common_stock_code(code: str) -> bool:
    """Rough filter; prefer official company lists when available."""
    code = str(code).strip()
    if re.fullmatch(r"\d{4}", code):
        return not code.startswith("00")
    return False




# ---------------------------------------------------------------------------
# Discover trade date / market index + breadth
# ---------------------------------------------------------------------------

def mi_index(date_ymd: str, typ: str) -> dict:
    url = f"{TWSE_RWD}/afterTrading/MI_INDEX"
    return get_json(url, params={"date": date_ymd, "type": typ, "response": "json"})


def discover_trade_date(forced: Optional[date] = None) -> date:
    if forced:
        return forced
    d = today_tpe()
    for _ in range(14):
        ymd = d.strftime("%Y%m%d")
        try:
            payload = mi_index(ymd, "IND")
            if twse_ok(payload):
                return date.fromisoformat(to_iso(payload.get("date") or ymd))
        except Exception:
            pass
        d -= timedelta(days=1)
    raise RuntimeError("could not find a recent TWSE trade date")


def _extract_index_row(tables: list, name: str) -> Optional[dict]:
    for tbl in tables or []:
        if not isinstance(tbl, dict):
            continue
        fields = tbl.get("fields") or []
        for row in tbl.get("data") or []:
            if not row or str(row[0]).strip() != name:
                continue
            close = parse_num(row[1])
            chg_pts = parse_num(row[3])
            chg_pct = parse_num(row[4])
            if chg_pts is not None:
                # apply sign from HTML in col 2 if needed
                if chg_pts > 0 and sign_from_html(row[2]) < 0:
                    chg_pts = -chg_pts
                    if chg_pct is not None:
                        chg_pct = -abs(chg_pct)
            return {"close": close, "change": chg_pts, "pct": chg_pct}
    return None


def fetch_twse_index_breadth(trade: date) -> dict:
    ymd = trade.strftime("%Y%m%d")
    src_ind = f"{TWSE_RWD}/afterTrading/MI_INDEX?date={ymd}&type=IND&response=json"
    src_ms = f"{TWSE_RWD}/afterTrading/MI_INDEX?date={ymd}&type=MS&response=json"
    out: dict[str, Any] = {}
    try:
        payload = mi_index(ymd, "IND")
        if not twse_ok(payload):
            out["taiex.close"] = missing(f"MI_INDEX IND not ready: {payload.get('stat')}", src_ind, as_of=trade.isoformat())
        else:
            row = _extract_index_row(payload.get("tables") or [], "發行量加權股價指數")
            if not row or row["close"] is None:
                out["taiex.close"] = missing("發行量加權股價指數 row not found", src_ind, as_of=trade.isoformat())
            else:
                out["taiex.close"] = field(row["close"], src_ind, unit="point", as_of=trade.isoformat())
                out["taiex.change"] = field(row["change"], src_ind, unit="point", as_of=trade.isoformat())
                out["taiex.pct"] = field(row["pct"], src_ind, unit="%", as_of=trade.isoformat())
    except Exception as e:
        out["taiex.close"] = missing(str(e), src_ind, as_of=trade.isoformat())

    # breadth
    try:
        payload = mi_index(ymd, "MS")
        if not twse_ok(payload):
            raise RuntimeError(str(payload.get("stat")))
        tbl = None
        for t in payload.get("tables") or []:
            if t.get("title") == "漲跌證券數合計":
                tbl = t
                break
        parsed: dict[str, dict] = {}
        if tbl:
            # fields: 類型, 整體市場, 股票
            # rows: 上漲(漲停), 下跌(跌停), 持平, 未成交, 無比價
            for row in tbl.get("data") or []:
                typ = str(row[0])
                def split_pair(cell: str) -> tuple[Optional[float], Optional[float]]:
                    # '10,654(353)' or '5,080'
                    cell = str(cell).replace(",", "")
                    m = re.match(r"([-\d.]+)\((\d+)\)", cell)
                    if m:
                        return parse_num(m.group(1)), parse_num(m.group(2))
                    return parse_num(cell), None
                overall, stock = split_pair(row[1]), split_pair(row[2])
                parsed[typ] = {"整體市場": overall, "股票": stock}

        def pick(row_name: str, col: str, idx: int) -> Optional[float]:
            rec = parsed.get(row_name)
            if not rec:
                return None
            return rec[col][idx]

        # 股票 column is the meaningful 家數 for 上市股票
        up = pick("上漲(漲停)", "股票", 0)
        limit_up = pick("上漲(漲停)", "股票", 1)
        down = pick("下跌(跌停)", "股票", 0)
        limit_down = pick("下跌(跌停)", "股票", 1)
        flat = pick("持平", "股票", 0)
        untraded = pick("未成交", "股票", 0)
        incomparable = pick("無比價", "股票", 0)
        total = None
        parts = [up, down, flat, untraded, incomparable]
        if all(p is not None for p in parts):
            total = sum(parts)  # type: ignore
        as_of = trade.isoformat()
        out["twse.advancers"] = field(up, src_ms, unit="家", as_of=as_of, note="股票欄（不含權證等）")
        out["twse.decliners"] = field(down, src_ms, unit="家", as_of=as_of, note="股票欄（不含權證等）")
        out["twse.limit_up"] = field(limit_up, src_ms, unit="家", as_of=as_of, note="股票欄")
        out["twse.limit_down"] = field(limit_down, src_ms, unit="家", as_of=as_of, note="股票欄")
        out["twse.unchanged"] = field(flat, src_ms, unit="家", as_of=as_of)
        out["twse.listed_stock_count"] = field(total, src_ms, unit="家", as_of=as_of, note="上漲+下跌+持平+未成交+無比價（股票欄）")
    except Exception as e:
        for k in ("twse.advancers", "twse.decliners", "twse.limit_up", "twse.limit_down", "twse.listed_stock_count"):
            out[k] = missing(str(e), src_ms, as_of=trade.isoformat())
    return out


def fetch_tpex_index_breadth(trade: date) -> dict:
    ymd = trade.strftime("%Y%m%d")
    roc = to_roc_slash(trade)
    src_hl = f"{TPEX_WEB}/stock/aftertrading/market_highlight/highlight_result.php?l=zh-tw&d={roc}"
    src_idx = f"{TPEX_WWW}/afterTrading/tradingIndex?date={trade.strftime('%Y/%m/%d')}&id=EWITS&response=json"
    src_mis = f"{MIS}?ex_ch=tse_t00.tw|otc_o00.tw&json=1&delay=0"
    out: dict[str, Any] = {}
    close = chg = total = up = down = lu = ld = flat = None
    used_src = src_hl
    as_of = trade.isoformat()
    official = False
    try:
        payload = get_json(src_hl)
        if tpex_ok(payload) and payload.get("tables"):
            tbl = payload["tables"][0]
            fields = tbl.get("fields") or []
            row = (tbl.get("data") or [None])[0]
            if row and fields:
                rec = dict(zip(fields, row))
                close = parse_num(rec.get("收市指數"))
                chg = parse_num(rec.get("指數漲跌"))
                total = parse_num(rec.get("上櫃家數"))
                up = parse_num(rec.get("上漲家數"))
                down = parse_num(rec.get("下跌家數"))
                lu = parse_num(rec.get("漲停家數"))
                ld = parse_num(rec.get("跌停家數"))
                flat = parse_num(rec.get("平盤家數"))
                official = close is not None
    except Exception:
        pass

    if close is None:
        # monthly tradingIndex may already include today late in the day
        try:
            payload = get_json(src_idx)
            if tpex_ok(payload):
                tbl = (payload.get("tables") or [{}])[0]
                for row in tbl.get("data") or []:
                    if to_iso(row[0]) == trade.isoformat():
                        close = parse_num(row[4])
                        chg = parse_num(row[5])
                        used_src = src_idx
                        official = True
                        break
        except Exception:
            pass

    if close is None:
        try:
            payload = get_json(src_mis)
            for m in payload.get("msgArray") or []:
                if str(m.get("c") or "") == "o00" or "櫃買" in str(m.get("n") or ""):
                    if str(m.get("d") or "") == ymd or True:
                        close = parse_num(m.get("z"))
                        prev = parse_num(m.get("y"))
                        if close is not None and prev:
                            chg = round(close - prev, 2)
                        used_src = src_mis
                        note_mis = "official highlight not yet published; using TWSE/TPEx MIS close"
                        as_of = to_iso(m.get("d") or trade)
                        out["tpex.close"] = field(close, used_src, unit="point", as_of=as_of, note=note_mis, stale=as_of != trade.isoformat())
                        out["tpex.change"] = field(chg, used_src, unit="point", as_of=as_of, note=note_mis)
                        if close and prev:
                            out["tpex.pct"] = field(round((close / prev - 1) * 100, 2), used_src, unit="%", as_of=as_of)
                        break
        except Exception as e:
            out["tpex.close"] = missing(str(e), src_hl, as_of=trade.isoformat())
    else:
        out["tpex.close"] = field(close, used_src, unit="point", as_of=as_of)
        out["tpex.change"] = field(chg, used_src, unit="point", as_of=as_of)
        # pct if we have prev from change
        if close and chg is not None and (close - chg) != 0:
            out["tpex.pct"] = field(round(chg / (close - chg) * 100, 2), used_src, unit="%", as_of=as_of)

    src_b = src_hl if official or up is not None else src_hl
    if up is not None:
        out["tpex.advancers"] = field(up, src_hl, unit="家", as_of=as_of)
        out["tpex.decliners"] = field(down, src_hl, unit="家", as_of=as_of)
        out["tpex.limit_up"] = field(lu, src_hl, unit="家", as_of=as_of)
        out["tpex.limit_down"] = field(ld, src_hl, unit="家", as_of=as_of)
        out["tpex.unchanged"] = field(flat, src_hl, unit="家", as_of=as_of)
        out["tpex.listed_stock_count"] = field(total, src_hl, unit="家", as_of=as_of, note="上櫃家數")
    else:
        for k in ("tpex.advancers", "tpex.decliners", "tpex.limit_up", "tpex.limit_down", "tpex.listed_stock_count"):
            out.setdefault(k, missing("TPEx market highlight not yet published for this date", src_hl, as_of=trade.isoformat()))
    return out


# ---------------------------------------------------------------------------
# Official index history + moving averages + drawdown
# ---------------------------------------------------------------------------

def fetch_fmtqik_month(month: date) -> list[tuple[date, float]]:
    ymd = month.strftime("%Y%m") + "01"
    path = cache_path("fmtqik", f"{month.strftime('%Y%m')}.json")
    # Re-fetch current month every run (it grows); cache past months.
    is_current = (month.year, month.month) == (today_tpe().year, today_tpe().month)
    if path.exists() and not is_current:
        raw = cache_load_json(path)
    else:
        url = f"{TWSE_RWD}/afterTrading/FMTQIK"
        payload = get_json(url, params={"date": ymd, "response": "json"})
        if not twse_ok(payload):
            return []
        raw = payload.get("data") or []
        cache_save_json(path, raw)
    rows = []
    for rec in raw:
        try:
            d = parse_date_any(rec[0])
            v = parse_num(rec[4])
            if d and v is not None:
                rows.append((d, v))
        except Exception:
            continue
    return rows


def fetch_tpex_index_month(month: date) -> list[tuple[date, float]]:
    path = cache_path("tpex_index", f"{month.strftime('%Y%m')}.json")
    is_current = (month.year, month.month) == (today_tpe().year, today_tpe().month)
    if path.exists() and not is_current:
        raw = cache_load_json(path)
    else:
        url = f"{TPEX_WWW}/afterTrading/tradingIndex"
        payload = get_json(url, params={"date": month.strftime("%Y/%m/01"), "id": "EWITS", "response": "json"})
        if not tpex_ok(payload):
            return []
        tbl = (payload.get("tables") or [{}])[0]
        raw = tbl.get("data") or []
        cache_save_json(path, raw)
    rows = []
    for rec in raw:
        d = parse_date_any(rec[0])
        v = parse_num(rec[4])
        if d and v is not None:
            rows.append((d, v))
    return rows


def load_index_history(kind: str, end: date, years: int = 12) -> list[tuple[date, float]]:
    start = date(end.year - years, 1, 1)
    months = iter_months(start, end)
    fn = fetch_fmtqik_month if kind == "taiex" else fetch_tpex_index_month
    rows: list[tuple[date, float]] = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fn, m): m for m in months}
        for fut in as_completed(futs):
            try:
                rows.extend(fut.result())
            except Exception:
                continue
    uniq: dict[date, float] = {}
    for d, v in rows:
        uniq[d] = v
    return [(d, uniq[d]) for d in sorted(uniq) if d <= end]


def append_or_replace_close(hist: list[tuple[date, float]], d: date, close: Optional[float]) -> list[tuple[date, float]]:
    if close is None:
        return hist
    hist = [(x, y) for x, y in hist if x != d]
    hist.append((d, close))
    hist.sort()
    return hist


def compute_index_indicators(name: str, hist: list[tuple[date, float]], trade: date, source_hist: str) -> dict:
    out: dict[str, Any] = {}
    if not hist:
        for k in ("ma10d_dir", "ma10w_dir", "ma10m_dir"):
            out[f"{name}.{k}"] = missing("no history", source_hist)
        return out
    # If trade close not in hist, indicators use last hist date
    daily = [(d, v) for d, v in hist if d <= trade]
    if not daily:
        return out
    closes = [v for _, v in daily]
    dates = [d for d, _ in daily]
    last_d, last_c = daily[-1]
    as_of = last_d.isoformat()

    # 10-day
    ma10 = ma(closes, 10)
    ma10_prev = ma(closes[:-1], 10) if len(closes) > 10 else None
    out[f"{name}.ma10d"] = field(round(ma10, 2) if ma10 else None, source_hist, unit="point", as_of=as_of, method="SMA of last 10 official daily closes")
    out[f"{name}.ma10d_dir"] = field(ma_direction(ma10, ma10_prev), source_hist, as_of=as_of, method="today SMA10 vs previous session SMA10; |chg|<0.05% => flat")

    # 10-week
    weekly = last_of_week(daily)
    wcloses = [v for _, v in weekly]
    ma10w = ma(wcloses, 10)
    ma10w_prev = ma(wcloses[:-1], 10) if len(wcloses) > 10 else None
    out[f"{name}.ma10w"] = field(round(ma10w, 2) if ma10w else None, source_hist, unit="point", as_of=as_of, method="SMA of last 10 weekly closes (last trading day of ISO week)")
    out[f"{name}.ma10w_dir"] = field(ma_direction(ma10w, ma10w_prev), source_hist, as_of=as_of, method="this week's SMA10w vs previous week's SMA10w")

    # 10-month
    monthly = last_of_month(daily)
    mcloses = [v for _, v in monthly]
    ma10m = ma(mcloses, 10)
    ma10m_prev = ma(mcloses[:-1], 10) if len(mcloses) > 10 else None
    out[f"{name}.ma10m"] = field(round(ma10m, 2) if ma10m else None, source_hist, unit="point", as_of=as_of, method="SMA of last 10 monthly closes (last trading day of month)")
    out[f"{name}.ma10m_dir"] = field(ma_direction(ma10m, ma10m_prev), source_hist, as_of=as_of, method="this month's SMA10m vs previous month's SMA10m")

    # daily MAs 60/120/240/480
    for n, label, zh in ((60, "ma60d", "季線"), (120, "ma120d", "半年線"), (240, "ma240d", "年線"), (480, "ma480d", "2年線(日)")):
        avg = ma(closes, n)
        out[f"{name}.{label}"] = field(
            round(avg, 2) if avg else None,
            source_hist,
            unit="point",
            as_of=as_of,
            method=f"SMA of last {n} official daily closes（{zh}）",
        )
        out[f"{name}.vs_{label}"] = field(
            position_vs(last_c, avg),
            source_hist,
            as_of=as_of,
            method=f"close vs {zh} SMA{n}",
        )

    # long MAs from monthly closes (Taiwan convention)
    for n, label, zh in ((24, "ma24m", "2年線"), (60, "ma60m", "5年線"), (120, "ma120m", "10年線")):
        avg = ma(mcloses, n)
        method = f"SMA of last {n} official monthly closes（{zh}；台股長均線慣例用月線）"
        out[f"{name}.{label}"] = field(round(avg, 2) if avg else None, source_hist, unit="point", as_of=as_of, method=method)
        out[f"{name}.vs_{label}"] = field(position_vs(last_c, avg), source_hist, as_of=as_of, method=f"close vs {zh}")

    # Preferred "2年線" is 24-month if we have it, else SMA480
    pref_2y = ma(mcloses, 24) or ma(closes, 480)
    out[f"{name}.vs_2y"] = field(
        position_vs(last_c, pref_2y),
        source_hist,
        as_of=as_of,
        method="24-month SMA if ≥24 monthly closes, else daily SMA480",
    )
    out[f"{name}.vs_5y"] = out[f"{name}.vs_ma60m"]
    out[f"{name}.vs_10y"] = out[f"{name}.vs_ma120m"]
    out[f"{name}.vs_year"] = out[f"{name}.vs_ma240d"]
    out[f"{name}.vs_half"] = out[f"{name}.vs_ma120d"]
    out[f"{name}.vs_quarter"] = out[f"{name}.vs_ma60d"]

    # drawdowns
    def max_since(days: Optional[int]) -> Optional[float]:
        window = daily if days is None else [p for p in daily if p[0] >= last_d - timedelta(days=days)]
        if not window:
            return None
        return max(v for _, v in window)

    for days, key, note in ((365, "drawdown_1y", "自近一年高點回落"), (None, "drawdown_hist", "自資料區間高點回落（約12年官方日線）")):
        mx = max_since(days)
        if mx:
            pct = (last_c / mx - 1.0) * 100.0
            out[f"{name}.{key}"] = field(
                round(pct, 2),
                source_hist,
                unit="%",
                as_of=as_of,
                note=f"{note}; high={round(mx, 2)}",
                method="(close / period high - 1) * 100",
            )
        else:
            out[f"{name}.{key}"] = missing("insufficient history", source_hist, as_of=as_of)
    return out




# ---------------------------------------------------------------------------
# Margin / 融資維持率 / 追繳戶數 / official 整戶擔保維持率
# ---------------------------------------------------------------------------

def _latest_available_date(fetch_fn, trade: date, tries: int = 10) -> tuple[Optional[date], Any]:
    d = trade
    skipped = 0
    while skipped < tries:
        # skip weekends quickly
        try:
            payload = fetch_fn(d)
            return d, payload
        except Exception:
            d -= timedelta(days=1)
            skipped += 1
            continue
    return None, None


def fetch_twse_margin_ms(d: date) -> dict:
    ymd = d.strftime("%Y%m%d")
    url = f"{TWSE_RWD}/marginTrading/MI_MARGN"
    payload = get_json(url, params={"date": ymd, "selectType": "MS", "response": "json"})
    if not twse_ok(payload):
        raise RuntimeError(str(payload.get("stat") or "no data"))
    return payload


def fetch_twse_margin_all(d: date) -> dict:
    ymd = d.strftime("%Y%m%d")
    url = f"{TWSE_RWD}/marginTrading/MI_MARGN"
    payload = get_json(url, params={"date": ymd, "selectType": "ALL", "response": "json"})
    if not twse_ok(payload):
        raise RuntimeError(str(payload.get("stat") or "no data"))
    return payload


def parse_twse_margin_ms(payload: dict) -> dict:
    amt_today = amt_prev = shares_today = shares_prev = None
    for tbl in payload.get("tables") or []:
        for row in tbl.get("data") or []:
            name = str(row[0])
            if "融資金額" in name:
                amt_prev = parse_num(row[4])  # 仟元
                amt_today = parse_num(row[5])
            elif name.startswith("融資(交易單位)"):
                shares_prev = parse_num(row[4])
                shares_today = parse_num(row[5])
    return {
        "amt_today_k": amt_today,
        "amt_prev_k": amt_prev,
        "shares_today": shares_today,
        "shares_prev": shares_prev,
    }


def fetch_margin_twse(trade: date) -> dict:
    src = f"{TWSE_RWD}/marginTrading/MI_MARGN?date={trade.strftime('%Y%m%d')}&selectType=MS&response=json"
    out: dict[str, Any] = {}
    try:
        d, payload = _latest_available_date(fetch_twse_margin_ms, trade)
        if not payload:
            return {"twse.margin_balance": missing("MI_MARGN not yet published", src, as_of=trade.isoformat())}
        rec = parse_twse_margin_ms(payload)
        as_of = d.isoformat() if d else trade.isoformat()
        stale = d != trade
        yi = 1e5  # 仟元 -> 億元
        bal = rec["amt_today_k"] / yi if rec["amt_today_k"] is not None else None
        prev = rec["amt_prev_k"] / yi if rec["amt_prev_k"] is not None else None
        chg = round(bal - prev, 2) if bal is not None and prev is not None else None
        note = "融資金額(仟元)／100000 = 億元"
        if stale:
            note += f"；{trade.isoformat()} 尚未公布，先用 {as_of}"
        out["twse.margin_balance"] = field(round(bal, 2) if bal is not None else None, src.replace(trade.strftime("%Y%m%d"), d.strftime("%Y%m%d") if d else ""), unit="億元", as_of=as_of, note=note, stale=stale)
        out["twse.margin_change"] = field(chg, src, unit="億元", as_of=as_of, stale=stale, note="今日餘額 − 前日餘額")
        out["twse.margin_shares"] = field(rec["shares_today"], src, unit="張", as_of=as_of, stale=stale)
        out["_twse_margin_raw"] = {"date": d, "rec": rec, "payload_all_needed": True}
    except Exception as e:
        out["twse.margin_balance"] = missing(str(e), src, as_of=trade.isoformat())
    return out


def fetch_tpex_dashboard_margin(d: Optional[date] = None) -> dict:
    url = f"{TPEX_WWW}/dashboardOtc/margin"
    params = {"lang": "zh-tw"}
    if d:
        params["date"] = to_roc_slash(d)
    payload = get_json(url, params=params)
    if not tpex_ok(payload):
        raise RuntimeError(str(payload.get("stat") or "no data"))
    return payload


def fetch_tpex_margin_trend(days: int = 8) -> dict:
    url = f"{TPEX_WWW}/dashboardOtc/marginTrend"
    return get_json(url, params={"lang": "zh-tw", "days": days})


def fetch_margin_tpex(trade: date) -> dict:
    src = f"{TPEX_WWW}/dashboardOtc/margin?lang=zh-tw"
    out: dict[str, Any] = {}
    try:
        # latest snapshot
        snap = fetch_tpex_dashboard_margin()
        as_of = to_iso(snap.get("date"))
        stale = as_of != trade.isoformat()
        bal = parse_num(snap.get("marginBalance"))
        # previous from trend
        prev = None
        trend_src = f"{TPEX_WWW}/dashboardOtc/marginTrend?lang=zh-tw&days=8"
        try:
            tr = fetch_tpex_margin_trend(8)
            rows = tr.get("trend") or []
            # find as_of and previous
            rows_sorted = sorted(rows, key=lambda r: r.get("date") or "")
            for i, r in enumerate(rows_sorted):
                if to_iso(r.get("date")) == as_of and i > 0:
                    prev = parse_num(rows_sorted[i - 1].get("marginBalance"))
                    bal = parse_num(r.get("marginBalance")) or bal
        except Exception:
            pass
        chg = round(bal - prev, 2) if bal is not None and prev is not None else None
        note = "櫃買中心臺股儀表板 marginBalance，單位億元"
        if stale:
            note += f"；{trade.isoformat()} 尚未公布，先用 {as_of}"
        out["tpex.margin_balance"] = field(bal, src, unit="億元", as_of=as_of, note=note, stale=stale)
        out["tpex.margin_change"] = field(chg, trend_src, unit="億元", as_of=as_of, stale=stale)
        out["_tpex_margin_raw"] = {"date": parse_date_any(as_of), "balance": bal, "snap": snap}
    except Exception as e:
        out["tpex.margin_balance"] = missing(str(e), src, as_of=trade.isoformat())
    return out


def fetch_official_keep_rate(trade: date) -> dict:
    """TWSE 全市場整戶擔保維持率 (BFIJ3U). Published T+1 (next calendar/trading day)."""
    src = f"{TWSE_RWD}/marginTrading/BFIJ3U?response=json&date={trade.strftime('%Y%m%d')}"
    out: dict[str, Any] = {}
    payload = None
    used = trade
    try:
        payload = get_json(f"{TWSE_RWD}/marginTrading/BFIJ3U", params={"response": "json", "date": trade.strftime("%Y%m%d")})
        if not twse_ok(payload):
            # try latest (no date) and trend
            payload = get_json(f"{TWSE_RWD}/marginTrading/BFIJ3U", params={"response": "json"})
        if not twse_ok(payload):
            raise RuntimeError(str(payload.get("stat") or "no data"))
        used = parse_date_any(payload.get("date")) or trade
        src = f"{TWSE_RWD}/marginTrading/BFIJ3U?response=json&date={used.strftime('%Y%m%d')}"
        stale = used != trade
        note = (
            "證交所「全市場擔保維持率／整戶擔保維持率」，融資+融券、上市+上櫃合併。"
            "與民間「大盤融資維持率」（融資市值/融資金額、分市場）口徑不同。"
        )
        if stale:
            note += f" 此欄為隔日公布，{trade.isoformat()} 的數字要到次一營業日才有；目前最新是 {used.isoformat()}。"
        out["official.keep_rate"] = field(parse_num(payload.get("keepRate")), src, unit="%", as_of=used.isoformat(), note=note, stale=stale)
        out["official.below_130_accounts"] = field(
            parse_num(payload.get("belowAccNum")), src, unit="戶", as_of=used.isoformat(),
            note="整戶擔保維持率低於130%之戶數（截至當日）", stale=stale,
        )
        out["official.call_accounts"] = field(
            parse_num(payload.get("callAccNum")), src, unit="戶", as_of=used.isoformat(),
            note="當日證券商通知應補繳之追繳戶數", stale=stale,
        )
        out["official.call_amount"] = field(
            (parse_num(payload.get("callAmt")) or 0) / 1e8 if payload.get("callAmt") is not None else None,
            src, unit="億元", as_of=used.isoformat(), note="當日追繳通知金額", stale=stale,
        )
        out["official.dispose_accounts"] = field(
            parse_num(payload.get("exeAccNum")), src, unit="戶", as_of=used.isoformat(),
            note="處分（斷頭）戶數", stale=stale,
        )
        # also grab trend for previous keep rate
        try:
            tr = get_json(f"{TWSE_RWD}/marginTrading/BFIJ3U_TREND", params={"response": "json", "days": 8})
            rows = tr.get("data") or []
            out["_keep_rate_trend"] = rows
        except Exception:
            pass
    except Exception as e:
        out["official.keep_rate"] = missing(str(e), src, as_of=trade.isoformat())
        out["official.call_accounts"] = missing(str(e), src, as_of=trade.isoformat())
        out["official.below_130_accounts"] = missing(str(e), src, as_of=trade.isoformat())
    return out


def _twse_closes(d: date) -> dict[str, float]:
    ymd = d.strftime("%Y%m%d")
    payload = mi_index(ymd, "ALLBUT0999")
    if not twse_ok(payload):
        raise RuntimeError(str(payload.get("stat")))
    closes: dict[str, float] = {}
    for tbl in payload.get("tables") or []:
        fields = [str(f) for f in (tbl.get("fields") or [])]
        if "收盤價" not in fields and "收盤指數" not in fields:
            # typical: 證券代號, 證券名稱, ... 收盤價
            if not fields or "證券代號" not in fields:
                continue
        idx_code = next((i for i, f in enumerate(fields) if "代號" in f), 0)
        idx_close = next((i for i, f in enumerate(fields) if f == "收盤價" or f == "收盤"), None)
        if idx_close is None:
            continue
        for row in tbl.get("data") or []:
            code = str(row[idx_code]).strip()
            # TWSE uses '--' or a special char when limit / no close; sometimes close is in a later col
            v = parse_num(row[idx_close])
            if v is not None:
                closes[code] = v
    if not closes:
        raise RuntimeError("no closes parsed from MI_INDEX ALLBUT0999")
    return closes


def _twse_margin_shares(d: date) -> dict[str, float]:
    payload = fetch_twse_margin_all(d)
    shares: dict[str, float] = {}
    # tables may wrap the list
    rows = []
    if payload.get("tables"):
        for t in payload["tables"]:
            rows.extend(t.get("data") or [])
            fields = t.get("fields") or []
    else:
        rows = payload.get("data") or []
        fields = payload.get("fields") or []
    # If no fields, use known order from OpenAPI/rwd
    # 股票代號, 股票名稱, 融資買進, 融資賣出, 融資現金償還, 融資前日餘額, 融資今日餘額, ...
    for row in rows:
        if not row:
            continue
        if isinstance(row, dict):
            code = str(row.get("股票代號") or row.get("代號") or "").strip()
            sh = parse_num(row.get("融資今日餘額"))
        else:
            code = str(row[0]).strip()
            sh = parse_num(row[6]) if len(row) > 6 else None
        if code and sh is not None:
            shares[code] = sh
    return shares


def _tpex_closes(d: date) -> dict[str, float]:
    url = f"{TPEX_WWW}/afterTrading/dailyQuotes"
    payload = get_json(url, params={"date": d.strftime("%Y/%m/%d"), "response": "json"})
    if not tpex_ok(payload):
        raise RuntimeError(str(payload.get("stat")))
    closes: dict[str, float] = {}
    shares_out: dict[str, float] = {}
    for tbl in payload.get("tables") or []:
        fields = tbl.get("fields") or []
        if "收盤" not in fields:
            continue
        i_code = fields.index("代號")
        i_close = fields.index("收盤")
        i_shares = fields.index("發行股數") if "發行股數" in fields else None
        for row in tbl.get("data") or []:
            code = str(row[i_code]).strip()
            v = parse_num(row[i_close])
            if v is not None:
                closes[code] = v
            if i_shares is not None:
                sh = parse_num(row[i_shares])
                if sh is not None:
                    shares_out[code] = sh
    if not closes:
        raise RuntimeError("no TPEx closes")
    closes["_issued_shares"] = shares_out  # type: ignore
    return closes


def _tpex_margin_shares(d: date) -> dict[str, float]:
    roc = to_roc_slash(d)
    url = f"{TPEX_WEB}/stock/margin_trading/margin_balance/margin_bal_result.php"
    payload = get_json(url, params={"l": "zh-tw", "d": roc})
    if not tpex_ok(payload):
        # new path
        url2 = f"{TPEX_WWW}/margin/balance"
        payload = get_json(url2, params={"date": d.strftime("%Y/%m/%d"), "response": "json"})
    if not tpex_ok(payload):
        raise RuntimeError(str(payload.get("stat")))
    shares: dict[str, float] = {}
    for tbl in payload.get("tables") or []:
        fields = tbl.get("fields") or []
        i_code = next((i for i, f in enumerate(fields) if "代號" in str(f)), 0)
        i_sh = next((i for i, f in enumerate(fields) if str(f) in ("資餘額", "融資今日餘額")), None)
        if i_sh is None:
            # typical: 代號, 名稱, 前資餘額, 資買, 資賣, 現償, 資餘額
            i_sh = 6 if fields else 6
        for row in tbl.get("data") or []:
            code = str(row[i_code]).strip()
            sh = parse_num(row[i_sh])
            if code and sh is not None:
                shares[code] = sh
    return shares


def estimate_margin_maintenance(trade: date, twse_margin: dict, tpex_margin: dict) -> dict:
    """Estimate 上市/上櫃/整體 融資維持率 = 融資股票市值 / 融資金額.

    This is the commonly cited「大盤融資維持率」, NOT the official 整戶擔保維持率.
    Computed from official TWSE/TPEx 餘額 + 收盤, not from 玩股網.
    Wantgoo pages are reachable but their JSON API returned HTTP 400
    without documented parameters; we do not reverse-engineer it.
    """
    out: dict[str, Any] = {}
    method = (
        "融資維持率估算 = Σ(收盤價 × 融資餘額張數 × 1000) ÷ 官方融資金額 × 100%。"
        "含 ETF 等有融資餘額的證券；無收盤價者跳過。"
        "資料來源為證交所/櫃買官方餘額與收盤，非政府民間平台數字。"
        "與證交所整戶擔保維持率（BFIJ3U）口徑不同。"
    )
    # TWSE
    try:
        raw = twse_margin.get("_twse_margin_raw") or {}
        d = raw.get("date") or trade
        rec = raw.get("rec") or {}
        amt = rec.get("amt_today_k")  # 仟元
        if amt is None:
            raise RuntimeError("no official 融資金額")
        closes = _twse_closes(d)
        shares = _twse_margin_shares(d)
        mv = 0.0
        used = skipped = 0
        for code, sh in shares.items():
            px = closes.get(code)
            if px is None or sh <= 0:
                skipped += 1
                continue
            mv += px * sh * 1000.0  # 元
            used += 1
        denom = amt * 1000.0  # 仟元 -> 元
        ratio = mv / denom * 100.0 if denom else None
        src = f"{TWSE_RWD}/marginTrading/MI_MARGN + MI_INDEX ALLBUT0999"
        out["twse.margin_maint_est"] = field(
            round(ratio, 2) if ratio else None,
            src,
            unit="%",
            as_of=d.isoformat(),
            method=method,
            note=f"用了 {used} 檔、跳過 {skipped} 檔無收盤；金額分母為官方融資金額",
            stale=d != trade,
        )
        out["_twse_margin_mv"] = mv
        out["_twse_margin_amt"] = denom
    except Exception as e:
        out["twse.margin_maint_est"] = missing(
            f"cannot estimate yet: {e}",
            f"{TWSE_RWD}/marginTrading/MI_MARGN",
            as_of=trade.isoformat(),
        )

    # TPEX
    try:
        raw = tpex_margin.get("_tpex_margin_raw") or {}
        d = raw.get("date") or trade
        bal_yi = raw.get("balance")  # 億元
        if bal_yi is None:
            raise RuntimeError("no official 上櫃融資餘額金額")
        closes_pack = _tpex_closes(d)
        issued = closes_pack.pop("_issued_shares", {}) if "_issued_shares" in closes_pack else {}
        shares = _tpex_margin_shares(d)
        mv = 0.0
        used = skipped = 0
        for code, sh in shares.items():
            px = closes_pack.get(code)
            if px is None or sh <= 0:
                skipped += 1
                continue
            mv += px * sh * 1000.0
            used += 1
        denom = bal_yi * 1e8
        ratio = mv / denom * 100.0 if denom else None
        src = f"{TPEX_WEB}/stock/margin_trading/margin_balance/margin_bal_result.php + dailyQuotes + dashboardOtc/margin"
        out["tpex.margin_maint_est"] = field(
            round(ratio, 2) if ratio else None,
            src,
            unit="%",
            as_of=d.isoformat(),
            method=method,
            note=f"用了 {used} 檔、跳過 {skipped} 檔無收盤；金額分母為櫃買儀表板融資餘額",
            stale=d != trade,
        )
        out["_tpex_margin_mv"] = mv
        out["_tpex_margin_amt"] = denom
        out["_tpex_issued_shares"] = issued
        out["_tpex_closes"] = {k: v for k, v in closes_pack.items()}
    except Exception as e:
        out["tpex.margin_maint_est"] = missing(f"cannot estimate yet: {e}", f"{TPEX_WWW}/dashboardOtc/margin", as_of=trade.isoformat())

    try:
        mv = (out.get("_twse_margin_mv") or 0) + (out.get("_tpex_margin_mv") or 0)
        amt = (out.get("_twse_margin_amt") or 0) + (out.get("_tpex_margin_amt") or 0)
        if amt and out.get("_twse_margin_mv") is not None and out.get("_tpex_margin_mv") is not None:
            d_tw = (twse_margin.get("_twse_margin_raw") or {}).get("date")
            d_ot = (tpex_margin.get("_tpex_margin_raw") or {}).get("date")
            as_of = (d_tw or trade).isoformat() if d_tw else trade.isoformat()
            if d_tw and d_ot:
                as_of = min(d_tw, d_ot).isoformat()
            stale = as_of != trade.isoformat()
            out["all.margin_maint_est"] = field(
                round(mv / amt * 100.0, 2),
                "TWSE MI_MARGN + TPEx margin/quotes (computed)",
                unit="%",
                as_of=as_of,
                method=method,
                note="兩市融資市值加總 ÷ 兩市融資金額加總（金額加權，非兩率平均）",
                stale=stale,
            )
        else:
            out["all.margin_maint_est"] = missing("need both markets", None, as_of=trade.isoformat())
    except Exception as e:
        out["all.margin_maint_est"] = missing(str(e), None, as_of=trade.isoformat())
    return out


# ---------------------------------------------------------------------------
# 三大法人買賣超：上市 BFI82U、上櫃「三大法人買賣金額彙總」
# 單日 / 近3日 / 近5日（一週） / 近一月（20 個交易日）；日資料寫入 cache
# 既有欄位 twse.foreign_net、twse.foreign_net_5d 維持不變
# ---------------------------------------------------------------------------

class NoDataError(RuntimeError):
    pass


INST_CACHE_TWSE = CACHE / "institutional" / "twse.json"
INST_CACHE_TPEX = CACHE / "institutional" / "tpex.json"
INST_CATS = ("foreign", "trust", "dealer", "total")
INST_WINDOWS: tuple[tuple[str, int, str], ...] = (
    ("1d", 1, "單日"),
    ("3d", 3, "近3日"),
    ("5d", 5, "近5日（一週）"),
    ("20d", 20, "近一月（20 個交易日）"),
)


def twse_inst_src(d: date) -> str:
    return f"{TWSE_RWD}/fund/BFI82U?response=json&dayDate={d.strftime('%Y%m%d')}&type=day"


def tpex_inst_src(d: date) -> str:
    return (
        f"{TPEX_WEB}/stock/3insti/3insti_summary/3itrdsum_result.php"
        f"?l=zh-tw&d={to_roc_slash(d)}&o=json"
    )


def fetch_bfi82u(d: date) -> dict:
    ymd = d.strftime("%Y%m%d")
    url = f"{TWSE_RWD}/fund/BFI82U"
    payload = get_json(url, params={"response": "json", "dayDate": ymd, "type": "day"})
    if not twse_ok(payload):
        raise NoDataError(str(payload.get("stat") or "no data"))
    return payload


def fetch_tpex_inst_summary(d: date) -> dict:
    url = f"{TPEX_WEB}/stock/3insti/3insti_summary/3itrdsum_result.php"
    return get_json(url, params={"l": "zh-tw", "d": to_roc_slash(d), "o": "json"})


def _norm_inst_name(name: Any) -> str:
    s = str(name or "").replace("\u3000", "").replace("　", "").strip()
    return s


def parse_institutional_rows(rows: list) -> Optional[dict[str, float]]:
    """Parse 外資/投信/自營商合計/合計 買賣超，單位為元。欄位不足則回傳 None。"""
    by: dict[str, float] = {}
    dealer_parts: list[float] = []
    for row in rows or []:
        if not row:
            continue
        name = _norm_inst_name(row[0])
        net = parse_num(row[3] if len(row) > 3 else None)
        if net is None or not name:
            continue
        if name in ("外資及陸資(不含外資自營商)", "外資及陸資(不含自營商)"):
            by["foreign"] = net
        elif name in ("外資及陸資合計", "外資及陸資") and "foreign" not in by:
            by["foreign"] = net
        elif name.startswith("外資及陸資") and "自營" not in name and "foreign" not in by:
            by["foreign"] = net
        elif name == "投信" or (name.startswith("投信") and "自營" not in name):
            by["trust"] = net
        elif name in ("自營商合計", "自營商"):
            by["dealer"] = net
        elif name in ("自營商(自行買賣)", "自營商(避險)"):
            dealer_parts.append(net)
        elif name in ("合計", "三大法人合計", "三大法人合計*"):
            by["total"] = net
    if "dealer" not in by and dealer_parts:
        by["dealer"] = sum(dealer_parts)
    if not any(k in by for k in INST_CATS):
        return None
    return by


def parse_foreign_net(payload: dict) -> Optional[float]:
    """Return 外資及陸資(不含外資自營商) 買賣差額 in 元."""
    parsed = parse_institutional_rows(payload.get("data") or [])
    return None if not parsed else parsed.get("foreign")


def _yi(v: Optional[float]) -> Optional[float]:
    if v is None:
        return None
    return round(v / 1e8, 2)


def _parse_twse_inst(payload: dict, requested: date) -> Optional[dict[str, Any]]:
    cats = parse_institutional_rows(payload.get("data") or [])
    if not cats:
        return None
    as_of = parse_date_any(payload.get("date")) or requested
    return {"as_of": as_of, "cats": cats}


def _parse_tpex_inst(payload: dict, requested: date) -> Optional[dict[str, Any]]:
    tables = payload.get("tables") or []
    tbl = tables[0] if tables else {}
    rows = (tbl.get("data") if isinstance(tbl, dict) else None) or []
    if not rows:
        return None
    cats = parse_institutional_rows(rows)
    if not cats:
        return None
    as_of = parse_date_any(payload.get("date") or tbl.get("date")) or requested
    return {"as_of": as_of, "cats": cats}


def _cache_inst_empty(cache: dict, iso: str, d: date, have_later: bool) -> bool:
    """Cache a confirmed non-session. Never cache 'maybe not published yet'."""
    today = today_tpe()
    if d >= today:
        return False
    if d.weekday() >= 5 or have_later:
        cache[iso] = {"empty": True}
        return True
    return False


def collect_inst_history(market: str, trade: date, need: int = 20) -> tuple[list[tuple[date, dict[str, Optional[float]], str]], str]:
    """Newest-first list of (as_of, cats in 元, source). Fills cache/institutional/."""
    if market == "twse":
        path = INST_CACHE_TWSE
        src_fn = twse_inst_src

        def fetch_one(d: date) -> Optional[dict[str, Any]]:
            return _parse_twse_inst(fetch_bfi82u(d), d)

    else:
        path = INST_CACHE_TPEX
        src_fn = tpex_inst_src

        def fetch_one(d: date) -> Optional[dict[str, Any]]:
            payload = fetch_tpex_inst_summary(d)
            return _parse_tpex_inst(payload, d)

    raw = cache_load_json(path)
    cache: dict[str, Any] = raw if isinstance(raw, dict) else {}
    sessions: list[tuple[date, dict[str, Optional[float]], str]] = []
    d = trade
    tried = 0
    dirty = False
    while len(sessions) < need and tried < 60:
        tried += 1
        iso = d.isoformat()
        cached = cache.get(iso)
        if isinstance(cached, dict) and cached.get("empty"):
            d -= timedelta(days=1)
            continue
        if isinstance(cached, dict) and any(k in cached for k in INST_CATS):
            as_of = parse_date_any(cached.get("as_of")) or d
            cats = {k: cached.get(k) for k in INST_CATS}
            sessions.append((as_of, cats, cached.get("source") or src_fn(d)))
            d -= timedelta(days=1)
            continue

        parsed: Optional[dict[str, Any]] = None
        confirmed_empty = False
        fetched = False
        for attempt in range(4):
            try:
                parsed = fetch_one(d)
                fetched = True
                confirmed_empty = parsed is None
                break
            except NoDataError:
                fetched = True
                confirmed_empty = True
                parsed = None
                break
            except Exception:
                time.sleep(1.0 * (attempt + 1))
        if not fetched:
            d -= timedelta(days=1)
            continue

        if parsed and parsed.get("cats"):
            as_of: date = parsed["as_of"] or d
            rec: dict[str, Any] = {k: parsed["cats"].get(k) for k in INST_CATS}
            rec["as_of"] = as_of.isoformat()
            rec["source"] = src_fn(d)
            # If the official payload is for a different date, store under as_of
            # and mark the requested date empty when it is a confirmed non-session.
            cache[as_of.isoformat()] = rec
            if as_of != d:
                cache[iso] = {"empty": True}
            dirty = True
            # Avoid inserting the same session twice if the API returned a later date.
            if not any(s[0] == as_of for s in sessions):
                sessions.append((as_of, {k: rec[k] for k in INST_CATS}, rec["source"]))
        elif confirmed_empty:
            if _cache_inst_empty(cache, iso, d, have_later=bool(sessions)):
                dirty = True
        d -= timedelta(days=1)
        if fetched:
            time.sleep(0.12)

    if dirty:
        cache_save_json(path, cache)
    return sessions, (sessions[0][2] if sessions else src_fn(trade))


def emit_inst_fields(
    prefix: str,
    sessions: list[tuple[date, dict[str, Optional[float]], str]],
    trade: date,
    source: str,
    note_map: dict[str, str],
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not sessions:
        for cat in INST_CATS:
            for key, _nwin, _label in INST_WINDOWS:
                out[f"{prefix}.{cat}_{key}"] = missing("not yet published", source, as_of=trade.isoformat())
        return out
    last_d, _last_cats, last_src = sessions[0]
    stale = last_d != trade
    for cat in INST_CATS:
        cat_note = note_map.get(cat, "")
        for key, nwin, label in INST_WINDOWS:
            field_key = f"{prefix}.{cat}_{key}"
            if len(sessions) < nwin:
                out[field_key] = missing(
                    f"only {len(sessions)} sessions available",
                    last_src,
                    as_of=last_d.isoformat(),
                )
                continue
            window = sessions[:nwin]
            if any(s[1].get(cat) is None for s in window):
                out[field_key] = missing(f"missing {cat} in window", last_src, as_of=last_d.isoformat())
                continue
            total = sum(s[1][cat] or 0.0 for s in window)
            note = f"{cat_note}；{label}為最近 {nwin} 個已公布交易日加總；負值=賣超，單位由元換算億元"
            if nwin == 1:
                note = f"{cat_note}；單位由元換算億元；負值=賣超"
            if stale:
                note += f"；{trade.isoformat()} 尚未公布，先用 {last_d.isoformat()}"
            extra = None
            if nwin > 1:
                extra = {
                    "sessions": [
                        {"date": a.isoformat(), "yi": _yi(b.get(cat))}
                        for a, b, _ in window
                    ]
                }
            out[field_key] = field(
                _yi(total),
                last_src,
                unit="億元",
                as_of=last_d.isoformat(),
                note=note,
                stale=stale,
                extra=extra,
            )
    return out


def fetch_institutional(trade: date) -> dict:
    """上市 + 上櫃 三大法人買賣超，並維持既有 twse.foreign_net / twse.foreign_net_5d。"""
    out: dict[str, Any] = {}
    # 兩個市場並行回填快取
    twse_sessions: list = []
    tpex_sessions: list = []
    twse_src = twse_inst_src(trade)
    tpex_src = tpex_inst_src(trade)
    errors: list[str] = []

    def _one(market: str):
        return collect_inst_history(market, trade, need=20)

    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(_one, m): m for m in ("twse", "tpex")}
        for fut, m in futs.items():
            try:
                sessions, src = fut.result()
                if m == "twse":
                    twse_sessions, twse_src = sessions, src
                else:
                    tpex_sessions, tpex_src = sessions, src
            except Exception as e:
                errors.append(f"{m}: {e}")

    twse_notes = {
        "foreign": "外資及陸資(不含外資自營商)",
        "trust": "投信",
        "dealer": "自營商合計（自行買賣＋避險）",
        "total": "三大法人合計（外資自營商已計入自營商，不重複加總）",
    }
    tpex_notes = {
        "foreign": "外資及陸資(不含自營商)",
        "trust": "投信",
        "dealer": "自營商合計",
        "total": "三大法人合計（外資自營商已計入自營商，不重複加總）",
    }
    out.update(emit_inst_fields("twse.inst", twse_sessions, trade, twse_src, twse_notes))
    out.update(emit_inst_fields("tpex.inst", tpex_sessions, trade, tpex_src, tpex_notes))

    # backward-compatible fields (same definition as before)
    if twse_sessions and twse_sessions[0][1].get("foreign") is not None:
        last_d, cats, last_src = twse_sessions[0]
        stale = last_d != trade
        note = "外資及陸資(不含外資自營商)，單位由元換算億元"
        if stale:
            note += f"；{trade.isoformat()} 尚未公布，先用 {last_d.isoformat()}"
        out["twse.foreign_net"] = field(
            _yi(cats["foreign"]), last_src, unit="億元", as_of=last_d.isoformat(), note=note, stale=stale
        )
        if len(twse_sessions) >= 5 and all(s[1].get("foreign") is not None for s in twse_sessions[:5]):
            s5 = sum(s[1]["foreign"] or 0.0 for s in twse_sessions[:5])
            out["twse.foreign_net_5d"] = field(
                _yi(s5),
                last_src,
                unit="億元",
                as_of=last_d.isoformat(),
                note="最近 5 個已公布交易日（含最新一日）外資買賣超加總；負值=賣超",
                stale=stale,
                extra={"sessions": [{"date": a.isoformat(), "yi": _yi(b.get("foreign"))} for a, b, _ in twse_sessions[:5]]},
            )
        else:
            out["twse.foreign_net_5d"] = missing(
                f"only {len(twse_sessions)} sessions available",
                twse_src,
                as_of=last_d.isoformat(),
            )
    else:
        out["twse.foreign_net"] = missing("BFI82U not yet published", twse_src, as_of=trade.isoformat())
        out["twse.foreign_net_5d"] = missing("need 5 sessions", twse_src, as_of=trade.isoformat())
    if errors:
        # surface but do not abort the other market's fields
        out.setdefault("_institutional_errors", errors)
    return out


def fetch_foreign(trade: date) -> dict:
    """Back-compat wrapper around fetch_institutional."""
    return fetch_institutional(trade)


# ---------------------------------------------------------------------------
# TAIFEX 外資台指期 空單 / 淨空單
# ---------------------------------------------------------------------------

def fetch_taifex(trade: date) -> dict:
    url = f"{TAIFEX_OPENAPI}/MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate"
    out: dict[str, Any] = {}
    try:
        payload = get_json(url)
        if not isinstance(payload, list) or not payload:
            raise RuntimeError("empty TAIFEX payload")
        # OpenAPI is a latest snapshot (usually previous session until ~15:00)
        rows = [r for r in payload if str(r.get("ContractCode") or "") in ("臺股期貨", "TX", "TXF")]
        if not rows:
            raise RuntimeError("no 臺股期貨 rows")
        as_of = to_iso(rows[0].get("Date"))
        stale = as_of != trade.isoformat()
        foreign = None
        for r in rows:
            item = str(r.get("Item") or "")
            if "外資" in item:
                foreign = r
                break
        if not foreign:
            raise RuntimeError("no 外資 row for 臺股期貨")
        short_oi = parse_num(foreign.get("OpenInterest(Short)"))
        long_oi = parse_num(foreign.get("OpenInterest(Long)"))
        net = None
        if long_oi is not None and short_oi is not None:
            net = long_oi - short_oi  # negative => net short
        net_short = -net if net is not None else None
        note = "臺股期貨（TX）外資未平倉；淨空單 = 空單 − 多單（正值代表淨空）"
        if stale:
            note += f"；{trade.isoformat()} 尚未公布（期交所約 15:00 後更新），先用 {as_of}"
        out["taifex.tx_foreign_short_oi"] = field(short_oi, url, unit="口", as_of=as_of, note=note, stale=stale)
        out["taifex.tx_foreign_long_oi"] = field(long_oi, url, unit="口", as_of=as_of, stale=stale)
        out["taifex.tx_foreign_net_oi"] = field(net, url, unit="口", as_of=as_of, note="多單 − 空單，負值=淨空", stale=stale)
        out["taifex.tx_foreign_net_short"] = field(net_short, url, unit="口", as_of=as_of, note="空單 − 多單；正值=淨空單口數", stale=stale)
    except Exception as e:
        out["taifex.tx_foreign_short_oi"] = missing(str(e), url, as_of=trade.isoformat())
        out["taifex.tx_foreign_net_short"] = missing(str(e), url, as_of=trade.isoformat())
    return out


# ---------------------------------------------------------------------------
# 當沖比率
# ---------------------------------------------------------------------------

def fetch_daytrade_twse(trade: date) -> dict:
    src = f"{TWSE_RWD}/dayTrading/TWTB4U?date={trade.strftime('%Y%m%d')}&selectType=All&response=json"
    out: dict[str, Any] = {}
    try:
        d = trade
        payload = None
        for _ in range(8):
            try:
                p = get_json(f"{TWSE_RWD}/dayTrading/TWTB4U", params={"date": d.strftime("%Y%m%d"), "selectType": "All", "response": "json"})
                if twse_ok(p) and first_filled_table(p, require="占市場比重"):
                    payload = p
                    break
            except Exception:
                pass
            d -= timedelta(days=1)
        if not payload:
            raise RuntimeError("TWTB4U not yet published (typically ~20:00)")
        used = parse_date_any(payload.get("date")) or d
        stale = used != trade
        tbl = first_filled_table(payload, require="占市場比重")
        fields = tbl.get("fields") or []
        row = (tbl.get("data") or [None])[0]
        rec = dict(zip(fields, row or []))
        # 金額占比 is the commonly cited 當沖比率
        amt_pct = parse_num(rec.get("當日沖銷交易總買進成交金額占市場比重%"))
        vol_pct = parse_num(rec.get("當日沖銷交易總成交股數占市場比重%"))
        note = "常用口徑為「當日沖銷交易總買進成交金額占市場比重%」。T 日晚間約 20:00 初值，T+2 才定稿。"
        if stale:
            note += f"；{trade.isoformat()} 尚未公布，先用 {used.isoformat()}"
        src_u = f"{TWSE_RWD}/dayTrading/TWTB4U?date={used.strftime('%Y%m%d')}&selectType=All&response=json"
        out["twse.daytrade_amt_ratio"] = field(amt_pct, src_u, unit="%", as_of=used.isoformat(), note=note, stale=stale)
        out["twse.daytrade_vol_ratio"] = field(vol_pct, src_u, unit="%", as_of=used.isoformat(), note="成交股數占市場比重", stale=stale)
    except Exception as e:
        out["twse.daytrade_amt_ratio"] = missing(str(e), src, as_of=trade.isoformat())
        out["twse.daytrade_vol_ratio"] = missing(str(e), src, as_of=trade.isoformat())
    return out


def fetch_daytrade_tpex(trade: date) -> dict:
    src = f"{TPEX_WEB}/stock/trading/intraday_stat/intraday_trading_stat_result.php?l=zh-tw&d={to_roc_slash(trade)}"
    out: dict[str, Any] = {}
    try:
        d = trade
        payload = None
        for _ in range(8):
            try:
                p = get_json(
                    f"{TPEX_WEB}/stock/trading/intraday_stat/intraday_trading_stat_result.php",
                    params={"l": "zh-tw", "d": to_roc_slash(d)},
                )
                if tpex_ok(p) and first_filled_table(p, require="占市場比重"):
                    payload = p
                    break
            except Exception:
                pass
            d -= timedelta(days=1)
        if not payload:
            raise RuntimeError("TPEx 現股當沖統計尚未公布（晚間）")
        used = parse_date_any(payload.get("date")) or d
        stale = used != trade
        tbl = first_filled_table(payload, require="占市場比重")
        fields = tbl.get("fields") or []
        row = (tbl.get("data") or [None])[0]
        rec = dict(zip(fields, row or []))
        amt_pct = parse_num(rec.get("當日沖銷交易總買進成交金額占市場比重"))
        vol_pct = parse_num(rec.get("當日沖銷交易總成交股數占市場比重"))
        note = "上櫃現股當沖；常用口徑為買進成交金額占市場比重。晚間公布，T+2 定稿。"
        if stale:
            note += f"；{trade.isoformat()} 尚未公布，先用 {used.isoformat()}"
        src_u = f"{TPEX_WEB}/stock/trading/intraday_stat/intraday_trading_stat_result.php?l=zh-tw&d={to_roc_slash(used)}"
        out["tpex.daytrade_amt_ratio"] = field(amt_pct, src_u, unit="%", as_of=used.isoformat(), note=note, stale=stale)
        out["tpex.daytrade_vol_ratio"] = field(vol_pct, src_u, unit="%", as_of=used.isoformat(), stale=stale)
    except Exception as e:
        out["tpex.daytrade_amt_ratio"] = missing(str(e), src, as_of=trade.isoformat())
        out["tpex.daytrade_vol_ratio"] = missing(str(e), src, as_of=trade.isoformat())
    return out




# ---------------------------------------------------------------------------
# 庫藏股 announcements
# Primary source: MOPS「庫藏股統計彙總表」t35sc09（上市/上櫃、依董事會決議日）
# t187ap04 重大訊息快照只作同日補漏，不再當歷史家數來源
# ---------------------------------------------------------------------------

MOPS_HOSTS = (
    "https://mopsc.twse.com.tw",
    "https://mopsov.twse.com.tw",
)
BUYBACK_PURPOSE = {
    "1": "轉讓股份予員工",
    "2": "股權轉換",
    "3": "維護公司信用及股東權益",
}
BUYBACK_NEW = re.compile(
    r"(董事會.*決議.*買回|決議買回.*(庫藏|股份)|買回本公司股份|辦理庫藏股買回|擬買回庫藏股|決議辦理庫藏股)"
)
BUYBACK_EXCLUDE = re.compile(
    r"(執行完畢|期間屆滿|註銷|減資基準|轉讓予員工|變更登記|更正實際|轉讓辦法|已買回股份|庫藏股註銷)"
)


def _is_new_buyback(title: str, body: str = "") -> bool:
    text = (title or "") + " " + (body or "")
    if BUYBACK_EXCLUDE.search(title or ""):
        return False
    return bool(BUYBACK_NEW.search(text) or ("庫藏股" in (title or "") and "決議買回" in text))


def to_roc_ymd(d: date) -> str:
    return f"{d.year - 1911:03d}{d.month:02d}{d.day:02d}"


def _html_table_rows(html: str) -> list[tuple[str, list[list[str]]]]:
    """Return (css_class, rows) for each HTML table. Cells join <br> with |."""

    class Parser(HTMLParser):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__()
            self.tables: list[tuple[str, list[list[str]]]] = []
            self.cur: Optional[list[list[str]]] = None
            self.cls = ""
            self.row: Optional[list[str]] = None
            self.cell: Optional[str] = None

        def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
            ad = dict(attrs)
            if tag == "table":
                self.cls = ad.get("class") or ""
                self.cur = []
                self.tables.append((self.cls, self.cur))
            elif tag == "tr" and self.cur is not None:
                self.row = []
                self.cur.append(self.row)
            elif tag in ("td", "th") and self.row is not None:
                self.cell = ""
            elif tag == "br" and self.cell is not None:
                self.cell += "|"

        def handle_endtag(self, tag: str) -> None:
            if tag in ("td", "th") and self.cell is not None and self.row is not None:
                self.row.append(re.sub(r"\s+", " ", self.cell).strip())
                self.cell = None
            if tag == "table":
                self.cur = None
                self.row = None

        def handle_data(self, data: str) -> None:
            if self.cell is not None:
                self.cell += data

    cleaned = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.S | re.I)
    p = Parser()
    p.feed(cleaned)
    return p.tables


def _parse_t35sc09(html: str, market: str) -> list[dict]:
    """Parse 上市/上櫃公司買回自己公司股份彙總統計表 rows."""
    rows_out: list[dict] = []
    for cls, rows in _html_table_rows(html):
        if "hasBorder" not in (cls or "") and not any(
            any("董事會" in c and "決議" in c for c in r) for r in rows[:2]
        ):
            continue
        for rec in rows:
            if len(rec) < 11:
                continue
            code = re.sub(r"\s+", "", rec[1])
            if not re.fullmatch(r"\d{3,6}[A-Za-z*]?", code):
                continue
            board = parse_date_any(rec[3])
            if board is None:
                continue
            planned = parse_num(rec[6])
            price_lo = parse_num(rec[7])
            price_hi = parse_num(rec[8])
            start = parse_date_any(rec[9])
            end = parse_date_any(rec[10])
            purpose_code = str(rec[4] or "").strip()
            done_raw = str(rec[11] if len(rec) > 11 else "").strip().upper()
            rows_out.append(
                {
                    "market": market,
                    "market_zh": "上市" if market == "sii" else "上櫃",
                    "code": code,
                    "name": rec[2].replace("|", "").strip(),
                    "date": board.isoformat(),
                    "purpose_code": purpose_code,
                    "purpose": BUYBACK_PURPOSE.get(purpose_code, purpose_code),
                    "legal_amount_cap": parse_num(rec[5]),
                    "planned_shares": planned,
                    "planned_lots": int(planned / 1000) if planned is not None else None,
                    "price_low": price_lo,
                    "price_high": price_hi,
                    "period_start": start.isoformat() if start else None,
                    "period_end": end.isoformat() if end else None,
                    "done": done_raw in {"Y", "是"},
                }
            )
    # de-dup by market+code+board date
    uniq: dict[tuple, dict] = {}
    for r in rows_out:
        uniq[(r["market"], r["code"], r["date"])] = r
    return list(uniq.values())


def _mops_session(host: str) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        }
    )
    return s


def _mops_blocked(text: str) -> bool:
    return "FOR SECURITY REASONS" in (text or "") or "因為安全性考量" in (text or "")


def fetch_t35sc09_market(trade: date, start: date, market: str) -> tuple[list[dict], str]:
    """POST MOPS ajax_t35sc09 for one market. Returns (rows, source_url)."""
    data = {
        "encodeURIComponent": "1",
        "step": "1",
        "firstin": "1",
        "off": "1",
        "TYPEK": market,
        "RD": "1",
        "d1": to_roc_ymd(start),
        "d2": to_roc_ymd(trade),
    }
    errors: list[str] = []
    for host in MOPS_HOSTS:
        form = f"{host}/mops/web/t35sc09"
        ajax = f"{host}/mops/web/ajax_t35sc09"
        for attempt in range(RETRIES):
            s = _mops_session(host)
            try:
                r0 = s.get(form, timeout=TIMEOUT, headers={"Referer": f"{host}/"})
                if r0.status_code >= 400 or _mops_blocked(r0.text):
                    errors.append(f"{form}: HTTP {r0.status_code} blocked={_mops_blocked(r0.text)}")
                    time.sleep(0.8 * (attempt + 1))
                    continue
                r = s.post(
                    ajax,
                    data=data,
                    timeout=max(TIMEOUT, 40),
                    headers={
                        "Referer": form,
                        "Origin": host,
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
                if r.status_code != 200 or _mops_blocked(r.text) or "買回自己公司股份" not in r.text:
                    errors.append(
                        f"{ajax}: HTTP {r.status_code} blocked={_mops_blocked(r.text)} len={len(r.content)}"
                    )
                    time.sleep(0.8 * (attempt + 1))
                    continue
                rows = _parse_t35sc09(r.text, market)
                src = (
                    f"{ajax}?TYPEK={market}&d1={data['d1']}&d2={data['d2']}&RD=1"
                )
                return rows, src
            except Exception as e:
                errors.append(f"{ajax} attempt {attempt + 1}: {e}")
                time.sleep(1.2 * (attempt + 1))
                continue
        time.sleep(0.6)
    raise RuntimeError(f"t35sc09 {market} failed: " + "; ".join(errors[:6]))


def trading_days_upto(trade: date, months_back: int = 6) -> list[date]:
    """Official TWSE trading days through `trade`, from FMTQIK monthly cache."""
    days: list[date] = []
    y, m = trade.year, trade.month
    for _ in range(months_back):
        try:
            rows = fetch_fmtqik_month(date(y, m, 1))
        except Exception:
            rows = []
        days.extend(d for d, _ in rows if d <= trade)
        m -= 1
        if m == 0:
            y -= 1
            m = 12
    return sorted(set(days))


def _in_trading_window(d: Optional[date], window: list[date]) -> bool:
    if d is None or not window:
        return False
    return window[0] <= d <= window[-1]


def _uniq_companies(rows: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for r in sorted(rows, key=lambda x: x.get("date") or ""):
        seen[str(r.get("code"))] = r
    return list(seen.values())


def _buyback_brief(r: dict) -> dict:
    return {
        "date": r.get("date"),
        "market": r.get("market_zh") or r.get("market"),
        "code": r.get("code"),
        "name": r.get("name"),
        "purpose": r.get("purpose"),
        "planned_shares": r.get("planned_shares"),
        "planned_lots": r.get("planned_lots"),
        "price_low": r.get("price_low"),
        "price_high": r.get("price_high"),
        "period_start": r.get("period_start"),
        "period_end": r.get("period_end"),
        "legal_amount_cap": r.get("legal_amount_cap"),
        "done": r.get("done"),
    }


def fetch_buybacks(trade: date) -> dict:
    """Board-resolution buybacks from official MOPS t35sc09, cached per trade date."""
    start = trade - timedelta(days=130)
    cache_file = cache_path("buybacks", f"t35sc09_{trade.isoformat()}.json")
    rows: list[dict] = []
    sources: list[str] = []
    errors: list[str] = []
    cached = cache_load_json(cache_file)
    actual_start = start
    cache_ok = (
        isinstance(cached, dict)
        and cached.get("trade") == trade.isoformat()
        and isinstance(cached.get("rows"), list)
        and cached.get("rows")
    )

    def _cache_fresh(doc: dict) -> bool:
        # Re-fetch later the same day so evening filings are picked up
        # by the nightly routine; keep a short TTL so repeated runs stay cheap.
        raw = doc.get("fetched_at")
        if not raw:
            return False
        try:
            dt = datetime.fromisoformat(str(raw))
        except ValueError:
            return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TPE)
        return (now_tpe() - dt) < timedelta(hours=2)

    if cache_ok and _cache_fresh(cached):
        rows = cached["rows"]
        sources = list(cached.get("sources") or [])
        cached_start = parse_date_any(cached.get("start"))
        if cached_start:
            actual_start = cached_start
    else:
        for market in ("sii", "otc"):
            try:
                part, src = fetch_t35sc09_market(trade, start, market)
                rows.extend(part)
                sources.append(src)
                time.sleep(0.7)
            except Exception as e:
                errors.append(str(e))
        if rows:
            cache_save_json(
                cache_file,
                {
                    "trade": trade.isoformat(),
                    "start": start.isoformat(),
                    "fetched_at": now_tpe().isoformat(),
                    "sources": sources,
                    "rows": rows,
                    "errors": errors,
                },
            )
        elif cache_ok:
            # Network miss: keep today's earlier snapshot rather than going blank.
            rows = cached["rows"]
            sources = list(cached.get("sources") or [])
            errors.append("t35sc09 refresh failed; used earlier same-day cache")
            cached_start = parse_date_any(cached.get("start"))
            if cached_start:
                actual_start = cached_start

    src = sources[0] if sources else f"{MOPS_HOSTS[0]}/mops/web/t35sc09"
    note_base = (
        "公開資訊觀測站「庫藏股統計彙總表」（ajax_t35sc09），依董事會決議日；"
        "上市+上櫃。金額上限為法定買回總金額上限，不是預定買回金額。"
        "預定買回股數單位為股（planned_lots 為張）。"
        f" 查詢區間 {actual_start.isoformat()}～{trade.isoformat()}。"
    )
    if errors:
        note_base += " 部分來源失敗: " + "; ".join(errors)

    if not rows and errors:
        reason = "; ".join(errors)
        keys = [
            "buybacks.new_today",
            "buybacks.new_1d",
            "buybacks.new_5d",
            "buybacks.new_20d",
            "buybacks.mtd",
            "buybacks.recent",
            "buybacks.list_5d",
        ]
        return {k: missing(reason, src, as_of=trade.isoformat()) for k in keys}

    def on_date(d: date) -> list[dict]:
        return [r for r in rows if r.get("date") == d.isoformat()]

    tdays = trading_days_upto(trade)
    win5 = tdays[-5:] if len(tdays) >= 5 else tdays
    win20 = tdays[-20:] if len(tdays) >= 20 else tdays
    month_prefix = trade.strftime("%Y-%m")
    start_30 = trade - timedelta(days=30)

    day_rows = on_date(trade)
    rows_5 = [r for r in rows if _in_trading_window(parse_date_any(r.get("date")), win5)]
    rows_20 = [r for r in rows if _in_trading_window(parse_date_any(r.get("date")), win20)]
    mtd_rows = [r for r in rows if str(r.get("date") or "").startswith(month_prefix)]
    recent_rows = [
        r
        for r in rows
        if (parse_date_any(r.get("date")) or date.min) >= start_30
        and (parse_date_any(r.get("date")) or date.max) <= trade
    ]

    # daily case counts for trend notes (board date)
    by_day: dict[str, int] = {}
    by_day_cos: dict[str, set[str]] = {}
    for r in rows:
        d = r.get("date") or ""
        by_day[d] = by_day.get(d, 0) + 1
        by_day_cos.setdefault(d, set()).add(str(r.get("code")))
    daily = [
        {"date": d, "cases": by_day[d], "companies": len(by_day_cos[d])}
        for d in sorted(by_day)
        if parse_date_any(d) and parse_date_any(d) >= start_30  # type: ignore[operator]
    ]

    def count_note(label: str, subset: list[dict], window_desc: str) -> str:
        names = [f"{r.get('code')}{r.get('name')}" for r in subset]
        n_co = len({r.get("code") for r in subset})
        n_case = len(subset)
        extra = ""
        if n_case != n_co:
            extra = f"（{n_case} 件、{n_co} 家）"
        listed = f" 名單: {', '.join(names)}" if names else " 無新決議"
        return f"{label}{extra}；{window_desc}。{note_base}{listed}"

    list_5d = [_buyback_brief(r) for r in sorted(rows_5, key=lambda x: (x.get("date") or ""), reverse=True)]
    win5_desc = f"近5個交易日 {win5[0].isoformat()}～{win5[-1].isoformat()}" if win5 else "近5個交易日"
    win20_desc = f"近20個交易日 {win20[0].isoformat()}～{win20[-1].isoformat()}" if win20 else "近20個交易日"

    out = {
        "buybacks.new_today": field(
            len({r.get("code") for r in day_rows}),
            src,
            unit="家",
            as_of=trade.isoformat(),
            note=count_note("當日董事會決議買回", day_rows, f"決議日={trade.isoformat()}"),
            extra={"cases": len(day_rows), "list": [_buyback_brief(r) for r in day_rows]},
        ),
        "buybacks.new_1d": field(
            len({r.get("code") for r in day_rows}),
            src,
            unit="家",
            as_of=trade.isoformat(),
            note=count_note("單日新宣布", day_rows, f"決議日={trade.isoformat()}"),
            extra={"cases": len(day_rows), "list": [_buyback_brief(r) for r in day_rows]},
        ),
        "buybacks.new_5d": field(
            len({r.get("code") for r in rows_5}),
            src,
            unit="家",
            as_of=trade.isoformat(),
            note=count_note("近5個交易日新宣布", rows_5, win5_desc),
            extra={"cases": len(rows_5), "sessions": daily, "window": [d.isoformat() for d in win5]},
        ),
        "buybacks.new_20d": field(
            len({r.get("code") for r in rows_20}),
            src,
            unit="家",
            as_of=trade.isoformat(),
            note=count_note("近20個交易日新宣布", rows_20, win20_desc),
            extra={"cases": len(rows_20), "window": [d.isoformat() for d in win20]},
        ),
        "buybacks.mtd": field(
            len({r.get("code") for r in mtd_rows}),
            src,
            unit="家",
            as_of=trade.isoformat(),
            note=count_note("本月至今（同一家只計一次）", _uniq_companies(mtd_rows), f"月份 {month_prefix}"),
            extra={"cases": len(mtd_rows)},
        ),
        "buybacks.recent": field(
            len({r.get("code") for r in recent_rows}),
            src,
            unit="家",
            as_of=trade.isoformat(),
            note=count_note("近30日曆日內宣布買回的公司（去重）", _uniq_companies(recent_rows), "近30日曆日"),
            extra={"cases": len(recent_rows)},
        ),
        "buybacks.list_5d": field(
            list_5d,
            src,
            unit=None,
            as_of=trade.isoformat(),
            note=f"近5個交易日董事會決議買回明細。{win5_desc}。{note_base}",
            extra={"count": len(list_5d)},
        ),
    }
    return out


# ---------------------------------------------------------------------------
# International indices (Yahoo Finance)
# ---------------------------------------------------------------------------

INTL = {
    "sox": ("^SOX", "費半 SOX"),
    "nasdaq": ("^IXIC", "NASDAQ"),
    "sp500": ("^GSPC", "S&P500"),
    "dow": ("^DJI", "道瓊"),
    "kospi": ("^KS11", "KOSPI"),
    "nikkei": ("^N225", "日經225"),
    "vix": ("^VIX", "VIX"),
}


def fetch_international(trade: date) -> dict:
    out: dict[str, Any] = {}
    source = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    try:
        import yfinance as yf
    except ImportError:
        for key in INTL:
            out[f"intl.{key}.close"] = missing("yfinance not installed", source)
        return out

    tickers = " ".join(t for t, _ in INTL.values())
    try:
        data = yf.download(tickers, period="2y", interval="1d", group_by="ticker", auto_adjust=False, threads=True, progress=False)
    except Exception as e:
        for key in INTL:
            out[f"intl.{key}.close"] = missing(str(e), source)
        return out

    for key, (ticker, zh) in INTL.items():
        src = source.format(ticker=ticker)
        try:
            if ticker in getattr(data, "columns", []) and isinstance(data.columns, pd.MultiIndex):
                close = data[ticker]["Close"].dropna()
            else:
                # single ticker fallback or weird shape
                close = data["Close"].dropna() if "Close" in data.columns else data.dropna()
                if isinstance(close, pd.DataFrame):
                    close = close.iloc[:, 0]
            if close.empty:
                raise RuntimeError("no yahoo rows")
            last = float(close.iloc[-1])
            prev = float(close.iloc[-2]) if len(close) >= 2 else None
            pct = (last / prev - 1) * 100 if prev else None
            last_dt = close.index[-1]
            as_of = last_dt.date().isoformat() if hasattr(last_dt, "date") else str(last_dt)[:10]
            # 年線
            ma240 = float(close.tail(240).mean()) if len(close) >= 240 else None
            vs = position_vs(last, ma240)
            out[f"intl.{key}.close"] = field(round(last, 2), src, unit="point", as_of=as_of, note=zh)
            out[f"intl.{key}.pct"] = field(round(pct, 2) if pct is not None else None, src, unit="%", as_of=as_of, note=zh)
            if key != "vix":
                out[f"intl.{key}.vs_year"] = field(vs, src, as_of=as_of, method="SMA240 of Yahoo daily closes", note=zh)
        except Exception as e:
            out[f"intl.{key}.close"] = missing(str(e), src, as_of=trade.isoformat())
    return out


# ---------------------------------------------------------------------------
# All-stock 年線/月線/60日新低 + 前100大位階
# ---------------------------------------------------------------------------


def twse_issued_shares(d: date) -> dict[str, float]:
    """Official 發行股數 from MI_QFIIS, falling back to t187ap03_L capital/par."""
    shares: dict[str, float] = {}
    # try today then previous sessions
    cur = d
    for _ in range(6):
        try:
            url = f"{TWSE_RWD}/fund/MI_QFIIS"
            payload = get_json(url, params={"date": cur.strftime("%Y%m%d"), "selectType": "ALLBUT0999", "response": "json"})
            if twse_ok(payload) and payload.get("data"):
                fields = payload.get("fields") or []
                i_code = fields.index("證券代號") if "證券代號" in fields else 0
                i_sh = fields.index("發行股數") if "發行股數" in fields else 3
                for row in payload["data"]:
                    code = str(row[i_code]).strip()
                    sh = parse_num(row[i_sh])
                    if code and sh:
                        shares[code] = sh
                if shares:
                    return shares
        except Exception:
            pass
        cur -= timedelta(days=1)
    return shares


def parse_par_value(v: Any) -> Optional[float]:
    if v is None:
        return None
    n = parse_num(v)
    if n:
        return n
    m = re.search(r"(\d+(?:\.\d+)?)", str(v).replace(",", ""))
    return float(m.group(1)) if m else None


def load_company_lists() -> dict[str, set[str]]:
    lists = {"twse": set(), "tpex": set()}
    urls = {
        "twse": [f"{TWSE_OPENAPI}/t187ap03_L"],
        "tpex": [
            f"{TPEX_OPENAPI}/mopsfin_t187ap03_O",
            f"{TWSE_OPENAPI}/mopsfin_t187ap03_O",
            f"{TPEX_OPENAPI}/t187ap03_O",
        ],
    }
    shares: dict[tuple[str, str], float] = {}
    for mkt, cands in urls.items():
        for url in cands:
            try:
                payload = get_json(url)
                if not isinstance(payload, list) or not payload:
                    continue
                for rec in payload:
                    code = str(rec.get("公司代號") or rec.get("Code") or rec.get("companyCode") or rec.get("有價證券代號") or "").strip()
                    if not code:
                        # sometimes English keys
                        for k, v in rec.items():
                            if "代號" in str(k) or str(k).lower() in ("code", "companyid"):
                                code = str(v).strip()
                                break
                    if code:
                        lists[mkt].add(code)
                    cap = parse_num(rec.get("實收資本額") or rec.get("PaidInCapital") or rec.get("capital"))
                    par = parse_par_value(rec.get("普通股每股面額") or rec.get("ParValue") or rec.get("面額"))
                    if cap and par:
                        shares[(mkt, code)] = cap / par
                if lists[mkt]:
                    break
            except Exception:
                continue
    return {"codes": lists, "shares": shares}  # type: ignore


def load_price_cache() -> dict[tuple[str, str], list[tuple[date, float]]]:
    path = cache_path("prices.json")
    raw = cache_load_json(path) or {}
    out: dict[tuple[str, str], list[tuple[date, float]]] = {}
    for key, rows in raw.items():
        if "|" not in key:
            continue
        mkt, code = key.split("|", 1)
        series = []
        for rec in rows:
            d = parse_date_any(rec[0])
            v = parse_num(rec[1])
            if d and v is not None:
                series.append((d, v))
        out[(mkt, code)] = series
    return out


def save_price_cache(cache: dict[tuple[str, str], list[tuple[date, float]]]) -> None:
    raw = {}
    for (mkt, code), rows in cache.items():
        # keep last ~12y
        rows = sorted(rows)[-2800:]
        raw[f"{mkt}|{code}"] = [[d.isoformat(), v] for d, v in rows]
    cache_save_json(cache_path("prices.json"), raw)


def upsert_prices(cache: dict, mkt: str, closes: dict[str, float], d: date) -> None:
    for code, px in closes.items():
        if code.startswith("_"):
            continue
        series = cache.setdefault((mkt, code), [])
        series = [(x, y) for x, y in series if x != d]
        series.append((d, px))
        series.sort()
        cache[(mkt, code)] = series


def yahoo_history(tickers: list[str], period: str = "2y") -> dict[str, list[tuple[date, float]]]:
    out: dict[str, list[tuple[date, float]]] = {}
    if not tickers:
        return out
    try:
        import yfinance as yf
    except ImportError:
        return out
    # batch in chunks of 40
    for i in range(0, len(tickers), 40):
        chunk = tickers[i : i + 40]
        try:
            data = yf.download(
                " ".join(chunk),
                period=period,
                interval="1d",
                group_by="ticker",
                auto_adjust=False,
                threads=True,
                progress=False,
            )
        except Exception:
            continue
        cols = data.columns
        for t in chunk:
            try:
                if isinstance(cols, pd.MultiIndex) and t in data.columns.get_level_values(0):
                    s = data[t]["Close"].dropna()
                elif "Close" in data.columns:
                    s = data["Close"].dropna()
                else:
                    continue
                rows = []
                for idx, val in s.items():
                    dd = idx.date() if hasattr(idx, "date") else parse_date_any(str(idx)[:10])
                    v = parse_num(val)
                    if dd and v is not None:
                        rows.append((dd, v))
                if rows:
                    out[t] = rows
            except Exception:
                continue
    return out


def fetch_stock_stats(trade: date, budget_sec: int, twse_closes_date: Optional[date] = None) -> dict:
    """年線/月線/60日新低家數 + 前100大位階. Uses official today closes + cached/Yahoo history."""
    t0 = time.time()
    src_note_y = "Yahoo Finance daily close backfill + official daily close upsert"
    out: dict[str, Any] = {}
    try:
        companies = load_company_lists()
        twse_codes: set[str] = companies["codes"]["twse"]
        tpex_codes: set[str] = companies["codes"]["tpex"]
        shares_map: dict[tuple[str, str], float] = companies["shares"]  # type: ignore
    except Exception:
        twse_codes, tpex_codes, shares_map = set(), set(), {}

    cache = load_price_cache()

    # official closes for trade (or last available)
    twse_px: dict[str, float] = {}
    tpex_px: dict[str, float] = {}
    tpex_issued: dict[str, float] = {}
    twse_asof = trade
    tpex_asof = trade
    try:
        d = trade
        for _ in range(5):
            try:
                twse_px = _twse_closes(d)
                twse_asof = d
                break
            except Exception:
                d -= timedelta(days=1)
    except Exception:
        pass
    try:
        d = trade
        for _ in range(5):
            try:
                pack = _tpex_closes(d)
                tpex_issued = pack.pop("_issued_shares", {}) if "_issued_shares" in pack else {}
                tpex_px = pack
                tpex_asof = d
                break
            except Exception:
                d -= timedelta(days=1)
    except Exception:
        pass

    if twse_px:
        upsert_prices(cache, "twse", twse_px, twse_asof)
    if tpex_px:
        upsert_prices(cache, "tpex", tpex_px, tpex_asof)

    # universe
    if not twse_codes:
        twse_codes = {c for c in twse_px if is_common_stock_code(c)}
    if not tpex_codes:
        tpex_codes = {c for c in tpex_px if is_common_stock_code(c)}

    def coverage(mkt: str, codes: set[str], need: int) -> list[str]:
        missing_codes = []
        for c in codes:
            series = cache.get((mkt, c), [])
            if len(series) < need:
                missing_codes.append(c)
        return missing_codes

    # backfill 2y for MA240 if budget allows
    need_2y = coverage("twse", twse_codes, 240) + coverage("tpex", tpex_codes, 240)
    # map to yahoo tickers; only codes still missing
    y_tickers = []
    y_map = {}
    for c in coverage("twse", twse_codes, 240):
        t = f"{c}.TW"
        y_tickers.append(t)
        y_map[t] = ("twse", c)
    for c in coverage("tpex", tpex_codes, 240):
        t = f"{c}.TWO"
        y_tickers.append(t)
        y_map[t] = ("tpex", c)

    if y_tickers and (time.time() - t0) < budget_sec:
        # limit how many we try this run so we stay under budget
        remaining = budget_sec - (time.time() - t0)
        # ~0.15s per ticker in batches of 40, conservative cap
        cap = max(40, int(min(len(y_tickers), remaining / 0.08)))
        hist = yahoo_history(y_tickers[:cap], period="2y")
        for t, rows in hist.items():
            mkt, code = y_map[t]
            existing = {d: v for d, v in cache.get((mkt, code), [])}
            for d, v in rows:
                existing.setdefault(d, v)  # official upsert later wins if already present
            # re-apply official closes so they win
            cache[(mkt, code)] = sorted(existing.items())
        if twse_px:
            upsert_prices(cache, "twse", twse_px, twse_asof)
        if tpex_px:
            upsert_prices(cache, "tpex", tpex_px, tpex_asof)

    # counts
    def stock_flags(mkt: str, codes: set[str], as_of: date) -> dict:
        below_year = below_month = new_60_low = 0
        with_year = with_month = with_60 = 0
        for c in codes:
            series = [(d, v) for d, v in cache.get((mkt, c), []) if d <= as_of]
            if not series:
                continue
            last_d, last_c = series[-1]
            if last_d != as_of:
                # no print today, skip for "當日" stats
                continue
            closes = [v for _, v in series]
            if len(closes) >= 20:
                with_month += 1
                if last_c < ma(closes, 20):  # type: ignore
                    below_month += 1
            if len(closes) >= 240:
                with_year += 1
                if last_c < ma(closes, 240):  # type: ignore
                    below_year += 1
            if len(closes) >= 60:
                with_60 += 1
                prior_min = min(closes[-60:-1]) if len(closes) >= 61 else min(closes[-60:])
                if last_c <= min(closes[-60:]) and last_c <= prior_min:
                    # 當日創 60 日新低：收盤 = 近 60 日最低，且不大於前 59 日最低
                    if last_c <= min(closes[-60:-1] or [last_c]):
                        new_60_low += 1
        return {
            "below_year": below_year,
            "below_month": below_month,
            "new_60_low": new_60_low,
            "with_year": with_year,
            "with_month": with_month,
            "with_60": with_60,
            "universe": len(codes),
        }

    twse_f = stock_flags("twse", twse_codes, twse_asof)
    tpex_f = stock_flags("tpex", tpex_codes, tpex_asof)
    all_f = {k: twse_f[k] + tpex_f[k] for k in twse_f}

    def pack_count(total_key, val, cov, need, as_of, label):
        note = (
            f"{label}；樣本 {cov} 家有足夠歷史可算（需 {need} 日），"
            f"上市 {twse_f['universe']} / 上櫃 {tpex_f['universe']}。"
            "年線=SMA240，月線=SMA20。僅計當日有收盤的公司股。"
        )
        if cov < max(50, int(0.5 * (twse_f["universe"] + tpex_f["universe"]))):
            note += " 歷史快取尚未補齊，家數可能偏低；後續每日跑會累積官方收盤並補 Yahoo 歷史。"
        return field(val, src_note_y, unit="家", as_of=as_of.isoformat(), note=note, method="official close vs SMA from cached daily history")

    as_of_all = max(twse_asof, tpex_asof)
    out["stocks.below_year_line"] = pack_count("year", all_f["below_year"], all_f["with_year"], 240, as_of_all, "上市+上櫃跌破年線家數")
    out["stocks.below_month_line"] = pack_count("month", all_f["below_month"], all_f["with_month"], 20, as_of_all, "上市+上櫃跌破月線家數")
    out["stocks.new_60d_low"] = pack_count("60", all_f["new_60_low"], all_f["with_60"], 60, as_of_all, "當日創60日波段新低家數")
    out["stocks.universe"] = field(
        all_f["universe"],
        "t187ap03 company lists or common-stock code filter",
        unit="家",
        as_of=as_of_all.isoformat(),
        note="上市+上櫃公司家數（公司名單優先，否則用四碼非00開頭代號）",
    )
    out["twse.company_count"] = field(twse_f["universe"], "t187ap03_L / quotes", unit="家", as_of=twse_asof.isoformat())
    out["tpex.company_count"] = field(tpex_f["universe"], "t187ap03_O / quotes", unit="家", as_of=tpex_asof.isoformat())

    # ----- top 100 by market cap -----
    # need issued shares
    twse_issued: dict[str, float] = {}
    try:
        twse_issued = twse_issued_shares(twse_asof)
    except Exception:
        twse_issued = {}

    def market_cap(mkt: str, code: str, px: float) -> Optional[float]:
        if mkt == "tpex" and code in tpex_issued:
            return px * tpex_issued[code]
        if mkt == "twse" and code in twse_issued:
            return px * twse_issued[code]
        sh = shares_map.get((mkt, code))
        if sh:
            return px * sh
        return None

    def top100(mkt: str, codes: set[str], px: dict[str, float], as_of: date) -> dict:
        caps = []
        for c in codes:
            if c not in px:
                continue
            cap = market_cap(mkt, c, px[c])
            if cap:
                caps.append((cap, c, px[c]))
        caps.sort(reverse=True)
        top = caps[:100]
        # backfill 10y for these if needed and budget remains
        need = []
        for _, c, _ in top:
            series = cache.get((mkt, c), [])
            if len(series) < 1200:  # ~5y daily; 10y monthly needs a lot
                need.append(c)
        if need and (time.time() - t0) < budget_sec:
            suffix = ".TW" if mkt == "twse" else ".TWO"
            tickers = [c + suffix for c in need]
            hist = yahoo_history(tickers, period="max")
            for t, rows in hist.items():
                code = t.replace(".TWO", "").replace(".TW", "")
                existing = {d: v for d, v in cache.get((mkt, code), [])}
                for d, v in rows:
                    existing.setdefault(d, v)
                cache[(mkt, code)] = sorted(existing.items())
            if mkt == "twse" and twse_px:
                upsert_prices(cache, "twse", twse_px, twse_asof)
            if mkt == "tpex" and tpex_px:
                upsert_prices(cache, "tpex", tpex_px, tpex_asof)

        counts = {
            "above_year": 0,
            "below_year": 0,
            "within_10pct_5y": 0,
            "below_5y": 0,
            "within_10pct_10y": 0,
            "below_10y": 0,
            "n_year": 0,
            "n_5y": 0,
            "n_10y": 0,
            "n": len(top),
        }
        for _, c, last_c in top:
            series = [(d, v) for d, v in cache.get((mkt, c), []) if d <= as_of]
            if not series:
                continue
            closes = [v for _, v in series]
            monthly = last_of_month(series)
            mcloses = [v for _, v in monthly]
            if len(closes) >= 240:
                counts["n_year"] += 1
                avg = ma(closes, 240)
                if avg and last_c >= avg:
                    counts["above_year"] += 1
                else:
                    counts["below_year"] += 1
            avg5 = ma(mcloses, 60) or ma(closes, 1200)
            if avg5:
                counts["n_5y"] += 1
                if last_c < avg5:
                    counts["below_5y"] += 1
                if abs(last_c / avg5 - 1) <= 0.10:
                    counts["within_10pct_5y"] += 1
            avg10 = ma(mcloses, 120) or ma(closes, 2400)
            if avg10:
                counts["n_10y"] += 1
                if last_c < avg10:
                    counts["below_10y"] += 1
                if abs(last_c / avg10 - 1) <= 0.10:
                    counts["within_10pct_10y"] += 1
        method = (
            "前100大依「收盤價 × 發行股數」市值排序（上櫃用行情表發行股數，上市用實收資本/面額）。"
            "年線=SMA240 日；5年線=60月SMA（不足則日線SMA1200）；10年線=120月SMA（不足則日線SMA2400）。"
            "「距5/10年線 10% 內」為 |close/MA-1| ≤ 10%，含站上與跌破附近。"
        )
        prefix = f"{mkt}.top100"
        src = "TWSE/TPEx official quotes + company capital + Yahoo history for long MAs"
        asof_s = as_of.isoformat()
        out_local = {
            f"{prefix}.n": field(counts["n"], src, unit="家", as_of=asof_s, method=method, note="市值排得出來的檔數，目標100"),
            f"{prefix}.above_year": field(counts["above_year"], src, unit="家", as_of=asof_s, method=method, note=f"可計算年線 {counts['n_year']} 家"),
            f"{prefix}.below_year": field(counts["below_year"], src, unit="家", as_of=asof_s, method=method, note=f"可計算年線 {counts['n_year']} 家"),
            f"{prefix}.within_10pct_5y": field(counts["within_10pct_5y"], src, unit="家", as_of=asof_s, method=method, note=f"可計算5年線 {counts['n_5y']} 家"),
            f"{prefix}.below_5y": field(counts["below_5y"], src, unit="家", as_of=asof_s, method=method, note=f"可計算5年線 {counts['n_5y']} 家"),
            f"{prefix}.within_10pct_10y": field(counts["within_10pct_10y"], src, unit="家", as_of=asof_s, method=method, note=f"可計算10年線 {counts['n_10y']} 家"),
            f"{prefix}.below_10y": field(counts["below_10y"], src, unit="家", as_of=asof_s, method=method, note=f"可計算10年線 {counts['n_10y']} 家"),
        }
        return out_local

    out.update(top100("twse", twse_codes, twse_px, twse_asof))
    out.update(top100("tpex", tpex_codes, tpex_px, tpex_asof))

    try:
        save_price_cache(cache)
    except Exception:
        pass
    out["_stock_stats_elapsed_sec"] = round(time.time() - t0, 1)
    return out


# ---------------------------------------------------------------------------
# Optional 觀察類股 from local notes (KAI-provided; not scraped)
# ---------------------------------------------------------------------------

def load_observations(trade: date) -> dict:
    src = str(OBSERVATIONS_PATH)
    if not OBSERVATIONS_PATH.exists():
        return {
            "watchlist.observed": missing(
                "觀察類股需由 KAI 提供（官方沒有這個欄位）。可寫入 observations.md 或傳給更新流程。",
                None,
                as_of=trade.isoformat(),
            )
        }
    text = OBSERVATIONS_PATH.read_text(encoding="utf-8")
    # grab the section for trade date or the latest date <= trade
    blocks = re.split(r"\n##\s+", text)
    picked = None
    picked_date = None
    for b in blocks:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", b)
        if not m:
            continue
        d = parse_date_any(m.group(1))
        if d and d <= trade and (picked_date is None or d > picked_date):
            picked, picked_date = b, d
    if not picked:
        return {
            "watchlist.observed": missing(
                "observations.md 沒有對應日期的紀錄",
                src,
                as_of=trade.isoformat(),
            )
        }
    # parse simple bullets
    observed = []
    mild = []
    section = "none"
    for line in picked.splitlines():
        if "稍微觀察" in line:
            section = "mild"
            rest = line.split("：", 1)[-1] if "：" in line else ""
            if rest.strip() and rest.strip() not in ("", "無"):
                mild.extend([x.strip() for x in re.split(r"[、,]", rest) if x.strip() and x.strip() != "無"])
            continue
        if "觀察類股" in line:
            section = "obs"
            rest = line.split("：", 1)[-1] if "：" in line else ""
            if rest.strip() and not rest.strip().startswith("無"):
                observed.extend([x.strip() for x in re.split(r"[、,]", rest) if x.strip()])
            continue
    has_none = bool(re.search(r"觀察類股：\s*無", picked))
    return {
        "watchlist.observed": field(
            observed if observed else ("無" if has_none else []),
            src,
            as_of=picked_date.isoformat(),
            note="KAI 提供，非公開行情欄位。此值從 market-signals/observations.md 讀取。",
            stale=picked_date != trade,
        ),
        "watchlist.mild": field(
            mild,
            src,
            as_of=picked_date.isoformat(),
            note="稍微觀察類股（KAI 提供）",
            stale=picked_date != trade,
        ),
    }




# ---------------------------------------------------------------------------
# Assemble / I/O
# ---------------------------------------------------------------------------

PUBLICATION_SCHEDULE = {
    "timezone": "Asia/Taipei",
    "notes": [
        "13:30 收盤。加權指數與上市漲跌家數（MI_INDEX）通常 13:45–14:30 可取。",
        "櫃買官方 highlight / dailyQuotes 有時比上市稍晚；在那之前可用官方 MIS（otc_o00.tw）先拿櫃買收盤。",
        "期交所三大法人（台指期外資未平倉）約 15:00 後更新。",
        "證交所三大法人買賣超（BFI82U：外資／投信／自營商／合計）約 16:00 前後；櫃買「三大法人買賣金額彙總」時段相近。",
        "上市/上櫃融資餘額（MI_MARGN、櫃買儀表板）約 16:00–17:30。",
        "當沖比率（TWTB4U / 櫃買現股當沖統計）約 20:00 初值，T+2 才定稿。",
        "上市/上櫃融資維持率估算需收盤＋融資餘額，約 17:30 後可算；民間網站多寫約 21:30。",
        "證交所「全市場整戶擔保維持率」（BFIJ3U）是隔日才公布前一交易日數字（儀表板日期上限為昨天）。",
        "美股（費半/那斯達克/S&P/道瓊/VIX）收盤約為台北次日 04:00–05:00（視夏令時間）。台股午後 Routine 只能拿到前一晚美股收盤。",
        "庫藏股新宣布來自公開資訊觀測站「庫藏股統計彙總表」（t35sc09 / ajax_t35sc09），依董事會決議日統計上市+上櫃；盤後至晚間可能仍有晚申報。",
    ],
    "recommended_routine": {
        "same_day_most_fields": "21:30–22:00 Asia/Taipei（含當沖初值、融資餘額、融資維持率估算、外資、期貨未平倉；不含當日官方整戶擔保維持率）",
        "complete_including_official_keep_rate": "次一營業日 09:30 Asia/Taipei（補上 T 日整戶擔保維持率，以及較完整的庫藏股快照）",
        "earliest_all_daily_fields_reliably_published": "次一營業日 09:30 Asia/Taipei（若只要 T 日能拿到的欄位，21:30 已涵蓋除官方整戶擔保維持率以外的每日欄位）",
    },
}

MA_METHOD_NOTE = (
    "短均線用官方日收盤：10日=SMA10，10週=每週最後交易日收盤的 SMA10，"
    "10月=每月最後交易日收盤的 SMA10。"
    "季線/半年線/年線分別為日線 SMA60/120/240。"
    "2年線優先用 24 個月 SMA（不足則日線 SMA480）；"
    "5年線=60 個月 SMA，10年線=120 個月 SMA（台股長均線慣例）。"
    "方向：本日均線對上一期均線，漲跌幅絕對值小於 0.05% 視為 flat。"
)


def drop_private(fields: dict) -> dict:
    return {k: v for k, v in fields.items() if not k.startswith("_")}


def write_outputs(doc: dict) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = LATEST_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(LATEST_PATH)

    # append / replace same trade_date line in history
    line = json.dumps(doc, ensure_ascii=False)
    rows = []
    if HISTORY_PATH.exists():
        for ln in HISTORY_PATH.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            try:
                prev = json.loads(ln)
            except Exception:
                rows.append(ln)
                continue
            if prev.get("trade_date") == doc.get("trade_date"):
                continue
            rows.append(ln)
    rows.append(line)
    HISTORY_PATH.write_text("\n".join(rows) + "\n", encoding="utf-8")


def run(trade: Optional[date], skip_stock_stats: bool, stock_budget: int) -> dict:
    started = now_tpe()
    trade_date = discover_trade_date(trade)
    fields: dict[str, Any] = {}
    errors: list[str] = []

    def absorb(fn, *args, **kwargs):
        try:
            fields.update(fn(*args, **kwargs))
        except Exception as e:
            errors.append(f"{fn.__name__}: {e}")
            traceback.print_exc()

    # Parallel-ish official market blocks
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {
            ex.submit(fetch_twse_index_breadth, trade_date): "twse_index",
            ex.submit(fetch_tpex_index_breadth, trade_date): "tpex_index",
            ex.submit(fetch_margin_twse, trade_date): "margin_twse",
            ex.submit(fetch_margin_tpex, trade_date): "margin_tpex",
            ex.submit(fetch_official_keep_rate, trade_date): "keep_rate",
            ex.submit(fetch_institutional, trade_date): "institutional",
            ex.submit(fetch_taifex, trade_date): "taifex",
            ex.submit(fetch_daytrade_twse, trade_date): "dt_twse",
            ex.submit(fetch_daytrade_tpex, trade_date): "dt_tpex",
            ex.submit(fetch_buybacks, trade_date): "buybacks",
            ex.submit(fetch_international, trade_date): "intl",
            ex.submit(load_observations, trade_date): "obs",
        }
        for fut, name in futs.items():
            try:
                fields.update(fut.result())
            except Exception as e:
                errors.append(f"{name}: {e}")
                traceback.print_exc()

    # Index history + MAs (needs closes)
    try:
        taiex_close = (fields.get("taiex.close") or {}).get("value")
        tpex_close = (fields.get("tpex.close") or {}).get("value")
        taiex_hist = load_index_history("taiex", trade_date, years=12)
        tpex_hist = load_index_history("tpex", trade_date, years=12)
        taiex_hist = append_or_replace_close(taiex_hist, trade_date, taiex_close)
        # only append tpex close on trade_date if we actually have today's official/MIS close for that date
        tpex_asof = parse_date_any((fields.get("tpex.close") or {}).get("as_of")) or trade_date
        tpex_hist = append_or_replace_close(tpex_hist, tpex_asof, tpex_close)
        fields.update(
            compute_index_indicators(
                "taiex",
                taiex_hist,
                trade_date,
                f"{TWSE_RWD}/afterTrading/FMTQIK",
            )
        )
        fields.update(
            compute_index_indicators(
                "tpex",
                tpex_hist,
                tpex_asof,
                f"{TPEX_WWW}/afterTrading/tradingIndex",
            )
        )
    except Exception as e:
        errors.append(f"index_ma: {e}")
        traceback.print_exc()

    # 融資維持率 estimate (depends on margin raw + closes)
    try:
        twse_m = {k: v for k, v in fields.items() if k.startswith("twse.margin") or k.startswith("_twse")}
        tpex_m = {k: v for k, v in fields.items() if k.startswith("tpex.margin") or k.startswith("_tpex")}
        # raw keys may be in fields
        fields.update(estimate_margin_maintenance(trade_date, fields, fields))
    except Exception as e:
        errors.append(f"margin_est: {e}")
        traceback.print_exc()

    # combined listed+OTC counts
    try:
        def add_vals(*keys):
            vals = []
            for k in keys:
                v = (fields.get(k) or {}).get("value")
                if v is None:
                    return None
                vals.append(v)
            return sum(vals)

        total_cos = add_vals("twse.listed_stock_count", "tpex.listed_stock_count")
        # prefer company_count later if stock stats fills it
        if total_cos is not None:
            fields["all.listed_stock_count"] = field(
                total_cos,
                "TWSE MI_INDEX MS 股票欄 + TPEx highlight 上櫃家數",
                unit="家",
                as_of=trade_date.isoformat(),
                note="先用漲跌統計加總；若之後有公司名單會另給 company_count",
            )
    except Exception:
        pass

    if not skip_stock_stats:
        try:
            fields.update(fetch_stock_stats(trade_date, stock_budget))
            if (fields.get("stocks.universe") or {}).get("value"):
                fields["all.listed_company_count"] = fields["stocks.universe"]
        except Exception as e:
            errors.append(f"stock_stats: {e}")
            traceback.print_exc()
    else:
        for k in ("stocks.below_year_line", "stocks.below_month_line", "stocks.new_60d_low",
                  "twse.top100.above_year", "tpex.top100.above_year"):
            fields[k] = missing("skipped (--skip-stock-stats)", None, as_of=trade_date.isoformat())

    # Wantgoo probe status (informational, not a data source we rely on)
    fields["meta.wantgoo_status"] = field(
        "html_ok_api_400",
        WANTGOO_MARGIN,
        as_of=started.date().isoformat(),
        note=(
            "玩股網資券頁面 HTTP 200（Cloudflare 存在但未封鎖）。"
            "其 /stock/0000/margin-trading/lending-balance JSON API 回 400 Bad Request，"
            "沒有公開參數文件，未再嘗試繞過。"
            "上市/上櫃融資維持率改由官方餘額與收盤自行估算。"
        ),
    )

    public_fields = drop_private(fields)
    finished = now_tpe()
    doc = {
        "trade_date": trade_date.isoformat(),
        "fetched_at": finished.isoformat(),
        "timezone": "Asia/Taipei",
        "elapsed_sec": round((finished - started).total_seconds(), 1),
        "methods": {
            "moving_averages": MA_METHOD_NOTE,
            "margin_maintenance_estimate": (
                "上市/上櫃融資維持率 = 融資股票市值 ÷ 官方融資金額 × 100%，"
                "市值 = Σ(收盤價 × 融資餘額張數 × 1000)。含有融資餘額的 ETF。"
                "這是民間常用的大盤融資維持率，不是證交所整戶擔保維持率。"
            ),
            "wantgoo": "Checked; not used as a numeric source (API 400). Official sources + official-data estimate instead.",
        },
        "publication_schedule": PUBLICATION_SCHEDULE,
        "fields": public_fields,
        "errors": errors,
    }
    write_outputs(doc)
    return doc


def summarize(doc: dict) -> None:
    print(f"trade_date={doc['trade_date']}  elapsed={doc['elapsed_sec']}s  fields={len(doc['fields'])}  errors={len(doc['errors'])}")
    if doc["errors"]:
        print("errors:")
        for e in doc["errors"]:
            print(" -", e)
    # print key values
    keys = [
        "taiex.close", "taiex.pct", "taiex.vs_year", "taiex.drawdown_1y",
        "tpex.close", "tpex.pct", "tpex.vs_year", "tpex.drawdown_1y",
        "twse.advancers", "twse.decliners", "twse.limit_up", "twse.limit_down", "twse.listed_stock_count",
        "tpex.advancers", "tpex.decliners", "tpex.limit_up", "tpex.limit_down", "tpex.listed_stock_count",
        "twse.margin_balance", "twse.margin_change", "tpex.margin_balance", "tpex.margin_change",
        "twse.margin_maint_est", "tpex.margin_maint_est", "all.margin_maint_est",
        "official.keep_rate", "official.call_accounts", "official.below_130_accounts",
        "twse.foreign_net", "twse.foreign_net_5d",
        "twse.inst.foreign_1d", "twse.inst.foreign_3d", "twse.inst.foreign_5d", "twse.inst.foreign_20d",
        "twse.inst.trust_1d", "twse.inst.trust_3d", "twse.inst.trust_5d", "twse.inst.trust_20d",
        "twse.inst.dealer_1d", "twse.inst.dealer_3d", "twse.inst.dealer_5d", "twse.inst.dealer_20d",
        "twse.inst.total_1d", "twse.inst.total_3d", "twse.inst.total_5d", "twse.inst.total_20d",
        "tpex.inst.foreign_1d", "tpex.inst.foreign_3d", "tpex.inst.foreign_5d", "tpex.inst.foreign_20d",
        "tpex.inst.trust_1d", "tpex.inst.trust_3d", "tpex.inst.trust_5d", "tpex.inst.trust_20d",
        "tpex.inst.dealer_1d", "tpex.inst.dealer_3d", "tpex.inst.dealer_5d", "tpex.inst.dealer_20d",
        "tpex.inst.total_1d", "tpex.inst.total_3d", "tpex.inst.total_5d", "tpex.inst.total_20d",
        "taifex.tx_foreign_short_oi", "taifex.tx_foreign_net_short",
        "twse.daytrade_amt_ratio", "tpex.daytrade_amt_ratio",
        "buybacks.new_today", "buybacks.new_1d", "buybacks.new_5d", "buybacks.new_20d", "buybacks.mtd", "buybacks.recent", "buybacks.list_5d",
        "intl.sox.close", "intl.sox.pct", "intl.nasdaq.close", "intl.sp500.close",
        "intl.dow.close", "intl.kospi.close", "intl.nikkei.close", "intl.vix.close",
        "stocks.below_year_line", "stocks.below_month_line", "stocks.new_60d_low", "stocks.universe",
        "twse.top100.above_year", "twse.top100.below_year", "twse.top100.below_5y", "twse.top100.below_10y",
        "tpex.top100.above_year", "tpex.top100.below_year",
        "watchlist.observed", "watchlist.mild",
    ]
    for k in keys:
        f = doc["fields"].get(k)
        if not f:
            print(f"  {k}: <absent>")
            continue
        v = f.get("value")
        as_of = f.get("as_of")
        stale = " STALE" if f.get("stale") else ""
        reason = f.get("reason") or ""
        print(f"  {k}: {v}  as_of={as_of}{stale}  {reason}")


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Fetch daily Taiwan market signals")
    p.add_argument("--date", help="Trade date YYYY-MM-DD or YYYYMMDD (default: latest session)")
    p.add_argument("--skip-stock-stats", action="store_true", help="Skip all-stock MA and top-100 counts")
    p.add_argument("--stock-budget", type=int, default=150, help="Seconds allowed for Yahoo history backfill")
    args = p.parse_args(argv)
    forced = parse_date_any(args.date) if args.date else None
    doc = run(forced, args.skip_stock_stats, args.stock_budget)
    summarize(doc)
    print(f"wrote {LATEST_PATH} and {HISTORY_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
