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
(21,080,417 rows, 13.8 GB, InnoDB), with lookup tables `agencies`,
`community_boards`, `location_types`, `address_types`.

Query through `db/q` from the project root; it reads credentials from
`~/.my.cnf` (mode 600, deliberately outside this Dropbox-synced folder — never
put a credentials file in this directory).

    db/q "SELECT Borough, COUNT(*) FROM NYC311 GROUP BY Borough"

Known issues, both unresolved:

1. **All four date columns are 100% NULL** (`Created_Date`, `Closed_Date`,
   `Due_Date`, `Resolution_Action_Updated_Date`). No temporal analysis is
   possible until the data is reimported with correct date parsing.
2. **No primary key and no index** on `Unique_Key`, `Problem`, `Borough`, or
   `Status`, so aggregates on those full-scan 13.8 GB — ~10 s warm since the
   buffer pool was raised to 20 GB (2026-08-25), but still a full scan.

Server tuning applied 2026-08-25 in `/etc/mysql/mariadb.conf.d/50-server.cnf`:
buffer pool 128 MB → 20 GB, log file 96 MB → 2 GB, io_capacity 200 → 2000,
io_capacity_max 2000 → 4000. Full-table aggregate went from ~240 s to ~10 s.

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

## Output convention

Each agent writes the files it produces to its own directory under `output/`:
`output/plato/`, `output/thales/`, `output/aristotle/`, `output/democritus/`.
Agents do not write into each other's directories or into the project root, and
they report outputs by full path.

Create `output/<name>/` alongside any new agent added to the roster.
