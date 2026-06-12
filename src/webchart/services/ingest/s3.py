"""S3 / MinIO ingest backend.

MinIO is S3-compatible, so a single boto3-based backend serves both. For
MinIO, pass options.endpoint_url (e.g. http://minio:9000); path-style
addressing is selected automatically when a custom endpoint is present.

URI:    s3://bucket/key/path.parquet   (or minio://bucket/key)
creds:  access_key, secret_key, [session_token]
options: endpoint_url, region, addressing_style ('path'|'virtual'|'auto')
"""

import logging
from pathlib import Path
from urllib.parse import unquote, urlparse

from webchart.core.concurrency import run_in_thread
from webchart.core.errors import ConnectorError
from webchart.models.dataset import DatasetMeta, RemoteSourceSpec
from webchart.services.ingest.common import (
    temp_download_path,
    validate_remote_format,
)
from webchart.services.ingest.upload import convert_to_parquet

logger = logging.getLogger("webchart.ingest.s3")


class S3IngestBackend:
    """Download an object from S3/MinIO and convert it to Parquet."""

    @property
    def supported_protocols(self) -> list[str]:
        return ["s3", "minio"]

    @staticmethod
    def _parse(uri: str) -> tuple[str, str]:
        parsed = urlparse(uri)
        bucket = parsed.netloc
        key = unquote(parsed.path).lstrip("/")
        if not bucket or not key:
            raise ConnectorError(f"Invalid S3 URI (need s3://bucket/key): {uri}")
        return bucket, key

    @staticmethod
    def _client(spec: RemoteSourceSpec):
        import boto3
        from botocore.config import Config

        creds = spec.credentials
        opts = spec.options

        kwargs: dict = {
            "service_name": "s3",
            "aws_access_key_id": creds.get("access_key") or creds.get("aws_access_key_id"),
            "aws_secret_access_key": (
                creds.get("secret_key") or creds.get("aws_secret_access_key")
            ),
            "region_name": opts.get("region") or opts.get("region_name") or "us-east-1",
        }
        if creds.get("session_token"):
            kwargs["aws_session_token"] = creds["session_token"]

        endpoint = opts.get("endpoint_url")
        if endpoint:
            kwargs["endpoint_url"] = endpoint

        # MinIO and most self-hosted gateways require path-style addressing.
        addressing = opts.get("addressing_style") or ("path" if endpoint else "auto")
        kwargs["config"] = Config(
            signature_version="s3v4",
            s3={"addressing_style": addressing},
        )
        return boto3.client(**kwargs)

    async def validate_source(self, spec: RemoteSourceSpec) -> bool:
        def _head() -> bool:
            bucket, key = self._parse(spec.uri)
            self._client(spec).head_object(Bucket=bucket, Key=key)
            return True

        try:
            return await run_in_thread(_head)
        except Exception as e:
            logger.debug(f"S3 validate_source failed: {e}")
            return False

    async def ingest_to_parquet(self, spec: RemoteSourceSpec, dest: Path) -> DatasetMeta:
        dataset_id = dest.name
        suffix = validate_remote_format(spec.uri)
        temp_path = temp_download_path(dataset_id, suffix)

        def _download() -> None:
            bucket, key = self._parse(spec.uri)
            try:
                self._client(spec).download_file(bucket, key, str(temp_path))
            except Exception as e:
                temp_path.unlink(missing_ok=True)
                raise ConnectorError(f"S3 download failed for {spec.uri}: {e}") from e

        await run_in_thread(_download)
        # convert_to_parquet runs in a thread and cleans up temp_path itself.
        return await run_in_thread(convert_to_parquet, temp_path, dataset_id)
