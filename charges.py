"""Indian equity trading charges -> net P&L.

Statutory rates as of Nov 2026 (verify yearly -- the Budget changes these):
  STT          delivery 0.1% buy+sell | intraday 0.025% sell | futures 0.05% sell (from 1 Apr 2026)
  NSE txn      cash 0.00307% | futures 0.00183%  each side (from 1 Mar 2026)
  SEBI fee     Rs 10 / crore each side
  Stamp duty   buy side only: delivery 0.015% | intraday 0.003% | futures 0.002%
  GST          18% on (brokerage + exchange txn + SEBI fee)
Broker-specific (Kotak Neo -- depends on YOUR plan, set in .env):
  brokerage    delivery % | intraday min(flat, %) | futures flat per order
  DP charge    flat per scrip per day on delivery SELL (+GST)
Not included: income tax on profits, auto square-off / call-&-trade fees, interest (MTF).
"""
import os


def _f(k, d):
    return float(os.getenv(k, d))


class ChargeModel:
    def __init__(self):
        self.brk_delivery_pct = _f("BROKERAGE_DELIVERY_PCT", 0.20)   # Trade Free plan after 30 days
        self.brk_intraday_flat = _f("BROKERAGE_INTRADAY_FLAT", 10)
        self.brk_intraday_pct = _f("BROKERAGE_INTRADAY_PCT", 0.05)
        self.brk_fo_flat = _f("BROKERAGE_FO_FLAT", 10)
        self.dp_charge = _f("DP_CHARGE_PER_SELL", 20)                 # verify on a contract note
        self.stt = {"delivery": (0.1, 0.1), "intraday": (0.0, 0.025), "futures": (0.0, 0.05)}  # % (buy, sell)
        self.txn = {"delivery": 0.00307, "intraday": 0.00307, "futures": 0.00183}            # % each side
        self.stamp = {"delivery": 0.015, "intraday": 0.003, "futures": 0.002}                 # % buy only
        self.sebi_per_cr = 10.0
        self.gst = 0.18
        self.auto_squareoff_fee = _f("AUTO_SQUAREOFF_FEE", 49)       # + GST, if the broker squares you off

    @staticmethod
    def kind(segment: str, product: str) -> str:
        if segment == "fut":
            return "futures"
        return "delivery" if product == "CNC" else "intraday"

    def leg(self, segment: str, product: str, side: str, value: float, kind: str = None) -> dict:
        """Charges for ONE executed order (one leg). `kind` overrides the product mapping --
        e.g. CNC bought and sold the same day is charged as intraday (no delivery happens)."""
        k = kind or self.kind(segment, product)
        if k == "delivery":
            brk = value * self.brk_delivery_pct / 100
        elif k == "intraday":
            brk = min(self.brk_intraday_flat, value * self.brk_intraday_pct / 100)
        else:
            brk = self.brk_fo_flat
        stt = value * self.stt[k][0 if side == "B" else 1] / 100
        txn = value * self.txn[k] / 100
        sebi = value * self.sebi_per_cr / 1e7
        stamp = value * self.stamp[k] / 100 if side == "B" else 0.0
        dp = self.dp_charge if (k == "delivery" and side == "S") else 0.0
        gst = (brk + txn + sebi + dp) * self.gst
        parts = {"brokerage": brk, "stt": stt, "exchange": txn, "sebi": sebi, "stamp": stamp, "dp": dp, "gst": gst}
        parts = {p: round(v, 2) for p, v in parts.items()}
        parts["total"] = round(sum(parts.values()), 2)
        return parts

    def round_trip(self, segment, product, entry_side, entry_px, exit_px, qty, kind=None) -> dict:
        """Gross, charges and net for opening at entry_px and closing at exit_px."""
        exit_side = "S" if entry_side == "B" else "B"
        a = self.leg(segment, product, entry_side, entry_px * qty, kind)
        b = self.leg(segment, product, exit_side, exit_px * qty, kind)
        direction = 1 if entry_side == "B" else -1
        gross = (exit_px - entry_px) * qty * direction
        charges = {k: round(a[k] + b[k], 2) for k in a}
        return {"gross": round(gross, 2), "charges": charges, "net": round(gross - charges["total"], 2)}

    def breakeven(self, segment, product, entry_side, entry_px, qty, kind=None) -> float:
        """Exit price at which net P&L = 0 (charges depend slightly on price, so iterate)."""
        direction = 1 if entry_side == "B" else -1
        px = entry_px
        for _ in range(6):
            c = self.round_trip(segment, product, entry_side, entry_px, px, qty, kind)["charges"]["total"]
            px = entry_px + direction * c / qty
        return round(px, 2)

    def public(self) -> dict:
        return {"brokerage_delivery_pct": self.brk_delivery_pct, "brokerage_intraday": f"min(₹{self.brk_intraday_flat:g}, {self.brk_intraday_pct}%)",
                "brokerage_fo": self.brk_fo_flat, "dp_charge": self.dp_charge}
