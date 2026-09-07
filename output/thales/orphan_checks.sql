-- Foreign-key and lookup integrity checks for nyc311_calls.NYC311.
--
--     db/q < output/thales/orphan_checks.sql
--
-- Every count below must be 0.
--
-- Two distinct failures are checked, because only the first is caught by the
-- database itself:
--
--   orphan_*   an id column pointing at a lookup row that does not exist.
--              The FOREIGN KEY constraints reject these on write, so a non-zero
--              count means a constraint was dropped or bypassed.
--   unmapped_* a row whose text value is present but whose id came out NULL.
--              No constraint catches this — NULL satisfies a foreign key. It is
--              the signature of the collation bug: MariaDB collates these tables
--              utf8mb4_general_ci (case-insensitive) while Python dicts are
--              case-sensitive, so a loader keying its {name: id} map on the raw
--              string silently writes NULL for every value whose stored spelling
--              differs in case ('RESIDENTIAL BUILDING' vs 'Residential Building').

SELECT 'orphan_agency_id' AS check_name, COUNT(*) AS bad_rows
  FROM NYC311 n LEFT JOIN agencies a ON n.agency_id = a.agency_id
 WHERE n.agency_id IS NOT NULL AND a.agency_id IS NULL
UNION ALL
SELECT 'orphan_cb_id', COUNT(*)
  FROM NYC311 n LEFT JOIN community_boards c ON n.cb_id = c.cb_id
 WHERE n.cb_id IS NOT NULL AND c.cb_id IS NULL
UNION ALL
SELECT 'orphan_lt_id', COUNT(*)
  FROM NYC311 n LEFT JOIN location_types l ON n.lt_id = l.lt_id
 WHERE n.lt_id IS NOT NULL AND l.lt_id IS NULL
UNION ALL
SELECT 'orphan_at_id', COUNT(*)
  FROM NYC311 n LEFT JOIN address_types t ON n.at_id = t.at_id
 WHERE n.at_id IS NOT NULL AND t.at_id IS NULL
UNION ALL
SELECT 'unmapped_Agency_Name', COUNT(*)
  FROM NYC311 WHERE Agency_Name IS NOT NULL AND agency_id IS NULL
UNION ALL
SELECT 'unmapped_Community_Board', COUNT(*)
  FROM NYC311 WHERE Community_Board IS NOT NULL AND cb_id IS NULL
UNION ALL
SELECT 'unmapped_Location_Type', COUNT(*)
  FROM NYC311 WHERE Location_Type IS NOT NULL AND lt_id IS NULL
UNION ALL
SELECT 'unmapped_Address_Type', COUNT(*)
  FROM NYC311 WHERE Address_Type IS NOT NULL AND at_id IS NULL
UNION ALL
-- Created_Date is mandatory in the feed. A non-zero count here is the failure
-- assert_batch_sane() exists to prevent: an import that wrote NULL dates.
SELECT 'null_Created_Date', COUNT(*)
  FROM NYC311 WHERE Created_Date IS NULL;
