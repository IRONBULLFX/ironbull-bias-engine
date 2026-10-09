import ast
import pathlib
from datetime import datetime, timezone
import numpy as np
import pandas as pd
import pytest

from engines.scoring import classify, agreement, weighted_score, UNAVAILABLE
from engines.surprise import surprise, asset_impact
from engines.fundamental import fundamental_engine
from engines.technical import technical_engine
from engines.structure import confirmed_swings
from engines.sessions import ny_time_to_sast, current_sessions

ROOT = pathlib.Path(__file__).resolve().parents[1]


def make_df(n=400, drift=0.2, seed=1, noise=1.0, start=1000.0, freq="h"):
    rng = np.random.default_rng(seed)
    close = start + np.cumsum(rng.normal(drift, noise, n))
    open_ = np.concatenate([[start], close[:-1]])
    high = np.maximum(open_, close) + rng.random(n) * noise
    low = np.minimum(open_, close) - rng.random(n) * noise
    idx = pd.date_range("2026-01-01", periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=idx)


# ---- classification & scoring
@pytest.mark.parametrize("s,b", [(100, "STRONGLY BULLISH"), (70, "STRONGLY BULLISH"), (69.9, "BULLISH"),
                                 (30, "BULLISH"), (29.9, "NEUTRAL"), (0, "NEUTRAL"), (-29.9, "NEUTRAL"),
                                 (-30, "BEARISH"), (-69.9, "BEARISH"), (-70, "STRONGLY BEARISH"),
                                 (None, UNAVAILABLE)])
def test_classification(s, b):
    assert classify(s) == b


def test_missing_data_is_not_neutral():
    r = fundamental_engine("USD", {"fed_policy_expectations": {"value": 1.0}})
    assert r["score"] is None and r["bias"] == UNAVAILABLE


def test_weights_not_silently_renormalised_below_coverage():
    c = [{"name": "a", "weight": 0.3, "value": 1.0}, {"name": "b", "weight": 0.7, "value": None}]
    score, cov, _, _ = weighted_score(c, 0.5)
    assert score is None and cov == pytest.approx(0.3)


def test_full_bullish_scores_100():
    c = [{"name": "a", "weight": 0.5, "value": 1.0}, {"name": "b", "weight": 0.5, "value": 1.0}]
    assert weighted_score(c, 0.5)[0] == 100


def test_unknown_factor_rejected():
    with pytest.raises(ValueError):
        fundamental_engine("USD", {"typo_factor": {"value": 1}})


def test_agreement_states():
    assert agreement("BULLISH", "STRONGLY BULLISH") == "BULLISH ALIGNMENT"
    assert agreement("BEARISH", "BEARISH") == "BEARISH ALIGNMENT"
    assert agreement("BULLISH", "BEARISH") == "CONFLICTING BIASES"
    assert agreement("NEUTRAL", "NEUTRAL") == "NO CLEAR DIRECTION"
    assert agreement(UNAVAILABLE, "BULLISH") == "INCOMPLETE DATA"


# ---- surprise
def test_surprise_units_and_z():
    s = surprise(0.4, 0.3, hist_std=0.1)
    assert s.raw == pytest.approx(0.1) and s.z == pytest.approx(1.0) and s.direction == 1
    assert surprise(None, 0.3) is None
    assert surprise(0.4, 0.3).z is None  # no invented distribution


def test_hot_cpi_is_usd_up_gold_down_not_blind_rule():
    s = surprise(0.5, 0.3, 0.1)
    assert asset_impact("US CPI m/m", s, asset="USD") > 0
    assert asset_impact("US CPI m/m", s, asset="XAUUSD") < 0
    assert asset_impact("US CPI m/m", s, asset="NASDAQ") < 0


def test_higher_unemployment_is_not_bullish_growth():
    s = surprise(4.6, 4.3, 0.1)
    assert asset_impact("Unemployment Rate", s, regime="growth_focus", asset="US30") < 0


def test_regime_changes_interpretation_of_strong_jobs():
    s = surprise(250, 150, 50)
    infl = asset_impact("Non-Farm Payrolls", s, regime="inflation_focus", asset="NASDAQ")
    grow = asset_impact("Non-Farm Payrolls", s, regime="growth_focus", asset="NASDAQ")
    assert infl < grow


def test_unknown_indicator_returns_none():
    assert asset_impact("Mystery Index", surprise(1, 0, 1)) is None


# ---- timezone
def test_ny_release_to_sast_dst():
    summer = ny_time_to_sast(datetime(2026, 7, 10, 8, 30))
    winter = ny_time_to_sast(datetime(2026, 12, 4, 8, 30))
    assert summer.strftime("%H:%M") == "14:30"
    assert winter.strftime("%H:%M") == "15:30"


def test_weekend_closed():
    assert current_sessions(datetime(2026, 10, 10, 12, tzinfo=timezone.utc)) == ["MARKET CLOSED (weekend)"]


# ---- technical engine
def data_for(df):
    return {"D1": df, "H4": df, "H1": df, "M15": df}


def test_uptrend_bullish_downtrend_bearish():
    up = technical_engine(data_for(make_df(drift=0.4, noise=0.8)))
    dn = technical_engine(data_for(make_df(drift=-0.4, noise=0.8, start=2000)))
    assert up["score"] > 30 and dn["score"] < -30


def test_insufficient_bars_unavailable():
    r = technical_engine({"D1": make_df(n=30), "H4": make_df(n=30)})
    assert r["score"] is None and r["bias"] == UNAVAILABLE


def test_forming_candle_ignored():
    df = make_df()
    base = technical_engine(data_for(df))["score"]
    spiked = df.copy()
    spiked.iloc[-1, spiked.columns.get_loc("close")] = 1e6
    spiked.iloc[-1, spiked.columns.get_loc("high")] = 1e6
    assert technical_engine(data_for(spiked))["score"] == base


def test_swings_do_not_repaint():
    df = make_df(n=300, drift=0.0, noise=2.0)
    full = confirmed_swings(df, 3)
    for cut in (150, 200, 250):
        part = confirmed_swings(df.iloc[:cut], 3)
        known = [s for s in full if s.confirmed_at < cut]
        assert [(s.idx, s.kind, s.price) for s in part] == [(s.idx, s.kind, s.price) for s in known]


def test_historical_prefix_result_independent_of_future():
    df = make_df(n=500)
    a = technical_engine(data_for(df.iloc[:401]), drop_forming=True)["score"]
    b = technical_engine(data_for(df.iloc[:400].copy()), drop_forming=False)["score"]
    assert a == b


def test_mirror_symmetry_no_directional_bias():
    """Flipping the price series upside down must flip the score."""
    for seed in range(1, 61):
        df = make_df(drift=0, noise=0.8, seed=seed)
        m = pd.DataFrame({"open": 4000 - df.open, "high": 4000 - df.low,
                          "low": 4000 - df.high, "close": 4000 - df.close}, index=df.index)
        a = technical_engine(data_for(df))["score"]
        b = technical_engine(data_for(m))["score"]
        assert a is not None and b is not None
        assert abs(a + b) < 0.5, (seed, a, b)


# ---- independence
def test_engines_do_not_import_each_other():
    def imports(p):
        tree = ast.parse((ROOT / "engines" / p).read_text())
        names = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module:
                names.add(n.module)
            if isinstance(n, ast.Import):
                names.update(a.name for a in n.names)
        return names
    assert "engines.fundamental" not in imports("technical.py")
    assert "engines.technical" not in imports("fundamental.py")
    assert "engines.surprise" not in imports("technical.py")
    assert "engines.structure" not in imports("fundamental.py")
