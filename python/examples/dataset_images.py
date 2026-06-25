#!/usr/bin/env python3
"""Upload images to a Lakekeeper `dataset` generic table (unstructured data).

The `dataset` format is for unstructured data — here, raw image files rather than a
columnar dataset. The SDK creates the table and vends short-lived S3 credentials; the
actual file upload is done with **boto3** (the S3 case).

Storage-backend note: the upload client is backend-specific. This example covers S3
(boto3). An Azure (ADLS) warehouse would vend Azure credentials and use
`azure-storage-blob`; a GCS warehouse would use `google-cloud-storage`. Only the upload
step changes — the Lakekeeper catalog flow (create + vended load) is identical.

Env:
  LAKEKEEPER    e.g. http://localhost:8181
  WAREHOUSE_ID  warehouse UUID (the URL path prefix, not the name)
  PROJECT_ID    x-project-id (optional)
  NAMESPACE     default ai.test          TABLE     default product_images
  IMG_DIR       default examples/assets/img
  # auth — a static token, or client_credentials:
  TOKEN  |  OAUTH_TOKEN_URL / OAUTH_CLIENT_ID / OAUTH_CLIENT_SECRET / OAUTH_SCOPE
"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from urllib.parse import urlparse

from pylakekeeper import (
    Client,
    ClientCredentials,
    ConflictError,
    GenericTableFormat,
    StaticToken,
)

NAMESPACE = os.environ.get("NAMESPACE", "ai.test")
TABLE = os.environ.get("TABLE", "product_images")
IMG_DIR = Path(os.environ.get("IMG_DIR", Path(__file__).parent / "assets" / "img"))


def build_auth():
    if token := os.environ.get("TOKEN"):
        return StaticToken(token)
    return ClientCredentials(
        token_url=os.environ["OAUTH_TOKEN_URL"],
        client_id=os.environ["OAUTH_CLIENT_ID"],
        client_secret=os.environ["OAUTH_CLIENT_SECRET"],
        scope=os.environ.get("OAUTH_SCOPE"),
    )


def s3_client(opts: dict[str, str]):
    """A boto3 S3 client from the vended credentials.

    ``lance_storage_options`` already uses AWS-style keys; only region/endpoint are renamed
    to boto3's parameter names. ``endpoint_url`` is None for real AWS, set for MinIO/SeaweedFS.
    """
    import boto3

    return boto3.client(
        "s3",
        aws_access_key_id=opts["aws_access_key_id"],
        aws_secret_access_key=opts["aws_secret_access_key"],
        aws_session_token=opts.get("aws_session_token"),
        region_name=opts.get("aws_region"),
        endpoint_url=opts.get("aws_endpoint"),
    )


def main() -> None:
    with Client(
        base_url=os.environ["LAKEKEEPER"],
        warehouse=os.environ["WAREHOUSE_ID"],
        auth=build_auth(),
        project_id=os.environ.get("PROJECT_ID"),
    ) as c:
        try:
            c.generic_tables.create(
                NAMESPACE, TABLE, format=GenericTableFormat.DATASET, doc="product images"
            )
        except ConflictError:
            print(f"table {NAMESPACE}.{TABLE} already exists — continuing")

        t = c.generic_tables.load(NAMESPACE, TABLE, vended=True)
        print(f"location = {t.location}")

        parsed = urlparse(t.location)  # s3://<bucket>/<key-prefix>
        bucket, prefix = parsed.netloc, parsed.path.strip("/")
        s3 = s3_client(t.lance_storage_options)

        images = sorted(p for p in IMG_DIR.iterdir() if p.is_file())
        if not images:
            raise SystemExit(f"no files in {IMG_DIR}")
        for p in images:
            key = f"{prefix}/{p.name}"
            s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=p.read_bytes(),
                ContentType=mimetypes.guess_type(p.name)[0] or "application/octet-stream",
            )
            print(f"  uploaded {p.name} -> s3://{bucket}/{key}")

        resp = s3.list_objects_v2(Bucket=bucket, Prefix=f"{prefix}/")
        print(f"\n{resp.get('KeyCount', 0)} objects under {t.location}:")
        for obj in resp.get("Contents", []):
            print(f"  {obj['Key']}  ({obj['Size']} bytes)")


if __name__ == "__main__":
    main()
