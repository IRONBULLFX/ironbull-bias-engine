"""Fundamental engine. Input: factor values in -1..+1 (None = unavailable) per asset.
It knows nothing about prices or charts, so it cannot be influenced by the technical engine."""
from config.settings import FUNDAMENTAL_WEIGHTS, MIN_DATA_COVERAGE
from engines.scoring import classify, weighted_score, confidence


def fundamental_engine(asset: str, factors: dict, freshness: float = 1.0, weights: dict | None = None) -> dict:
    """factors: {factor_name: {"value": float|None, "evidence": str, "source": str, "as_of": iso}}
    Unknown factor names are rejected so typos cannot silently drop weight."""
    w = weights or FUNDAMENTAL_WEIGHTS[asset]
    unknown = set(factors) - set(w)
    if unknown:
        raise ValueError(f"Unknown factors for {asset}: {sorted(unknown)}")
    contribs = []
    for name, weight in w.items():
        f = factors.get(name) or {}
        contribs.append({"name": name, "weight": weight, "value": f.get("value"),
                         "evidence": f.get("evidence", "no data"),
                         "source": f.get("source"), "as_of": f.get("as_of")})
    score, cov, agr, rows = weighted_score(contribs, MIN_DATA_COVERAGE)
    return {"engine": "fundamental", "asset": asset, "score": score, "bias": classify(score),
            "coverage": round(cov, 3), "contributions": rows,
            "confidence": None if score is None else confidence(cov, freshness, agr)}
