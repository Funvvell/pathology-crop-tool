"""ROI 预览选择对话框 — 缩略图网格 + checkbox 选择 + 双击全分辨率预览。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from PySide6.QtCore import Qt, QRectF, QThread, Signal, QSize
from PySide6.QtGui import QImage, QPixmap, QPainter, QPen, QBrush, QColor
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFileDialog, QFormLayout, QGraphicsPixmapItem,
    QGraphicsScene, QGraphicsView, QGridLayout, QHBoxLayout, QLabel,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy,
    QSlider, QVBoxLayout, QWidget, QComboBox,
)

from liver_portal_crop.reader import SDPCReader
from liver_portal_crop.roi import ROIModel


class ROICardWidget(QWidget):
    """单个 ROI 缩略图卡片：缩略图 + 左上角 checkbox + 底部尺寸标签。"""

    double_clicked = Signal(str)  # roi_id

    def __init__(self, roi_id: str, roi_w: int, roi_h: int,
                 thumb_size: int = 120, parent=None):
        super().__init__(parent)
        self._roi_id = roi_id
        self._thumb_size = thumb_size
        self._setup_ui(roi_w, roi_h)

    def _setup_ui(self, roi_w: int, roi_h: int) -> None:
        self.setFixedSize(self._thumb_size + 16, self._thumb_size + 40)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 2)
        layout.setSpacing(2)

        # 缩略图容器（带 checkbox 叠加）
        container = QWidget()
        container.setFixedSize(self._thumb_size, self._thumb_size)
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)

        self._thumb_label = QLabel()
        self._thumb_label.setFixedSize(self._thumb_size, self._thumb_size)
        self._thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb_label.setStyleSheet(
            "background: #1e293b; border: 1px solid #334155; border-radius: 4px;"
        )
        self._thumb_label.setText("加载中...")
        container_layout.addWidget(self._thumb_label)

        # checkbox 叠加在左上角
        self._checkbox = QCheckBox(container)
        self._checkbox.move(4, 4)
        self._checkbox.setStyleSheet(
            "QCheckBox::indicator { width: 16px; height: 16px; }"
        )

        layout.addWidget(container)

        # 尺寸标签
        size_label = QLabel(f"{roi_w}x{roi_h}")
        size_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        size_label.setStyleSheet("font-size: 10px; color: #94a3b8;")
        layout.addWidget(size_label)

    def set_thumbnail(self, pixmap: QPixmap) -> None:
        scaled = pixmap.scaled(
            self._thumb_size, self._thumb_size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._thumb_label.setPixmap(scaled)

    def set_loading_error(self) -> None:
        self._thumb_label.setText("加载失败")
        self._thumb_label.setStyleSheet(
            "background: #1e293b; border: 1px solid #ef4444; "
            "border-radius: 4px; color: #ef4444; font-size: 11px;"
        )

    def is_checked(self) -> bool:
        return self._checkbox.isChecked()

    def set_checked(self, checked: bool) -> None:
        self._checkbox.setChecked(checked)

    @property
    def roi_id(self) -> str:
        return self._roi_id

    def mouseDoubleClickEvent(self, event) -> None:
        self.double_clicked.emit(self._roi_id)
        super().mouseDoubleClickEvent(event)


class ThumbnailWorker(QThread):
    """后台生成 ROI 缩略图。"""

    thumbnail_ready = Signal(str, QPixmap)  # roi_id, pixmap
    finished_all = Signal()
    progress = Signal(int, int)  # current, total

    def __init__(self, rois: list[ROIModel],
                 readers: dict[Path, SDPCReader],
                 thumb_size: int = 120,
                 parent=None):
        super().__init__(parent)
        self._rois = rois
        self._readers = readers
        self._thumb_size = thumb_size
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        total = len(self._rois)
        for idx, roi in enumerate(self._rois):
            if self._cancel:
                break
            self.progress.emit(idx + 1, total)
            try:
                reader = self._readers.get(roi.slide_path)
                if reader is None:
                    continue
                pixmap = self._generate_thumbnail(reader, roi)
                if pixmap:
                    self.thumbnail_ready.emit(roi.id, pixmap)
            except Exception:
                pass
        self.finished_all.emit()

    def _generate_thumbnail(self, reader: SDPCReader, roi: ROIModel) -> QPixmap | None:
        """选择合适的金字塔层级生成缩略图。"""
        level = self._pick_level(reader, roi.w, roi.h, self._thumb_size)
        ds = reader.levels[level].downsample

        # ROI 坐标在 level 0，转为 target level 坐标
        lx = int(roi.x / ds)
        ly = int(roi.y / ds)
        lw = max(1, int(roi.w / ds))
        lh = max(1, int(roi.h / ds))

        # clamp to level bounds
        lv_w, lv_h = reader.levels[level].width, reader.levels[level].height
        lx = max(0, min(lx, lv_w - 1))
        ly = max(0, min(ly, lv_h - 1))
        lw = min(lw, lv_w - lx)
        lh = min(lh, lv_h - ly)
        if lw <= 0 or lh <= 0:
            return None

        region = reader._read_level_region(level, lx, ly, lw, lh)
        img = Image.fromarray(region)
        img.thumbnail((self._thumb_size, self._thumb_size), Image.Resampling.LANCZOS)

        # PIL → QPixmap
        rgb = img.convert("RGB")
        data = rgb.tobytes()
        qimg = QImage(data, rgb.width, rgb.height,
                       rgb.width * 3, QImage.Format.Format_RGB888)
        return QPixmap.fromImage(qimg)

    @staticmethod
    def _pick_level(reader: SDPCReader, roi_w: int, roi_h: int,
                    target_size: int) -> int:
        """选择能满足缩略图尺寸的最高金字塔层级。"""
        for level in range(reader.level_count - 1, -1, -1):
            ds = reader.levels[level].downsample
            lw = int(roi_w / ds)
            lh = int(roi_h / ds)
            if lw >= target_size and lh >= target_size:
                return level
        return 0
