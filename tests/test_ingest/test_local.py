"""file:// 인제스트: 허용 디렉터리 밖은 읽지 못한다."""

import pandas as pd
import pytest

from webchart.config import settings
from webchart.core.errors import ConnectorError
from webchart.models.dataset import RemoteSourceSpec
from webchart.services.ingest.local import LocalIngestBackend


@pytest.fixture
def shared_dir(tmp_path):
    root = tmp_path / "shared"
    root.mkdir()
    pd.DataFrame({"x": range(5), "y": range(5)}).to_csv(root / "ok.csv", index=False)
    (tmp_path / "secret.csv").write_text("x,y\n1,2\n")
    return root


async def _ingest(uri: str, tmp_data_dir):
    spec = RemoteSourceSpec(protocol="file", uri=uri)
    return await LocalIngestBackend().ingest_to_parquet(spec, tmp_data_dir / "datasets" / "loc")


async def test_disabled_without_root(shared_dir, tmp_data_dir):
    with pytest.raises(ConnectorError):
        await _ingest(f"file://{shared_dir}/ok.csv", tmp_data_dir)


async def test_reads_inside_root(shared_dir, tmp_data_dir, monkeypatch):
    monkeypatch.setattr(settings, "local_ingest_root", shared_dir)
    meta = await _ingest(f"file://{shared_dir}/ok.csv", tmp_data_dir)
    assert meta.row_count == 5


@pytest.mark.parametrize("relative", ["../secret.csv", "../../../../etc/passwd.csv"])
async def test_rejects_paths_outside_root(shared_dir, tmp_data_dir, monkeypatch, relative):
    monkeypatch.setattr(settings, "local_ingest_root", shared_dir)
    with pytest.raises(ConnectorError):
        await _ingest(f"file://{shared_dir}/{relative}", tmp_data_dir)
