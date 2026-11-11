"""Market clock. Live: real IST time. Mock: a simulated trading day that starts at 09:15
and runs MOCK_TIME_SCALE times faster (default 20x -> a full session in ~19 minutes),
so signals, phases and the intraday square-off can be watched outside market hours."""
import time
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

_sim = False
_scale = 1.0
_t0 = 0.0
_start: datetime = None
_end_hold = None


def configure(sim: bool, scale: float = 20.0):
    global _sim, _scale, _t0, _start, _end_hold
    _sim, _scale = sim, scale
    if sim:
        d = datetime.now(IST)
        while d.weekday() >= 5:                 # use the most recent weekday's date
            d -= timedelta(days=1)
        _start = d.replace(hour=9, minute=15, second=0, microsecond=0)
        _end_hold = d.replace(hour=15, minute=35, second=0, microsecond=0)
        _t0 = time.time()


def is_sim() -> bool:
    return _sim


def now() -> datetime:
    if not _sim:
        return datetime.now(IST)
    t = _start + timedelta(seconds=(time.time() - _t0) * _scale)
    hold = _start.replace(hour=15, minute=35, second=0, microsecond=0)
    return t if _start >= hold else min(t, hold)   # freeze after the close


def hm(t: datetime = None) -> int:
    """Minutes since midnight."""
    t = t or now()
    return t.hour * 60 + t.minute


def parse_hm(s: str) -> int:
    h, m = map(int, s.split(":"))
    return h * 60 + m


def market_open(t: datetime = None) -> bool:
    t = t or now()
    return t.weekday() < 5 and 9 * 60 + 15 <= hm(t) < 15 * 60 + 30
