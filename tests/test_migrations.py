"""Tests for the schema migration runner."""

import sqlite3
from pathlib import Path

import aiosqlite
import pytest

from transcode_forge.migrations import (
    apply_sqlite,
    discover_migrations,
)


async def _table_exists(conn: aiosqlite.Connection, name: str) -> bool:
    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    row = await cur.fetchone()
    return row is not None


async def _applied_versions(conn: aiosqlite.Connection) -> set[int]:
    cur = await conn.execute("SELECT version FROM schema_migrations")
    return {row[0] for row in await cur.fetchall()}


class TestDiscover:
    def test_finds_initial_migration(self):
        migrations = discover_migrations()
        assert any(v == 1 and "initial" in name for v, name, _sql, _pg in migrations)

    def test_returns_sorted_by_version(self):
        migrations = discover_migrations()
        versions = [v for v, _, _, _ in migrations]
        assert versions == sorted(versions)


class TestFreshInstall:
    async def test_fresh_creates_all_tables(self, tmp_path: Path):
        db_path = tmp_path / "fresh.db"
        conn = await aiosqlite.connect(db_path)
        try:
            await apply_sqlite(conn)
            for tbl in (
                "libraries",
                "media_files",
                "jobs",
                "workers",
                "skipped_files",
                "scans",
                "system_state",
                "excluded_paths",
                "schedules",
                "schema_migrations",
            ):
                assert await _table_exists(conn, tbl), f"missing table {tbl}"

            applied = await _applied_versions(conn)
            assert 1 in applied, "v1 should be marked applied"
        finally:
            await conn.close()

    async def test_idempotent(self, tmp_path: Path):
        db_path = tmp_path / "idem.db"
        conn = await aiosqlite.connect(db_path)
        try:
            await apply_sqlite(conn)
            applied_first = await _applied_versions(conn)
            await apply_sqlite(conn)  # second run is a no-op
            applied_second = await _applied_versions(conn)
            assert applied_first == applied_second
        finally:
            await conn.close()


class TestExistingInstallBootstrap:
    """A v0.4 install has tables but no schema_migrations row. The runner
    must mark migration 1 as applied without trying to re-run it.
    """

    async def test_bootstrap_stamps_existing_install(self, tmp_path: Path):
        db_path = tmp_path / "existing.db"
        conn = await aiosqlite.connect(db_path)
        try:
            # Simulate v0.4: the hand-created schema (jobs + libraries + ...) exists,
            # but schema_migrations does not. A real pre-migrations install had the
            # full table set including libraries and workers, which later migrations
            # (0005 ALTERs libraries, 0006 ALTERs workers) touch — so the simulation
            # must include them, not just jobs.
            await conn.execute(
                "CREATE TABLE jobs (id TEXT PRIMARY KEY, "
                "source_path TEXT NOT NULL, library TEXT NOT NULL, "
                "source_codec TEXT NOT NULL, "
                "target_codec TEXT NOT NULL DEFAULT 'hevc', "
                "quality_value INTEGER NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'pending', "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            await conn.execute(
                "CREATE TABLE libraries (id TEXT PRIMARY KEY, name TEXT NOT NULL, "
                "media_type TEXT NOT NULL, path TEXT NOT NULL UNIQUE, "
                "quality_preset INTEGER NOT NULL DEFAULT 21, "
                "enabled INTEGER NOT NULL DEFAULT 1, auto_scan INTEGER NOT NULL DEFAULT 0, "
                "scan_interval_hours INTEGER DEFAULT 24, "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            await conn.execute(
                "CREATE TABLE workers (id TEXT PRIMARY KEY, name TEXT NOT NULL, "
                "host TEXT NOT NULL, capabilities TEXT NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'offline', "
                "registered_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            # Insert a row to make sure data is preserved through bootstrap
            await conn.execute(
                "INSERT INTO jobs (id, source_path, library, source_codec, "
                "quality_value, created_at, updated_at) "
                "VALUES ('preexisting', '/m.mkv', 'movies', 'h264', 21, '2026-01-01', '2026-01-01')"
            )
            await conn.commit()

            await apply_sqlite(conn)

            # v1 marked as applied without re-running
            applied = await _applied_versions(conn)
            assert 1 in applied

            # Pre-existing data still there
            cur = await conn.execute("SELECT id FROM jobs WHERE id = 'preexisting'")
            assert (await cur.fetchone()) is not None
        finally:
            await conn.close()


class TestNeverEditReleasedMigrations:
    """Lint-style check: every migration file ever shipped must keep its
    version number stable. This catches accidental renumbering.
    """

    def test_version_one_is_initial(self):
        migrations = discover_migrations()
        v1 = [m for m in migrations if m[0] == 1]
        assert len(v1) == 1
        assert v1[0][1] == "initial"

    def test_no_duplicate_versions(self):
        migrations = discover_migrations()
        versions = [v for v, _, _, _ in migrations]
        assert len(versions) == len(set(versions)), "duplicate migration version"


class TestWorkerTokenUniqueBinding:
    """Migration 0010: one worker identity per token, enforced by a unique
    index (SQLite can't ALTER TABLE ADD CONSTRAINT; the index form is valid
    on both dialects)."""

    async def test_unique_index_enforced_and_nulls_allowed(self, tmp_path: Path):
        db_path = tmp_path / "uq.db"
        conn = await aiosqlite.connect(db_path)
        try:
            await apply_sqlite(conn)

            cur = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name='uq_worker_tokens_worker_id'"
            )
            assert (await cur.fetchone()) is not None

            # Multiple unbound tokens (NULL worker_id) coexist fine.
            await conn.execute(
                "INSERT INTO worker_tokens (token, label, created_at) VALUES ('t1', 'a', 'x')"
            )
            await conn.execute(
                "INSERT INTO worker_tokens (token, label, created_at) VALUES ('t2', 'b', 'x')"
            )

            # Two tokens bound to the same worker identity are rejected.
            await conn.execute("UPDATE worker_tokens SET worker_id = 'w' WHERE token = 't1'")
            with pytest.raises(sqlite3.IntegrityError):
                await conn.execute("UPDATE worker_tokens SET worker_id = 'w' WHERE token = 't2'")
        finally:
            await conn.close()


class TestPostgresAdapter:
    """The Postgres adapter substitutes BIGINT for selected INTEGER cols.
    No live connection needed — verify the string transform directly.
    """

    def test_promotes_byte_columns_to_bigint(self):
        from transcode_forge.migrations import _adapt_for_postgres

        sql = (
            "CREATE TABLE jobs ("
            "  source_size INTEGER, output_size INTEGER, "
            "  retry_count INTEGER, file_size INTEGER)"
        )
        out = _adapt_for_postgres(sql)
        assert "source_size BIGINT" in out
        assert "output_size BIGINT" in out
        assert "file_size BIGINT" in out
        # retry_count stays INTEGER — small counter, doesn't need bigint
        assert "retry_count INTEGER" in out


@pytest.fixture
async def fresh_sqlite_db(tmp_path: Path):
    db_path = tmp_path / "test.db"
    conn = await aiosqlite.connect(db_path)
    yield conn
    await conn.close()


class TestPgOnlyMigrations:
    """`NNNN_name.pg.sql` files are postgres-only DDL (ALTER COLUMN TYPE
    is unparseable by SQLite — and unneeded: its INTEGER is 8-byte).
    SQLite must record the version WITHOUT executing, so both dialects
    agree on the schema version."""

    def test_discover_flags_pg_only(self):
        migrations = discover_migrations()
        by_version = {v: pg for v, _n, _s, pg in migrations}
        assert by_version[15] is True  # 0015_skipped_file_size_bigint.pg.sql
        assert by_version[1] is False

    async def test_sqlite_stamps_pg_only_without_executing(self, tmp_path: Path):
        import aiosqlite

        async with aiosqlite.connect(tmp_path / "m.db") as conn:
            await apply_sqlite(conn)
            cur = await conn.execute("SELECT version FROM schema_migrations")
            versions = {row[0] for row in await cur.fetchall()}
        assert 15 in versions

    async def test_postgres_lane_exercises_already_bigint_alter(self, db):
        """On Postgres the adapter makes 0001 create file_size as BIGINT,
        so 0015's ALTER runs against an ALREADY-BIGINT column on every
        fresh PG migration — i.e. CI's PG lane exercises the exact
        redundant-ALTER path of the manually-widened production DB. This
        test makes that implicit coverage explicit: migrations applied
        (db fixture) + version 15 recorded + inserts >2**31 survive."""
        import os

        from transcode_forge.repos import skipped as skip_repo

        rows = await (await db.execute("SELECT version FROM schema_migrations")).fetchall()
        assert 15 in {r[0] for r in rows}
        await skip_repo.record_skip(
            db,
            file_path="/m/huge-" + os.urandom(4).hex() + ".mkv",
            library="movies",
            codec="h264",
            file_size=2_884_089_163,
            skip_reason="size_regression",
        )
