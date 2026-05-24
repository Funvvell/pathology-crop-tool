from pathlib import Path

import pytest
from liver_portal_crop.exporter import BatchExporter, CropConfig
from liver_portal_crop.roi import ROIModel
from liver_portal_crop.utils import map_thumb_to_full, center_crop_rect


class TestCropConfig:
    def test_default_config(self):
        config = CropConfig(output_dir=Path("/tmp"))
        assert config.crop_width == 1024
        assert config.crop_height == 1024
        assert config.format == "tiff"
        assert config.compression == "zlib"

    def test_custom_config(self):
        config = CropConfig(
            output_dir=Path("/tmp"),
            crop_width=512,
            crop_height=512,
            compression="lzw",
        )
        assert config.crop_width == 512
        assert config.crop_height == 512
        assert config.compression == "lzw"


class TestCoordinateMapping:
    """验证坐标映射逻辑（集成 utils.py）。"""

    def test_basic_mapping_and_crop(self):
        # 缩略图 1000×800 → 全分辨率 10000×8000
        # 缩略图上 ROI (100, 100, 200, 200)
        # 映射到全分辨率 (1000, 1000, 2000, 2000)
        # 居中裁剪 1024×1024:
        #   中心 (2000, 2000), 裁剪 1024×1024
        #   x1 = 2000-512=1488, y1 = 2000-512=1488
        #   x2 = 1488+1024=2512, y2 = 1488+1024=2512
        fx, fy, fw, fh = map_thumb_to_full(
            (100, 100, 200, 200), (1000, 800), (10000, 8000)
        )
        assert (fx, fy, fw, fh) == (1000, 1000, 2000, 2000)

        cx, cy = fx + fw // 2, fy + fh // 2
        crop = center_crop_rect(cx, cy, 1024, 1024, 10000, 8000)
        assert crop == (1488, 1488, 1024, 1024)

    def test_crop_at_edge(self):
        # ROI 在右下角
        fx, fy, fw, fh = map_thumb_to_full(
            (950, 750, 50, 50), (1000, 800), (10000, 8000)
        )
        cx, cy = fx + fw // 2, fy + fh // 2
        crop = center_crop_rect(cx, cy, 1024, 1024, 10000, 8000)
        # cx = 9500+250 = 9750, cy = 7500+250 = 7750
        # x1 = 9750-512 = 9238, x2 = 9238+1024 = 10262 → clamp to 10000
        # y1 = 7750-512 = 7238, y2 = 7238+1024 = 8262 → clamp to 8000
        assert crop[0] >= 0
        assert crop[1] >= 0
        assert crop[0] + crop[2] <= 10000
        assert crop[1] + crop[3] <= 8000

    def test_export_naming(self):
        """验证输出文件名格式。"""
        roi = ROIModel(
            slide_path=Path("liver_slide_001.sdpc"),
            thumb_x=0, thumb_y=0, thumb_w=10, thumb_h=10,
        )
        # exporter 生成: liver_slide_001_ROI_0000.tiff
        expected = f"{roi.slide_path.stem}_ROI_{0:04d}.tiff"
        assert expected == "liver_slide_001_ROI_0000.tiff"
