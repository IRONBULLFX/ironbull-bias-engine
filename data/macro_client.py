"""Client for the existing Ironbull macro backend (Express on Railway).
Endpoints reused: /api/calendar, /api/yields, /api/fed, /api/prices.
Failures return the last good payload flagged stale, or None with the error, never invented data."""
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from data.http import get_json

DEFAULT_BASE = "https://ironbull-macro-backend-production.up.railway.app"


@dataclass
class Fetched:
    data: object
    fetched_at: datetime
    error: str | None = None
    stale: bool = False

    @property
    def ok(self) -> bool:
        return self.data is not None


@dataclass
class Event:
    id: str
    name: str
    t_utc: datetime
    forecast: float | None
    previous: float | None
    actual: float | None
    unit: str
    impact: str
    group: str


def _num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def parse_calendar(payload) -> list[Event]:
    out = []
    for r in (payload or {}).get("events", []) or []:
        t = r.get("t")
        if not isinstance(t, (int, float)) or not r.get("name"):
            continue
        out.append(Event(str(r.get("id", "")), r["name"], datetime.fromtimestamp(t / 1000, tz=timezone.utc),
                         _num(r.get("forecast")), _num(r.get("previous")), _num(r.get("actual")),
                         r.get("unit", ""), str(r.get("impact", "")).lower(), str(r.get("group", "")).lower()))
    return sorted(out, key=lambda e: e.t_utc)


def parse_yields(payload):
    y = (payload or {}).get("yields") or {}
    keys = ("y2", "y5", "y10", "prev2", "prev10")
    got = {k: _num(y.get(k)) for k in keys}
    return got if got["y2"] is not None or got["y10"] is not None else None


def parse_fed(payload):
    f = (payload or {}).get("fed") or {}
    h = _num(f.get("hawkishScore"))
    if h is None or not 0 <= h <= 1:
        return None
    return {"hawkish": h, "cut_prob": _num(f.get("cutProb")), "stance": f.get("stance"), "basis": f.get("basis")}


class MacroClient:
    def __init__(self, base_url=None, ttl_seconds=300, getter=get_json, clock=lambda: datetime.now(timezone.utc)):
        self.base = (base_url or os.environ.get("MACRO_API_BASE") or DEFAULT_BASE).rstrip("/")
        self.ttl, self.getter, self.clock = ttl_seconds, getter, clock
        self._cache: dict[str, Fetched] = {}

    def _get(self, path: str, parser) -> Fetched:
        now = self.clock()
        hit = self._cache.get(path)
        if hit and not hit.stale and now - hit.fetched_at < timedelta(seconds=self.ttl):
            return hit
        payload, err = self.getter(self.base + path)
        if payload is not None and payload.get("ok") is False:
            payload, err = None, "backend reported ok=false"
        data = parser(payload) if payload is not None else None
        if data is None:
            err = err or "unusable payload"
            if hit and hit.data is not None:
                return Fetched(hit.data, hit.fetched_at, err, stale=True)
            return Fetched(None, now, err)
        f = Fetched(data, now)
        self._cache[path] = f
        return f

    def calendar(self) -> Fetched:
        return self._get("/api/calendar", parse_calendar)

    def yields(self) -> Fetched:
        return self._get("/api/yields", parse_yields)

    def fed(self) -> Fetched:
        return self._get("/api/fed", parse_fed)
