"""Startup swap-recovery scan (worker/storage/filesystem.py).

Stages the on-disk states a crash can leave behind — the original hidden
as .tf_bak, stale .tf_lock/.tf_tmp leftovers — and verifies the scan
restores originals, clears stale locks, respects fresh locks from other
workers on shared storage, and never clobbers a finished transcode.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from transcode_forge.worker import pipeline
from transcode_forge.worker.storage.filesystem import (
    LOCK_TOUCH_INTERVAL,
    RECOVERY_STALE_LOCK_SECONDS,
    LockHeartbeatGuard,
    _touch_lock,
    lock_holder,
    recover_orphaned_backups,
    recover_source_path,
)

WORKER = "worker-self"
OTHER = "worker-other"

FIVE_MINUTES_HOURS = 5 / 60
TWENTY_MINUTES_HOURS = 20 / 60

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


class TestStaleWindow:
    """Freshness is measured against the lock heartbeat cadence, not a
    wall-clock guess. Incident 2026-09-02: a SIGKILLed worker's 22 s old
    lock blocked every retry of that file for the old 2 h window."""

    def test_foreign_lock_refreshed_five_minutes_ago_is_active(self, tmp_path: Path):
        original, bak, lock, _ = _stage(tmp_path, lock_owner=OTHER, lock_age=FIVE_MINUTES_HOURS)
        assert recover_source_path(original, worker_id=WORKER) == "active"
        stats = recover_orphaned_backups([tmp_path], worker_id=WORKER)
        assert stats["skipped_active"] == 1
        assert bak.exists() and lock.exists() and not original.exists()

    def test_foreign_lock_refreshed_twenty_minutes_ago_is_stale(self, tmp_path: Path):
        original, bak, lock, _ = _stage(tmp_path, lock_owner=OTHER, lock_age=TWENTY_MINUTES_HOURS)
        assert recover_source_path(original, worker_id=WORKER) == "restored"
        assert original.read_bytes() == ORIGINAL_BYTES
        assert not bak.exists()
        assert not lock.exists()

    def test_lock_holder_reports_owner_and_age(self, tmp_path: Path):
        """The claim-time wait names who holds the lock and how long ago
        it was refreshed; an unreadable or missing lock reads as None."""
        original, _, lock, _ = _stage(tmp_path, lock_owner=OTHER, lock_age=FIVE_MINUTES_HOURS)
        holder = lock_holder(original)
        assert holder is not None
        owner, age = holder
        assert owner == OTHER
        assert 299 <= age <= 302
        lock.write_text("not json at all")
        assert lock_holder(original) is None
        lock.unlink()
        assert lock_holder(original) is None

    def test_stale_window_is_derived_from_the_touch_interval(self):
        """The window must absorb missed touches (a busy NFS mount, a
        failed os.replace) without declaring a running pipeline dead, and
        the pipeline must heartbeat on the SAME cadence the recovery scans
        measure against. One source of truth, checked here so nobody can
        tune them apart."""
        assert RECOVERY_STALE_LOCK_SECONDS == 3 * LOCK_TOUCH_INTERVAL
        assert pipeline.LOCK_TOUCH_INTERVAL == LOCK_TOUCH_INTERVAL
        # find_stale_locks reports against the same window.
        import inspect

        default_hours = inspect.signature(pipeline.find_stale_locks).parameters["max_age_hours"]
        assert default_hours.default == RECOVERY_STALE_LOCK_SECONDS / 3600


class TestTouchLock:
    """The lock heartbeat: run_pipeline refreshes the lock's timestamp
    periodically so 'stale' means dead, not just long-running. Without
    it, a restarting neighbor on shared NFS treats a live encode's lock
    as abandoned after 15 min and deletes the tmp out from under it."""

    def test_touch_refreshes_timestamp_and_keeps_identity(self, tmp_path: Path):
        lock = tmp_path / "movie.mkv.tf_lock"
        _write_lock(lock, worker_id=WORKER, age_hours=3.0)
        old_ts = json.loads(lock.read_text())["timestamp"]

        _touch_lock(lock, job_id="job-1", worker_id=WORKER)

        data = json.loads(lock.read_text())
        assert data["timestamp"] > old_ts
        assert data["worker_id"] == WORKER
        assert data["job_id"] == "job-1"

    def test_touch_leaves_no_temp_file(self, tmp_path: Path):
        lock = tmp_path / "movie.mkv.tf_lock"
        _write_lock(lock, worker_id=WORKER)
        _touch_lock(lock, job_id="job-1", worker_id=WORKER)
        leftovers = [p for p in tmp_path.iterdir() if p != lock]
        assert leftovers == []

    def test_touched_foreign_lock_counts_as_fresh(self, tmp_path: Path):
        """An old lock that was just touched belongs to a live pipeline —
        recovery must respect it even though it was CREATED hours ago."""
        original, bak, lock, _ = _stage(tmp_path, lock_owner=OTHER, lock_age=48.0)
        _touch_lock(lock, job_id="job-1", worker_id=OTHER)
        stats = recover_orphaned_backups([tmp_path], worker_id=WORKER)
        assert stats["restored"] == 0
        assert stats["skipped_active"] == 1
        assert bak.exists() and lock.exists() and not original.exists()


class TestLockHeartbeatGuard:
    """Cancelling a task awaiting asyncio.to_thread does NOT wait for the
    OS thread — without the guard, an in-flight touch completing after
    UNLOCK resurrects the deleted lock with a fresh timestamp, and other
    workers decline the path as 'active' for hours (review HIGH-1)."""

    def test_touch_after_stop_is_a_noop(self, tmp_path: Path):
        guard = LockHeartbeatGuard()
        lock = tmp_path / "movie.mkv.tf_lock"
        guard.stop()
        guard.touch(lock, job_id="j1", worker_id=WORKER)
        assert not lock.exists(), "a touch after stop() must never recreate the lock"

    def test_stop_waits_for_inflight_touch(self, tmp_path: Path, monkeypatch):
        """stop() must block until a touch that already entered the guarded
        region finishes — that ordering is what lets the caller delete the
        lock afterwards without a resurrection window."""
        import threading
        import time

        import transcode_forge.worker.storage.filesystem as fs

        lock = tmp_path / "movie.mkv.tf_lock"
        started = threading.Event()
        release = threading.Event()
        real_touch = fs._touch_lock

        def blocking_touch(lock_path, *, job_id, worker_id):
            started.set()
            assert release.wait(timeout=5), "test deadlock"
            real_touch(lock_path, job_id=job_id, worker_id=worker_id)

        monkeypatch.setattr(fs, "_touch_lock", blocking_touch)

        guard = LockHeartbeatGuard()
        toucher = threading.Thread(
            target=guard.touch, args=(lock,), kwargs={"job_id": "j1", "worker_id": WORKER}
        )
        toucher.start()
        assert started.wait(timeout=5)

        stopper = threading.Thread(target=guard.stop)
        stopper.start()
        time.sleep(0.05)
        assert stopper.is_alive(), "stop() must block while a touch is in flight"

        release.set()
        stopper.join(timeout=5)
        toucher.join(timeout=5)
        assert not stopper.is_alive()

        # The in-flight touch completed BEFORE stop returned — so a delete
        # performed after stop() can never be undone by it.
        assert lock.exists()
        lock.unlink()
        guard.touch(lock, job_id="j1", worker_id=WORKER)
        assert not lock.exists()


class TestRecoverSourcePath:
    """Claim-time single-path recovery: the same crash matrix as the
    startup scan, applied lazily when a worker is about to process a
    path — this is what heals a crash whose worker never comes back."""

    def test_clean_path_is_untouched(self, tmp_path: Path):
        original = tmp_path / "movie.mkv"
        original.write_bytes(ORIGINAL_BYTES)
        assert recover_source_path(original, worker_id=WORKER) == "clean"
        assert original.read_bytes() == ORIGINAL_BYTES

    def test_missing_original_restored_from_bak(self, tmp_path: Path):
        original, bak, lock, _ = _stage(tmp_path, lock_owner=OTHER, lock_age=48.0)
        assert recover_source_path(original, worker_id=WORKER) == "restored"
        assert original.read_bytes() == ORIGINAL_BYTES
        assert not bak.exists()
        assert not lock.exists()

    def test_unconfirmed_encode_replaced_when_lock_stale(self, tmp_path: Path):
        original, bak, _, _ = _stage(tmp_path, original=True, lock_owner=OTHER, lock_age=48.0)
        assert recover_source_path(original, worker_id=WORKER) == "restored"
        assert original.read_bytes() == ORIGINAL_BYTES
        assert not bak.exists()

    def test_stale_lock_and_tmp_cleaned(self, tmp_path: Path):
        original, _, lock, tmp = _stage(
            tmp_path, bak=False, original=True, lock_owner=OTHER, lock_age=48.0, tmp=True
        )
        assert recover_source_path(original, worker_id=WORKER) == "cleaned"
        assert not lock.exists()
        assert not tmp.exists()
        assert original.read_bytes() == ENCODE_BYTES  # source untouched

    def test_own_leftover_lock_is_stale(self, tmp_path: Path):
        """A fresh lock under OUR worker_id at claim time is a leftover from
        this worker's previous life (one pipeline per path) — clean it."""
        original, _, lock, _ = _stage(tmp_path, bak=False, original=True, lock_owner=WORKER)
        assert recover_source_path(original, worker_id=WORKER) == "cleaned"
        assert not lock.exists()

    def test_fresh_foreign_lock_declines(self, tmp_path: Path):
        original, bak, lock, _ = _stage(tmp_path, lock_owner=OTHER)
        assert recover_source_path(original, worker_id=WORKER) == "active"
        assert bak.exists()
        assert lock.exists()

    def test_finished_transcode_plus_bak_needs_attention(self, tmp_path: Path):
        """bak + media file + NO lock: a completed job whose backup delete
        failed. The bak is the LAST copy of the true original — never
        proceed to an encode that would swap over it."""
        original, bak, _, _ = _stage(tmp_path, original=True)
        assert recover_source_path(original, worker_id=WORKER) == "attention"
        assert original.read_bytes() == ENCODE_BYTES
        assert bak.read_bytes() == ORIGINAL_BYTES

    def test_failed_restore_is_not_attention(self, tmp_path: Path):
        """A restore that ERRORS must be distinguishable from the ambiguous
        'attention' state — the operator guidance is opposite (attention:
        'delete the backup after verifying'; restore_failed: 'the backup may
        be the only copy — never delete') (review HIGH-2)."""
        bak = tmp_path / "movie.tf_bak.mkv"
        bak.write_bytes(ORIGINAL_BYTES)
        original = tmp_path / "movie.mkv"
        original.mkdir()  # a directory at the media path makes replace() error
        _write_lock(tmp_path / "movie.mkv.tf_lock", worker_id=OTHER, age_hours=48.0)

        assert recover_source_path(original, worker_id=WORKER) == "restore_failed"
        assert bak.read_bytes() == ORIGINAL_BYTES  # backup untouched

    def test_lost_restore_race_reports_restored(self, tmp_path: Path, monkeypatch):
        """Two recoveries racing the same path: the loser's replace() raises
        because the winner consumed the bak — that's a success, not an
        operator alert (review MEDIUM-1)."""
        original, _bak, _, _ = _stage(tmp_path)  # bak only, original missing
        real_replace = Path.replace

        def racing_replace(self: Path, target):
            # Simulate the concurrent winner: it restores the original and
            # consumes the bak an instant before our rename executes.
            real_replace(self, target)
            raise FileNotFoundError("simulated lost rename race")

        monkeypatch.setattr(Path, "replace", racing_replace)

        assert recover_source_path(original, worker_id=WORKER) == "restored"
        assert original.read_bytes() == ORIGINAL_BYTES


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
