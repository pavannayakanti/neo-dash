"""Instrument universe: symbols, sectors, Kotak tokens and previous-day reference data."""
import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def _f(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


@dataclass
class Instrument:
    symbol: str
    sector: str
    cash_token: str = ""
    fut_token: str = ""
    fut_symbol: str = ""      # e.g. Kotak futures trading symbol (needed for orders)
    lot_size: Optional[int] = None
    # previous trading day (filled from data/prevday.csv, see tools/prep_prevday.py)
    prev_close: Optional[float] = None
    prev_high: Optional[float] = None
    prev_low: Optional[float] = None
    prev_volume: Optional[float] = None
    fut_prev_close: Optional[float] = None
    prev_oi: Optional[float] = None


def load_universe(universe_csv: Path, prevday_csv: Optional[Path] = None) -> list[Instrument]:
    out: list[Instrument] = []
    with open(universe_csv, newline="") as fh:
        for r in csv.DictReader(fh):
            if not r.get("symbol"):
                continue
            out.append(Instrument(
                symbol=r["symbol"].strip(),
                sector=(r.get("sector") or "Other").strip(),
                cash_token=(r.get("cash_token") or "").strip(),
                fut_token=(r.get("fut_token") or "").strip(),
                fut_symbol=(r.get("fut_symbol") or "").strip(),
                lot_size=int(float(r["lot_size"])) if (r.get("lot_size") or "").strip() else None,
            ))

    if prevday_csv and Path(prevday_csv).exists():
        by_sym = {i.symbol: i for i in out}
        with open(prevday_csv, newline="") as fh:
            for r in csv.DictReader(fh):
                ins = by_sym.get(r.get("symbol", ""))
                if not ins:
                    continue
                for k in ("prev_close", "prev_high", "prev_low", "prev_volume", "fut_prev_close", "prev_oi"):
                    setattr(ins, k, _f(r.get(k)))
    return out
