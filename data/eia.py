"""EIA Open Data API v2 (official US Energy Information Administration source). Free key:
https://www.eia.gov/opendata/register.php  -> set EIA_API_KEY in Railway.
Weekly series used: PET.WCESTUS1.W (US crude stocks excl. SPR, thousand bbl), PET.WCRFPUS2.W (US field production,
thousand b/d). The key must be in the URL (EIA does not read headers); errors never include the URL."""
import time
from data.http import get_json

BASE = "https://api.eia.gov/v2/seriesid/"
NON_VALUE_KEYS = {"period", "series", "series-description", "seriesid", "units", "unit", "duoarea", "area-name",
                  "product", "product-name", "process", "process-name", "frequency"}
CACHE_SECONDS = 3 * 3600          # weekly data: no need to call EIA every 10 minutes
_cache: dict = {}


def _value(row: dict):
    """The value column is usually 'value'; fall back to the first numeric field that is not metadata."""
    candidates = [row.get("value")] + [v for k, v in row.items() if k not in NON_VALUE_KEYS and not k.endswith("-units") and k != "value"]
    for v in candidates:
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def weekly_series(series_id: str, key: str, n: int = 2, getter=get_json, clock=time.monotonic):
    """Returns ([(period, value), ...] newest first, error | None)."""
    hit = _cache.get(series_id)
    if hit and clock() - hit[0] < CACHE_SECONDS:
        return hit[1][:n], None
    params = {"api_key": key, "sort[0][column]": "period", "sort[0][direction]": "desc", "length": 10}
    payload, err = getter(BASE + series_id, params)
    if payload is None and err == "HTTP 400":                    # sort/length not accepted on this route: plain call
        payload, err = getter(BASE + series_id, {"api_key": key})
    if payload is None:
        return None, err
    rows = ((payload.get("response") or {}).get("data")) if isinstance(payload, dict) else None
    if not rows:
        msg = (payload.get("error") or payload.get("message")) if isinstance(payload, dict) else None
        return None, str(msg)[:120] if msg else "no data returned"
    obs = sorted(((str(r.get("period")), _value(r)) for r in rows if r.get("period") is not None and _value(r) is not None),
                 key=lambda x: x[0], reverse=True)
    if len(obs) < n:
        return None, "not enough observations"
    _cache[series_id] = (clock(), obs[:10])
    return obs[:n], None
