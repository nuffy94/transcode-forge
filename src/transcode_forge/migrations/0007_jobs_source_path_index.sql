-- 0007: index jobs.source_path.
-- The file-detail drawer loads a file's full job history by path on every
-- open; without an index that's a table scan per click.
CREATE INDEX IF NOT EXISTS idx_jobs_source_path ON jobs(source_path);
