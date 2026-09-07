-- Recreate the four lookup tables and repopulate the id columns in NYC311.
-- All four were dropped along with NYC311 on 2026-08-25; their contents are
-- fully derivable from the loaded data, so nothing is lost.
--
-- COST WARNING: the four UPDATE ... JOIN statements each rewrite all ~21 M
-- rows. Expect this script to run far longer than the import itself. Run it
-- deliberately, not casually.

CREATE TABLE IF NOT EXISTS `agencies` (
  `agency_id` int(11) NOT NULL AUTO_INCREMENT,
  `agency_name` varchar(75) NOT NULL,
  PRIMARY KEY (`agency_id`),
  UNIQUE KEY `agency_name` (`agency_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS `community_boards` (
  `cb_id` int(11) NOT NULL AUTO_INCREMENT,
  `community_board_name` varchar(75) NOT NULL,
  PRIMARY KEY (`cb_id`),
  UNIQUE KEY `community_board_name` (`community_board_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS `location_types` (
  `lt_id` int(11) NOT NULL AUTO_INCREMENT,
  `location_type_name` varchar(50) NOT NULL,
  PRIMARY KEY (`lt_id`),
  UNIQUE KEY `location_type_name` (`location_type_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS `address_types` (
  `at_id` int(11) NOT NULL AUTO_INCREMENT,
  `address_type_name` varchar(50) NOT NULL,
  PRIMARY KEY (`at_id`),
  UNIQUE KEY `address_type_name` (`address_type_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

INSERT IGNORE INTO agencies (agency_name)
  SELECT DISTINCT Agency_Name FROM NYC311 WHERE Agency_Name IS NOT NULL;
INSERT IGNORE INTO community_boards (community_board_name)
  SELECT DISTINCT Community_Board FROM NYC311 WHERE Community_Board IS NOT NULL;
INSERT IGNORE INTO location_types (location_type_name)
  SELECT DISTINCT Location_Type FROM NYC311 WHERE Location_Type IS NOT NULL;
INSERT IGNORE INTO address_types (address_type_name)
  SELECT DISTINCT Address_Type FROM NYC311 WHERE Address_Type IS NOT NULL;

UPDATE NYC311 n JOIN agencies a
    ON n.Agency_Name = a.agency_name           SET n.agency_id = a.agency_id;
UPDATE NYC311 n JOIN community_boards c
    ON n.Community_Board = c.community_board_name SET n.cb_id = c.cb_id;
UPDATE NYC311 n JOIN location_types l
    ON n.Location_Type = l.location_type_name  SET n.lt_id = l.lt_id;
UPDATE NYC311 n JOIN address_types t
    ON n.Address_Type = t.address_type_name    SET n.at_id = t.at_id;

-- ---------------------------------------------------------------------------
-- Referential integrity.
--
-- The original schema had indexes on the id columns but no FOREIGN KEYs, so
-- nothing stopped an orphaned id. Adding them makes the normalization real:
-- the database now refuses a row pointing at a lookup value that does not
-- exist, and refuses to delete a lookup row still in use.
--
-- Run only after the UPDATEs above have populated every id column.
-- ---------------------------------------------------------------------------

ALTER TABLE NYC311
  ADD CONSTRAINT fk_nyc311_agency  FOREIGN KEY (agency_id) REFERENCES agencies(agency_id),
  ADD CONSTRAINT fk_nyc311_cb      FOREIGN KEY (cb_id)     REFERENCES community_boards(cb_id),
  ADD CONSTRAINT fk_nyc311_lt      FOREIGN KEY (lt_id)     REFERENCES location_types(lt_id),
  ADD CONSTRAINT fk_nyc311_at      FOREIGN KEY (at_id)     REFERENCES address_types(at_id);

-- Check for rows whose text value found no lookup match (should be 0 apart
-- from genuinely NULL source values):
-- SELECT SUM(Agency_Name IS NOT NULL AND agency_id IS NULL) AS orphan_agency,
--        SUM(Community_Board IS NOT NULL AND cb_id IS NULL) AS orphan_cb,
--        SUM(Location_Type IS NOT NULL AND lt_id IS NULL)   AS orphan_lt,
--        SUM(Address_Type IS NOT NULL AND at_id IS NULL)    AS orphan_at
-- FROM NYC311;
