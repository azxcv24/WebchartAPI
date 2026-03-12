"""Shared test fixtures."""

import shutil
import tempfile
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from webchart.config import settings


@pytest.fixture(autouse=True)
def tmp_data_dir(tmp_path):
    """Use a temp directory for data during tests."""
    settings.data_dir = tmp_path / "data"
    settings.data_dir.mkdir()
    (settings.data_dir / "uploads").mkdir()
    (settings.data_dir / "datasets").mkdir()
    yield settings.data_dir


@pytest.fixture
def client():
    """FastAPI test client."""
    from webchart.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def sample_csv(tmp_path) -> Path:
    """Create a sample CSV file for testing."""
    df = pd.DataFrame({
        "x": range(1000),
        "y": [i * 2.5 + (i % 7) for i in range(1000)],
        "category": [f"cat_{i % 5}" for i in range(1000)],
    })
    path = tmp_path / "sample.csv"
    df.to_csv(path, index=False)
    return path


@pytest.fixture
def sample_dataframe() -> pd.DataFrame:
    """Sample DataFrame for unit tests."""
    return pd.DataFrame({
        "x": range(10000),
        "y": [i * 1.5 + (i % 13) for i in range(10000)],
        "z": [i ** 0.5 for i in range(10000)],
    })
