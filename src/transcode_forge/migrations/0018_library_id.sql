-- Full review F5: the library display name was a relationship key. jobs,
-- scans and skipped_files matched their library by name, libraries.name is
-- not unique, and a rename touched only the libraries row. So a renamed
-- library's queued S3 jobs lost their bucket at claim time, and two
-- libraries sharing a name shared one scan clock. From here library_id is
-- the key every filter and lookup matches on, and the name column is the
-- name at the time the row was written, kept for display.
-- NULL = no library carried that name when this ran: the row stays
-- visible, just unfiltered, exactly as a missed name lookup left it.
ALTER TABLE jobs ADD COLUMN library_id TEXT;
ALTER TABLE scans ADD COLUMN library_id TEXT;
ALTER TABLE skipped_files ADD COLUMN library_id TEXT;
-- The first library by creation wins a shared name, matching the LIMIT 1
-- name lookup the claim path used until now. A name that moved between
-- libraries over time lands on whichever carries it today; the history
-- has no better witness.
UPDATE jobs SET library_id = (SELECT id FROM libraries l WHERE l.name = jobs.library ORDER BY l.created_at, l.id LIMIT 1) WHERE library_id IS NULL;
UPDATE scans SET library_id = (SELECT id FROM libraries l WHERE l.name = scans.library ORDER BY l.created_at, l.id LIMIT 1) WHERE library_id IS NULL;
UPDATE skipped_files SET library_id = (SELECT id FROM libraries l WHERE l.name = skipped_files.library ORDER BY l.created_at, l.id LIMIT 1) WHERE library_id IS NULL;
-- Strays that predate 0008 hold the id in the name column.
UPDATE jobs SET library_id = library WHERE library_id IS NULL AND library IN (SELECT id FROM libraries);
CREATE INDEX IF NOT EXISTS idx_jobs_library_id ON jobs(library_id);
CREATE INDEX IF NOT EXISTS idx_scans_library_id ON scans(library_id);
CREATE INDEX IF NOT EXISTS idx_skipped_library_id ON skipped_files(library_id);
