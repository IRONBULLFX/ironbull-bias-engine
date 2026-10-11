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


EIA_SERIES = {"stocks": "PET.WCESTUS1.W", "production": "PET.WCRFPUS2.W"}   # EIA weekly, official source


def oil_data(key=None, getter=get_json, clock=lambda: datetime.now(timezone.utc), eia_key=None, eia_getter=None):
    """EIA weekly crude stocks ex-SPR (thousand bbl) and US crude production (thousand b/d) from the EIA API, plus the
    Fed's broad dollar index from FRED, last two observations each. Each series is independent: one failing does not
    drop the others. Returns (data | None, error | None); data is None only when every series failed."""
    from data.eia import weekly_series
    key = key or os.environ.get("FRED_KEY")
    eia_key = eia_key if eia_key is not None else os.environ.get("EIA_API_KEY", "")
    out, errors = {"fetched_at": clock()}, []
    if eia_key:
        for name, sid in EIA_SERIES.items():
            obs, err = weekly_series(sid, eia_key, 2, **({"getter": eia_getter} if eia_getter else {}))
            if obs is None:
                errors.append(f"EIA {sid}: {err}")
                continue
            out[name], out[name + "_prev"], out[name + "_date"] = obs[0][1], obs[1][1], obs[0][0]
    else:
        errors.append("EIA_API_KEY not set (crude stocks and production)")
    if key:
        obs, err = last_observations("DTWEXBGS", key, 2, getter)
        if obs is None:
            errors.append(f"DTWEXBGS: {err}")
        else:
            out["dollar"], out["dollar_prev"], out["dollar_date"] = obs[0][1], obs[1][1], obs[0][0]
    else:
        errors.append("FRED_KEY not set")
    if len(out) == 1:
        return None, "; ".join(errors)
    return out, ("; ".join(errors) or None)
