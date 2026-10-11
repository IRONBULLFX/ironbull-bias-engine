"""Database layer. Railway Postgres in production (DATABASE_URL), SQLite for tests.
Railway's disk is wiped on every deploy, so everything that must survive lives here."""
import hashlib
import os
from datetime import datetime, timedelta, timezone
from sqlalchemy import (JSON, Column, DateTime, Float, Integer, MetaData, String, Table, create_engine, func, insert,
                        select, update)

meta = MetaData()

snapshots = Table(
    "snapshots", meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("kind", String(20), nullable=False),            # "fundamental"
    Column("created_at", DateTime(timezone=True), nullable=False, index=True),
    Column("inputs_sha256", String(64)),
    Column("payload", JSON, nullable=False),
)

bias_history = Table(
    "bias_history", meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("at", DateTime(timezone=True), nullable=False, index=True),
    Column("asset", String(10), nullable=False, index=True),
    Column("engine", String(12), nullable=False),          # fundamental | technical
    Column("owner", String(16), nullable=False, default="global"),  # "global" or hashed access code
    Column("score", Float),
    Column("bias", String(24), nullable=False),
    Column("prev_bias", String(24)),
    Column("reason", String(500)),
)

chart_readings = Table(
    "chart_readings", meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("owner", String(16), nullable=False, index=True),
    Column("asset", String(10), nullable=False),
    Column("tf", String(4), nullable=False),
    Column("uploaded_at", DateTime(timezone=True), nullable=False),
    Column("image_sha256", String(64)),
    Column("accepted", Integer, nullable=False),
    Column("reject_reason", String(300)),
    Column("raw", JSON, nullable=False),
)

usage = Table(
    "usage", meta,
    Column("owner", String(16), primary_key=True),
    Column("day", String(10), primary_key=True),
    Column("count", Integer, nullable=False, default=0),
)


members = Table(
    "members", meta,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("token_hash", String(64), nullable=False, unique=True),
    Column("account", String(16), nullable=False, index=True),      # HFM trading account number (needed to re-check)
    Column("email_hash", String(64), nullable=False),                 # email is never stored, only its hash
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("verified_at", DateTime(timezone=True), nullable=False),
    Column("revoked", Integer, nullable=False, default=0),
    Column("revoke_reason", String(100)),
)


releases = Table(
    "releases", meta,                                   # every released event the feed has shown, kept past the feed's 7-day window
    Column("id", String(200), primary_key=True),
    Column("name", String(200), nullable=False),
    Column("t_utc", DateTime(timezone=True), nullable=False, index=True),
    Column("forecast", Float), Column("previous", Float), Column("actual", Float),
    Column("unit", String(10)), Column("impact", String(10)), Column("grp", String(20)),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


def secret_hash(value: str) -> str:
    return hashlib.sha256(("ironbull-member:" + value).encode()).hexdigest()


def email_hash(email: str) -> str:
    return secret_hash("email:" + str(email or "").strip().casefold())


def owner_id(code: str) -> str:
    """Access codes are never stored. Only a short hash identifies the uploader."""
    return hashlib.sha256(("ironbull:" + code).encode()).hexdigest()[:16]


def _utc(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)   # SQLite drops tzinfo


def db_url():
    url = os.environ.get("DATABASE_URL", "sqlite:///bias.db")
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


class Store:
    def __init__(self, url=None):
        url = url or db_url()
        kw = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {"pool_pre_ping": True}
        self.engine = create_engine(url, **kw)
        meta.create_all(self.engine)

    # ---- fundamental snapshots
    def save_snapshot(self, payload: dict, kind="fundamental"):
        with self.engine.begin() as c:
            c.execute(insert(snapshots).values(kind=kind, created_at=datetime.fromisoformat(payload["generated_at"]),
                                               inputs_sha256=payload.get("inputs_sha256"), payload=payload))

    def latest_snapshot(self, kind="fundamental"):
        with self.engine.connect() as c:
            r = c.execute(select(snapshots.c.payload).where(snapshots.c.kind == kind)
                          .order_by(snapshots.c.id.desc()).limit(1)).first()
        return r[0] if r else None

    # ---- bias history (only rows where the bias label changed, plus the first ever)
    def last_bias(self, asset, engine, owner="global"):
        with self.engine.connect() as c:
            r = c.execute(select(bias_history.c.bias).where(bias_history.c.asset == asset, bias_history.c.engine == engine,
                                                            bias_history.c.owner == owner)
                          .order_by(bias_history.c.id.desc()).limit(1)).first()
        return r[0] if r else None

    def record_bias(self, at, asset, engine, score, bias, reason="", owner="global"):
        """Returns the change dict if the label changed, else None."""
        prev = self.last_bias(asset, engine, owner)
        if prev == bias:
            return None
        with self.engine.begin() as c:
            c.execute(insert(bias_history).values(at=at, asset=asset, engine=engine, owner=owner, score=score, bias=bias,
                                                  prev_bias=prev, reason=(reason or "")[:500]))
        return {"at": at.isoformat(), "asset": asset, "engine": engine, "from": prev, "to": bias, "score": score, "reason": reason}

    def history(self, asset=None, days=7, owner="global", now=None):
        now = now or datetime.now(timezone.utc)
        q = select(bias_history).where(bias_history.c.at >= now - timedelta(days=days),
                                       bias_history.c.owner.in_(["global", owner]))
        if asset:
            q = q.where(bias_history.c.asset == asset)
        with self.engine.connect() as c:
            rows = c.execute(q.order_by(bias_history.c.at.desc()).limit(500)).mappings().all()
        return [{"at": _utc(r["at"]).isoformat(), "asset": r["asset"], "engine": r["engine"], "score": r["score"],
                 "bias": r["bias"], "prev_bias": r["prev_bias"], "reason": r["reason"]} for r in rows]

    def score_series(self, asset, days=7, now=None):
        """Fundamental score over time, from stored snapshots (for the history chart)."""
        now = now or datetime.now(timezone.utc)
        with self.engine.connect() as c:
            rows = c.execute(select(snapshots.c.created_at, snapshots.c.payload)
                             .where(snapshots.c.kind == "fundamental", snapshots.c.created_at >= now - timedelta(days=days))
                             .order_by(snapshots.c.created_at)).all()
        out = []
        for at, p in rows:
            f = p["markets"].get(asset, {}).get("fundamental", {})
            out.append({"at": _utc(at).isoformat(), "score": f.get("score"), "bias": f.get("bias")})
        return out

    # ---- chart readings (latest per owner/asset/tf wins)
    def save_reading(self, owner, asset, tf, raw, uploaded_at, image_sha, accepted, reject_reason=None):
        with self.engine.begin() as c:
            c.execute(insert(chart_readings).values(owner=owner, asset=asset, tf=tf, raw=raw, uploaded_at=uploaded_at,
                                                    image_sha256=image_sha, accepted=1 if accepted else 0,
                                                    reject_reason=reject_reason))

    def latest_readings(self, owner) -> dict:
        """{asset: {tf: {"raw", "uploaded_at"}}} using each owner's newest ACCEPTED reading per asset/timeframe.
        A newer rejected upload does not delete an older accepted one; expiry still applies when scoring."""
        with self.engine.connect() as c:
            rows = c.execute(select(chart_readings).where(chart_readings.c.owner == owner, chart_readings.c.accepted == 1)
                             .order_by(chart_readings.c.id)).mappings().all()
        out: dict = {}
        for r in rows:
            out.setdefault(r["asset"], {})[r["tf"]] = {"raw": r["raw"], "uploaded_at": _utc(r["uploaded_at"])}
        return out

    # ---- usage limits
    def use(self, owner, now, per_owner_limit, global_limit) -> tuple[bool, str]:
        """Counts one upload. Refuses (without counting) when the member or the whole service is at its daily cap."""
        day = now.strftime("%Y-%m-%d")
        with self.engine.begin() as c:
            total = c.execute(select(func.coalesce(func.sum(usage.c.count), 0)).where(usage.c.day == day)).scalar()
            if total >= global_limit:
                return False, "daily service limit reached, try again tomorrow"
            row = c.execute(select(usage.c.count).where(usage.c.owner == owner, usage.c.day == day)).first()
            if row and row[0] >= per_owner_limit:
                return False, f"your daily limit of {per_owner_limit} charts is reached"
            if row:
                c.execute(update(usage).where(usage.c.owner == owner, usage.c.day == day).values(count=row[0] + 1))
            else:
                c.execute(insert(usage).values(owner=owner, day=day, count=1))
        return True, ""

    # ---- release memory
    def upsert_releases(self, events, now):
        """Stores released events (actual known). A later feed value for the same id (revision) overwrites."""
        n = 0
        with self.engine.begin() as c:
            for e in events or []:
                if e.actual is None or not e.id:
                    continue
                vals = dict(name=e.name, t_utc=e.t_utc, forecast=e.forecast, previous=e.previous, actual=e.actual,
                            unit=e.unit, impact=e.impact, grp=e.group, updated_at=now)
                if c.execute(update(releases).where(releases.c.id == e.id).values(**vals)).rowcount == 0:
                    c.execute(insert(releases).values(id=e.id, **vals))
                n += 1
        return n

    def releases_empty(self):
        with self.engine.connect() as c:
            return c.execute(select(func.count()).select_from(releases)).scalar() == 0

    def snapshot_payloads(self, now, days, limit=3000):
        with self.engine.connect() as c:
            rows = c.execute(select(snapshots.c.payload).where(snapshots.c.kind == "fundamental",
                                                               snapshots.c.created_at >= now - timedelta(days=days))
                             .order_by(snapshots.c.id).limit(limit)).all()
        return [r[0] for r in rows]

    def recent_releases(self, now, days):
        from data.macro_client import Event
        with self.engine.connect() as c:
            rows = c.execute(select(releases).where(releases.c.t_utc >= now - timedelta(days=days),
                                                    releases.c.t_utc <= now)).mappings().all()
        return [Event(r["id"], r["name"], _utc(r["t_utc"]), r["forecast"], r["previous"], r["actual"],
                      r["unit"] or "", r["impact"] or "", r["grp"] or "") for r in rows]

    # ---- HFM members
    def add_member(self, token, account, email, now):
        with self.engine.begin() as c:
            c.execute(insert(members).values(token_hash=secret_hash(token), account=account, email_hash=email_hash(email),
                                             created_at=now, verified_at=now, revoked=0))

    def member_by_token(self, token):
        with self.engine.connect() as c:
            r = c.execute(select(members).where(members.c.token_hash == secret_hash(token))).mappings().first()
        if not r:
            return None
        d = dict(r)
        d["verified_at"], d["created_at"] = _utc(d["verified_at"]), _utc(d["created_at"])
        return d

    def touch_member(self, account, now):
        with self.engine.begin() as c:
            c.execute(update(members).where(members.c.account == account, members.c.revoked == 0).values(verified_at=now))

    def revoke_account(self, account, reason):
        with self.engine.begin() as c:
            c.execute(update(members).where(members.c.account == account, members.c.revoked == 0)
                      .values(revoked=1, revoke_reason=reason[:100]))

    def member_counts(self):
        with self.engine.connect() as c:
            active = c.execute(select(func.count(func.distinct(members.c.account))).where(members.c.revoked == 0)).scalar()
            revoked = c.execute(select(func.count(func.distinct(members.c.account))).where(members.c.revoked == 1)).scalar()
        return {"active_accounts": active, "revoked_accounts": revoked}

    def used_today(self, owner, now):
        with self.engine.connect() as c:
            r = c.execute(select(usage.c.count).where(usage.c.owner == owner, usage.c.day == now.strftime("%Y-%m-%d"))).first()
        return r[0] if r else 0
