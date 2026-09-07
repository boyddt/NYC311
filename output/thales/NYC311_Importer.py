#!/usr/bin/env python3
"""Chunked importer for the NYC 311 service request export.

Streams the 14.5 GB CSV in chunks and batch-inserts into MariaDB.

Fixes over the original NYC311_Importer.py:
  * Dates parsed with the format the file actually uses
    ('05/09/2026 02:32:59 AM'), instead of '%m/%d/%Y' which matched nothing
    and — under errors='coerce' — silently nulled all 21 M rows.
  * Time of day preserved; the original called .dt.date and discarded it.
  * A parse failure is now detected and raised instead of being coerced away.
  * Dropped `df.where(pd.notna(df), None)`, which reintroduced NaN on float
    columns and caused "nan can not be used with MySQL".
  * Credentials read from ~/.my.cnf instead of a hardcoded password.
  * Target table, truncation, and row limit are explicit arguments, so a test
    run cannot accidentally append to the populated table.
"""

from __future__ import annotations

import argparse
import functools
import logging
import sys
import time
from pathlib import Path
from typing import Callable, Iterator, Sequence

import pandas as pd
import pymysql

LOG = logging.getLogger("nyc311.import")

DEFAULT_CSV = Path(
    "/home/davidtboyd/Dropbox/Data Science Projects/Datasets/NYC311/311_Service_Requests.csv"
)
DEFAULT_OPTION_FILE = Path.home() / ".my.cnf"
DEFAULT_DATABASE = "nyc311_calls"
DEFAULT_TABLE = "NYC311"
DEFAULT_CHUNK_SIZE = 10_000

# The export writes '05/09/2026 02:32:59 AM'. %I is the 12-hour clock and pairs
# with %p; %H would silently fail to match on every row.
DATE_FORMAT = "%m/%d/%Y %I:%M:%S %p"

COLUMN_MAPPING = {
    "Unique Key": "Unique_Key",
    "Created Date": "Created_Date",
    "Closed Date": "Closed_Date",
    "Agency": "Agency",
    "Agency Name": "Agency_Name",
    "Problem (formerly Complaint Type)": "Problem",
    "Problem Detail (formerly Descriptor)": "Problem_Detail",
    "Additional Details": "Additional_Details",
    "Location Type": "Location_Type",
    "Incident Zip": "Incident_Zip",
    "Incident Address": "Incident_Address",
    "Street Name": "Street_Name",
    "Cross Street 1": "Cross_Street_1",
    "Cross Street 2": "Cross_Street_2",
    "Intersection Street 1": "Intersection_Street_1",
    "Intersection Street 2": "Intersection_Street_2",
    "Address Type": "Address_Type",
    "City": "City",
    "Landmark": "Landmark",
    "Facility Type": "Facility_Type",
    "Status": "Status",
    "Due Date": "Due_Date",
    "Resolution Description": "Resolution_Description",
    "Resolution Action Updated Date": "Resolution_Action_Updated_Date",
    "Community Board": "Community_Board",
    "Council District": "Council_Dicharict",
    "Police Precinct": "Police_Precinct",
    "BBL": "BBL",
    "Borough": "Borough",
    "X Coordinate (State Plane)": "X_Coordinate_State_Plane",
    "Y Coordinate (State Plane)": "Y_Coordinate_State_Plane",
    "Open Data Channel Type": "Open_Data_Channel_Type",
    "Park Facility Name": "Park_Facility_Name",
    "Park Borough": "Park_Borough",
    "Vehicle Type": "Vehicle_Type",
    "Taxi Company Borough": "Taxi_Company_Borough",
    "Taxi Pick Up Location": "Taxi_Pick_Up_Location",
    "Bridge Highway Name": "Bridge_Highway_Name",
    "Bridge Highway Direction": "Bridge_Highway_Direction",
    "Road Ramp": "Road_Ramp",
    "Bridge Highway Segment": "Bridge_Highway_Segment",
    "Latitude": "Latitude",
    "Longitude": "Longitude",
    "Location": "Location",
}

DATE_COLUMNS = ("Created_Date", "Closed_Date", "Due_Date", "Resolution_Action_Updated_Date")
INTEGER_COLUMNS = ("Unique_Key",)
FLOAT_COLUMNS = ("BBL", "Latitude", "Longitude")

VARCHAR_LIMITS = {
    "Agency": 6, "Agency_Name": 50, "Problem": 40, "Problem_Detail": 50,
    "Additional_Details": 40, "Location_Type": 30, "Incident_Zip": 255,
    "Incident_Address": 50, "Street_Name": 50, "Cross_Street_1": 30,
    "Cross_Street_2": 30, "Intersection_Street_1": 30, "Intersection_Street_2": 30,
    "Address_Type": 15, "City": 25, "Landmark": 30, "Facility_Type": 20,
    "Status": 15, "Community_Board": 30, "Council_Dicharict": 255,
    "Police_Precinct": 20, "Borough": 15, "X_Coordinate_State_Plane": 15,
    "Y_Coordinate_State_Plane": 15, "Open_Data_Channel_Type": 20,
    "Park_Facility_Name": 15, "Park_Borough": 20, "Vehicle_Type": 15,
    "Taxi_Company_Borough": 25, "Taxi_Pick_Up_Location": 125,
    "Bridge_Highway_Name": 25, "Bridge_Highway_Direction": 125,
    "Road_Ramp": 125, "Bridge_Highway_Segment": 15, "Location": 255,
}


class ImportError_(RuntimeError):
    """Raised when the import cannot proceed safely."""


def timed(label: str) -> Callable:
    """Log how long the wrapped call took."""

    def decorate(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            started = time.monotonic()
            try:
                return func(*args, **kwargs)
            finally:
                LOG.info("%s finished in %.1f s", label, time.monotonic() - started)

        return wrapper

    return decorate


def parse_date_column(values: pd.Series, column: str, date_format: str = DATE_FORMAT) -> pd.Series:
    """Parse one date column, distinguishing empty values from parse failures.

    The original importer used errors='coerce', which turned a total format
    mismatch into 21 million silent NULLs. Blanks are legitimate — Closed Date
    is empty for open requests — so they are allowed through, but a value that
    is present and unparseable is a bug and raises.
    """
    text = values.astype("string").str.strip()
    blank = text.isna() | (text == "")

    parsed = pd.to_datetime(text, format=date_format, errors="coerce")
    unparseable = parsed.isna() & ~blank

    if unparseable.any():
        samples = text[unparseable].head(3).tolist()
        raise ImportError_(
            f"{column}: {int(unparseable.sum())} value(s) did not match "
            f"{date_format!r}; examples: {samples}"
        )
    return parsed


def truncate_to_schema(values: pd.Series, limit: int) -> pd.Series:
    """Clip strings to the column's varchar width, leaving nulls alone."""
    return values.map(lambda v: v if v is None or pd.isna(v) else str(v)[:limit])


def clean_chunk(chunk: pd.DataFrame, date_format: str = DATE_FORMAT) -> pd.DataFrame:
    """Type-convert and clip one chunk. Nulls stay as pandas NA until insert."""
    chunk = chunk.replace(["nan", "NaN", "NAN", ""], pd.NA)

    for column in DATE_COLUMNS:
        if column in chunk.columns:
            chunk[column] = parse_date_column(chunk[column], column, date_format)

    for column in INTEGER_COLUMNS:
        if column in chunk.columns:
            chunk[column] = pd.to_numeric(chunk[column], errors="coerce").astype("Int64")

    for column in FLOAT_COLUMNS:
        if column in chunk.columns:
            chunk[column] = pd.to_numeric(chunk[column], errors="coerce")

    for column, limit in VARCHAR_LIMITS.items():
        if column in chunk.columns:
            chunk[column] = truncate_to_schema(chunk[column], limit)

    return chunk


def to_rows(chunk: pd.DataFrame, columns: Sequence[str]) -> list[tuple]:
    """Convert to insert tuples, mapping every flavour of null to None.

    This is the only place nulls become None. Doing it on the DataFrame — as
    the original did with df.where(pd.notna(df), None) — puts NaN straight back
    into float columns, which pymysql rejects.
    """
    return [
        tuple(None if pd.isna(value) else value for value in row)
        for row in chunk[list(columns)].itertuples(index=False, name=None)
    ]


def normalize_headers(chunk: pd.DataFrame) -> pd.DataFrame:
    """Strip stray whitespace and quote characters from column names.

    Some exports write ', "Created Date"' with a space after the comma, which
    pandas reads as the literal column ' "Created Date"'. Left alone, the name
    fails to match COLUMN_MAPPING and the column is silently dropped.
    """
    return chunk.rename(columns=lambda name: name.strip().strip('"').strip("'").strip())


def require_columns(present: Sequence[str], csv_path: Path) -> None:
    """Refuse to import if a column we depend on never arrived."""
    missing = [c for c in (*DATE_COLUMNS, "Unique_Key", "Problem", "Borough") if c not in present]
    if missing:
        raise ImportError_(
            f"{csv_path.name}: required columns absent after header mapping: {missing}. "
            "Check the file's header row rather than importing partial data."
        )


def build_insert(table: str, columns: Sequence[str]) -> str:
    placeholders = ", ".join(["%s"] * len(columns))
    return f"INSERT INTO `{table}` ({', '.join(columns)}) VALUES ({placeholders})"


def connect(option_file: Path, database: str) -> pymysql.connections.Connection:
    if not option_file.is_file():
        raise ImportError_(f"no credentials file at {option_file}")
    return pymysql.connect(
        read_default_file=str(option_file),
        database=database,
        charset="utf8mb4",
        local_infile=False,
    )


def read_chunks(csv_path: Path, chunk_size: int, max_rows: int | None) -> Iterator[pd.DataFrame]:
    return pd.read_csv(csv_path, dtype=str, chunksize=chunk_size, nrows=max_rows)


@timed("Import")
def import_csv(
    csv_path: Path,
    *,
    table: str,
    database: str,
    option_file: Path,
    chunk_size: int,
    max_rows: int | None,
    truncate: bool,
    date_format: str = DATE_FORMAT,
) -> int:
    if not csv_path.is_file():
        raise ImportError_(f"CSV not found: {csv_path}")

    LOG.info("Source: %s (%.2f GB)", csv_path, csv_path.stat().st_size / 1024**3)
    LOG.info("Target: %s.%s | chunk size %d", database, table, chunk_size)

    connection = connect(option_file, database)
    inserted = 0
    try:
        with connection.cursor() as cursor:
            if truncate:
                cursor.execute(f"SELECT COUNT(*) FROM `{table}`")
                existing = cursor.fetchone()[0]
                LOG.warning("Truncating %s (%d existing rows)", table, existing)
                cursor.execute(f"TRUNCATE TABLE `{table}`")
                connection.commit()

            for number, chunk in enumerate(read_chunks(csv_path, chunk_size, max_rows), start=1):
                chunk = normalize_headers(chunk).rename(columns=COLUMN_MAPPING)
                columns = [c for c in COLUMN_MAPPING.values() if c in chunk.columns]
                if number == 1:
                    require_columns(columns, csv_path)
                rows = to_rows(clean_chunk(chunk[columns], date_format), columns)

                cursor.executemany(build_insert(table, columns), rows)
                connection.commit()
                inserted += len(rows)
                LOG.info("chunk %d: +%d rows | total %d", number, len(rows), inserted)
    finally:
        connection.close()

    LOG.info("Inserted %d rows", inserted)
    return inserted


def verify(table: str, database: str, option_file: Path) -> None:
    """Confirm the load actually populated the dates. Absence of an error is
    not evidence of success — that is exactly how the original failure hid."""
    connection = connect(option_file, database)
    try:
        with connection.cursor() as cursor:
            checks = ", ".join(f"SUM(`{c}` IS NOT NULL)" for c in DATE_COLUMNS)
            cursor.execute(f"SELECT COUNT(*), {checks} FROM `{table}`")
            total, *populated = cursor.fetchone()
            LOG.info("Verification on %s: %d rows", table, total)
            for column, count in zip(DATE_COLUMNS, populated):
                share = (count / total * 100) if total else 0.0
                LOG.info("  %-32s %d non-null (%.1f%%)", column, count, share)
            if total and populated[0] == 0:
                raise ImportError_("Created_Date is entirely NULL — the original bug has recurred")
    finally:
        connection.close()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--table", default=DEFAULT_TABLE)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--option-file", type=Path, default=DEFAULT_OPTION_FILE)
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument("--max-rows", type=int, default=None)
    parser.add_argument("--date-format", default=DATE_FORMAT,
                        help="strptime format of the date columns in this CSV")
    parser.add_argument("--truncate", action="store_true",
                        help="empty the target table first (required for a clean reload)")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    args = parse_args(argv)
    try:
        import_csv(
            args.csv,
            table=args.table,
            database=args.database,
            option_file=args.option_file,
            chunk_size=args.chunk_size,
            max_rows=args.max_rows,
            truncate=args.truncate,
            date_format=args.date_format,
        )
        verify(args.table, args.database, args.option_file)
    except ImportError_ as exc:
        LOG.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
