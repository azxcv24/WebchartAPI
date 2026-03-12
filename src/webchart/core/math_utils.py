"""Coordinate mapping utilities for pixel <-> data space transforms."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Viewport:
    """Represents a viewport mapping between pixel and data coordinates."""

    pixel_width: int
    pixel_height: int
    x_min: float
    x_max: float
    y_min: float
    y_max: float

    @property
    def x_scale(self) -> float:
        return (self.x_max - self.x_min) / self.pixel_width if self.pixel_width else 1.0

    @property
    def y_scale(self) -> float:
        return (self.y_max - self.y_min) / self.pixel_height if self.pixel_height else 1.0

    def pixel_to_data(self, px: float, py: float) -> tuple[float, float]:
        """Convert pixel coordinates to data coordinates."""
        data_x = self.x_min + px * self.x_scale
        data_y = self.y_max - py * self.y_scale  # y축 반전 (픽셀은 위→아래, 데이터는 아래→위)
        return data_x, data_y

    def data_to_pixel(self, dx: float, dy: float) -> tuple[float, float]:
        """Convert data coordinates to pixel coordinates."""
        px = (dx - self.x_min) / self.x_scale if self.x_scale else 0.0
        py = (self.y_max - dy) / self.y_scale if self.y_scale else 0.0
        return px, py


def compute_tile_ranges(
    x_min: float,
    x_max: float,
    zoom: int,
    tile_size: int = 512,
) -> list[tuple[int, float, float]]:
    """Compute tile indices and their data ranges for a given zoom level.

    Returns list of (tile_index, tile_x_min, tile_x_max).
    """
    num_tiles = 2**zoom
    tile_data_width = (x_max - x_min) / num_tiles
    return [
        (i, x_min + i * tile_data_width, x_min + (i + 1) * tile_data_width)
        for i in range(num_tiles)
    ]


def visible_tile_indices(
    viewport_x_min: float,
    viewport_x_max: float,
    data_x_min: float,
    data_x_max: float,
    zoom: int,
) -> list[int]:
    """Determine which tile indices are visible in the current viewport."""
    num_tiles = 2**zoom
    tile_data_width = (data_x_max - data_x_min) / num_tiles

    first = int((viewport_x_min - data_x_min) / tile_data_width)
    last = int((viewport_x_max - data_x_min) / tile_data_width)

    first = max(0, first)
    last = min(num_tiles - 1, last)

    return list(range(first, last + 1))
