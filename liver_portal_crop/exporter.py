"""BatchExporter — 批量 ROI 导出为 TIFF。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import tifffile

from PySide6.QtCore import QObject, Signal

from liver_portal_crop.reader import SDPCReader
from liver_portal_crop.roi import ROIModel
from liver_portal_crop.utils import center_crop_rect


@dataclass
class CropConfig:
    """输出裁剪配置。"""
    output_dir: Path
    crop_width: int = 1024
    crop_height: int = 1024
    format: str = "tiff"
    compression: str = "zlib"


class BatchExporter(QObject):
    """在 QThread 中运行批量导出。

    ROI 坐标已为 level 0（全分辨率）坐标，直接居中裁剪后输出。
    """

    progress = Signal(int, int)       # current, total
    file_done = Signal(str, str)      # output_path, status ("ok" / "error:msg")
    finished = Signal()

    def __init__(self, config: CropConfig, parent=None):
        super().__init__(parent)
        self._config = config
        self._cancel_flag = False

    def cancel(self) -> None:
        self._cancel_flag = True

    def run(
        self,
        rois: list[ROIModel],
        readers: dict[Path, SDPCReader],
    ) -> None:
        """执行批量导出。

        Args:
            rois: 全部 ROI 列表（坐标 = level 0 全分辨率）
            readers: {slide_path: SDPCReader} 映射
        """
        total = len(rois)
        self._config.output_dir.mkdir(parents=True, exist_ok=True)

        for idx, roi in enumerate(rois):
            if self._cancel_flag:
                break

            self.progress.emit(idx + 1, total)

            try:
                reader = readers.get(roi.slide_path)
                if reader is None:
                    self.file_done.emit(
                        f"{roi.slide_path.stem}_ROI_{idx:04d}.tiff",
                        "error:slide not loaded",
                    )
                    continue

                # ROI 坐标已经是 level 0 全分辨率坐标
                # 以 ROI 中心为中点，裁剪为配置尺寸
                cx = roi.thumb_x + roi.thumb_w // 2
                cy = roi.thumb_y + roi.thumb_h // 2
                crop_x, crop_y, crop_w, crop_h = center_crop_rect(
                    cx, cy,
                    self._config.crop_width,
                    self._config.crop_height,
                    reader.full_width,
                    reader.full_height,
                )

                # 提取
                region = reader.extract_region(
                    crop_x, crop_y, crop_w, crop_h, level=0,
                )

                # 保存 TIFF
                output_name = f"{roi.slide_path.stem}_ROI_{idx:04d}.tiff"
                output_path = self._config.output_dir / output_name
                tifffile.imwrite(
                    str(output_path),
                    region,
                    compression=self._config.compression,
                )

                self.file_done.emit(str(output_path), "ok")

            except Exception as e:
                self.file_done.emit(
                    f"{roi.slide_path.stem}_ROI_{idx:04d}.tiff",
                    f"error:{e}",
                )

        self.finished.emit()
