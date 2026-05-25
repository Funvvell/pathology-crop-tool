from pathlib import Path

import pytest
from liver_portal_crop.exporter import BatchExporter, CropConfig
from liver_portal_crop.roi import ROIModel
from liver_portal_crop.utils import center_crop_rect


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


class TestCenterCrop:
    def test_basic_crop(self):
        crop = center_crop_rect(1000, 1000, 1024, 1024, 10000, 8000)
        assert crop == (488, 488, 1024, 1024)

    def test_crop_at_edge(self):
        crop = center_crop_rect(50, 50, 1024, 1024, 10000, 8000)
        assert crop[0] >= 0
        assert crop[1] >= 0
        assert crop[0] + crop[2] <= 10000
        assert crop[1] + crop[3] <= 8000

    def test_export_naming(self):
        """验证输出文件名格式。"""
        roi = ROIModel(
            slide_path=Path("liver_slide_001.sdpc"),
            x=0, y=0, w=10, h=10,
        )
        expected = f"{roi.slide_path.stem}_ROI_{0:04d}.tiff"
        assert expected == "liver_slide_001_ROI_0000.tiff"
