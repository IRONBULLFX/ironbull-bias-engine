import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import service.app as appmod
from data.macro_client import Event, MacroClient
from pipeline import next_event, refresh_interval, run_fundamental
from service.store import Store, owner_id
from tests.test_charts import reading, stress
from tests.test_data import load

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def backend_getter(url):
    name = url.rsplit("/", 1)[-1]
    return load(f"{name}.json"), None


def fake_vision(payload):
    def create(**kw):
        return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(payload))])
    return SimpleNamespace(messages=SimpleNamespace(create=create))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DISABLE_SCHEDULER", "1")
    monkeypatch.setenv("ACCESS_CODES", "GOLD-123,VIP-999")
    monkeypatch.setenv("ADMIN_TOKEN", "admintok")
    monkeypatch.setenv("DAILY_UPLOAD_LIMIT", "3")
    monkeypatch.setenv("DAILY_GLOBAL_LIMIT", "5")
    monkeypatch.delenv("FRED_KEY", raising=False)
    appmod.S.store = Store(f"sqlite:///{tmp_path}/t.db")
    appmod.S.client = MacroClient("http://backend", getter=backend_getter)
    appmod.S.vision_client = fake_vision(reading(instrument_seen=None, timeframe_seen=None))
    appmod.S.changes, appmod.S.last_error = [], None
    with TestClient(appmod.app) as c:
        yield c
    appmod.S.store = appmod.S.client = appmod.S.vision_client = None


def upload(c, tf="H4", asset="XAUUSD", code="GOLD-123", data=PNG):
    return c.post("/api/analyze", data={"asset": asset, "tf": tf}, files={"file": ("c.png", data, "image/png")},
                  headers={"X-Access-Code": code} if code else {})


# ---- refresh + public dashboard
def test_refresh_stores_snapshot_and_dashboard_shows_it(client):
    assert client.get("/health").json()["stale"] is True
    assert client.post("/api/refresh").status_code == 401
    r = client.post("/api/refresh", headers={"X-Admin-Token": "admintok"}).json()
    assert r["ok"] and set(r["markets"]) == {"USD", "XAUUSD", "US30", "NASDAQ"}
    b = client.get("/api/bias").json()
    assert b["status"]["stale"] is False and b["status"]["last_calculation"]
    assert b["markets"]["USD"]["fundamental"]["score"] is not None
    assert "FRED_KEY not set" in json.dumps(b["status"]["data_errors"])      # missing source is reported
    h = client.get("/health").json()
    assert h["sources_failing"] == ["real", "stress"] and h["macro_backend_down"] is False
    assert b["markets"]["USD"]["technical"]["bias"] == "DATA UNAVAILABLE"      # no code, no charts


def test_bias_change_recorded_once(client):
    client.post("/api/refresh", headers={"X-Admin-Token": "admintok"})
    client.post("/api/refresh", headers={"X-Admin-Token": "admintok"})
    ch = [c for c in client.get("/api/history?asset=USD").json()["changes"] if c["engine"] == "fundamental"]
    assert len(ch) == 1 and ch[0]["prev_bias"] is None


def test_empty_db_is_unavailable_not_neutral(client):
    b = client.get("/api/bias").json()
    assert all(m["fundamental"]["bias"] == "DATA UNAVAILABLE" for m in b["markets"].values())


# ---- uploads
def test_upload_requires_valid_code(client):
    assert upload(client, code=None).status_code == 401
    assert upload(client, code="guess").status_code == 401


def test_upload_flow_and_per_member_isolation(client):
    client.post("/api/refresh", headers={"X-Admin-Token": "admintok"})
    r1 = upload(client, "H4").json()
    assert r1["accepted"] and r1["technical"]["bias"] == "DATA UNAVAILABLE"  # H4 alone is only 30% coverage
    appmod.S.vision_client = fake_vision(reading(instrument_seen=None, timeframe_seen="D1"))
    r2 = upload(client, "D1").json()
    assert r2["technical"]["score"] > 30
    assert r2["agreement"] == "INCOMPLETE DATA"        # gold fundamentals need FRED, which this test has not set
    upload(client, "H4", asset="USD")
    r3 = upload(client, "D1", asset="USD", code="VIP-999").json()
    assert r3["technical"]["bias"] == "DATA UNAVAILABLE"   # VIP-999 has no H4 USD chart of its own
    mine = client.get("/api/bias", headers={"X-Access-Code": "GOLD-123"}).json()
    other = client.get("/api/bias", headers={"X-Access-Code": "VIP-999"}).json()
    assert mine["markets"]["XAUUSD"]["technical"]["score"] is not None
    assert other["markets"]["XAUUSD"]["technical"]["bias"] == "DATA UNAVAILABLE"   # members don't share charts
    assert mine["uploads_today"] == 3 and mine["upload_limit"] == 3


def test_rejected_reading_does_not_erase_good_one(client):
    upload(client, "H4")
    appmod.S.vision_client = fake_vision(reading(instrument_seen=None, timeframe_seen="D1"))
    upload(client, "D1")
    appmod.S.vision_client = fake_vision({"readable": False, "reason_unreadable": "blurry"})
    r = upload(client, "H4").json()
    assert r["accepted"] is False and "blurry" in r["reject_reason"]
    assert r["technical"]["score"] is not None


def test_wrong_instrument_rejected(client):
    appmod.S.vision_client = fake_vision(reading(instrument_seen="EURUSD", timeframe_seen=None))
    r = upload(client).json()
    assert r["accepted"] is False and "EURUSD" in r["reject_reason"]


def test_bad_file_rejected_before_any_cost(client):
    called = {"n": 0}

    def create(**kw):
        called["n"] += 1
    appmod.S.vision_client = SimpleNamespace(messages=SimpleNamespace(create=create))
    assert upload(client, data=b"%PDF-1.7 nope").status_code == 400
    assert upload(client, data=PNG + b"0" * (6 * 1024 * 1024)).status_code == 400
    assert called["n"] == 0
    assert client.get("/api/bias", headers={"X-Access-Code": "GOLD-123"}).json()["uploads_today"] == 0


def test_daily_limits(client):
    for _ in range(3):
        assert upload(client).status_code == 200
    r = upload(client)
    assert r.status_code == 429 and "your daily limit" in r.json()["detail"]
    assert upload(client, code="VIP-999").status_code == 200
    assert upload(client, code="VIP-999").status_code == 200        # service total now 5
    r = upload(client, code="VIP-999")
    assert r.status_code == 429 and "service limit" in r.json()["detail"]


def test_vision_failure_is_502_not_fake_bias(client):
    def create(**kw):
        raise RuntimeError("down")
    appmod.S.vision_client = SimpleNamespace(messages=SimpleNamespace(create=create))
    r = upload(client)
    assert r.status_code == 502


def test_bad_inputs(client):
    assert upload(client, asset="EURUSD").status_code == 400
    assert upload(client, tf="W1").status_code == 400
    assert client.get("/api/history?asset=BTC").status_code == 400
    assert client.get("/api/bias", headers={"X-Access-Code": "nope"}).status_code == 401


def test_codes_never_stored(client, tmp_path):
    upload(client)
    import sqlite3
    db = sqlite3.connect(f"{tmp_path}/t.db")
    dump = "\n".join(db.iterdump())
    assert "GOLD-123" not in dump and owner_id("GOLD-123") in dump


# ---- scheduling helpers
def ev(mins, impact="high", name="Core CPI m/m"):
    return Event("x", name, NOW + timedelta(minutes=mins), 0.3, 0.3, None, "%", impact, "inflation")


def test_next_event_and_pre_event_warning():
    assert next_event([ev(-10), ev(25), ev(90)], NOW)["minutes_until"] == 25
    assert next_event([ev(25)], NOW)["pre_event_warning"] is True
    assert next_event([ev(45)], NOW)["pre_event_warning"] is False
    assert next_event([ev(30, "medium")], NOW) is None


def test_refresh_interval_speeds_up_around_releases():
    assert refresh_interval([ev(20)], NOW) == 120
    assert refresh_interval([ev(-50)], NOW) == 120
    assert refresh_interval([ev(120)], NOW) == 600
    assert refresh_interval(None, NOW) == 600


def test_events_roundtrip_through_snapshot():
    snap = run_fundamental({"events": [ev(20)], "yields": None, "fed": None, "errors": {}}, NOW)
    back = appmod.events_from_snapshot(snap)
    assert len(back) == 1 and back[0].t_utc == NOW + timedelta(minutes=20)


def test_run_fundamental_hash_ignores_fetch_time():
    m = {"events": [], "yields": None, "fed": None, "stress": dict(stress(), fetched_at=NOW), "errors": {}}
    m2 = dict(m, stress=dict(stress(), fetched_at=NOW + timedelta(hours=1)))
    assert run_fundamental(m, NOW)["inputs_sha256"] == run_fundamental(m2, NOW)["inputs_sha256"]


def test_database_url_normalised(monkeypatch):
    from service.store import db_url
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@h:5432/db")
    assert db_url() == "postgresql+psycopg://u:p@h:5432/db"
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    assert db_url() == "postgresql+psycopg://u:p@h/db"
