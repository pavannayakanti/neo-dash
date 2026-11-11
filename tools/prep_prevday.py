"""Build data/prevday.csv from NSE's previous-day bhavcopies (UDiFF format).

Download yesterday's files from nseindia.com -> All Reports:
  * Equities  -> "CM - Bhavcopy (UDiFF)"    e.g. BhavCopy_NSE_CM_0_0_0_20261106_F_0000.csv
  * Derivatives -> "F&O - Bhavcopy (UDiFF)" e.g. BhavCopy_NSE_FO_0_0_0_20261106_F_0000.csv
(unzip them), then:

  python tools/prep_prevday.py --cm BhavCopy_NSE_CM_...csv --fo BhavCopy_NSE_FO_...csv

Gives pivots (prev H/L/C), relative volume (prev volume) and true OI change (prev OI).
Run every morning before 09:15.
"""
import argparse
import csv
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cm", required=True, help="NSE CM UDiFF bhavcopy CSV")
    ap.add_argument("--fo", help="NSE F&O UDiFF bhavcopy CSV (for prev OI)")
    ap.add_argument("--universe", default=BASE / "data/universe.csv")
    ap.add_argument("--out", default=BASE / "data/prevday.csv")
    a = ap.parse_args()

    symbols = [r["symbol"] for r in csv.DictReader(open(a.universe))]
    want = set(symbols)
    rows = {s: {"symbol": s} for s in symbols}

    for r in csv.DictReader(open(a.cm)):
        s = r.get("TckrSymb")
        if s in want and r.get("SctySrs") == "EQ":
            rows[s].update(prev_close=r["ClsPric"], prev_high=r["HghPric"],
                           prev_low=r["LwPric"], prev_volume=r["TtlTradgVol"])

    if a.fo:
        near: dict[str, dict] = {}
        for r in csv.DictReader(open(a.fo)):
            s = r.get("TckrSymb")
            if s in want and r.get("FinInstrmTp") == "STF":   # stock futures
                if s not in near or r["XpryDt"] < near[s]["XpryDt"]:
                    near[s] = r                                 # keep nearest expiry
        for s, r in near.items():
            rows[s].update(fut_prev_close=r.get("SttlmPric") or r["ClsPric"], prev_oi=r["OpnIntrst"])

    cols = ["symbol", "prev_close", "prev_high", "prev_low", "prev_volume", "fut_prev_close", "prev_oi"]
    with open(a.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for s in symbols:
            w.writerow({c: rows[s].get(c, "") for c in cols})
    missing = [s for s in symbols if "prev_close" not in rows[s]]
    print(f"wrote {a.out}  ({len(symbols) - len(missing)}/{len(symbols)} matched)")
    if missing:
        print("not found in CM bhavcopy:", ", ".join(missing))


if __name__ == "__main__":
    main()
