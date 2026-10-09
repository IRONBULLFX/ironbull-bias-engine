"""Shared classification, confidence and agreement logic. No market logic here."""
from config.settings import THRESHOLDS

UNAVAILABLE = "DATA UNAVAILABLE"


def classify(score, thresholds=None) -> str:
    if score is None:
        return UNAVAILABLE
    t = thresholds or THRESHOLDS
    if score >= t["strong"]:
        return "STRONGLY BULLISH"
    if score >= t["normal"]:
        return "BULLISH"
    if score > -t["normal"]:
        return "NEUTRAL"
    if score > -t["strong"]:
        return "BEARISH"
    return "STRONGLY BEARISH"


def _side(bias: str) -> int:
    if bias in ("STRONGLY BULLISH", "BULLISH"):
        return 1
    if bias in ("STRONGLY BEARISH", "BEARISH"):
        return -1
    return 0


def agreement(fund_bias: str, tech_bias: str) -> str:
    if UNAVAILABLE in (fund_bias, tech_bias):
        return "INCOMPLETE DATA"
    f, t = _side(fund_bias), _side(tech_bias)
    if f == 1 and t == 1:
        return "BULLISH ALIGNMENT"
    if f == -1 and t == -1:
        return "BEARISH ALIGNMENT"
    if f * t == -1:
        return "CONFLICTING BIASES"
    if f == 0 and t == 0:
        return "NO CLEAR DIRECTION"
    return "PARTIAL SIGNAL (one engine neutral)"


def confidence(coverage: float, freshness: float, factor_agreement: float) -> dict:
    """Evidence quality 0..1 from data completeness, freshness and factor agreement.
    This is NOT a win probability and no model validation is claimed."""
    q = 0.4 * coverage + 0.25 * freshness + 0.35 * factor_agreement
    label = "HIGH" if q >= 0.75 else "MODERATE" if q >= 0.5 else "LOW"
    return {"evidence_quality": round(q, 3), "label": label,
            "note": "Evidence quality, not a probability of success. Not validated out of sample."}


def weighted_score(contribs: list[dict], min_coverage: float):
    """contribs: [{name, weight, value(-1..1 or None), evidence}].
    Contribution = 100 * value * weight / TOTAL weight. Missing factors are NOT
    renormalised away, so partial data cannot inflate conviction: with 60% coverage
    the maximum possible score is +/-60. If covered weight < min_coverage -> None
    (DATA UNAVAILABLE). Returns (score|None, coverage, agreement, rows)."""
    total_w = sum(c["weight"] for c in contribs)
    have = [c for c in contribs if c["value"] is not None]
    cov_w = sum(c["weight"] for c in have)
    coverage = cov_w / total_w if total_w else 0.0
    rows = []
    for c in contribs:
        if c["value"] is None:
            rows.append({**c, "contribution": None, "status": "missing"})
        else:
            v = max(-1.0, min(1.0, c["value"]))
            rows.append({**c, "value": v, "contribution": round(100 * v * c["weight"] / total_w, 2) if total_w else 0,
                         "status": "ok"})
    if coverage + 1e-9 < min_coverage or cov_w == 0:
        return None, coverage, 0.0, rows
    score = sum(r["contribution"] for r in rows if r["contribution"] is not None)
    signs = [1 if r["value"] > 0.05 else -1 if r["value"] < -0.05 else 0 for r in rows if r["status"] == "ok"]
    nz = [s for s in signs if s != 0]
    agree = abs(sum(nz)) / len(nz) if nz else 0.0
    return round(max(-100, min(100, score)), 2), coverage, agree, rows
