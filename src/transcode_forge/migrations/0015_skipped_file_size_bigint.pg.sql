-- skipped_files.file_size was int32 on installs that predate the
-- migration runner (stamped v1 without re-running, so the runner's
-- INTEGER→BIGINT adapter never shaped their DDL). Any skip of a file
-- >2 GiB then 500s the report forever — observed 2026-07-20 as an 18k
-- errors/6h storm with BOTH Docker workers held idle by outbox
-- backpressure (attempt 5,176 on one entry). Lossless widen; every
-- other size column is already BIGINT.
-- Postgres-only (.pg.sql): SQLite INTEGER is already 8-byte and cannot
-- parse ALTER COLUMN TYPE.
ALTER TABLE skipped_files ALTER COLUMN file_size TYPE BIGINT;
