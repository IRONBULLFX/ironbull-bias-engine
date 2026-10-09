"""SAST conversion and market sessions. Uses real tz database so US DST changes are automatic."""
from datetime import datetime, time
from zoneinfo import ZoneInfo

SAST = ZoneInfo("Africa/Johannesburg")
NY = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")
TOKYO = ZoneInfo("Asia/Tokyo")


def to_sast(dt_utc: datetime) -> datetime:
    return dt_utc.astimezone(SAST)


def ny_time_to_sast(dt_naive_ny: datetime) -> datetime:
    """e.g. 08:30 New York release time on a given date -> SAST (DST aware)."""
    return dt_naive_ny.replace(tzinfo=NY).astimezone(SAST)


def current_sessions(dt_utc: datetime) -> list[str]:
    out = []
    if dt_utc.weekday() >= 5:
        return ["MARKET CLOSED (weekend)"]
    for name, tz, o, c in (("Tokyo", TOKYO, time(9), time(15)),
                           ("London", LONDON, time(8), time(16, 30)),
                           ("New York", NY, time(8), time(17))):
        t = dt_utc.astimezone(tz).time()
        if o <= t < c:
            out.append(name)
    return out or ["Off-hours"]
