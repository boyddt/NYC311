# NYC 311 update API — ETL reference

Verified live 2026-08-26. Every figure below came from a real request.

## Endpoint

Socrata (SODA 2.1), dataset **`erm2-nwe9`**:

```
https://data.cityofnewyork.us/resource/erm2-nwe9.json    # JSON
https://data.cityofnewyork.us/resource/erm2-nwe9.csv     # CSV, same query params
```

The `.csv` form takes identical query parameters and is cheaper to parse for
bulk pulls — pandas reads it directly.

**Page size: 50,000 rows per request, confirmed.** A 50,000-row single-column
page returned in 0.69 s. Page with `$limit` + `$offset`, and always pair paging
with `$order` (`unique_key` works) — without an explicit order, paging can skip
or repeat rows across requests.

**Register an app token.** Sign in at data.cityofnewyork.us → profile →
Developer Settings → create an app token, then send it as the `X-App-Token`
header. Unauthenticated requests share a throttled per-IP pool; a token gives
you a much higher, per-application limit. Store it in `~/.my.cnf`-style config
outside this Dropbox folder, not in the script.

## Current state of the published dataset

| | |
|---|---|
| Rows published | 22,262,351 |
| Rows in our DB | 21,080,417 |
| `created_date` range | 2020-01-01 → 2026-08-25 |
| `:updated_at` range | 2025-12-26 → 2026-08-26 |
| New rows since our max (2026-05-09) | 1,181,282 |
| Rows with `:updated_at` in last 7 days | 627,262 |

**The 2020-01-01 floor is the dataset's own coverage, not an artifact of our
export.** The published dataset starts there too. This corrects the earlier
guess that our CSV had been filtered — it had not.

## The key decision: what drives "incremental"

Two candidate watermarks, and the choice matters.

**`created_date` — wrong on its own.** It captures new requests but misses every
*change* to an existing one: a request closes, its status moves to Closed, a
resolution description is written. Those rows keep their original
`created_date`. There is also evidence of late arrivals: the published row count
exceeds ours by 1,181,934 while only 1,181,282 rows have a newer
`created_date` — a gap of ~652 rows that were added with older creation dates
and would be missed forever by a `created_date` watermark.

**`:updated_at` — the right one.** A Socrata system field, exposed via
`$select=:id,:updated_at` and filterable in `$where`. It changes whenever the
row changes, so it captures inserts and updates alike.

```
?$where=:updated_at > '2026-08-19T00:00:00'&$order=unique_key&$limit=50000&$offset=0
```

One caveat: `min(:updated_at)` is 2025-12-26 across the whole dataset, meaning
the publisher rewrote every row that day. `:updated_at` is therefore useless for
reconstructing history before that date, but reliable going forward.

## Volume, and what it implies for scheduling

627,262 rows changed in the last 7 days — roughly **90,000 rows/day**, against
only ~78,000 *new* rows per week. Most of the churn is modifications to existing
records, which is the strongest argument for `:updated_at` over `created_date`.

- **Daily**: ~90 k rows ≈ 2 requests of 50 k. Trivial.
- **Weekly**: ~630 k rows ≈ 13 requests. Also trivial.

Daily is the better default: smaller batches, faster recovery from a failed run,
and the watermark never drifts far. Overlap each run by an hour
(`:updated_at > last_success - 1 hour`) so a run that dies mid-page loses
nothing — the upsert makes reprocessing harmless.

## Loading: upsert, not insert

`Unique_Key` is our PRIMARY KEY and the natural key in the feed, so:

```sql
INSERT INTO NYC311 (...) VALUES (...)
ON DUPLICATE KEY UPDATE
    Closed_Date = VALUES(Closed_Date),
    Status      = VALUES(Status),
    Resolution_Description = VALUES(Resolution_Description),
    ...
```

A plain `INSERT` will fail on the ~87% of a daily batch that is updates to rows
we already hold.

**The foreign keys constrain the load.** `agency_id`/`cb_id`/`lt_id`/`at_id` are
FK-enforced, so any incoming row carrying a *new* agency, community board,
location type, or address type must have that value inserted into the lookup
table **before** the row itself, or the insert is rejected with error 1452. The
ETL needs a lookup-reconciliation step ahead of the upsert. This is a feature —
it is exactly the integrity the constraints were added for — but it must be
designed in rather than discovered at 3 a.m.

## Field mapping — the API does not match our column names

| API field | Our column |
|---|---|
| `complaint_type` | `Problem` |
| `descriptor` | `Problem_Detail` |
| `council_district` | `Council_Dicharict` *(our typo, preserved)* |
| `created_date` | `Created_Date` |
| `location` | `Location` |

The rest map by uppercasing and underscoring. Two format differences to handle:

- **Dates are ISO 8601** in the API (`2026-08-25T02:06:15.000`), not the
  `MM/DD/YYYY hh:mm:ss AM/PM` of the bulk CSV. The importer's `--date-format`
  flag exists for exactly this; pass `%Y-%m-%dT%H:%M:%S.%f`.
- **`location` is a GeoJSON object** (`{"type":"Point","coordinates":[...]}`),
  where our column holds WKT text (`POINT (-73.85 40.83)`). Convert, or derive
  it from `latitude`/`longitude`.
- The feed also carries `:@computed_region_*` columns. Ignore them.

## Suggested shape

1. Read watermark: `SELECT MAX(api_updated_at) FROM etl_watermark` — track it in
   its own small table, not by scanning `NYC311`.
2. Page `:updated_at > watermark - 1 hour` ordered by `unique_key`, 50 k at a
   time.
3. Reconcile lookup tables: insert any unseen agency / board / location type /
   address type.
4. Upsert the batch with `ON DUPLICATE KEY UPDATE`.
5. Record the new watermark **only after** the batch commits.
6. Verify: rows affected, and null rate on the date columns. The original import
   failed silently for months — every run should assert, not assume.
