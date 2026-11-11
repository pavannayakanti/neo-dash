"""FastAPI server: runs the feed, keeps market state, pushes snapshots to browsers over a websocket.

    FEED_MODE=mock  uvicorn app:app --port 8000     # demo data, no credentials
    FEED_MODE=kotak uvicorn app:app --port 8000     # live Kotak Neo feed
Orders: TRADING_MODE=paper (default) simulates fills; TRADING_MODE=live sends real orders.
Keep the server on 127.0.0.1 (uvicorn's default) -- never expose order endpoints publicly.
"""
import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import clock
from analytics import MarketState
from feed import KotakFeed, MockFeed
from daily import load_history, synth_history
from signals import SignalEngine
from swing import SwingEngine
from trading import OrderDesk
from universe import load_universe

load_dotenv()
BASE = Path(__file__).parent
MODE = os.getenv("FEED_MODE", "mock").lower()
# mock mode replays a simulated trading day (09:15 -> 15:30) at MOCK_TIME_SCALE x speed
clock.configure(sim=(MODE == "mock" and float(os.getenv("MOCK_TIME_SCALE", "20")) > 0),
                scale=float(os.getenv("MOCK_TIME_SCALE", "20")))
PUSH_INTERVAL = float(os.getenv("PUSH_INTERVAL", "1"))

universe = load_universe(BASE / "data/universe.csv", BASE / "data/prevday.csv")
state = MarketState(universe)
state.mode = MODE
clients: set[WebSocket] = set()


def _client_provider():
    from neo_session import get_client
    return get_client()


desk = OrderDesk(state, universe, BASE, client_provider=_client_provider)
engine = SignalEngine(desk, BASE)

# daily candles for swing: real files from tools/build_daily.py, or synthetic history in mock mode
mock_feed = MockFeed(universe) if MODE != "kotak" else None
history = load_history(BASE / "data/daily", [i.symbol for i in universe])
if not history and mock_feed:
    today = clock.now().date()
    history = {i.symbol: synth_history(i, today, seed=n) for n, i in enumerate(universe)}
    for i in universe:                       # keep the intraday feed consistent with the synthetic history
        mock_feed.px[i.symbol] = mock_feed.fpx[i.symbol] = i.prev_close
swing = SwingEngine(desk, state, history)


def full_snapshot() -> dict:
    snap = state.snapshot()
    snap["trading"] = desk.summary()
    snap["signals"] = engine.evaluate(snap)
    snap["swing"] = swing.evaluate(snap)
    return snap
if desk.lim.mode == "live" and MODE != "kotak":
    raise SystemExit("TRADING_MODE=live requires FEED_MODE=kotak (orders must be priced off the live feed)")


async def run_feed():
    backoff = 5
    while True:
        feed = KotakFeed(universe) if MODE == "kotak" else mock_feed
        try:
            state.status = "connecting"
            async for tick in feed.stream():
                state.status = "live"
                backoff = 5
                state.on_tick(tick)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # reconnect (also re-logs in, which handles daily token expiry)
            state.status = f"error: {e}"[:120]
            print("[feed]", repr(e))
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 120)


async def broadcast():
    while True:
        await asyncio.sleep(PUSH_INTERVAL)
        desk.mark()
        snap = full_snapshot()        # always evaluate, so the signal journal keeps scoring
        if not clients:
            continue
        msg = json.dumps(snap)
        for ws in list(clients):
            try:
                await ws.send_text(msg)
            except Exception:
                clients.discard(ws)


@asynccontextmanager
async def lifespan(_app):
    tasks = [asyncio.create_task(run_feed()), asyncio.create_task(broadcast())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


@app.get("/")
async def index():
    return FileResponse(BASE / "static/index.html")


@app.get("/api/snapshot")
async def snapshot():
    return JSONResponse(full_snapshot())


# ---------------------------------------------------------------- orders -------
def _auth(token: str | None):
    """Order endpoints need ORDER_API_TOKEN when it is set (always set it for live mode)."""
    want = os.getenv("ORDER_API_TOKEN")
    if want and token != want:
        raise HTTPException(401, "bad or missing X-Order-Token")


@app.post("/api/orders/estimate")
async def order_estimate(req: dict):
    return desk.estimate(req)   # read-only: no ticket, no auth needed


@app.post("/api/orders/preview")
async def order_preview(req: dict, x_order_token: str | None = Header(None)):
    _auth(x_order_token)
    return desk.preview(req)


@app.post("/api/orders/confirm")
async def order_confirm(req: dict, x_order_token: str | None = Header(None)):
    _auth(x_order_token)
    # place_order is a blocking HTTP call -> keep it off the event loop
    return await asyncio.to_thread(desk.confirm, str(req.get("ticket", "")))


@app.post("/api/orders/cancel")
async def order_cancel(req: dict, x_order_token: str | None = Header(None)):
    _auth(x_order_token)
    return desk.cancel(str(req.get("id", "")))


@app.post("/api/positions/stop")
async def move_stop(req: dict, x_order_token: str | None = Header(None)):
    """Raise / trail a position's stop (paper: enforced by the simulated GTT; live: update your GTT in Neo)."""
    _auth(x_order_token)
    return desk.update_stop(str(req.get("symbol", "")).upper(), str(req.get("product", "CNC")).upper(),
                            req.get("stop", 0))


@app.post("/api/kill")
async def kill(req: dict, x_order_token: str | None = Header(None)):
    _auth(x_order_token)
    desk.killed = bool(req.get("on", True))
    return {"killed": desk.killed}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    try:
        await ws.send_text(json.dumps(full_snapshot()))
        while True:
            await ws.receive_text()  # keepalive; client doesn't send anything meaningful
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)
