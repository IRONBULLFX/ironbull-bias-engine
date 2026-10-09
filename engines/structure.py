"""Market structure from CONFIRMED swings only.

A swing high at bar i is confirmed only once `n` later bars have closed and none
exceeded it. So the swing is known at bar i+n, never earlier -> no repaint.
"""
from dataclasses import dataclass
import pandas as pd


@dataclass
class Swing:
    idx: int          # bar index of the extreme
    confirmed_at: int # bar index when it became known
    price: float
    kind: str         # "H" or "L"


def confirmed_swings(df: pd.DataFrame, n: int = 3) -> list[Swing]:
    hi, lo = df["high"].values, df["low"].values
    out: list[Swing] = []
    for i in range(n, len(df) - n):
        if hi[i] > max(hi[i - n:i]) and hi[i] >= max(hi[i + 1:i + n + 1]):
            out.append(Swing(i, i + n, float(hi[i]), "H"))
        if lo[i] < min(lo[i - n:i]) and lo[i] <= min(lo[i + 1:i + n + 1]):
            out.append(Swing(i, i + n, float(lo[i]), "L"))
    return sorted(out, key=lambda s: s.idx)


def structure_state(df: pd.DataFrame, n: int = 3) -> dict:
    """Returns trend (+1/-1/0), last BOS, CHoCH flag, dealing range.

    trend: HH+HL = +1, LH+LL = -1, else 0.
    BOS: last close beyond the last swing in trend direction.
    CHoCH: last close beyond the opposite swing of the prevailing trend.
    """
    sw = confirmed_swings(df, n)
    highs = [s for s in sw if s.kind == "H"]
    lows = [s for s in sw if s.kind == "L"]
    res = {"trend": 0, "bos": 0, "choch": 0, "range_high": None, "range_low": None, "equilibrium": None}
    if len(highs) < 2 or len(lows) < 2:
        res["available"] = False
        return res
    res["available"] = True
    h1, h2 = highs[-2].price, highs[-1].price
    l1, l2 = lows[-2].price, lows[-1].price
    if h2 > h1 and l2 > l1:
        res["trend"] = 1
    elif h2 < h1 and l2 < l1:
        res["trend"] = -1
    close = float(df["close"].iloc[-1])
    if res["trend"] == 1:
        if close > h2:
            res["bos"] = 1
        if close < l2:
            res["choch"] = -1
    elif res["trend"] == -1:
        if close < l2:
            res["bos"] = -1
        if close > h2:
            res["choch"] = 1
    res["range_high"], res["range_low"] = h2, l2
    hi, lo = max(h2, l2), min(h2, l2)
    res["equilibrium"] = (hi + lo) / 2
    res["last_swing_high"], res["last_swing_low"] = h2, l2
    return res
