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



def list_r2_keys_by_prefix(prefix: str) -> list[str]:
    """
    Return all R2 object keys below a prefix.

    This is used for hard project/session deletion. It makes the cleanup
    future-proof because it also removes files that are not part of the
    current fixed slot naming scheme.
    """
    prefix = str(prefix or "").strip()
    if not prefix:
        return []

    client = get_r2_client()
    bucket_name = get_r2_bucket_name()

    keys: list[str] = []

    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket_name, Prefix=prefix):
        for obj in page.get("Contents", []) or []:
            key = obj.get("Key")
            if key:
                keys.append(str(key))

    return keys


def delete_prefix_from_r2(prefix: str) -> list[str]:
    """
    Delete all R2 objects below a prefix and return the deleted keys.

    Missing prefixes are treated as already clean.
    """
    prefix = str(prefix or "").strip()
    if not prefix:
        return []

    client = get_r2_client()
    bucket_name = get_r2_bucket_name()
    keys = list_r2_keys_by_prefix(prefix)

    if not keys:
        return []

    # S3 DeleteObjects accepts max. 1000 objects per request.
    for start in range(0, len(keys), 1000):
        batch = keys[start:start + 1000]
        client.delete_objects(
            Bucket=bucket_name,
            Delete={
                "Objects": [{"Key": key} for key in batch],
                "Quiet": True,
            },
        )

    return keys
