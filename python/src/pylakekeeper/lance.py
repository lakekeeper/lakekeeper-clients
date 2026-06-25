"""Map Lakekeeper's vended Iceberg-style storage credentials to Lance ``storage_options``.

The generic-tables load response returns S3 credentials using Iceberg property names
(``s3.access-key-id``, ...). Lance's object-store expects different keys
(``aws_access_key_id``, ...). :func:`iceberg_creds_to_lance` translates between them so a
loaded table drops straight into ``lance.dataset(t.location, storage_options=...)``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

#: Iceberg credential/config property name -> Lance object-store option name.
ICEBERG_TO_LANCE = {
    "s3.access-key-id": "aws_access_key_id",
    "s3.secret-access-key": "aws_secret_access_key",
    "s3.session-token": "aws_session_token",
    "s3.region": "aws_region",
    "client.region": "aws_region",
    "s3.endpoint": "aws_endpoint",
}


def iceberg_creds_to_lance(
    storage_credentials: Iterable[Mapping[str, object]] | None = None,
    config: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Translate vended Iceberg credentials into Lance ``storage_options``.

    Args:
        storage_credentials: the ``storage-credentials`` array from the load response;
            each entry is a mapping with a ``config`` dict of Iceberg property names.
        config: the top-level ``config`` map from the load response (also Iceberg keys).

    Reads credential ``config`` first, then the top-level ``config`` (which wins on
    conflict). Adds ``allow_http=true`` when the endpoint is plaintext ``http://``.
    """
    opts: dict[str, str] = {}
    for cred in storage_credentials or []:
        cred_config = cred.get("config") or {}
        if isinstance(cred_config, Mapping):
            for key, value in cred_config.items():
                if key in ICEBERG_TO_LANCE:
                    opts[ICEBERG_TO_LANCE[key]] = str(value)
    for key, value in (config or {}).items():
        if key in ICEBERG_TO_LANCE:
            opts[ICEBERG_TO_LANCE[key]] = str(value)
    if opts.get("aws_endpoint", "").startswith("http://"):
        opts["allow_http"] = "true"
    return opts
