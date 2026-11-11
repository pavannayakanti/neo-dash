"""Download recent NSE CM UDiFF bhavcopies (one zip per trading day) into a folder.

    python tools/fetch_bhavcopy.py --days 260 --out bhav/

Run this on your own machine or VPS. Holidays simply return 404 and are skipped.
NSE sometimes rejects automated downloads; if every request fails, download the files
manually (nseindia.com -> All Reports -> "CM - Bhavcopy (UDiFF)") into the same folder.
Be polite: the script waits between requests.
"""
import argparse
import time
import urllib.request
from datetime import date, timedelta
from pathlib import Path

URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d}_F_0000.csv.zip"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "*/*", "Referer": "https://www.nseindia.com/"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=260, help="calendar weekdays to look back")
    ap.add_argument("--out", default="bhav")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    d, done, ok, missing = date.today(), 0, 0, 0
    while done < a.days:
        d -= timedelta(days=1)
        if d.weekday() >= 5:
            continue
        done += 1
        ds = d.strftime("%Y%m%d")
        dest = out / f"BhavCopy_NSE_CM_0_0_0_{ds}_F_0000.csv.zip"
        if dest.exists():
            continue
        try:
            req = urllib.request.Request(URL.format(d=ds), headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as r:
                dest.write_bytes(r.read())
            ok += 1
        except Exception:
            missing += 1   # holiday, not yet published, or blocked
        time.sleep(0.7)
    print(f"downloaded {ok}, skipped/unavailable {missing} -> {out}")


if __name__ == "__main__":
    main()
