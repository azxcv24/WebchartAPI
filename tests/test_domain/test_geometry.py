"""domain.geometry 값 객체 테스트."""

import math

import pytest

from webchart.domain.geometry import PixelLattice, ScreenTransform, TileAddress, XRange


class TestXRange:
    def test_width_and_half_open_membership(self):
        span = XRange(10.0, 20.0)
        assert span.width == 10.0
        assert span.contains(10.0)
        assert not span.contains(20.0)  # 끝은 포함하지 않는다

    @pytest.mark.parametrize(
        "start, end", [(1.0, 1.0), (2.0, 1.0), (0.0, math.inf), (math.nan, 1.0)]
    )
    def test_rejects_empty_or_non_finite(self, start, end):
        with pytest.raises(ValueError):
            XRange(start, end)

    def test_overlaps_excludes_touching_edges(self):
        assert XRange(0, 10).overlaps(XRange(9, 20))
        assert not XRange(0, 10).overlaps(XRange(10, 20))


class TestTileAddress:
    def test_tile_id_keeps_v1_format(self):
        assert TileAddress(3, 5).tile_id == "3-5-0"

    @pytest.mark.parametrize("zoom, index", [(-1, 0), (25, 0), (2, 4), (2, -1)])
    def test_rejects_out_of_range(self, zoom, index):
        with pytest.raises(ValueError):
            TileAddress(zoom, index)


class TestPixelLattice:
    def lattice(self) -> PixelLattice:
        return PixelLattice(XRange(0.0, 100.0), tile_size_px=512)

    def test_tile_ranges_partition_domain(self):
        lattice = self.lattice()
        ranges = [lattice.tile_range(TileAddress(2, i)) for i in range(4)]
        assert ranges[0] == XRange(0.0, 25.0)
        assert ranges[3] == XRange(75.0, 100.0)
        # 인접 타일 사이에 틈이나 겹침이 없어야 한다
        for left, right in zip(ranges, ranges[1:]):
            assert left.end == right.start

    def test_last_tile_ends_exactly_at_domain_end(self):
        # 3등분처럼 나누어떨어지지 않는 폭에서도 마지막 끝은 domain.end와 같아야 한다
        lattice = PixelLattice(XRange(0.0, 0.3), tile_size_px=256)
        for zoom in range(0, 12):
            last = lattice.tile_range(TileAddress(zoom, 2**zoom - 1))
            assert last.end == lattice.domain.end

    def test_from_data_bounds_includes_max_point(self):
        lattice = PixelLattice.from_data_bounds(0.0, 100.0, 512)
        assert lattice.domain.contains(100.0)
        assert lattice.tile_index_at(100.0, zoom=2) == 3

    def test_from_data_bounds_widens_single_value(self):
        lattice = PixelLattice.from_data_bounds(5.0, 5.0, 512)
        assert lattice.domain == XRange(4.5, 5.5)

    @pytest.mark.parametrize("size", [0, 3, 500, -512])
    def test_rejects_non_power_of_two_tile_size(self, size):
        with pytest.raises(ValueError):
            PixelLattice(XRange(0, 1), tile_size_px=size)

    def test_tiles_covering_full_view(self):
        tiles = self.lattice().tiles_covering(XRange(0.0, 100.0), zoom=2)
        assert [t.index for t in tiles] == [0, 1, 2, 3]

    def test_tiles_covering_partial_view(self):
        tiles = self.lattice().tiles_covering(XRange(30.0, 60.0), zoom=2)
        assert [t.index for t in tiles] == [1, 2]

    def test_tiles_covering_view_ending_on_boundary(self):
        # [0, 50)은 50 이상의 점을 포함하지 않으므로 인덱스 2 타일은 필요 없다
        tiles = self.lattice().tiles_covering(XRange(0.0, 50.0), zoom=2)
        assert [t.index for t in tiles] == [0, 1]

    def test_tiles_covering_view_outside_domain(self):
        assert self.lattice().tiles_covering(XRange(200.0, 300.0), zoom=2) == []

    def test_tiles_covering_view_wider_than_domain(self):
        tiles = self.lattice().tiles_covering(XRange(-50.0, 500.0), zoom=1)
        assert [t.index for t in tiles] == [0, 1]

    def test_with_neighbors_clamps_to_lattice(self):
        lattice = self.lattice()
        expanded = lattice.with_neighbors([TileAddress(2, 0), TileAddress(2, 3)], radius=2)
        assert [t.index for t in expanded] == [0, 1, 2, 3]

    def test_with_neighbors_zero_radius_is_identity(self):
        tiles = [TileAddress(3, 4)]
        assert self.lattice().with_neighbors(tiles, radius=0) == tiles


class TestScreenTransform:
    def transform(self) -> ScreenTransform:
        return ScreenTransform(
            x_view=XRange(0.0, 100.0), y_low=0.0, y_high=50.0, width_px=1000, height_px=500
        )

    def test_to_data_flips_y(self):
        assert self.transform().to_data(500, 250) == (50.0, 25.0)
        # 픽셀 y=0은 화면 맨 위 = 데이터 y 최댓값
        assert self.transform().to_data(0, 0) == (0.0, 50.0)

    def test_to_pixel(self):
        assert self.transform().to_pixel(50.0, 25.0) == (500.0, 250.0)

    def test_roundtrip(self):
        transform = ScreenTransform(
            x_view=XRange(10.0, 90.0), y_low=-5.0, y_high=45.0, width_px=800, height_px=600
        )
        px, py = transform.to_pixel(*transform.to_data(400.0, 300.0))
        assert px == pytest.approx(400.0)
        assert py == pytest.approx(300.0)

    def test_rejects_degenerate_y(self):
        with pytest.raises(ValueError):
            ScreenTransform(x_view=XRange(0, 1), y_low=1.0, y_high=1.0, width_px=10, height_px=10)
