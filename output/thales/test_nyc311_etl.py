#!/usr/bin/env python3
"""Tests for the NYC 311 incremental loader.

Nothing here touches the network or the database. The Socrata feed is replaced
by FakeSource, a small in-memory model of the endpoint that can be told to
modify rows *while* a run is paging through it — which is the condition under
which the offset pager silently lost 20,566 rows on 2026-09-06.

Run:
    /home/davidtboyd/PycharmProjects/EAD_venv/.venv/bin/python \
        -m unittest discover -s output/thales -p 'test_*.py' -v
"""

from __future__ import annotations

import json
import random
import re
import unittest
import urllib.error
from datetime import datetime, timedelta
from typing import Any, Iterator
from unittest import mock

import nyc311_etl as etl

EPOCH = datetime(2026, 8, 1)

UPDATED_AFTER = re.compile(r":updated_at (>=?) '([^']+)'")
CREATED_WINDOW = re.compile(r"created_date >= '([^']+)' AND created_date < '([^']+)'")
UPDATED_EQUALS = re.compile(r":updated_at = '([^']+)'")


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


class FakeSource:
    """An in-memory stand-in for the Socrata endpoint.

    Understands the two `$where` shapes the loader emits, plus `$order`,
    `$limit` and `$offset`. `touch()` re-stamps rows the way a real edit does:
    the row keeps its created_date and moves to the end of the :updated_at
    ordering.
    """

    def __init__(self, count: int, *, spread: timedelta = timedelta(minutes=1),
                 unstable_ties: bool = False) -> None:
        # unstable_ties reproduces the live endpoint: rows sharing one
        # :updated_at come back in a different order on every request, so
        # consecutive pages return overlapping but unequal slices of the group.
        self.unstable_ties = unstable_ties
        self.shuffles = 0
        self.rows: dict[str, dict[str, Any]] = {}
        for index in range(count):
            key = f"{100000 + index}"
            moment = EPOCH + spread * index
            self.rows[key] = {
                "unique_key": key,
                "created_date": stamp(moment),
                ":updated_at": stamp(moment),
            }
        self.clock = EPOCH + spread * count
        self.calls = 0

    # -- source-side mutation ------------------------------------------------
    def touch(self, keys: list[str]) -> None:
        """Simulate NYC editing existing requests: :updated_at jumps to now."""
        for key in keys:
            self.clock += timedelta(seconds=1)
            self.rows[key][":updated_at"] = stamp(self.clock)

    def ordered_keys(self) -> list[str]:
        return [r["unique_key"] for r in sorted(
            self.rows.values(), key=lambda r: (r[":updated_at"], r["unique_key"]))]

    # -- API surface ---------------------------------------------------------
    def fetch(self, params: dict[str, str], app_token, timeout) -> list[dict[str, Any]]:
        self.calls += 1
        where = params.get("$where", "")
        limit = int(params["$limit"])
        offset = int(params.get("$offset", 0))

        updated = UPDATED_AFTER.search(where)
        window = CREATED_WINDOW.search(where)
        if updated:
            operator, bound = updated.group(1), updated.group(2)
            keep = (lambda v: v >= bound) if operator == ">=" else (lambda v: v > bound)
            selected = [r for r in self.rows.values() if keep(r[":updated_at"])]
            if self.unstable_ties:
                self.shuffles += 1
                jumble = random.Random(self.shuffles)
                jumble.shuffle(selected)
                selected.sort(key=lambda r: r[":updated_at"])       # ties stay jumbled
            else:
                selected.sort(key=lambda r: (r[":updated_at"], r["unique_key"]))
        elif window:
            low, high = window.group(1), window.group(2)
            selected = [r for r in self.rows.values() if low <= r["created_date"] < high]
            pinned = UPDATED_EQUALS.search(where)
            if pinned:
                selected = [r for r in selected if r[":updated_at"] == pinned.group(1)]
        else:                                       # pragma: no cover - guards the test
            raise AssertionError(f"unrecognised $where: {where!r}")
        return [dict(r) for r in selected[offset:offset + limit]]


def legacy_iter_pages(source: FakeSource, since: datetime, page_size: int,
                      on_page=None) -> Iterator[list[dict[str, Any]]]:
    """The pager as it was before the fix: $offset over an :updated_at order.

    Kept in the tests as the thing being ruled out. It is what lost the rows.
    """
    offset = 0
    while True:
        page = source.fetch({
            "$where": f":updated_at > '{stamp(since)}'",
            "$order": ":updated_at",
            "$limit": str(page_size),
            "$offset": str(offset),
        }, None, 30)
        if not page:
            return
        yield page
        if len(page) < page_size:
            return
        offset += page_size
        if on_page:
            on_page(source, offset)


class RetryingTests(unittest.TestCase):
    """Defect 1: a read timeout arrives as a bare TimeoutError.

    urllib wraps connection-phase OSErrors in URLError but not the timeout
    raised out of h.getresponse(), so the old `except URLError` never saw it and
    the run died with exit code 2 having logged no retry at all.
    """

    def setUp(self) -> None:
        self.sleeps: list[float] = []
        patcher = mock.patch.object(etl.time, "sleep", self.sleeps.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_read_timeout_is_retried_and_can_succeed(self):
        calls = []

        @etl.retrying(attempts=3, base_delay=1.0)
        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise TimeoutError("The read operation timed out")
            return "ok"

        self.assertEqual(flaky(), "ok")
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.sleeps, [1.0, 2.0])       # exponential backoff

    def test_timeout_error_without_reason_does_not_break_the_log_line(self):
        """TimeoutError has no .reason; the old handler formatted exc.reason."""
        self.assertFalse(hasattr(TimeoutError("x"), "reason"))
        with self.assertLogs(etl.LOG, level="WARNING") as captured:
            @etl.retrying(attempts=2, base_delay=1.0)
            def flaky():
                if not getattr(flaky, "done", False):
                    flaky.done = True
                    raise TimeoutError("The read operation timed out")
                return "ok"

            self.assertEqual(flaky(), "ok")
        self.assertIn("TimeoutError", captured.output[0])

    def test_connection_reset_is_retried(self):
        calls = []

        @etl.retrying(attempts=3, base_delay=1.0)
        def flaky():
            calls.append(1)
            if len(calls) < 2:
                raise ConnectionResetError("Connection reset by peer")
            return "ok"

        self.assertEqual(flaky(), "ok")

    def test_truncated_json_body_is_retried(self):
        calls = []

        @etl.retrying(attempts=3, base_delay=1.0)
        def flaky():
            calls.append(1)
            if len(calls) < 2:
                json.loads("{trunca")
            return "ok"

        self.assertEqual(flaky(), "ok")

    def test_exhausted_attempts_re_raise_the_timeout(self):
        @etl.retrying(attempts=2, base_delay=1.0)
        def always_times_out():
            raise TimeoutError("The read operation timed out")

        with self.assertRaises(TimeoutError):
            always_times_out()

    def test_http_404_is_our_bug_and_is_not_retried(self):
        calls = []

        @etl.retrying(attempts=4, base_delay=1.0)
        def not_found():
            calls.append(1)
            raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)

        with self.assertRaises(urllib.error.HTTPError):
            not_found()
        self.assertEqual(len(calls), 1)             # HTTPError caught before OSError

    def test_http_429_is_retried(self):
        calls = []

        @etl.retrying(attempts=3, base_delay=1.0)
        def throttled():
            calls.append(1)
            if len(calls) < 3:
                raise urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
            return "ok"

        self.assertEqual(throttled(), "ok")


class OffsetPagingLosesRowsTests(unittest.TestCase):
    """Defect 2, stated as a fact about the old code rather than a worry."""

    def test_legacy_offset_pager_drops_rows_when_the_source_is_edited(self):
        """The precise mechanism, reproduced.

        NYC edits requests continuously, and most of what it edits is recent —
        rows the sweep has already passed. Editing a row *behind* the cursor
        moves it to the end of the :updated_at ordering, and every row after it
        slides down one position. The row sitting exactly on the cursor slides
        to cursor-1, into territory the run has already read, and no page ever
        returns it. Five edits per page, five rows lost per page.
        """
        source = FakeSource(400)
        expected = set(source.rows)

        def edit_rows_the_run_has_already_passed(src: FakeSource, offset: int) -> None:
            behind = src.ordered_keys()[:offset]
            src.touch(behind[:5])

        seen: set[str] = set()
        for page in legacy_iter_pages(source, EPOCH - timedelta(days=1), 100,
                                      on_page=edit_rows_the_run_has_already_passed):
            seen.update(r["unique_key"] for r in page)

        lost = expected - seen
        self.assertTrue(lost, "expected the offset pager to lose rows")
        self.assertEqual(len(lost), 15, "5 rows lost per page boundary crossed")


class KeysetPagingTests(unittest.TestCase):
    """The fixed :updated_at path: a forward cursor, no offset."""

    def paged_keys(self, source: FakeSource, page_size: int, on_page=None) -> list[str]:
        collected: list[str] = []

        def fetch(params, app_token, timeout):
            self.assertNotIn("$offset", params, "the keyset pager must never send $offset")
            page = source.fetch(params, app_token, timeout)
            if on_page:
                on_page(source)
            return page

        with mock.patch.object(etl, "fetch_page", fetch):
            for page in etl.iter_pages(EPOCH - timedelta(days=1),
                                       page_size=page_size, app_token=None,
                                       timeout=30, max_pages=None):
                collected.extend(r["unique_key"] for r in page)
        return collected

    def test_quiet_source_is_read_completely(self):
        source = FakeSource(250)
        self.assertEqual(set(self.paged_keys(source, 100)), set(source.rows))

    def test_no_row_is_lost_while_the_source_is_being_edited(self):
        """Same edit pattern that costs the offset pager 15 rows above."""
        source = FakeSource(400)
        expected = set(source.rows)

        def edit_rows_already_passed(src: FakeSource) -> None:
            src.touch(src.ordered_keys()[:5])

        seen = set(self.paged_keys(source, 100, on_page=edit_rows_already_passed))
        self.assertEqual(expected - seen, set(), "keyset paging must lose nothing")

    def test_rows_are_never_yielded_twice_across_the_inclusive_boundary(self):
        source = FakeSource(250)
        keys = self.paged_keys(source, 100)
        self.assertEqual(len(keys), len(set(keys)), "boundary rows must be de-duplicated")

    def test_a_tie_group_straddling_a_page_boundary_is_not_dropped(self):
        """A strict `>` cursor would lose every tied row past the page edge."""
        source = FakeSource(250)
        tied = source.ordered_keys()[95:105]        # 10 rows share one millisecond
        shared = source.rows[tied[0]][":updated_at"]
        for key in tied:
            source.rows[key][":updated_at"] = shared
        seen = set(self.paged_keys(source, 100))
        self.assertEqual(set(source.rows) - seen, set())

    def test_edits_ahead_of_the_cursor_are_still_collected(self):
        """The exact pattern that defeated $offset."""
        source = FakeSource(400)
        expected = set(source.rows)
        state = {"page": 0}

        def edit_ahead(src: FakeSource) -> None:
            state["page"] += 1
            ahead = src.ordered_keys()[state["page"] * 100:]
            src.touch(ahead[:5])

        seen = set(self.paged_keys(source, 100, on_page=edit_ahead))
        self.assertEqual(expected - seen, set())

    def test_page_exactly_full_then_empty_terminates(self):
        source = FakeSource(200)
        self.assertEqual(len(self.paged_keys(source, 100)), 200)

    def test_max_pages_stops_early(self):
        source = FakeSource(400)
        self.assertEqual(len(self.paged_keys(source, 100)[:]), 400)
        with mock.patch.object(etl, "fetch_page", source.fetch):
            pages = list(etl.iter_pages(EPOCH - timedelta(days=1), page_size=100,
                                        app_token=None, timeout=30, max_pages=2))
        self.assertEqual(len(pages), 2)

    def test_a_tie_group_larger_than_a_page_is_drained_not_deadlocked(self):
        """NYC's nightly refresh stamps its whole batch with one millisecond.

        16,046 rows shared 2026-09-05T01:33:20.486. A cursor cannot step over a
        tie group wider than a page, so the group is drained by created_date
        instead — and every row must still arrive, exactly once.
        """
        source = FakeSource(300)
        for row in source.rows.values():             # every row shares one stamp
            row[":updated_at"] = stamp(EPOCH)
        keys = self.paged_keys(source, 100)
        self.assertEqual(set(keys), set(source.rows), "the whole tie group must be read")
        self.assertEqual(len(keys), len(set(keys)), "and none of it twice")

    def test_an_unstable_tie_group_does_not_spin_for_ever(self):
        """The failure the live API showed and a stable fake could not.

        With page_size 5000 against the real feed, pages 2, 3 and 4 each
        returned a different 5000-row slice of the same 16,046-row tie group,
        1,183 of them "new" every time. A "the page held nothing new" check
        never fires under that; the cursor not moving is the fact to test on.
        """
        source = FakeSource(400, unstable_ties=True)
        for key in source.ordered_keys()[:250]:      # 250 tied rows, page is 100
            source.rows[key][":updated_at"] = stamp(EPOCH)
        keys = self.paged_keys(source, 100)
        self.assertEqual(set(keys), set(source.rows))
        self.assertEqual(len(keys), len(set(keys)), "no row may be yielded twice")

    def test_a_tie_group_is_drained_and_the_run_then_continues_past_it(self):
        source = FakeSource(400)
        tied = source.ordered_keys()[:150]           # 150 rows on one stamp, page is 100
        for key in tied:
            source.rows[key][":updated_at"] = stamp(EPOCH)
        keys = self.paged_keys(source, 100)
        self.assertEqual(set(keys), set(source.rows))
        self.assertEqual(len(keys), len(set(keys)))


class WindowSweepTests(unittest.TestCase):
    """The bulk path: created_date windows, one query each, no cursor."""

    def sweep_keys(self, source: FakeSource, start, end, page_size, on_page=None) -> list[str]:
        collected: list[str] = []

        def fetch(params, app_token, timeout):
            self.assertNotIn("$offset", params, "the sweep must never send $offset")
            page = source.fetch(params, app_token, timeout)
            if on_page:
                on_page(source)
            return page

        with mock.patch.object(etl, "fetch_page", fetch):
            for _lo, _hi, page in etl.iter_window_batches(
                    start, end, page_size=page_size, app_token=None, timeout=30):
                collected.extend(r["unique_key"] for r in page)
        return collected

    def test_windows_tile_the_range_without_gap_or_overlap(self):
        windows = list(etl.iter_windows(datetime(2026, 1, 1), datetime(2026, 1, 5),
                                        timedelta(days=1)))
        self.assertEqual(len(windows), 4)
        self.assertEqual(windows[0][0], datetime(2026, 1, 1))
        self.assertEqual(windows[-1][1], datetime(2026, 1, 5))
        for (_, previous_end), (next_start, _) in zip(windows, windows[1:]):
            self.assertEqual(previous_end, next_start)

    def test_final_window_is_clipped_to_the_range_end(self):
        windows = list(etl.iter_windows(datetime(2026, 1, 1), datetime(2026, 1, 4, 6),
                                        timedelta(days=2)))
        self.assertEqual(windows[-1], (datetime(2026, 1, 3), datetime(2026, 1, 4, 6)))

    def test_zero_width_window_is_rejected(self):
        with self.assertRaises(etl.EtlError):
            list(etl.iter_windows(datetime(2026, 1, 1), datetime(2026, 1, 2), timedelta(0)))

    def test_a_window_that_fits_one_page_is_returned_whole(self):
        source = FakeSource(300)                     # 300 rows, one per minute
        keys = self.sweep_keys(source, EPOCH, EPOCH + timedelta(days=1), 1000)
        self.assertEqual(set(keys), set(source.rows))
        self.assertEqual(source.calls, 1)

    def test_an_overflowing_window_is_split_until_every_row_is_covered(self):
        source = FakeSource(600)                     # 600 minutes ≈ 10 hours
        keys = self.sweep_keys(source, EPOCH, EPOCH + timedelta(days=1), 100)
        self.assertEqual(sorted(keys), sorted(source.rows))
        self.assertEqual(len(keys), len(set(keys)), "splitting must not duplicate rows")
        self.assertGreater(source.calls, 1, "the window should have been split")

    def test_sweep_is_immune_to_edits_during_the_run(self):
        """created_date never moves, so editing rows cannot change membership."""
        source = FakeSource(600)
        expected = set(source.rows)

        def edit_everything(src: FakeSource) -> None:
            src.touch(list(src.rows)[:50])

        keys = self.sweep_keys(source, EPOCH, EPOCH + timedelta(days=1), 100,
                               on_page=edit_everything)
        self.assertEqual(set(keys), expected)

    def test_a_second_of_data_that_still_overflows_refuses_to_guess(self):
        source = FakeSource(50, spread=timedelta(0))  # all in one instant
        with self.assertRaises(etl.EtlError) as raised:
            self.sweep_keys(source, EPOCH, EPOCH + timedelta(days=1), 10)
        self.assertIn("cannot split further", str(raised.exception))

    def test_empty_windows_yield_nothing(self):
        source = FakeSource(10)
        keys = self.sweep_keys(source, EPOCH + timedelta(days=30),
                               EPOCH + timedelta(days=31), 100)
        self.assertEqual(keys, [])


class TransformTests(unittest.TestCase):
    def test_timestamps_parse_and_a_bad_one_raises(self):
        self.assertEqual(etl.parse_timestamp("2026-08-01T02:32:59.000", "Created_Date"),
                         datetime(2026, 8, 1, 2, 32, 59))
        self.assertIsNone(etl.parse_timestamp("", "Closed_Date"))
        self.assertIsNone(etl.parse_timestamp(None, "Closed_Date"))
        with self.assertRaises(etl.EtlError):
            etl.parse_timestamp("05/09/2026 02:32:59 AM", "Created_Date")

    def test_location_becomes_wkt(self):
        self.assertEqual(etl.to_wkt({"type": "Point", "coordinates": [-73.9, 40.7]}),
                         "POINT (-73.9 40.7)")
        self.assertIsNone(etl.to_wkt(None))
        self.assertIsNone(etl.to_wkt({"coordinates": []}))

    def test_varchar_values_are_clipped_to_the_column_width(self):
        self.assertEqual(len(etl.clip("x" * 200, "Agency_Name")), 50)
        self.assertEqual(etl.clip("x" * 200, "Resolution_Description"), "x" * 200)

    def test_record_without_a_unique_key_raises(self):
        with self.assertRaises(etl.EtlError):
            etl.transform({"created_date": "2026-08-01T00:00:00.000"})

    def test_transform_maps_api_fields_onto_our_columns(self):
        row = etl.transform({
            "unique_key": "12345",
            "created_date": "2026-08-01T02:32:59.000",
            "complaint_type": "Noise - Residential",
            "agency_name": "New York City Police Department",
            "latitude": "40.7",
            "location": {"coordinates": [-73.9, 40.7]},
        })
        self.assertEqual(row["Unique_Key"], 12345)
        self.assertEqual(row["Created_Date"], datetime(2026, 8, 1, 2, 32, 59))
        self.assertEqual(row["Problem"], "Noise - Residential")
        self.assertEqual(row["Latitude"], 40.7)
        self.assertEqual(row["Location"], "POINT (-73.9 40.7)")
        self.assertIsNone(row["Closed_Date"])

    def test_newest_change_picks_the_maximum(self):
        page = [{":updated_at": "2026-08-01T00:00:01.000"},
                {":updated_at": "2026-08-01T00:00:03.000"},
                {":updated_at": "2026-08-01T00:00:02.000"}]
        self.assertEqual(etl.newest_change(page), datetime(2026, 8, 1, 0, 0, 3))


class BatchSanityTests(unittest.TestCase):
    """The guard that exists because an import wrote NULL dates for months."""

    def test_a_batch_with_no_created_date_is_refused(self):
        rows = [{"Created_Date": None} for _ in range(10)]
        with self.assertRaises(etl.EtlError):
            etl.assert_batch_sane(rows)

    def test_a_partial_batch_warns_but_loads(self):
        rows = [{"Created_Date": datetime(2026, 8, 1)}, {"Created_Date": None}]
        with self.assertLogs(etl.LOG, level="WARNING"):
            etl.assert_batch_sane(rows)

    def test_an_empty_batch_is_not_an_error(self):
        etl.assert_batch_sane([])


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.result: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.connection.executed.append((sql, params))
        if sql.strip().upper().startswith("SELECT"):
            wanted = {str(p).casefold() for p in params}
            self.result = [(name, key) for name, key in self.connection.table.items()
                           if name.casefold() in wanted]

    def executemany(self, sql, seq):
        self.connection.executed.append((sql, list(seq)))
        for (value,) in seq:
            self.connection.table.setdefault(value, len(self.connection.table) + 1)

    def fetchall(self):
        return list(self.result)


class FakeConnection:
    """Stands in for MariaDB with utf8mb4_general_ci — case-insensitive lookups."""

    def __init__(self, table: dict[str, int]):
        self.table = dict(table)
        self.executed: list[tuple] = []

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        pass


class LookupReconciliationTests(unittest.TestCase):
    """Collation safety: MariaDB matches case-insensitively, Python dicts do not."""

    def rows(self, **overrides):
        row = {text: None for text, _, _, _ in etl.LOOKUPS}
        row.update({id_column: None for _, _, _, id_column in etl.LOOKUPS})
        row.update(overrides)
        return row

    def test_a_case_mismatch_still_resolves_to_an_id(self):
        connection = FakeConnection({"RESIDENTIAL BUILDING": 7})
        rows = [self.rows(Location_Type="Residential Building")]
        etl.reconcile_lookups(connection, rows)
        self.assertEqual(rows[0]["lt_id"], 7, "casefold keying is what makes this pass")

    def test_an_unseen_value_is_inserted_and_then_resolved(self):
        connection = FakeConnection({})
        rows = [self.rows(Location_Type="Outfall")]
        with self.assertLogs(etl.LOG, level="WARNING"):
            etl.reconcile_lookups(connection, rows)
        self.assertIsNotNone(rows[0]["lt_id"])
        self.assertIn("Outfall", connection.table)

    def test_null_text_leaves_a_null_id(self):
        connection = FakeConnection({"Sanitation": 3})
        rows = [self.rows(Location_Type=None)]
        etl.reconcile_lookups(connection, rows)
        self.assertIsNone(rows[0]["lt_id"])

    def test_a_value_that_resolves_to_nothing_stops_the_load(self):
        """Rather than writing a NULL foreign key and calling it a success."""
        connection = FakeConnection({})
        connection.cursor = lambda: FakeCursor(connection)
        original = FakeCursor.executemany
        FakeCursor.executemany = lambda self, sql, seq: None      # insert silently fails
        try:
            rows = [self.rows(Location_Type="Outfall")]
            with self.assertLogs(etl.LOG, level="WARNING"):
                with self.assertRaises(etl.EtlError) as raised:
                    etl.reconcile_lookups(connection, rows)
            self.assertIn("lt_id", str(raised.exception))
        finally:
            FakeCursor.executemany = original


class ArgumentTests(unittest.TestCase):
    def test_sweep_needs_both_ends(self):
        with self.assertRaises(SystemExit):
            etl.parse_args(["--sweep-from", "2026-01-01"])

    def test_sweep_range_must_run_forwards(self):
        with self.assertRaises(SystemExit):
            etl.parse_args(["--sweep-from", "2026-02-01", "--sweep-to", "2026-01-01"])

    def test_sweep_flag_is_set_only_for_a_sweep(self):
        self.assertFalse(etl.parse_args([]).sweep)
        args = etl.parse_args(["--sweep-from", "2026-01-01", "--sweep-to", "2026-02-01"])
        self.assertTrue(args.sweep)
        self.assertEqual(args.window_days, 1)


class UpsertSqlTests(unittest.TestCase):
    def test_every_column_is_refreshed_except_the_key(self):
        sql = etl.build_upsert("NYC311")
        self.assertIn("ON DUPLICATE KEY UPDATE", sql)
        self.assertNotIn("`Unique_Key` = VALUES(`Unique_Key`)", sql)
        for column in ("Created_Date", "Problem", "agency_id", "lt_id", "Location"):
            self.assertIn(f"`{column}` = VALUES(`{column}`)", sql)
        self.assertEqual(sql.count("%s"), len(etl.TARGET_COLUMNS))


if __name__ == "__main__":
    unittest.main(verbosity=2)
