# Review: lookup table build script

Reviewed 2026-08-26 against the rebuilt `nyc311_calls.NYC311` (21,080,417 rows).

## 1. Blocker — the column already exists

```sql
ALTER TABLE NYC311 ADD COLUMN at_id INT, ADD INDEX idx_at_id (at_id);
```

Fails with **error 1060, duplicate column name 'at_id'**. When the table was
rebuilt on 2026-08-25 the new schema kept all four id columns
(`agency_id`, `cb_id`, `lt_id`, `at_id`), all `int(11) NULL`. They are present
and empty. Same for the other three scripts.

Drop the `ADD COLUMN` clause and keep only the index:

```sql
ALTER TABLE NYC311 ADD INDEX idx_at_id (at_id);
```

## 2. Build the index after the UPDATE, not before

As written, the index exists while 21 M rows are updated, so every row
maintains it. Populating first and indexing afterwards builds the index once,
in a single sorted pass:

```sql
UPDATE ... SET n.at_id = a.at_id;          -- populate
ALTER TABLE NYC311 ADD INDEX idx_at_id (at_id);   -- then index
```

## 3. The verification query cannot detect the failure it is looking for

```sql
select n.Unique_Key, n.Address_Type, a.address_type_name
from NYC311 n join address_types a on n.at_id = a.at_id
where n.Address_Type <> a.address_type_name
```

Two blind spots:

- **It only examines rows that already matched.** The inner join requires
  `at_id` to be set, so a row the UPDATE failed to populate — the actual thing
  that can go wrong — is invisible to it.
- **`<>` is collation-aware.** These columns are `utf8mb4_general_ci`, which is
  case- and accent-insensitive, so `'ADDRESS' <> 'address'` is false. A case
  mismatch cannot be detected this way.

Replace with an orphan count plus a binary comparison:

```sql
SELECT COUNT(*) AS unmatched
FROM NYC311
WHERE Address_Type IS NOT NULL AND at_id IS NULL;      -- expect 0

SELECT COUNT(*) AS byte_mismatch
FROM NYC311 n JOIN address_types a ON n.at_id = a.at_id
WHERE n.Address_Type COLLATE utf8mb4_bin <> a.address_type_name;
```

Note 2,792,278 rows (13%) have a NULL `Address_Type`. Those correctly keep a
NULL `at_id` — the inner join in the UPDATE leaves them alone, which is right.

## 4. One pass instead of four

Each `UPDATE ... JOIN` is a full pass over 21 M rows. Running four of them
means four passes and four sets of undo. One statement with four LEFT JOINs
does it in a single pass:

```sql
UPDATE NYC311 n
  LEFT JOIN agencies         a ON n.Agency_Name     = a.agency_name
  LEFT JOIN community_boards c ON n.Community_Board = c.community_board_name
  LEFT JOIN location_types   l ON n.Location_Type   = l.location_type_name
  LEFT JOIN address_types    t ON n.Address_Type    = t.address_type_name
SET n.agency_id = a.agency_id,
    n.cb_id     = c.cb_id,
    n.lt_id     = l.lt_id,
    n.at_id     = t.at_id;
```

`LEFT JOIN` matters: with inner joins a row missing any one of the four values
would be skipped entirely, leaving the other three ids NULL.

## 5. INT undercuts the point of normalizing

The distinct counts are small: agencies 22, community boards 78, location types
216, address types 7. `INT` is 4 bytes; the values need one or two.

| Column | Distinct | Suggested | Bytes saved/row |
|---|---|---|---|
| `at_id` | 7 | `TINYINT UNSIGNED` | 3 |
| `agency_id` | 22 | `TINYINT UNSIGNED` | 3 |
| `cb_id` | 78 | `TINYINT UNSIGNED` | 3 |
| `lt_id` | 216 | `SMALLINT UNSIGNED` | 2 |

About 11 bytes per row across 21 M rows — roughly **230 MB**. Normalizing to
save space and then storing the keys as `INT` gives most of the saving back.
`SMALLINT UNSIGNED` everywhere is the safer choice if you expect new values;
216 of 255 leaves little headroom in a `TINYINT`.

## 6. Smaller points

- `INSERT ... SELECT DISTINCT` with no `ORDER BY` assigns ids in scan order, so
  a rerun can produce different ids. Add `ORDER BY Address_Type` if you want the
  mapping reproducible.
- No `FOREIGN KEY`. The `UNIQUE` constraint protects the lookup table, but
  nothing stops an orphaned id in `NYC311`. Adding the constraint is what makes
  the normalization enforced rather than merely structural.
- Once FKs exist, `DROP TABLE IF EXISTS address_types` fails while the
  constraint references it. Drop the constraint first, or wrap reruns in
  `SET FOREIGN_KEY_CHECKS = 0`.
- The final `SELECT` is missing its terminating semicolon.
- `VARCHAR(50)` for `address_type_name` against a `VARCHAR(15)` source column is
  harmless, but the widths could match the source (max observed length is 12).
