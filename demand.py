"""Cambridge hourly demand from Bluebikes trip files, corrected for stockouts with the GBFS logs.

    python demand.py --months 202510               # one month
    python demand.py --months 202501-202610        # a range (inclusive)
    python demand.py --selftest

Steps: download monthly trip files from S3 into data/trips/ (cached, gitignored), count hourly pickups and
drop-offs at Cambridge stations (region_id 8 in data/station_information), then join the logger's
hourly stockout shares and write data/demand_rates.csv, one row per station x day_type x hour:

    pickup_rate_raw        mean observed pickups per hour over every downloaded day
    logged_days            days where both trips and GBFS logs exist (the correction is estimated on these)
    mean_share_empty       share of that hour the station was empty, on logged days
    pickup_correction      in-stock-time rate / raw rate on logged days (1.0 when logged_days = 0)
    pickup_rate_corrected  pickup_rate_raw * pickup_correction  <- use this in the model
    (drop-offs likewise, with share_full)

The in-stock rate is pickups / hours the station had a bike, so an hour spent fully empty adds no
exposure and no pickups: its demand counts as ">= observed", as the plan says.
Only the post-2023 trip schema (started_at, start_station_id, ...) is supported.
"""
import argparse
import csv
import io
import re
import urllib.request
import zipfile
from collections import defaultdict
from datetime import date
from pathlib import Path

from gbfs_logger import DATA_DIR, summarize

BUCKET_URL = "https://s3.amazonaws.com/hubway-data/"
TRIPS_DIR = DATA_DIR / "trips"
CAMBRIDGE_REGION_ID = "8"


def expand_months(month_args):
    months = []
    for month_arg in month_args:
        first, _, last = month_arg.partition("-")
        year, month = int(first[:4]), int(first[4:])
        while True:
            months.append(f"{year}{month:02d}")
            if not last or months[-1] == last:
                break
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def download_month(month, bucket_keys):
    # file names vary: 202610-bluebikes-tripdata.zip, 202609-...csv, 202511-...csv.zip
    matching_keys = [key for key in bucket_keys if key.startswith(month) and "tripdata" in key]
    if not matching_keys:
        raise SystemExit(f"no trip file for {month} in the bucket (published ~5 days after month end)")
    local_path = TRIPS_DIR / matching_keys[0]
    if not local_path.exists():
        TRIPS_DIR.mkdir(parents=True, exist_ok=True)
        print(f"downloading {matching_keys[0]}")
        urllib.request.urlretrieve(BUCKET_URL + matching_keys[0], local_path)
    return local_path


def open_trip_csv(path):
    if path.suffix == ".zip":
        archive = zipfile.ZipFile(path)
        csv_name = next(name for name in archive.namelist() if name.endswith(".csv") and "__MACOSX" not in name)
        return io.TextIOWrapper(archive.open(csv_name), encoding="utf-8")
    return open(path, encoding="utf-8")


def cambridge_short_names():
    short_names = set()
    for info_path in (DATA_DIR / "station_information").glob("*.csv"):
        with open(info_path) as csv_file:
            short_names |= {row["short_name"].strip() for row in csv.DictReader(csv_file) if row["region_id"] == CAMBRIDGE_REGION_ID}
    if not short_names:
        raise SystemExit("no station_information logs found: run `git pull` or `python gbfs_logger.py` first")
    return short_names


def count_hourly_trips(trip_paths, stations):
    """-> {(short_name, date_str, hour): [pickups, dropoffs]}, plus the set of dates covered."""
    counts = defaultdict(lambda: [0, 0])
    trip_dates = set()
    for trip_path in trip_paths:
        reader = csv.DictReader(open_trip_csv(trip_path))
        if "started_at" not in reader.fieldnames:
            raise SystemExit(f"{trip_path.name} uses the pre-2023 schema, which this script doesn't read")
        for trip in reader:
            trip_dates.add(trip["started_at"][:10])
            if trip["start_station_id"] in stations:
                counts[(trip["start_station_id"], trip["started_at"][:10], int(trip["started_at"][11:13]))][0] += 1
            if trip["end_station_id"] in stations:
                counts[(trip["end_station_id"], trip["ended_at"][:10], int(trip["ended_at"][11:13]))][1] += 1
    return counts, trip_dates


def read_stockout_shares():
    path = DATA_DIR / "hourly_stockouts.csv"
    with open(path) as csv_file:
        return {(row["short_name"].strip(), row["date"], int(row["hour"])): row for row in csv.DictReader(csv_file)}


def day_type(date_str):
    # ponytail: weekday/weekend only; holidays count as weekdays. Add a holiday list if they skew results.
    return "weekend" if date.fromisoformat(date_str).weekday() >= 5 else "weekday"


def estimate_rates(stations, trip_dates, trip_counts, stockout_shares):
    totals = defaultdict(lambda: defaultdict(float))
    for short_name in stations:
        for date_str in trip_dates:
            for hour in range(24):
                pickups, dropoffs = trip_counts.get((short_name, date_str, hour), (0, 0))
                group = totals[(short_name, day_type(date_str), hour)]
                group["days"] += 1
                group["pickups"] += pickups
                group["dropoffs"] += dropoffs
                shares = stockout_shares.get((short_name, date_str, hour))
                if shares is None:
                    continue  # logger wasn't running: no correction info for this hour
                offline = float(shares["share_offline"])
                group["logged_days"] += 1
                group["logged_pickups"] += pickups
                group["logged_dropoffs"] += dropoffs
                group["share_empty"] += float(shares["share_empty"])
                group["share_full"] += float(shares["share_full"])
                group["pickup_exposure"] += 1 - float(shares["share_empty"]) - offline
                group["dropoff_exposure"] += 1 - float(shares["share_full"]) - offline

    rows = []
    for (short_name, day_kind, hour), group in sorted(totals.items()):
        row = {"short_name": short_name, "day_type": day_kind, "hour": hour, "days": int(group["days"]),
               "logged_days": int(group["logged_days"])}
        for flow, stockout in (("pickup", "share_empty"), ("dropoff", "share_full")):
            raw_rate = group[flow + "s"] / group["days"]
            correction = 1.0
            if group["logged_days"] and group[f"logged_{flow}s"] and group[f"{flow}_exposure"] > 0:
                observed_rate_logged = group[f"logged_{flow}s"] / group["logged_days"]
                in_stock_rate_logged = group[f"logged_{flow}s"] / group[f"{flow}_exposure"]
                correction = in_stock_rate_logged / observed_rate_logged
            row[f"{flow}_rate_raw"] = round(raw_rate, 4)
            row[f"mean_{stockout}"] = round(group[stockout] / group["logged_days"], 3) if group["logged_days"] else ""
            row[f"{flow}_correction"] = round(correction, 4)
            row[f"{flow}_rate_corrected"] = round(raw_rate * correction, 4)
        rows.append(row)
    return rows


def main(month_args):
    with urllib.request.urlopen(BUCKET_URL, timeout=30) as response:
        bucket_keys = re.findall(r"<Key>(.*?)</Key>", response.read().decode())
    months = expand_months(month_args)
    trip_paths = [download_month(month, bucket_keys) for month in months]
    stations = cambridge_short_names()
    trip_counts, trip_dates = count_hourly_trips(trip_paths, stations)
    trip_dates = {d for d in trip_dates if d[:4] + d[5:7] in months}  # files hold a few trips from adjacent days
    summarize()  # refresh data/hourly_stockouts.csv from the latest logs
    rows = estimate_rates(stations, trip_dates, trip_counts, read_stockout_shares())

    output_path = DATA_DIR / "demand_rates.csv"
    with open(output_path, "w", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    logged = sum(row["logged_days"] > 0 for row in rows)
    print(f"wrote {output_path}: {len(stations)} stations, {len(trip_dates)} days, "
          f"{logged}/{len(rows)} station-hours have stockout logs")


def selftest():
    assert expand_months(["202511-202602"]) == ["202511", "202512", "202601", "202602"]
    assert expand_months(["202610"]) == ["202610"]
    # 2 weekdays at 8am: day 1 empty half the hour with 3 pickups, day 2 never empty with 4 pickups
    trip_counts = {("M1", "2026-11-17", 8): [3, 0], ("M1", "2026-11-18", 8): [4, 2]}
    shares = {("M1", "2026-11-17", 8): {"share_empty": "0.5", "share_full": "0", "share_offline": "0"},
              ("M1", "2026-11-18", 8): {"share_empty": "0", "share_full": "0", "share_offline": "0"}}
    rows = estimate_rates({"M1"}, {"2026-11-17", "2026-11-18"}, trip_counts, shares)
    eight_am = next(row for row in rows if row["day_type"] == "weekday" and row["hour"] == 8)
    assert eight_am["pickup_rate_raw"] == 3.5
    assert eight_am["pickup_rate_corrected"] == round(7 / 1.5, 4)  # 7 pickups over 1.5 in-stock hours
    assert eight_am["dropoff_rate_corrected"] == 1.0  # never full: no correction
    assert next(row for row in rows if row["hour"] == 3)["pickup_rate_corrected"] == 0.0
    print("selftest ok")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--months", nargs="+", help="YYYYMM or YYYYMM-YYYYMM")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        selftest()
    elif args.months:
        main(args.months)
    else:
        parser.error("give --months or --selftest")
