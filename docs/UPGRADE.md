# Upgrading Transcode Forge

Upgrading is safe: migrations run automatically on scheduler boot, and released migrations are immutable. Always back up the database first.

## Before you upgrade

1. **Back up your database** — see [BACKUP.md](./BACKUP.md).
2. **Check the release notes** — any breaking changes or new environment variables.

## Upgrade steps

### From source (git clone)

```bash
# Fetch the latest code.
git fetch origin
git checkout main

# Rebuild the image.
docker compose build --pull

# Pull dependent images (Redis, Postgres).
docker compose pull

# Stop and restart.
docker compose down
docker compose up -d
```

The scheduler will auto-apply any pending migrations on boot.

### From pre-built image (`docker-compose.prod.yml`)

```bash
# Pull the latest image.
docker compose -f docker-compose.prod.yml pull

# Or pin a specific version by setting TF_VERSION in .env, then pull.
# Edit .env first, then:
docker compose -f docker-compose.prod.yml pull

# Bring up the scheduler. It auto-applies migrations.
docker compose -f docker-compose.prod.yml up -d
```

## Verify the upgrade

Check the scheduler logs:

```bash
docker compose logs scheduler
```

Look for lines like (only *pending* migrations are applied):

```
Migration 0004_token_hash: applied
```

Check that the health endpoint returns 200:

```bash
curl http://localhost:8000/api/health/ready
```

The web UI should load at http://localhost:8000 (or your configured port).

## Rollback (if something goes wrong)

If the upgrade introduces a critical bug:

1. **Restore the database** — see [BACKUP.md](./BACKUP.md).
2. **Pin the previous version** — edit `.env` and set `TF_VERSION=0.5.0` (replace with the version you were running).
3. **Pull and restart**:

```bash
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

Migrations are recorded in the `schema_migrations` table and never re-run, so the database remains valid.

## Migration failures (rare)

If a migration fails at boot, the scheduler exits with an error. A failed
migration may have applied partially, so restore from your pre-upgrade backup
before retrying rather than assuming a clean slate. Check the logs:

```bash
docker compose logs scheduler | tail -50
```

Common fixes:

- **Corrupted data**: restore from backup and retry.
- **Disk full**: free up space and retry.
- **Schema conflict**: restore from backup, ensure no concurrent scheduler instances, and retry.

Restore and rollback as described above, then report the issue on GitHub.

## Running workers during upgrade

Workers can keep pulling jobs while the scheduler is down. When the scheduler restarts, workers reconnect automatically. No manual intervention needed.

## Important: don't edit released migrations

Migrations in the codebase are numbered SQL files. Once a release ships, those migrations are immutable. If you need a schema change after release, add a **new** numbered migration file (higher number). This ensures all deployments reach the same state.
