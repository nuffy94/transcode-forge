-- Pipeline phase on the job row (search/encode/verify/gauge/swap) — the
-- worker reports it with progress; the dashboard's station bar renders it.
-- NULL = pre-phase worker or not yet reported (UI falls back to the plain
-- meter row).
ALTER TABLE jobs ADD COLUMN phase TEXT;
