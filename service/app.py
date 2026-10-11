"""IRONBULL FX Daily Bias service (Railway).
- Fundamental bias: fully automatic. A background loop pulls the macro backend + FRED every 10 min (every 2 min around
  high-impact releases), scores all four markets and stores a snapshot + bias changes in Postgres.
- Technical bias: members upload chart screenshots (with their access code). Claude vision reads them, code scores them.
  Each member's technical bias comes only from their own charts.
- Access: HFM clients under your IB verify with trading account + registered email (HFM Affiliates API) and get a
  personal code, re-checked with HFM every 24h. Codes in ACCESS_CODES are admin/backup codes.
Start: uvicorn service.app:app --host 0.0.0.0 --port $PORT"""
import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import Body, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config.settings import MAX_IMAGE_BYTES, RELEASE_LOOKBACK_DAYS
from data.chart_vision import VisionError, extract_chart_reading, image_media_type, prepare_image
from data.macro_client import MacroClient
from engines.scoring import agreement, daily_bias
from engines.sessions import current_sessions, to_sast
from engines.vision_scoring import technical_from_charts, validate_reading
from pipeline import ASSETS, refresh_interval, run_fundamental
from service.macro_fetch import collect_macro
from service.store import Store, owner_id, email_hash
from data.macro_client import Event
from data.hfm import HFMClient

log = logging.getLogger("ironbull")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
TFS = ("D1", "H4", "H1", "M15")


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


RECHECK_AFTER = timedelta(hours=24)       # re-verify each member with HFM this often
HFM_OUTAGE_GRACE = timedelta(hours=72)    # if HFM is down, keep access this long since the last good check


def access_codes() -> set[str]:
    return {c.strip() for c in os.environ.get("ACCESS_CODES", "").split(",") if c.strip()}


def access(code: str | None, now=None) -> dict:
    """Resolves an access code to {owner, kind, account_masked}. Admin codes come from ACCESS_CODES (constant-time
    compare). Member codes are issued after HFM verification and re-checked with HFM every 24h."""
    if not code:
        raise HTTPException(401, "access code required")
    if any(hmac.compare_digest(code.encode(), c.encode()) for c in access_codes()):
        return {"owner": owner_id(code), "kind": "admin", "account_masked": None}
    m = S.store.member_by_token(code)
    if not m:
        raise HTTPException(401, "invalid access code")
    if m["revoked"]:
        raise HTTPException(401, f"access ended: {m['revoke_reason'] or 'no longer an Ironbull HFM client'}")
    now = now or datetime.now(timezone.utc)
    age = now - m["verified_at"]
    if age > RECHECK_AFTER:
        res = S.hfm.verify(m["account"], email_hash_check=lambda e: email_hash(e) == m["email_hash"],
                           min_deposit=_env_float("HFM_MIN_DEPOSIT", 1))
        if res["status"] == "ok":
            S.store.touch_member(m["account"], now)
        elif res["status"] == "error":
            if age > HFM_OUTAGE_GRACE:
                raise HTTPException(503, "could not re-check your HFM account, try again later")
        else:
            reason = {"no_match": "account no longer under Ironbull or email changed", "archived": "HFM account archived",
                      "unfunded": "HFM account has no deposit"}[res["status"]]
            S.store.revoke_account(m["account"], reason)
            raise HTTPException(401, f"access ended: {reason}")
    return {"owner": owner_id("hfm:" + m["account"]), "kind": "hfm", "account_masked": "••••" + m["account"][-4:]}


def check_code(code: str | None) -> str:
    return access(code)["owner"]


class AttemptLimiter:
    """In-memory sliding window per IP (single replica). Stops guessing and protects the HFM API quota."""
    def __init__(self):
        self.hits = defaultdict(deque)

    def allow(self, key, limit, window=3600, clock=time.monotonic):
        q, now = self.hits[key], clock()
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "unknown")


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
    hfm: HFMClient = None
    limiter: AttemptLimiter = None
    changes: list = []


S = State()


def merge_with_release_memory(feed_events, now):
    """The macro backend only shows ~7 days of past releases. Store every released event and add back any still inside
    the scoring window, so a release fades out over RELEASE_LOOKBACK_DAYS instead of vanishing when the feed drops it.
    The feed's own copy wins for the same event id."""
    try:
        S.store.upsert_releases(feed_events, now)
        remembered = S.store.recent_releases(now, RELEASE_LOOKBACK_DAYS)
    except Exception:
        log.exception("release memory unavailable")
        return feed_events
    feed = list(feed_events or [])
    ids = {e.id for e in feed}
    return feed + [e for e in remembered if e.id not in ids]


def backfill_release_memory(now=None):
    """One-time: seed release memory from stored snapshots (they kept the feed's events), so releases the feed has
    already dropped are not lost. Runs only while the releases table is empty."""
    now = now or datetime.now(timezone.utc)
    try:
        if not S.store.releases_empty():
            return 0
        n = 0
        for payload in S.store.snapshot_payloads(now, RELEASE_LOOKBACK_DAYS + 1):
            n += S.store.upsert_releases(events_from_snapshot(payload), now)
        log.info("release memory backfilled from snapshots (%d rows written)", n)
        return n
    except Exception:
        log.exception("release memory backfill failed")
        return 0


def do_refresh(now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    macro, freshness = collect_macro(S.client)
    macro["events"] = merge_with_release_memory(macro.get("events"), now)
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
    S.hfm = S.hfm or HFMClient()
    S.limiter = S.limiter or AttemptLimiter()
    await asyncio.to_thread(backfill_release_memory)
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
    acc = access(x_access_code, now) if x_access_code else None
    owner = acc["owner"] if acc else None
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
        markets[asset] = {"fundamental": f, "technical": t, "agreement": agreement(f["bias"], t["bias"]),
                          "daily": daily_bias(f.get("score"), t.get("score") if owner else None)}
    return {"ok": True, "status": st, "next_event": (snap or {}).get("next_event"),
            "regime_assumption": (snap or {}).get("regime_assumption"), "markets": markets,
            "uploads_today": S.store.used_today(owner, now) if owner else None,
            "upload_limit": _env_int("DAILY_UPLOAD_LIMIT", 40) if owner else None,
            "member": {"kind": acc["kind"], "account_masked": acc["account_masked"]} if acc else None,
            "hfm_signup_url": os.environ.get("HFM_SIGNUP_URL") or None,
            "hfm_verification": bool(S.hfm and S.hfm.configured)}


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
    owner = await asyncio.to_thread(check_code, x_access_code)
    if asset not in ASSETS or tf not in TFS:
        raise HTTPException(400, f"asset must be one of {ASSETS}, tf one of {TFS}")
    data = await file.read(MAX_IMAGE_BYTES + 1)
    try:
        mt = image_media_type(data)                         # size + type check before spending any money
        data, _ = await asyncio.to_thread(prepare_image, data, mt)   # broken files rejected here, not counted
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
    daily = daily_bias(f.get("score"), tech["score"])
    return {"ok": True, "accepted": feats is not None, "reject_reason": reason, "reading": raw, "technical": tech,
            "agreement": agreement(f["bias"], tech["bias"]), "daily": daily, "bias_change": ch,
            "uploads_today": S.store.used_today(owner, now)}


@app.post("/api/refresh")
async def force_refresh(x_admin_token: str | None = Header(None)):
    check_admin(x_admin_token)
    snap = await asyncio.to_thread(do_refresh)
    return {"ok": True, "generated_at": snap["generated_at"],
            "markets": {a: snap["markets"][a]["fundamental"]["bias"] for a in ASSETS}}


MESSAGES = {
    "no_match": "We couldn't match that account number and email to an Ironbull HFM client. Check both details, "
                "or open your HFM account through the Ironbull link.",
    "unfunded": "Your HFM account is linked to Ironbull but has no deposit yet. Fund it to unlock access.",
    "archived": "That HFM account is archived. Use an active account.",
}


@app.post("/api/member/verify")
async def member_verify(request: Request, account: str = Body(..., embed=True), email: str = Body(..., embed=True)):
    """HFM client self-service: trading account + registered email -> personal access code."""
    if not S.limiter.allow("verify:" + client_ip(request), _env_int("VERIFY_ATTEMPTS_PER_HOUR", 10)):
        raise HTTPException(429, "too many attempts, try again in an hour")
    account, email = str(account).strip(), str(email).strip()
    if len(email) > 200 or "@" not in email:
        raise HTTPException(400, "enter the email you registered with HFM")
    res = await asyncio.to_thread(S.hfm.verify, account, email, None, _env_float("HFM_MIN_DEPOSIT", 1))
    if res["status"] == "error":
        log.warning("hfm verify error: %s", res.get("reason"))
        raise HTTPException(503, "HFM verification is temporarily unavailable, try again shortly")
    if res["status"] != "ok":
        raise HTTPException(403, MESSAGES[res["status"]])
    code = "IBM-" + secrets.token_urlsafe(18)
    S.store.add_member(code, account, email, datetime.now(timezone.utc))
    return {"ok": True, "code": code, "account_masked": "••••" + account[-4:]}


@app.get("/api/admin/hfm-check")
async def hfm_check(x_admin_token: str | None = Header(None)):
    """Admin: confirms the HFM key works. Returns counts and field names only, never client details."""
    check_admin(x_admin_token)
    diag = await asyncio.to_thread(S.hfm.diagnostics)
    return {**diag, "min_deposit": _env_float("HFM_MIN_DEPOSIT", 1), "members": S.store.member_counts()}
