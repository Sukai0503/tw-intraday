#!/usr/bin/env python3
"""Fetch US stock/ETF daily closes + USD/TWD into us_px.json.

Source: Yahoo Finance chart API (query1, no key). Previous close is the
prior valid daily bar (not meta.chartPreviousClose, which can be stale).
FX: same API, symbol USDTWD=X (BOT/TWSE pages are Cloudflare-challenged
and must not be bypassed).

Tickers = defaults in us_tickers.json (plus a built-in seed). Firestore
holdings are not readable from this pipeline (no admin credentials).
Add uncommon symbols to us_tickers.json; the asset tab uses 手動價 until
the next publish includes them. No client-side Yahoo fetch (no CORS).

Rate limit: one request per symbol, ~0.35s pause, one try, no retry
storm. Skip if the output is younger than 30 minutes unless --force.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

UA = "tw-intraday/1.0 (+https://sukai0503.github.io/tw-intraday/)"
TPE = ZoneInfo("Asia/Taipei")
PAUSE = 0.35
TIMEOUT = 20
SKIP_SEC = 1800
SEED = [
    "NVDA", "TSLA", "AAPL", "MSFT", "AMZN", "GOOGL", "GOOG", "META",
    "AMD", "AVGO", "TSM", "QQQ", "SPY", "SOXX", "VOO", "VTI", "IWM",
    "NFLX", "INTC", "MU", "ARM", "PLTR", "AMAT", "ASML", "BRK-B",
    "JPM", "V", "MA", "COST", "BABA", "PDD", "SMCI", "COIN", "NVO",
]
FX_SYM = "USDTWD=X"


def paths():
    here = Path(__file__).resolve().parent
    if (Path("/workspace/intraday") / "us_px.py").exists() and here.name != "scripts":
        root = Path("/workspace/intraday")
        return root / "public" / "us_px.json", root / "us_tickers.json"
    if here.name == "scripts":
        return here.parent / "us_px.json", here / "us_tickers.json"
    out_dir = here / "public"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / "us_px.json", here / "us_tickers.json"


def tpe_now():
    return datetime.now(TPE).strftime("%Y-%m-%d %H:%M:%S")


def tpe_from_unix(ts):
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(TPE).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except (OSError, ValueError, TypeError, OverflowError):
        return None


def r4(v):
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x:  # NaN
        return None
    return round(x + 0.0, 4)


def norm_sym(s):
    t = str(s or "").strip().upper().replace(".", "-")
    if not t or len(t) > 16:
        return ""
    if any(c for c in t if not (c.isalnum() or c in "-=")):
        return ""
    return t


def load_tickers(path: Path):
    found = []
    seen = set()
    for src in (SEED,):
        for s in src:
            k = norm_sym(s)
            if k and k not in seen:
                seen.add(k)
                found.append(k)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            extra = data if isinstance(data, list) else (data or {}).get("tickers") or []
            for s in extra:
                k = norm_sym(s)
                if k and k not in seen:
                    seen.add(k)
                    found.append(k)
        except (OSError, json.JSONDecodeError) as e:
            print("us_tickers.json skip:", e, file=sys.stderr)
    return found


def fetch_chart(sym: str):
    q = urllib.parse.urlencode({"interval": "1d", "range": "10d"})
    enc = urllib.parse.quote(sym, safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{enc}?{q}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def parse_chart(data):
    chart = (data or {}).get("chart") or {}
    if chart.get("error"):
        return None
    result = chart.get("result") or []
    if not result:
        return None
    res = result[0]
    meta = res.get("meta") or {}
    ts = res.get("timestamp") or []
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    bars = []
    for i, t in enumerate(ts):
        c = r4(closes[i] if i < len(closes) else None)
        if c is None or c <= 0:
            continue
        bars.append((int(t), c))
    last = r4(meta.get("regularMarketPrice"))
    if last is None or last <= 0:
        last = bars[-1][1] if bars else None
    if last is None:
        return None
    prev = None
    if len(bars) >= 2:
        # last bar may already equal regularMarketPrice (session close)
        if abs(bars[-1][1] - last) < 1e-6:
            prev = bars[-2][1]
        else:
            prev = bars[-1][1]
    if prev is None:
        prev = r4(meta.get("chartPreviousClose"))  # last resort only
    pct = None
    if prev and prev > 0 and last is not None:
        pct = round((last / prev - 1) * 100, 3)
    name = meta.get("shortName") or meta.get("longName") or meta.get("symbol")
    t = tpe_from_unix(meta.get("regularMarketTime")) or (
        tpe_from_unix(bars[-1][0]) if bars else None
    )
    ccy = meta.get("currency") or "USD"
    return {
        "n": name,
        "p": last,
        "prev": prev,
        "pct": pct,
        "t": t,
        "ccy": ccy,
    }


def get_one(sym: str):
    try:
        return parse_chart(fetch_chart(sym))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError) as e:
        print(f"fail {sym}: {type(e).__name__} {e}", file=sys.stderr)
        return None


def stale(path: Path, force: bool):
    if force or not path.exists():
        return False
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    return age < SKIP_SEC


def main():
    force = "--force" in sys.argv
    out, tickers_path = paths()
    out.parent.mkdir(parents=True, exist_ok=True)
    if stale(out, force):
        print(f"us_px skip ({out} younger than {SKIP_SEC}s)")
        return 0

    prev_doc = {}
    if out.exists():
        try:
            prev_doc = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            prev_doc = {}
    old_px = prev_doc.get("px") or {}
    old_fx = prev_doc.get("fx") or {}

    tickers = load_tickers(tickers_path)
    px = {}
    ok = 0
    for i, sym in enumerate(tickers):
        if i:
            time.sleep(PAUSE)
        row = get_one(sym)
        if row:
            px[sym] = row
            ok += 1
        elif sym in old_px:
            keep = dict(old_px[sym])
            keep["stale"] = True
            px[sym] = keep
            print(f"reuse stale {sym}", file=sys.stderr)

    # FX after a pause
    time.sleep(PAUSE)
    fx_row = get_one(FX_SYM)
    fx = None
    if fx_row and fx_row.get("p"):
        fx = {
            "usdTwd": fx_row["p"],
            "prev": fx_row.get("prev"),
            "pct": fx_row.get("pct"),
            "time": fx_row.get("t"),
            "src": "Yahoo Finance USDTWD=X",
        }
    elif old_fx.get("usdTwd"):
        fx = dict(old_fx)
        fx["stale"] = True
        print("reuse stale FX", file=sys.stderr)

    doc = {
        "fetched": tpe_now(),
        "src": "Yahoo Finance chart (query1), daily close + prior bar; FX USDTWD=X",
        "tz": "Asia/Taipei",
        "fx": fx,
        "px": px,
        "n": len(px),
        "ok": ok,
    }
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(out)
    fxv = (fx or {}).get("usdTwd")
    nv = (px.get("NVDA") or {}).get("p")
    print(f"us_px wrote {out} n={len(px)} ok={ok} NVDA={nv} USD/TWD={fxv} {doc['fetched']}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
