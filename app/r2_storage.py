"""
r2_storage.py – Cloudflare R2 storage helper for BIMPruef
"""

import os
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


R2_ENDPOINT_URL = os.environ.get("R2_ENDPOINT_URL", "").strip()
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "").strip()


def r2_enabled() -> bool:
    return all(
        [
            R2_ENDPOINT_URL,
            R2_ACCESS_KEY_ID,
            R2_SECRET_ACCESS_KEY,
            R2_BUCKET_NAME,
        ]
    )


def get_r2_client():
    if not r2_enabled():
        raise RuntimeError("Cloudflare R2 is not configured.")

    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT_URL,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )


def upload_file_to_r2(
    local_path: str,
    storage_key: str,
    content_type: str = "application/octet-stream",
) -> None:
    client = get_r2_client()

    client.upload_file(
        Filename=local_path,
        Bucket=R2_BUCKET_NAME,
        Key=storage_key,
        ExtraArgs={"ContentType": content_type},
    )


def download_file_from_r2(storage_key: str, local_path: str) -> None:
    client = get_r2_client()

    Path(local_path).parent.mkdir(parents=True, exist_ok=True)

    client.download_file(
        Bucket=R2_BUCKET_NAME,
        Key=storage_key,
        Filename=local_path,
    )


def delete_file_from_r2(storage_key: str) -> None:
    client = get_r2_client()

    try:
        client.delete_object(
            Bucket=R2_BUCKET_NAME,
            Key=storage_key,
        )
    except ClientError:
        pass


def object_exists_in_r2(storage_key: str) -> bool:
    client = get_r2_client()

    try:
        client.head_object(
            Bucket=R2_BUCKET_NAME,
            Key=storage_key,
        )
        return True
    except ClientError:
        return False
