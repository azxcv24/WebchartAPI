"""Tests for the S3/MinIO connector (client construction + URI parsing)."""

import pytest

from webchart.core.errors import ConnectorError
from webchart.models.dataset import RemoteSourceSpec
from webchart.services.ingest.s3 import S3IngestBackend


def test_protocols():
    assert set(S3IngestBackend().supported_protocols) == {"s3", "minio"}


def test_parse_bucket_key():
    assert S3IngestBackend._parse("s3://my-bucket/a/b/c.parquet") == (
        "my-bucket",
        "a/b/c.parquet",
    )


def test_parse_rejects_missing_key():
    with pytest.raises(ConnectorError):
        S3IngestBackend._parse("s3://bucket-only")


def test_minio_client_uses_path_style_and_endpoint():
    spec = RemoteSourceSpec(
        protocol="minio",
        uri="minio://b/k/data.csv",
        credentials={"access_key": "AK", "secret_key": "SK"},
        options={"endpoint_url": "http://minio:9000"},
    )
    client = S3IngestBackend._client(spec)
    assert client.meta.endpoint_url == "http://minio:9000"
    assert client.meta.config.s3["addressing_style"] == "path"


def test_aws_client_defaults_to_auto_addressing():
    spec = RemoteSourceSpec(
        protocol="s3",
        uri="s3://b/k/data.csv",
        credentials={"access_key": "AK", "secret_key": "SK"},
        options={"region": "ap-northeast-2"},
    )
    client = S3IngestBackend._client(spec)
    assert client.meta.region_name == "ap-northeast-2"
    assert client.meta.config.s3["addressing_style"] == "auto"
