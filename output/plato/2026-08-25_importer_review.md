# NYC311_Importer.py — why every date column is NULL

Reviewed 2026-08-25. Source: `/home/davidtboyd/PycharmProjects/EAD_venv/NYC311_Importer.py` (351 lines).

## Root cause — confirmed, not inferred

`parse_dates()`, lines 81–91:

```python
df[col] = pd.to_datetime(
    df[col],
    errors='coerce',
    format='%m/%d/%Y'
).dt.date
```

The source file `311_Service_Requests.csv` (14.5 GB) stores dates as:

```
"05/09/2026 02:32:59 AM"
```

The format string `'%m/%d/%Y'` describes only the date part. Every value carries
a time and an AM/PM marker, so **no value matches**. With `errors='coerce'`,
pandas converts each failure to `NaT` instead of raising — silently. `NaT`
becomes `None`, which becomes `NULL`.

That is a 100% failure rate by construction, matching the 21,080,417 NULL rows
in all four date columns exactly.

`errors='coerce'` is why this passed unnoticed: it converts a total parse
failure into a successful-looking run. Without it, the first row would have
raised.

## The fix

```python
df[col] = pd.to_datetime(
    df[col],
    errors='raise',                    # fail loudly on the first bad value
    format='%m/%d/%Y %I:%M:%S %p',     # %I = 12-hour, %p = AM/PM
)
```

- `%I` with `%p`, not `%H`. `%H` is 24-hour and will not parse `02:32:59 AM`.
- **Drop `.dt.date`.** It truncates to midnight and discards the time of day,
  which kills any hour-of-day analysis even once parsing is correct.
- Change the four columns from `date` to `datetime` in the schema to match.
- If some rows legitimately have empty dates (`Closed Date` is blank for open
  requests — visible in row 3 of the source), keep `errors='raise'` and handle
  blanks explicitly with a null mask rather than blanket-coercing.

## Second defect — the batch insert

`nyc311_import.log` ends with:

```
ERROR - Batch insert failed: nan can not be used with MySQL
INFO  - Rows inserted: 0 | Rows failed: 37 | Success rate: 0.00%
```

`clean_data()` does:

```python
df = df.where(pd.notna(df), None)
```

On a float64 column, assigning `None` silently converts it back to `NaN` —
pandas has no null for float dtype other than `NaN`. So the NaN the line is
meant to remove is reintroduced, and the connector rejects it. Convert to
`object` dtype first, or replace at the row-tuple level immediately before
`executemany`.

This is a separate bug from the dates, and it is why the most recent test run
inserted zero of 37 rows.

## Note on the sample files

`311_Sample_50.csv` is unusable as a test fixture: it is UTF-7 encoded
(`Unique+AF8-Key` is `Unique_Key`, `2024+AC0-06` is `2024-06`) and its dates are
ISO `2024-06-09 23:13:17`, not the `MM/DD/YYYY hh:mm:ss AM/PM` of the real
export. Testing against it validates the wrong format. `311_Sample.csv` (693 KB,
UTF-8) has the correct headers and is the better fixture.

## Recommendation

The table must be rewritten to fix the dates, so batch every structural change
into that single pass rather than scanning 13.8 GB repeatedly:

1. Reparse dates correctly; retype the four columns to `datetime`.
2. Add `Unique_Key` as PRIMARY KEY — verified unique across all 21,080,417 rows.
3. Index `Problem`, `Borough`, `Status`, `Created_Date`.
4. Retype `X_Coordinate_State_Plane` / `Y_Coordinate_State_Plane` from `varchar`,
   and `BBL` from `double` to an identifier type.
5. Verify after load: row count plus null rate on every date column. Do not
   trust the absence of an error — that is precisely what failed here.
