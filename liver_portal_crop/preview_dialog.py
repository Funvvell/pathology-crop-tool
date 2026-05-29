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
