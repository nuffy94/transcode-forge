-- Storage backends: per-library backend configuration + derivatives registry.
--
-- Adds backend selection (filesystem/s3) per library, S3 bucket/prefix config,
-- and a derivatives table for dedup/reuse of transcoded outputs.

ALTER TABLE libraries ADD COLUMN backend TEXT NOT NULL DEFAULT 'filesystem';
ALTER TABLE libraries ADD COLUMN s3_bucket TEXT;
ALTER TABLE libraries ADD COLUMN s3_prefix TEXT;

CREATE TABLE IF NOT EXISTS derivatives (
    id TEXT PRIMARY KEY,
    library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    source_key TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_resolution TEXT,
    source_audio_codec TEXT,
    target_resolution TEXT NOT NULL,
    target_audio_codec TEXT NOT NULL,
    encoder TEXT NOT NULL,
    crf INTEGER NOT NULL,
    preset TEXT NOT NULL,
    derivative_key TEXT NOT NULL UNIQUE,
    output_size INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_derivatives_source
    ON derivatives(source_path, target_resolution, encoder, crf);
