"""Runs both engines for all four markets and saves an auditable snapshot.
The engines never see each other's output. Only this orchestration layer puts them side by side."""
import hashlib
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from config.settings import REGIME
from data.fundamental_inputs import build_factors
from engines.fundamental import fundamental_engine
from engines.scoring import agreement
from engines.sessions import to_sast, current_sessions
from engines.technical import technical_engine
from engines.vision_scoring import technical_from_charts

ASSETS = ("USD", "XAUUSD", "US30", "NASDAQ")


def _jsonable(o):
    if is_dataclass(o):
        return asdict(o)
    if isinstance(o, datetime):
        return o.isoformat()
    if hasattr(o, "item"):
        return o.item()
    raise TypeError(type(o))


def run_bias(ohlc: dict, macro: dict, now=None, mode="LIVE", regime=REGIME, charts: dict | None = None) -> dict:
    """ohlc: {asset: {"frames": {tf: df|None}, "errors": {...}, "symbol", "kind", "note"}}
    macro: {"events": [...], "yields": {...}|None, "fed": {...}|None, "real": {...}|None, "stress": {...}|None, "errors": {...}}
    charts: optional {asset: {tf: {"raw": model_json, "uploaded_at": datetime}}}. When an asset has chart uploads they
    are used for its technical bias (source=chart_image); otherwise the OHLC engine is used."""
    now = now or datetime.now(timezone.utc)
    result = {"mode": mode, "generated_at": now.isoformat(), "generated_sast": to_sast(now).isoformat(),
              "sessions": current_sessions(now), "regime_assumption": regime, "markets": {}}
    if mode != "LIVE":
        result["warning"] = f"{mode}: not live market data. Do not trade from this."
    for asset in ASSETS:
        data = ohlc.get(asset, {})
        frames = data.get("frames", {})
        fund = fundamental_engine(asset, build_factors(asset, now, macro.get("events"), macro.get("yields"),
                                                       macro.get("fed"), macro.get("real"), regime, macro.get("stress")))
        if charts and charts.get(asset):
            tech = technical_from_charts(charts[asset], asset, now)
        else:
            tech = technical_engine(frames, drop_forming=False)  # provider already removed forming bars
            tech["source"] = "ohlc" if frames else "none"
        last = None
        for tf in ("M15", "H1", "H4", "D1"):
            df = frames.get(tf)
            if df is not None and len(df):
                last = {"price": float(df["close"].iloc[-1]), "bar_open_utc": df.index[-1].isoformat(), "timeframe": tf}
                break
        if last is None and tech.get("source") == "chart_image":
            for tf in ("M15", "H1", "H4", "D1"):
                lp = tech["timeframes"].get(tf, {}).get("last_price")
                if lp is not None:
                    last = {"price": lp, "bar_open_utc": None, "timeframe": tf, "from": "chart screenshot"}
                    break
        result["markets"][asset] = {
            "reference_price": last, "instrument": {k: data.get(k) for k in ("symbol", "kind", "note")},
            "fundamental": fund, "technical": tech,
            "agreement": agreement(fund["bias"], tech["bias"]),
            "data_errors": {"ohlc": data.get("errors", {})},
        }
    result["data_errors"] = macro.get("errors", {})
    # inputs preserved so a past calculation can be re-audited without later information
    chart_inputs = {a: {tf: {"raw": r["raw"], "uploaded_at": r["uploaded_at"]} for tf, r in tfs.items()}
                    for a, tfs in (charts or {}).items()}
    result["inputs"] = {"charts": chart_inputs, "macro": {k: v for k, v in macro.items() if k != "errors"},
                        "ohlc_last_bars": {a: {tf: (df.index[-1].isoformat() if df is not None and len(df) else None)
                                               for tf, df in ohlc.get(a, {}).get("frames", {}).items()} for a in ASSETS}}
    result["inputs_sha256"] = hashlib.sha256(json.dumps(result["inputs"], default=_jsonable, sort_keys=True).encode()).hexdigest()
    return json.loads(json.dumps(result, default=_jsonable))


def save_snapshot(result: dict, folder="snapshots") -> Path:
    Path(folder).mkdir(exist_ok=True)
    stamp = result["generated_at"][:16].replace(":", "").replace("-", "").replace("T", "_")
    p = Path(folder) / f"{result['mode'].split()[0].lower()}_{stamp}.json"
    p.write_text(json.dumps(result, indent=2))
    return p


# ---------------- fundamental-only run (used by the hosted scheduler) ----------------
def _strip_fetched_at(o):
    if isinstance(o, dict):
        return {k: _strip_fetched_at(v) for k, v in o.items() if k != "fetched_at"}
    if isinstance(o, list):
        return [_strip_fetched_at(v) for v in o]
    return o


def next_event(events, now, impact=("high",)):
    """Next scheduled high-impact USD release after `now`. The backend calendar is USD-only, so it applies to all four markets."""
    fut = sorted((e for e in (events or []) if e.t_utc > now and e.impact in impact), key=lambda e: e.t_utc)
    if not fut:
        return None
    e = fut[0]
    return {"name": e.name, "t_utc": e.t_utc.isoformat(), "t_sast": to_sast(e.t_utc).isoformat(), "currency": "USD",
            "forecast": e.forecast, "previous": e.previous,
            "minutes_until": round((e.t_utc - now).total_seconds() / 60, 1),
            "pre_event_warning": (e.t_utc - now).total_seconds() <= 1800}


def refresh_interval(events, now, base=600, fast=120):
    """Poll faster around high-impact releases (from 30 min before to 60 min after)."""
    for e in events or []:
        if e.impact == "high" and -3600 <= (e.t_utc - now).total_seconds() <= 1800:
            return fast
    return base


def run_fundamental(macro: dict, now=None, regime=REGIME, freshness: float = 1.0) -> dict:
    now = now or datetime.now(timezone.utc)
    out = {"generated_at": now.isoformat(), "generated_sast": to_sast(now).isoformat(), "sessions": current_sessions(now),
           "regime_assumption": regime, "next_event": next_event(macro.get("events"), now), "markets": {}}
    for asset in ASSETS:
        f = fundamental_engine(asset, build_factors(asset, now, macro.get("events"), macro.get("yields"), macro.get("fed"),
                                                    macro.get("real"), regime, macro.get("stress")), freshness=freshness)
        out["markets"][asset] = {"fundamental": f}
    out["data_errors"] = macro.get("errors", {})
    inputs = _strip_fetched_at({k: v for k, v in macro.items() if k != "errors"})
    out["inputs"] = json.loads(json.dumps(inputs, default=_jsonable))
    out["inputs_sha256"] = hashlib.sha256(json.dumps(out["inputs"], sort_keys=True).encode()).hexdigest()
    return json.loads(json.dumps(out, default=_jsonable))
