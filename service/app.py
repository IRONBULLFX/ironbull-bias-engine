"""IRONBULL FX Daily Bias service (Railway).
- Fundamental bias: fully automatic. A background loop pulls the macro backend + FRED every 10 min (every 2 min around
  high-impact releases), scores all four markets and stores a snapshot + bias changes in Postgres.
- Technical bias: members upload chart screenshots (with their access code). Claude vision reads them, code scores them.
  Each member's technical bias comes only from their own charts.
Start: uvicorn service.app:app --host 0.0.0.0 --port $PORT"""
import asyncio
import hashlib
import hmac
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config.settings import MAX_IMAGE_BYTES
from data.chart_vision import VisionError, extract_chart_reading, image_media_type
from data.macro_client import MacroClient
from engines.scoring import agreement
from engines.sessions import current_sessions, to_sast
from engines.vision_scoring import technical_from_charts, validate_reading
from pipeline import ASSETS, refresh_interval, run_fundamental
from service.macro_fetch import collect_macro
from service.store import Store, owner_id
from data.macro_client import Event

log = logging.getLogger("ironbull")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
TFS = ("D1", "H4", "H1", "M15")


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def access_codes() -> set[str]:
    return {c.strip() for c in os.environ.get("ACCESS_CODES", "").split(",") if c.strip()}


def check_code(code: str | None) -> str:
    """Returns the owner id. Constant-time comparison so codes can't be guessed by timing."""
    if not code:
        raise HTTPException(401, "access code required")
    ok = any(hmac.compare_digest(code.encode(), c.encode()) for c in access_codes())
    if not ok:
        raise HTTPException(401, "invalid access code")
    return owner_id(code)


def check_admin(token: str | None):
    admin = os.environ.get("ADMIN_TOKEN", "")
    if not admin or not token or not hmac.compare_digest(token.encode(), admin.encode()):
        raise HTTPException(401, "admin token required")


def events_from_snapshot(snap) -> list:
    out = []
    for e in (snap or {}).get("inputs", {}).get("events") or []:
        try:
            out.append(Event(e.get("id", ""), e["name"], datetime.fromisoformat(e["t_utc"]), e.get("forecast"),
                             e.get("previous"), e.get("actual"), e.get("unit", ""), e.get("impact", ""), e.get("group", "")))
        except (KeyError, TypeError, ValueError):
            continue
    return out


class State:
    store: Store = None
    client: MacroClient = None
    last_run_at: datetime | None = None
    last_error: str | None = None
    vision_client = None          # tests inject a fake; None = real Anthropic client
    changes: list = []


S = State()


def do_refresh(now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    macro, freshness = collect_macro(S.client)
    snap = run_fundamental(macro, now, freshness=freshness)
    S.store.save_snapshot(snap)
    for asset in ASSETS:
        f = snap["markets"][asset]["fundamental"]
        top = sorted((c for c in f["contributions"] if c.get("contribution")), key=lambda c: -abs(c["contribution"]))[:2]
        reason = "; ".join(f"{c['name']} {c['contribution']:+.1f}" for c in top)
        ch = S.store.record_bias(now, asset, "fundamental", f["score"], f["bias"], reason)
        if ch:
            S.changes = (S.changes + [ch])[-50:]
    S.last_run_at, S.last_error = now, None
    return snap


async def scheduler():
    while True:
        try:
            snap = await asyncio.to_thread(do_refresh)
            log.info("fundamental refresh ok: %s",
                     {a: snap["markets"][a]["fundamental"]["bias"] for a in ASSETS})
        except Exception as e:  # keep the loop alive whatever happens
            S.last_error = f"{type(e).__name__}: {str(e)[:200]}"
            log.exception("refresh failed")
        try:
            events = events_from_snapshot(S.store.latest_snapshot())
        except Exception:
            events = None
        await asyncio.sleep(refresh_interval(events, datetime.now(timezone.utc)))


@asynccontextmanager
async def lifespan(app):
    S.store = S.store or Store()
    S.client = S.client or MacroClient()
    task = None
    if os.environ.get("DISABLE_SCHEDULER") != "1":
        task = asyncio.create_task(scheduler())
    yield
    if task:
        task.cancel()


app = FastAPI(title="IRONBULL FX Daily Bias", lifespan=lifespan)
origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"],
                   allow_headers=["X-Access-Code", "X-Admin-Token", "Content-Type"])


@app.exception_handler(Exception)
async def unhandled(request, exc):
    log.exception("unhandled error")
    return JSONResponse({"ok": False, "error": "internal error"}, status_code=500)


def _status(now):
    snap = S.store.latest_snapshot()
    age = None
    if snap:
        age = round((now - datetime.fromisoformat(snap["generated_at"])).total_seconds() / 60, 1)
    return snap, {
        "now_utc": now.isoformat(), "now_sast": to_sast(now).isoformat(), "sessions": current_sessions(now),
        "last_calculation": snap["generated_at"] if snap else None,
        "minutes_since_calculation": age, "stale": age is None or age > 30,
        "scheduler_error": S.last_error, "data_errors": (snap or {}).get("data_errors", {}),
        "sources_failing": sorted((snap or {}).get("data_errors", {})),
        "macro_backend_down": all(k in (snap or {}).get("data_errors", {}) for k in ("events", "yields", "fed")),
    }


@app.get("/health")
def health():
    now = datetime.now(timezone.utc)
    _, st = _status(now)
    return {"ok": True, "last_calculation": st["last_calculation"], "stale": st["stale"],
            "sources_failing": st["sources_failing"], "macro_backend_down": st["macro_backend_down"],
            "scheduler_error": S.last_error}


@app.get("/api/bias")
def bias(x_access_code: str | None = Header(None)):
    """Dashboard payload: automatic fundamentals for everyone; technical only for a valid access code."""
    now = datetime.now(timezone.utc)
    snap, st = _status(now)
    owner = check_code(x_access_code) if x_access_code else None
    readings = S.store.latest_readings(owner) if owner else {}
    markets = {}
    for asset in ASSETS:
        f = (snap or {}).get("markets", {}).get(asset, {}).get("fundamental") or {
            "engine": "fundamental", "score": None, "bias": "DATA UNAVAILABLE", "coverage": 0, "contributions": [],
            "confidence": None}
        if owner:
            t = technical_from_charts(readings.get(asset, {}), asset, now)
        else:
            t = {"engine": "technical", "source": "chart_image", "score": None, "bias": "DATA UNAVAILABLE",
                 "coverage": 0, "timeframes": {}, "contributions": [], "confidence": None,
                 "rejections": {"all": "enter your access code and upload charts"}}
        markets[asset] = {"fundamental": f, "technical": t, "agreement": agreement(f["bias"], t["bias"])}
    return {"ok": True, "status": st, "next_event": (snap or {}).get("next_event"),
            "regime_assumption": (snap or {}).get("regime_assumption"), "markets": markets,
            "uploads_today": S.store.used_today(owner, now) if owner else None,
            "upload_limit": _env_int("DAILY_UPLOAD_LIMIT", 40) if owner else None}


@app.get("/api/history")
def history(asset: str | None = Query(None), days: int = Query(7, ge=1, le=90), x_access_code: str | None = Header(None)):
    if asset and asset not in ASSETS:
        raise HTTPException(400, "unknown asset")
    owner = check_code(x_access_code) if x_access_code else "global"
    out = {"ok": True, "changes": S.store.history(asset, days, owner)}
    if asset:
        out["fundamental_series"] = S.store.score_series(asset, days)
    return out


@app.post("/api/analyze")
async def analyze(asset: str = Form(...), tf: str = Form(...), file: UploadFile = File(...),
                  x_access_code: str | None = Header(None)):
    owner = check_code(x_access_code)
    if asset not in ASSETS or tf not in TFS:
        raise HTTPException(400, f"asset must be one of {ASSETS}, tf one of {TFS}")
    data = await file.read(MAX_IMAGE_BYTES + 1)
    try:
        image_media_type(data)  # size + type check before spending any money
    except VisionError as e:
        raise HTTPException(400, str(e))
    now = datetime.now(timezone.utc)
    allowed, why = S.store.use(owner, now, _env_int("DAILY_UPLOAD_LIMIT", 40), _env_int("DAILY_GLOBAL_LIMIT", 400))
    if not allowed:
        raise HTTPException(429, why)
    try:
        raw = await asyncio.to_thread(extract_chart_reading, data, asset, tf, S.vision_client)
    except VisionError as e:
        raise HTTPException(502, f"chart reading failed: {e}")
    feats, reason = validate_reading(raw, asset, tf, now, now)
    S.store.save_reading(owner, asset, tf, raw, now, hashlib.sha256(data).hexdigest(), feats is not None, reason)
    tech = technical_from_charts(S.store.latest_readings(owner).get(asset, {}), asset, now)
    snap = S.store.latest_snapshot()
    f = (snap or {}).get("markets", {}).get(asset, {}).get("fundamental") or {"bias": "DATA UNAVAILABLE"}
    ch = S.store.record_bias(now, asset, "technical", tech["score"], tech["bias"], f"{tf} chart uploaded", owner)
    return {"ok": True, "accepted": feats is not None, "reject_reason": reason, "reading": raw, "technical": tech,
            "agreement": agreement(f["bias"], tech["bias"]), "bias_change": ch,
            "uploads_today": S.store.used_today(owner, now)}


@app.post("/api/refresh")
async def force_refresh(x_admin_token: str | None = Header(None)):
    check_admin(x_admin_token)
    snap = await asyncio.to_thread(do_refresh)
    return {"ok": True, "generated_at": snap["generated_at"],
            "markets": {a: snap["markets"][a]["fundamental"]["bias"] for a in ASSETS}}
