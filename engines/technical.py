"""Technical engine. Input: dict of timeframe -> OHLC DataFrame (columns open/high/low/close,
index = bar OPEN time, UTC). The newest bar is treated as FORMING and dropped
(use drop_forming=False if you already pass only closed bars)."""
import pandas as pd
from config.settings import INDICATORS, TF_WEIGHTS, TECH_FACTOR_WEIGHTS, MIN_TECH_COVERAGE
from engines import indicators as ind
from engines.structure import structure_state
from engines.scoring import classify, weighted_score, confidence

MIN_BARS = 60


def _tf_factors(df: pd.DataFrame) -> list[dict]:
    p = INDICATORS
    close = df["close"]
    price = float(close.iloc[-1])
    st = structure_state(df, p["swing_strength"])

    # structure
    if not st["available"]:
        s_val, s_ev = None, "not enough confirmed swings"
    else:
        s_val = float(st["trend"])
        if st["bos"]:
            s_ev = f"trend {st['trend']:+d}, BOS {st['bos']:+d} (confirmed)"
        elif st["choch"]:
            s_val = 0.5 * st["choch"]
            s_ev = f"CHoCH {st['choch']:+d}: prior trend broken, new trend not yet confirmed"
        else:
            s_ev = f"trend {st['trend']:+d} (HH/HL=+1, LH/LL=-1, 0=range)"

    # ema trend
    e20, e50, e200 = (ind.ema(close, n).iloc[-1] for n in p["ema"])
    if any(pd.isna(x) for x in (e20, e50, e200)):
        em_val, em_ev = None, "EMA200 needs more history"
    else:
        pts = sum(int(c) for c in (price > e200, price > e50, price > e20, e20 > e50, e50 > e200))
        em_val = (pts - 2.5) / 2.5
        em_ev = f"price vs EMA20/50/200 and stack, {int(pts)}/5 bullish conditions"

    # momentum (RSI + MACD histogram, damped when overextended)
    r = ind.rsi(close, p["rsi"]).iloc[-1]
    _, _, hist = ind.macd(close, *p["macd"])
    h = hist.iloc[-1]
    a = ind.atr(df, p["atr"]).iloc[-1]
    if pd.isna(r) or pd.isna(h) or pd.isna(a) or a == 0:
        mo_val, mo_ev = None, "momentum needs more history"
    else:
        rsi_part = max(-1, min(1, (r - 50) / 20))
        macd_part = max(-1, min(1, h / a * 2))
        mo_val = 0.5 * rsi_part + 0.5 * macd_part
        note = ""
        if r > 75 or r < 25:
            mo_val *= 0.5
            note = " (overextended, damped)"
        mo_ev = f"RSI {r:.1f}, MACD hist/ATR {h / a:+.2f}{note}"

    # premium / discount inside dealing range (discount favours longs)
    if st["available"] and st["range_high"] != st["range_low"]:
        hi, lo = max(st["range_high"], st["range_low"]), min(st["range_high"], st["range_low"])
        pos = (price - lo) / (hi - lo)
        # Premium/discount is context, not direction: it tempers the trend call.
        # In an uptrend discount is healthy (+) and premium is stretched (-); mirrored in downtrends.
        tr = st["trend"]
        pd_val = 0.0 if tr == 0 else max(-1, min(1, (0.5 - pos) * 2))
        pd_ev = f"price at {pos:.0%} of dealing range"
    else:
        pd_val, pd_ev = None, "no dealing range"

    vals = {"structure": (s_val, s_ev), "ema_trend": (em_val, em_ev),
            "momentum": (mo_val, mo_ev), "premium_discount": (pd_val, pd_ev)}
    return [{"name": k, "weight": TECH_FACTOR_WEIGHTS[k], "value": v, "evidence": e} for k, (v, e) in vals.items()]


def levels(df_d1: pd.DataFrame, df_w: pd.DataFrame | None = None) -> dict:
    out = {}
    if len(df_d1) >= 2:
        out["prev_day_high"] = float(df_d1["high"].iloc[-1])
        out["prev_day_low"] = float(df_d1["low"].iloc[-1])
    if df_w is not None and len(df_w) >= 1:
        out["prev_week_high"] = float(df_w["high"].iloc[-1])
        out["prev_week_low"] = float(df_w["low"].iloc[-1])
    return out


def technical_engine(data: dict, drop_forming: bool = True) -> dict:
    tf_rows, tf_scores = {}, []
    for tf, w in TF_WEIGHTS.items():
        df = data.get(tf)
        if df is None:
            tf_rows[tf] = {"score": None, "reason": "no data"}
            tf_scores.append({"name": tf, "weight": w, "value": None, "evidence": "no data"})
            continue
        if drop_forming:
            df = df.iloc[:-1]
        if len(df) < MIN_BARS:
            tf_rows[tf] = {"score": None, "reason": f"only {len(df)} closed bars"}
            tf_scores.append({"name": tf, "weight": w, "value": None, "evidence": f"only {len(df)} closed bars"})
            continue
        score, cov, agr, rows = weighted_score(_tf_factors(df), MIN_TECH_COVERAGE)
        tf_rows[tf] = {"score": score, "coverage": cov, "factors": rows}
        tf_scores.append({"name": tf, "weight": w, "value": None if score is None else score / 100,
                          "evidence": f"{tf} score {score}"})
    score, cov, agr, rows = weighted_score(tf_scores, MIN_TECH_COVERAGE)
    conf = None if score is None else confidence(cov, 1.0, agr)
    return {"engine": "technical", "score": score, "bias": classify(score), "coverage": round(cov, 3),
            "timeframes": tf_rows, "contributions": rows, "confidence": conf}
