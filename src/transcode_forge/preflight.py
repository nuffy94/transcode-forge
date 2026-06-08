"""Startup preflight checks.

Validate the things that otherwise fail *silently* on the first scan — a
mistyped/unmounted library path or a missing ffmpeg — and surface them in
the logs and the UI instead. Non-fatal: the app still starts so the
operator can fix config rather than crash-loop.
"""

from __future__ import annotations

import logging
import os
import shutil
from typing import Any
from urllib.parse import urlparse

from transcode_forge.config import Settings

logger = logging.getLogger(__name__)


def run_preflight(settings: Settings) -> list[dict[str, Any]]:
    """Return a list of config issues (empty when everything checks out).

    Each issue is ``{level, code, message}`` where level is 'critical' or
    'warning' and message includes a concrete fix hint.
    """
    issues: list[dict[str, Any]] = []

    # ffmpeg / ffprobe must be on PATH for scanning + transcoding.
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            issues.append(
                {
                    "level": "critical",
                    "code": f"{tool}_missing",
                    "message": (
                        f"{tool} not found on PATH — scanning and transcoding will fail. "
                        "The Docker image bundles ffmpeg; on bare metal install it with "
                        "your package manager."
                    ),
                }
            )

    # Each configured library path must exist and be a readable directory.
    if not settings.libraries:
        issues.append(
            {
                "level": "warning",
                "code": "no_libraries",
                "message": (
                    "No libraries configured — set TF_LIBRARY_MOVIES / TF_LIBRARY_TV "
                    "(or add a library in Settings) so there is something to scan."
                ),
            }
        )
    for name, (path, _quality) in settings.libraries.items():
        if not os.path.isdir(path):
            issues.append(
                {
                    "level": "critical",
                    "code": f"library_{name}_missing",
                    "message": (
                        f"Library '{name}' path does not exist or is not a directory: "
                        f"{path}. In Docker check the volume mount in docker-compose.yml; "
                        "on bare metal check TF_LIBRARY_* points at the right place."
                    ),
                }
            )
        elif not os.access(path, os.R_OK):
            issues.append(
                {
                    "level": "critical",
                    "code": f"library_{name}_unreadable",
                    "message": f"Library '{name}' path is not readable: {path}.",
                }
            )

    return issues


def log_preflight(issues: list[dict[str, Any]]) -> None:
    """Log each preflight issue at an appropriate level."""
    for issue in issues:
        level = logging.CRITICAL if issue["level"] == "critical" else logging.WARNING
        logger.log(level, "PREFLIGHT: %s", issue["message"])


async def validate_db_connection(db_url: str) -> list[dict[str, Any]]:
    """Validate database connectivity with clear error messages.

    Attempts a real connection (SELECT 1) and distinguishes between:
    - Malformed URL (invalid format)
    - Connection refused (DB unreachable)
    - TLS failure (certificate or SSL mode mismatch)
    - Authentication failure (wrong credentials)

    Returns a list of issues; empty if the connection succeeds.
    """
    issues: list[dict[str, Any]] = []

    # Check if it's a Postgres URL
    if not (db_url.startswith("postgres://") or db_url.startswith("postgresql://")):
        # SQLite is always okay, no validation needed
        return issues

    # Try to parse the URL
    try:
        parsed = urlparse(db_url)
        if not parsed.hostname or not parsed.path:
            issues.append(
                {
                    "level": "critical",
                    "code": "db_url_malformed",
                    "message": (
                        f"Malformed PostgreSQL URL: {db_url}. "
                        "Expected format: postgresql://user:pass@host:port/dbname"
                    ),
                }
            )
            return issues
    except Exception as e:
        issues.append(
            {
                "level": "critical",
                "code": "db_url_parse_error",
                "message": f"Failed to parse database URL: {e}",
            }
        )
        return issues

    # Attempt a real connection
    try:
        import asyncpg  # type: ignore[import-untyped]

        try:
            # asyncpg parses sslmode from the URL itself.
            conn = await asyncpg.connect(db_url, timeout=5)
            try:
                await conn.execute("SELECT 1")
                logger.debug("Database connection validated successfully")
            finally:
                await conn.close()
        except Exception as e:
            # Determine the error type by class name or error message
            error_type_name = type(e).__name__
            error_str = str(e).lower()

            if error_type_name == "InvalidPasswordError" or "password" in error_str:
                issues.append(
                    {
                        "level": "critical",
                        "code": "db_auth_failed",
                        "message": (
                            "Database authentication failed — incorrect username or password. "
                            "Check TF_DB_URL credentials."
                        ),
                    }
                )
            elif error_type_name in ("CannotConnectNowError", "ConnectionRefusedError") or any(
                x in error_str for x in ("refused", "unreachable", "no route", "timed out")
            ):
                port = parsed.port or 5432
                addr = f"{parsed.hostname}:{port}"
                issues.append(
                    {
                        "level": "critical",
                        "code": "db_connection_refused",
                        "message": (
                            "Database connection refused — host unreachable or DB not running. "
                            f"Check hostname/port in TF_DB_URL (parsed as {addr})."
                        ),
                    }
                )
            elif any(x in error_str for x in ("ssl", "certificate", "tls")):
                issues.append(
                    {
                        "level": "critical",
                        "code": "db_ssl_error",
                        "message": (
                            f"TLS/SSL connection error: {e}. "
                            "If using Linode DBaaS, ensure sslmode=require in TF_DB_URL. "
                            "For local Postgres, use sslmode=disable or sslmode=prefer."
                        ),
                    }
                )
            else:
                issues.append(
                    {
                        "level": "critical",
                        "code": "db_error",
                        "message": f"Database error: {e}",
                    }
                )
    except Exception as e:
        logger.exception("Unexpected error during database validation")
        issues.append(
            {
                "level": "critical",
                "code": "db_validation_error",
                "message": f"Unexpected database validation error: {e}",
            }
        )

    return issues
