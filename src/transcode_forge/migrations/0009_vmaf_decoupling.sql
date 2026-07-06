-- VMAF gate decoupling (plans/vmaf-decoupling-spec.md, locked 2026-07-05).
--
-- The CRF search predicts quality from short samples; the gate measures the
-- full file. Persisting both sides of that prediction on every terminal path
-- (completes AND skips) turns the sample-vs-full-file gap from a guess into
-- a measured, per-class quantity — the data a future adaptive gate needs.
--
-- All columns nullable/additive: old workers never send them and keep
-- working; v0.9.x binaries run unmodified against the migrated schema.
-- (This file is 0009 by deliberate reservation — 0010 shipped first. The
-- runner applies by recorded-version set, not high-water mark, so the
-- out-of-order number is safe.)

ALTER TABLE jobs ADD COLUMN predicted_vmaf_mean REAL;
ALTER TABLE jobs ADD COLUMN predicted_vmaf_perc5 REAL;
ALTER TABLE jobs ADD COLUMN achieved_vmaf_perc5 REAL;
