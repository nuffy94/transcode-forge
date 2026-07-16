-- Resolution downscale + same-codec shrink (plans/downscale-shrink-spec.md).
-- jobs.target_height: requested output height (1080/720); NULL = keep source
-- resolution, so every pre-feature job reads unchanged.
-- workers.supports_downscale: advertised at registration; claim filtering
-- keeps downscale jobs away from workers that would silently encode at
-- source resolution (the supported_codecs pattern).
ALTER TABLE jobs ADD COLUMN target_height INTEGER;
ALTER TABLE workers ADD COLUMN supports_downscale INTEGER NOT NULL DEFAULT 0;
