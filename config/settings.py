"""All tunable assumptions live here. They are modelling assumptions, not validated claims."""

TIMEZONE = "Africa/Johannesburg"

# score -> bias thresholds (lower bounds)
THRESHOLDS = {"strong": 70, "normal": 30}

INDICATORS = {
    "ema": (20, 50, 200),
    "rsi": 14,
    "macd": (12, 26, 9),
    "atr": 14,
    "swing_strength": 3,   # bars needed on each side to confirm a swing
}

# Technical timeframe weights (sum to 1). HTF structure dominates.
TF_WEIGHTS = {"D1": 0.40, "H4": 0.30, "H1": 0.20, "M15": 0.10}

# Technical factor weights inside one timeframe (sum to 1)
TECH_FACTOR_WEIGHTS = {
    "structure": 0.40,
    "ema_trend": 0.25,
    "momentum": 0.20,
    "premium_discount": 0.15,
}

# Fundamental factor weights per asset. Positive "sign" means factor
# is bullish for the asset when the factor value is positive.
# Factor values are normalised to -1..+1 by the caller or data layer.
FUNDAMENTAL_WEIGHTS = {
    "USD": {
        "fed_policy_expectations": 0.25,
        "inflation_surprise": 0.20,
        "labour_surprise": 0.20,
        "growth_surprise": 0.10,
        "yield_trend_2y": 0.15,
        "risk_sentiment_safe_haven": 0.10,
    },
    "XAUUSD": {
        "real_yield_trend": 0.30,
        "fed_policy_expectations": 0.20,
        "usd_pressure": 0.10,
        "safe_haven_demand": 0.20,
        "inflation_expectations": 0.10,
        "central_bank_buying": 0.10,
    },
    "US30": {
        "growth_outlook": 0.25,
        "fed_policy_expectations": 0.20,
        "earnings_trend": 0.20,
        "labour_consumer": 0.15,
        "credit_conditions": 0.10,
        "risk_sentiment": 0.10,
    },
    "NASDAQ": {
        "fed_policy_expectations": 0.25,
        "yield_trend_10y": 0.20,
        "mega_cap_earnings": 0.25,
        "ai_semis_momentum": 0.15,
        "risk_sentiment": 0.10,
        "inflation_trend": 0.05,
    },
}

# Minimum share of total weight that must have data, otherwise DATA UNAVAILABLE
MIN_DATA_COVERAGE = 0.5
MIN_TECH_COVERAGE = 0.5

# Indicators where a HIGHER actual is bearish for USD-risk context are handled in surprise.py

# ---- Step 2 additions ----
# Macro regime used to interpret releases: "inflation_focus" or "growth_focus". Modelling assumption.
REGIME = "inflation_focus"

# Typical size of a surprise (1 standard deviation), used ONLY until real history is stored.
# These are rough assumptions, not measured values. Evidence text says so. Replace with computed history.
SURPRISE_SCALE = {
    "non-farm employment change": 75.0, "non-farm payrolls": 75.0, "adp": 50.0,
    "unemployment rate": 0.1, "unemployment claims": 15.0, "jobless claims": 15.0,
    "average hourly earnings": 0.1, "core cpi": 0.1, "cpi": 0.1, "core pce": 0.1, "pce": 0.1,
    "gdp": 0.4, "retail sales": 0.5, "ism": 1.5,
}
RELEASE_LOOKBACK_DAYS = 10

# Chart data. kind: direct = one provider symbol; synthetic_dxy = built from 6 FX pairs (ICE formula);
# proxy = tracks the market but is NOT the CFD/index itself. Override via env OHLC_SYMBOL_<KEY>.
OHLC_SYMBOLS = {
    "XAUUSD": {"kind": "direct", "symbol": "XAU/USD", "note": "spot gold"},
    "USD":    {"kind": "synthetic_dxy", "symbol": "DXY", "note": "synthetic DXY from EURUSD,USDJPY,GBPUSD,USDCAD,USDSEK,USDCHF"},
    "US30":   {"kind": "proxy", "symbol": "DIA", "note": "ETF proxy, US cash hours only. Index symbols need a paid Twelve Data plan"},
    "NASDAQ": {"kind": "proxy", "symbol": "QQQ", "note": "ETF proxy, US cash hours only. Index symbols need a paid Twelve Data plan"},
}
TWELVE_DATA_CREDITS_PER_MIN = 8

# ---- Step 3: chart-image technical ----
# How long an uploaded chart stays valid before it is rejected as stale (hours). Assumptions.
CHART_TTL_HOURS = {"M15": 3, "H1": 8, "H4": 24, "D1": 48}
# Names a chart may legitimately show for each market (uppercase). A different symbol is rejected.
INSTRUMENT_ALIASES = {
    "XAUUSD": {"XAUUSD", "XAU/USD", "GOLD", "GC", "GC1!"},
    "USD": {"DXY", "USDX", "DX", "DX1!", "USDOLLAR", "DOLLAR INDEX", "US DOLLAR INDEX"},
    "US30": {"US30", "DJI", "DJIA", "DOW", "DJ30", "WS30", "YM", "YM1!", "DIA", "US30CASH"},
    "NASDAQ": {"NAS100", "NDX", "NASDAQ", "NASDAQ100", "US100", "USTEC", "NQ", "NQ1!", "QQQ", "NAS100USD"},
}
VISION_MODEL = "claude-sonnet-5-5"   # override with env VISION_MODEL
MAX_IMAGE_BYTES = 5 * 1024 * 1024
