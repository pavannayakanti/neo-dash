"""Market data feeds. Each feed exposes `async stream()` yielding normalised ticks:
    {"symbol": str, "kind": "cash"|"fut", "ltp": float, "prev_close": float|None,
     "volume": float|None, "oi": float|None}
"""
import asyncio
import os
import random
from typing import AsyncIterator, Optional

import clock
from universe import Instrument


# ---------------------------------------------------------------- mock ---------
class MockFeed:
    """Random-walk feed so the UI can be developed outside market hours / without credentials."""

    def __init__(self, universe: list[Instrument], tick_interval: float = 0.25):
        self.universe = universe
        self.tick_interval = tick_interval
        rnd = random.Random(42)
        self.px, self.fpx, self.oi, self.vol, self.drift = {}, {}, {}, {}, {}
        self.last_t: dict = {}
        for ins in universe:
            base = ins.prev_close or rnd.uniform(150, 5000)
            ins.prev_close = ins.prev_close or round(base, 2)
            ins.prev_high = ins.prev_high or round(base * rnd.uniform(1.005, 1.025), 2)
            ins.prev_low = ins.prev_low or round(base * rnd.uniform(0.975, 0.995), 2)
            ins.prev_volume = ins.prev_volume or rnd.uniform(1e6, 2e7)
            ins.fut_prev_close = ins.fut_prev_close or round(base * 1.004, 2)
            ins.prev_oi = ins.prev_oi or rnd.uniform(2e6, 6e7)
            ins.fut_symbol = ins.fut_symbol or f"{ins.symbol}MOCKFUT"
            ins.lot_size = ins.lot_size or rnd.choice([75, 150, 250, 500, 700, 1200])
            self.drift[ins.symbol] = rnd.gauss(0, 0.000015)
            self.px[ins.symbol] = base
            self.fpx[ins.symbol] = base * 1.004
            self.oi[ins.symbol] = ins.prev_oi
            self.vol[ins.symbol] = 0.0 if clock.is_sim() else ins.prev_volume * 0.3
        # pre-seed some intraday history so sparklines aren't empty
        self.seed = []
        for _ in range(0 if clock.is_sim() else 300):   # simulated day starts empty at 09:15
            for ins in universe:
                px, _f = self._step(ins, rnd)
                self.seed.append({"symbol": ins.symbol, "kind": "cash", "ltp": round(px, 2),
                                  "prev_close": ins.prev_close, "volume": self.vol[ins.symbol], "oi": None, "seed": True})

    def _step(self, ins: Instrument, rnd) -> tuple[float, float]:
        s = ins.symbol
        r = self.drift[s] + rnd.gauss(0, 0.0007)
        self.px[s] *= 1 + r
        self.fpx[s] *= 1 + r + rnd.gauss(0, 0.0002)
        # OI tends to rise with trend, sometimes falls (covering / unwinding)
        self.oi[s] *= 1 + abs(r) * rnd.choice([4, 4, 4, -3])
        # volume follows (simulated) time at roughly a normal day's pace, spiking on bigger moves;
        # trending stocks (bigger |drift|) attract more volume, like real movers do
        now = clock.now()
        dt_min = (now - self.last_t[s]).total_seconds() / 60 if s in self.last_t else 0.5
        self.last_t[s] = now
        heat = 1 + min(abs(self.drift[s]) / 0.00002, 2)
        self.vol[s] += ins.prev_volume / 375 * max(dt_min, 0) * rnd.uniform(0.5, 1.3) * (1 + abs(r) / 0.0007) * 0.45 * heat
        return self.px[s], self.fpx[s]

    async def stream(self) -> AsyncIterator[dict]:
        rnd = random.Random()
        for t in self.seed:
            yield t
        while True:
            if clock.is_sim() and not clock.market_open():
                await asyncio.sleep(1)                       # simulated market is closed
                continue
            for ins in rnd.sample(self.universe, k=max(1, len(self.universe) // 4)):
                px, fpx = self._step(ins, rnd)
                yield {"symbol": ins.symbol, "kind": "cash", "ltp": round(px, 2),
                       "prev_close": ins.prev_close, "volume": self.vol[ins.symbol], "oi": None}
                yield {"symbol": ins.symbol, "kind": "fut", "ltp": round(fpx, 2),
                       "prev_close": ins.fut_prev_close, "volume": None, "oi": self.oi[ins.symbol]}
            await asyncio.sleep(self.tick_interval)


# ---------------------------------------------------------------- kotak --------
def _pick(msg, *names) -> Optional[float]:
    for n in names:
        v = getattr(msg, n, None)
        if v not in (None, "", 0, "0"):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


# Candidate attribute names on SFeedScrip. Only `last_traded_price`, `instrument_token`
# and `trading_symbol` are confirmed in Kotak's README -- run with NEO_DEBUG_FIELDS=1 once
# during market hours and adjust these lists to the names it prints.
LTP_FIELDS = ("last_traded_price", "ltp")
PREV_CLOSE_FIELDS = ("close_price", "prev_close", "previous_close", "close")
VOLUME_FIELDS = ("volume", "total_traded_volume", "volume_traded_today", "vol")
OI_FIELDS = ("open_interest", "oi", "total_open_interest")
HIGH_FIELDS = ("high_price", "high", "day_high")
LOW_FIELDS = ("low_price", "low", "day_low")
ATP_FIELDS = ("average_traded_price", "average_price", "atp", "avg_price")   # exchange VWAP
SEGMENT_FIELDS = ("exchange_segment", "segment", "exchange")


class KotakFeed:
    """Live feed via the official Kotak Neo Python SDK (`pip install kotakneoapi`)."""

    def __init__(self, universe: list[Instrument]):
        self.universe = universe
        self.debug = os.getenv("NEO_DEBUG_FIELDS") == "1"
        self._printed = False

    async def stream(self) -> AsyncIterator[dict]:
        from neo_api_client.websocket.feed import SFeedScrip, WsToken

        from neo_session import get_client
        client = await asyncio.to_thread(get_client, True)  # fresh login on every (re)connect

        tokens, by_seg_tok, by_tok = [], {}, {}
        for ins in self.universe:
            for seg, tok, kind in (("nse_cm", ins.cash_token, "cash"), ("nse_fo", ins.fut_token, "fut")):
                if not tok:
                    continue
                tokens.append(WsToken(seg, str(tok)))
                by_seg_tok[(seg, str(tok))] = (ins.symbol, kind)
                if str(tok) in by_tok:
                    print(f"[kotak] warning: token {tok} appears in two segments; relying on segment field")
                by_tok[str(tok)] = (ins.symbol, kind)
        if not tokens:
            raise RuntimeError("No tokens in data/universe.csv -- run tools/fill_tokens.py first")

        async with client.create_websocket() as ws:
            await ws.subscribe_scrips(tokens)
            print(f"[kotak] subscribed {len(tokens)} instruments")
            async for msg in ws:
                if not isinstance(msg, SFeedScrip):
                    continue
                if self.debug and not self._printed:
                    print("[kotak] SFeedScrip fields:", getattr(msg, "__dict__", None) or dir(msg))
                    self._printed = True
                tok = str(getattr(msg, "instrument_token", ""))
                seg = next((str(getattr(msg, f)) for f in SEGMENT_FIELDS if getattr(msg, f, None)), None)
                hit = by_seg_tok.get((seg, tok)) if seg else None
                hit = hit or by_tok.get(tok)
                if not hit:
                    continue
                symbol, kind = hit
                yield {
                    "symbol": symbol, "kind": kind,
                    "ltp": _pick(msg, *LTP_FIELDS),
                    "prev_close": _pick(msg, *PREV_CLOSE_FIELDS),
                    "volume": _pick(msg, *VOLUME_FIELDS),
                    "oi": _pick(msg, *OI_FIELDS) if kind == "fut" else None,
                    "high": _pick(msg, *HIGH_FIELDS), "low": _pick(msg, *LOW_FIELDS),
                    "atp": _pick(msg, *ATP_FIELDS) if kind == "cash" else None,
                }
