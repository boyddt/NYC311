# NYC311

A local MariaDB warehouse of NYC 311 service requests (2020 to present, about
tens of millions of rows), kept current by a nightly incremental loader that is built
to distrust the API it reads from.

It is also a working example of a small team of Claude Code agents, each owning
one specialty, building and operating a real data pipeline.

Source: the [NYC Open Data 311 feed](https://data.cityofnewyork.us/resource/erm2-nwe9)
(Socrata dataset `erm2-nwe9`).

## What is interesting here

The hard part was not loading the data. It was knowing when the load was wrong.
The feed is served by replicas that disagree with each other, and its
`:updated_at` filters sometimes return truncated or flatly contradictory
answers while reporting success. Most of the engineering is a response to that.

- **Silent data loss, found and fixed three times.** Each time the loader
  advanced its watermark past rows it had never read, which made the gap
  permanent. The rule that came out of it: *a watermark must be earned*. It is
  written only from rows actually committed.
- **The nightly tie group.** About 99.9% of each day's updates carry one
  identical millisecond timestamp. Paging inside a group that ties on the sort
  key returns a different overlapping slice on every request. The loader drains
  such a group by immutable `created_date` windows instead of paging it.
- **No `$offset` on large sweeps.** Offset paging over `:updated_at` order lost
  20,566 rows with no error. Bulk work sweeps by `created_date` window instead,
  with progress committed in the same transaction as its rows, so an
  interrupted sweep resumes correctly.
- **Guards that refuse to believe a short read.** A tie-group drain that
  returns far fewer rows than the page already proved it holds aborts the run.
  An empty page is checked against an unfiltered probe that does not rely on
  the broken filter, and a contradiction aborts without writing a watermark.
- **Independent reconciliation.** After every load, `reconcile_counts.py`
  compares per-month row counts against the API. The wrapper fails the night if
  the database is short by more than 1,000 rows, because the loader's own exit
  status was shown not to be enough.
- **Retries and alerting.** A failed night is retried twice, two hours apart,
  under one lock and one log. A night that fails every attempt raises a desktop
  notification and leaves a marker file that the next successful night clears.

The reasoning, measurements and incident write-ups are in [`CLAUDE.md`](CLAUDE.md),
which doubles as the project's operating manual for the agents.

## Layout

| Path | What it is |
|---|---|
| `output/thales/nyc311_etl.py` | The incremental loader: keyset cursor, tie-group drain, sweep mode |
| `output/thales/reconcile_counts.py` | Compares the database against the API |
| `output/thales/test_nyc311_etl.py` | Unit tests; need no network or database |
| `nyc311_daily_update.sh` | Cron wrapper: lock, load, reconcile, retry, notify |
| `output/democritus/` | Schema DDL, index builds, lookup tables with foreign keys, verification SQL |
| `output/plato/` | Design notes and the API-to-schema reference |
| `db/q` | Thin client for ad hoc queries against the local database |
| `.claude/agents/` | The agent definitions |

## The team

Agents are named after Greek philosophers. A request goes to Plato first, which
splits it into units a single specialist can finish and then integrates the
results.

- **Plato** orchestrates and routes.
- **Thales** writes and tests the Python.
- **Democritus** owns the schema, indexes, constraints and server tuning.
- **Aristotle** analyses the data.

Each agent writes its output to its own directory under `output/`.

## Database

MariaDB, InnoDB. One fact table, `NYC311`, with `Unique_Key` as primary key and
lookup tables for agencies, community boards, location types and address types.
Lookup ids are enforced by foreign keys, so an orphaned id is rejected by the
database instead of being found later. Buffer pool tuning took a full-table
aggregate from about 240 s to about 10 s.

## Running it

This is configured for one machine and is not packaged for general use. The
shape of it:

    # credentials live in ~/.my.cnf (mode 600), never in this directory
    db/q "SELECT Borough, COUNT(*) FROM NYC311 GROUP BY Borough"

    # incremental load (reads NYC_APP_TOKEN from the environment)
    python output/thales/nyc311_etl.py

    # bulk re-read of a created_date range
    python output/thales/nyc311_etl.py --sweep-from 2026-01-01 --sweep-to 2026-02-01

    # unit tests
    python -m unittest discover -s output/thales -p 'test_*.py'

The Socrata app token goes in the `X-App-Token` header. Without one, requests
fall to a throttled shared pool and run roughly eight times slower.

## Status

A training and portfolio project, not a production service. The data is real and
the pipeline runs nightly, but some discrepancies are tolerated and reported
instead of chased, and the loader upserts without deleting, so requests NYC
withdraws upstream persist here.
