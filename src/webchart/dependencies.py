"""FastAPI dependency injection providers."""

from webchart.config import Settings, settings


def get_settings() -> Settings:
    return settings
