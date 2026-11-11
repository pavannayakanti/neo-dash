"""Build data/daily/<SYMBOL>.csv (daily OHLCV) from NSE CM UDiFF bhavcopy files.

Swing signals need ~200 trading days of history (60 minimum). Two ways to get the files:

  1. Let tools/fetch_bhavcopy.py download them (run on your own machine/VPS):
        python tools/fetch_bhavcopy.py --days 260 --out bhav/
  2. Or download manually from nseindia.com -> All Reports -> "CM - Bhavcopy (UDiFF)"

Then:
        python tools/build_daily.py bhav/

Re-run every evening after ~18:00 (when NSE publishes the day's file) -- it's incremental,
so just drop the new file in the folder. Prices are NOT adjusted for splits/bonuses: after a
corporate action on one of your stocks, delete its CSV and rebuild from adjusted data.
"""
import argparse
import csv
import io
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent


def rows_from(path: Path):
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if name.lower().endswith(".csv"):
                    yield from csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8"))
    else:
        yield from csv.DictReader(open(path, encoding="utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", help="folder with BhavCopy_NSE_CM_*.csv or .zip files")
    ap.add_argument("--universe", default=BASE / "data/universe.csv")
    ap.add_argument("--out", default=BASE / "data/daily")
    a = ap.parse_args()

    want = {r["symbol"] for r in csv.DictReader(open(a.universe))}
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    bars: dict[str, dict[str, dict]] = {s: {} for s in want}
    for s in want:                                   # keep what's already built
        p = out / f"{s.replace('&', '_')}.csv"
        if p.exists():
            for r in csv.DictReader(open(p)):
                bars[s][r["date"]] = r

    files = sorted(Path(a.folder).glob("BhavCopy_NSE_CM_*"))
    for f in files:
        for r in rows_from(f):
            s = r.get("TckrSymb")
            if s in want and r.get("SctySrs") == "EQ":
                d = r["TradDt"]  # YYYY-MM-DD
                bars[s][d] = {"date": d, "open": r["OpnPric"], "high": r["HghPric"], "low": r["LwPric"],
                              "close": r["ClsPric"], "volume": r["TtlTradgVol"]}

    for s, by_date in bars.items():
        with open(out / f"{s.replace('&', '_')}.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["date", "open", "high", "low", "close", "volume"])
            w.writeheader()
            for d in sorted(by_date):
                w.writerow(by_date[d])
    lens = sorted(len(v) for v in bars.values())
    print(f"processed {len(files)} files -> {out}  (days per symbol: min {lens[0]}, max {lens[-1]})")
    if lens[0] < 60:
        print("note: swing signals need at least 60 days per symbol (200 for the 200-day average)")


if __name__ == "__main__":
    main()
