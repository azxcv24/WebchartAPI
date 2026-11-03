"""Tests for connectors whose drivers are optional extras.

These verify URI parsing and the clear-error behavior when the driver is
not installed, without requiring the actual servers.
"""

import pytest

from webchart.core.errors import ConnectorError
from webchart.models.dataset import RemoteSourceSpec
from webchart.services.ingest.clickhouse import ClickHouseIngestBackend
from webchart.services.ingest.ftp import FtpIngestBackend
from webchart.services.ingest.sftp import SftpIngestBackend
from webchart.services.ingest.smb import SmbIngestBackend


def test_smb_unc_path():
    assert (
        SmbIngestBackend._unc_path("smb://host/share/dir/file.csv") == r"\\host\share\dir\file.csv"
    )


def test_smb_unc_path_invalid():
    with pytest.raises(ConnectorError):
        SmbIngestBackend._unc_path("smb://host")


def test_clickhouse_query_from_table():
    b = ClickHouseIngestBackend()
    spec = RemoteSourceSpec(
        protocol="clickhouse", uri="clickhouse://h/db", options={"table": "events"}
    )
    assert b._resolve_query(spec) == ("SELECT * FROM events", "events")


def test_clickhouse_query_explicit():
    b = ClickHouseIngestBackend()
    spec = RemoteSourceSpec(
        protocol="clickhouse", uri="clickhouse://h/db", options={"query": "SELECT 1"}
    )
    assert b._resolve_query(spec) == ("SELECT 1", "clickhouse_query")


def test_clickhouse_requires_query_or_table():
    b = ClickHouseIngestBackend()
    with pytest.raises(ConnectorError):
        b._resolve_query(RemoteSourceSpec(protocol="clickhouse", uri="clickhouse://h/db"))


def test_clickhouse_table_identifier_validated():
    b = ClickHouseIngestBackend()
    # db.table is fine
    spec_ok = RemoteSourceSpec(
        protocol="clickhouse", uri="clickhouse://h/db", options={"table": "analytics.events"}
    )
    assert b._resolve_query(spec_ok)[0] == "SELECT * FROM analytics.events"
    # injection attempt via table is rejected
    spec_bad = RemoteSourceSpec(
        protocol="clickhouse",
        uri="clickhouse://h/db",
        options={"table": "events; DROP TABLE users"},
    )
    with pytest.raises(ConnectorError):
        b._resolve_query(spec_bad)


@pytest.mark.parametrize(
    "backend_cls, scheme, lib",
    [
        (FtpIngestBackend, "ftp", "aioftp"),
        (SftpIngestBackend, "sftp", "asyncssh"),
    ],
)
async def test_missing_driver_raises_connector_error(backend_cls, scheme, lib, tmp_data_dir):
    pytest.importorskip  # noqa: keep import semantics explicit
    try:
        __import__(lib)
        pytest.skip(f"{lib} is installed; missing-driver path not exercised")
    except ImportError:
        pass

    backend = backend_cls()
    spec = RemoteSourceSpec(protocol=scheme, uri=f"{scheme}://h/path/data.csv")
    with pytest.raises(ConnectorError) as exc:
        await backend.ingest_to_parquet(spec, tmp_data_dir / "datasets" / "ds-x")
    assert lib in str(exc.value)


async def test_smb_missing_driver(tmp_data_dir):
    try:
        __import__("smbclient")
        pytest.skip("smbclient installed")
    except ImportError:
        pass
    spec = RemoteSourceSpec(protocol="smb", uri="smb://h/share/data.csv")
    with pytest.raises(ConnectorError):
        await SmbIngestBackend().ingest_to_parquet(spec, tmp_data_dir / "datasets" / "ds-x")
