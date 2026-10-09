"""Deterministic technical scoring from structured chart readings (extracted from screenshots by data/chart_vision.py).
The AI only reports what it sees; ALL arithmetic, weights, thresholds and validation happen here.
Output matches engines.technical.technical_engine so the pipeline treats both the same, with source='chart_image'.
Imports nothing from the fundamental engine."""
from datetime import datetime
from config.settings import TF_WEIGHTS, TECH_FACTOR_WEIGHTS, MIN_TECH_COVERAGE, CHART_TTL_HOURS, INSTRUMENT_ALIASES
from engines.scoring import classify, weighted_score, confidence

TRENDS = {"up", "down", "range", "unclear"}
SEQ = {"HH_HL", "LH_LL", "mixed", "unclear"}
DIR = {"bullish", "bearish", "none"}
POS = {"above", "below", "unknown"}
STACK = {"bullish", "bearish", "mixed", "unknown"}
HIST = {"positive", "negative", "unknown"}
LEVEL_TYPES = {"support", "resistance", "supply", "demand"}
TF_ALIASES = {"M15": "M15", "15M": "M15", "15": "M15", "H1": "H1", "1H": "H1", "60": "H1", "60M": "H1",
              "H4": "H4", "4H": "H4", "240": "H4", "D1": "D1", "1D": "D1", "D": "D1", "DAILY": "D1"}
SIGN = {"bullish": 1, "bearish": -1, "above": 1, "below": -1, "positive": 1, "negative": -1}


def _num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def validate_reading(raw, asset: str, tf: str, uploaded_at: datetime, now: datetime):
    """Returns (clean_features | None, reason). Strict: anything off-schema is rejected, so text inside an image
    cannot smuggle in new fields or out-of-range values."""
    if not isinstance(raw, dict):
        return None, "reading is not an object"
    if raw.get("readable") is not True:
        return None, f"chart not readable: {str(raw.get('reason_unreadable') or 'no reason given')[:120]}"
    age_h = (now - uploaded_at).total_seconds() / 3600
    if age_h < 0:
        return None, "upload time is in the future"
    if age_h > CHART_TTL_HOURS[tf]:
        return None, f"stale: uploaded {age_h:.1f}h ago, {tf} charts expire after {CHART_TTL_HOURS[tf]}h"
    seen_tf = raw.get("timeframe_seen")
    if seen_tf is not None:
        norm = TF_ALIASES.get(str(seen_tf).strip().upper())
        if norm != tf:
            return None, f"expected {tf} chart but image shows {seen_tf}"
    seen_ins = raw.get("instrument_seen")
    if seen_ins is not None and str(seen_ins).strip().upper().replace(" ", "") not in {a.replace(" ", "") for a in INSTRUMENT_ALIASES[asset]}:
        return None, f"expected {asset} chart but image shows {seen_ins}"
    ax = raw.get("price_axis") or {}
    last, lo, hi = _num(ax.get("last_price")), _num(ax.get("visible_low")), _num(ax.get("visible_high"))
    if ax.get("visible") is not True or None in (last, lo, hi) or not lo < hi:
        return None, "price axis not clearly visible"
    if not lo <= last <= hi:
        return None, "last price outside visible axis range"
    st = raw.get("structure") or {}
    tr, seq, bos, choch = st.get("trend"), st.get("sequence"), st.get("bos", "none"), st.get("choch", "none")
    if tr not in TRENDS or seq not in SEQ or bos not in DIR or choch not in DIR:
        return None, "structure fields invalid"
    sh, sl = _num(st.get("last_swing_high")), _num(st.get("last_swing_low"))
    for v in (sh, sl):
        if v is not None and not lo * 0.98 <= v <= hi * 1.02:
            return None, "swing level outside chart range"
    if sh is not None and sl is not None and sh <= sl:
        return None, "swing high not above swing low"
    em = raw.get("ema") or {}
    e = {k: em.get(k, "unknown") for k in ("price_vs_20", "price_vs_50", "price_vs_200")}
    stack = em.get("stack", "unknown")
    if any(v not in POS for v in e.values()) or stack not in STACK:
        return None, "ema fields invalid"
    mo = raw.get("momentum") or {}
    rsi, hist = _num(mo.get("rsi")), mo.get("macd_histogram", "unknown")
    if (rsi is not None and not 0 <= rsi <= 100) or hist not in HIST:
        return None, "momentum fields invalid"
    levels = []
    for lv in raw.get("key_levels") or []:
        p = _num((lv or {}).get("price"))
        if isinstance(lv, dict) and lv.get("type") in LEVEL_TYPES and p is not None and lo * 0.98 <= p <= hi * 1.02:
            levels.append({"type": lv["type"], "price": p})
    return {"last": last, "trend": tr, "sequence": seq, "bos": bos, "choch": choch, "swing_high": sh, "swing_low": sl,
            "ema": e, "ema_stack": stack, "rsi": rsi, "macd_hist": hist, "levels": levels[:12],
            "instrument_visible": seen_ins is not None, "age_hours": round(age_h, 2)}, None


def chart_factors(f: dict) -> list[dict]:
    # structure
    if f["trend"] == "unclear" and f["choch"] == "none":
        s, s_ev = None, "structure unclear on chart"
    elif f["choch"] != "none":
        s, s_ev = 0.5 * SIGN[f["choch"]], f"CHoCH {f['choch']}: prior trend broken, new trend not confirmed"
    else:
        s = {"up": 1.0, "down": -1.0, "range": 0.0}[f["trend"]]
        s_ev = f"trend {f['trend']} ({f['sequence']})" + (f", BOS {f['bos']}" if f["bos"] != "none" else "")
    # ema
    votes = [SIGN[v] for v in f["ema"].values() if v in SIGN] + ([SIGN[f["ema_stack"]]] if f["ema_stack"] in SIGN else [])
    em = sum(votes) / len(votes) if votes else None
    em_ev = f"{len(votes)} EMA readings visible" if votes else "EMAs not visible on chart"
    # momentum
    parts = []
    if f["rsi"] is not None:
        parts.append(max(-1.0, min(1.0, (f["rsi"] - 50) / 20)))
    if f["macd_hist"] in SIGN:
        parts.append(0.5 * SIGN[f["macd_hist"]])
    mo = sum(parts) / len(parts) if parts else None
    if mo is not None and f["rsi"] is not None and (f["rsi"] > 75 or f["rsi"] < 25):
        mo *= 0.5
    mo_ev = f"RSI {f['rsi']}, MACD histogram {f['macd_hist']}" if parts else "no RSI/MACD visible"
    # premium / discount, computed by code from the read swing prices
    pdv, pd_ev = None, "swing prices not read"
    if f["swing_high"] is not None and f["swing_low"] is not None:
        pos = (f["last"] - f["swing_low"]) / (f["swing_high"] - f["swing_low"])
        if -0.5 <= pos <= 1.5:
            pdv = 0.0 if f["trend"] in ("range", "unclear") else max(-1.0, min(1.0, (0.5 - pos) * 2))
            pd_ev = f"price at {pos:.0%} of dealing range"
        else:
            pd_ev = "price far outside the read swing range, ignored"
    vals = {"structure": (s, s_ev), "ema_trend": (em, em_ev), "momentum": (mo, mo_ev), "premium_discount": (pdv, pd_ev)}
    return [{"name": k, "weight": TECH_FACTOR_WEIGHTS[k], "value": v, "evidence": ev} for k, (v, ev) in vals.items()]


def technical_from_charts(readings: dict, asset: str, now: datetime) -> dict:
    """readings: {tf: {"raw": model_json, "uploaded_at": datetime}}. Missing/rejected timeframes count as missing data."""
    tf_rows, tf_scores, rejections, fresh = {}, [], {}, []
    for tf, w in TF_WEIGHTS.items():
        r = readings.get(tf)
        feats, why = (None, "no chart uploaded") if r is None else validate_reading(r["raw"], asset, tf, r["uploaded_at"], now)
        if feats is None:
            rejections[tf] = why
            tf_rows[tf] = {"score": None, "reason": why}
            tf_scores.append({"name": tf, "weight": w, "value": None, "evidence": why})
            continue
        score, cov, _, rows = weighted_score(chart_factors(feats), MIN_TECH_COVERAGE)
        tf_rows[tf] = {"score": score, "coverage": cov, "factors": rows, "levels": feats["levels"], "last_price": feats["last"]}
        tf_scores.append({"name": tf, "weight": w, "value": None if score is None else score / 100, "evidence": f"{tf} chart score {score}"})
        if score is not None:
            fresh.append(max(0.0, 1 - feats["age_hours"] / CHART_TTL_HOURS[tf]))
    score, cov, agr, rows = weighted_score(tf_scores, MIN_TECH_COVERAGE)
    conf = None
    if score is not None:
        conf = confidence(cov, sum(fresh) / len(fresh) if fresh else 0.0, agr)
        if conf["label"] == "HIGH":
            conf["label"] = "MODERATE"
        conf["note"] = "Chart-image read by AI vision, not computed from OHLC; capped at MODERATE. Not a probability of success."
    return {"engine": "technical", "source": "chart_image", "score": score, "bias": classify(score), "coverage": round(cov, 3),
            "timeframes": tf_rows, "contributions": rows, "confidence": conf, "rejections": rejections}
