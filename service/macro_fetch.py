"""Collects every fundamental input. Blocking (urllib), so the app runs it in a worker thread."""
from data.fred import market_stress, real_yields
from data.macro_client import MacroClient


def collect_macro(client: MacroClient) -> tuple[dict, float]:
    """Returns (macro dict for the pipeline, freshness 0..1). Stale or failed sources lower freshness."""
    macro, errors, penalties = {}, {}, 0
    for key, call in (("events", client.calendar), ("yields", client.yields), ("fed", client.fed)):
        r = call()
        macro[key] = r.data
        if r.error:
            errors[key] = r.error + (" (last good data used)" if r.stale else "")
            penalties += 1
    for key, fn in (("real", real_yields), ("stress", market_stress)):
        macro[key], err = fn()
        if err:
            errors[key] = err
            penalties += 1
    macro["errors"] = errors
    return macro, max(0.0, 1.0 - 0.2 * penalties)
