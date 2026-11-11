"""Order desk with guard rails: preview -> confirm, limit orders only, hard caps, kill switch,
paper mode by default, append-only audit log.

Flow:  POST /api/orders/preview  -> server validates, returns a ticket (valid TICKET_TTL s)
       POST /api/orders/confirm  -> server RE-validates against the latest price, then sends
The browser can't skip the preview: confirm only accepts a ticket id the server issued.
"""
import json
import os
from datetime import timedelta
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

import clock
from analytics import MarketState
from charges import ChargeModel
from universe import Instrument

TICK = 0.05          # every NSE tick size is a divisor of 0.05, so this is always valid
TICKET_TTL = 30      # seconds a preview stays confirmable


def _env(k, d):
    return type(d)(os.getenv(k, d))


class Limits:
    def __init__(self):
        self.mode = os.getenv("TRADING_MODE", "paper").lower()            # paper | live
        self.max_qty_cash = _env("MAX_QTY_CASH", 10)                       # shares per order
        self.max_value_cash = _env("MAX_ORDER_VALUE_CASH", 20000.0)        # ₹ per order
        self.max_qty_cash_swing = _env("SWING_MAX_QTY", 500)               # swing (delivery) orders
        self.max_value_swing = _env("SWING_MAX_ORDER_VALUE", 50000.0)
        self.monthly_loss_limit = _env("MONTHLY_LOSS_LIMIT", 8000.0)       # ₹ net; blocks new buys
        self.allow_fo = os.getenv("ALLOW_FO", "0") == "1"                  # futures off by default
        self.max_lots_fo = _env("MAX_LOTS_FO", 1)
        self.max_value_fo = _env("MAX_ORDER_VALUE_FO", 1500000.0)          # ₹ notional per order
        self.max_orders_day = _env("MAX_ORDERS_PER_DAY", 20)
        self.price_band_pct = _env("PRICE_BAND_PCT", 1.0)                  # limit must be within ±x% of LTP
        self.min_gap_sec = _env("MIN_SECONDS_BETWEEN_ORDERS", 2.0)
        self.squareoff_time = os.getenv("SQUAREOFF_TIME", "15:15")            # set to YOUR broker's MIS cut-off

    def public(self):
        return {k: v for k, v in vars(self).items()}


class OrderDesk:
    def __init__(self, state: MarketState, universe: list[Instrument], base: Path,
                 client_provider: Optional[Callable] = None):
        self.state = state
        self.by_sym = {i.symbol: i for i in universe}
        self.lim = Limits()
        self.costs = ChargeModel()
        self.client_provider = client_provider
        self.tickets: dict[str, dict] = {}
        self.killed = False
        self.last_sent = 0.0
        self.log_path = base / "data" / f"orders_{self.lim.mode}.jsonl"
        self.orders: list[dict] = self._load_orders()
        self._month_realised = 0.0

    # ---------------------------------------------------------------- helpers
    def _load_orders(self) -> list[dict]:
        """All orders ever logged (latest record per id). Delivery holdings span days."""
        if not self.log_path.exists():
            return []
        latest = {}
        for line in self.log_path.read_text().splitlines():
            try:
                o = json.loads(line)
            except ValueError:
                continue
            latest[o["id"]] = o
        return sorted(latest.values(), key=lambda o: (o.get("date", ""), o.get("time", "")))

    def _log(self, o: dict):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "a") as fh:
            fh.write(json.dumps(o) + "\n")

    def _ltp(self, sym: str, seg: str) -> Optional[float]:
        s = self.state.stocks.get(sym)
        if not s:
            return None
        return s.fut_ltp if seg == "fut" else s.ltp

    @staticmethod
    def _today() -> str:
        return clock.now().strftime("%Y-%m-%d")

    def _todays(self) -> list[dict]:
        t = self._today()
        return [o for o in self.orders if o.get("date") == t]

    def orders_today(self) -> int:
        return sum(1 for o in self._todays() if not o.get("auto"))

    def _squareoff_window(self) -> bool:
        """True between the intraday square-off time and market close on weekdays."""
        now = clock.now()
        h, m = map(int, self.lim.squareoff_time.split(":"))
        mins = now.hour * 60 + now.minute
        return now.weekday() < 5 and h * 60 + m <= mins < 15 * 60 + 30

    def minutes_to_squareoff(self) -> Optional[int]:
        now = clock.now()
        if now.weekday() >= 5:
            return None
        h, m = map(int, self.lim.squareoff_time.split(":"))
        left = (h * 60 + m) - (now.hour * 60 + now.minute)
        return left if 0 < left <= 375 else None

    # ---------------------------------------------------------------- validation
    def _build(self, req: dict) -> dict:
        """Validate a raw request and return a normalised order + errors/warnings."""
        errors, warns = [], []
        sym = str(req.get("symbol", "")).upper()
        seg = req.get("segment", "cash")
        side = req.get("side")
        product = str(req.get("product", "")).upper()
        ins = self.by_sym.get(sym)

        if self.killed:
            errors.append("Kill switch is ON -- trading halted")
        if not ins:
            errors.append(f"Unknown symbol {sym}")
        if seg not in ("cash", "fut"):
            errors.append("segment must be cash or fut")
        if side not in ("B", "S"):
            errors.append("side must be B or S")
        try:
            qty_in = int(req.get("qty", 0))
        except (TypeError, ValueError):
            qty_in = 0
        if qty_in < 1:
            errors.append("Quantity must be at least 1")
        try:
            price = round(round(float(req.get("price", 0)) / TICK) * TICK, 2)
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            errors.append("Limit price required (market orders are not allowed for API trading)")

        swing = req.get("strategy") == "swing"
        if swing and product != "CNC":
            errors.append("Swing trades must be Delivery (CNC)")
        lot, tsym, units, exch = 1, "", qty_in, "nse_cm"
        if ins and seg == "cash":
            if product not in ("CNC", "MIS"):
                errors.append("Stock product must be CNC (delivery) or MIS (intraday)")
            qcap = self.lim.max_qty_cash_swing if swing else self.lim.max_qty_cash
            if qty_in > qcap:
                errors.append(f"Qty {qty_in} > cap {qcap} shares{' (swing)' if swing else ''}")
            tsym = f"{sym}-EQ"
        elif ins and seg == "fut":
            exch = "nse_fo"
            if not self.lim.allow_fo:
                errors.append("Futures trading is disabled (set ALLOW_FO=1)")
            if product not in ("NRML", "MIS"):
                errors.append("Futures product must be NRML (carry) or MIS (intraday)")
            if not ins.fut_symbol or not ins.lot_size:
                errors.append("No futures symbol/lot size -- run tools/fill_tokens.py")
            lot = int(ins.lot_size or 1)
            tsym = ins.fut_symbol
            units = qty_in * lot
            if qty_in > self.lim.max_lots_fo:
                errors.append(f"{qty_in} lots > cap {self.lim.max_lots_fo}")

        value = round(price * units, 2)
        cap = self.lim.max_value_fo if seg == "fut" else (self.lim.max_value_swing if swing else self.lim.max_value_cash)
        if price > 0 and value > cap:
            errors.append(f"Order value ₹{value:,.0f} > cap ₹{cap:,.0f}")

        ltp = self._ltp(sym, seg) if ins else None
        if ltp is None:
            errors.append("No live price yet for this instrument")
        elif price > 0:
            dev = (price - ltp) / ltp * 100
            if abs(dev) > self.lim.price_band_pct:
                errors.append(f"Limit {price} is {dev:+.2f}% from LTP {ltp:.2f} (band ±{self.lim.price_band_pct}%)")

        if seg == "cash" and product == "MIS" and self._squareoff_window():
            errors.append(f"Intraday (MIS) is closed after {self.lim.squareoff_time} -- use Delivery (CNC)")
        if side == "B" and self._opens_position(sym, seg, product, qty_in):
            ms = self.month_status()
            if ms["blocked"]:
                errors.append(f"Monthly loss limit reached (₹{-ms['pnl']:,.0f} of ₹{ms['limit']:,.0f}) — "
                              "no new buys until next month; exits are still allowed")
        if self.orders_today() >= self.lim.max_orders_day:
            errors.append(f"Daily order cap reached ({self.lim.max_orders_day})")

        now = clock.now()
        mins = now.hour * 60 + now.minute
        if not (9 * 60 + 15 <= mins < 15 * 60 + 30) or now.weekday() >= 5:
            warns.append("Market closed — paper orders queue for the next session; "
                         "a live order would be rejected (place it at 09:15, or as AMO if Kotak's API allows)")
        if self.lim.mode == "live" and not os.getenv("ORDER_API_TOKEN"):
            errors.append("Live mode requires ORDER_API_TOKEN to be set")

        econ = self._economics(sym, seg, side, product, price, units, req) if (ins and price > 0 and units > 0 and side in ("B", "S")) else None

        return {
            "economics": econ,
            "order": {"symbol": sym, "segment": seg, "exchange_segment": exch, "trading_symbol": tsym,
                      "side": side, "product": product, "order_type": "L", "validity": "DAY",
                      "lots" if seg == "fut" else "shares": qty_in, "lot_size": lot,
                      "quantity": units, "price": price, "value": value, "ltp": ltp},
            "errors": errors, "warnings": warns,
        }

    # ---------------------------------------------------------------- P&L preview
    def _position(self, sym, seg, product) -> Optional[dict]:
        for p in self.positions():
            if p["symbol"] == sym and p["segment"] == seg and p["product"] == product and p["qty"]:
                return p
        return None

    def _economics(self, sym, seg, side, product, price, units, req) -> dict:
        """What this order does to your P&L, after all charges.
        Closing (part of) a position -> realised net P&L of the closed quantity.
        Opening -> charges for the round trip and the breakeven exit price."""
        pos = self._position(sym, seg, product) if self.lim.mode == "paper" else None
        manual_avg = req.get("avg_price")
        if pos is None and manual_avg:            # live mode: user supplies their entry price
            try:
                a = float(manual_avg)
                if a > 0:
                    pos = {"qty": units if side == "S" else -units, "avg": a, "source": "manual"}
            except (TypeError, ValueError):
                pass
        out: dict = {}
        if pos and (pos["qty"] > 0) == (side == "S"):          # order is opposite to the position
            closing = min(units, abs(pos["qty"]))
            entry_side = "B" if pos["qty"] > 0 else "S"
            same_day = product == "CNC" and (pos.get("opened") == self._today() or req.get("bought_today"))
            kind = "intraday" if same_day else None
            rt = self.costs.round_trip(seg, product, entry_side, pos["avg"], price, closing, kind)
            out["close"] = {"qty": closing, "avg": pos["avg"], "exit": price, **rt,
                            "per_share": round(rt["net"] / closing, 2),
                            "kind": kind or self.costs.kind(seg, product),
                            "same_day_delivery": bool(same_day),
                            "source": pos.get("source", "paper position")}
            units -= closing
        if units > 0:                                          # (remaining) qty opens a new position
            be = self.costs.breakeven(seg, product, side, price, units)
            one = self.costs.round_trip(seg, product, side, price, be, units)
            out["open"] = {"qty": units, "entry": price, "breakeven": be,
                           "move_needed": round(abs(be - price), 2),
                           "round_trip_charges": one["charges"]["total"],
                           "kind": self.costs.kind(seg, product)}
            if product == "CNC":   # the same buy, if you end up selling it today instead
                be2 = self.costs.breakeven(seg, product, side, price, units, "intraday")
                out["open"]["breakeven_same_day"] = be2
        return out

    # ---------------------------------------------------------------- API
    def estimate(self, req: dict) -> dict:
        """Same checks + P&L economics, but issues no ticket (safe to call on every keystroke)."""
        return self._build(req)

    def preview(self, req: dict) -> dict:
        b = self._build(req)
        ticket = None
        if not b["errors"]:
            ticket = uuid.uuid4().hex[:12]
            self.tickets[ticket] = {"req": req, "exp": time.time() + TICKET_TTL}
        # purge expired tickets
        for k in [k for k, v in self.tickets.items() if v["exp"] < time.time()]:
            self.tickets.pop(k, None)
        return {"ok": not b["errors"], "ticket": ticket, "ttl": TICKET_TTL, "mode": self.lim.mode, **b}

    def confirm(self, ticket: str) -> dict:
        t = self.tickets.pop(ticket, None)
        if not t or t["exp"] < time.time():
            return {"ok": False, "errors": ["Ticket missing or expired -- preview again"]}
        b = self._build(t["req"])  # re-check against the latest price & counters
        if time.time() - self.last_sent < self.lim.min_gap_sec:
            b["errors"].append("Too fast -- wait a moment between orders")
        if b["errors"]:
            return {"ok": False, **b}

        plan = self._clean_plan(t["req"].get("plan"))
        o = {**b["order"], "id": uuid.uuid4().hex[:10], "mode": self.lim.mode, "plan": plan,
             "strategy": "swing" if t["req"].get("strategy") == "swing" else "intraday",
             "date": self._session_date(), "time": clock.now().strftime("%H:%M:%S")}
        self.last_sent = time.time()
        if self.lim.mode == "live":
            o.update(self._send_live(o))
        else:
            o.update(self._paper(o))
        self.orders.append(o)
        self._log(o)
        return {"ok": o["status"] not in ("REJECTED", "ERROR"), "order": o}

    def _session_date(self) -> str:
        """Trading date an order belongs to: today during/before the session, else the next weekday."""
        now = clock.now()
        d = now
        if now.weekday() >= 5 or clock.hm(now) >= 15 * 60 + 30:
            d = now + timedelta(days=1)
            while d.weekday() >= 5:
                d += timedelta(days=1)
        return d.strftime("%Y-%m-%d")

    def _opens_position(self, sym, seg, product, qty) -> bool:
        p = self._position(sym, seg, product) if self.lim.mode == "paper" else None
        return not (p and p["qty"] < 0)          # a buy that covers a short is an exit, not a new risk

    @staticmethod
    def _clean_plan(plan) -> Optional[dict]:
        """Keep only the numeric trade plan (entry/stop/target) a signal attached to the order."""
        if not isinstance(plan, dict):
            return None
        try:
            out = {k: round(float(plan[k]), 2) for k in ("entry", "stop", "target")}
        except (KeyError, TypeError, ValueError):
            return None
        out["setup"] = str(plan.get("setup", ""))[:12]
        out["kind"] = "swing" if plan.get("kind") == "swing" else "intraday"
        out["initial_stop"] = out["stop"]
        return out

    # ---------------------------------------------------------------- plan updates (trail / raise stop)
    def _overrides_path(self) -> Path:
        return self.log_path.parent / f"plan_overrides_{self.lim.mode}.json"

    def _overrides(self) -> dict:
        try:
            return json.loads(self._overrides_path().read_text())
        except (OSError, ValueError):
            return {}

    def update_stop(self, symbol: str, product: str, new_stop: float) -> dict:
        """Move a position's stop. Only upward for longs (a stop is never loosened)."""
        pos = next((p for p in self.positions() if p["symbol"] == symbol and p["product"] == product and p["qty"] > 0), None)
        if not pos or not pos.get("plan"):
            return {"ok": False, "errors": ["No open long position with a plan"]}
        new_stop = round(float(new_stop), 2)
        if new_stop <= pos["plan"]["stop"]:
            return {"ok": False, "errors": ["New stop must be above the current stop — stops only move up"]}
        if pos["ltp"] and new_stop >= pos["ltp"]:
            return {"ok": False, "errors": ["New stop must be below the current price"]}
        ov = self._overrides()
        ov[f"{symbol}|{product}"] = {"stop": new_stop, "since": pos["opened"]}
        self._overrides_path().write_text(json.dumps(ov))
        return {"ok": True, "stop": new_stop}

    def _send_live(self, o: dict) -> dict:
        try:
            client = self.client_provider()
            resp = client.place_order(
                exchange_segment=o["exchange_segment"], product=o["product"],
                price=f"{o['price']:.2f}", order_type="L", quantity=str(o["quantity"]),
                validity="DAY", trading_symbol=o["trading_symbol"], transaction_type=o["side"],
            )
        except Exception as e:  # never auto-retry an order: risk of duplicates
            return {"status": "ERROR", "broker": repr(e)[:300]}
        bad = isinstance(resp, dict) and (
            str(resp.get("stat", "")).lower() == "not_ok" or "error" in {k.lower() for k in resp})
        return {"status": "REJECTED" if bad else "SENT", "broker": resp}

    # ---------------------------------------------------------------- paper
    def _paper(self, o: dict) -> dict:
        if not clock.market_open() or o["date"] != self._today():
            return {"status": "PAPER_OPEN"}               # queued for the next session
        ltp = o["ltp"]
        marketable = (o["side"] == "B" and o["price"] >= ltp) or (o["side"] == "S" and o["price"] <= ltp)
        if marketable:
            return {"status": "PAPER_FILLED", "fill_price": ltp}   # marketable limit fills at LTP
        return {"status": "PAPER_OPEN"}

    def mark(self):
        """Called every push cycle: fill resting paper orders when price crosses the limit,
        and auto square-off intraday (MIS) paper positions at the cut-off, like the broker does."""
        if self.lim.mode == "paper" and self._squareoff_window():
            for o in self.orders:
                if o["status"] == "PAPER_OPEN" and o["product"] == "MIS":
                    o["status"] = "PAPER_CANCELLED"
                    self._log(o)
            for p in self.positions():
                if p["product"] == "MIS" and p["qty"] and p["ltp"]:
                    now = clock.now()
                    sq = {"id": uuid.uuid4().hex[:10], "symbol": p["symbol"], "segment": p["segment"],
                          "product": "MIS", "side": "S" if p["qty"] > 0 else "B", "quantity": abs(p["qty"]),
                          "price": p["ltp"], "fill_price": p["ltp"], "status": "PAPER_FILLED", "auto": True,
                          "auto_reason": "SQUARE-OFF",
                          "trading_symbol": f"{p['symbol']}-EQ", "mode": "paper",
                          "date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M:%S")}
                    self.orders.append(sq)
                    self._log(sq)
        today = self._today()
        open_now = clock.market_open()
        if self.lim.mode == "paper" and open_now:
            # simulated GTT (OCO): swing positions exit automatically at their stop or target
            for p in self.positions():
                pl = p.get("plan")
                if p["product"] != "CNC" or p["qty"] <= 0 or not pl or pl.get("kind") != "swing" or not p["ltp"]:
                    continue
                hit = "GTT-STOP" if p["ltp"] <= pl["stop"] else ("GTT-TARGET" if p["ltp"] >= pl["target"] else None)
                if hit:
                    now = clock.now()
                    x = {"id": uuid.uuid4().hex[:10], "symbol": p["symbol"], "segment": "cash", "product": "CNC",
                         "side": "S", "quantity": p["qty"], "price": p["ltp"], "fill_price": p["ltp"],
                         "status": "PAPER_FILLED", "auto": True, "auto_reason": hit, "strategy": "swing",
                         "trading_symbol": f"{p['symbol']}-EQ", "mode": "paper",
                         "date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M:%S")}
                    self.orders.append(x)
                    self._log(x)
        for o in self.orders:
            if o["status"] != "PAPER_OPEN":
                continue
            if o.get("date", today) < today:    # DAY validity: unfilled orders expire at close
                o["status"] = "PAPER_EXPIRED"
                self._log(o)
                continue
            if not open_now or o.get("date") != today:
                continue                         # queued for a later session
            ltp = self._ltp(o["symbol"], o["segment"])
            if ltp is None:
                continue
            if (o["side"] == "B" and ltp <= o["price"]) or (o["side"] == "S" and ltp >= o["price"]):
                o["status"], o["fill_price"] = "PAPER_FILLED", o["price"]
                self._log(o)

    def cancel(self, oid: str) -> dict:
        for o in self.orders:
            if o["id"] == oid and o["status"] == "PAPER_OPEN":
                o["status"] = "PAPER_CANCELLED"
                self._log(o)
                return {"ok": True}
        return {"ok": False, "errors": ["Only open paper orders can be cancelled here; cancel live orders in the Neo app"]}

    def _replay(self) -> tuple[dict, float]:
        """Replay every paper fill. Returns (positions keyed by (symbol, seg, product, MIS-date),
        realised net P&L for the current month across ALL days and products)."""
        today = self._today()
        month = today[:7]
        ov = self._overrides()
        pos: dict[tuple, dict] = {}
        month_net = 0.0
        for o in sorted(self.orders, key=lambda o: (o.get("date", ""), o.get("time", ""))):
            if o["status"] != "PAPER_FILLED":
                continue
            k = (o["symbol"], o["segment"], o["product"], o.get("date") if o["product"] == "MIS" else None)
            p = pos.setdefault(k, {"symbol": k[0], "segment": k[1], "product": k[2], "day": k[3], "qty": 0,
                                   "cost": 0.0, "open_fees": 0.0, "realised": 0.0, "realised_net": 0.0,
                                   "opened": None, "touched_today": False, "plan": None})
            fill_today = o.get("date") == today
            p["touched_today"] |= fill_today
            q = o["quantity"] if o["side"] == "B" else -o["quantity"]
            px = o["fill_price"]
            if p["qty"] == 0:
                p["opened"], p["plan"] = o.get("date"), None
            kind = None
            if o["product"] == "CNC":
                kind = "intraday" if (o.get("date") == p["opened"]) else "delivery"
            sq_fee = o.get("auto") and o.get("auto_reason", "SQUARE-OFF") == "SQUARE-OFF"
            extra = self.costs.auto_squareoff_fee * (1 + self.costs.gst) if sq_fee else 0.0

            if p["qty"] == 0 or (p["qty"] > 0) == (q > 0):          # opening / adding
                if o.get("plan"):
                    p["plan"] = dict(o["plan"])
                p["open_fees"] += self.costs.leg(o["segment"], o["product"], o["side"], px * abs(q), kind)["total"]
                p["cost"] += q * px
                p["qty"] += q
                continue
            avg = p["cost"] / p["qty"]                               # reducing / flipping
            direction = 1 if p["qty"] > 0 else -1
            closed = min(abs(q), abs(p["qty"]))
            entry_part = p["open_fees"] * closed / abs(p["qty"])
            exit_fee = self.costs.leg(o["segment"], o["product"], o["side"], px * closed, kind)["total"] + extra
            gross = closed * (px - avg) * direction
            net = gross - entry_part - exit_fee
            if fill_today:
                p["realised"] += gross
                p["realised_net"] += net
            if o.get("date", "")[:7] == month:
                month_net += net
            p["open_fees"] -= entry_part
            p["qty"] -= closed * direction
            p["cost"] = avg * p["qty"]
            rest = q + closed * direction
            if rest:
                p["opened"], p["plan"] = o.get("date"), None
                p["open_fees"] += self.costs.leg(o["segment"], o["product"], o["side"], px * abs(rest), kind)["total"]
                p["qty"] += rest
                p["cost"] += rest * px
        # apply stop overrides (raise / trail) to open positions
        for p in pos.values():
            o2 = ov.get(f"{p['symbol']}|{p['product']}")
            if p["qty"] and p["plan"] and o2 and o2.get("since") == p["opened"]:
                p["plan"]["stop"] = max(p["plan"]["stop"], o2["stop"])
        return pos, month_net

    def positions(self) -> list[dict]:
        """Paper positions with net P&L.
        * MIS (intraday) positions only cover today's fills (older ones were squared off).
        * CNC (delivery) holdings carry across days; a CNC buy+sell on the same day is
          charged as intraday (no delivery takes place).
        * net_if_exit_now = today's realised net + unrealised - entry charges of open qty
          - charges to exit the open qty at LTP right now."""
        today = self._today()
        pos, self._month_realised = self._replay()
        out = []
        for p in pos.values():
            if p["product"] == "MIS" and p["day"] != today:
                continue
            if not p["qty"] and not p["touched_today"]:
                continue                                               # closed on an earlier day
            ltp = self._ltp(p["symbol"], p["segment"])
            avg = p["cost"] / p["qty"] if p["qty"] else 0
            unreal = (ltp - avg) * p["qty"] if (ltp and p["qty"]) else 0
            exit_kind = None
            if p["product"] == "CNC":
                exit_kind = "intraday" if p["opened"] == today else "delivery"
            exit_fees = self.costs.leg(p["segment"], p["product"], "S" if p["qty"] > 0 else "B",
                                       ltp * abs(p["qty"]), exit_kind)["total"] if (ltp and p["qty"]) else 0.0
            net_now = p["realised_net"] + unreal - p["open_fees"] - exit_fees
            out.append({"symbol": p["symbol"], "segment": p["segment"], "product": p["product"],
                        "qty": p["qty"], "avg": round(avg, 2), "ltp": ltp, "opened": p["opened"],
                        "held_overnight": bool(p["qty"]) and p["opened"] != today, "plan": p["plan"] if p["qty"] else None,
                        "unrealised": round(unreal, 2), "realised": round(p["realised"], 2),
                        "open_net": round(unreal - p["open_fees"] - exit_fees, 2) if p["qty"] else 0.0,
                        "fees": round(p["open_fees"] + exit_fees, 2), "net_if_exit_now": round(net_now, 2)})
        return out

    def month_status(self) -> dict:
        """Month-to-date net P&L (realised + losses on open positions) vs the monthly loss limit."""
        if self.lim.mode != "paper":
            return {"pnl": None, "limit": self.lim.monthly_loss_limit, "blocked": False,
                    "note": "Live P&L isn't tracked yet — watch it in the Neo app"}
        opens = self.positions()
        open_loss = sum(min(0.0, p["open_net"]) for p in opens)
        pnl = round(self._month_realised + open_loss, 2)
        lim = self.lim.monthly_loss_limit
        return {"pnl": pnl, "realised": round(self._month_realised, 2), "open_loss": round(open_loss, 2),
                "limit": lim, "used_pct": round(max(0.0, -pnl) / lim * 100, 1) if lim else 0,
                "blocked": bool(lim) and pnl <= -lim}

    def summary(self) -> dict:
        pos = self.positions() if self.lim.mode == "paper" else []
        return {
            "mode": self.lim.mode, "killed": self.killed, "orders_today": self.orders_today(),
            "squareoff_time": self.lim.squareoff_time, "minutes_to_squareoff": self.minutes_to_squareoff(),
            "limits": self.lim.public(),
            "orders": sorted(self._todays(), key=lambda o: o["time"], reverse=True)[:15],
            "positions": pos,
            "pnl": round(sum(p["unrealised"] + p["realised"] for p in pos), 2),
            "pnl_net": round(sum(p["net_if_exit_now"] for p in pos), 2),
            "charges": self.costs.public(),
        }
