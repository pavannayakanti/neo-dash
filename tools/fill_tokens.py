"""Fill cash_token / fut_token in data/universe.csv from Kotak Neo scrip-master CSVs.

Get the scrip master for nse_cm and nse_fo via the SDK's scrip-master function
(see docs/functions in github.com/Kotak-Neo/kotak-neo-python) and save both CSVs, then:

  python tools/fill_tokens.py --cm nse_cm.csv --fo nse_fo.csv

Picks the <SYMBOL>-EQ cash scrip and the NEAREST-expiry stock future (FUTSTK),
including its trading symbol and lot size (needed for futures orders).
Re-run after every monthly expiry so fut_token points at the current contract.

Column names below are the ones Kotak has used historically (pSymbol = token,
pTrdSymbol, pSymbolName, pInstType, pExpiryDate). If your file differs, edit COLS.
"""
import argparse
import csv
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
COLS = {
    "token": ["pSymbol", "pToken", "token"],
    "trd": ["pTrdSymbol", "tradingSymbol"],
    "name": ["pSymbolName", "symbolName", "name"],
    "inst": ["pInstType", "instrumentType"],
    "expiry": ["pExpiryDate", "lExpiryDate", "expiry"],
    "lot": ["lLotSize", "iLotSize", "pLotSize", "lotSize"],
}


def col(header, key):
    hs = {h.strip(): h for h in header}
    for c in COLS[key]:
        if c in hs:
            return hs[c]
    raise SystemExit(f"Could not find a {key} column in {header[:12]}... -- edit COLS")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cm", required=True)
    ap.add_argument("--fo", required=True)
    ap.add_argument("--universe", default=BASE / "data/universe.csv")
    a = ap.parse_args()

    uni = list(csv.DictReader(open(a.universe)))
    want = {r["symbol"] for r in uni}

    cash = {}
    rd = csv.DictReader(open(a.cm))
    tk, trd = col(rd.fieldnames, "token"), col(rd.fieldnames, "trd")
    for r in rd:
        t = r[trd].strip()
        if t.endswith("-EQ") and t[:-3] in want:
            cash[t[:-3]] = r[tk].strip()

    fut = {}
    rd = csv.DictReader(open(a.fo))
    tk, nm, inst, exp, trd, lot = (col(rd.fieldnames, k) for k in ("token", "name", "inst", "expiry", "trd", "lot"))
    for r in rd:
        s = r[nm].strip()
        if s in want and r[inst].strip() == "FUTSTK":
            e = float(r[exp] or 0)   # epoch-like; any fixed offset doesn't change ordering
            if s not in fut or e < fut[s][0]:
                fut[s] = (e, r[tk].strip(), r[trd].strip(), r[lot].strip())

    for r in uni:
        r["cash_token"] = cash.get(r["symbol"], r.get("cash_token", ""))
        if r["symbol"] in fut:
            _, r["fut_token"], r["fut_symbol"], r["lot_size"] = fut[r["symbol"]]
    with open(a.universe, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["symbol", "sector", "cash_token", "fut_token", "fut_symbol", "lot_size"])
        w.writeheader()
        w.writerows({k: r.get(k, "") for k in w.fieldnames} for r in uni)

    print(f"cash tokens: {len(cash)}/{len(uni)}   futures tokens: {len(fut)}/{len(uni)}")
    miss = [r["symbol"] for r in uni if not r["cash_token"] or not r["fut_token"]]
    if miss:
        print("missing:", ", ".join(miss))


if __name__ == "__main__":
    main()
