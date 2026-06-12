"""Unit tests for connector shared helpers."""

import pytest

from webchart.core.errors import ConnectorError, UnsupportedFormatError
from webchart.services.ingest.common import (
    remote_suffix,
    require_lib,
    size_limit_for,
    validate_remote_format,
)


def test_remote_suffix_lowercases_and_ignores_query():
    assert remote_suffix("s3://b/k/data.CSV") == ".csv"
    assert remote_suffix("https://h/a/file.parquet?token=x") == ".parquet"
    assert remote_suffix("ftp://h/path/no_ext") == ""


def test_validate_remote_format_accepts_known():
    assert validate_remote_format("https://h/f.csv") == ".csv"


def test_validate_remote_format_rejects_unknown():
    with pytest.raises(UnsupportedFormatError):
        validate_remote_format("s3://b/k/file.bin")


def test_size_limit_xlsx_is_tighter(monkeypatch):
    from webchart.config import settings

    monkeypatch.setattr(settings, "max_upload_size", 10_000_000_000)
    monkeypatch.setattr(settings, "max_xlsx_upload_size", 200_000_000)
    assert size_limit_for(".xlsx") == 200_000_000
    assert size_limit_for(".csv") == 10_000_000_000


def test_require_lib_missing_raises_connector_error():
    with pytest.raises(ConnectorError) as exc:
        require_lib("totally_missing_pkg_zzz", "connectors")
    assert "pip install" in str(exc.value)


def test_require_lib_present_returns_module():
    mod = require_lib("json", "connectors")
    assert mod.dumps({"a": 1}) == '{"a": 1}'
