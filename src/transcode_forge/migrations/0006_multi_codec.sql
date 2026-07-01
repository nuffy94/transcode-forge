-- Multi-codec (AV1) + VMAF quality gate + settings overrides.
--
-- jobs: quality-goal snapshot (target_vmaf) and encode outcome
--       (resolved_crf, achieved_vmaf, backend_used). Existing rows keep
--       target_codec='hevc' (column default since 0001) and NULL goals —
--       NULL target_vmaf means "no gate", i.e. pre-feature behavior.
-- workers: supported_codecs advertisement; existing workers default to
--       '["hevc"]' so a rolling update never mis-assigns AV1 jobs.
-- derivatives: hardware axis renamed encoder → backend (D2); goal fields
--       (target_codec, target_vmaf) + achieved_vmaf become part of the
--       record. Existing rows are historical (no goal-key backfill).
-- app_settings: DB-backed overrides for the allowlisted tuning settings
--       (repos/settings.py). effective(key) = override if set, else env.

ALTER TABLE jobs ADD COLUMN target_vmaf REAL;
ALTER TABLE jobs ADD COLUMN resolved_crf INTEGER;
ALTER TABLE jobs ADD COLUMN achieved_vmaf REAL;
ALTER TABLE jobs ADD COLUMN backend_used TEXT;

ALTER TABLE workers ADD COLUMN supported_codecs TEXT NOT NULL DEFAULT '["hevc"]';

ALTER TABLE derivatives RENAME COLUMN encoder TO backend;
ALTER TABLE derivatives ADD COLUMN target_codec TEXT NOT NULL DEFAULT 'hevc';
ALTER TABLE derivatives ADD COLUMN target_vmaf REAL;
ALTER TABLE derivatives ADD COLUMN achieved_vmaf REAL;

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_target_codec ON jobs(target_codec);
