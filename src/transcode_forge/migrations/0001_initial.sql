-- Initial schema (snapshot of v0.4).
--
-- All tables, indexes, and constraints that exist as of v0.4. Existing
-- installs are detected by the migration runner and have this version
-- recorded as already-applied without re-running it.
--
-- Dialect notes: this file uses SQLite-flavored types (INTEGER, TEXT, REAL).
-- The migration runner upgrades INTEGER → BIGINT on Postgres for columns
-- that hold file sizes, bitrates, or byte counters where 2GB is plausible.

CREATE TABLE IF NOT EXISTS libraries (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    quality_preset INTEGER NOT NULL DEFAULT 21,
    enabled INTEGER NOT NULL DEFAULT 1,
    auto_scan INTEGER NOT NULL DEFAULT 0,
    scan_interval_hours INTEGER DEFAULT 24,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS media_files (
    id TEXT PRIMARY KEY,
    library_id TEXT NOT NULL REFERENCES libraries(id),
    file_path TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    show_name TEXT,
    season INTEGER,
    episode INTEGER,
    video_codec TEXT,
    audio_codec TEXT,
    resolution TEXT,
    width INTEGER,
    height INTEGER,
    bitrate INTEGER,
    duration REAL,
    file_size INTEGER,
    transcode_status TEXT NOT NULL DEFAULT 'pending',
    skip_reason TEXT,
    job_id TEXT,
    file_modified_at TEXT,
    scanned_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    library TEXT NOT NULL,
    source_codec TEXT NOT NULL,
    source_resolution TEXT,
    source_bitrate INTEGER,
    source_duration REAL,
    source_size INTEGER,
    target_codec TEXT NOT NULL DEFAULT 'hevc',
    quality_value INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    worker_id TEXT,
    progress REAL DEFAULT 0,
    output_size INTEGER,
    space_saved INTEGER,
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skipped_files (
    id TEXT PRIMARY KEY,
    file_path TEXT NOT NULL UNIQUE,
    library TEXT NOT NULL,
    codec TEXT NOT NULL,
    resolution TEXT,
    file_size INTEGER,
    skip_reason TEXT NOT NULL,
    scan_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    host TEXT NOT NULL,
    capabilities TEXT NOT NULL,
    ffmpeg_version TEXT,
    max_concurrent INTEGER DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'offline',
    current_job_id TEXT,
    paused INTEGER NOT NULL DEFAULT 0,
    last_heartbeat TEXT,
    registered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scans (
    id TEXT PRIMARY KEY,
    library TEXT NOT NULL,
    files_found INTEGER DEFAULT 0,
    files_new INTEGER DEFAULT 0,
    files_updated INTEGER DEFAULT 0,
    files_skipped INTEGER DEFAULT 0,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS system_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS excluded_paths (
    path TEXT PRIMARY KEY,
    library TEXT,
    reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    start_hour INTEGER NOT NULL,
    end_hour INTEGER NOT NULL,
    days_mask INTEGER NOT NULL DEFAULT 127,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_media_library ON media_files(library_id);
CREATE INDEX IF NOT EXISTS idx_media_codec ON media_files(video_codec);
CREATE INDEX IF NOT EXISTS idx_media_status ON media_files(transcode_status);
CREATE INDEX IF NOT EXISTS idx_media_show ON media_files(show_name);
CREATE INDEX IF NOT EXISTS idx_media_size ON media_files(file_size);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_library ON jobs(library);
CREATE INDEX IF NOT EXISTS idx_jobs_worker ON jobs(worker_id);
CREATE INDEX IF NOT EXISTS idx_skipped_path ON skipped_files(file_path);
CREATE INDEX IF NOT EXISTS idx_skipped_reason ON skipped_files(skip_reason);
CREATE INDEX IF NOT EXISTS idx_workers_status ON workers(status);
CREATE INDEX IF NOT EXISTS idx_scans_library ON scans(library);
CREATE INDEX IF NOT EXISTS idx_excluded_library ON excluded_paths(library);
