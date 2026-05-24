import pytest
from liver_portal_crop.utils import map_thumb_to_full, center_crop_rect


class TestMapThumbToFull:
    def test_basic_mapping(self):
        # 缩略图 1000×800, 全分辨率 10000×8000
        # 缩略图上 (100, 100, 200, 200) → 全分辨率 (1000, 1000, 2000, 2000)
        result = map_thumb_to_full(
            (100, 100, 200, 200),
            (1000, 800),
            (10000, 8000)
        )
        assert result == (1000, 1000, 2000, 2000)

    def test_identity_scale(self):
        # 缩略图 = 全分辨率，1:1
        result = map_thumb_to_full(
            (50, 50, 100, 100),
            (2000, 2000),
            (2000, 2000)
        )
        assert result == (50, 50, 100, 100)

    def test_zero_size_thumb(self):
        with pytest.raises(ValueError):
            map_thumb_to_full((0, 0, 10, 10), (0, 100), (1000, 1000))

    def test_non_integer_scale(self):
        # 缩略图 3×3, 全分辨率 10×10 → 非整数缩放
        result = map_thumb_to_full(
            (1, 1, 1, 1), (3, 3), (10, 10)
        )
        # 1 * (10/3) = 3.33 → round to 3
        assert result == (3, 3, 3, 3)


class TestCenterCropRect:
    def test_basic_crop(self):
        # 图像 2000×2000, 中心 (1000,1000), 裁剪 500×500
        result = center_crop_rect(1000, 1000, 500, 500, 2000, 2000)
        assert result == (750, 750, 500, 500)

    def test_crop_near_left_edge(self):
        # 图像 1000×1000, 中心 (10, 500), 裁剪 200×200
        result = center_crop_rect(10, 500, 200, 200, 1000, 1000)
        assert result == (0, 400, 110, 200)

    def test_crop_near_right_edge(self):
        # 图像 1000×1000, 中心 (990, 500), 裁剪 200×200
        result = center_crop_rect(990, 500, 200, 200, 1000, 1000)
        assert result == (890, 400, 110, 200)

    def test_crop_larger_than_image(self):
        # 裁剪大于图像 → clamp 到图像尺寸
        result = center_crop_rect(500, 500, 3000, 3000, 1000, 1000)
        assert result == (0, 0, 1000, 1000)

    def test_crop_top_edge(self):
        # 图像 1000×1000, 中心 (500, 10), 裁剪 200×200
        result = center_crop_rect(500, 10, 200, 200, 1000, 1000)
        assert result == (400, 0, 200, 110)

    def test_odd_crop_size(self):
        # 奇数裁剪尺寸，中心偏移处理
        result = center_crop_rect(100, 100, 101, 101, 500, 500)
        # half = 50, x1=50, y1=50, x2=151, y2=151
        assert result == (50, 50, 101, 101)
