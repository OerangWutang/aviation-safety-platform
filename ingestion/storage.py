import json
import uuid
from pathlib import Path

import boto3
from django.conf import settings


def upload_json_payload(payload: dict) -> str:
    key = f"ingestion/{uuid.uuid4()}.json"

    if settings.S3_ENDPOINT_URL:
        client = boto3.client(
            "s3",
            endpoint_url=settings.S3_ENDPOINT_URL,
            aws_access_key_id=settings.S3_ACCESS_KEY_ID,
            aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY,
        )
        try:
            client.put_object(Bucket=settings.S3_BUCKET_NAME, Key=key, Body=json.dumps(payload).encode("utf-8"))
            return key
        except Exception:
            pass

    tmp_dir = Path(settings.BASE_DIR) / ".local_blobs"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    (tmp_dir / key.replace("/", "_")).write_text(json.dumps(payload), encoding="utf-8")
    return key


def fetch_json_payload(key: str) -> dict:
    if settings.S3_ENDPOINT_URL:
        client = boto3.client(
            "s3",
            endpoint_url=settings.S3_ENDPOINT_URL,
            aws_access_key_id=settings.S3_ACCESS_KEY_ID,
            aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY,
        )
        try:
            obj = client.get_object(Bucket=settings.S3_BUCKET_NAME, Key=key)
            return json.loads(obj["Body"].read().decode("utf-8"))
        except Exception:
            pass

    local = Path(settings.BASE_DIR) / ".local_blobs" / key.replace("/", "_")
    return json.loads(local.read_text(encoding="utf-8"))
