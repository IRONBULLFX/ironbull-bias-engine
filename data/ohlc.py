"""OHLC providers. Index = bar OPEN time (UTC). Forming bars are removed here (closed_only), so the
engines only ever see closed candles. Needs TWELVE_DATA_KEY (free plan: 8 credits/min, 800/day).
One request = one credit. A full run (9 symbols x 4 timeframes) is 36 credits, about 5 minutes."""
import os
import time
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd
from data.http import get_json

TF = {"M15": ("15min", 900), "H1": ("1h", 3600), "H4": ("4h", 14400), "D1": ("1day", 86400)}
TD_URL = "https://api.twelvedata.com/time_series"
DXY_PAIRS = ("EUR/USD", "USD/JPY", "GBP/USD", "USD/CAD", "USD/SEK", "USD/CHF")
DXY_EXPONENTS = {"EUR/USD": -0.576, "USD/JPY": 0.136, "GBP/USD": -0.119,
                 "USD/CAD": 0.091, "USD/SEK": 0.042, "USD/CHF": 0.036}
DXY_CONST = 50.14348112


class ProviderError(Exception):
    pass


class RateLimiter:
    def __init__(self, per_minute=8, clock=time.monotonic, sleep=time.sleep):
        self.gap, self.clock, self.sleep, self.last = 60.0 / per_minute, clock, sleep, None

    def wait(self):
        if self.last is not None:
            d = self.gap - (self.clock() - self.last)
            if d > 0:
                self.sleep(d)
        self.last = self.clock()


def closed_only(df: pd.DataFrame, seconds: int, now: datetime) -> pd.DataFrame:
    """Drop any bar whose period has not finished yet."""
    end = df.index + pd.to_timedelta(seconds, unit="s")
    return df[end <= pd.Timestamp(now)]


def parse_twelvedata(payload) -> pd.DataFrame:
    if not isinstance(payload, dict) or payload.get("status") == "error":
        msg = (payload or {}).get("message", "bad payload") if isinstance(payload, dict) else "bad payload"
        raise ProviderError(str(msg)[:200])
    rows = payload.get("values") or []
    if not rows:
        raise ProviderError("no candles returned")
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["datetime"], utc=True)
    df = df.set_index("ts").sort_index()
    out = df[["open", "high", "low", "close"]].astype(float)
    if out.isna().any().any() or (out["high"] < out["low"]).any():
        raise ProviderError("corrupt candles")
    return out[~out.index.duplicated(keep="last")]


def synthetic_dxy(frames: dict) -> pd.DataFrame:
    """ICE DXY formula on aligned bars. High/low use the open/close extremes of each bar (tighter than
    pairing every pair's extreme, which never happen together), so wicks are approximate. Label it so."""
    idx = None
    for p in DXY_PAIRS:
        idx = frames[p].index if idx is None else idx.intersection(frames[p].index)
    if len(idx) == 0:
        raise ProviderError("no overlapping bars across DXY pairs")

    def calc(col):
        v = DXY_CONST * np.ones(len(idx))
        for p, e in DXY_EXPONENTS.items():
            v = v * frames[p].loc[idx, col].values ** e
        return v
    o, c = calc("open"), calc("close")
    return pd.DataFrame({"open": o, "high": np.maximum(o, c), "low": np.minimum(o, c), "close": c}, index=idx)


class TwelveDataProvider:
    def __init__(self, key=None, cache_dir="cache", limiter=None, getter=get_json,
                 clock=lambda: datetime.now(timezone.utc)):
        self.key = key or os.environ.get("TWELVE_DATA_KEY")
        if not self.key:
            raise ProviderError("TWELVE_DATA_KEY not set")
        self.cache = Path(cache_dir)
        self.cache.mkdir(exist_ok=True)
        self.limiter, self.getter, self.clock = limiter or RateLimiter(), getter, clock

    def _cache_path(self, symbol, tf):
        return self.cache / f"{symbol.replace('/', '_')}_{tf}.csv"

    def candles(self, symbol: str, tf: str, n: int = 300) -> pd.DataFrame:
        interval, secs = TF[tf]
        path = self._cache_path(symbol, tf)
        now = self.clock()
        if path.exists() and time.time() - path.stat().st_mtime < min(secs / 2, 3600):
            df = pd.read_csv(path, index_col=0, parse_dates=True)
            df.index = pd.to_datetime(df.index, utc=True)
            return closed_only(df, secs, now)
        self.limiter.wait()
        payload, err = self.getter(TD_URL, {"symbol": symbol, "interval": interval, "outputsize": n,
                                            "timezone": "UTC", "order": "ASC", "apikey": self.key})
        if payload is None:
            raise ProviderError(f"{symbol} {tf}: {err}")
        try:
            df = parse_twelvedata(payload)
        except ProviderError as e:
            raise ProviderError(f"{symbol} {tf}: {e}") from None
        df.to_csv(path)
        return closed_only(df, secs, now)


def load_asset_frames(asset: str, cfg: dict, provider) -> dict:
    """Returns {tf: closed-bar DataFrame or None}. Per-timeframe failures become None (engine then reports
    missing data) and are listed in errors."""
    symbol = os.environ.get(f"OHLC_SYMBOL_{asset}", cfg["symbol"])
    frames, errors = {}, {}
    for tf in TF:
        try:
            if cfg["kind"] == "synthetic_dxy":
                pairs = {p: provider.candles(p, tf) for p in DXY_PAIRS}
                frames[tf] = synthetic_dxy(pairs)
            else:
                frames[tf] = provider.candles(symbol, tf)
        except ProviderError as e:
            frames[tf], errors[tf] = None, str(e)
    return {"frames": frames, "errors": errors, "symbol": symbol, "kind": cfg["kind"], "note": cfg["note"]}
