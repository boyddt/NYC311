#!/usr/bin/env python3
"""Reconcile NYC311 row counts in MariaDB against the Socrata source.

Counts by created_date on both sides and prints the difference. This is the
check that caught the offset pager losing 20,566 rows: totals looked plausible,
and only a period-by-period comparison showed the shortfall concentrated in the
months NYC edits most.

A small non-zero drift is normal — the source is live, and rows are created
while the comparison runs. A drift that grows with recency, or that persists
across repeated checks, is not drift.

    python reconcile_counts.py                     # by year, then 2026 by month
    python reconcile_counts.py --by month --from 2026-01 --to 2026-10
    python reconcile_counts.py --by day --from 2026-08-01 --to 2026-09-08

The app token is read from NYC_APP_TOKEN; database credentials from ~/.my.cnf.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import pymysql

RESOURCE_URL = "https://data.cityofnewyork.us/resource/erm2-nwe9.json"
DEFAULT_OPTION_FILE = Path.home() / ".my.cnf"
DEFAULT_DATABASE = "nyc311_calls"
DEFAULT_TABLE = "NYC311"
TIMEOUT_SECONDS = 300
MAX_ATTEMPTS = 4

# grain -> (Socrata truncation function, MySQL format string, label width)
GRAINS = {
    "year":  ("date_trunc_y",  "%Y",       4),
    "month": ("date_trunc_ym", "%Y-%m",    7),
    "day":   ("date_trunc_ymd", "%Y-%m-%d", 10),
}


class ReconcileError(RuntimeError):
    """The comparison could not be completed."""


def fetch(params: dict[str, str], token: str | None) -> list[dict]:
    """GET one aggregate from Socrata, retrying transient network failures.

    Aggregates over 22M rows regularly take 20-45 s and occasionally time out;
    a read timeout surfaces as a bare TimeoutError that urllib does not wrap in
    URLError, so OSError is what has to be caught here.
    """
    url = f"{RESOURCE_URL}?{urllib.parse.urlencode(params)}"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        if token:
            request.add_header("X-App-Token", token)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if not (exc.code == 429 or exc.code >= 500) or attempt == MAX_ATTEMPTS:
                raise
            print(f"  HTTP {exc.code}, retrying ({attempt}/{MAX_ATTEMPTS})", file=sys.stderr)
        except (OSError, json.JSONDecodeError) as exc:
            if attempt == MAX_ATTEMPTS:
                raise
            print(f"  {type(exc).__name__}, retrying ({attempt}/{MAX_ATTEMPTS})", file=sys.stderr)
        time.sleep(2 ** attempt)
    raise ReconcileError("retries exhausted")


def api_counts(grain: str, low: str | None, high: str | None, token: str | None) -> dict[str, int]:
    """Rows per period on the source side, keyed by period label."""
    truncate, _, width = GRAINS[grain]
    params = {
        "$select": f"{truncate}(created_date) AS period, count(1) AS n",
        "$group": "period",
        "$order": "period",
        "$limit": "50000",
    }
    clauses = []
    if low:
        clauses.append(f"created_date >= '{low}'")
    if high:
        clauses.append(f"created_date < '{high}'")
    if clauses:
        params["$where"] = " AND ".join(clauses)
    return {row["period"][:width]: int(row["n"]) for row in fetch(params, token)}


def db_counts(connection, table: str, grain: str,
              low: str | None, high: str | None) -> dict[str, int]:
    """Rows per period on our side, keyed the same way."""
    _, fmt, _ = GRAINS[grain]
    where, params = "", []
    clauses = []
    if low:
        clauses.append("Created_Date >= %s")
        params.append(low)
    if high:
        clauses.append("Created_Date < %s")
        params.append(high)
    if clauses:
        where = "WHERE " + " AND ".join(clauses)
    sql = (f"SELECT DATE_FORMAT(Created_Date, %s) AS period, COUNT(*) "
           f"FROM `{table}` {where} GROUP BY period ORDER BY period")
    with connection.cursor() as cursor:
        cursor.execute(sql, [fmt, *params])
        return {period: int(count) for period, count in cursor.fetchall() if period is not None}


def report(title: str, api: dict[str, int], db: dict[str, int]) -> int:
    """Print one comparison table; return the total absolute shortfall."""
    print(f"\n{title}")
    print(f"{'period':<12}{'API':>12}{'DB':>12}{'diff':>10}")
    print("-" * 46)
    shortfall = 0
    for period in sorted(set(api) | set(db)):
        source, ours = api.get(period, 0), db.get(period, 0)
        difference = ours - source
        if difference < 0:
            shortfall += -difference
        flag = "" if difference == 0 else ("  <- DB short" if difference < 0 else "  <- DB extra")
        print(f"{period:<12}{source:>12,}{ours:>12,}{difference:>+10,}{flag}")
    print("-" * 46)
    total_api, total_db = sum(api.values()), sum(db.values())
    print(f"{'total':<12}{total_api:>12,}{total_db:>12,}{total_db - total_api:>+10,}")
    return shortfall


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reconcile DB row counts against Socrata.")
    parser.add_argument("--by", choices=sorted(GRAINS), default=None,
                        help="grain to compare (default: year, then 2026 by month)")
    parser.add_argument("--from", dest="low", default=None,
                        help="created_date lower bound, inclusive (ISO)")
    parser.add_argument("--to", dest="high", default=None,
                        help="created_date upper bound, exclusive (ISO)")
    parser.add_argument("--table", default=DEFAULT_TABLE)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--option-file", type=Path, default=DEFAULT_OPTION_FILE)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    token = os.environ.get("NYC_APP_TOKEN")
    if not token:
        print("warning: no NYC_APP_TOKEN; sharing the throttled per-IP pool", file=sys.stderr)
    if not args.option_file.is_file():
        print(f"error: no credentials file at {args.option_file}", file=sys.stderr)
        return 1

    connection = pymysql.connect(read_default_file=str(args.option_file),
                                 database=args.database, charset="utf8mb4")
    try:
        if args.by:
            passes = [(args.by, args.low, args.high)]
        else:                       # the default view: years, then this year's months
            passes = [("year", None, None), ("month", "2026-01-01", "2026-10-01")]
        shortfall = 0
        for grain, low, high in passes:
            started = time.monotonic()
            api = api_counts(grain, low, high, token)
            ours = db_counts(connection, args.table, grain, low, high)
            title = f"By {grain}" + (f", {low} to {high}" if low or high else "")
            shortfall += report(f"{title}  ({time.monotonic() - started:.0f}s)", api, ours)
    finally:
        connection.close()

    print(f"\nchecked at {datetime.now().isoformat(timespec='seconds')}; "
          f"total DB shortfall across periods: {shortfall:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
