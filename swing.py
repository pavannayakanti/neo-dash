"""Swing trading (hold days to ~3 weeks) on daily candles -- built for a 10-15 minute daily routine.

Rule-based setups, not advice. Long only, delivery (CNC).
  BREAKOUT  close above the prior 20-day high, above the 50-day average, on >=1.5x average volume.
  PULLBACK  uptrend (20-day avg above 50-day, 20-day rising), price dips to the 20-day average
            (RSI 40-60) and closes up on the day.
Entry: limit near today's close for the NEXT session (or before today's close).
Stop:  2x ATR below entry (breakout) / under the 5-day swing low (pullback), at least 1 ATR away.
Target: TARGET_R x risk.  Time stop: exit if the target isn't reached in SWING_TIME_STOP_DAYS.
Holding actions (evaluated every update, final after the close):
  STOP HIT, TARGET HIT, RAISE STOP to cost at +1R, TRAIL under the 20-day avg at +2R,
  TREND BROKEN (close below the 50-day avg), TIME STOP.
"""
import math
import os
from datetime import date

import clock
from daily import indicators

TICK = 0.05


def _r(x):
    return round(round(x / TICK) * TICK, 2)


def _f(k, d):
    return float(os.getenv(k, d))


class SwingCfg:
    def __init__(self):
        self.risk = _f("SWING_RISK_PER_TRADE", 1200)
        self.max_value = _f("SWING_MAX_ORDER_VALUE", 50000)
        self.max_positions = int(_f("SWING_MAX_POSITIONS", 5))
        self.target_r = _f("SWING_TARGET_R", 2)
        self.time_stop_days = int(_f("SWING_TIME_STOP_DAYS", 15))
        self.atr_mult = _f("SWING_ATR_STOP", 2)


def trading_days_between(d0: str, d1: str) -> int:
    a, b = date.fromisoformat(d0), date.fromisoformat(d1)
    n, d = 0, a
    while d < b:
        d = date.fromordinal(d.toordinal() + 1)
        if d.weekday() < 5:
            n += 1
    return n


class SwingEngine:
    def __init__(self, desk, state, history: dict[str, list[dict]]):
        self.cfg = SwingCfg()
        self.desk, self.state, self.history = desk, state, history

    def _bars(self, sym: str) -> list[dict]:
        """History plus today's (provisional) candle from the live feed."""
        hist = self.history.get(sym, [])
        s = self.state.stocks.get(sym)
        today = clock.now().strftime("%Y-%m-%d")
        if s and s.ltp and s.day_high and (not hist or hist[-1]["date"] < today) and clock.now().weekday() < 5 \
                and clock.hm() >= 9 * 60 + 15:
            return hist + [{"date": today, "open": s.day_open or s.ltp, "high": s.day_high, "low": s.day_low,
                            "close": s.ltp, "volume": s.volume or 0}]
        return hist

    def evaluate(self, snap: dict) -> dict:
        now = clock.now()
        final = clock.hm() >= 15 * 60 + 30 or now.weekday() >= 5 or clock.hm() < 9 * 60 + 15
        frac = 1.0
        if not final:
            frac = max(0.05, (clock.hm() - (9 * 60 + 15)) / 375)
        inds = {}
        for d in snap["stocks"]:
            ind = indicators(self._bars(d["symbol"]))
            if ind:
                inds[d["symbol"]] = (d, ind)
        above50 = [1 for _, i in inds.values() if i["sma50"] and i["close"] > i["sma50"]]
        breadth50 = round(100 * len(above50) / max(1, len(inds)))
        regime = "risk-on" if breadth50 >= 55 else ("risk-off" if breadth50 < 40 else "neutral")

        positions = [p for p in snap.get("trading", {}).get("positions", []) if p["qty"] and p["product"] == "CNC"]
        held = {p["symbol"] for p in positions}
        slots = max(0, self.cfg.max_positions - len(positions))

        setups = []
        for sym, (d, i) in inds.items():
            if sym in held:
                continue
            sig = self._breakout(i, frac) or self._pullback(i)
            if not sig:
                continue
            entry = _r(i["close"] * 1.003)
            stop = _r(min(sig["stop"], entry - i["atr"]))
            risk = entry - stop
            if risk <= 0 or risk > 3.5 * i["atr"]:
                continue
            target = _r(entry + self.cfg.target_r * risk)
            qty = max(0, min(math.floor(self.cfg.risk / risk), math.floor(self.cfg.max_value / entry),
                             int(self.desk.lim.max_qty_cash_swing)))
            score, notes = 50, []
            if regime == "risk-on":
                score += 15; notes.append("most stocks above their 50-day average")
            elif regime == "risk-off":
                score -= 20; notes.append("weak market — most stocks below their 50-day average")
            if i["sma200"] and i["close"] > i["sma200"]:
                score += 10; notes.append("above 200-day average")
            if sig["setup"] == "BREAKOUT" and i["volume"] / max(1, i["vol_avg20_prev"] * frac) >= 2:
                score += 10; notes.append("very strong volume")
            score = max(5, min(95, score))
            setups.append({"symbol": sym, "sector": d["sector"], "setup": sig["setup"], "ltp": d["ltp"], "pct": d["pct"],
                           "entry": entry, "stop": stop, "target": target, "risk_per_share": round(risk, 2),
                           "qty": qty, "risk_rupees": round(qty * risk, 2), "value": round(qty * entry, 2),
                           "atr": round(i["atr"], 2), "rsi": round(i["rsi"]), "time_stop_days": self.cfg.time_stop_days,
                           "score": score, "conf_label": "High" if score >= 70 else ("Medium" if score >= 50 else "Low"),
                           "reasons": sig["reasons"] + notes})
        setups.sort(key=lambda x: -x["score"])

        holdings = [self._manage(p, inds.get(p["symbol"])) for p in positions]
        month = self.desk.month_status()
        return {"final": final, "regime": regime, "breadth50": breadth50, "slots": slots,
                "setups": setups[:8], "holdings": holdings, "month": month,
                "cfg": {"risk": self.cfg.risk, "max_positions": self.cfg.max_positions, "target_r": self.cfg.target_r,
                        "time_stop_days": self.cfg.time_stop_days},
                "has_history": bool(self.history)}

    # ------------------------------------------------------------------ setups
    def _breakout(self, i, frac):
        if not i["sma50"] or i["close"] <= i["hh20_prev"] or i["close"] <= i["sma50"]:
            return None
        vr = i["volume"] / max(1, i["vol_avg20_prev"] * frac)
        if vr < 1.5:
            return None
        return {"setup": "BREAKOUT", "stop": i["close"] - self.cfg.atr_mult * i["atr"],
                "reasons": [f"Closed above 20-day high ₹{i['hh20_prev']:,.2f}", f"Above 50-day avg ₹{i['sma50']:,.2f}",
                            f"Volume {vr:.1f}x 20-day avg"]}

    def _pullback(self, i):
        if not (i["sma20"] and i["sma50"] and i["sma20_5ago"]):
            return None
        if not (i["sma20"] > i["sma50"] and i["sma20"] > i["sma20_5ago"]):
            return None
        if not (i["low3"] <= i["sma20"] * 1.005 and i["close"] > i["sma20"] and i["close"] > i["prev_close"]):
            return None
        if not (40 <= i["rsi"] <= 60):
            return None
        return {"setup": "PULLBACK", "stop": i["ll5"] - 0.25 * i["atr"],
                "reasons": ["Uptrend: 20-day avg above 50-day and rising", f"Dipped to 20-day avg ₹{i['sma20']:,.2f} and closed up",
                            f"RSI {i['rsi']:.0f} (not overbought)"]}

    # ------------------------------------------------------------------ holdings
    def _manage(self, p: dict, di) -> dict:
        plan = p.get("plan") or {}
        ltp = p["ltp"]
        out = {"symbol": p["symbol"], "qty": p["qty"], "avg": p["avg"], "ltp": ltp, "plan": plan or None,
               "net_if_exit_now": p.get("net_if_exit_now"), "opened": p.get("opened"), "actions": []}
        days = trading_days_between(p["opened"], clock.now().strftime("%Y-%m-%d")) if p.get("opened") else 0
        out["days_held"] = days
        act = out["actions"]
        if not ltp:
            return out
        if plan:
            entry, stop, target = plan["entry"], plan["stop"], plan["target"]
            risk = entry - plan.get("initial_stop", stop) if plan.get("initial_stop") else entry - stop
            r_now = (ltp - entry) / risk if risk > 0 else 0
            out["r_now"] = round(r_now, 2)
            if ltp <= stop:
                act.append(("urgent", "STOP HIT", f"Below stop ₹{stop:,.2f} — exit", None))
            elif ltp >= target:
                act.append(("good", "TARGET HIT", f"Reached ₹{target:,.2f} — book profit, or trail the stop to lock it in", None))
            elif r_now >= 2 and di:
                new = _r(max(stop, di[1]["sma20"] * 0.995))
                if new > stop:
                    act.append(("info", "TRAIL STOP", f"Up {r_now:.1f}R — trail stop to just under the 20-day avg ₹{new:,.2f}", new))
            elif r_now >= 1 and stop < entry:
                act.append(("info", "RAISE STOP", f"Up {r_now:.1f}R — raise stop to cost ₹{entry:,.2f}; this trade can no longer lose", entry))
            if days >= self.cfg.time_stop_days and ltp < target:
                act.append(("warn", "TIME STOP", f"Held {days} trading days without reaching target — free the capital", None))
        if di and di[1]["sma50"] and di[1]["close"] < di[1]["sma50"]:
            act.append(("warn", "TREND BROKEN", f"Closed below the 50-day avg ₹{di[1]['sma50']:,.2f}", None))
        if not act:
            act.append(("ok", "HOLD", "Plan intact — nothing to do", None))
        out["actions"] = [{"level": a, "tag": t, "msg": m, "new_stop": n} for a, t, m, n in act]
        return out
