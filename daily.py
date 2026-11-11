"""Daily candles for swing trading: load from data/daily/<SYMBOL>.csv (built from NSE bhavcopies by
tools/build_daily.py), or synthesise realistic history in mock mode. Plus the indicators the
swing engine needs (SMA, ATR, RSI, N-day highs/lows, average volume)."""
import csv
import math
import random
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from universe import Instrument

FIELDS = ("date", "open", "high", "low", "close", "volume")


def load_history(folder: Path, symbols: list[str]) -> dict[str, list[dict]]:
    out = {}
    for s in symbols:
        p = folder / f"{s.replace('&', '_')}.csv"
        if not p.exists():
            continue
        rows = []
        for r in csv.DictReader(open(p)):
            try:
                rows.append({"date": r["date"], **{k: float(r[k]) for k in FIELDS[1:]}})
            except (KeyError, ValueError):
                continue
        out[s] = sorted(rows, key=lambda b: b["date"])
    return out


def _weekdays_back(end: date, n: int) -> list[str]:
    d, out = end, []
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append(d.isoformat())
    return out[::-1]


def synth_history(ins: Instrument, today: date, days: int = 260, seed: int = 0) -> list[dict]:
    """Trending-regime random walk ending at the instrument's prev_close (mock mode only)."""
    rnd = random.Random(seed)
    rets, drift = [], 0.0
    for i in range(days):
        if i % rnd.randint(20, 45) == 0:
            drift = rnd.gauss(0.0006, 0.0025)          # regimes: up-trends, down-trends, ranges
        rets.append(drift + rnd.gauss(0, 0.014))
    p = ins.prev_close / math.exp(sum(rets))
    bars = []
    for d, r in zip(_weekdays_back(today, days), rets):
        o = p
        c = p * math.exp(r)
        rng = abs(r) + abs(rnd.gauss(0, 0.008))
        h = max(o, c) * (1 + rng * rnd.uniform(0.2, 0.6))
        l = min(o, c) * (1 - rng * rnd.uniform(0.2, 0.6))
        v = ins.prev_volume * math.exp(rnd.gauss(0, 0.35)) * (1 + 15 * abs(r))
        bars.append({"date": d, "open": round(o, 2), "high": round(h, 2), "low": round(l, 2),
                     "close": round(c, 2), "volume": round(v)})
        p = c
    last = bars[-1]
    ins.prev_close, ins.prev_high, ins.prev_low, ins.prev_volume = last["close"], last["high"], last["low"], last["volume"]
    return bars


def _sma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def indicators(bars: list[dict]) -> Optional[dict]:
    """Indicators on the latest bar (the last element may be today's provisional candle)."""
    if len(bars) < 60:
        return None
    c = [b["close"] for b in bars]
    h = [b["high"] for b in bars]
    l = [b["low"] for b in bars]
    v = [b["volume"] for b in bars]
    trs = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(len(bars) - 14, len(bars))]
    gains = [max(c[i] - c[i - 1], 0) for i in range(len(c) - 14, len(c))]
    losses = [max(c[i - 1] - c[i], 0) for i in range(len(c) - 14, len(c))]
    ag, al = sum(gains) / 14, sum(losses) / 14
    rsi = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    sma20 = _sma(c, 20)
    return {
        "close": c[-1], "prev_close": c[-2], "high": h[-1], "low": l[-1], "volume": v[-1],
        "sma20": sma20, "sma50": _sma(c, 50), "sma200": _sma(c, 200),
        "sma20_5ago": _sma(c[:-5], 20),
        "atr": sum(trs) / 14, "rsi": rsi,
        "hh20_prev": max(h[-21:-1]), "ll5": min(l[-5:]), "ll10": min(l[-10:]),
        "vol_avg20_prev": sum(v[-21:-1]) / 20,
        "low3": min(l[-3:]),
    }
