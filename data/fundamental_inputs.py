"""Turns macro data into fundamental factor values (-1..+1) for each asset.
Anything without a real source stays None (earnings, AI/semis, central-bank buying) so coverage honestly
shows what the score is based on.

Double counting: the backend's Fed stance is derived from the 2Y LEVEL. So fed_policy_expectations uses that
level, while the yield factors use the DAY CHANGE only. They are related but not the same signal."""
from datetime import datetime, timedelta
from config.settings import SURPRISE_SCALE, REGIME, RELEASE_LOOKBACK_DAYS
from engines.surprise import surprise, asset_impact, lookup_meta, ASSET_SENSITIVITY

IMPACT_WEIGHT = {"high": 1.0, "medium": 0.5, "low": 0.2}


def _clamp(x):
    return max(-1.0, min(1.0, x))


def _scale(name: str):
    n = name.lower()
    for k in sorted(SURPRISE_SCALE, key=len, reverse=True):
        if k in n:
            return SURPRISE_SCALE[k]
    return None


def release_impacts(events, asset: str, now: datetime, regime: str = REGIME, lookback_days: int = RELEASE_LOOKBACK_DAYS):
    """Weighted mean impact per category over RELEASED events only. Unreleased events are never used."""
    acc: dict[str, list] = {}
    for e in events:
        if e.actual is None or e.forecast is None or e.t_utc > now:
            continue
        if e.forecast == 0 and (e.previous or 0) != 0:
            continue  # feed uses 0 as "no forecast" placeholder (e.g. ADP weekly); never score it as a surprise
        age = (now - e.t_utc).total_seconds() / 86400
        if age > lookback_days:
            continue
        meta, scale = lookup_meta(e.name), _scale(e.name)
        if meta is None or scale is None:
            continue
        v = asset_impact(e.name, surprise(e.actual, e.forecast, scale), regime, asset)
        if v is None:
            continue
        w = IMPACT_WEIGHT.get(e.impact, 0.2) * max(0.3, 1 - age / lookback_days)
        acc.setdefault(meta[0], []).append((w, v, e))
    out = {}
    for cat, rows in acc.items():
        tw = sum(w for w, _, _ in rows)
        val = sum(w * v for w, v, _ in rows) / tw
        ev = "; ".join(f"{e.name} {e.actual:g} vs {e.forecast:g} fcst" for _, _, e in rows)
        out[cat] = (_clamp(val), f"{ev} (surprise scale is an assumption, regime={regime})")
    return out


def _all_categories(imp):
    if not imp:
        return None, "no released events in window"
    vals = [v for v, _ in imp.values()]
    return _clamp(sum(vals) / len(vals)), "; ".join(f"{k}: {e}" for k, (_, e) in imp.items())


def _f(value, evidence, source, as_of=None):
    return {"value": value, "evidence": evidence, "source": source, "as_of": as_of}


def vix_stress(st):
    """+1 = fear. Assumed thresholds: VIX 20 is the pivot, 10 points = full scale; day change counts 40%."""
    return _clamp(((st["vix"] - 20) / 10) * 0.6 + ((st["vix"] - st["vix_prev"]) / 2) * 0.4)


def credit_stress(st):
    """+1 = stress. Assumed thresholds: HY spread 4.0% is the pivot, 1.5pt = full scale; 15bp day change counts 30%."""
    return _clamp(((st["hy_oas"] - 4.0) / 1.5) * 0.7 + ((st["hy_oas"] - st["hy_oas_prev"]) / 0.15) * 0.3)


def build_factors(asset: str, now: datetime, events=None, yields=None, fed=None, real=None, regime: str = REGIME, stress=None):
    events = events or []
    imp = release_impacts(events, asset, now, regime)
    cal = "macro backend /api/calendar"
    F: dict = {}

    def cat(name):
        if name in imp:
            return _f(imp[name][0], imp[name][1], cal)
        return _f(None, f"no released {name} events in window", cal)

    fed_f = _f(None, "Fed stance unavailable", "macro backend /api/fed")
    if fed:
        sens = ASSET_SENSITIVITY[asset]["hawkish"]
        fed_f = _f(_clamp(sens * (fed["hawkish"] - 0.5) * 2),
                   f"hawkish score {fed['hawkish']:.2f}, {fed.get('stance')} ({fed.get('basis')}); 2Y-yield proxy, not FedWatch odds",
                   "macro backend /api/fed")
    F["fed_policy_expectations"] = fed_f

    if asset == "USD":
        F["inflation_surprise"] = cat("inflation")
        F["labour_surprise"] = cat("labour")
        F["growth_surprise"] = cat("growth")
        if yields and yields.get("y2") is not None and yields.get("prev2") is not None:
            d = yields["y2"] - yields["prev2"]
            F["yield_trend_2y"] = _f(_clamp(d / 0.10), f"2Y {yields['y2']:.2f}% vs prev {yields['prev2']:.2f}% ({d * 100:+.0f}bp)", "macro backend /api/yields")
    elif asset == "XAUUSD":
        v, ev = _all_categories(imp)
        F["usd_pressure"] = _f(v, "release surprises mapped through gold's own sensitivity (not the USD score): " + ev, cal)
        if real:
            d = real["real10"] - real["real10_prev"]
            F["real_yield_trend"] = _f(_clamp(-d / 0.10), f"10Y real yield {real['real10']:.2f}% ({d * 100:+.0f}bp), higher is a headwind", "FRED DFII10", real["real10_date"])
            b = real["be10"] - real["be10_prev"]
            F["inflation_expectations"] = _f(_clamp(b / 0.05), f"10Y breakeven {real['be10']:.2f}% ({b * 100:+.0f}bp)", "FRED T10YIE", real["be10_date"])
    elif asset == "US30":
        F["growth_outlook"] = cat("growth")
        F["labour_consumer"] = cat("labour")
    elif asset == "NASDAQ":
        F["inflation_trend"] = cat("inflation")
        if yields and yields.get("y10") is not None and yields.get("prev10") is not None:
            d = yields["y10"] - yields["prev10"]
            F["yield_trend_10y"] = _f(_clamp(-d / 0.10), f"10Y {yields['y10']:.2f}% vs prev {yields['prev10']:.2f}% ({d * 100:+.0f}bp), rising yields pressure growth stocks", "macro backend /api/yields")
    if stress:
        vs, cs = vix_stress(stress), credit_stress(stress)
        vx = f"VIX {stress['vix']:.1f} (prev {stress['vix_prev']:.1f})"
        hy = f"HY spread {stress['hy_oas']:.2f}% (prev {stress['hy_oas_prev']:.2f}%)"
        note = "assumed thresholds"
        if asset == "USD":
            F["risk_sentiment_safe_haven"] = _f(_clamp(0.5 * vs), f"{vx}, fear supports USD as a haven, {note}", "FRED VIXCLS", stress["vix_date"])
        elif asset == "XAUUSD":
            F["safe_haven_demand"] = _f(_clamp(0.5 * (vs + cs) / 2), f"{vx}; {hy}, stress supports gold, {note}", "FRED VIXCLS, BAMLH0A0HYM2", stress["vix_date"])
        elif asset == "US30":
            F["credit_conditions"] = _f(-cs, f"{hy}, wider spreads are a headwind, {note}", "FRED BAMLH0A0HYM2", stress["hy_oas_date"])
            F["risk_sentiment"] = _f(-vs, f"{vx}, fear is a headwind, {note}", "FRED VIXCLS", stress["vix_date"])
        elif asset == "NASDAQ":
            F["risk_sentiment"] = _f(-vs, f"{vx}, fear is a headwind, {note}", "FRED VIXCLS", stress["vix_date"])
    return F
