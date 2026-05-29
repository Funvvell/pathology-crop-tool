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


class FullResPreviewDialog(QDialog):
    """双击缩略图弹出的全分辨率 ROI 预览。"""

    def __init__(self, reader: SDPCReader, roi: ROIModel, parent=None):
        super().__init__(parent)
        self.setWindowTitle(
            f"ROI 预览 — {roi.slide_path.name} "
            f"({roi.x}, {roi.y}) {roi.w}x{roi.h}"
        )
        self.resize(700, 600)
        self._reader = reader
        self._roi = roi
        self._setup_ui()
        self._load_image()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        info = QLabel(
            f"文件: {self._roi.slide_path.name}  |  "
            f"位置: ({self._roi.x}, {self._roi.y})  |  "
            f"尺寸: {self._roi.w} x {self._roi.h}"
        )
        info.setStyleSheet("font-size: 12px; color: #94a3b8; padding: 4px;")
        layout.addWidget(info)

        self._view = QGraphicsView()
        self._scene = QGraphicsScene()
        self._view.setScene(self._scene)
        self._view.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self._view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        layout.addWidget(self._view, 1)

        self._status = QLabel("加载中...")
        layout.addWidget(self._status)

    def _load_image(self) -> None:
        try:
            region = self._reader.extract_region(
                self._roi.x, self._roi.y, self._roi.w, self._roi.h, level=0,
            )
            h, w, ch = region.shape
            qimg = QImage(region.tobytes(), w, h, w * ch,
                          QImage.Format.Format_RGB888)
            pix = QPixmap.fromImage(qimg)
            self._scene.clear()
            self._scene.addPixmap(pix)
            self._scene.setSceneRect(0, 0, w, h)
            self._view.fitInView(self._scene.sceneRect(),
                                 Qt.AspectRatioMode.KeepAspectRatio)
            self._status.setText(f"全分辨率: {w}x{h}")
        except Exception as e:
            self._status.setText(f"加载失败: {e}")

    def wheelEvent(self, event) -> None:
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self._view.scale(factor, factor)


class ROIPreviewDialog(QDialog):
    """ROI 预览选择对话框 — 网格展示缩略图，支持勾选后导出。"""

    def __init__(self, rois: list[ROIModel],
                 readers: dict[Path, SDPCReader],
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("ROI 预览与导出")
        self.resize(800, 600)
        self._rois = rois
        self._readers = readers
        self._cards: dict[str, ROICardWidget] = {}
        self._selected_ids: set[str] = set()
        self._thumb_size = 120
        self._worker: ThumbnailWorker | None = None
        self._setup_ui()
        self._start_thumbnail_generation()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        # 工具栏
        toolbar = QHBoxLayout()
        self._select_all_btn = QPushButton("全选")
        self._select_all_btn.clicked.connect(self._select_all)
        toolbar.addWidget(self._select_all_btn)

        self._invert_btn = QPushButton("反选")
        self._invert_btn.clicked.connect(self._invert_selection)
        toolbar.addWidget(self._invert_btn)

        self._deselect_btn = QPushButton("全不选")
        self._deselect_btn.clicked.connect(self._deselect_all)
        toolbar.addWidget(self._deselect_btn)

        toolbar.addSpacing(16)
        toolbar.addWidget(QLabel("筛选:"))

        self._filter_cb = QComboBox()
        self._filter_cb.addItem("全部文件")
        slide_names = sorted(set(r.slide_path.name for r in self._rois))
        self._filter_cb.addItems(slide_names)
        self._filter_cb.currentTextChanged.connect(self._apply_filter)
        toolbar.addWidget(self._filter_cb)

        toolbar.addStretch()

        self._count_label = QLabel(f"已选: 0/{len(self._rois)}")
        toolbar.addWidget(self._count_label)

        layout.addLayout(toolbar)

        # 进度条（缩略图生成）
        self._thumb_progress = QProgressBar()
        self._thumb_progress.setRange(0, len(self._rois))
        self._thumb_progress.setValue(0)
        self._thumb_progress.setFormat("生成缩略图: %v/%m")
        layout.addWidget(self._thumb_progress)

        # 滚动区域
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._grid_container = QWidget()
        self._grid_layout = QGridLayout(self._grid_container)
        self._grid_layout.setSpacing(8)
        self._grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

        scroll.setWidget(self._grid_container)
        layout.addWidget(scroll, 1)

        # 底部：缩略图大小滑块 + 导出按钮
        bottom = QHBoxLayout()
        bottom.addWidget(QLabel("缩略图大小:"))

        self._size_slider = QSlider(Qt.Orientation.Horizontal)
        self._size_slider.setRange(80, 200)
        self._size_slider.setValue(self._thumb_size)
        self._size_slider.setFixedWidth(160)
        self._size_slider.valueChanged.connect(self._on_size_changed)
        bottom.addWidget(self._size_slider)

        self._size_label = QLabel(f"{self._thumb_size}px")
        bottom.addWidget(self._size_label)

        bottom.addStretch()

        self._export_btn = QPushButton("导出选中")
        self._export_btn.setObjectName("exportBtn")
        self._export_btn.clicked.connect(self._on_export)
        bottom.addWidget(self._export_btn)

        layout.addLayout(bottom)

        # 创建卡片（按文件分组）
        self._create_cards()

    def _create_cards(self) -> None:
        """按文件分组创建 ROI 卡片。"""
        # 按文件分组
        groups: dict[str, list[ROIModel]] = {}
        for roi in self._rois:
            name = roi.slide_path.name
            groups.setdefault(name, []).append(roi)

        row = 0
        for file_name in sorted(groups.keys()):
            # 文件名标题
            header = QLabel(f"  {file_name}")
            header.setStyleSheet(
                "font-weight: 600; font-size: 12px; color: #0891b2; "
                "padding: 4px 0; background: transparent;"
            )
            self._grid_layout.addWidget(header, row, 0, 1, -1)
            row += 1

            col = 0
            max_cols = 5  # will be recalculated on resize
            for roi in groups[file_name]:
                card = ROICardWidget(
                    roi.id, roi.w, roi.h, self._thumb_size
                )
                card._checkbox.stateChanged.connect(
                    lambda state, rid=roi.id: self._on_check_changed(rid, state)
                )
                card.double_clicked.connect(self._on_double_click)
                self._cards[roi.id] = card
                self._grid_layout.addWidget(card, row, col)
                col += 1
                if col >= max_cols:
                    col = 0
                    row += 1
            if col > 0:
                row += 1

    def _start_thumbnail_generation(self) -> None:
        """启动后台缩略图生成。"""
        self._worker = ThumbnailWorker(
            self._rois, self._readers, self._thumb_size
        )
        self._worker.thumbnail_ready.connect(self._on_thumbnail_ready)
        self._worker.progress.connect(self._on_thumb_progress)
        self._worker.finished_all.connect(self._on_thumbnails_done)
        self._worker.start()

    def _on_thumbnail_ready(self, roi_id: str, pixmap: QPixmap) -> None:
        card = self._cards.get(roi_id)
        if card:
            card.set_thumbnail(pixmap)

    def _on_thumb_progress(self, current: int, total: int) -> None:
        self._thumb_progress.setValue(current)

    def _on_thumbnails_done(self) -> None:
        self._thumb_progress.hide()

    def _on_check_changed(self, roi_id: str, state: int) -> None:
        if state == Qt.CheckState.Checked.value:
            self._selected_ids.add(roi_id)
        else:
            self._selected_ids.discard(roi_id)
        self._update_count()

    def _update_count(self) -> None:
        visible_count = sum(
            1 for c in self._cards.values() if not c.isHidden()
        )
        self._count_label.setText(
            f"已选: {len(self._selected_ids)}/{len(self._rois)}"
        )

    def _select_all(self) -> None:
        for card in self._cards.values():
            if not card.isHidden():
                card.set_checked(True)

    def _deselect_all(self) -> None:
        for card in self._cards.values():
            if not card.isHidden():
                card.set_checked(False)

    def _invert_selection(self) -> None:
        for card in self._cards.values():
            if not card.isHidden():
                card.set_checked(not card.is_checked())

    def _apply_filter(self, text: str) -> None:
        """按文件名筛选显示。"""
        for roi_id, card in self._cards.items():
            roi = next((r for r in self._rois if r.id == roi_id), None)
            if roi is None:
                continue
            if text == "全部文件" or roi.slide_path.name == text:
                card.show()
            else:
                card.hide()
        self._update_count()

    def _on_size_changed(self, value: int) -> None:
        self._thumb_size = value
        self._size_label.setText(f"{value}px")
        # 更新所有卡片大小（简单方案：重建卡片）
        # 对于更好的 UX，可以只 resize，但重建最简单
        for card in self._cards.values():
            card.setFixedSize(value + 16, value + 40)
            card._thumb_label.setFixedSize(value, value)

    def _on_double_click(self, roi_id: str) -> None:
        """双击打开全分辨率预览。"""
        roi = next((r for r in self._rois if r.id == roi_id), None)
        if roi is None:
            return
        reader = self._readers.get(roi.slide_path)
        if reader is None:
            QMessageBox.warning(self, "错误", "无法读取该切片")
            return
        dlg = FullResPreviewDialog(reader, roi, self)
        dlg.exec()

    def _on_export(self) -> None:
        if not self._selected_ids:
            QMessageBox.information(self, "提示", "请先勾选要导出的 ROI")
            return
        self.accept()

    def get_selected_ids(self) -> list[str]:
        return list(self._selected_ids)

    def closeEvent(self, event) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(3000)
        super().closeEvent(event)
