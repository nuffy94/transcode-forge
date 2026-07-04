#!/usr/bin/env bash
# Transcode Forge -- scheduler StackScript (Linode Compute).
#
# Deploys the full scheduler stack (Redis + PostgreSQL + scheduler behind a
# Caddy TLS edge) from the public GHCR image. Standalone-capable: after
# first boot you can join the instance's own spare CPU as a worker with
# ./join-local-worker.sh -- a one-node deploy is simply "don't add workers".
#
# Tested target image: Ubuntu 24.04 LTS. Run as root at first boot.
# StackScript output persists on disk at /root/StackScript.out (and shows
# in the Lish console) -- this script never prints secrets.
#
# Control-plane steps that are NOT this script's job (see deploy/linode/README.md):
# attach a Cloud Firewall (allow 22/80/443 only), attach a Block Storage
# volume for media/scratch, point DNS at the instance, /setup, token issuance.
#
# <UDF name="domain" label="HTTPS domain for the web UI (blank = localhost-only, no TLS edge)" default="" example="forge.example.com" />
# <UDF name="cloudflare_dns_token_password" label="Cloudflare API token for DNS-01 certificates (blank = HTTP-01 on port 80)" default="" />
# <UDF name="s3_endpoint" label="Object Storage endpoint URL (blank = local media on the instance)" default="" example="https://us-ord-1.linodeobjects.com" />
# <UDF name="s3_bucket" label="Object Storage bucket holding your media" default="" example="forge-media" />
# <UDF name="s3_access_key" label="Object Storage access key" default="" />
# <UDF name="s3_secret_password" label="Object Storage secret key" default="" />
# <UDF name="managed_db_url_password" label="Managed Database PostgreSQL URL (blank = built-in Postgres container)" default="" example="postgresql://user:pass@host:5432/db?sslmode=require" />

set -euo pipefail

# UDF env vars are absent (not just empty) outside Linode -- normalize so the
# script also runs by hand / in render-only test mode.
DOMAIN="${DOMAIN:-}"
CLOUDFLARE_DNS_TOKEN_PASSWORD="${CLOUDFLARE_DNS_TOKEN_PASSWORD:-}"
S3_ENDPOINT="${S3_ENDPOINT:-}"
S3_BUCKET="${S3_BUCKET:-}"
S3_ACCESS_KEY="${S3_ACCESS_KEY:-}"
S3_SECRET_PASSWORD="${S3_SECRET_PASSWORD:-}"
MANAGED_DB_URL_PASSWORD="${MANAGED_DB_URL_PASSWORD:-}"

# Render-only mode (tests / dry runs): set TF_SS_RENDER_DIR to a directory and
# the script renders every config file there and exits -- no installs, no
# mounts, no docker.
RENDER_DIR="${TF_SS_RENDER_DIR:-}"

APP_DIR="${RENDER_DIR:-/opt/transcode-forge}"
IMAGE="ghcr.io/nuffy94/transcode-forge"

log() { echo "[transcode-forge] $*"; }

# ---------------------------------------------------------------- derivations

# Normalize the S3 endpoint to an https:// URL and derive the signing region
# from its first hostname label (us-ord-1.linodeobjects.com -> us-ord-1).
S3_REGION=""
if [[ -n "$S3_ENDPOINT" ]]; then
    [[ "$S3_ENDPOINT" == http*://* ]] || S3_ENDPOINT="https://${S3_ENDPOINT}"
    S3_HOST="${S3_ENDPOINT#*://}"
    S3_HOST="${S3_HOST%%/*}"
    S3_REGION="${S3_HOST%%.*}"
fi

# Auto-tune concurrency to the plan (D9): one CPU transcode saturates ~4
# vCPUs of x265/SVT-AV1. Clamp to the app's 1..4 range.
CPUS="$(nproc 2>/dev/null || echo 4)"
WORKER_MAX_CONCURRENT=$(( CPUS / 4 ))
(( WORKER_MAX_CONCURRENT < 1 )) && WORKER_MAX_CONCURRENT=1
(( WORKER_MAX_CONCURRENT > 4 )) && WORKER_MAX_CONCURRENT=4

random_token() {
    openssl rand -base64 48 | tr -d '=+/' | cut -c1-44
}

# ------------------------------------------------- block storage (skip render)

DATA_DIR="/mnt/data"
if [[ -z "$RENDER_DIR" ]]; then
    volume_device="$(find /dev/disk/by-id -name 'scsi-0Linode_Volume_*' 2>/dev/null | head -n1 || true)"
    if [[ -n "$volume_device" ]]; then
        if ! blkid "$volume_device" >/dev/null 2>&1; then
            log "Formatting attached Block Storage volume (no filesystem found)."
            mkfs.ext4 -q "$volume_device"
        fi
        mkdir -p "$DATA_DIR"
        if ! mountpoint -q "$DATA_DIR"; then
            mount "$volume_device" "$DATA_DIR"
            echo "$volume_device $DATA_DIR ext4 defaults,noatime 0 2" >> /etc/fstab
        fi
        log "Block Storage volume mounted at $DATA_DIR."
    else
        DATA_DIR="/opt/transcode-forge/data"
        log "WARNING: no Block Storage volume attached -- media/scratch will live"
        log "on the root disk ($DATA_DIR). Fine for a smoke test; attach a volume"
        log "for real media (root disks are small)."
    fi
    mkdir -p "$DATA_DIR/media/movies" "$DATA_DIR/media/tv" "$DATA_DIR/scratch"
fi

# ------------------------------------------------------------- render configs

mkdir -p "$APP_DIR"

PG_PASSWORD="$(random_token)"
AUTH_SECRET="$(random_token)"

if [[ -n "$MANAGED_DB_URL_PASSWORD" ]]; then
    DB_URL="$MANAGED_DB_URL_PASSWORD"
else
    DB_URL="postgresql://tf:${PG_PASSWORD}@postgres:5432/transcode_forge"
fi

SESSION_SECURE="false"
[[ -n "$DOMAIN" ]] && SESSION_SECURE="true"

# .env holds every dynamic value; the compose file stays static and reads
# ${VARS} from here at up-time. Never printed to stdout.
{
    printf 'TF_VERSION=latest\n'
    printf 'TF_DATA_DIR=%s\n' "$DATA_DIR"
    printf 'TF_DB_URL=%s\n' "$DB_URL"
    printf 'TF_PG_PASSWORD=%s\n' "$PG_PASSWORD"
    printf 'TF_AUTH_SECRET=%s\n' "$AUTH_SECRET"
    printf 'TF_SESSION_SECURE=%s\n' "$SESSION_SECURE"
    printf 'TF_LOG_LEVEL=info\n'
    printf 'TF_DOMAIN=%s\n' "$DOMAIN"
    printf 'CLOUDFLARE_API_TOKEN=%s\n' "$CLOUDFLARE_DNS_TOKEN_PASSWORD"
    printf 'TF_S3_ENDPOINT_URL=%s\n' "$S3_ENDPOINT"
    printf 'TF_S3_REGION=%s\n' "$S3_REGION"
    printf 'TF_S3_ACCESS_KEY_ID=%s\n' "$S3_ACCESS_KEY"
    printf 'TF_S3_SECRET_ACCESS_KEY=%s\n' "$S3_SECRET_PASSWORD"
    printf 'TF_WORKER_MAX_CONCURRENT=%s\n' "$WORKER_MAX_CONCURRENT"
    printf 'TF_WORKER_TOKEN=\n'
} > "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"

# --- docker-compose.yml, assembled per deploy shape ---

cat > "$APP_DIR/docker-compose.yml" <<'EOF'
# Generated by the Transcode Forge scheduler StackScript. Values come from
# the .env file next to this file.

services:
  redis:
    image: redis:7-alpine
    command: redis-server --save 60 1 --loglevel warning
    volumes:
      - redis-data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5
    restart: unless-stopped
EOF

if [[ -z "$MANAGED_DB_URL_PASSWORD" ]]; then
    cat >> "$APP_DIR/docker-compose.yml" <<'EOF'

  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: transcode_forge
      POSTGRES_USER: tf
      POSTGRES_PASSWORD: ${TF_PG_PASSWORD}
    volumes:
      - postgres-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U tf -d transcode_forge"]
      interval: 5s
      timeout: 3s
      retries: 5
    restart: unless-stopped
EOF
fi

cat >> "$APP_DIR/docker-compose.yml" <<'EOF'

  scheduler:
    image: ghcr.io/nuffy94/transcode-forge:${TF_VERSION:-latest}
    ports:
      # Loopback only -- Caddy (or an SSH tunnel) is the way in.
      - "127.0.0.1:8000:8000"
    environment:
      TF_DB_URL: ${TF_DB_URL}
      TF_REDIS_URL: "redis://redis:6379/0"
      TF_AUTH_SECRET: ${TF_AUTH_SECRET}
      TF_SESSION_SECURE: ${TF_SESSION_SECURE}
      TF_LOG_LEVEL: ${TF_LOG_LEVEL:-info}
      TF_LIBRARY_MOVIES: "/media/movies"
      TF_LIBRARY_TV: "/media/tv"
      TF_S3_ENDPOINT_URL: ${TF_S3_ENDPOINT_URL}
      TF_S3_REGION: ${TF_S3_REGION}
      TF_S3_ACCESS_KEY_ID: ${TF_S3_ACCESS_KEY_ID}
      TF_S3_SECRET_ACCESS_KEY: ${TF_S3_SECRET_ACCESS_KEY}
    volumes:
      - ${TF_DATA_DIR}/media/movies:/media/movies:ro
      - ${TF_DATA_DIR}/media/tv:/media/tv:ro
    depends_on:
      redis:
        condition: service_healthy
EOF

if [[ -z "$MANAGED_DB_URL_PASSWORD" ]]; then
    cat >> "$APP_DIR/docker-compose.yml" <<'EOF'
      postgres:
        condition: service_healthy
EOF
fi

cat >> "$APP_DIR/docker-compose.yml" <<'EOF'
    healthcheck:
      test: ["CMD-SHELL", "curl -fsS http://localhost:8000/api/health/ready || exit 1"]
      interval: 15s
      timeout: 3s
      retries: 5
      start_period: 20s
    restart: unless-stopped

  # Joined on demand: ./join-local-worker.sh (docker compose --profile worker).
  worker:
    image: ghcr.io/nuffy94/transcode-forge:${TF_VERSION:-latest}
    profiles: ["worker"]
    command: ["python", "-m", "transcode_forge.worker"]
    environment:
      TF_SERVER_URL: "http://scheduler:8000"
      TF_WORKER_TOKEN: ${TF_WORKER_TOKEN}
      TF_WORKER_NAME: "scheduler-local"
      TF_PREFERRED_BACKEND: "cpu"
      TF_WORKER_MAX_CONCURRENT: ${TF_WORKER_MAX_CONCURRENT:-1}
      TF_SCRATCH_DIR: "/scratch"
      TF_S3_ENDPOINT_URL: ${TF_S3_ENDPOINT_URL}
      TF_S3_REGION: ${TF_S3_REGION}
      TF_S3_ACCESS_KEY_ID: ${TF_S3_ACCESS_KEY_ID}
      TF_S3_SECRET_ACCESS_KEY: ${TF_S3_SECRET_ACCESS_KEY}
    volumes:
      - ${TF_DATA_DIR}/media/movies:/media/movies:rw
      - ${TF_DATA_DIR}/media/tv:/media/tv:rw
      - ${TF_DATA_DIR}/scratch:/scratch
    depends_on:
      scheduler:
        condition: service_healthy
    restart: unless-stopped
EOF

if [[ -n "$DOMAIN" ]]; then
    if [[ -n "$CLOUDFLARE_DNS_TOKEN_PASSWORD" ]]; then
        # DNS-01 needs the Cloudflare DNS module -- built from the official
        # builder image at first boot (~2-4 min on a Dedicated 8GB).
        mkdir -p "$APP_DIR/caddy"
        cat > "$APP_DIR/caddy/Dockerfile" <<'EOF'
FROM caddy:2-builder AS builder
RUN xcaddy build --with github.com/caddy-dns/cloudflare
FROM caddy:2
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
EOF
        cat >> "$APP_DIR/docker-compose.yml" <<'EOF'

  caddy:
    build: ./caddy
EOF
    else
        cat >> "$APP_DIR/docker-compose.yml" <<'EOF'

  caddy:
    image: caddy:2
EOF
    fi

    cat >> "$APP_DIR/docker-compose.yml" <<'EOF'
    ports:
      - "80:80"
      - "443:443"
    environment:
      TF_DOMAIN: ${TF_DOMAIN}
      CLOUDFLARE_API_TOKEN: ${CLOUDFLARE_API_TOKEN}
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy-data:/data
      - caddy-config:/config
    depends_on:
      - scheduler
    restart: unless-stopped
EOF
fi

cat >> "$APP_DIR/docker-compose.yml" <<'EOF'

volumes:
  redis-data:
EOF
if [[ -z "$MANAGED_DB_URL_PASSWORD" ]]; then
    printf '  postgres-data:\n' >> "$APP_DIR/docker-compose.yml"
fi
if [[ -n "$DOMAIN" ]]; then
    printf '  caddy-data:\n  caddy-config:\n' >> "$APP_DIR/docker-compose.yml"
fi

# --- Caddyfile ---

if [[ -n "$DOMAIN" ]]; then
    if [[ -n "$CLOUDFLARE_DNS_TOKEN_PASSWORD" ]]; then
        cat > "$APP_DIR/Caddyfile" <<'EOF'
{$TF_DOMAIN} {
    encode zstd gzip
    reverse_proxy scheduler:8000
    tls {
        dns cloudflare {$CLOUDFLARE_API_TOKEN}
    }
}
EOF
    else
        cat > "$APP_DIR/Caddyfile" <<'EOF'
{$TF_DOMAIN} {
    encode zstd gzip
    reverse_proxy scheduler:8000
}
EOF
    fi
fi

# --- join-local-worker.sh ---

cat > "$APP_DIR/join-local-worker.sh" <<'EOF'
#!/usr/bin/env bash
# Join this instance's spare CPU as a transcode worker.
# Issue a token in the web UI first (Workers -> Issue token), then run this
# and paste it (prompt, not argument -- keeps the token out of shell history).
set -euo pipefail
umask 077
cd "$(dirname "$0")"
read -rs -p "Paste worker token: " token
echo
# Tokens are URL-safe base64 -- anything else is a bad paste.
[[ "$token" =~ ^[A-Za-z0-9_-]+$ ]] || { echo "That doesn't look like a worker token."; exit 1; }
grep -v '^TF_WORKER_TOKEN=' .env > .env.tmp
printf 'TF_WORKER_TOKEN=%s\n' "$token" >> .env.tmp
mv .env.tmp .env
docker compose --profile worker up -d
echo "Local worker started -- it should appear on the Workers page shortly."
EOF
chmod +x "$APP_DIR/join-local-worker.sh"

# --- next-steps note (no secrets) ---

{
    echo "Transcode Forge -- next steps"
    echo "============================"
    if [[ -n "$DOMAIN" ]]; then
        echo "1. Point DNS: an A record for ${DOMAIN} -> this instance's public IP."
        echo "2. Open https://${DOMAIN}/setup and create the admin account."
    else
        echo "1. No domain configured -- the UI listens on 127.0.0.1:8000 only."
        echo "   Reach it via an SSH tunnel (ssh -L 8000:127.0.0.1:8000 root@<ip>)"
        echo "   or add a proxy/tunnel before exposing it."
        echo "2. Open http://localhost:8000/setup (through the tunnel) and create"
        echo "   the admin account."
    fi
    if [[ -n "$S3_BUCKET" ]]; then
        echo "3. Settings -> Add library: storage 'S3 Object Storage',"
        echo "   bucket '${S3_BUCKET}', prefix 'masters/movies/'."
        echo "   Seed media into the bucket with deploy/linode/seed-media.sh."
    else
        echo "3. Settings -> Add library: path /media/movies (host: ${DATA_DIR}/media/movies)."
        echo "   Upload media there (scp/rsync), then Scan."
    fi
    echo "4. Transcode on this instance too: Workers -> Issue token, then run"
    echo "   ${APP_DIR}/join-local-worker.sh"
    echo "5. Add worker nodes: create Linodes with the worker StackScript"
    echo "   (one issued token each). Multi-node requires an S3 library."
    echo "6. Smoke test: curl -fsS https://${DOMAIN:-<domain>}/api/health/ready"
} > "$APP_DIR/NEXT-STEPS.txt"

if [[ -n "$RENDER_DIR" ]]; then
    log "Render-only mode: configs written to $RENDER_DIR. Exiting."
    exit 0
fi

# ------------------------------------------------------------ system install

log "Installing Docker CE (get.docker.com)..."
curl -fsSL https://get.docker.com | sh >/dev/null

log "Pulling images and starting the stack..."
cd "$APP_DIR"
docker compose pull -q
docker compose up -d --build

log "Waiting for the scheduler to become ready..."
ready=0
for _ in $(seq 1 36); do
    if curl -fsS http://127.0.0.1:8000/api/health/ready >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 5
done

if (( ready )); then
    log "Scheduler is up."
else
    log "WARNING: scheduler not ready after 3 minutes -- check 'docker compose logs scheduler'."
fi

cat "$APP_DIR/NEXT-STEPS.txt"
