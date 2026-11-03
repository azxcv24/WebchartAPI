"""S3 / MinIO 인제스트 백엔드.

MinIO는 S3 호환이라 boto3 기반 백엔드 하나로 둘 다 처리한다. MinIO는
options.endpoint_url(예: http://minio:9000)을 주면 되고, 사용자 지정 엔드포인트가 있으면
path-style 주소 방식이 자동으로 선택된다.

URI:     s3://bucket/key/path.parquet   (또는 minio://bucket/key)
creds:   access_key, secret_key, [session_token]
options: endpoint_url, region, addressing_style ('path'|'virtual'|'auto')
"""

from pathlib import Path
from urllib.parse import unquote, urlparse

from webchart.core.concurrency import run_in_thread
from webchart.core.errors import ConnectorError, WebchartError
from webchart.models.dataset import DatasetMeta, RemoteSourceSpec
from webchart.services.ingest.common import (
    enforce_size,
    guard_egress_host,
    temp_download_path,
    validate_remote_format,
)
from webchart.services.ingest.upload import convert_to_parquet


class S3IngestBackend:
    """S3/MinIO에서 객체를 내려받아 Parquet으로 변환한다."""

    @property
    def supported_protocols(self) -> list[str]:
        return ["s3", "minio"]

    @staticmethod
    def _parse(uri: str) -> tuple[str, str]:
        """s3://bucket/key 를 (bucket, key)로 나눈다."""
        parsed = urlparse(uri)
        bucket = parsed.netloc
        key = unquote(parsed.path).lstrip("/")
        if not bucket or not key:
            raise ConnectorError(f"Invalid S3 URI (need s3://bucket/key): {uri}")
        return bucket, key

    @staticmethod
    def _client(spec: RemoteSourceSpec):
        """spec의 자격 증명·옵션으로 boto3 S3 클라이언트를 만든다."""
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

        # MinIO와 대부분의 자체 호스팅 게이트웨이는 path-style 주소 방식만 지원한다.
        addressing = opts.get("addressing_style") or ("path" if endpoint else "auto")
        kwargs["config"] = Config(
            signature_version="s3v4",
            s3={"addressing_style": addressing},
        )
        return boto3.client(**kwargs)

    async def ingest_to_parquet(self, spec: RemoteSourceSpec, dest: Path) -> DatasetMeta:
        dataset_id = dest.name
        suffix = validate_remote_format(spec.uri)
        temp_path = temp_download_path(dataset_id, suffix)

        # 실제로 접속할 호스트만 검사한다. 사용자 지정 엔드포인트(MinIO)는 임의 주소일 수
        # 있어 검사하고, 없으면 AWS S3 공개 엔드포인트라 검사하지 않는다.
        endpoint = spec.options.get("endpoint_url")
        if endpoint:
            guard_egress_host(urlparse(endpoint).hostname)

        def _download() -> None:
            bucket, key = self._parse(spec.uri)
            client = self._client(spec)
            try:
                # 내려받기 전에 ContentLength로 너무 큰 객체를 거부한다.
                head = client.head_object(Bucket=bucket, Key=key)
                enforce_size(int(head.get("ContentLength", 0)), suffix)
                client.download_file(bucket, key, str(temp_path))
            except WebchartError:
                temp_path.unlink(missing_ok=True)
                raise
            except Exception as e:
                temp_path.unlink(missing_ok=True)
                raise ConnectorError(f"S3 download failed for {spec.uri}: {e}") from e

        await run_in_thread(_download)
        # convert_to_parquet은 끝나면 temp_path를 스스로 지운다.
        return await run_in_thread(convert_to_parquet, temp_path, dataset_id)
