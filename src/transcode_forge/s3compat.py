"""Shared botocore client config for S3-compatible storage.

boto3/botocore >= 1.36 compute CRC32 request checksums by default and
expect checksum validation on responses. Non-AWS S3-compatible endpoints
(Linode Object Storage, MinIO, …) reject the checksum headers with 403.
Restrict checksums to operations that strictly require them so uploads
and downloads work against any S3-compatible service.
"""

from aiobotocore.config import AioConfig  # type: ignore[import-untyped]


def s3_client_config() -> AioConfig:
    """Return the client config every S3 client in this codebase must use."""
    return AioConfig(
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    )
