"""Market analytics: % change, OI build-up classification, pivots, score, breadth, sectors."""
import os
import time
from collections import deque
from datetime import datetime

import clock
from clock import IST
from typing import Optional

from universe import Instrument


LONG_BUILDUP = "Long Build-up"      # price up,   OI up   -> fresh longs
SHORT_BUILDUP = "Short Build-up"    # price down, OI up   -> fresh shorts
SHORT_COVERING = "Short Covering"   # price up,   OI down -> shorts exiting
LONG_UNWINDING = "Long Unwinding"   # price down, OI down -> longs exiting
NEUTRAL = "Neutral"
NO_OI = "No OI"


def classify_buildup(price_pct: Optional[float], oi_pct: Optional[float],
                     price_eps: float = 0.05, oi_eps: float = 0.25) -> str:
    """Classic price-vs-OI matrix. Small moves inside the epsilons count as Neutral."""
    if oi_pct is None or price_pct is None:
        return NO_OI
    if abs(price_pct) < price_eps or abs(oi_pct) < oi_eps:
        return NEUTRAL
    if price_pct > 0:
        return LONG_BUILDUP if oi_pct > 0 else SHORT_COVERING
    return SHORT_BUILDUP if oi_pct > 0 else LONG_UNWINDING


def pivots(h: Optional[float], l: Optional[float], c: Optional[float]) -> Optional[dict]:
    """Classic floor pivots from previous day's high/low/close."""
    if None in (h, l, c):
        return None
    p = (h + l + c) / 3
    return {"s2": p - (h - l), "s1": 2 * p - h, "p": p, "r1": 2 * p - l, "r2": p + (h - l)}


def session_fraction(now: Optional[datetime] = None) -> float:
    """Fraction of the NSE cash session (09:15-15:30 IST) elapsed; 1.0 outside market hours."""
    now = now or clock.now()
    start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    frac = (now - start).total_seconds() / (375 * 60)
    if frac <= 0 or frac >= 1:   # outside session: compare against the full previous day
        return 1.0
    return max(frac, 0.05)


def stock_signal(ltp, hi, lo, vwap, pct) -> str:
    """Plain-language intraday signal for cash-market traders."""
    rng = (hi - lo) if (hi and lo) else 0
    if rng > 0 and pct > 0 and ltp >= hi - 0.1 * rng:
        return "Near day high"
    if rng > 0 and pct < 0 and ltp <= lo + 0.1 * rng:
        return "Near day low"
    if vwap:
        return "Above VWAP" if ltp >= vwap else "Below VWAP"
    return "—"


def score(pct: float, oi_pct: Optional[float], vol_ratio: Optional[float], buildup: str,
          vs_vwap: Optional[float] = None) -> int:
    """0-100 conviction score: momentum 45, OI change (or VWAP distance for stocks) 25,
    relative volume 20, alignment 10. A starting point -- tune the weights to your style."""
    m = min(abs(pct) / 3.0, 1) * 45
    v = min((vol_ratio or 0) / 2.0, 1) * 20
    if oi_pct is not None:
        o = min(abs(oi_pct) / 8.0, 1) * 25
        aligned = (pct > 0 and buildup in (LONG_BUILDUP, SHORT_COVERING)) or \
                  (pct < 0 and buildup in (SHORT_BUILDUP, LONG_UNWINDING))
    else:  # stock mode: price holding above/below VWAP in the direction of the move
        o = min(abs(vs_vwap or 0) / 1.5, 1) * 25
        aligned = vs_vwap is not None and ((pct > 0 and vs_vwap > 0) or (pct < 0 and vs_vwap < 0))
    return round(m + o + v + (10 if aligned else 0))


class StockState:
    SPARK_LEN = 240  # points kept for the intraday sparkline

    def __init__(self, ins: Instrument):
        self.ins = ins
        self.ltp: Optional[float] = None
        self.ref_close: Optional[float] = ins.prev_close
        self.volume: Optional[float] = None
        self.fut_ltp: Optional[float] = None
        self.fut_ref: Optional[float] = ins.fut_prev_close
        self.oi: Optional[float] = None
        self.oi_ref: Optional[float] = ins.prev_oi
        self.spark: deque = deque(maxlen=self.SPARK_LEN)
        self.day_open: Optional[float] = None
        self.day_high: Optional[float] = None
        self.day_low: Optional[float] = None
        self.atp: Optional[float] = None       # exchange VWAP, if the feed sends it
        self._pv = 0.0                          # fallback VWAP accumulators
        self._v = 0.0
        self._last_vol: Optional[float] = None
        self.or_high: Optional[float] = None   # opening range (first ORB_MINUTES of the session)
        self.or_low: Optional[float] = None
        self.or_end = 9 * 60 + 15 + int(os.getenv("ORB_MINUTES", "15"))
        self._last_spark = 0.0

    def on_cash(self, t: dict):
        if t.get("ltp") is None:
            return
        self.ltp = t["ltp"]
        if self.ref_close is None:
            self.ref_close = t.get("prev_close") or self.ltp
        ltp = self.ltp
        if self.day_open is None and not t.get("seed"):
            self.day_open = ltp
        if not t.get("seed") and clock.hm() < self.or_end:
            self.or_high = max(self.or_high or ltp, ltp)
            self.or_low = min(self.or_low or ltp, ltp)
        self.day_high = max(self.day_high or ltp, t.get("high") or ltp, ltp)
        self.day_low = min(self.day_low or ltp, t.get("low") or ltp, ltp)
        if t.get("atp"):
            self.atp = t["atp"]
        if t.get("volume") is not None:
            if self._last_vol is not None and t["volume"] > self._last_vol:
                dv = t["volume"] - self._last_vol
                self._pv += dv * ltp
                self._v += dv
            self._last_vol = t["volume"]
            self.volume = t["volume"]
        now = time.time()
        if t.get("seed") or now - self._last_spark >= 1.0:  # 1 point/sec max
            self.spark.append(self.ltp)
            self._last_spark = now

    def on_fut(self, t: dict):
        if t.get("ltp") is not None:
            self.fut_ltp = t["ltp"]
            if self.fut_ref is None:
                self.fut_ref = t.get("prev_close") or self.fut_ltp
        if t.get("oi") is not None:
            self.oi = t["oi"]
            if self.oi_ref is None:  # no prev-day OI: fall back to first OI seen today
                self.oi_ref = self.oi

    def view(self, sess_frac: float) -> Optional[dict]:
        if self.ltp is None or not self.ref_close:
            return None
        chg = self.ltp - self.ref_close
        pct = chg / self.ref_close * 100
        fut_pct = ((self.fut_ltp - self.fut_ref) / self.fut_ref * 100) if (self.fut_ltp and self.fut_ref) else pct
        oi_pct = ((self.oi - self.oi_ref) / self.oi_ref * 100) if (self.oi and self.oi_ref) else None
        bu = classify_buildup(fut_pct, oi_pct)
        vwap = self.atp or (self._pv / self._v if self._v else None)
        vs_vwap = ((self.ltp - vwap) / vwap * 100) if vwap else None
        rng = (self.day_high - self.day_low) if (self.day_high and self.day_low) else 0
        range_pos = ((self.ltp - self.day_low) / rng * 100) if rng > 0 else 50.0
        vol_ratio = (self.volume / (self.ins.prev_volume * sess_frac)) \
            if (self.volume and self.ins.prev_volume) else None
        pv = pivots(self.ins.prev_high, self.ins.prev_low, self.ins.prev_close or self.ref_close)
        support = resistance = None
        if pv:
            levels = sorted(pv.values())
            below = [x for x in levels if x <= self.ltp]
            above = [x for x in levels if x > self.ltp]
            support = below[-1] if below else None
            resistance = above[0] if above else None
        return {
            "symbol": self.ins.symbol, "sector": self.ins.sector,
            "ltp": round(self.ltp, 2), "chg": round(chg, 2), "pct": round(pct, 2),
            "fut_ltp": None if self.fut_ltp is None else round(self.fut_ltp, 2),
            "lot_size": self.ins.lot_size,
            "oi_pct": None if oi_pct is None else round(oi_pct, 2),
            "buildup": bu, "score": score(pct, oi_pct, vol_ratio, bu, vs_vwap),
            "signal": stock_signal(self.ltp, self.day_high, self.day_low, vwap, pct),
            "day_high": None if self.day_high is None else round(self.day_high, 2),
            "day_low": None if self.day_low is None else round(self.day_low, 2),
            "vwap": None if vwap is None else round(vwap, 2),
            "vs_vwap": None if vs_vwap is None else round(vs_vwap, 2),
            "range_pos": round(range_pos, 1),
            "or_high": None if self.or_high is None else round(self.or_high, 2),
            "or_low": None if self.or_low is None else round(self.or_low, 2),
            "or_ready": self.or_high is not None and clock.hm() >= self.or_end,
            "bias": "BULLISH" if pct >= 0 else "BEARISH",
            "vol_ratio": None if vol_ratio is None else round(vol_ratio, 2),
            "pivots": {k: round(v, 2) for k, v in pv.items()} if pv else None,
            "support": None if support is None else round(support, 2),
            "resistance": None if resistance is None else round(resistance, 2),
            "spark": [round(x, 2) for x in list(self.spark)[::2]],  # downsample for the wire
        }


class MarketState:
    def __init__(self, universe: list[Instrument]):
        self.stocks = {i.symbol: StockState(i) for i in universe}
        self.mode = "mock"
        self.status = "starting"

    def on_tick(self, t: dict):
        s = self.stocks.get(t["symbol"])
        if not s:
            return
        (s.on_fut if t["kind"] == "fut" else s.on_cash)(t)

    def snapshot(self) -> dict:
        frac = session_fraction()
        rows = [v for v in (s.view(frac) for s in self.stocks.values()) if v]
        adv = sum(1 for r in rows if r["pct"] > 0)
        dec = sum(1 for r in rows if r["pct"] < 0)
        sectors: dict[str, dict] = {}
        for r in rows:
            sec = sectors.setdefault(r["sector"], {"name": r["sector"], "sum": 0.0, "n": 0, "adv": 0, "dec": 0})
            sec["sum"] += r["pct"]; sec["n"] += 1
            sec["adv"] += r["pct"] > 0; sec["dec"] += r["pct"] < 0
        sec_list = sorted(
            ({"name": s["name"], "pct": round(s["sum"] / s["n"], 2), "adv": s["adv"], "dec": s["dec"]}
             for s in sectors.values()), key=lambda s: -s["pct"])
        return {
            "ts": clock.now().strftime("%H:%M:%S"), "date": clock.now().strftime("%Y-%m-%d"), "sim": clock.is_sim(), "mode": self.mode, "status": self.status,
            "breadth": {"adv": adv, "dec": dec, "unch": len(rows) - adv - dec},
            "sectors": sec_list,
            "stocks": sorted(rows, key=lambda r: -r["pct"]),
        }
