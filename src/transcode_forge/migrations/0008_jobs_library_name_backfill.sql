-- 0008: backfill jobs.library from UUID to library name.
-- Jobs queued via /api/media/queue stored the library's UUID while the
-- scanner/seed paths stored its name; every library filter (queue page,
-- Activity, stats group-bys) matches on name, so media-queued jobs were
-- invisible to filtering. The queue endpoint now stores the name; this
-- backfills existing rows. Jobs whose library row no longer exists are
-- left untouched.
UPDATE jobs
SET library = (SELECT name FROM libraries WHERE libraries.id = jobs.library)
WHERE library IN (SELECT id FROM libraries);
