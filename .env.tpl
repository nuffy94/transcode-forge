# Transcode Forge — 1Password environment template.
#
# Same settings as .env.example, but secrets are resolved at runtime from
# 1Password via op:// references — so no secret values live in the repo.
# Safe to commit. Run with:
#   op run --env-file=.env.tpl -- docker compose up -d
# Replace "YourVault" with your own 1Password vault name.

# --- Secrets (pulled from 1Password at runtime) ---
TF_PG_PASSWORD=op://YourVault/transcode-forge/postgres-password
TF_AUTH_SECRET=op://YourVault/transcode-forge/auth-secret

# --- Library paths (host media, mounted into the containers) ---
TF_LIBRARY_MOVIES=./media/movies
TF_LIBRARY_TV=./media/tv

# --- Quality presets (lower = better quality, bigger file) ---
TF_QUALITY_MOVIES=21
TF_QUALITY_TV=24

# --- Database — self-hosted Postgres or Linode DBaaS ---
# For local/self-hosted Postgres (from 1Password vault):
# TF_DB_URL=postgresql://tf:op://YourVault/transcode-forge/postgres-password@localhost:5432/transcode_forge

# For Linode Managed Postgres (DBaaS with TLS required):
# TF_DB_URL=postgresql://tf:op://YourVault/transcode-forge/linode-db-password@YOUR-LINODE-DBAAS-HOST:PORT/transcode_forge?sslmode=require

# --- Optional ---
TF_PORT=8000
# TF_SESSION_SECURE=true       # set when serving over HTTPS (behind a TLS proxy)
# TF_LOG_LEVEL=info            # debug | info | warning | error

# --- S3 object storage (optional, for S3-library backend) ---
# Leave blank to use filesystem-only. Credentials are pulled from 1Password.
# TF_S3_ENDPOINT_URL=https://s3.example.com
# TF_S3_REGION=op://YourVault/transcode-forge/s3-region
# TF_S3_ACCESS_KEY_ID=op://YourVault/transcode-forge/s3-access-key
# TF_S3_SECRET_ACCESS_KEY=op://YourVault/transcode-forge/s3-secret-key
