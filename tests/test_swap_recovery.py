"""Startup swap-recovery scan (worker/storage/filesystem.py).

Stages the on-disk states a crash can leave behind — the original hidden
as .tf_bak, stale .tf_lock/.tf_tmp leftovers — and verifies the scan
restores originals, clears stale locks, respects fresh locks from other
workers on shared storage, and never clobbers a finished transcode.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from transcode_forge.worker.storage.filesystem import recover_orphaned_backups

WORKER = "worker-self"
OTHER = "worker-other"

ORIGINAL_BYTES = b"ORIGINAL BYTES"
ENCODE_BYTES = b"NEW ENCODE"


def _write_lock(lock: Path, *, worker_id: str, age_hours: float = 0.0) -> None:
    ts = datetime.now(UTC) - timedelta(hours=age_hours)
    lock.write_text(
        json.dumps({"job_id": "job-1", "worker_id": worker_id, "timestamp": ts.isoformat()})
    )


def _stage(
    directory: Path,
    *,
    bak: bool = True,
    original: bool = False,
    lock_owner: str | None = None,
    lock_age: float = 0.0,
    tmp: bool = False,
) -> tuple[Path, Path, Path, Path]:
    """Stage pipeline state files for 'movie.mkv'; returns
    (original, bak, lock, tmp) paths whether or not each was created."""
    original_p = directory / "movie.mkv"
    bak_p = directory / "movie.tf_bak.mkv"
    lock_p = directory / "movie.mkv.tf_lock"
    tmp_p = directory / "movie.tf_tmp.mkv"
    if bak:
        bak_p.write_bytes(ORIGINAL_BYTES)
    if original:
        original_p.write_bytes(ENCODE_BYTES)
    if lock_owner is not None:
        _write_lock(lock_p, worker_id=lock_owner, age_hours=lock_age)
    if tmp:
        tmp_p.write_bytes(b"partial encode")
    return original_p, bak_p, lock_p, tmp_p


class TestBackupRestore:
    def test_power_loss_between_renames_restores_original(self, tmp_path: Path):
        """Crash between the two swap renames: media path missing, backup
        holds the original — the headline recovery case."""
        original, bak, lock, _ = _stage(tmp_path, lock_owner=WORKER)
        stats = recover_orphaned_backups([tmp_path], worker_id=WORKER)
        assert stats["restored"] == 1
        assert original.read_bytes() == ORIGINAL_BYTES
        assert not bak.exists()
        assert not lock.exists()

    def test_unconfirmed_encode_replaced_by_original(self, tmp_path: Path):
        """Crash after the swap but before CONFIRM/CLEANUP (lock still
        held): the file at the media path is unverified — the original
        wins, per the never-lose-a-file discipline."""
        original, bak, lock, _ = _stage(tmp_path, original=True, lock_owner=WORKER)
        stats = recover_orphaned_backups([tmp_path], worker_id=WORKER)
        assert stats["restored"] == 1
        assert original.read_bytes() == ORIGINAL_BYTES
        assert not bak.exists()
        assert not lock.exists()

    def test_finished_transcode_never_clobbered(self, tmp_path: Path):
        """Backup + media file but NO lock: the pipeline finished (UNLOCK
        ran) — this is a completed job whose backup delete failed. The
        confirmed encode must not be overwritten; both files are left for
        the operator."""
        original, bak, _, _ = _stage(tmp_path, original=True)
        stats = recover_orphaned_backups([tmp_path], worker_id=WORKER)
        assert stats["restored"] == 0
        assert stats["needs_attention"] == 1
        assert original.read_bytes() == ENCODE_BYTES
        assert bak.exists()

    def test_fresh_foreign_lock_respected(self, tmp_path: Path):
        """On shared NFS another worker may be mid-swap right now — a fresh
        lock from a different worker means hands off."""
        original, bak, lock, _ = _stage(tmp_path, lock_owner=OTHER)
        stats = recover_orphaned_backups([tmp_path], worker_id=WORKER)
        assert stats["restored"] == 0
        assert stats["skipped_active"] == 1
        assert bak.exists()
        assert lock.exists()
        assert not original.exists()

    def test_ancient_foreign_lock_treated_as_stale(self, tmp_path: Path):
        original, _bak, lock, _ = _stage(tmp_path, lock_owner=OTHER, lock_age=48.0)
        stats = recover_orphaned_backups([tmp_path], worker_id=WORKER)
        assert stats["restored"] == 1
        assert original.read_bytes() == ORIGINAL_BYTES
        assert not lock.exists()

    def test_own_lock_is_always_stale_at_startup(self, tmp_path: Path):
        """A freshly started worker has no pipeline in flight — its own
        locks are stale no matter how recent the timestamp."""
        original, _, lock, _ = _stage(tmp_path, lock_owner=WORKER, lock_age=0.0)
        stats = recover_orphaned_backups([tmp_path], worker_id=WORKER)
        assert stats["restored"] == 1
        assert original.exists()
        assert not lock.exists()

    def test_tmp_partial_cleaned_alongside_restore(self, tmp_path: Path):
        _, _, _, tmp = _stage(tmp_path, lock_owner=WORKER, tmp=True)
        stats = recover_orphaned_backups([tmp_path], worker_id=WORKER)
        assert stats["restored"] == 1
        assert stats["tmp_removed"] == 1
        assert not tmp.exists()

    def test_nested_directories_scanned(self, tmp_path: Path):
        deep = tmp_path / "Movies" / "Some Film (2020)"
        deep.mkdir(parents=True)
        (deep / "film.tf_bak.mkv").write_bytes(ORIGINAL_BYTES)
        _write_lock(deep / "film.mkv.tf_lock", worker_id=WORKER)
        stats = recover_orphaned_backups([tmp_path], worker_id=WORKER)
        assert stats["restored"] == 1
        assert (deep / "film.mkv").read_bytes() == ORIGINAL_BYTES


class TestStaleLockCleanup:
    def test_mid_transcode_crash_leftovers_removed(self, tmp_path: Path):
        """Crash during TRANSCODE leaves lock + tmp (no backup); the lock
        would block every retry at the LOCK step."""
        original, _, lock, tmp = _stage(
            tmp_path, bak=False, original=True, lock_owner=WORKER, tmp=True
        )
        stats = recover_orphaned_backups([tmp_path], worker_id=WORKER)
        assert stats["locks_removed"] == 1
        assert stats["tmp_removed"] == 1
        assert not lock.exists()
        assert not tmp.exists()
        assert original.read_bytes() == ENCODE_BYTES  # source untouched

    def test_fresh_foreign_lock_left_alone(self, tmp_path: Path):
        _, _, lock, _ = _stage(tmp_path, bak=False, original=True, lock_owner=OTHER)
        stats = recover_orphaned_backups([tmp_path], worker_id=WORKER)
        assert stats["locks_removed"] == 0
        assert lock.exists()

    def test_unreadable_lock_is_stale(self, tmp_path: Path):
        lock = tmp_path / "movie.mkv.tf_lock"
        lock.write_text("not json at all")
        stats = recover_orphaned_backups([tmp_path], worker_id=WORKER)
        assert stats["locks_removed"] == 1
        assert not lock.exists()

    def test_nonexistent_root_is_noop(self, tmp_path: Path):
        stats = recover_orphaned_backups([tmp_path / "does-not-exist"], worker_id=WORKER)
        assert all(count == 0 for count in stats.values())


class TestAgentWiring:
    """The scan is wired into HttpWorkerAgent startup, over the worker's
    local media roots (TF_PATH_MAP values + TF_LIBRARY_* paths)."""

    def test_recovery_roots_from_path_map_and_libraries(self, tmp_path: Path):
        from transcode_forge.config import Settings
        from transcode_forge.worker.http_agent import HttpWorkerAgent

        movies = tmp_path / "movies"
        movies.mkdir()
        mapped = tmp_path / "mnt-media"
        mapped.mkdir()
        settings = Settings(
            path_map={"/data/media": str(mapped)},
            library_movies=str(movies),
            library_tv=str(tmp_path / "missing-tv"),  # nonexistent → excluded
            scratch_dir=str(tmp_path / "scratch"),
        )
        agent = HttpWorkerAgent(settings, "http://scheduler", "test-token")
        roots = agent._recovery_roots()
        assert mapped in roots
        assert movies in roots
        assert all(root.is_dir() for root in roots)

    async def test_recover_filesystem_state_runs_scan(self, tmp_path: Path):
        from transcode_forge.config import Settings
        from transcode_forge.worker.http_agent import HttpWorkerAgent

        movies = tmp_path / "movies"
        movies.mkdir()
        (movies / "film.tf_bak.mkv").write_bytes(ORIGINAL_BYTES)
        _write_lock(movies / "film.mkv.tf_lock", worker_id="w-1")
        settings = Settings(
            library_movies=str(movies),
            scratch_dir=str(tmp_path / "scratch"),
        )
        agent = HttpWorkerAgent(settings, "http://scheduler", "test-token")
        agent.worker_id = "w-1"

        await agent._recover_filesystem_state()

        assert (movies / "film.mkv").read_bytes() == ORIGINAL_BYTES
        assert not (movies / "film.tf_bak.mkv").exists()
        assert not (movies / "film.mkv.tf_lock").exists()
