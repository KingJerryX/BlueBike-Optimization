"""Log Bluebikes GBFS station_status so we have the inventory history GBFS itself doesn't keep.

    python gbfs_logger.py                 # one snapshot, then exit (for Task Scheduler / cron / GitHub Actions)
    python gbfs_logger.py --loop 300      # poll every 300 s until killed
    python gbfs_logger.py --regions 8     # Cambridge only (what the GitHub Action runs)
    python gbfs_logger.py --summarize     # -> data/hourly_stockouts.csv (per station, date, hour)
    python gbfs_logger.py --selftest
On Windows, run `pip install tzdata` first (zoneinfo has no built-in time zone database there).

Output (all stations unless --regions is given):
    data/station_status/YYYY-MM-DD.csv       one row per station per snapshot
    data/station_information/YYYY-MM-DD.csv  capacity/coords, refreshed once a day (all stations)
    data/system_alerts.jsonl                 only written when Bluebikes posts an alert
Usable docks vary: num_bikes_available + num_bikes_disabled + num_docks_available + num_docks_disabled
differs from `capacity` at ~88 stations, so use that per-snapshot sum, not `capacity`, as the ceiling.
`short_name` (e.g. M32076) is the key that matches start_station_id/end_station_id in the trip CSVs.
"""
import argparse
import csv
import json
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

FEED_ROOT = "https://gbfs.lyft.com/gbfs/1.1/bos/en/"
DATA_DIR = Path(__file__).parent / "data"
BOSTON_TZ = ZoneInfo("America/New_York")

STATUS_COLUMNS = [
    "snapshot_unix", "snapshot_local", "station_id", "short_name", "region_id", "capacity",
    "num_bikes_available", "num_ebikes_available", "num_bikes_disabled",
    "num_docks_available", "num_docks_disabled",
    "is_installed", "is_renting", "is_returning", "last_reported",
]
# legacy_id = the numeric station id used in pre-2023 trip files
INFO_COLUMNS = ["station_id", "short_name", "legacy_id", "name", "region_id", "lat", "lon", "capacity"]


def fetch_feed(feed_name, retries=3):
    for attempt in range(retries):
        try:
            request = urllib.request.Request(FEED_ROOT + feed_name + ".json", headers={"User-Agent": "gbfs-logger"})
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except Exception as error:  # network blips must not kill a weeks-long logger
            if attempt == retries - 1:
                raise
            print(f"fetch {feed_name} failed ({error}), retrying", file=sys.stderr)
            time.sleep(10)


def append_rows(csv_path, columns, rows):
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    is_new_file = not csv_path.exists()
    with open(csv_path, "a", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=columns, extrasaction="ignore")
        if is_new_file:
            writer.writeheader()
        writer.writerows(rows)


def take_snapshot(region_ids=None):
    status_feed = fetch_feed("station_status")
    snapshot_unix = status_feed["last_updated"]
    snapshot_local = datetime.fromtimestamp(snapshot_unix, BOSTON_TZ)
    day = snapshot_local.strftime("%Y-%m-%d")

    status_path = DATA_DIR / "station_status" / f"{day}.csv"
    if status_path.exists():  # feed TTL is 60 s; skip if we already logged this exact feed version
        with open(status_path) as csv_file:
            last_line = csv_file.readlines()[-1]
        if last_line.startswith(f"{snapshot_unix},"):
            print(f"{snapshot_local:%H:%M:%S} unchanged feed, skipped")
            return

    info_path = DATA_DIR / "station_information" / f"{day}.csv"
    if not info_path.exists():
        stations = fetch_feed("station_information")["data"]["stations"]
        append_rows(info_path, INFO_COLUMNS, stations)
    with open(info_path) as csv_file:
        info_by_station_id = {row["station_id"]: row for row in csv.DictReader(csv_file)}

    rows = []
    for station in status_feed["data"]["stations"]:
        info = info_by_station_id.get(station["station_id"], {})
        if region_ids and info.get("region_id") not in region_ids:
            continue
        rows.append({
            **station,
            "snapshot_unix": snapshot_unix,
            "snapshot_local": snapshot_local.isoformat(timespec="seconds"),
            "short_name": info.get("short_name", ""),
            "region_id": info.get("region_id", ""),
            "capacity": info.get("capacity", ""),
        })
    append_rows(status_path, STATUS_COLUMNS, rows)

    alerts = fetch_feed("system_alerts")["data"]["alerts"]  # closures/events; usually empty, gone once over
    if alerts:
        with open(DATA_DIR / "system_alerts.jsonl", "a") as alerts_file:
            alerts_file.write(json.dumps({"snapshot_unix": snapshot_unix, "alerts": alerts}) + "\n")
    print(f"{snapshot_local:%Y-%m-%d %H:%M:%S} logged {len(rows)} stations")


def classify(row):
    """Return 'offline', 'empty', 'full' or 'ok' for one station snapshot row."""
    if row["is_installed"] != "1" or row["is_renting"] != "1" or row["is_returning"] != "1":
        return "offline"  # closed by the operator: not a demand-driven stockout
    if int(row["num_bikes_available"]) == 0:  # ebikes are included in num_bikes_available
        return "empty"
    if int(row["num_docks_available"]) == 0:
        return "full"
    return "ok"


def summarize():
    """Share of each station-hour spent empty / full. Feeds the stockout demand correction:
    pickup_rate_estimate = observed_pickups / (1 - share_empty), drop-offs likewise with share_full.
    Hours with share_empty ~ 1 carry no information: treat demand there as >= observed."""
    # ponytail: share of snapshots, not of minutes; equal if polling interval is steady, gaps bias it slightly
    counts = defaultdict(lambda: defaultdict(int))
    for status_path in sorted((DATA_DIR / "station_status").glob("*.csv")):
        with open(status_path) as csv_file:
            for row in csv.DictReader(csv_file):
                key = (row["short_name"], row["region_id"], row["snapshot_local"][:10], int(row["snapshot_local"][11:13]))
                counts[key][classify(row)] += 1
                counts[key]["bikes_total"] += int(row["num_bikes_available"])

    output_path = DATA_DIR / "hourly_stockouts.csv"
    with open(output_path, "w", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(["short_name", "region_id", "date", "hour", "snapshots",
                         "share_empty", "share_full", "share_offline", "mean_bikes_available"])
        for (short_name, region_id, date, hour), tally in sorted(counts.items()):
            snapshots = tally["empty"] + tally["full"] + tally["ok"] + tally["offline"]
            writer.writerow([short_name, region_id, date, hour, snapshots,
                             round(tally["empty"] / snapshots, 3), round(tally["full"] / snapshots, 3),
                             round(tally["offline"] / snapshots, 3), round(tally["bikes_total"] / snapshots, 2)])
    print(f"wrote {output_path} ({len(counts)} station-hours)")


def selftest():
    base = {"is_installed": "1", "is_renting": "1", "is_returning": "1", "num_bikes_available": "3", "num_docks_available": "5"}
    assert classify(base) == "ok"
    assert classify({**base, "num_bikes_available": "0"}) == "empty"
    assert classify({**base, "num_docks_available": "0"}) == "full"
    assert classify({**base, "is_renting": "0", "num_bikes_available": "0"}) == "offline"
    print("selftest ok")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", type=int, metavar="SECONDS", help="poll forever at this interval")
    parser.add_argument("--regions", nargs="+", metavar="REGION_ID", help="only log these region_ids (Cambridge = 8)")
    parser.add_argument("--summarize", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        selftest()
    elif args.summarize:
        summarize()
    elif args.loop:
        while True:
            try:
                take_snapshot(args.regions)
            except Exception as error:
                print(f"snapshot failed: {error}", file=sys.stderr)
            time.sleep(args.loop)
    else:
        take_snapshot(args.regions)
