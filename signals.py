"""Rule-based intraday signals for cash-market stocks.

These are transparent SETUPS, not predictions or advice. Every signal says which rule fired,
where the entry / stop-loss / target are, and how many shares fit your rupee risk budget.
A journal records how each setup actually played out, so you can judge the rules in paper
mode before trusting them with money.

Setups (long only -- you buy stocks):
  ORB   Opening Range Breakout: price breaks above the first-15-minute high, above VWAP,
        on above-normal volume. Stop: VWAP or the middle of the opening range. Target: 2R.
  VWAP  VWAP Pullback: a stock up >0.5% on the day dips back to VWAP and turns up.
        Stop: just under VWAP. Target: 2R.
Exit alerts on positions you hold: stop hit, target hit, trail stop to cost at +1R,
momentum lost (below VWAP), and exit-before-square-off for intraday positions.
"""
import json
import math
import os
from pathlib import Path
from typing import Optional

import clock

TICK = 0.05


def _r(x: float) -> float:
    return round(round(x / TICK) * TICK, 2)


class Cfg:
    def __init__(self):
        self.risk_per_trade = float(os.getenv("RISK_PER_TRADE", "50"))     # ₹ you accept losing per trade
        self.target_r = float(os.getenv("TARGET_R", "2"))                   # reward : risk
        self.min_rvol = float(os.getenv("MIN_RVOL", "1.2"))
        self.no_new_after = os.getenv("NO_NEW_ENTRIES_AFTER", "14:45")
        self.time_exit = os.getenv("TIME_EXIT", "15:05")
        self.orb_minutes = int(os.getenv("ORB_MINUTES", "15"))
        self.min_risk_pct = 0.25                                            # stop at least 0.25% away


def phase(cfg: Cfg, squareoff: str) -> dict:
    """Where we are in the trading day, and what that means for new entries."""
    t = clock.hm()
    orb_end = 9 * 60 + 15 + cfg.orb_minutes
    nna, sq = clock.parse_hm(cfg.no_new_after), clock.parse_hm(squareoff)
    if not clock.market_open():
        return {"key": "CLOSED", "label": "Market closed", "hint": "Signals resume at 09:15", "entries": False}
    if t < orb_end:
        return {"key": "OPENING", "label": "Opening range", "hint": f"Wait — first {cfg.orb_minutes} min are noisy; the range is forming", "entries": False}
    if t < 11 * 60 + 30:
        return {"key": "PRIME", "label": "Prime window", "hint": "Best time for breakouts — volume is high", "entries": True}
    if t < 13 * 60 + 30:
        return {"key": "MIDDAY", "label": "Midday lull", "hint": "Lower volume — be selective, smaller size", "entries": True}
    if t < nna:
        return {"key": "AFTERNOON", "label": "Afternoon", "hint": "Trend continuation only", "entries": True}
    if t < sq:
        return {"key": "LATE", "label": "No new intraday entries", "hint": "Manage exits — square-off approaching", "entries": False}
    return {"key": "SQUAREOFF", "label": "Square-off", "hint": "Intraday positions are being closed", "entries": False}


class SignalEngine:
    def __init__(self, desk, base: Path):
        self.cfg = Cfg()
        self.desk = desk
        self.base = base
        self.journal: dict[tuple, dict] = {}      # (date, symbol, setup) -> record
        self._day = None

    # ------------------------------------------------------------------ sizing
    def _size(self, entry: float, risk: float) -> dict:
        lim = self.desk.lim
        by_risk = math.floor(self.cfg.risk_per_trade / risk) if risk > 0 else 0
        by_value = math.floor(lim.max_value_cash / entry) if entry > 0 else 0
        qty = max(0, min(by_risk, by_value, lim.max_qty_cash))
        why = "risk budget" if qty == by_risk else ("order value cap" if qty == by_value else "qty cap")
        return {"qty": qty, "limited_by": why, "risk_rupees": round(qty * risk, 2), "value": round(qty * entry, 2)}

    # ------------------------------------------------------------------ setups
    def _orb(self, d: dict) -> Optional[dict]:
        if not d.get("or_ready") or d.get("vwap") is None:
            return None
        orh, orl, ltp, vwap = d["or_high"], d["or_low"], d["ltp"], d["vwap"]
        if ltp <= orh or ltp <= vwap:
            return None
        entry = orh
        stop = max(vwap, (orh + orl) / 2)
        stop = min(stop, entry * (1 - self.cfg.min_risk_pct / 100))
        risk = entry - stop
        reasons = [f"Broke above opening-range high ₹{orh:,.2f}", f"Above VWAP ₹{vwap:,.2f}"]
        rv = d.get("vol_ratio")
        if rv is not None:
            if rv < self.cfg.min_rvol:
                return None
            reasons.append(f"Volume {rv}x usual")
        return {"setup": "ORB", "name": "Opening Range Breakout", "entry": entry, "stop": stop, "risk": risk, "reasons": reasons}

    def _vwap_pullback(self, d: dict) -> Optional[dict]:
        vwap, ltp, sp = d.get("vwap"), d["ltp"], d.get("spark") or []
        if vwap is None or len(sp) < 6 or d["pct"] < 0.5:
            return None
        if not (0 <= d["vs_vwap"] <= 0.3):                 # sitting just above VWAP
            return None
        if not d.get("day_high") or d["day_high"] < vwap * 1.006:  # had a real run-up first
            return None
        if not (sp[-1] > sp[-3] and min(sp[-6:]) <= vwap * 1.002):  # touched VWAP, now turning up
            return None
        entry = ltp
        stop = min(vwap * 0.998, entry * (1 - self.cfg.min_risk_pct / 100))
        return {"setup": "VWAP", "name": "VWAP Pullback", "entry": entry, "stop": stop, "risk": entry - stop,
                "reasons": [f"Up {d['pct']:+.2f}% today", f"Pulled back to VWAP ₹{vwap:,.2f} and turning up"]}

    # ------------------------------------------------------------------ main
    def evaluate(self, snap: dict) -> dict:
        today = clock.now().strftime("%Y-%m-%d")
        if today != self._day:
            self._day, self.journal = today, {}
        ph = phase(self.cfg, self.desk.lim.squareoff_time)
        adv, dec = snap["breadth"]["adv"], snap["breadth"]["dec"]
        market = "strong" if adv > dec * 1.5 else ("weak" if dec > adv * 1.5 else "mixed")
        sectors = {s["name"]: s["pct"] for s in snap["sectors"]}
        held = {p["symbol"] for p in snap.get("trading", {}).get("positions", []) if p["qty"]}

        buys, avoid = [], []
        for d in snap["stocks"]:
            # breakdown warning (useful if you were thinking of buying it)
            if d.get("or_ready") and d.get("vwap") and d["ltp"] < d["or_low"] and d["ltp"] < d["vwap"]:
                avoid.append({"symbol": d["symbol"], "why": f"Broke below opening-range low ₹{d['or_low']:,.2f} and VWAP"})
            if not ph["entries"] or d["symbol"] in held:
                continue
            sig = self._orb(d) or self._vwap_pullback(d)
            if not sig:
                continue
            entry, stop, risk = _r(sig["entry"]), _r(sig["stop"]), sig["entry"] - sig["stop"]
            if risk <= 0:
                continue
            if d["ltp"] >= entry + self.cfg.target_r * risk:
                continue                                           # already ran to target
            extended = d["ltp"] > entry + 0.5 * risk
            # confidence: transparent points, not a probability
            conf, notes = 50, []
            rv = d.get("vol_ratio")
            if rv is not None and rv >= 1.5:
                conf += 15; notes.append("strong volume")
            if market == "strong":
                conf += 10; notes.append("market breadth positive")
            elif market == "weak":
                conf -= 15; notes.append("market breadth weak")
            sp = sectors.get(d["sector"], 0)
            if sp > 0.3:
                conf += 10; notes.append(f"{d['sector']} sector up")
            elif sp < -0.3:
                conf -= 10; notes.append(f"{d['sector']} sector down")
            if ph["key"] == "MIDDAY":
                conf -= 10; notes.append("midday — lower follow-through")
            conf = max(5, min(95, conf))
            limit = _r(min(d["ltp"] * 1.0005, entry + 0.5 * risk)) if not extended else entry
            target = _r(limit + self.cfg.target_r * (limit - stop))   # 2R from the price you actually pay
            size = self._size(limit, limit - stop)
            rec = {"symbol": d["symbol"], "sector": d["sector"], "setup": sig["setup"], "name": sig["name"],
                   "status": "EXTENDED" if extended else "BUY", "ltp": d["ltp"], "pct": d["pct"],
                   "entry": entry, "limit": limit, "stop": stop, "target": target,
                   "risk_per_share": round(limit - stop, 2), "reward_per_share": round(target - limit, 2),
                   "confidence": conf, "conf_label": "High" if conf >= 70 else ("Medium" if conf >= 50 else "Low"),
                   "reasons": sig["reasons"] + notes, **size}
            buys.append(rec)
            self._journal_open(today, rec)

        buys.sort(key=lambda x: (x["status"] != "BUY", -x["confidence"]))
        self._journal_update(today, {d["symbol"]: d for d in snap["stocks"]})
        return {"phase": ph, "market": market, "buys": buys[:8], "avoid": avoid[:5],
                "exits": self._exits(snap), "stats": self._stats(today),
                "cfg": {"risk_per_trade": self.cfg.risk_per_trade, "target_r": self.cfg.target_r,
                        "no_new_after": self.cfg.no_new_after, "time_exit": self.cfg.time_exit}}

    # ------------------------------------------------------------------ exits
    def _exits(self, snap: dict) -> list[dict]:
        out = []
        stocks = {d["symbol"]: d for d in snap["stocks"]}
        late = clock.hm() >= clock.parse_hm(self.cfg.time_exit) and clock.market_open()
        for p in snap.get("trading", {}).get("positions", []):
            if not p["qty"] or p["segment"] != "cash":
                continue
            if (p.get("plan") or {}).get("kind") == "swing":
                continue          # swing holdings are managed on daily candles in the SWING view
            d = stocks.get(p["symbol"])
            if not d:
                continue
            ltp, plan, long = d["ltp"], p.get("plan"), p["qty"] > 0
            sgn = 1 if long else -1
            alerts = []
            if plan:
                risk = abs(plan["entry"] - plan["stop"])
                if (ltp - plan["stop"]) * sgn <= 0:
                    alerts.append(("urgent", "STOP HIT", f"Price ₹{ltp:,.2f} crossed your stop ₹{plan['stop']:,.2f} — exit to cap the loss"))
                elif (ltp - plan["target"]) * sgn >= 0:
                    alerts.append(("good", "TARGET HIT", f"Reached target ₹{plan['target']:,.2f} — book profit or trail the stop"))
                elif risk and (ltp - plan["entry"]) * sgn >= risk:
                    alerts.append(("info", "TRAIL STOP", f"Up 1R — move your stop to cost ₹{plan['entry']:,.2f} so this can't become a loss"))
            if d.get("vwap") and (ltp - d["vwap"]) * sgn < 0 and not any(a[1] == "STOP HIT" for a in alerts):
                alerts.append(("warn", "MOMENTUM LOST", f"{'Below' if long else 'Above'} VWAP ₹{d['vwap']:,.2f} — the intraday trend has turned"))
            if p["product"] == "MIS" and late:
                alerts.append(("warn", "TIME EXIT", f"Exit before the {self.desk.lim.squareoff_time} auto square-off (and its fee)"))
            for level, tag, msg in alerts:
                out.append({"symbol": p["symbol"], "level": level, "tag": tag, "msg": msg, "qty": p["qty"],
                            "product": p["product"], "net_if_exit_now": p.get("net_if_exit_now")})
        order = {"urgent": 0, "good": 1, "warn": 2, "info": 3}
        return sorted(out, key=lambda a: order[a["level"]])

    # ------------------------------------------------------------------ journal
    def _journal_open(self, today: str, rec: dict):
        k = (today, rec["symbol"], rec["setup"])
        if k in self.journal or rec["status"] != "BUY":
            return
        self.journal[k] = {"date": today, "time": clock.now().strftime("%H:%M"), "symbol": rec["symbol"],
                           "setup": rec["setup"], "entry": rec["limit"], "stop": rec["stop"],
                           "target": rec["target"], "result": "open", "r": None}

    def _journal_update(self, today: str, stocks: dict):
        time_up = clock.hm() >= clock.parse_hm(self.cfg.time_exit)
        for k, j in self.journal.items():
            if j["result"] != "open":
                continue
            d = stocks.get(j["symbol"])
            if not d:
                continue
            risk = j["entry"] - j["stop"]
            if d["ltp"] <= j["stop"]:
                j["result"], j["r"] = "stop", -1.0
            elif d["ltp"] >= j["target"]:
                j["result"], j["r"] = "target", round((j["target"] - j["entry"]) / risk, 2)
            elif time_up:
                j["result"], j["r"] = "time", round((d["ltp"] - j["entry"]) / risk, 2)
            if j["result"] != "open":
                self._persist(j)

    def _persist(self, j: dict):
        p = self.base / "data" / "signal_journal.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as fh:
            fh.write(json.dumps({**j, "mode": "sim" if clock.is_sim() else "live"}) + "\n")

    def _stats(self, today: str) -> dict:
        """Today's scorecard per setup (assumes a fill at the signal's limit price)."""
        out = {}
        for (day, _, setup), j in self.journal.items():
            if day != today:
                continue
            s = out.setdefault(setup, {"signals": 0, "target": 0, "stop": 0, "time": 0, "open": 0, "r_total": 0.0})
            s["signals"] += 1
            s[j["result"]] += 1
            s["r_total"] = round(s["r_total"] + (j["r"] or 0), 2)
        return out
