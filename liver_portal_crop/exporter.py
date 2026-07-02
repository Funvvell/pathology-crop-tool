"""BatchExporter — 批量 ROI 导出为 TIFF。"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import tifffile

from PySide6.QtCore import QObject, Signal

from liver_portal_crop.reader import SDPCReader
from liver_portal_crop.roi import ROIModel
from liver_portal_crop.utils import center_crop_rect

logger = logging.getLogger(__name__)


@dataclass
class CropConfig:
    """输出裁剪配置。"""
    output_dir: Path
    crop_width: int = 1024
    crop_height: int = 1024
    format: str = "tiff"
    compression: str = "zlib"
    mag_label: str = ""  # 用于文件名，如 "20x"


class BatchExporter(QObject):
    """在 QThread 中运行批量导出。

    接收文件路径字典，在后台线程打开 reader 并导出。
    """

    progress = Signal(int, int)       # current, total
    file_done = Signal(str, str)      # output_path, status ("ok" / "error:msg")
    finished = Signal()

    def __init__(self, config: CropConfig, parent=None):
        super().__init__(parent)
        self._config = config
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(
        self,
        rois: list[ROIModel],
        path_to_reader: dict[Path, SDPCReader | str],
    ) -> None:
        """执行批量导出。

        Args:
            rois: 全部 ROI 列表（坐标 = level 0 全分辨率）
            path_to_reader: {slide_path: SDPCReader} 或 {slide_path: "file/path"}
        """
        total = len(rois)
        self._config.output_dir.mkdir(parents=True, exist_ok=True)
        logger.info("导出开始: %d 个 ROI, 输出: %s", total, self._config.output_dir)

        # 按文件分组
        groups: dict[Path, list[tuple[int, ROIModel]]] = defaultdict(list)
        for idx, roi in enumerate(rois):
            groups[roi.slide_path].append((idx, roi))

        # 在后台线程打开 reader（避免主线程卡顿）
        readers: dict[Path, SDPCReader] = {}
        for path in groups:
            val = path_to_reader.get(path)
            if val is None:
                logger.warning("导出跳过: 无 reader 映射 — %s", path)
                continue
            if isinstance(val, SDPCReader):
                readers[path] = val
                logger.info("导出复用已有 reader: %s", path.name)
            else:
                try:
                    readers[path] = SDPCReader(val)
                    logger.info("导出新建 reader: %s", path.name)
                except Exception:
                    logger.warning("导出跳过: 无法打开文件 — %s", path, exc_info=True)

        # 预计算全局索引 → 局部索引映射（避免 O(N²) 线性查找）
        global_to_local: dict[int, int] = {}
        for _path, file_rois in groups.items():
            for local_n, (global_n, _) in enumerate(file_rois):
                global_to_local[global_n] = local_n

        for idx, roi in enumerate(rois):
            if self._cancel_event.is_set():
                break

            self.progress.emit(idx + 1, total)

            try:
                reader = readers.get(roi.slide_path)
                if reader is None:
                    local_idx = global_to_local.get(idx, idx)
                    self.file_done.emit(
                        f"{roi.slide_path.stem}_ROI_{local_idx:04d}.tiff",
                        "error:slide not loaded",
                    )
                    continue

                cx = roi.x + roi.w // 2
                cy = roi.y + roi.h // 2
                crop_x, crop_y, crop_w, crop_h = center_crop_rect(
                    cx, cy,
                    roi.w, roi.h,
                    reader.full_width,
                    reader.full_height,
                )

                region = reader.extract_region(
                    crop_x, crop_y, crop_w, crop_h, level=0,
                )

                local_idx = global_to_local.get(idx, idx)

                mag_suffix = f"_{self._config.mag_label}" if self._config.mag_label else ""
                output_name = f"{roi.slide_path.stem}_ROI_{local_idx:04d}{mag_suffix}.tiff"
                output_path = self._config.output_dir / output_name
                tifffile.imwrite(
                    str(output_path), region,
                    compression=self._config.compression,
                )

                self.file_done.emit(str(output_path), "ok")

            except Exception as e:
                logger.warning("导出失败 ROI #%d: %s", idx, e)
                local_idx = global_to_local.get(idx, idx)
                self.file_done.emit(
                    f"{roi.slide_path.stem}_ROI_{local_idx:04d}.tiff",
                    f"error:{e}",
                )

        logger.info("导出完成: %d 个 ROI", total)
        self.finished.emit()
