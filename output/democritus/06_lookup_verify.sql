-- Verification. Each count must be 0: a non-NULL text value with a NULL id
-- means the UPDATE found no lookup match, which is the failure the original
-- inner-join check could not see.
SELECT SUM(Agency_Name     IS NOT NULL AND agency_id IS NULL) AS unmatched_agency,
       SUM(Community_Board IS NOT NULL AND cb_id     IS NULL) AS unmatched_cb,
       SUM(Location_Type   IS NOT NULL AND lt_id     IS NULL) AS unmatched_lt,
       SUM(Address_Type    IS NOT NULL AND at_id     IS NULL) AS unmatched_at
FROM NYC311;

-- Byte-exact comparison. Plain <> uses utf8mb4_general_ci and is blind to
-- case differences; COLLATE utf8mb4_bin is not.
SELECT COUNT(*) AS byte_mismatch
FROM NYC311 n JOIN address_types a ON n.at_id = a.at_id
WHERE n.Address_Type COLLATE utf8mb4_bin <> a.address_type_name;
