"""JSON state adapter — S3 in Lambda (STATE_BUCKET set), local files otherwise."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_bucket = os.environ.get("STATE_BUCKET", "")


def read_json(key: str, default: Any = None) -> Any:
    """Read a JSON object from S3 or local filesystem."""
    if _bucket:
        import boto3
        import botocore.exceptions
        s3 = boto3.client("s3")
        try:
            obj = s3.get_object(Bucket=_bucket, Key=key)
            return json.loads(obj["Body"].read())
        except s3.exceptions.NoSuchKey:
            return default
        except botocore.exceptions.ClientError as exc:
            logger.warning("S3 read failed for %s: %s", key, exc)
            return default

    if os.path.exists(key):
        try:
            with open(key, encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            logger.warning("Local read failed for %s: %s", key, exc)
    return default


def write_json(key: str, data: Any) -> None:
    """Write a JSON object to S3 or local filesystem (atomic for local)."""
    if _bucket:
        import boto3
        boto3.client("s3").put_object(
            Bucket=_bucket,
            Key=key,
            Body=json.dumps(data, ensure_ascii=False, indent=2).encode(),
        )
        return

    parent = os.path.dirname(key)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = key + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, key)
