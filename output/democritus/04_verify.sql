-- Post-load verification. Absence of an error is not evidence of success.
SELECT COUNT(*)                              AS rows_total,
       COUNT(DISTINCT Unique_Key)            AS distinct_keys,
       SUM(Created_Date IS NOT NULL)         AS created_ok,
       SUM(Closed_Date IS NOT NULL)          AS closed_ok,
       SUM(TIME(Created_Date) = '00:00:00')  AS midnight_only,
       MIN(Created_Date)                     AS earliest,
       MAX(Created_Date)                     AS latest,
       SUM(Closed_Date < Created_Date)       AS closed_before_created
FROM NYC311;
