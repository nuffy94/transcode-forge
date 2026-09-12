# Backups

Transcode Forge stores state in PostgreSQL and Redis. This guide covers backing up and restoring the database and configuration.

## What to back up

- **Database**: PostgreSQL (`postgres:16-alpine` service) holds all jobs, library metadata, user data, and settings.
- **Redis state**: optional, and rarely worth it. Redis relays live progress to the browser; the queue itself lives in PostgreSQL, so losing Redis costs you nothing a scheduler restart does not rebuild.
- **Configuration**: your `.env` file (contains credentials and library paths). Keep this safe.

## Database backup (PostgreSQL)

The scheduler runs with a PostgreSQL container on the internal Docker network. Back it up from the **host** using `docker compose exec`:

### Full dump to file

```bash
# From the directory containing docker-compose.yml:
docker compose exec -T postgres pg_dump -U tf transcode_forge > backup.sql
```

This writes a plain-text SQL dump to `backup.sql` on the host. `-T` runs without a terminal, suitable for scripts. The file is human-readable and safe to inspect.

### Backup to gzip (recommended)

```bash
docker compose exec -T postgres pg_dump -U tf transcode_forge | gzip > backup.sql.gz
```

Compresses the dump, saving disk space.

## Database backup (SQLite, dev and test only)

If you're running with SQLite (`TF_DB_URL=sqlite:///transcode_forge.db`), take the backup through SQLite:

```bash
sqlite3 transcode_forge.db ".backup transcode_forge.db.bak"
```

The scheduler runs SQLite in WAL mode, so a committed row can still be sitting in `transcode_forge.db-wal` when you take the backup. `cp` copies the main file alone and drops that row. `.backup` reads through a real connection, so it captures everything committed, and it is safe to run while the scheduler is up.

`VACUUM INTO 'transcode_forge.db.bak'` produces the same consistent snapshot, compacted, and refuses to overwrite an existing file.

## Redis data (optional)

Redis lives on the internal Docker network and is not backed up by default. If you want a snapshot of the relay anyway:

```bash
docker compose exec -T redis redis-cli SAVE
docker cp $(docker compose ps -q redis):/data/dump.rdb ./redis-dump.rdb
```

On restore, copy `redis-dump.rdb` back into the Redis container before restarting.

## Restore procedure

### Restore PostgreSQL dump

Load the dump into a fresh database, then promote it. Importing over the
populated database is what turns a recovery into a loss: `psql` prints
"already exists" and duplicate-key errors line by line, then exits 0, so a
restore that did nothing reads as a finished one.

```bash
# Stop the scheduler. Nothing may write while the databases swap.
docker compose stop scheduler

# 1. An empty target beside the live database.
docker compose exec -T postgres createdb -U tf transcode_forge_restore

# 2. Load the backup into it. ON_ERROR_STOP=1 stops at the first error and
#    exits non-zero instead of running to the end.
gunzip < backup.sql.gz | docker compose exec -T postgres \
  psql -v ON_ERROR_STOP=1 -U tf -d transcode_forge_restore

# 3. Check the exit code before going further. Anything but 0 means the
#    restore failed and the live database has not been touched yet.
echo $?

# 4. Promote it. The live database is renamed, never dropped.
docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U tf -d postgres \
  -c 'ALTER DATABASE transcode_forge RENAME TO transcode_forge_prerestore;' \
  -c 'ALTER DATABASE transcode_forge_restore RENAME TO transcode_forge;'

# 5. Start the scheduler. Migrations auto-apply on boot.
docker compose up -d scheduler
```

For a plain (ungzipped) dump, step 2 is `docker compose exec -T postgres psql
-v ON_ERROR_STOP=1 -U tf -d transcode_forge_restore` with `< backup.sql` on the
host side.

The rename in step 4 needs every other client off `transcode_forge`, which is
why the scheduler is stopped. Two properties follow from restoring this way:

- **Your backup is only ever read.** Nothing here writes to `backup.sql.gz`.
- **The restore is reversible.** `transcode_forge_prerestore` still holds the
  state you started with, so a wrong backup costs you a second rename, not the
  data. Drop it once the instance looks right.

There is no need for `pg_dump --clean --if-exists` here. A fresh target has
nothing to drop, so the "already exists" conflicts that option works around
cannot happen.

### Restore SQLite

```bash
# Stop the scheduler.
docker compose stop scheduler

# Write the backup back through SQLite.
sqlite3 transcode_forge.db ".restore transcode_forge.db.bak"

# Restart.
docker compose up -d scheduler
```

A plain `cp` would drop the backup's pages next to the live database's stale
`-wal` and `-shm` sidecars, which SQLite can then replay over them. `.restore`
writes through a real connection, so the file and its sidecars agree.

### Restore Redis (optional)

```bash
# Stop the scheduler and redis.
docker compose stop scheduler redis

# Copy the backup into the container.
docker cp redis-dump.rdb $(docker compose ps -q redis):/data/dump.rdb

# Restart.
docker compose up -d
```

## File backups during transcode

During an active transcode, the worker creates a `.tf_bak` backup of the original file. If verification fails after swap, the original is restored automatically. These `.tf_bak` files are cleaned up when the transcode succeeds. This is **not** the database backup; it's a per-file safety mechanism.

## Scheduling backups

Use `cron` or your system scheduler to back up regularly. Example crontab entry (daily at 2 AM):

```bash
0 2 * * * cd /path/to/transcode-forge && docker compose exec -T postgres pg_dump -U tf transcode_forge | gzip > backups/backup-$(date +\%Y\%m\%d).sql.gz
```

Ensure the `backups/` directory exists and has sufficient space.
