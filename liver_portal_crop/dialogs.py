"""设置对话框和图片查看对话框。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap, QWheelEvent, QTransform
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QScrollArea, QSizePolicy,
    QVBoxLayout, QDialogButtonBox, QWidget,
    QTextEdit, QTabWidget, QTableWidget, QTableWidgetItem,
    QHeaderView,
)

from liver_portal_crop.exporter import CropConfig


class SettingsDialog(QDialog):
    """导出设置对话框（仅输出目录，尺寸由底栏浮动框决定）。"""

    def __init__(self, config: CropConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("导出设置")
        self.resize(400, 120)
        self._config = config
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        form = QFormLayout()

        # 输出目录
        dir_layout = QHBoxLayout()
        self._dir_edit = QLineEdit(str(self._config.output_dir))
        self._dir_edit.setReadOnly(True)
        browse_btn = QPushButton("浏览...")
        browse_btn.clicked.connect(self._browse_dir)
        dir_layout.addWidget(self._dir_edit)
        dir_layout.addWidget(browse_btn)
        form.addRow("输出目录:", dir_layout)

        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse_dir(self) -> None:
        dir_path = QFileDialog.getExistingDirectory(
            self, "选择输出目录", self._dir_edit.text(),
        )
        if dir_path:
            self._dir_edit.setText(dir_path)

    def get_config(self) -> CropConfig:
        return CropConfig(
            output_dir=Path(self._dir_edit.text()),
        )


class ImageViewDialog(QDialog):
    """图片查看对话框 — 显示 SDPC 标签图 / 宏观图。

    支持滚轮缩放和保存为 PNG/JPEG。
    """

    def __init__(self, image: np.ndarray, title: str = "图片查看", parent=None):
        """
        Args:
            image: RGB numpy array (H, W, 3), dtype=uint8
            title: 对话框标题
        """
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(720, 560)
        self._original_image = image
        self._zoom = 1.0
        self._rotation = 0  # 当前旋转角度（0/90/180/270）
        self._setup_ui()
        self._update_pixmap()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # 工具栏
        toolbar = QHBoxLayout()
        self._zoom_in_btn = QPushButton("放大 +")
        self._zoom_in_btn.clicked.connect(lambda: self._zoom_by(1.25))
        self._zoom_out_btn = QPushButton("缩小 −")
        self._zoom_out_btn.clicked.connect(lambda: self._zoom_by(0.8))
        self._fit_btn = QPushButton("适应窗口")
        self._fit_btn.clicked.connect(self._fit_to_window)
        self._rotate_left_btn = QPushButton("左转 90\u00b0")
        self._rotate_left_btn.clicked.connect(lambda: self._rotate(-90))
        self._rotate_right_btn = QPushButton("右转 90\u00b0")
        self._rotate_right_btn.clicked.connect(lambda: self._rotate(90))
        self._zoom_label = QLabel("100%")
        self._zoom_label.setMinimumWidth(50)
        self._zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._save_btn = QPushButton("保存...")
        self._save_btn.clicked.connect(self._save_image)

        toolbar.addWidget(self._zoom_in_btn)
        toolbar.addWidget(self._zoom_out_btn)
        toolbar.addWidget(self._fit_btn)
        toolbar.addWidget(self._rotate_left_btn)
        toolbar.addWidget(self._rotate_right_btn)
        toolbar.addWidget(self._zoom_label)
        toolbar.addStretch()
        toolbar.addWidget(self._save_btn)
        layout.addLayout(toolbar)

        # 尺寸信息
        h, w = self._original_image.shape[:2]
        self._info_label = QLabel(f"{w} × {h} px")
        self._info_label.setObjectName("statusLabel")
        layout.addWidget(self._info_label)

        # 滚动区域
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(False)
        self._scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._image_label = QLabel()
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored,
        )
        self._scroll.setWidget(self._image_label)
        layout.addWidget(self._scroll, 1)

        # 关闭按钮
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

    def _numpy_to_qpixmap(self, img: np.ndarray) -> QPixmap:
        h, w = img.shape[:2]
        if img.ndim == 3 and img.shape[2] == 3:
            qimg = QImage(img.data, w, h, w * 3, QImage.Format.Format_RGB888)
        else:
            qimg = QImage(img.data, w, h, w, QImage.Format.Format_Grayscale8)
        return QPixmap.fromImage(qimg.copy())

    def _update_pixmap(self):
        img = self._original_image
        h, w = img.shape[:2]
        pix = self._numpy_to_qpixmap(img)

        # 使用 Qt 原生旋转（避免 numpy rot90 内存问题）
        if self._rotation != 0:
            transform = QTransform().rotate(self._rotation)
            pix = pix.transformed(transform, Qt.TransformationMode.SmoothTransformation)

        rh, rw = pix.height(), pix.width()
        self._info_label.setText(f"{rw} \u00d7 {rh} px" + (f"  ({self._rotation}\u00b0)" if self._rotation else ""))
        if self._zoom != 1.0:
            pix = pix.scaled(
                int(pix.width() * self._zoom),
                int(pix.height() * self._zoom),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        self._image_label.setPixmap(pix)
        self._image_label.resize(pix.size())
        self._zoom_label.setText(f"{self._zoom * 100:.0f}%")

    def _rotate(self, degrees: int):
        """旋转图像（+90 右转，-90 左转）。"""
        self._rotation = (self._rotation + degrees) % 360
        self._zoom = 1.0
        self._update_pixmap()

    def _zoom_by(self, factor: float):
        self._zoom = max(0.1, min(10.0, self._zoom * factor))
        self._update_pixmap()

    def _fit_to_window(self):
        h, w = self._original_image.shape[:2]
        # 旋转 90°/270° 时宽高互换
        if self._rotation in (90, 270):
            w, h = h, w
        vw = self._scroll.viewport().width() - 4
        vh = self._scroll.viewport().height() - 4
        self._zoom = min(vw / w, vh / h, 1.0)
        self._update_pixmap()

    def _save_image(self):
        from PIL import Image as PILImage
        path, _ = QFileDialog.getSaveFileName(
            self, "保存图片", "", "PNG (*.png);;JPEG (*.jpg);;所有文件 (*)",
        )
        if path:
            img = PILImage.fromarray(self._original_image)
            img.save(path)

    def wheelEvent(self, event: QWheelEvent):
        delta = event.angleDelta().y()
        if delta > 0:
            self._zoom_by(1.15)
        elif delta < 0:
            self._zoom_by(1 / 1.15)


class SliceInfoDialog(QDialog):
    """切片信息对话框 — 显示 SDPC 文件的元数据。"""

    _LABEL_MAP = {
        # PicHead
        "version": "版本",
        "file_size": "文件大小",
        "src_width": "原始宽度",
        "src_height": "原始高度",
        "slice_width": "切片宽度",
        "slice_height": "切片高度",
        "hierarchy": "金字塔层数",
        "scale": "缩放比例",
        "ruler": "微米/像素 (mpp)",
        "rate": "倍率",
        "quality": "质量",
        "slice_format": "切片格式",
        "person_infor": "患者信息标记",
        "macrograph": "标签图数量",
        # PersonInfo
        "pathology_id": "病理号",
        "name": "姓名",
        "sex": "性别",
        "age": "年龄",
        "departments": "科室",
        "hospital": "医院",
        "submitted_samples": "送检标本",
        "clinical_diagnosis": "临床诊断",
        "pathological_diagnosis": "病理诊断",
        "report_date": "报告日期",
        "attending_doctor": "主治医生",
        "remark": "备注",
        # ExtraInfo
        "model": "扫描仪型号",
        "serial": "序列号",
        "barcode": "条码",
        "fusion_layer": "融合层",
        "step": "步长",
        "scan_time": "扫描时间",
        "camera_gamma": "相机 Gamma",
        "camera_exposure": "曝光时间",
        "camera_gain": "相机增益",
    }

    def __init__(self, metadata: dict, title: str = "切片信息", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(480, 420)
        self._metadata = metadata
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        tabs = QTabWidget()

        # ── 基本信息 Tab（PicHead） ──
        pic_head = self._metadata.get("pic_head") or {}
        tabs.addTab(self._make_table(pic_head), "基本参数")

        # ── 患者信息 Tab（PersonInfo） ──
        person = self._metadata.get("person_info")
        if person:
            tabs.addTab(self._make_table(person), "患者信息")

        # ── 扫描信息 Tab（ExtraInfo） ──
        extra = self._metadata.get("extra_info")
        if extra:
            tabs.addTab(self._make_table(extra), "扫描信息")

        layout.addWidget(tabs, 1)

        # 关闭按钮
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

    def _make_table(self, data: dict) -> QTableWidget:
        """将 dict 渲染为两列表格（名称 / 值）。"""
        table = QTableWidget(len(data), 2)
        table.setHorizontalHeaderLabels(["属性", "值"])
        table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)

        for row, (key, value) in enumerate(data.items()):
            label = self._LABEL_MAP.get(key, key)

            # 格式化特殊值
            if isinstance(value, list):
                display = ", ".join(str(v) for v in value)
            elif isinstance(value, float):
                display = f"{value:.4f}" if abs(value) < 1 else f"{value:.2f}"
            elif value is None:
                display = "—"
            else:
                display = str(value)

            name_item = QTableWidgetItem(label)
            name_item.setFlags(
                name_item.flags() & ~Qt.ItemFlag.ItemIsEditable
            )
            val_item = QTableWidgetItem(display)
            val_item.setFlags(
                val_item.flags() & ~Qt.ItemFlag.ItemIsEditable
            )
            table.setItem(row, 0, name_item)
            table.setItem(row, 1, val_item)

        return table
