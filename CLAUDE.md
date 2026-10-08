# NYC311

## Request routing

All requests go through **Plato** first. Plato is the orchestrator: it decides
who handles what, dispatches, and integrates the results.

Because delegation in Claude Code is one level deep, the main session runs the
Plato protocol itself rather than handing every request to a `plato` subagent —
only the top-level session can dispatch to the rest of the team.

On every request, before acting:

1. Restate the request in one line.
2. Decompose it into units a single specialist can finish end to end.
3. Check `.claude/agents/` for the current roster and route each unit to the
   member whose specialty matches. Dispatch independent units in parallel.
   Handle it directly when no specialist fits, or when doing the work is faster
   than describing it.
4. Integrate the results — reconcile contradictions, verify claims you can
   cheaply check — and give the user one coherent answer.

Report failures with their actual output. Surface disagreements between
specialists instead of silently picking one.

`.claude/agents/plato.md` holds the same protocol as a delegatable agent, for
when Plato should be invoked explicitly.

## Team naming

Agents are named after Greek philosophers. Current roster:

- **plato** — orchestrator; all requests route through it first
- **thales** — Python specialist
- **aristotle** — NYC 311 data analyst
- **democritus** — SQL/database engineer: schema, indexes, optimization, imports

Name any new agent after a Greek philosopher, and keep the agent's `name:`
field, its filename, and every reference to it in other agents' definitions in
sync when renaming.

## Data

NYC 311 data is local, in MariaDB — database `nyc311_calls`, table `NYC311`
(22,485,781 rows as of 2026-09-16, InnoDB), with lookup tables `agencies`,
`community_boards`, `location_types`, `address_types`. Size reads 16.2 GB
(12.4 data + 3.9 index), but `information_schema` lags a bulk load until
`ANALYZE TABLE` runs.

Query through `db/q` from the project root; it reads credentials from
`~/.my.cnf` (mode 600, deliberately outside this Dropbox-synced folder — never
put a credentials file in this directory).

    db/q "SELECT Borough, COUNT(*) FROM NYC311 GROUP BY Borough"

Reloaded from scratch on 2026-08-25: the table was dropped and rebuilt with
`Unique_Key` as PRIMARY KEY and the four date columns typed `datetime`, then
reimported (21,080,417 rows, 37.7 min). Indexes on `Created_Date`, `Problem`,
`Borough`, `Status`, `Incident_Zip`.

Dates are correct — `Created_Date` 100% populated, time of day preserved.

Coverage is **2020-01-01 to 2026-09-15**, not 2010 onward. The 2010 start you
may see quoted elsewhere is wrong for this dataset; 2020-01-01 is the published
feed's own minimum, verified against the live API.

Backfilled 2026-09-06 from the Socrata API: the table had been complete only to
2026-05-08, and 1.27 M rows were pulled to bring it current. See **Data
currency** below before doing anything time-based.

Lookup tables rebuilt 2026-08-26: `agencies` (22), `community_boards` (78),
`location_types` (224 — grows as the feed introduces new values),
`address_types` (7). All four id columns on `NYC311`
are populated, typed `SMALLINT UNSIGNED`, indexed, and enforced by FOREIGN KEY
constraints — an orphaned id or a delete of an in-use lookup row is now
rejected by the database. Every orphan check returned 0.

Server tuning applied 2026-08-25 in `/etc/mysql/mariadb.conf.d/50-server.cnf`:
buffer pool 128 MB → 20 GB, log file 96 MB → 2 GB, io_capacity 200 → 2000,
io_capacity_max 2000 → 4000. Full-table aggregate went from ~240 s to ~10 s.

## Data currency

**Verify coverage before any temporal analysis.** Twice now, this table has
looked complete while its recent tail was empty — and a tail-off in row counts
reads exactly like a real collapse in complaint volume if you do not check.

Cheap check, always worth running first:

    db/q "SELECT DATE_FORMAT(Created_Date,'%Y-%m') m, COUNT(*) c
          FROM NYC311 WHERE Created_Date >= '2026-01-01' GROUP BY m ORDER BY m"

Healthy months run ~300–345 K rows (roughly 11 K/day). A month reading in the
hundreds or low thousands means missing data, **not** a drop in 311 calls. Stop
and say so rather than reporting the artifact as a finding.

To confirm against the source, compare with the API:

    curl -sS -G https://data.cityofnewyork.us/resource/erm2-nwe9.json \
      --data-urlencode "\$select=date_trunc_ym(created_date) AS month, count(1)" \
      --data-urlencode "\$where=created_date >= '2026-01-01'" \
      --data-urlencode '$group=month' --data-urlencode '$order=month'

## Source data

The canonical source is:

    /home/davidtboyd/Dropbox/Data Science Projects/Datasets/NYC311/311_Service_Requests.csv

14.5 GB, UTF-8, quoted fields, header uses the original NYC Open Data names
(`"Unique Key"`, `"Created Date"`, `"Problem (formerly Complaint Type)"`).
Dates are `MM/DD/YYYY hh:mm:ss AM/PM`, e.g. `05/09/2026 02:32:59 AM`.

**Use this file for any load, reload, or validation.** The other files in that
directory are not substitutes:

- `311_Sample.csv` (693 KB, UTF-8) — acceptable as a quick fixture; correct
  headers and date format.
- `311_Sample_50.csv` (34 KB) — **do not use.** UTF-7 encoded and its dates are
  ISO, not the real format. It has known issues and validates the wrong things.
- `column_schema.csv` — also UTF-7 encoded.

Never load the 14.5 GB file whole into memory. Stream it in chunks, and filter
or aggregate before materializing anything.

## Writing standards

Anyone on the team who writes a document, README, report, data dictionary, code
review or write-up follows `Writing_Standards.md` in the project root.

- Section 1 applies to every document: no em dashes, no fabricated claims,
  verify every figure against its source and date it, the banned-words list
  (technical terms of art exempt), and no credentials, private addresses or
  home-directory paths in tracked files. This repository is public.
- Sections 2 to 6 give the standard for each document type: README, data
  dictionary, code review, design and reference notes, and analysis report. Each
  names the reader, the required contents, the format and location, and the checks
  to run before reporting the file as done.
- Section 7 lists types with no standard yet. Follow section 1 for those and tell
  the Owner if one is needed.
- Where the standards leave a question open, ask the Owner instead of guessing.

Existing files predate these rules and still contain em dashes. Do not rewrite
them in bulk; apply the rules to new text and to any file you are already editing.

## Output convention

Each agent writes the files it produces to its own directory under `output/`:
`output/plato/`, `output/thales/`, `output/aristotle/`, `output/democritus/`.
Agents do not write into each other's directories or into the project root, and
they report outputs by full path.

Create `output/<name>/` alongside any new agent added to the roster.

## Update API

Incremental updates come from the Socrata endpoint for dataset `erm2-nwe9`:
`https://data.cityofnewyork.us/resource/erm2-nwe9.json` (`.csv` also works).
Maximum page size is 50,000.

The app token lives in `~/.nyc311.env` (mode 600, outside this Dropbox-synced
folder) and is sourced by `run_claude_NYC311.sh` at launch, so agents started
that way inherit `NYC_APP_TOKEN`. Source it by hand otherwise. Send it as the
`X-App-Token` header; never put it on a command line or in this directory.
Without it, requests fall to a throttled shared pool — measured at ~60 s per
page versus ~7 s with a token.

Drive incremental loads off the `:updated_at` system field, **not**
`created_date` — most daily churn is modifications to existing requests, which
keep their original creation date. Load with `INSERT ... ON DUPLICATE KEY
UPDATE` on `Unique_Key`, and reconcile the lookup tables *before* the upsert or
the foreign keys will reject rows carrying unseen values.

### The nightly tie group — the key operational fact about this feed

NYC stamps **~99.9% of each day's updates with one identical millisecond**, in a
single batch at 01:33 UTC. Measured by night:

    2026-09-01   12,705      2026-09-04   11,807
    2026-09-02   13,739      2026-09-05   16,046
    2026-09-03   14,504      2026-09-06  550,168   <- 35x outlier

Ordering by a column on which rows tie leaves their order arbitrary **and
unstable between requests**. Paging inside a tie group returns a different,
overlapping slice every time — verified live at 5,000 rows/page, which reported
~1,183 "new" rows on every request, indefinitely.

**A normal night's group is 12–16 K rows and fits inside a single 50,000-row
page**, so the cursor steps over it and `drain_tie_group` never fires. The drain
is a safety valve, not the everyday path — but 2026-09-06's bulk re-stamp of
550,168 rows proves the valve is needed, and it happened to be the day we ran
the backfill. Do not size or schedule this loader on that day's numbers.

Everything else here follows from that.

### Never page a large sweep with `$offset`

`$offset` paging over `:updated_at`-ordered results **silently loses rows**, and
no error is raised. The 2026-09-06 backfill lost 20,566 — 3.7% of that one tie
group — with the shortfall tracking recency (May −2,117 … August −9,161). Deep
offsets are also slow: ~7 s at offset 0, 326 s at offset 1 M.

Sweep a large range by **`created_date` windows** instead:

    nyc311_etl.py --sweep-from 2026-01-01 --sweep-to 2026-09-07 [--window-days 1]

`created_date` is immutable, so window membership cannot change mid-run. One
query per window, no cursor, no offset. A page-full window is halved and
re-queried, so completeness never depends on page size. Progress is recorded in
`etl_sweep_progress`, committed with its window's rows, so an interrupted sweep
resumes correctly; `--resweep` forces windows already recorded. The sweep never
touches the `:updated_at` watermark. Verified run: 248 windows, 2,698,315 rows,
1710 s, no splits.

`etl_sweep_progress` is also the record of **what has ever been independently
verified**. As of 2026-09-16 it holds 257 windows covering 2026-01-01 to
2026-09-15, in two batches: the 09-06 backfill (249 windows, through 2026-09-06)
and the 09-16 repair (8 windows, 09-08 through 09-15). **2026-09-07 has never
been swept** — it falls in the seam between the two, and was checked against the
API by hand instead (10,623 rows, exact). Everything from 09-07 onward rested
solely on the `:updated_at` daily path until the 09-16 repair, which is how the
09-15 loss went unnoticed for a night.

Do not send `$order=unique_key` — it times out against this endpoint (measured
3× 240 s, versus 0.9 s unordered).

### The daily path

Routine churn still runs off the `:updated_at` watermark, now as a forward
keyset cursor (inclusive `>=` with `Unique_Key` de-duplication of boundary rows;
strict `>` drops rows tied on the boundary millisecond). When the cursor lands
inside a tie group it drains that group by `created_date` windows before
stepping past it. **This is the everyday path, not a safety valve** — the daily
run hits a tie group every time.

### A watermark must be earned

Advancing the watermark past rows that were never loaded makes the gap
permanent: those rows keep their old `:updated_at`, so every later run filters
them out and reports success. This has now happened three times — once from a
capped 2,000-row test that wrote a full watermark, once from the offset row loss
above, and once on 2026-09-15 from a silent drain (below).
Write the watermark only from data actually committed, and reconcile row counts
against the API before trusting a load.

#### The silent drain, 2026-09-15

Socrata returned **truncated results for the pinned `:updated_at = '...'`
predicate** — not an error, just short pages. The nightly run under-read twice:
the `2026-09-15 01:33:25.924` group drained 14,689 rows, and the
`2026-09-16 01:33:24.809` group drained **0** when it actually held 551,857.
The loader took both as authoritative, stepped the cursor past with `>`, and
committed a watermark above the group. 8,197 rows — mostly requests created
2026-09-13 — became invisible to the cursor for ever. The whole run processed
126,252 rows against ~578,000 on a normal night.

Caught by `reconcile_counts.py` the same night (`FAIL: DB is short 8213 rows`),
repaired 2026-09-16 by `created_date` sweep, and now guarded:

`drain_tie_group` is only ever called because the caller just saw a full page
whose newest row equals the cursor — so the caller already **proved** the group
is at least that wide. It now passes that count as `expected_at_least`, and a
drain observing fewer than `expected_at_least - max(5, 1% of page_size)` raises
`EtlError` and exits non-zero without stepping the cursor past the group. A
drain observing exactly zero retries 3 times (5 s, 10 s) first, since nothing
was yielded and re-reading is a clean repeat; any non-zero shortfall aborts
immediately, because a partial drain has already updated `already_seen` and
cannot be safely retried. Tolerance is sized against mid-drain churn, not group
size, and kept tight on purpose: a false abort costs one re-run, a false
"complete" costs rows permanently.

**A drain that comes back suspiciously small is data loss in progress, not a
quiet night.** Every healthy drain on this feed returns 495–517 K rows.

Note what actually recovers an aborted group: **the next run's
`--overlap-hours` re-read, not the watermark.** The stored watermark is *not*
below the group — the batch that ended on the tie stamp commits its watermark
before the drain begins, so it sits exactly at it (verified 2026-09-18:
`etl_watermark.last_updated_at` = `2026-09-19 01:33:25.618`, the very group the
run refused to drain). Safe, but for a different reason than it looks.

#### The empty page, 2026-09-18

The same Socrata fault escalated: `$where` and aggregate answers over
`:updated_at` went not just truncated but flatly self-contradictory, measured
minutes apart on one endpoint —

    count where :updated_at > '2026-09-19T00:33:25.618'      0
    count where :updated_at = '2026-09-19T01:33:25.618'      537,194
    rows  where :updated_at = '2026-09-19T01:33:25.618'      []
    max(:updated_at)                                         2026-09-18T02:10:09Z
    $order=:updated_at DESC $limit=3                         rows at 2026-09-19T02:11:45Z

The last line is the trustworthy one: the rows exist, and it is the **filters
and aggregates over `:updated_at` that lie**. A re-run fetched `page 1: 0 rows`,
concluded it was caught up, and exited 0 while the table sat ~24,000 rows short.
`reconcile_counts.py` was again the only thing that noticed.

So an empty page is now **verified, not believed**. `confirm_caught_up` probes
`$select=:updated_at&$order=:updated_at DESC&$limit=1` — deliberately **with no
`$where`**, because filtering on `:updated_at` is the broken capability and
using it to check itself would inherit the same wrong answer. A probe newer than
the cursor aborts the run without writing a watermark.

The probe runs 5 times and the newest answer wins: **the endpoint is served by
replicas that disagree.** Unfiltered probes seconds apart returned
`2026-09-19T02:11:45` or a day-stale `2026-09-18T02:10:09` — measured at 2/10
stale, then 4/8 stale an hour later. A stale answer can only hide a
contradiction, never invent one, so repeating cannot cause a false abort.

Not covered: partial truncation on a **non-empty** page away from a tie group.
Those rows keep `:updated_at` values above the committed watermark, so a later
run can still reach them — unlike the tie-group and empty-page failures, it is
not usually permanent. `reconcile_counts.py` remains the only backstop.

**When `:updated_at` filters are misbehaving, the `created_date` sweep is the
way in.** It touches neither `:updated_at` nor the watermark, so it is purely
additive: it can pull data forward early, but it cannot create a permanent hole.
When the filters recover, the daily path resumes from the intact watermark and
picks up every change since, including updates to arbitrarily old records that
no recent-range sweep would have seen.

### Scheduled daily load

`nyc311_daily_update.sh` in the project root, from the user crontab:

    0 21 * * *  /home/davidtboyd/Dropbox/Agentics/NYC311/nyc311_daily_update.sh

It sources the token (cron does not run the launcher, so without this every
request silently drops to the throttled pool), takes an exclusive `flock` so a
slow night cannot overlap the next trigger, runs the loader, then runs
`reconcile_counts.py` and **fails if the DB is short more than 1,000 rows**.
Failures go to stderr as well as the log, because cron mails a job's output and
not its exit code.

Note `reconcile_counts.py` exits 0 even when rows are missing, so the wrapper
reads the shortfall figure it prints rather than trusting its exit status.

**It retries.** A failed night is tried up to 3 times, 2 h apart (~03:00, 05:00
and 07:00 UTC), because the feed's `:updated_at` faults clear with time — see
"The empty page" above. Retries happen on exit 1 and 2 only, never on 3: that
means another copy holds the lock, so retrying is pointless. One `flock` and one
log file span all attempts, and the feed's newest `:updated_at` is logged per
attempt — that per-attempt probe caught replica disagreement *within a single
night* on its first outing (attempt 1 read `2026-09-19 02:11:45`, attempt 2 read
the day-stale `2026-09-18 02:10:09`, 27 s apart). Override with
`NYC311_MAX_ATTEMPTS` / `NYC311_RETRY_INTERVAL_SECONDS` for testing; cron sets
neither.

**A night that fails every attempt notifies.** The wrapper sends a critical
desktop notification (`notify-send`, with the user bus address set explicitly
because cron has no session) and writes `output/plato/nyc311_daily_FAILED`. The
next successful night removes that marker, so **its existence means the latest
night is unresolved** — check it if you were away from the screen. A
notification failure never changes the exit code. There is no MTA on this
machine, so cron's stderr mail most likely goes nowhere; the notification and
marker are the real signal. Intermediate failures that a retry recovers do not
notify. Since 2026-09-18 about 13 of 31 nights hit a feed fault and 4 failed
outright (09-18, 09-29, 09-30, 10-04), each healed by a later run's overlap
re-read.

Consequences worth knowing:

- **A manual run during a retry window gets exit 3 for hours, not minutes.** If
  a night is failing and you want to intervene at 05:30, run the loader
  directly — not the wrapper, which will just say "already running".
- **A suspend, reboot, or killed session ends the pending retry silently.** The
  process is only sleeping; nothing re-arms it. That night stops at whichever
  attempt it reached. (Cron itself is independent of any Claude session; a
  process launched *from* a session is not.)
- A run starting 21:00 on the last of a month and retrying past midnight logs
  everything to the **old** month's file. That is the "one night, one story"
  behaviour, but do not hunt for it under the new month.

Exit codes: 0 ok, 1 loader failed, 2 rows missing, 3 already running — with
retries, 1 and 2 mean *all* attempts failed. 143/130 if signalled mid-run.
Logs: `output/plato/nyc311_daily_YYYY-MM.log`.

A normal night is 12–16 K rows in a single page. Verified end to end on
2026-09-06 under a cron-like environment (`env -i`), including the lock.

### The loader does not delete

It only upserts, so a request NYC withdraws upstream persists in our table
indefinitely. As of 2026-09-06 that is 8 rows (e.g. `68278287`, `68316007` —
closed DOT requests the API no longer serves). Harmless at this scale, but it
means our count runs slightly *above* the API's, and closing it needs a
reconciliation pass, not a loader change.

Full reference, with verified volumes and the API-to-schema field mapping:
`output/plato/2026-08-26_etl_api_reference.md`. Tests for the loader:
`output/thales/test_nyc311_etl.py` (64, no network or DB required).

### Collation gotcha

MariaDB collates these tables `utf8mb4_general_ci` — case-insensitive. Python
dicts are not. Any code that maps a text value to a lookup id by building a
`{name: id}` dict from a query result will silently produce `NULL` ids whenever
the stored spelling differs in case (`RESIDENTIAL BUILDING` vs `Residential
Building`). Key such dicts on `.casefold()`, and assert that every non-NULL text
value resolved to an id rather than letting a NULL through.

The loader is `output/thales/nyc311_etl.py`. Run it with the venv at
`/home/davidtboyd/PycharmProjects/EAD_venv/.venv/bin/python`.
