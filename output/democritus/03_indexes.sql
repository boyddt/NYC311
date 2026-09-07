-- Secondary indexes, built after the load so each is built once rather than
-- maintained per insert. One ALTER = one pass over the table.
--
-- Unique_Key is already the PRIMARY KEY, created with the table.

ALTER TABLE NYC311
  ADD INDEX idx_created  (Created_Date),
  ADD INDEX idx_problem  (Problem),
  ADD INDEX idx_borough  (Borough),
  ADD INDEX idx_status   (Status),
  ADD INDEX idx_zip      (Incident_Zip);

ANALYZE TABLE NYC311;
