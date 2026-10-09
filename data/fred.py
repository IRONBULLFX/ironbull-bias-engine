"""FRED (St. Louis Fed) real yield + breakeven. Free key: https://fred.stlouisfed.org/docs/api/api_key.html
Needs FRED_KEY in the environment. DFII10 = 10Y TIPS real yield, T10YIE = 10Y breakeven inflation."""
import os
from datetime import datetime, timezone
from data.http import get_json

URL = "https://api.stlouisfed.org/fred/series/observations"


def last_observations(series_id: str, key: str, n: int = 2, getter=get_json):
    payload, err = getter(URL, {"series_id": series_id, "api_key": key, "file_type": "json",
                                "sort_order": "desc", "limit": 10})
    if payload is None:
        return None, err
    obs = []
    for o in payload.get("observations", []):
        try:
            obs.append((o["date"], float(o["value"])))  # "." (missing) raises ValueError, skipped
        except (ValueError, KeyError):
            continue
    if len(obs) < n:
        return None, "not enough observations"
    return obs[:n], None


def real_yields(key=None, getter=get_json, clock=lambda: datetime.now(timezone.utc)):
    key = key or os.environ.get("FRED_KEY")
    if not key:
        return None, "FRED_KEY not set"
    out = {"fetched_at": clock()}
    for name, sid in (("real10", "DFII10"), ("be10", "T10YIE")):
        obs, err = last_observations(sid, key, 2, getter)
        if obs is None:
            return None, f"{sid}: {err}"
        out[name], out[name + "_prev"], out[name + "_date"] = obs[0][1], obs[1][1], obs[0][0]
    return out, None


def market_stress(key=None, getter=get_json, clock=lambda: datetime.now(timezone.utc)):
    """VIX (VIXCLS) and ICE BofA US High Yield spread (BAMLH0A0HYM2), last two daily observations each."""
    key = key or os.environ.get("FRED_KEY")
    if not key:
        return None, "FRED_KEY not set"
    out = {"fetched_at": clock()}
    for name, sid in (("vix", "VIXCLS"), ("hy_oas", "BAMLH0A0HYM2")):
        obs, err = last_observations(sid, key, 2, getter)
        if obs is None:
            return None, f"{sid}: {err}"
        out[name], out[name + "_prev"], out[name + "_date"] = obs[0][1], obs[1][1], obs[0][0]
    return out, None
