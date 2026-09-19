#!/usr/bin/env python3
"""Incremental loader for NYC 311 service requests.

Pulls changed rows from the Socrata feed for dataset erm2-nwe9 and upserts them
into nyc311_calls.NYC311.

Design notes, all of which are load-bearing:

  * Incremental loads are driven by the ``:updated_at`` system field, not
    ``created_date``. Roughly 87% of daily churn is modification of existing
    requests, which keep their original creation date; a created_date watermark
    silently drops all of it.
  * Nothing pages with ``$offset``. Two independent things break it here, and
    both were measured on 2026-09-06, when a backfill silently lost 20,566 rows:

      1. The source is edited continuously, and an update re-sorts that row to a
         later position in an ``:updated_at`` ordering. Every such re-sort slides
         one not-yet-read row back across an offset cursor, where no page will
         ever return it. No error is raised; the rows are simply absent.
      2. Worse, and particular to this feed: NYC's nightly refresh stamps its
         entire batch with a single identical ``:updated_at``. On 2026-09-06 that
         was **550,168 rows sharing one millisecond** (01:33:47.468). Ordering by
         a column on which half a million rows tie leaves their relative order
         arbitrary *and unstable between requests* — successive ``$offset`` pages
         return overlapping, gap-ridden slices of the same group. The 3.7%
         shortfall the reconciliation found is that, not just edit churn.

    Two paging strategies replace it, both immune to concurrent edits by
    construction:
      - incremental (``:updated_at``): a forward keyset cursor. Each page asks
        for ``:updated_at >= <max seen>`` and drops the rows it already has by
        Unique_Key. Rows only ever move *forward* in that ordering, so a row
        updated mid-run stays ahead of the cursor and is still collected. As a
        bonus this is O(1) in depth; ``$offset`` degraded from 7 s to 326 s by
        offset 1,000,000.
      - sweep (``created_date``): narrow windows, one query per window, no
        cursor at all. ``created_date`` is immutable, so a window's membership
        cannot change under us. A window that fills a whole page is split in
        half and retried, so completeness never depends on the page size.
  * Loads are idempotent. Each run overlaps the previous watermark by an hour
    and upserts on the Unique_Key primary key, so a run that dies mid-page can
    simply be re-run.
  * The lookup tables are reconciled before the upsert. agency_id, cb_id,
    lt_id and at_id are FOREIGN KEY enforced, so a row carrying an unseen
    agency or location type is rejected outright unless the lookup row exists
    first.
  * An empty page is not taken at face value. The 2026-09-18 feed answered
    `$where` and aggregate queries over ``:updated_at`` with values that
    contradicted the rows it would actually serve, so "page 1: 0 rows" stopped
    meaning "caught up" and a run reported success ~24,000 rows short. Every
    zero-row page is now corroborated by an unfiltered ordered probe for the
    newest ``:updated_at``, and a page that contradicts it raises.
  * The watermark advances only after a batch commits.
  * The run asserts rather than assumes. A batch far outside the expected size
    band, or a date column that arrives empty, exits non-zero. This pipeline's
    predecessor wrote NULL into every date column for months without
    complaining; silence is not success.
"""

from __future__ import annotations

import argparse
import functools
import http.client
import json
import logging
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

import pymysql

LOG = logging.getLogger("nyc311.etl")

RESOURCE_URL = "https://data.cityofnewyork.us/resource/erm2-nwe9.json"
SOURCE_NAME = "socrata:erm2-nwe9"

DEFAULT_OPTION_FILE = Path.home() / ".my.cnf"
DEFAULT_DATABASE = "nyc311_calls"
DEFAULT_TABLE = "NYC311"
DEFAULT_PAGE_SIZE = 50_000          # verified maximum for this endpoint
DEFAULT_OVERLAP_HOURS = 1
DEFAULT_TIMEOUT_SECONDS = 120
MAX_ATTEMPTS = 4

DEFAULT_SWEEP_WINDOW = timedelta(days=1)   # ~11k created rows/day, well under a page
MIN_SWEEP_WINDOW = timedelta(seconds=1)    # below this, a full page is not credible

# Outer bounds for a created_date sweep that has no range of its own — used
# when paging a single :updated_at tie group. The dataset starts in 2010; the
# floor is set earlier and the ceiling ahead of now so a mis-stamped record
# cannot fall outside the sweep.
CREATED_FLOOR = datetime(2000, 1, 1)
CREATED_CEILING_SLACK = timedelta(days=30)

# A drain is only ever started because the cursor just read a full page whose
# rows all carry one :updated_at — so the caller has already *proved* the group
# holds at least that many rows. A drain that reads back fewer is contradicting
# evidence we already hold, and the only safe reading of a contradiction is that
# the re-read failed, not that the group shrank to nothing.
#
# It can shrink a little for real: a row edited mid-drain moves to a later
# :updated_at and leaves the pinned group. Off-batch churn on this feed is a few
# rows a minute against a drain measured in minutes, so 1% of a page (500 rows
# at the default 50,000) is roughly two orders of magnitude of headroom. The
# floor keeps a small page size (tests, --page-size tuning) from aborting on a
# single edit.
#
# The threshold is deliberately tight because the two errors do not cost the
# same thing: a false abort costs one re-run, while a false "drained, complete"
# advances the watermark past rows no later cursor run will ever ask for again.
# That is the unearned-watermark failure mode, and this is its third occurrence.
DRAIN_SHORTFALL_FRACTION = 0.01
DRAIN_SHORTFALL_FLOOR = 5

# A drain that reads *nothing* has yielded nothing, so re-reading it from the
# start cannot double-count or pass off a partial read as complete. That — and
# only that — is retried: on 2026-09-15 the pinned predicate returned an empty
# body for a 551,857-row group and served the same group correctly hours later.
# Any other shortfall aborts on the first attempt; correctness beats recovery.
DRAIN_EMPTY_ATTEMPTS = 3
DRAIN_RETRY_DELAY = 5.0

# An empty page is the cursor's only signal for "there is nothing left to read",
# and on 2026-09-18 that signal stopped being trustworthy. The feed began
# answering `$where` and aggregate queries over :updated_at with values that
# contradicted its own rows. Measured live, minutes apart, same endpoint:
#
#     count where :updated_at > '2026-09-19T00:33:25.618'      ->       0
#     count where :updated_at = '2026-09-19T01:33:25.618'      -> 537,194
#     fetch rows where :updated_at = '2026-09-19T01:33:25.618' ->      []
#     max(:updated_at)                                         -> 2026-09-18T02:10:09.615
#     $order=:updated_at DESC $limit=3                         -> rows at 2026-09-19T02:11:45.242
#
# Those cannot all be true. The last line is the one backed by actual rows; the
# filtered and aggregated readings of :updated_at are the broken ones. A manual
# run at 21:36 that day fetched "page 1: 0 rows", reported "run complete: 0 rows
# processed" and exited 0 while the table was ~24,000 rows short. Only
# reconcile_counts.py noticed.
#
# So an empty page is now corroborated before it is believed, by the one shape
# that still tells the truth: order plus limit, and deliberately NO $where.
# Filtering on :updated_at is the broken capability — a filtered check would
# inherit the same wrong answer and confirm the bug to itself. That is the
# entire value of this probe; do not "simplify" it into a filtered query.
#
# One probe is not conclusive. Ten identical unfiltered probes on 2026-09-18
# came back 8x 2026-09-19T02:11:45.242 and 2x a stale 2026-09-18T02:10:09.615,
# so roughly a fifth of requests are served by a replica missing the newest
# batch. A stale answer can only make this check miss a real contradiction,
# never invent one, so the probe is repeated and the newest answer wins: at a
# measured 20% stale rate, five probes miss on the order of once in 3,000 runs
# where one probe would miss one run in five. They cost almost nothing — 8 of
# those 10 returned in under a second, the slowest in 8 s — and only an empty
# page pays for them at all.
NEWEST_PROBE_ATTEMPTS = 5
NEWEST_PROBE_PARAMS = {
    "$select": ":updated_at",
    "$order": ":updated_at DESC",
    "$limit": "1",
}

# Transient failures worth another attempt. OSError covers urllib's URLError
# (a subclass) *and* the bare TimeoutError that http.client raises from
# getresponse() on a read timeout — which urllib does not wrap, because it only
# wraps OSErrors from the connection phase. That escape killed two backfill runs
# with exit code 2 and never logged a single retry.
TRANSIENT_ERRORS = (OSError, http.client.HTTPException, json.JSONDecodeError)

# Socrata field -> our column. Our schema predates the API and does not match
# it: `complaint_type` is `Problem`, and `Council_Dicharict` is a typo we keep
# because the column is named that.
FIELD_MAP = {
    "unique_key": "Unique_Key",
    "created_date": "Created_Date",
    "closed_date": "Closed_Date",
    "agency": "Agency",
    "agency_name": "Agency_Name",
    "complaint_type": "Problem",
    "descriptor": "Problem_Detail",
    "location_type": "Location_Type",
    "incident_zip": "Incident_Zip",
    "incident_address": "Incident_Address",
    "street_name": "Street_Name",
    "cross_street_1": "Cross_Street_1",
    "cross_street_2": "Cross_Street_2",
    "intersection_street_1": "Intersection_Street_1",
    "intersection_street_2": "Intersection_Street_2",
    "address_type": "Address_Type",
    "city": "City",
    "landmark": "Landmark",
    "facility_type": "Facility_Type",
    "status": "Status",
    "due_date": "Due_Date",
    "resolution_description": "Resolution_Description",
    "resolution_action_updated_date": "Resolution_Action_Updated_Date",
    "community_board": "Community_Board",
    "council_district": "Council_Dicharict",
    "police_precinct": "Police_Precinct",
    "bbl": "BBL",
    "borough": "Borough",
    "x_coordinate_state_plane": "X_Coordinate_State_Plane",
    "y_coordinate_state_plane": "Y_Coordinate_State_Plane",
    "open_data_channel_type": "Open_Data_Channel_Type",
    "park_facility_name": "Park_Facility_Name",
    "park_borough": "Park_Borough",
    "vehicle_type": "Vehicle_Type",
    "taxi_company_borough": "Taxi_Company_Borough",
    "taxi_pick_up_location": "Taxi_Pick_Up_Location",
    "bridge_highway_name": "Bridge_Highway_Name",
    "bridge_highway_direction": "Bridge_Highway_Direction",
    "road_ramp": "Road_Ramp",
    "bridge_highway_segment": "Bridge_Highway_Segment",
    "latitude": "Latitude",
    "longitude": "Longitude",
}

DATE_COLUMNS = ("Created_Date", "Closed_Date", "Due_Date", "Resolution_Action_Updated_Date")
FLOAT_COLUMNS = ("BBL", "Latitude", "Longitude")
INTEGER_COLUMNS = ("Unique_Key",)

# (text column, lookup table, name column, id column) — order matters only for
# readable logging.
LOOKUPS = (
    ("Agency_Name",     "agencies",         "agency_name",          "agency_id"),
    ("Community_Board", "community_boards", "community_board_name", "cb_id"),
    ("Location_Type",   "location_types",   "location_type_name",   "lt_id"),
    ("Address_Type",    "address_types",    "address_type_name",    "at_id"),
)

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

TARGET_COLUMNS = tuple(FIELD_MAP.values()) + ("Location",) + tuple(l[3] for l in LOOKUPS)

WATERMARK_DDL = """
CREATE TABLE IF NOT EXISTS etl_watermark (
    source          VARCHAR(64)  NOT NULL,
    last_updated_at DATETIME(3)  NOT NULL,
    last_run_at     DATETIME     NOT NULL,
    rows_loaded     BIGINT       NOT NULL DEFAULT 0,
    PRIMARY KEY (source)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
"""

# A sweep has no watermark to advance — it walks created_date, which is
# unrelated to :updated_at — so its resumption point is a record of the windows
# that have committed. The progress row commits in the same transaction as the
# window's data, so an interrupted sweep leaves progress behind what was
# written, never ahead.
SWEEP_DDL = """
CREATE TABLE IF NOT EXISTS etl_sweep_progress (
    source       VARCHAR(64) NOT NULL,
    window_start DATETIME(3) NOT NULL,
    window_end   DATETIME(3) NOT NULL,
    rows_seen    BIGINT      NOT NULL DEFAULT 0,
    completed_at DATETIME    NOT NULL,
    PRIMARY KEY (source, window_start)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
"""


class EtlError(RuntimeError):
    """Raised when the load cannot proceed, or produced a result we distrust."""


# --------------------------------------------------------------------------
# decorators
# --------------------------------------------------------------------------

def timed(label: str) -> Callable:
    """Log how long the wrapped call took, whether or not it succeeded."""

    def decorate(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            started = time.monotonic()
            try:
                return func(*args, **kwargs)
            finally:
                LOG.info("%s took %.1f s", label, time.monotonic() - started)

        return wrapper

    return decorate


def retrying(attempts: int = MAX_ATTEMPTS, base_delay: float = 2.0) -> Callable:
    """Retry on transient HTTP and network failures with exponential backoff.

    Socrata throttles by returning 429, and 5xx responses are common enough
    under load to be worth surviving. A 4xx other than 429 is our bug, not a
    transient fault, so it is re-raised immediately.

    Everything in TRANSIENT_ERRORS is retried. Note that HTTPError is caught
    first: it is a subclass of URLError, which is a subclass of OSError, so a
    bare `except OSError` would otherwise swallow a 404 as a network blip.
    TimeoutError has no `.reason`, so the log line asks for one and falls back
    to the exception itself.
    """

    def decorate(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(1, attempts + 1):
                try:
                    return func(*args, **kwargs)
                except urllib.error.HTTPError as exc:
                    transient = exc.code == 429 or exc.code >= 500
                    if not transient or attempt == attempts:
                        raise
                    delay = base_delay * 2 ** (attempt - 1)
                    LOG.warning("HTTP %s, retry %d/%d in %.0f s", exc.code, attempt, attempts, delay)
                    time.sleep(delay)
                except TRANSIENT_ERRORS as exc:
                    if attempt == attempts:
                        raise
                    delay = base_delay * 2 ** (attempt - 1)
                    LOG.warning("network error (%s: %s), retry %d/%d in %.0f s",
                                type(exc).__name__, getattr(exc, "reason", exc),
                                attempt, attempts, delay)
                    time.sleep(delay)
            raise EtlError("retry loop exhausted without returning")

        return wrapper

    return decorate


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

@retrying()
def fetch_page(params: dict[str, str], app_token: str | None, timeout: int) -> list[dict[str, Any]]:
    url = f"{RESOURCE_URL}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    if app_token:
        request.add_header("X-App-Token", app_token)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def socrata_stamp(moment: datetime) -> str:
    """Render a datetime the way SoQL floating timestamps compare."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


SELECT_ALL_FIELDS = ",".join([":updated_at", *FIELD_MAP.keys(), "location"])


def fetch_newest_updated_at(*, app_token: str | None, timeout: int,
                            probes: int = 1) -> datetime | None:
    """The newest :updated_at the feed will admit to, asked without any filter.

    Sends `$select=:updated_at`, `$order=:updated_at DESC`, `$limit=1` and
    nothing else. The absence of a `$where` is the whole point, and is asserted
    by the tests rather than left to a reader's good intentions — see the note
    at NEWEST_PROBE_ATTEMPTS for why a filtered check would be worthless here.

    The probe is repeated `probes` times and the newest answer wins, because the
    endpoint currently serves some requests from a lagging replica. Returns None
    only if every probe came back with no row at all.
    """
    if probes < 1:
        raise EtlError(f"newest-:updated_at probe count must be positive, got {probes}")
    answers: list[datetime | None] = []
    for _ in range(probes):
        page = fetch_page(dict(NEWEST_PROBE_PARAMS), app_token, timeout)
        record = page[0] if page else {}
        answers.append(parse_timestamp(record.get(":updated_at"), ":updated_at"))
    if len(set(answers)) > 1:
        LOG.warning("the feed gave %d different answers to the same unfiltered "
                    "newest-:updated_at probe (%s); taking the newest",
                    len(set(answers)), ", ".join(str(answer) for answer in answers))
    present = [answer for answer in answers if answer is not None]
    return max(present) if present else None


def confirm_caught_up(cursor: datetime, *, app_token: str | None, timeout: int) -> None:
    """Refuse to read an empty page as "caught up" until the feed corroborates it.

    Called for every zero-row page, first or last. If the feed's own newest row
    is strictly newer than the cursor, then a correct server would have returned
    it and the empty page is the API contradicting itself — a failed read, not
    the end of the data. That raises, so the run exits non-zero and no watermark
    is written for rows nothing ever read. If the newest row is at or below the
    cursor, there genuinely is nothing left and the run returns normally.
    """
    newest = fetch_newest_updated_at(app_token=app_token, timeout=timeout,
                                     probes=NEWEST_PROBE_ATTEMPTS)
    if newest is None:
        raise EtlError(
            f"the cursor at {cursor} read an empty page, and {NEWEST_PROBE_ATTEMPTS} "
            f"unfiltered probes ($select=:updated_at, $order=:updated_at DESC, "
            f"$limit=1, no $where) returned no row at all. A feed of this size cannot "
            f"be empty, so \"caught up\" cannot be confirmed — and it will not be "
            f"assumed. Aborting without writing a watermark; re-run once the endpoint "
            f"answers an unordered, unfiltered read."
        )
    if newest > cursor:
        raise EtlError(
            f"the cursor at {cursor} read an empty page, but an unfiltered probe "
            f"($select=:updated_at, $order=:updated_at DESC, $limit=1, no $where) "
            f"shows the feed holds rows updated at {newest}, which is newer. A server "
            f"answering correctly would have returned them, so the empty page "
            f"contradicts the feed's own data: this is a failed read, not the end of "
            f"the data. On 2026-09-18 exactly this cost a run ~24,000 rows, which it "
            f"reported as success. Aborting without writing a watermark for rows that "
            f"were never read; re-run once :updated_at filters answer correctly."
        )
    LOG.info("empty page confirmed: the feed's newest :updated_at is %s, not past the "
             "cursor at %s — genuinely caught up", newest, cursor)


def log_upstream_newest(cursor: datetime, *, app_token: str | None, timeout: int) -> datetime | None:
    """Log how far behind the feed the run is starting, once per run.

    One unfiltered probe, logged at INFO, so "how far behind is the feed?" can be
    read straight out of cron mail instead of reconstructed with curl. This is
    reporting, not a check: a probe that fails must not fail a load that is
    otherwise healthy, so transport trouble is logged and swallowed here. The
    same probe is used for correctness in confirm_caught_up, where it is
    emphatically not swallowed.
    """
    try:
        newest = fetch_newest_updated_at(app_token=app_token, timeout=timeout)
    except (*TRANSIENT_ERRORS, EtlError) as exc:
        LOG.warning("could not read the feed's newest :updated_at (%s: %s)",
                    type(exc).__name__, exc)
        return None
    if newest is None:
        LOG.warning("the feed reported no newest :updated_at at all")
        return None
    if newest >= cursor:
        LOG.info("feed newest :updated_at %s (one unfiltered probe); reading from %s, "
                 "so %s of changes to cover", newest, cursor, newest - cursor)
    else:
        LOG.info("feed newest :updated_at %s (one unfiltered probe); reading from %s, "
                 "which is %s ahead of it — a lagging replica or a quiet feed, and one "
                 "probe cannot tell them apart", newest, cursor, cursor - newest)
    return newest


def iter_pages(
    since: datetime,
    *,
    page_size: int,
    app_token: str | None,
    timeout: int,
    max_pages: int | None,
) -> Iterator[list[dict[str, Any]]]:
    """Yield pages of changed rows, oldest change first.

    Paging is a forward keyset cursor on :updated_at — never $offset. After a
    page, the cursor becomes that page's largest :updated_at and the next page
    asks for that value or greater. An edit only ever moves a row *forward* in
    this ordering, so a row modified mid-run cannot slip behind the cursor and
    vanish the way it did under $offset. It also keeps every request as cheap as
    the first; the offset version reached 326 s per page by offset 1,000,000.

    The cursor is inclusive (>=) rather than strict, because several rows can
    share the boundary millisecond and a strict > would drop the ones that fell
    past the page edge. The boundary rows therefore come back at the head of the
    next page and are filtered out by Unique_Key, so nothing is yielded twice.
    Progress is checked rather than assumed. If a full page's newest row is the
    cursor itself, the tie group at that timestamp is wider than a page and the
    cursor can never step over it; that group is handed to drain_tie_group,
    which walks it by created_date, and the cursor then steps strictly past.
    Stepping past is what makes the drain load-bearing, so the caller tells the
    drain how many tied rows it just saw and the drain refuses to report success
    on fewer — a drain that comes back near-empty raises rather than letting the
    watermark move over rows nothing ever read.

    An empty page never ends the run on its own. It is the only "nothing left"
    signal there is, and on 2026-09-18 the feed started producing it while
    holding rows the cursor had not seen, so every zero-row page is put to
    confirm_caught_up first: an unfiltered probe for the feed's newest
    :updated_at, which raises if it is past the cursor.

    max_pages counts requests made by this cursor, not rows or batches; a drain
    yields its own batches without consuming the allowance, and neither does the
    empty-page confirmation.
    """
    page_number = 0
    cursor = since
    inclusive = False        # the first request must exclude the watermark itself
    boundary_keys: set[str] = set()
    while True:
        page_number += 1
        if max_pages is not None and max_pages < page_number:
            LOG.info("stopping at max-pages=%d", max_pages)
            return
        comparison = ">=" if inclusive else ">"
        params = {
            "$select": SELECT_ALL_FIELDS,
            "$where": f":updated_at {comparison} '{socrata_stamp(cursor)}'",
            "$order": ":updated_at",
            "$limit": str(page_size),
        }
        page = fetch_page(params, app_token, timeout)
        fresh = [record for record in page if str(record.get("unique_key")) not in boundary_keys]
        LOG.info("page %d: %d rows, %d new (:updated_at %s %s)",
                 page_number, len(page), len(fresh), comparison, socrata_stamp(cursor))
        if not page:
            # Never a plain "caught up": see confirm_caught_up. This raises
            # rather than returns when the feed's own newest row is past the
            # cursor, which is what an empty page looked like on 2026-09-18.
            confirm_caught_up(cursor, app_token=app_token, timeout=timeout)
            return
        if len(page) < page_size:
            if fresh:
                yield fresh
            return

        top = newest_change(page)
        if top == cursor:
            # A full page whose newest row is the cursor itself: the tie group
            # at this timestamp is at least a page wide, so the cursor can never
            # step over it. This is the normal daily case, not an edge case:
            # NYC's nightly refresh re-stamps its whole batch with one identical
            # :updated_at — 550,168 rows on 2026-09-06, 16,046 on 2026-09-05 —
            # so the daily run drains a group every time it passes one.
            #
            # Nor can this be detected by "the page held nothing new". The API's
            # row order *within* a tie group is unstable: paging the 2026-09-05
            # group at 5,000 rows/page returned a different slice every request,
            # 1,183 of them new each time, for ever. The cursor failing to move
            # is the fact to test on. The group is drained by created_date
            # instead — immutable, therefore splittable — after which the cursor
            # steps strictly past it.
            #
            # Count the tied rows rather than passing page_size: this is the
            # evidence the drain is checked against, so it has to be what was
            # actually observed. (Every row on this page must carry the cursor's
            # stamp — the request asked for >= cursor and the maximum came back
            # equal to it — but the drain's guard is only as trustworthy as the
            # number behind it, so it is counted, not inferred.)
            tied = sum(1 for record in page
                       if parse_timestamp(record.get(":updated_at"), ":updated_at") == cursor)
            yield from drain_tie_group(cursor, set(boundary_keys), page_size=page_size,
                                       app_token=app_token, timeout=timeout,
                                       expected_at_least=tied)
            inclusive = False
            boundary_keys = set()
            continue

        if fresh:
            yield fresh
        cursor = top
        inclusive = True
        boundary_keys = {str(record.get("unique_key")) for record in page
                         if parse_timestamp(record.get(":updated_at"), ":updated_at") == cursor}


def iter_window_batches(
    start: datetime,
    end: datetime,
    *,
    page_size: int,
    app_token: str | None,
    timeout: int,
    extra_where: str | None = None,
) -> Iterator[tuple[datetime, datetime, list[dict[str, Any]]]]:
    """Yield (window_start, window_end, rows) covering created_date in [start, end).

    One query per window and no cursor of any kind, which is what makes this
    correct while the source is being edited: created_date is immutable, so a
    window's membership is fixed no matter how many rows are updated mid-sweep.

    Completeness rests on a window fitting in a single page. That is asserted,
    not assumed: a window that comes back exactly page-full may have been
    truncated, so it is halved and both halves re-queried. At ~11k created rows
    per day against a 50k page, splitting should never happen — but the sweep
    stays correct if NYC's volume changes, and refuses to guess if a window
    reaches one second and still overflows.

    No $order is sent. Ordering is irrelevant to a single unpaged query, and
    $order=unique_key (a text column the API cannot index-serve) times out
    outright; $order=created_date triples the response time for nothing.
    """
    pending: deque[tuple[datetime, datetime]] = deque([(start, end)])
    while pending:
        lo, hi = pending.popleft()
        clauses = [f"created_date >= '{socrata_stamp(lo)}'",
                   f"created_date < '{socrata_stamp(hi)}'"]
        if extra_where:
            clauses.append(extra_where)
        params = {
            "$select": SELECT_ALL_FIELDS,
            "$where": " AND ".join(clauses),
            "$limit": str(page_size),
        }
        rows = fetch_page(params, app_token, timeout)
        if len(rows) < page_size:
            LOG.info("window %s .. %s: %d rows", lo, hi, len(rows))
            if rows:
                yield lo, hi, rows
            continue

        span = hi - lo
        if span <= MIN_SWEEP_WINDOW:
            raise EtlError(
                f"window {lo} .. {hi} spans {span} and still fills a {page_size}-row page; "
                f"cannot split further without risking silent truncation"
            )
        mid = lo + span / 2
        LOG.warning("window %s .. %s filled the page (%d rows); splitting at %s",
                    lo, hi, len(rows), mid)
        pending.appendleft((mid, hi))
        pending.appendleft((lo, mid))


def drain_shortfall_tolerance(page_size: int) -> int:
    """How far short of the proven size a drain may legitimately come back.

    See DRAIN_SHORTFALL_FRACTION: the allowance exists for rows edited mid-drain
    and nothing else, so it is sized against churn, not against the group.
    """
    return max(DRAIN_SHORTFALL_FLOOR, int(page_size * DRAIN_SHORTFALL_FRACTION))


def drain_tie_group(
    moment: datetime,
    already_seen: set[str],
    *,
    page_size: int,
    app_token: str | None,
    timeout: int,
    expected_at_least: int,
) -> Iterator[list[dict[str, Any]]]:
    """Yield every row stamped exactly `moment`, in pages, minus those seen.

    Reached when a tie group is too large to page past with the cursor, which
    for this feed is an everyday occurrence rather than an emergency: the
    nightly refresh puts hundreds of thousands of rows on one timestamp.
    The group is pinned with `:updated_at = moment` and then walked by
    created_date windows — the same immutable-key trick the sweep uses — so no
    $offset is needed here either.

    `expected_at_least` is what the caller already established before calling:
    it saw a full page in which that many rows carried this exact stamp, so the
    group cannot be smaller than that. The drain is checked against it rather
    than trusted, because the cursor steps *strictly past* this stamp the
    moment this generator finishes, and the watermark goes with it.

    The group can only shrink while we read it (an edit moves a row to a later
    stamp, where the cursor will meet it again), never gain members — but it can
    only shrink by the handful of rows edited during the drain itself. A small
    shortfall is therefore a note; a large one is not a shrunken group, it is a
    read that failed silently, and it raises EtlError so the run exits non-zero
    without the cursor ever stepping past this stamp. The stored watermark is
    not below the stamp when that happens — write_watermark runs per committed
    batch, so the batch that ended on this stamp already banked it (verified
    2026-09-18: etl_watermark.last_updated_at was 2026-09-19 01:33:25.618, the
    very group the drain refused). It is still safe, because the next run
    re-reads from the watermark minus --overlap-hours and meets the group again;
    the safety comes from that overlap, not from the watermark staying put. On
    2026-09-15 a drain of a
    551,857-row group read back 0 rows, was believed, and cost 8,213 rows that
    no later cursor run could see.
    """
    LOG.warning("tie group at %s exceeds one page (%d row(s) of it already seen at "
                "that stamp); draining it by created_date", moment, expected_at_least)
    pinned = f":updated_at = '{socrata_stamp(moment)}'"
    tolerance = drain_shortfall_tolerance(page_size)
    floor = expected_at_least - tolerance
    observed = 0
    collected = 0

    for attempt in range(1, DRAIN_EMPTY_ATTEMPTS + 1):
        observed = 0
        collected = 0
        ceiling = datetime.now() + CREATED_CEILING_SLACK
        for _lo, _hi, rows in iter_window_batches(
            CREATED_FLOOR, ceiling,
            page_size=page_size, app_token=app_token, timeout=timeout, extra_where=pinned,
        ):
            observed += len(rows)
            fresh = [record for record in rows if str(record.get("unique_key")) not in already_seen]
            already_seen.update(str(record.get("unique_key")) for record in rows)
            collected += len(fresh)
            if fresh:
                yield fresh
        if observed or attempt == DRAIN_EMPTY_ATTEMPTS:
            break
        # Nothing was read, therefore nothing was yielded and `already_seen` is
        # untouched: starting over is a clean repeat, not a resumption.
        delay = DRAIN_RETRY_DELAY * 2 ** (attempt - 1)
        LOG.warning("tie group at %s read back empty, but a full page proved it holds at "
                    "least %d row(s); retry %d/%d in %.0f s",
                    moment, expected_at_least, attempt, DRAIN_EMPTY_ATTEMPTS, delay)
        time.sleep(delay)

    if observed < floor:
        raise EtlError(
            f"tie group at {moment} drained only {observed} row(s), but the cursor page "
            f"proved it holds at least {expected_at_least} (tolerance {tolerance}). "
            f"The group cannot have shrunk that far, so the read failed. Aborting "
            f"without stepping the cursor past {moment}, and without banking anything "
            f"at or beyond that stamp. Note the stored watermark is NOT below "
            f"{moment}: the batch that ended on this stamp committed its watermark "
            f"before the drain began, so the watermark sits at it. What recovers this "
            f"group is the next run's --overlap-hours re-read, not the watermark. "
            f"Re-run once the API serves :updated_at = '{socrata_stamp(moment)}' "
            f"completely."
        )
    if observed < expected_at_least:
        LOG.warning("tie group at %s drained %d row(s), %d short of the %d proven — within "
                    "the %d-row tolerance, consistent with rows edited mid-drain",
                    moment, observed, expected_at_least - observed, expected_at_least, tolerance)
    LOG.info("tie group at %s drained: %d row(s) at that stamp, %d further row(s) new",
             moment, observed, collected)


def iter_windows(start: datetime, end: datetime, window: timedelta) -> Iterator[tuple[datetime, datetime]]:
    """Yield consecutive [lo, hi) windows covering [start, end), oldest first."""
    if window <= timedelta(0):
        raise EtlError(f"sweep window must be positive, got {window}")
    lo = start
    while lo < end:
        hi = min(lo + window, end)
        yield lo, hi
        lo = hi


# --------------------------------------------------------------------------
# transformation
# --------------------------------------------------------------------------

def parse_timestamp(value: Any, field: str) -> datetime | None:
    """Parse a Socrata ISO 8601 timestamp.

    Unlike the bulk CSV loader this faces ISO input, but the rule is the same:
    a value that is present and unparseable raises. It never becomes None.
    """
    if value in (None, ""):
        return None
    text = str(value).rstrip("Z")
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise EtlError(f"{field}: cannot parse timestamp {value!r}") from exc


def to_wkt(location: Any) -> str | None:
    """Convert the feed's GeoJSON point to the WKT text our column stores."""
    if not isinstance(location, dict):
        return None
    coordinates = location.get("coordinates")
    if not coordinates or len(coordinates) != 2:
        return None
    longitude, latitude = coordinates
    return f"POINT ({longitude} {latitude})"


def clip(value: Any, column: str) -> Any:
    limit = VARCHAR_LIMITS.get(column)
    if value is None or limit is None:
        return value
    return str(value)[:limit]


def transform(record: dict[str, Any]) -> dict[str, Any]:
    """Map one API record onto our column names and types."""
    row: dict[str, Any] = {column: None for column in TARGET_COLUMNS}

    for api_field, column in FIELD_MAP.items():
        row[column] = record.get(api_field)

    for column in DATE_COLUMNS:
        row[column] = parse_timestamp(row[column], column)

    for column in INTEGER_COLUMNS:
        row[column] = int(row[column]) if row[column] not in (None, "") else None

    for column in FLOAT_COLUMNS:
        try:
            row[column] = float(row[column]) if row[column] not in (None, "") else None
        except (TypeError, ValueError):
            row[column] = None

    row["Location"] = to_wkt(record.get("location"))

    for column in tuple(row):
        row[column] = clip(row[column], column)

    if row["Unique_Key"] is None:
        raise EtlError(f"record without unique_key: {record!r}")
    return row


def newest_change(page: Sequence[dict[str, Any]]) -> datetime:
    """The largest :updated_at in a page — the candidate new watermark."""
    stamps = [parse_timestamp(r.get(":updated_at"), ":updated_at") for r in page]
    present = [s for s in stamps if s is not None]
    if not present:
        raise EtlError("page contained no usable :updated_at values")
    return max(present)


# --------------------------------------------------------------------------
# database
# --------------------------------------------------------------------------

def connect(option_file: Path, database: str) -> pymysql.connections.Connection:
    if not option_file.is_file():
        raise EtlError(f"no credentials file at {option_file}")
    return pymysql.connect(
        read_default_file=str(option_file),
        database=database,
        charset="utf8mb4",
        autocommit=False,
    )


def ensure_watermark_table(connection) -> None:
    """Create the watermark table if absent.

    Called unconditionally at the start of a run: --since skips reading the
    watermark but still writes one, so creation cannot live in the read path.
    """
    with connection.cursor() as cursor:
        cursor.execute(WATERMARK_DDL)
    connection.commit()


def read_watermark(connection, default: datetime) -> datetime:
    with connection.cursor() as cursor:
        cursor.execute("SELECT last_updated_at FROM etl_watermark WHERE source = %s", (SOURCE_NAME,))
        found = cursor.fetchone()
    connection.commit()
    if found is None:
        LOG.warning("no watermark for %s; starting from %s", SOURCE_NAME, default)
        return default
    return found[0]


def write_watermark(connection, stamp: datetime, rows: int) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO etl_watermark (source, last_updated_at, last_run_at, rows_loaded)
               VALUES (%s, %s, NOW(), %s)
               ON DUPLICATE KEY UPDATE
                   last_updated_at = VALUES(last_updated_at),
                   last_run_at     = VALUES(last_run_at),
                   rows_loaded     = VALUES(rows_loaded)""",
            (SOURCE_NAME, stamp, rows),
        )


def ensure_sweep_table(connection) -> None:
    """Create the sweep progress table if absent."""
    with connection.cursor() as cursor:
        cursor.execute(SWEEP_DDL)
    connection.commit()


def read_completed_windows(connection, start: datetime, end: datetime) -> set[datetime]:
    """Window starts already committed inside [start, end)."""
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT window_start FROM etl_sweep_progress
               WHERE source = %s AND window_start >= %s AND window_start < %s""",
            (SOURCE_NAME, start, end),
        )
        found = {row[0] for row in cursor.fetchall()}
    connection.commit()
    return found


def record_window(connection, lo: datetime, hi: datetime, rows_seen: int) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO etl_sweep_progress (source, window_start, window_end, rows_seen, completed_at)
               VALUES (%s, %s, %s, %s, NOW())
               ON DUPLICATE KEY UPDATE
                   window_end   = VALUES(window_end),
                   rows_seen    = VALUES(rows_seen),
                   completed_at = VALUES(completed_at)""",
            (SOURCE_NAME, lo, hi, rows_seen),
        )


def reconcile_lookups(connection, rows: Sequence[dict[str, Any]]) -> None:
    """Insert unseen lookup values, then stamp each row's foreign keys.

    This must happen before the upsert. The id columns are FK-enforced, so a
    row naming an agency the lookup table has never seen is rejected with
    error 1452 rather than quietly loaded.
    """
    for text_column, table, name_column, id_column in LOOKUPS:
        values = {row[text_column] for row in rows if row[text_column] is not None}
        if not values:
            continue

        # The lookup tables collate utf8mb4_general_ci, so the database matches
        # 'Residential Building' to a stored 'RESIDENTIAL BUILDING'. Python
        # dicts do not. Keying on casefold() keeps our mapping consistent with
        # the collation; keying on the raw string silently yields a NULL id for
        # every value whose stored spelling differs in case.
        with connection.cursor() as cursor:
            placeholders = ", ".join(["%s"] * len(values))
            cursor.execute(
                f"SELECT {name_column}, {id_column} FROM {table} WHERE {name_column} IN ({placeholders})",
                tuple(values),
            )
            known = {name.casefold(): key for name, key in cursor.fetchall()}

            missing = sorted(v for v in values if v.casefold() not in known)
            if missing:
                LOG.warning("%s: %d new value(s): %s", table, len(missing), missing[:5])
                cursor.executemany(
                    f"INSERT IGNORE INTO {table} ({name_column}) VALUES (%s)",
                    [(value,) for value in missing],
                )
                placeholders = ", ".join(["%s"] * len(missing))
                cursor.execute(
                    f"SELECT {name_column}, {id_column} FROM {table} WHERE {name_column} IN ({placeholders})",
                    tuple(missing),
                )
                known.update({name.casefold(): key for name, key in cursor.fetchall()})

        unresolved = 0
        for row in rows:
            value = row[text_column]
            row[id_column] = known.get(value.casefold()) if value is not None else None
            if value is not None and row[id_column] is None:
                unresolved += 1

        if unresolved:
            raise EtlError(
                f"{table}: {unresolved} row(s) have a {text_column} value that resolved to no "
                f"{id_column}. Loading them would write NULL foreign keys silently."
            )


def build_upsert(table: str) -> str:
    columns = ", ".join(f"`{c}`" for c in TARGET_COLUMNS)
    placeholders = ", ".join(["%s"] * len(TARGET_COLUMNS))
    # Unique_Key identifies the row; everything else is refreshed from the feed.
    updates = ", ".join(f"`{c}` = VALUES(`{c}`)" for c in TARGET_COLUMNS if c != "Unique_Key")
    return f"INSERT INTO `{table}` ({columns}) VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {updates}"


def upsert(connection, table: str, rows: Sequence[dict[str, Any]]) -> int:
    payload = [tuple(row[column] for column in TARGET_COLUMNS) for row in rows]
    with connection.cursor() as cursor:
        cursor.executemany(build_upsert(table), payload)
        return cursor.rowcount


def assert_batch_sane(rows: Sequence[dict[str, Any]]) -> None:
    """Refuse to accept a batch whose dates are empty.

    The bulk importer wrote NULL into every date column for months because
    nothing checked. Created_Date is mandatory in the feed; if none of a batch
    has one, something upstream changed and we stop rather than load rubbish.
    """
    if not rows:
        return
    with_created = sum(1 for row in rows if row["Created_Date"] is not None)
    if with_created == 0:
        raise EtlError(f"batch of {len(rows)} rows has no Created_Date at all — refusing to load")
    if with_created < len(rows):
        LOG.warning("%d of %d rows lack Created_Date", len(rows) - with_created, len(rows))


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------

@timed("Run")
def run(args: argparse.Namespace) -> int:
    connection = connect(args.option_file, args.database)
    total = 0
    try:
        ensure_watermark_table(connection)
        if args.since is not None:
            watermark = args.since
            LOG.info("watermark overridden: %s", watermark)
        else:
            stored = read_watermark(connection, args.default_since)
            watermark = stored - timedelta(hours=args.overlap_hours)
            LOG.info("stored watermark %s, re-reading from %s (overlap %d h)",
                     stored, watermark, args.overlap_hours)

        # One unfiltered probe per run, purely so the log says how far behind
        # the feed the run is starting. confirm_caught_up re-asks for itself.
        log_upstream_newest(watermark, app_token=args.app_token, timeout=args.timeout)

        highest = watermark
        for page in iter_pages(
            watermark,
            page_size=args.page_size,
            app_token=args.app_token,
            timeout=args.timeout,
            max_pages=args.max_pages,
        ):
            rows = [transform(record) for record in page]
            assert_batch_sane(rows)
            highest = max(highest, newest_change(page))

            if args.dry_run:
                LOG.info("dry run: would upsert %d rows (newest change %s)", len(rows), highest)
                total += len(rows)
                continue

            reconcile_lookups(connection, rows)
            affected = upsert(connection, args.table, rows)
            write_watermark(connection, highest, len(rows))
            connection.commit()          # watermark and data commit together
            total += len(rows)
            LOG.info("committed %d rows (%d affected), watermark %s", len(rows), affected, highest)

        LOG.info("run complete: %d rows processed, watermark %s", total, highest)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return total


@timed("Sweep")
def run_sweep(args: argparse.Namespace) -> int:
    """Re-read a created_date range window by window and upsert what it holds.

    This is the bulk path: reloading history, or recovering rows an earlier
    offset-paged run skipped. It deliberately does not touch the :updated_at
    watermark. The rows it recovers have :updated_at values *below* the
    watermark — that is precisely why no incremental run would ever fetch them —
    so moving the watermark on their account would mean nothing.
    """
    connection = connect(args.option_file, args.database)
    total = 0
    windows_done = 0
    try:
        ensure_sweep_table(connection)
        already = set() if args.resweep else read_completed_windows(
            connection, args.sweep_from, args.sweep_to)
        if already:
            LOG.info("%d window(s) already committed; use --resweep to redo them", len(already))

        for lo, hi in iter_windows(args.sweep_from, args.sweep_to, timedelta(days=args.window_days)):
            if lo in already:
                LOG.info("window %s .. %s: already committed, skipping", lo, hi)
                continue

            seen = 0
            for _batch_lo, _batch_hi, page in iter_window_batches(
                lo, hi,
                page_size=args.page_size,
                app_token=args.app_token,
                timeout=args.timeout,
            ):
                rows = [transform(record) for record in page]
                assert_batch_sane(rows)
                seen += len(rows)
                if args.dry_run:
                    continue
                reconcile_lookups(connection, rows)
                affected = upsert(connection, args.table, rows)
                LOG.info("  upserted %d rows (%d affected)", len(rows), affected)

            total += seen
            windows_done += 1
            if args.dry_run:
                LOG.info("dry run: window %s .. %s held %d rows", lo, hi, seen)
                continue
            record_window(connection, lo, hi, seen)
            connection.commit()      # window data and its progress row commit together
            LOG.info("window %s .. %s committed (%d rows; %d windows, %d rows this run)",
                     lo, hi, seen, windows_done, total)

        LOG.info("sweep complete: %d window(s), %d rows processed", windows_done, total)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return total


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Incremental NYC 311 loader.")
    parser.add_argument("--table", default=DEFAULT_TABLE)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--option-file", type=Path, default=DEFAULT_OPTION_FILE)
    parser.add_argument("--app-token", default=None,
                        help="Socrata app token; strongly recommended (also read from NYC_APP_TOKEN)")
    parser.add_argument("--since", type=datetime.fromisoformat, default=None,
                        help="override the stored watermark, ISO 8601")
    parser.add_argument("--default-since", type=datetime.fromisoformat,
                        default=datetime(2026, 5, 9, 2, 32, 59),
                        help="watermark to use on the very first run")
    parser.add_argument("--overlap-hours", type=int, default=DEFAULT_OVERLAP_HOURS)
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and transform, but write nothing")

    sweep = parser.add_argument_group(
        "sweep mode",
        "Bulk re-read of a created_date range, window by window, with no offset "
        "and no cursor. Use for backfills and for recovering rows an "
        "offset-paged run skipped; it leaves the :updated_at watermark alone.")
    sweep.add_argument("--sweep-from", type=datetime.fromisoformat, default=None,
                       help="start of the created_date range to sweep, ISO 8601 (inclusive)")
    sweep.add_argument("--sweep-to", type=datetime.fromisoformat, default=None,
                       help="end of the created_date range to sweep, ISO 8601 (exclusive)")
    sweep.add_argument("--window-days", type=float, default=DEFAULT_SWEEP_WINDOW.days,
                       help="width of each sweep window in days (default: 1)")
    sweep.add_argument("--resweep", action="store_true",
                       help="re-do windows already recorded as committed")

    args = parser.parse_args(argv)
    args.sweep = args.sweep_from is not None or args.sweep_to is not None
    if args.sweep:
        if args.sweep_from is None or args.sweep_to is None:
            parser.error("--sweep-from and --sweep-to must be given together")
        if args.sweep_from >= args.sweep_to:
            parser.error(f"--sweep-from ({args.sweep_from}) must precede --sweep-to ({args.sweep_to})")
        if args.window_days <= 0:
            parser.error("--window-days must be positive")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    args = parse_args(argv)
    if args.app_token is None:
        import os
        args.app_token = os.environ.get("NYC_APP_TOKEN")
        if not args.app_token:
            LOG.warning("no app token; requests share a throttled per-IP pool")
    try:
        run_sweep(args) if args.sweep else run(args)
    except EtlError as exc:
        LOG.error("%s", exc)
        return 1
    except Exception as exc:                      # noqa: BLE001 - top level guard
        LOG.exception("unhandled failure: %s", exc)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
