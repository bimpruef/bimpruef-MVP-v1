"""
r2_storage.py – Cloudflare R2 storage helper for BIMPruef

This module provides a small S3-compatible wrapper around Cloudflare R2.
It is used for uploading, downloading, checking and deleting files from
the configured R2 bucket.
"""

import os
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


def _get_env(name: str) -> str:
    return os.environ.get(name, "").strip()


def get_r2_config() -> dict:
    return {
        "endpoint_url": _get_env("R2_ENDPOINT_URL"),
        "access_key_id": _get_env("R2_ACCESS_KEY_ID"),
        "secret_access_key": _get_env("R2_SECRET_ACCESS_KEY"),
        "bucket_name": _get_env("R2_BUCKET_NAME"),
    }


def r2_enabled() -> bool:
    config = get_r2_config()
    return all(config.values())


def get_r2_client():
    config = get_r2_config()

    missing = [key for key, value in config.items() if not value]
    if missing:
        raise RuntimeError(
            "Cloudflare R2 is not configured. Missing values: "
            + ", ".join(missing)
        )

    return boto3.client(
        "s3",
        endpoint_url=config["endpoint_url"],
        aws_access_key_id=config["access_key_id"],
        aws_secret_access_key=config["secret_access_key"],
        region_name="auto",
    )


def get_r2_bucket_name() -> str:
    bucket_name = _get_env("R2_BUCKET_NAME")
    if not bucket_name:
        raise RuntimeError("R2_BUCKET_NAME is not configured.")
    return bucket_name


def upload_file_to_r2(
    local_path: str,
    storage_key: str,
    content_type: str = "application/octet-stream",
) -> None:
    """
    Upload a local file to Cloudflare R2.
    """
    if not local_path:
        raise ValueError("local_path is required.")

    if not storage_key:
        raise ValueError("storage_key is required.")

    local_file = Path(local_path)

    if not local_file.exists() or not local_file.is_file():
        raise FileNotFoundError(f"Local file not found: {local_path}")

    client = get_r2_client()
    bucket_name = get_r2_bucket_name()

    client.upload_file(
        Filename=str(local_file),
        Bucket=bucket_name,
        Key=storage_key,
        ExtraArgs={"ContentType": content_type},
    )


def download_file_from_r2(storage_key: str, local_path: str) -> None:
    """
    Download an object from Cloudflare R2 to a local path.
    """
    if not storage_key:
        raise ValueError("storage_key is required.")

    if not local_path:
        raise ValueError("local_path is required.")

    client = get_r2_client()
    bucket_name = get_r2_bucket_name()

    destination = Path(local_path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    client.download_file(
        Bucket=bucket_name,
        Key=storage_key,
        Filename=str(destination),
    )


def delete_file_from_r2(storage_key: str) -> None:
    """
    Delete an object from Cloudflare R2.
    Missing objects are ignored.
    """
    if not storage_key:
        return

    client = get_r2_client()
    bucket_name = get_r2_bucket_name()

    try:
        client.delete_object(
            Bucket=bucket_name,
            Key=storage_key,
        )
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return
        raise


def object_exists_in_r2(storage_key: str) -> bool:
    """
    Return True if an object exists in Cloudflare R2.
    """
    if not storage_key:
        return False

    client = get_r2_client()
    bucket_name = get_r2_bucket_name()

    try:
        client.head_object(
            Bucket=bucket_name,
            Key=storage_key,
        )
        return True
    except ClientError:
        return False
