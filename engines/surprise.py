"""Economic surprise maths. Direction depends on what the indicator MEANS, not on actual > forecast."""
from dataclasses import dataclass

# category: inflation | labour | growth. inverse=True means a higher number is weaker economy
# (unemployment, claims). Names are matched case-insensitively by substring.
INDICATOR_META = {
    "core cpi": ("inflation", False), "cpi": ("inflation", False),
    "core pce": ("inflation", False), "pce": ("inflation", False),
    "average hourly earnings": ("inflation", False),
    "non-farm payrolls": ("labour", False), "non-farm employment change": ("labour", False),
    "nfp": ("labour", False), "adp": ("labour", False),
    "unemployment rate": ("labour", True), "jobless claims": ("labour", True),
    "unemployment claims": ("labour", True),
    "gdp": ("growth", False), "ism": ("growth", False), "retail sales": ("growth", False),
}

# Sensitivity of each asset to (hawkish_shift, growth_shift). Regime flips which dominates.
# hawkish_shift = +1 means rate-cut hopes fall. Values are modelling assumptions.
ASSET_SENSITIVITY = {
    "USD":    {"hawkish": +0.8, "growth": +0.3},
    "XAUUSD": {"hawkish": -0.8, "growth": -0.1},
    "US30":   {"hawkish": -0.3, "growth": +0.6},
    "NASDAQ": {"hawkish": -0.7, "growth": +0.3},
}


@dataclass
class Surprise:
    raw: float
    z: float | None
    direction: int  # +1 actual above forecast, -1 below, 0 equal


def lookup_meta(name: str):
    n = name.lower()
    for key in sorted(INDICATOR_META, key=len, reverse=True):
        if key in n:
            return INDICATOR_META[key]
    return None


def surprise(actual, forecast, hist_std=None):
    """Raw and standardised surprise. z is None when no historical std is supplied
    (we never invent a distribution). Returns None if actual/forecast missing."""
    if actual is None or forecast is None:
        return None
    raw = actual - forecast
    z = raw / hist_std if hist_std and hist_std > 0 else None
    return Surprise(raw, z, (raw > 0) - (raw < 0))


def asset_impact(name: str, s: Surprise, regime: str = "inflation_focus", asset: str = "USD", cap: float = 2.0):
    """Map a surprise to an asset factor value in -1..+1.
    regime 'inflation_focus': inflation/wages drive Fed expectations, weak labour = dovish.
    regime 'growth_focus': good growth/labour data is read as risk-positive.
    Returns None if indicator unknown or no standardised surprise (no guessing)."""
    meta = lookup_meta(name)
    if meta is None or s is None or s.z is None:
        return None
    cat, inverse = meta
    z = max(-cap, min(cap, s.z)) / cap
    if inverse:
        z = -z  # higher unemployment/claims = weaker economy
    sens = ASSET_SENSITIVITY[asset]
    if cat == "inflation":
        hawk, growth = z, 0.0
    elif cat == "labour":
        # strong jobs: hawkish under inflation focus, growth-positive under growth focus
        hawk = z if regime == "inflation_focus" else 0.3 * z
        growth = z if regime == "growth_focus" else 0.4 * z
    else:  # growth
        hawk = 0.3 * z if regime == "inflation_focus" else 0.0
        growth = z
    return max(-1.0, min(1.0, sens["hawkish"] * hawk + sens["growth"] * growth))
