"""ROI 预览选择对话框 — 缩略图网格 + checkbox 选择 + 双击全分辨率预览。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from PySide6.QtCore import Qt, QRectF, QThread, QTimer, Signal, QSize, QPoint
from PySide6.QtGui import (
    QImage, QPixmap, QPainter, QPen, QBrush, QColor, QFont, QMouseEvent,
    QCloseEvent,
)
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFileDialog, QFormLayout, QGraphicsPixmapItem,
    QGraphicsScene, QGraphicsView, QGridLayout, QHBoxLayout, QLabel,
    QMainWindow, QMessageBox, QProgressBar, QPushButton, QScrollArea,
    QSizePolicy, QSlider, QVBoxLayout, QWidget, QComboBox, QFrame,
)

from liver_portal_crop.reader import SDPCReader
from liver_portal_crop.roi import ROIModel


# ── 自定义勾选指示器 ──────────────────────────────────

class CheckIndicator(QWidget):
    """无边框圆形勾选指示器，点击可切换状态。"""

    toggled = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(20, 20)
        self._checked = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def is_checked(self) -> bool:
        return self._checked

    def set_checked(self, checked: bool) -> None:
        self._checked = checked
        self.update()

    def mousePressEvent(self, event) -> None:
        self._checked = not self._checked
        self.toggled.emit(self._checked)
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(1, 1, -1, -1)
        if self._checked:
            p.setBrush(QBrush(QColor("#0891b2")))
            p.setPen(Qt.PenStyle.NoPen)
        else:
            p.setBrush(QBrush(QColor("#1e293b")))
            p.setPen(QPen(QColor("#64748b"), 1.5))
        p.drawEllipse(rect)
        p.end()


# ── ROI 缩略图卡片 ────────────────────────────────────

class ROICardWidget(QWidget):
    """单个 ROI 缩略图卡片：缩略图 + 左上角勾选指示器 + 底部尺寸标签。"""

    double_clicked = Signal(str)   # roi_id
    check_toggled = Signal(str, bool)  # roi_id, checked

    def __init__(self, roi_id: str, roi_w: int, roi_h: int,
                 thumb_size: int = 120, parent=None):
        super().__init__(parent)
        self._roi_id = roi_id
        self._thumb_size = thumb_size
        self._setup_ui(roi_w, roi_h)

    def _setup_ui(self, roi_w: int, roi_h: int) -> None:
        self.setFixedSize(self._thumb_size + 16, self._thumb_size + 40)
        self.setStyleSheet("background: transparent;")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 2)
        layout.setSpacing(2)

        # 缩略图容器（带勾选指示器叠加）
        self._container = QWidget()
        self._container.setObjectName("roiCardThumb")
        self._container.setFixedSize(self._thumb_size, self._thumb_size)
        container_layout = QVBoxLayout(self._container)
        container_layout.setContentsMargins(0, 0, 0, 0)

        self._thumb_label = QLabel()
        self._thumb_label.setObjectName("roiCardImage")
        self._thumb_label.setFixedSize(self._thumb_size, self._thumb_size)
        self._thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb_label.setText("加载中...")
        container_layout.addWidget(self._thumb_label)

        self._original_pixmap: QPixmap | None = None

        # 自定义勾选指示器叠加在左上角
        self._indicator = CheckIndicator(self._container)
        self._indicator.move(4, 4)
        self._indicator.toggled.connect(
            lambda checked: self.check_toggled.emit(self._roi_id, checked)
        )

        layout.addWidget(self._container)

        # 尺寸标签
        size_label = QLabel(f"{roi_w}x{roi_h}")
        size_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        size_label.setStyleSheet("font-size: 10px; color: #94a3b8;")
        layout.addWidget(size_label)

    def set_thumbnail(self, pixmap: QPixmap) -> None:
        self._original_pixmap = pixmap
        self._apply_thumb()

    def resize_thumb(self, size: int) -> None:
        """调整缩略图尺寸并重新缩放。"""
        self._thumb_size = size
        self._thumb_label.setFixedSize(size, size)
        self._container.setFixedSize(size, size)
        self.setFixedSize(size + 16, size + 40)
        self._apply_thumb()

    def _apply_thumb(self) -> None:
        if self._original_pixmap is None:
            return
        scaled = self._original_pixmap.scaled(
            self._thumb_size, self._thumb_size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._thumb_label.setPixmap(scaled)

    def set_loading_error(self) -> None:
        self._thumb_label.setText("加载失败")
        self._thumb_label.setStyleSheet("color: #ef4444; font-size: 11px;")

    def is_checked(self) -> bool:
        return self._indicator.is_checked()

    def set_checked(self, checked: bool) -> None:
        self._indicator.set_checked(checked)

    @property
    def roi_id(self) -> str:
        return self._roi_id

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """单击卡片切换勾选状态。"""
        if event.button() == Qt.MouseButton.LeftButton:
            new_state = not self._indicator.is_checked()
            self._indicator.set_checked(new_state)
            self.check_toggled.emit(self._roi_id, new_state)
        super().mousePressEvent(event)

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


class _TitleBar(QFrame):
    """自定义标题栏：可拖拽移动窗口。"""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setFixedHeight(36)
        self.setObjectName("previewTitleBar")
        self._drag_pos: QPoint | None = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 8, 0)
        layout.setSpacing(8)

        self._title_label = QLabel(title)
        self._title_label.setObjectName("previewTitleLabel")
        layout.addWidget(self._title_label, 1)

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(28, 28)
        close_btn.setObjectName("previewCloseBtn")
        close_btn.clicked.connect(self._on_close)
        layout.addWidget(close_btn)

    def set_title(self, title: str) -> None:
        self._title_label.setText(title)

    def _on_close(self) -> None:
        self.window().close()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.window().pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_pos = None
        super().mouseReleaseEvent(event)


class _ArrowButton(QPushButton):
    """半透明箭头按钮，手绘三角形确保居中。"""

    def __init__(self, direction: Qt.ArrowType, parent=None):
        super().__init__("", parent)
        self._direction = direction
        self._hovered = False
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 背景
        if not self.isEnabled():
            bg = QColor(15, 23, 42, 100)
            color = QColor(71, 85, 105)
        elif self._hovered:
            bg = QColor(30, 41, 59, 230)
            color = QColor(34, 211, 238)
        else:
            bg = QColor(15, 23, 42, 180)
            color = QColor(226, 232, 240)

        p.setBrush(QBrush(bg))
        p.setPen(QPen(QColor(71, 85, 105, 150), 1))
        p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 8, 8)

        # 三角形
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(color))
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        s = 8  # 三角形半径
        if self._direction == Qt.ArrowType.LeftArrow:
            pts = [QPoint(int(cx - s * 0.4), int(cy)),
                   QPoint(int(cx + s * 0.5), int(cy - s)),
                   QPoint(int(cx + s * 0.5), int(cy + s))]
        else:
            pts = [QPoint(int(cx + s * 0.4), int(cy)),
                   QPoint(int(cx - s * 0.5), int(cy - s)),
                   QPoint(int(cx - s * 0.5), int(cy + s))]
        p.drawPolygon(pts)
        p.end()


class FullResPreviewDialog(QDialog):
    """双击缩略图弹出的全分辨率 ROI 预览 — 无边框，自定义标题栏，支持左右切换。"""

    check_toggled = Signal(str, bool)  # roi_id, checked

    def __init__(self, rois: list[ROIModel], readers: dict[Path, SDPCReader],
                 current_index: int, selected_ids: set[str], parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.resize(700, 600)
        self._rois = rois
        self._readers = readers
        self._selected_ids = selected_ids
        self._current_index = current_index
        self._zoom_factor = 1.0
        self._setup_ui()
        self._navigate_to(current_index)

    def _setup_ui(self) -> None:
        self.setStyleSheet("""
            FullResPreviewDialog {
                background: #0f172a;
                border: 1px solid #334155;
                border-radius: 8px;
            }
            #previewTitleBar {
                background: #0c1322;
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
                border-bottom: 1px solid #1e293b;
            }
            #previewTitleLabel {
                color: #e2e8f0;
                font-size: 13px;
                font-weight: 600;
                background: transparent;
            }
            #previewCloseBtn {
                background: #334155;
                border: 1px solid #475569;
                color: #e2e8f0;
                font-size: 14px;
                font-weight: bold;
                border-radius: 6px;
                min-width: 28px;
                max-width: 28px;
                min-height: 28px;
                max-height: 28px;
                padding: 0;
            }
            #previewCloseBtn:hover {
                background: #ef4444;
                border-color: #ef4444;
                color: #fff;
            }
            #previewInfoLabel {
                color: #94a3b8;
                font-size: 11px;
                background: transparent;
                padding: 2px 12px;
            }
            #previewStatus {
                color: #64748b;
                font-size: 11px;
                background: #0c1322;
                padding: 2px 12px;
                border-top: 1px solid #1e293b;
                border-bottom-left-radius: 8px;
                border-bottom-right-radius: 8px;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 自定义标题栏（纯标题 + 关闭）
        title = self._make_title_text()
        self._title_bar = _TitleBar(title)
        layout.addWidget(self._title_bar)

        # 信息栏
        self._info_label = QLabel()
        self._info_label.setObjectName("previewInfoLabel")
        layout.addWidget(self._info_label)

        # 图像查看（overlay 按钮放在 view 上）
        self._view = QGraphicsView()
        self._scene = QGraphicsScene()
        self._view.setScene(self._scene)
        self._view.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self._view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self._view.setStyleSheet("QGraphicsView { background: #0f172a; border: none; }")
        layout.addWidget(self._view, 1)

        # 左侧切换按钮（overlay，手绘三角形确保居中）
        self._prev_btn = _ArrowButton(Qt.LeftArrow, self._view)
        self._prev_btn.setFixedSize(40, 60)
        self._prev_btn.clicked.connect(self._prev)

        # 右侧切换按钮（overlay）
        self._next_btn = _ArrowButton(Qt.RightArrow, self._view)
        self._next_btn.setFixedSize(40, 60)
        self._next_btn.clicked.connect(self._next)

        # 左上角勾选指示器（overlay）
        self._check_indicator = CheckIndicator(self._view)
        self._check_indicator.setFixedSize(24, 24)
        self._check_indicator.toggled.connect(self._on_check_toggled)
        self._check_indicator.raise_()

        # 状态栏
        self._status = QLabel("加载中...")
        self._status.setObjectName("previewStatus")
        layout.addWidget(self._status)

    def _make_title_text(self) -> str:
        roi = self._rois[self._current_index]
        pos = f"{self._current_index + 1}/{len(self._rois)}"
        return f"[{pos}]  {roi.slide_path.name}  ({roi.x}, {roi.y}) {roi.w}x{roi.h}"

    def _navigate_to(self, index: int) -> None:
        """切换到指定索引的 ROI。"""
        if index < 0 or index >= len(self._rois):
            return
        self._current_index = index
        roi = self._rois[index]
        self._title_bar.set_title(self._make_title_text())
        self._info_label.setText(
            f"文件: {roi.slide_path.name}  |  "
            f"位置: ({roi.x}, {roi.y})  |  "
            f"尺寸: {roi.w} x {roi.h}"
        )
        self._check_indicator.set_checked(roi.id in self._selected_ids)
        self._update_nav_buttons()
        self._load_image()

    def _load_image(self) -> None:
        roi = self._rois[self._current_index]
        reader = self._readers.get(roi.slide_path)
        if reader is None:
            self._scene.clear()
            self._status.setText("加载失败: 无法读取该切片")
            return
        try:
            region = reader.extract_region(roi.x, roi.y, roi.w, roi.h, level=0)
            h, w, ch = region.shape
            qimg = QImage(region.tobytes(), w, h, w * ch,
                          QImage.Format.Format_RGB888)
            pix = QPixmap.fromImage(qimg)
            self._scene.clear()
            self._scene.addPixmap(pix)
            self._scene.setSceneRect(0, 0, w, h)
            self._view.fitInView(self._scene.sceneRect(),
                                 Qt.AspectRatioMode.KeepAspectRatio)
            self._zoom_factor = self._view.transform().m11()
            self._status.setText(f"全分辨率: {w}x{h}  |  ← → 切换 · Space 勾选 · 滚轮缩放")
        except Exception as e:
            self._status.setText(f"加载失败: {e}")

    def _prev(self) -> None:
        self._navigate_to(self._current_index - 1)

    def _next(self) -> None:
        self._navigate_to(self._current_index + 1)

    def _on_check_toggled(self, checked: bool) -> None:
        roi = self._rois[self._current_index]
        if checked:
            self._selected_ids.add(roi.id)
        else:
            self._selected_ids.discard(roi.id)
        self.check_toggled.emit(roi.id, checked)

    def _update_nav_buttons(self) -> None:
        self._prev_btn.setEnabled(self._current_index > 0)
        self._next_btn.setEnabled(self._current_index < len(self._rois) - 1)

    def _reposition_overlays(self) -> None:
        """将 overlay 按钮定位到图像查看区域的正确位置。"""
        vp = self._view.viewport()
        vw, vh = vp.width(), vp.height()
        # viewport 坐标相对于 view，需要加上 viewport 的偏移
        ox, oy = vp.x(), vp.y()
        self._prev_btn.move(ox + 8, oy + (vh - self._prev_btn.height()) // 2)
        self._next_btn.move(ox + vw - self._next_btn.width() - 8,
                            oy + (vh - self._next_btn.height()) // 2)
        self._check_indicator.move(ox + 10, oy + 10)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reposition_overlays()

    def wheelEvent(self, event) -> None:
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self._zoom_factor *= factor
        self._view.scale(factor, factor)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        elif event.key() == Qt.Key.Key_Left:
            self._prev()
        elif event.key() == Qt.Key.Key_Right:
            self._next()
        elif event.key() == Qt.Key.Key_Space:
            new_state = not self._check_indicator.is_checked()
            self._check_indicator.set_checked(new_state)
            self._on_check_toggled(new_state)
        elif event.key() == Qt.Key.Key_0:
            self._view.resetTransform()
            self._view.fitInView(self._scene.sceneRect(),
                                 Qt.AspectRatioMode.KeepAspectRatio)
            self._zoom_factor = self._view.transform().m11()
        elif event.key() == Qt.Key.Key_F:
            self._view.fitInView(self._scene.sceneRect(),
                                 Qt.AspectRatioMode.KeepAspectRatio)
            self._zoom_factor = self._view.transform().m11()
        else:
            super().keyPressEvent(event)


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
        self._reflow_timer = QTimer()
        self._reflow_timer.setSingleShot(True)
        self._reflow_timer.setInterval(150)
        self._reflow_timer.timeout.connect(self._reflow_cards)
        self._setup_ui()
        self._first_show = True

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._first_show:
            self._first_show = False
            QTimer.singleShot(0, self._init_cards)

    def _init_cards(self) -> None:
        self._reflow_cards()
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
        self._grid_container.setStyleSheet("background: transparent;")
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
        self._file_groups: list[tuple[str, list[ROIModel]]] = []
        groups: dict[str, list[ROIModel]] = {}
        for roi in self._rois:
            groups.setdefault(roi.slide_path.name, []).append(roi)
        for name in sorted(groups.keys()):
            self._file_groups.append((name, groups[name]))

    def _reflow_cards(self) -> None:
        """根据容器宽度重新排列卡片位置（不销毁任何 widget）。"""
        available = self._grid_container.width() - 20
        if available < 50:
            return

        card_w = self._thumb_size + 16
        cols = max(1, available // card_w)

        # 从布局中移除所有项（不销毁 widget）
        while self._grid_layout.count():
            self._grid_layout.takeAt(0)

        # 创建持久化的 header（仅首次）
        if not hasattr(self, '_headers'):
            self._headers: dict[str, QLabel] = {}
            for file_name, _ in self._file_groups:
                header = QLabel(f"  {file_name}")
                header.setStyleSheet(
                    "font-weight: 600; font-size: 12px; color: #0891b2; "
                    "padding: 4px 0; background: transparent;"
                )
                self._headers[file_name] = header

        # 创建持久化的卡片（仅首次）
        for _, rois in self._file_groups:
            for roi in rois:
                if roi.id not in self._cards:
                    card = ROICardWidget(
                        roi.id, roi.w, roi.h, self._thumb_size
                    )
                    card.check_toggled.connect(self._on_card_check_toggled)
                    card.double_clicked.connect(self._on_double_click)
                    self._cards[roi.id] = card

        # 重新放置 header 和卡片到网格
        row = 0
        for file_name, rois in self._file_groups:
            self._grid_layout.addWidget(self._headers[file_name], row, 0, 1, cols)
            row += 1
            for i, roi in enumerate(rois):
                col = i % cols
                if col == 0 and i > 0:
                    row += 1
                self._grid_layout.addWidget(self._cards[roi.id], row, col)
            row += 1

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow_timer.start()

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

    def _on_card_check_toggled(self, roi_id: str, checked: bool) -> None:
        if checked:
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
                self._selected_ids.add(card.roi_id)
        self._update_count()

    def _deselect_all(self) -> None:
        for card in self._cards.values():
            if not card.isHidden():
                card.set_checked(False)
                self._selected_ids.discard(card.roi_id)
        self._update_count()

    def _invert_selection(self) -> None:
        for card in self._cards.values():
            if not card.isHidden():
                new_state = not card.is_checked()
                card.set_checked(new_state)
                if new_state:
                    self._selected_ids.add(card.roi_id)
                else:
                    self._selected_ids.discard(card.roi_id)
        self._update_count()

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
        # 同步显示/隐藏文件分组标题
        for file_name, header in self._headers.items():
            visible = text == "全部文件" or file_name == text
            header.setVisible(visible)
        self._update_count()

    def _on_size_changed(self, value: int) -> None:
        self._thumb_size = value
        self._size_label.setText(f"{value}px")
        for card in self._cards.values():
            card.resize_thumb(value)
        self._reflow_cards()

    def _on_double_click(self, roi_id: str) -> None:
        """双击打开全分辨率预览，支持左右切换。"""
        # 构建当前筛选下的可见 ROI 列表
        filter_text = self._filter_cb.currentText() if self._filter_cb else "全部文件"
        visible_rois = [
            r for r in self._rois
            if filter_text == "全部文件" or r.slide_path.name == filter_text
        ]
        clicked_index = next(
            (i for i, r in enumerate(visible_rois) if r.id == roi_id), -1
        )
        if clicked_index < 0:
            return

        dlg = FullResPreviewDialog(
            visible_rois, self._readers, clicked_index, self._selected_ids, self
        )
        dlg.check_toggled.connect(self._on_viewer_check_toggled)
        dlg.exec()
        # 关闭后刷新卡片勾选状态
        for cid, card in self._cards.items():
            card.set_checked(cid in self._selected_ids)
        self._update_count()

    def _on_viewer_check_toggled(self, roi_id: str, checked: bool) -> None:
        """同步全分辨率预览中的勾选变化到卡片。"""
        card = self._cards.get(roi_id)
        if card:
            card.set_checked(checked)
        self._update_count()

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


# ── 独立预览组件（嵌入主窗口，QStackedWidget 切换）─────────

class ROIPreviewPanel(QWidget):
    """ROI 预览面板 — 缩略图网格 + 双击全分辨率预览。

    嵌入主窗口的 QStackedWidget 中，与 WSI Canvas 切换显示。
    主窗口选中 ROI 时调用 on_roi_selected() 高亮对应卡片。
    ROI 增删改时调用 on_rois_changed() 刷新缩略图。
    """

    roi_selected = Signal(str)  # 面板选中 ROI → 通知主窗口

    def __init__(self, rois: list[ROIModel],
                 readers: dict[Path, SDPCReader],
                 toolbar_buttons: tuple | None = None,
                 filter_cb: QComboBox | None = None,
                 count_label: QLabel | None = None,
                 parent=None):
        super().__init__(parent)
        self._rois = list(rois)
        self._readers = readers
        self._cards: dict[str, ROICardWidget] = {}
        self._selected_ids: set[str] = set()
        self._highlighted_id: str | None = None
        self._thumb_size = 120
        self._worker: ThumbnailWorker | None = None
        self._reflow_timer = QTimer()
        self._reflow_timer.setSingleShot(True)
        self._reflow_timer.setInterval(150)
        self._reflow_timer.timeout.connect(self._reflow_cards)
        self._headers: dict[str, QLabel] = {}
        self._file_groups: list[tuple[str, list[ROIModel]]] = []
        # 外部传入的工具栏控件
        self._filter_cb = filter_cb
        self._count_label = count_label
        if toolbar_buttons and len(toolbar_buttons) >= 3:
            toolbar_buttons[0].clicked.connect(self._select_all)
            toolbar_buttons[1].clicked.connect(self._deselect_all)
            toolbar_buttons[2].clicked.connect(self._invert_selection)
        if self._filter_cb:
            self._filter_cb.currentTextChanged.connect(self._apply_filter)
        self._setup_ui()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._cards and self._rois:
            QTimer.singleShot(0, self._init_cards)

    def _init_cards(self) -> None:
        self._rebuild_file_groups()
        self._reflow_cards()
        self._start_thumbnail_generation()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        # 进度条
        self._thumb_progress = QProgressBar()
        self._thumb_progress.setRange(0, max(1, len(self._rois)))
        self._thumb_progress.setValue(0)
        self._thumb_progress.setFormat("生成缩略图: %v/%m")
        layout.addWidget(self._thumb_progress)

        # 滚动区域
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._grid_container = QWidget()
        self._grid_container.setStyleSheet("background: transparent;")
        self._grid_layout = QGridLayout(self._grid_container)
        self._grid_layout.setSpacing(8)
        self._grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

        scroll.setWidget(self._grid_container)
        layout.addWidget(scroll, 1)

        # 底部：缩略图大小滑块
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
        layout.addLayout(bottom)

    def _rebuild_file_groups(self) -> None:
        groups: dict[str, list[ROIModel]] = {}
        for roi in self._rois:
            groups.setdefault(roi.slide_path.name, []).append(roi)
        self._file_groups = [(name, groups[name]) for name in sorted(groups.keys())]

        current = self._filter_cb.currentText() if self._filter_cb else "全部文件"
        if self._filter_cb:
            self._filter_cb.blockSignals(True)
            self._filter_cb.clear()
            self._filter_cb.addItem("全部文件")
            for name, _ in self._file_groups:
                self._filter_cb.addItem(name)
            idx = self._filter_cb.findText(current)
            self._filter_cb.setCurrentIndex(max(0, idx))
            self._filter_cb.blockSignals(False)

    def _reflow_cards(self) -> None:
        available = self._grid_container.width() - 20
        if available < 50:
            return

        card_w = self._thumb_size + 16
        cols = max(1, available // card_w)

        while self._grid_layout.count():
            self._grid_layout.takeAt(0)

        for file_name, _ in self._file_groups:
            if file_name not in self._headers:
                header = QLabel(f"  {file_name}")
                header.setStyleSheet(
                    "font-weight: 600; font-size: 12px; color: #0891b2; "
                    "padding: 4px 0; background: transparent;"
                )
                self._headers[file_name] = header

        for _, rois in self._file_groups:
            for roi in rois:
                if roi.id not in self._cards:
                    card = ROICardWidget(
                        roi.id, roi.w, roi.h, self._thumb_size
                    )
                    card.double_clicked.connect(self._on_double_click)
                    card.check_toggled.connect(self._on_card_check_toggled)
                    self._cards[roi.id] = card

        row = 0
        filter_text = self._filter_cb.currentText() if self._filter_cb else "全部文件"
        for file_name, rois in self._file_groups:
            header_visible = filter_text == "全部文件" or file_name == filter_text
            if file_name in self._headers:
                self._headers[file_name].setVisible(header_visible)
                self._grid_layout.addWidget(self._headers[file_name], row, 0, 1, cols)
            row += 1
            for i, roi in enumerate(rois):
                card = self._cards.get(roi.id)
                if card is None:
                    continue
                card_visible = filter_text == "全部文件" or roi.slide_path.name == filter_text
                card.setVisible(card_visible)
                col = i % cols
                if col == 0 and i > 0:
                    row += 1
                self._grid_layout.addWidget(card, row, col)
            row += 1

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow_timer.start()

    def _start_thumbnail_generation(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(3000)
        self._thumb_progress.show()
        self._thumb_progress.setRange(0, max(1, len(self._rois)))
        self._thumb_progress.setValue(0)
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
        self._thumb_progress.setMaximum(total)
        self._thumb_progress.setValue(current)

    def _on_thumbnails_done(self) -> None:
        self._thumb_progress.hide()

    def _on_card_check_toggled(self, roi_id: str, checked: bool) -> None:
        if checked:
            self._selected_ids.add(roi_id)
        else:
            self._selected_ids.discard(roi_id)
        self._update_count()

    def _update_count(self) -> None:
        if self._count_label:
            self._count_label.setText(
                f"已选: {len(self._selected_ids)}/{len(self._rois)}"
            )

    def _select_all(self) -> None:
        for card in self._cards.values():
            if not card.isHidden():
                card.set_checked(True)
                self._selected_ids.add(card.roi_id)
        self._update_count()

    def _deselect_all(self) -> None:
        for card in self._cards.values():
            if not card.isHidden():
                card.set_checked(False)
                self._selected_ids.discard(card.roi_id)
        self._update_count()

    def _invert_selection(self) -> None:
        for card in self._cards.values():
            if not card.isHidden():
                new_state = not card.is_checked()
                card.set_checked(new_state)
                if new_state:
                    self._selected_ids.add(card.roi_id)
                else:
                    self._selected_ids.discard(card.roi_id)
        self._update_count()

    def _apply_filter(self, text: str) -> None:
        for roi_id, card in self._cards.items():
            roi = next((r for r in self._rois if r.id == roi_id), None)
            if roi is None:
                continue
            card.setVisible(text == "全部文件" or roi.slide_path.name == text)
        for file_name, header in self._headers.items():
            header.setVisible(text == "全部文件" or file_name == text)
        self._update_count()

    def _on_size_changed(self, value: int) -> None:
        self._thumb_size = value
        self._size_label.setText(f"{value}px")
        for card in self._cards.values():
            card.resize_thumb(value)
        self._reflow_cards()

    def _on_double_click(self, roi_id: str) -> None:
        filter_text = self._filter_cb.currentText() if self._filter_cb else "全部文件"
        visible_rois = [
            r for r in self._rois
            if filter_text == "全部文件" or r.slide_path.name == filter_text
        ]
        clicked_index = next(
            (i for i, r in enumerate(visible_rois) if r.id == roi_id), -1
        )
        if clicked_index < 0:
            return
        dlg = FullResPreviewDialog(
            visible_rois, self._readers, clicked_index, self._selected_ids, self
        )
        dlg.check_toggled.connect(self._on_viewer_check_toggled)
        dlg.exec()
        for cid, card in self._cards.items():
            card.set_checked(cid in self._selected_ids)
        self._update_count()

    def _on_viewer_check_toggled(self, roi_id: str, checked: bool) -> None:
        card = self._cards.get(roi_id)
        if card:
            card.set_checked(checked)
        self._update_count()

    # ── 外部接口 ──────────────────────────────────────

    def on_roi_selected(self, roi_id: str) -> None:
        """主窗口选中 ROI 时调用：高亮对应卡片并滚动到可见位置。"""
        self._highlighted_id = roi_id
        # 清除所有高亮
        for cid, c in self._cards.items():
            c.setStyleSheet("background: transparent;")
        card = self._cards.get(roi_id)
        if card and not card.isHidden():
            card.setStyleSheet(
                "background: rgba(8, 145, 178, 0.15); "
                "border: 1px solid #0891b2; border-radius: 6px;"
            )
            scroll = self.findChild(QScrollArea)
            if scroll:
                scroll.ensureWidgetVisible(card)

    def on_rois_changed(self, rois: list[ROIModel],
                        readers: dict[Path, SDPCReader]) -> None:
        """ROI 增删改时调用：刷新缩略图网格。"""
        # 保留仍然存在的选中 ID
        new_ids = {r.id for r in rois}
        self._selected_ids = {rid for rid in self._selected_ids if rid in new_ids}
        self._rois = list(rois)
        self._readers = readers
        for card in self._cards.values():
            self._grid_layout.removeWidget(card)
            card.deleteLater()
        for header in self._headers.values():
            self._grid_layout.removeWidget(header)
            header.deleteLater()
        self._cards.clear()
        self._headers.clear()
        if self._count_label:
            self._count_label.setText(f"已选: {len(self._selected_ids)}/{len(self._rois)}")
        self._rebuild_file_groups()
        self._reflow_cards()
        self._start_thumbnail_generation()

    def get_selected_ids(self) -> list[str]:
        return list(self._selected_ids)

    def closeEvent(self, event) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(3000)
        super().closeEvent(event)
