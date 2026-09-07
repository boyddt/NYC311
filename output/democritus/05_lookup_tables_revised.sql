-- Rebuild the four NYC311 lookup tables and populate the surrogate keys.
-- Revised 2026-08-26 from the original per-table script. Changes:
--   * No ADD COLUMN — the id columns already exist on the rebuilt table.
--   * Keys sized to their domains (SMALLINT UNSIGNED, not INT): ~230 MB saved.
--   * One UPDATE with four LEFT JOINs instead of four separate passes.
--   * Indexes built after population, not maintained during it.
--   * FOREIGN KEYs added, so the normalization is enforced, not just structural.
--   * ORDER BY on the DISTINCT inserts, so ids are reproducible across reruns.
--
-- Run the steps in order; each is timed separately in the accompanying log.

USE nyc311_calls;

-- ---------------------------------------------------------------------------
-- Step 1: lookup tables. SMALLINT UNSIGNED holds up to 65,535 — ample for
-- domains of 7 to 216 values, and must match the referencing column's type
-- exactly for the foreign keys in step 4.
-- ---------------------------------------------------------------------------

DROP TABLE IF EXISTS agencies;
DROP TABLE IF EXISTS community_boards;
DROP TABLE IF EXISTS location_types;
DROP TABLE IF EXISTS address_types;

CREATE TABLE agencies (
    agency_id   SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT,
    agency_name VARCHAR(50) NOT NULL,
    PRIMARY KEY (agency_id),
    UNIQUE KEY uq_agency_name (agency_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE community_boards (
    cb_id                SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT,
    community_board_name VARCHAR(30) NOT NULL,
    PRIMARY KEY (cb_id),
    UNIQUE KEY uq_community_board_name (community_board_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE location_types (
    lt_id              SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT,
    location_type_name VARCHAR(30) NOT NULL,
    PRIMARY KEY (lt_id),
    UNIQUE KEY uq_location_type_name (location_type_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE address_types (
    at_id             SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT,
    address_type_name VARCHAR(15) NOT NULL,
    PRIMARY KEY (at_id),
    UNIQUE KEY uq_address_type_name (address_type_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- ORDER BY makes id assignment deterministic rather than scan-order dependent.
INSERT INTO agencies (agency_name)
    SELECT DISTINCT Agency_Name FROM NYC311
    WHERE Agency_Name IS NOT NULL ORDER BY Agency_Name;

INSERT INTO community_boards (community_board_name)
    SELECT DISTINCT Community_Board FROM NYC311
    WHERE Community_Board IS NOT NULL ORDER BY Community_Board;

INSERT INTO location_types (location_type_name)
    SELECT DISTINCT Location_Type FROM NYC311
    WHERE Location_Type IS NOT NULL ORDER BY Location_Type;

INSERT INTO address_types (address_type_name)
    SELECT DISTINCT Address_Type FROM NYC311
    WHERE Address_Type IS NOT NULL ORDER BY Address_Type;

-- ---------------------------------------------------------------------------
-- Step 2: narrow the id columns while they are still empty. INT is 4 bytes for
-- values that need one or two; at 21 M rows the four columns waste ~230 MB.
-- Cheaper to do now than after they hold data.
-- ---------------------------------------------------------------------------

ALTER TABLE NYC311
    MODIFY agency_id SMALLINT UNSIGNED NULL,
    MODIFY cb_id     SMALLINT UNSIGNED NULL,
    MODIFY lt_id     SMALLINT UNSIGNED NULL,
    MODIFY at_id     SMALLINT UNSIGNED NULL;

-- ---------------------------------------------------------------------------
-- Step 3: populate all four keys in a single pass over the table.
-- LEFT JOIN, not JOIN: an inner join would skip any row missing one of the
-- four text values and leave its other three ids NULL.
-- ---------------------------------------------------------------------------

UPDATE NYC311 n
    LEFT JOIN agencies         a ON n.Agency_Name     = a.agency_name
    LEFT JOIN community_boards c ON n.Community_Board = c.community_board_name
    LEFT JOIN location_types   l ON n.Location_Type   = l.location_type_name
    LEFT JOIN address_types    t ON n.Address_Type    = t.address_type_name
SET n.agency_id = a.agency_id,
    n.cb_id     = c.cb_id,
    n.lt_id     = l.lt_id,
    n.at_id     = t.at_id;

-- ---------------------------------------------------------------------------
-- Step 4: index and constrain, after population so each index builds once.
-- The foreign keys are what make this enforced normalization: the database
-- will now refuse an orphaned id and refuse to delete a lookup row in use.
-- ---------------------------------------------------------------------------

ALTER TABLE NYC311
    ADD INDEX idx_agency_id (agency_id),
    ADD INDEX idx_cb_id     (cb_id),
    ADD INDEX idx_lt_id     (lt_id),
    ADD INDEX idx_at_id     (at_id),
    ADD CONSTRAINT fk_nyc311_agency FOREIGN KEY (agency_id) REFERENCES agencies(agency_id),
    ADD CONSTRAINT fk_nyc311_cb     FOREIGN KEY (cb_id)     REFERENCES community_boards(cb_id),
    ADD CONSTRAINT fk_nyc311_lt     FOREIGN KEY (lt_id)     REFERENCES location_types(lt_id),
    ADD CONSTRAINT fk_nyc311_at     FOREIGN KEY (at_id)     REFERENCES address_types(at_id);

ANALYZE TABLE NYC311;
