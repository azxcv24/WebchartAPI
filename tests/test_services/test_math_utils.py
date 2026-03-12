"""Tests for coordinate mapping utilities."""

from webchart.core.math_utils import Viewport, visible_tile_indices, compute_tile_ranges


class TestViewport:
    def test_pixel_to_data(self):
        vp = Viewport(
            pixel_width=1000, pixel_height=500,
            x_min=0.0, x_max=100.0, y_min=0.0, y_max=50.0,
        )
        dx, dy = vp.pixel_to_data(500, 250)
        assert dx == 50.0
        assert dy == 25.0

    def test_data_to_pixel(self):
        vp = Viewport(
            pixel_width=1000, pixel_height=500,
            x_min=0.0, x_max=100.0, y_min=0.0, y_max=50.0,
        )
        px, py = vp.data_to_pixel(50.0, 25.0)
        assert px == 500.0
        assert py == 250.0

    def test_roundtrip(self):
        vp = Viewport(
            pixel_width=800, pixel_height=600,
            x_min=10.0, x_max=90.0, y_min=-5.0, y_max=45.0,
        )
        px, py = 400.0, 300.0
        dx, dy = vp.pixel_to_data(px, py)
        px2, py2 = vp.data_to_pixel(dx, dy)
        assert abs(px - px2) < 0.01
        assert abs(py - py2) < 0.01


class TestTileRanges:
    def test_zoom_0(self):
        ranges = compute_tile_ranges(0.0, 100.0, zoom=0)
        assert len(ranges) == 1
        assert ranges[0] == (0, 0.0, 100.0)

    def test_zoom_2(self):
        ranges = compute_tile_ranges(0.0, 100.0, zoom=2)
        assert len(ranges) == 4
        assert ranges[0] == (0, 0.0, 25.0)
        assert ranges[3] == (3, 75.0, 100.0)


class TestVisibleTiles:
    def test_full_viewport(self):
        indices = visible_tile_indices(0.0, 100.0, 0.0, 100.0, zoom=2)
        assert indices == [0, 1, 2, 3]

    def test_partial_viewport(self):
        indices = visible_tile_indices(30.0, 60.0, 0.0, 100.0, zoom=2)
        assert 1 in indices
        assert 2 in indices

    def test_edge_case_single_tile(self):
        indices = visible_tile_indices(0.0, 100.0, 0.0, 100.0, zoom=0)
        assert indices == [0]
