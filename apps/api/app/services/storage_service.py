import logging

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import settings

logger = logging.getLogger(__name__)


_DEFAULT_S3_CREDENTIAL = "minioadmin"


def guard_production_credentials() -> None:
    """Mirrors deps.py::get_otp_provider's production guard — refuses to
    boot with the well-known MinIO dev credentials once
    ENVIRONMENT=production, so a deployment that forgets to set real S3
    credentials fails loudly instead of silently running with a
    widely-known access key/secret."""
    if settings.environment != "production":
        return
    if (
        settings.s3_access_key_id == _DEFAULT_S3_CREDENTIAL
        or settings.s3_secret_access_key == _DEFAULT_S3_CREDENTIAL
    ):
        raise RuntimeError(
            "Default MinIO credentials must not be used when "
            "ENVIRONMENT=production — set real S3_ACCESS_KEY_ID/S3_SECRET_ACCESS_KEY"
        )


def _get_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key,
        region_name=settings.s3_region,
        config=BotoConfig(signature_version="s3v4"),
    )


def upload_file(key: str, data: bytes, content_type: str) -> None:
    _get_s3_client().put_object(
        Bucket=settings.s3_bucket_name, Key=key, Body=data, ContentType=content_type,
    )


def download_file(key: str) -> bytes:
    response = _get_s3_client().get_object(Bucket=settings.s3_bucket_name, Key=key)
    return response["Body"].read()


def delete_file(key: str) -> None:
    """Best-effort cleanup — called when a mid-batch failure needs to undo
    already-uploaded objects. Never raises: a cleanup failure shouldn't mask
    the original error that triggered it."""
    try:
        _get_s3_client().delete_object(Bucket=settings.s3_bucket_name, Key=key)
    except ClientError:
        logger.exception("Failed to clean up orphaned storage object %s", key)


def generate_presigned_url(key: str, expires_in: int = 300) -> str:
    return _get_s3_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket_name, "Key": key},
        ExpiresIn=expires_in,
    )


def ensure_bucket_exists() -> None:
    """Creates the configured bucket if it doesn't exist yet. MinIO (used for
    local dev) doesn't auto-create buckets the way some managed S3 setups do.

    Never raises: this runs on every app startup (including in tests, via
    TestClient's own startup event), and a storage backend that's briefly
    unreachable must not crash app boot — it should just be logged. Catches
    BotoCoreError alongside ClientError because a connection failure (e.g.
    MinIO not running) raises the former, not the latter.
    """
    try:
        client = _get_s3_client()
        client.head_bucket(Bucket=settings.s3_bucket_name)
    except ClientError as exc:
        status = exc.response.get("Error", {}).get("Code")
        if status in ("404", "NoSuchBucket"):
            try:
                client.create_bucket(Bucket=settings.s3_bucket_name)
            except (ClientError, BotoCoreError):
                logger.exception(
                    "Failed to create storage bucket %s", settings.s3_bucket_name
                )
        else:
            logger.exception(
                "Failed to verify storage bucket %s exists", settings.s3_bucket_name
            )
    except BotoCoreError:
        logger.exception(
            "Failed to reach storage backend while verifying bucket %s",
            settings.s3_bucket_name,
        )
