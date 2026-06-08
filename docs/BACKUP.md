# Backups

Transcode Forge stores state in PostgreSQL and Redis. This guide covers backing up and restoring the database and configuration.

## What to back up

- **Database** — PostgreSQL (`postgres:16-alpine` service) holds all jobs, library metadata, user data, and settings.
- **Redis state** — optional, but recommended if you have queued jobs you'd like to survive a crash. Redis is rebuilt from the database on scheduler restart if lost.
- **Configuration** — your `.env` file (contains credentials and library paths). Keep this safe.

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

## Database backup (SQLite — dev/test only)

If you're running with SQLite (`TF_DB_URL=sqlite:///transcode_forge.db`), simply copy the database file:

```bash
cp transcode_forge.db transcode_forge.db.bak
```

## Redis data (optional)

Redis lives on the internal Docker network and is not backed up by default. If you want to preserve queued jobs:

```bash
docker compose exec -T redis redis-cli SAVE
docker cp $(docker compose ps -q redis):/data/dump.rdb ./redis-dump.rdb
```

On restore, copy `redis-dump.rdb` back into the Redis container before restarting.

## Restore procedure

### Restore PostgreSQL dump

```bash
# Stop the scheduler (workers can continue).
docker compose stop scheduler

# Restore from a plain-text dump:
docker compose exec -T postgres psql -U tf transcode_forge < backup.sql

# Or restore from gzip:
gunzip < backup.sql.gz | docker compose exec -T postgres psql -U tf transcode_forge

# Restart the scheduler. Migrations auto-apply on boot.
docker compose up -d scheduler
```

If a restore hits "already exists" conflicts, take the dump with `--clean
--if-exists` (a `pg_dump` option) so it drops objects before recreating them,
then restore it the same way:

```bash
docker compose exec -T postgres pg_dump -U tf --clean --if-exists transcode_forge | gzip > backup.sql.gz
```

### Restore SQLite

```bash
# Stop the scheduler.
docker compose stop scheduler

# Restore the backup.
cp transcode_forge.db.bak transcode_forge.db

# Restart.
docker compose up -d scheduler
```

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
