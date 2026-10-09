import copy
import json
import pathlib
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd
import pytest

from data.macro_client import MacroClient, parse_calendar, parse_yields, parse_fed, Event
from data.fred import real_yields, last_observations
from data.ohlc import (closed_only, parse_twelvedata, synthetic_dxy, RateLimiter, TwelveDataProvider,
                       ProviderError, DXY_PAIRS, DXY_CONST)
from data.fundamental_inputs import build_factors, release_impacts
from engines.fundamental import fundamental_engine
from pipeline import run_bias, ASSETS
from tests.test_engines import make_df

FX = pathlib.Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 9, 17, 0, tzinfo=timezone.utc)


def load(name):
    return json.loads((FX / name).read_text())


def macro_fixture():
    return {"events": parse_calendar(load("calendar.json")), "yields": parse_yields(load("yields.json")),
            "fed": parse_fed(load("fed.json")), "real": None, "errors": {}}


# ---- macro client
def test_parse_real_captured_payloads():
    ev = parse_calendar(load("calendar.json"))
    assert len(ev) == 3 and ev[0].t_utc.tzinfo is not None
    nfp = [e for e in ev if "Non-Farm" in e.name][0]
    assert (nfp.actual, nfp.forecast) == (29, 89)
    assert parse_yields(load("yields.json"))["y2"] == 4.77
    assert parse_fed(load("fed.json"))["hawkish"] == 0.96


def test_bad_rows_and_values_rejected():
    assert parse_calendar({"events": [{"name": "x"}, {"t": 1, "name": ""}]}) == []
    assert parse_fed({"fed": {"hawkishScore": 7}}) is None
    assert parse_yields({"yields": {}}) is None


def test_client_serves_stale_on_failure_and_never_invents():
    calls = {"n": 0}

    def getter(url):
        calls["n"] += 1
        return (load("yields.json"), None) if calls["n"] == 1 else (None, "HTTP 502")
    t = [NOW]
    c = MacroClient("http://x", ttl_seconds=60, getter=getter, clock=lambda: t[0])
    assert c.yields().ok and not c.yields().stale  # second call served from cache
    t[0] = NOW + timedelta(minutes=5)
    r = c.yields()
    assert r.ok and r.stale and r.error == "HTTP 502"
    c2 = MacroClient("http://x", getter=lambda u: (None, "HTTP 500"), clock=lambda: NOW)
    r2 = c2.fed()
    assert not r2.ok and r2.data is None


def test_backend_ok_false_is_failure():
    c = MacroClient("http://x", getter=lambda u: ({"ok": False}, None), clock=lambda: NOW)
    assert not c.calendar().ok


# ---- FRED
def test_fred_skips_missing_dots():
    payload = {"observations": [{"date": "2026-10-09", "value": "."}, {"date": "2026-10-08", "value": "1.9"},
                                {"date": "2026-10-07", "value": "1.8"}]}
    obs, err = last_observations("DFII10", "k", 2, getter=lambda u, p: (payload, None))
    assert obs == [("2026-10-08", 1.9), ("2026-10-07", 1.8)]


def test_fred_requires_key(monkeypatch):
    monkeypatch.delenv("FRED_KEY", raising=False)
    assert real_yields() == (None, "FRED_KEY not set")


# ---- OHLC
def test_closed_only_drops_forming_bar():
    df = make_df(n=10, freq="h")
    now = df.index[-1] + pd.Timedelta(minutes=20)  # last hour bar still forming
    out = closed_only(df, 3600, now.to_pydatetime())
    assert len(out) == 9 and out.index[-1] == df.index[-2]


def test_parse_twelvedata_and_errors():
    ok = {"status": "ok", "values": [{"datetime": "2026-10-09 14:00:00", "open": "2", "high": "3", "low": "1", "close": "2.5"},
                                     {"datetime": "2026-10-09 13:00:00", "open": "1", "high": "2", "low": "0.5", "close": "2"}]}
    df = parse_twelvedata(ok)
    assert df.index.is_monotonic_increasing and df["close"].iloc[-1] == 2.5
    with pytest.raises(ProviderError, match="Grow"):
        parse_twelvedata({"status": "error", "code": 404, "message": "available starting with the Grow plan"})
    with pytest.raises(ProviderError):
        parse_twelvedata({"status": "ok", "values": [{"datetime": "2026-10-09", "open": "1", "high": "1", "low": "2", "close": "1"}]})


def test_dxy_formula():
    idx = pd.date_range("2026-01-01", periods=5, freq="h", tz="UTC")

    def frames(**ov):
        return {p: pd.DataFrame({k: [ov.get(p, 1.0)] * 5 for k in ("open", "high", "low", "close")}, index=idx) for p in DXY_PAIRS}
    base = synthetic_dxy(frames())
    assert base["close"].iloc[0] == pytest.approx(DXY_CONST)
    assert synthetic_dxy(frames(**{"EUR/USD": 1.1}))["close"].iloc[0] < base["close"].iloc[0]   # euro up, dollar down
    assert synthetic_dxy(frames(**{"USD/JPY": 1.1}))["close"].iloc[0] > base["close"].iloc[0]   # yen weaker, dollar up


def test_rate_limiter_spacing():
    t = [0.0]
    slept = []

    def sleep(d):
        slept.append(d)
        t[0] += d
    rl = RateLimiter(8, clock=lambda: t[0], sleep=sleep)
    for _ in range(3):
        rl.wait()
    assert slept == [pytest.approx(7.5), pytest.approx(7.5)]


def test_provider_caches_and_hides_key_in_errors(tmp_path):
    n = {"calls": 0}
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    def getter(url, params):
        n["calls"] += 1
        vals = [{"datetime": (base - timedelta(hours=i + 2)).strftime("%Y-%m-%d %H:%M:%S"), "open": "1", "high": "2", "low": "0.5", "close": "1.5"} for i in range(5)]
        return {"status": "ok", "values": vals}, None
    p = TwelveDataProvider("SECRETKEY", tmp_path, RateLimiter(8, sleep=lambda d: None), getter)
    assert len(p.candles("XAU/USD", "H1")) == 5
    p.candles("XAU/USD", "H1")
    assert n["calls"] == 1  # second call came from cache
    bad = TwelveDataProvider("SECRETKEY", tmp_path / "b", RateLimiter(8, sleep=lambda d: None), lambda u, q: (None, "HTTP 429"))
    with pytest.raises(ProviderError) as e:
        bad.candles("XAU/USD", "H1")
    assert "SECRETKEY" not in str(e.value)


# ---- factor builder
def test_weak_jobs_bearish_usd_not_blind_rule():
    ev = macro_fixture()["events"]
    imp = release_impacts(ev, "USD", NOW)
    assert imp["labour"][0] < 0       # NFP miss + higher unemployment
    assert imp["inflation"][0] < 0    # soft wages read as dovish


def test_unreleased_and_future_events_ignored():
    future = Event("f", "Core CPI m/m", NOW + timedelta(hours=2), 0.3, 0.3, 0.9, "%", "high", "inflation")
    unreleased = Event("u", "Core CPI m/m", NOW - timedelta(hours=1), 0.3, 0.3, None, "%", "high", "inflation")
    assert release_impacts([future, unreleased], "USD", NOW) == {}


def test_old_events_outside_window_ignored():
    old = Event("o", "Core CPI m/m", NOW - timedelta(days=30), 0.3, 0.3, 0.5, "%", "high", "inflation")
    assert release_impacts([old], "USD", NOW) == {}


def test_gold_has_own_inputs_not_reversed_usd():
    m = macro_fixture()
    gold = build_factors("XAUUSD", NOW, m["events"], m["yields"], m["fed"])
    assert "usd_pressure" in gold and "inflation_surprise" not in gold
    real = {"real10": 1.9, "real10_prev": 2.0, "real10_date": "2026-10-08", "be10": 2.3, "be10_prev": 2.3, "be10_date": "2026-10-08"}
    g2 = build_factors("XAUUSD", NOW, m["events"], m["yields"], m["fed"], real)
    assert g2["real_yield_trend"]["value"] > 0  # real yields falling = tailwind


def test_missing_sources_reduce_coverage_not_fake_neutral():
    r = fundamental_engine("USD", build_factors("USD", NOW, [], None, None))
    assert r["score"] is None and r["bias"] == "DATA UNAVAILABLE"


def test_partial_coverage_caps_score():
    f = {"fed_policy_expectations": {"value": 1.0}, "inflation_surprise": {"value": 1.0}, "labour_surprise": {"value": 1.0}}
    r = fundamental_engine("USD", f)       # 0.65 of weight present, all maximally bullish
    assert r["score"] == pytest.approx(65.0) and r["bias"] == "BULLISH"


# ---- pipeline
def fake_ohlc(seed_shift=0):
    return {a: {"frames": {tf: make_df(seed=i + seed_shift, drift=0.2) for tf in ("D1", "H4", "H1", "M15")},
                "errors": {}, "symbol": "S", "kind": "demo", "note": ""} for i, a in enumerate(ASSETS)}


def test_pipeline_engines_independent():
    base = run_bias(fake_ohlc(), macro_fixture(), NOW, "DEMO")
    other_prices = run_bias(fake_ohlc(50), macro_fixture(), NOW, "DEMO")
    m2 = macro_fixture()
    m2["yields"]["y2"] = 6.0
    m2["fed"]["hawkish"] = 0.1
    other_macro = run_bias(fake_ohlc(), m2, NOW, "DEMO")
    for a in ASSETS:
        assert base["markets"][a]["fundamental"] == other_prices["markets"][a]["fundamental"]   # prices cannot move it
        assert base["markets"][a]["technical"] == other_macro["markets"][a]["technical"]        # macro cannot move it
    assert base["markets"]["USD"]["fundamental"] != other_macro["markets"]["USD"]["fundamental"]


def test_snapshot_hash_stable_and_demo_flagged():
    a = run_bias(fake_ohlc(), macro_fixture(), NOW, "DEMO (synthetic)")
    b = run_bias(fake_ohlc(), macro_fixture(), NOW, "DEMO (synthetic)")
    assert a["inputs_sha256"] == b["inputs_sha256"] and "warning" in a
    assert "warning" not in run_bias(fake_ohlc(), macro_fixture(), NOW, "LIVE")
    json.dumps(a)  # fully serialisable


def test_missing_ohlc_gives_unavailable_not_neutral():
    ohlc = fake_ohlc()
    ohlc["US30"]["frames"] = {tf: None for tf in ("D1", "H4", "H1", "M15")}
    r = run_bias(ohlc, macro_fixture(), NOW, "LIVE")
    assert r["markets"]["US30"]["technical"]["bias"] == "DATA UNAVAILABLE"
    assert r["markets"]["US30"]["agreement"] == "INCOMPLETE DATA"
