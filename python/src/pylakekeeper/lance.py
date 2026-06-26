"""Map Lakekeeper's vended Iceberg-style storage credentials to format-specific options.

The generic-tables load response returns S3 credentials using Iceberg property names
(``s3.access-key-id``, ...).  This module translates them to the key shapes expected by
Lance (``aws_access_key_id``, ...) and fsspec/s3fs (``key``/``secret``/``token``).
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

# Keys that go into fsspec's top-level kwargs.
_ICEBERG_TO_FSSPEC_FLAT = {
    "s3.access-key-id": "key",
    "s3.secret-access-key": "secret",
    "s3.session-token": "token",
}

# Keys that go into fsspec's client_kwargs dict.
_ICEBERG_TO_FSSPEC_CLIENT = {
    "s3.endpoint": "endpoint_url",
    "s3.region": "region_name",
    "client.region": "region_name",
}


def _collect_iceberg_props(
    storage_credentials: Iterable[Mapping[str, object]] | None,
    config: Mapping[str, str] | None,
) -> dict[str, str]:
    """Merge vended credential configs + top-level config into a single flat dict."""
    props: dict[str, str] = {}
    for cred in storage_credentials or []:
        cred_config = cred.get("config") or {}
        if isinstance(cred_config, Mapping):
            props.update({k: str(v) for k, v in cred_config.items()})
    props.update({k: str(v) for k, v in (config or {}).items()})
    return props


def iceberg_creds_to_lance(
    storage_credentials: Iterable[Mapping[str, object]] | None = None,
    config: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Translate vended Iceberg credentials into Lance ``storage_options``.

    Args:
        storage_credentials: the ``storage-credentials`` array from the load response.
        config: the top-level ``config`` map from the load response.

    Adds ``allow_http=true`` when the endpoint is a plaintext ``http://`` URL.
    """
    props = _collect_iceberg_props(storage_credentials, config)
    opts = {ICEBERG_TO_LANCE[k]: v for k, v in props.items() if k in ICEBERG_TO_LANCE}
    if opts.get("aws_endpoint", "").startswith("http://"):
        opts["allow_http"] = "true"
    return opts


def iceberg_creds_to_fsspec(
    storage_credentials: Iterable[Mapping[str, object]] | None = None,
    config: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Translate vended Iceberg credentials into ``fsspec.filesystem("s3", **kwargs)``.

    Returns a dict ready to be unpacked directly into ``fsspec.filesystem()``:

    .. code-block:: python

        resp = lk.generic_tables.load(ns, name, vended=True)
        fs = fsspec.filesystem("s3", **resp.fsspec_kwargs)
        files = fs.ls(resp.location)

    Non-default S3 endpoints and region end up in ``client_kwargs`` as fsspec expects.
    Omits keys whose values are empty or ``None``.
    """
    props = _collect_iceberg_props(storage_credentials, config)

    kwargs: dict[str, object] = {}
    for iceberg_key, fsspec_key in _ICEBERG_TO_FSSPEC_FLAT.items():
        val = props.get(iceberg_key)
        if val:
            kwargs[fsspec_key] = val

    client_kwargs: dict[str, str] = {}
    for iceberg_key, ck in _ICEBERG_TO_FSSPEC_CLIENT.items():
        val = props.get(iceberg_key)
        if val:
            client_kwargs[ck] = val
    if client_kwargs:
        kwargs["client_kwargs"] = client_kwargs

    return kwargs
