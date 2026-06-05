"""DeepLIIF 结果查看器 — 模态图像浏览、交互式阈值调整、评分显示。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QObject, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap, QPainter
from PySide6.QtWidgets import (
    QDialog,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSlider,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from liver_portal_crop.roi import ROIModel

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════
#  辅助: PIL → QImage
# ═══════════════════════════════════════════════════

def _pil_to_qimage(pil_img: Image.Image) -> QImage:
    rgb = pil_img.convert("RGB")
    data = rgb.tobytes()
    w, h = rgb.size
    return QImage(data, w, h, w * 3, QImage.Format.Format_RGB888).copy()


# ═══════════════════════════════════════════════════
#  模态图像查看面板
# ═══════════════════════════════════════════════════

class _ReprocessWorker(QObject):
    finished = Signal(int, str, int, int, dict, dict)
    error = Signal(int, str, str)

    def __init__(
        self,
        request_id: int,
        roi_id: str,
        orig: Image.Image,
        images: dict[str, Image.Image],
        tile_size: int,
        seg_thresh: int,
        size_thresh: int,
        parent=None,
    ):
        super().__init__(parent)
        self._request_id = request_id
        self._roi_id = roi_id
        self._orig = orig
        self._images = images
        self._tile_size = tile_size
        self._seg_thresh = seg_thresh
        self._size_thresh = size_thresh

    def run(self):
        try:
            from liver_portal_crop.deepliif_runner import reprocess

            processed_images, scoring = reprocess(
                orig=self._orig,
                images=self._images,
                tile_size=self._tile_size,
                seg_thresh=self._seg_thresh,
                size_thresh=self._size_thresh,
            )
        except Exception as e:
            self.error.emit(self._request_id, self._roi_id, str(e))
            return

        self.finished.emit(
            self._request_id,
            self._roi_id,
            self._seg_thresh,
            self._size_thresh,
            processed_images,
            scoring or {},
        )


class _ImageViewer(QGraphicsView):
    """可缩放的图像查看器。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setBackgroundBrush(Qt.GlobalColor.black)
        self._item: QGraphicsPixmapItem | None = None
        # 覆盖层：直接绘制到 viewport，完全绕过 scene
        self._cover_pix: QPixmap | None = None
        self._cover_rect: QRectF = QRectF()  # scene 坐标中的绘制区域

    def set_image(self, qimage: QImage, preserve_view: bool = False):
        """设置图像。preserve_view=True 时完全不动视图。"""
        pix = QPixmap.fromImage(qimage)

        if preserve_view and self._item is not None:
            # 只存 pixmap，不碰 scene，paintEvent 会直接覆盖绘制
            self._cover_pix = pix
            self._cover_rect = self._item.sceneBoundingRect()
            self.viewport().update()
            return

        # 正常重建
        self._cover_pix = None
        self._scene.clear()
        self._item = self._scene.addPixmap(pix)
        self._scene.setSceneRect(pix.rect())
        self.fitInView(self._scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def paintEvent(self, event):
        """正常绘制后，用覆盖层替换显示。"""
        super().paintEvent(event)
        if self._cover_pix is None or self._item is None:
            return
        # 在 viewport 上直接绘制，用 viewTransform 把 scene 坐标转为像素坐标
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        viewport_rect = self.mapFromScene(self._cover_rect).boundingRect()
        # scene rect → viewport pixels
        painter.drawPixmap(viewport_rect, self._cover_pix)
        painter.end()

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)


# ═══════════════════════════════════════════════════
#  结果查看对话框
# ═══════════════════════════════════════════════════

class DeepLIIFResultsDialog(QDialog):
    """DeepLIIF 分析结果查看器。

    支持交互式阈值调整：拖动 Size Gating / Intensity Threshold 滑块
    实时更新分割图像和 IHC 评分（调用 postprocess，不重跑推理）。
    """

    overlay_requested = Signal(str, QImage, int, int, int, int, float)

    MODALITY_ORDER = [
        ("SegRefined", "分割结果 (彩色)"),
        ("SegOverlaid", "原图叠加"),
        ("Seg", "分割概率图"),
        ("Marker", "Marker (蛋白表达)"),
        ("Hema", "Hematoxylin"),
        ("DAPI", "DAPI (核)"),
        ("Lap2", "Lap2 (核膜)"),
    ]

    def __init__(self, results: list[dict], tile_size: int = 512, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
        )
        self.setWindowTitle("DeepLIIF 分析结果")
        self.setMinimumSize(960, 680)
        self.resize(1160, 780)

        self._tile_size = tile_size
        self._results = {r["roi_id"]: r for r in results}
        self._result_list = results
        self._current_roi_id: str | None = None

        # 防抖定时器（阈值变化后延迟重算）
        self._reprocess_timer = QTimer()
        self._reprocess_timer.setSingleShot(True)
        self._reprocess_timer.setInterval(80)
        self._reprocess_timer.timeout.connect(self._reprocess)
        self._reprocess_request_id = 0
        self._reprocess_thread: QThread | None = None
        self._reprocess_worker: _ReprocessWorker | None = None
        self._pending_reprocess = False

        self._setup_ui()
        self._populate_list()

        if self._result_list:
            self._roi_list_widget.setCurrentRow(0)

    # ── UI 构建 ──────────────────────────────────────

    def _setup_ui(self):
        vl = QVBoxLayout(self)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)

        # 标题栏
        title_bar = QWidget()
        title_bar.setObjectName("resultsTitleBar")
        title_bar.setFixedHeight(36)
        title_lay = QHBoxLayout(title_bar)
        title_lay.setContentsMargins(12, 0, 8, 0)
        title_lbl = QLabel("🔬 DeepLIIF 分析结果")
        title_lbl.setStyleSheet("color: #e2e8f0; font-weight: bold; font-size: 13px;")
        title_lay.addWidget(title_lbl)
        title_lay.addStretch()
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(28, 28)
        close_btn.setObjectName("previewCloseBtn")
        close_btn.clicked.connect(self.close)
        title_lay.addWidget(close_btn)
        vl.addWidget(title_bar)

        # 主体三栏
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # ── 左栏: ROI 列表 ──
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(8, 8, 4, 8)
        left_layout.addWidget(QLabel("已分析 ROI"))
        self._roi_list_widget = QListWidget()
        self._roi_list_widget.currentRowChanged.connect(self._on_roi_selected)
        left_layout.addWidget(self._roi_list_widget)
        splitter.addWidget(left_panel)

        # ── 中栏: 图像标签页 ──
        center_panel = QWidget()
        center_layout = QVBoxLayout(center_panel)
        center_layout.setContentsMargins(4, 8, 4, 8)

        self._tab_widget = QTabWidget()
        self._tab_widget.setDocumentMode(True)
        self._viewer_tabs: dict[str, _ImageViewer] = {}
        for key, label in self.MODALITY_ORDER:
            viewer = _ImageViewer()
            self._viewer_tabs[key] = viewer
            self._tab_widget.addTab(viewer, label)
        center_layout.addWidget(self._tab_widget, 1)

        # 叠加透明度
        overlay_bar = QHBoxLayout()
        overlay_bar.addWidget(QLabel("叠加透明度:"))
        self._opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self._opacity_slider.setRange(0, 100)
        self._opacity_slider.setValue(40)
        self._opacity_label = QLabel("40%")
        self._opacity_slider.valueChanged.connect(
            lambda v: self._opacity_label.setText(f"{v}%")
        )
        overlay_bar.addWidget(self._opacity_slider, 1)
        overlay_bar.addWidget(self._opacity_label)
        center_layout.addLayout(overlay_bar)
        splitter.addWidget(center_panel)

        # ── 右栏: 信息 + 阈值 + 评分 ──
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(4, 8, 8, 8)

        # ROI 元数据
        self._info_lbl = QLabel()
        self._info_lbl.setStyleSheet("color: #94a3b8; font-size: 12px;")
        self._info_lbl.setWordWrap(True)
        right_layout.addWidget(self._info_lbl)

        # ── 阈值调整滑块 ──
        right_layout.addWidget(QLabel(""))
        thresh_title = QLabel("阈值调整")
        thresh_title.setStyleSheet("color: #e2e8f0; font-weight: bold; font-size: 13px;")
        right_layout.addWidget(thresh_title)

        # Intensity Threshold (seg_thresh)
        self._seg_thresh_lbl = QLabel("Intensity Threshold: 120")
        self._seg_thresh_lbl.setStyleSheet("color: #94a3b8; font-size: 11px;")
        right_layout.addWidget(self._seg_thresh_lbl)

        self._seg_thresh_slider = QSlider(Qt.Orientation.Horizontal)
        self._seg_thresh_slider.setRange(0, 254)
        self._seg_thresh_slider.setValue(120)
        self._seg_thresh_slider.valueChanged.connect(self._on_thresh_changed)
        right_layout.addWidget(self._seg_thresh_slider)

        # Size Gating (size_thresh)
        self._size_thresh_lbl = QLabel("Size Gating: 7")
        self._size_thresh_lbl.setStyleSheet("color: #94a3b8; font-size: 11px;")
        right_layout.addWidget(self._size_thresh_lbl)

        self._size_thresh_slider = QSlider(Qt.Orientation.Horizontal)
        self._size_thresh_slider.setRange(1, 20)
        self._size_thresh_slider.setValue(7)
        self._size_thresh_slider.valueChanged.connect(self._on_thresh_changed)
        right_layout.addWidget(self._size_thresh_slider)

        # ── 评分 ──
        right_layout.addWidget(QLabel(""))
        score_title = QLabel("IHC 评分")
        score_title.setStyleSheet("color: #e2e8f0; font-weight: bold; font-size: 13px;")
        right_layout.addWidget(score_title)

        self._score_table = QTableWidget()
        self._score_table.setColumnCount(2)
        self._score_table.setHorizontalHeaderLabels(["指标", "值"])
        self._score_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self._score_table.verticalHeader().setVisible(False)
        self._score_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._score_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._score_table.setFixedHeight(200)
        right_layout.addWidget(self._score_table)

        right_layout.addStretch()
        splitter.addWidget(right_panel)

        splitter.setSizes([160, 580, 280])
        vl.addWidget(splitter, 1)

        # 底部按钮
        btn_bar = QWidget()
        btn_bar.setObjectName("resultsBtnBar")
        btn_bar.setFixedHeight(44)
        btn_lay = QHBoxLayout(btn_bar)
        btn_lay.setContentsMargins(12, 0, 12, 0)

        self._export_btn = QPushButton("📥 导出选中结果")
        self._export_btn.clicked.connect(self._export_selected)
        btn_lay.addWidget(self._export_btn)

        self._export_all_btn = QPushButton("📥 导出全部")
        self._export_all_btn.clicked.connect(self._export_all)
        btn_lay.addWidget(self._export_all_btn)

        self._web_btn = QPushButton("🌐 在官网调参")
        self._web_btn.setToolTip("在浏览器中打开 DeepLIIF 官网交互式调参")
        self._web_btn.clicked.connect(self._open_website)
        btn_lay.addWidget(self._web_btn)

        btn_lay.addStretch()

        self._overlay_btn = QPushButton("🗺 叠加到画布")
        self._overlay_btn.setToolTip("将分割结果叠加到 WSI 画布上")
        self._overlay_btn.clicked.connect(self._request_overlay)
        btn_lay.addWidget(self._overlay_btn)

        close_btn2 = QPushButton("关闭")
        close_btn2.clicked.connect(self.close)
        btn_lay.addWidget(close_btn2)
        vl.addWidget(btn_bar)

    # ── 列表 ─────────────────────────────────────────

    def _populate_list(self):
        for result in self._result_list:
            roi = result["roi"]
            scoring = result.get("scoring") or {}
            pct = scoring.get("percent_pos")
            label = f"{roi.slide_path.stem}_ROI"
            if pct is not None:
                label += f"  ({pct:.1f}%+)"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, result["roi_id"])
            self._roi_list_widget.addItem(item)

    def _on_roi_selected(self, row: int):
        if row < 0:
            return
        item = self._roi_list_widget.item(row)
        roi_id = item.data(Qt.ItemDataRole.UserRole)
        self._current_roi_id = roi_id
        self._display_result(roi_id)

    # ── 显示结果 ─────────────────────────────────────

    def _display_result(self, roi_id: str):
        result = self._results.get(roi_id)
        if not result:
            return

        images = result.get("images", {})
        scoring = result.get("scoring") or {}
        roi = result["roi"]

        # 从 scoring 读取当前阈值，同步滑块
        seg_t = scoring.get("seg_thresh", 120)
        size_t = scoring.get("size_thresh", 7)
        self._seg_thresh_slider.blockSignals(True)
        self._size_thresh_slider.blockSignals(True)
        self._seg_thresh_slider.setValue(int(seg_t))
        self._size_thresh_slider.setValue(int(size_t))
        self._seg_thresh_slider.blockSignals(False)
        self._size_thresh_slider.blockSignals(False)
        self._seg_thresh_lbl.setText(f"Intensity Threshold: {int(seg_t)}")
        self._size_thresh_lbl.setText(f"Size Gating: {int(size_t)}")

        self._update_images(images)
        self._update_info(roi)
        self._update_scoring(scoring)

    def _update_images(self, images: dict[str, Image.Image], preserve_view: bool = False):
        for key, viewer in self._viewer_tabs.items():
            pil_img = images.get(key)
            if pil_img is not None:
                viewer.set_image(_pil_to_qimage(pil_img), preserve_view=preserve_view)
            else:
                viewer._scene.clear()

    def _update_info(self, roi: ROIModel):
        self._info_lbl.setText(
            f"文件: {roi.slide_path.name}\n"
            f"位置: ({roi.x}, {roi.y})\n"
            f"尺寸: {roi.w} × {roi.h} px"
        )

    def _update_scoring(self, scoring: dict):
        # 更新标签文本（列表中的百分比）
        if scoring and "percent_pos" in scoring:
            roi_id = self._current_roi_id
            if roi_id:
                result = self._results.get(roi_id)
                if result:
                    row = self._roi_list_widget.currentRow()
                    if row >= 0:
                        item = self._roi_list_widget.item(row)
                        roi = result["roi"]
                        item.setText(
                            f"{roi.slide_path.stem}_ROI  "
                            f"({scoring['percent_pos']:.1f}%+)"
                        )

        # 更新表格（只建一次，之后只改值）
        if not scoring:
            if self._score_table.rowCount() == 0:
                self._score_table.setRowCount(1)
                self._score_table.setItem(0, 0, QTableWidgetItem("状态"))
            self._score_table.setItem(0, 1, QTableWidgetItem("无评分数据"))
            return

        rows = [
            ("总细胞数", str(scoring.get("num_total", "N/A"))),
            ("阳性细胞 (IHC+)", str(scoring.get("num_pos", "N/A"))),
            ("阴性细胞", str(scoring.get("num_neg", "N/A"))),
            ("阳性率",
             f"{scoring.get('percent_pos', 0):.1f}%"
             if "percent_pos" in scoring else "N/A"),
            ("Intensity Threshold", str(scoring.get("seg_thresh", "N/A"))),
            ("Size Gating", str(scoring.get("size_thresh", "N/A"))),
        ]

        if self._score_table.rowCount() != len(rows):
            self._score_table.setRowCount(len(rows))
            for i, (name, _) in enumerate(rows):
                self._score_table.setItem(i, 0, QTableWidgetItem(name))
                self._score_table.setItem(i, 1, QTableWidgetItem(""))
        for i, (_, value) in enumerate(rows):
            self._score_table.item(i, 1).setText(value)

    # ── 交互式后处理 ──────────────────────────────────

    def _on_thresh_changed(self, _=None):
        """滑块变化 → 防抖后重算。"""
        logger.info("滑块变化: seg_thresh=%d, size_thresh=%d",
                     self._seg_thresh_slider.value(),
                     self._size_thresh_slider.value())
        self._seg_thresh_lbl.setText(
            f"Intensity Threshold: {self._seg_thresh_slider.value()}"
        )
        self._size_thresh_lbl.setText(
            f"Size Gating: {self._size_thresh_slider.value()}"
        )
        self._reprocess_timer.start()

    def _reprocess_blocking_legacy(self):
        """用新阈值重新运行 postprocess。"""
        if not self._current_roi_id:
            return
        result = self._results.get(self._current_roi_id)
        if not result:
            return

        images = result.get("images", {})
        # 需要原始 IHC 输入图像和 Seg 图才能 postprocess
        orig = images.get("IHC")
        seg = images.get("Seg")
        if orig is None or seg is None:
            return

        seg_thresh = self._seg_thresh_slider.value()
        size_thresh = self._size_thresh_slider.value()
        tile_size = result.get("tile_size", self._tile_size)

        try:
            from liver_portal_crop.deepliif_runner import reprocess

            processed_images, scoring = reprocess(
                orig=orig,
                images=images,
                tile_size=tile_size,
                seg_thresh=seg_thresh,
                size_thresh=size_thresh,
            )
        except Exception as e:
            logger.warning("postprocess 失败: %s", e)
            return

        # 合并更新后的图像
        images.update(processed_images)
        result["images"] = images
        result["scoring"] = scoring

        # 只刷新变化的标签页（SegOverlaid + SegRefined），不重建其他 6 个
        for key in ("SegOverlaid", "SegRefined"):
            viewer = self._viewer_tabs.get(key)
            pil_img = images.get(key)
            if viewer is not None and pil_img is not None:
                viewer.set_image(_pil_to_qimage(pil_img), preserve_view=True)
        self._update_scoring(scoring or {})

    # ── 官网调参 ──────────────────────────────────────

    def _reprocess(self):
        if not self._current_roi_id:
            logger.info("_reprocess: 跳过，无当前 ROI")
            return
        if self._reprocess_thread is not None:
            self._pending_reprocess = True
            logger.info("_reprocess: 线程忙，标记 pending")
            return

        result = self._results.get(self._current_roi_id)
        if not result:
            logger.info("_reprocess: 跳过，无结果数据")
            return

        images = result.get("images", {})
        orig = images.get("IHC")
        seg = images.get("Seg")
        if orig is None or seg is None:
            logger.info(
                "跳过 reprocess: %s (可用 keys: %s)",
                "缺少 IHC" if orig is None else "缺少 Seg（云端 API 可能未返回原始分割掩码）",
                list(images.keys()),
            )
            return

        seg_thresh = self._seg_thresh_slider.value()
        size_thresh = self._size_thresh_slider.value()
        tile_size = result.get("tile_size", self._tile_size)

        logger.info("_reprocess: 启动 worker, seg_thresh=%d, size_thresh=%d, seg mode=%s",
                     seg_thresh, size_thresh, seg.mode)
        self._reprocess_request_id += 1
        request_id = self._reprocess_request_id
        worker = _ReprocessWorker(
            request_id=request_id,
            roi_id=self._current_roi_id,
            orig=orig,
            images=dict(images),
            tile_size=tile_size,
            seg_thresh=seg_thresh,
            size_thresh=size_thresh,
        )
        thread = QThread()  # 无 parent，避免 dialog 销毁时连带销毁线程
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_reprocess_finished)
        worker.error.connect(self._on_reprocess_error)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_reprocess_thread_finished)
        self._reprocess_worker = worker
        self._reprocess_thread = thread
        thread.start()

    def _on_reprocess_finished(
        self,
        request_id: int,
        roi_id: str,
        seg_thresh: int,
        size_thresh: int,
        processed_images: dict,
        scoring: dict,
    ):
        logger.info("_on_reprocess_finished: req=%d, roi=%s, keys=%s",
                     request_id, roi_id, list(processed_images.keys()))
        if request_id != self._reprocess_request_id or roi_id != self._current_roi_id:
            logger.info("reprocess 结果已过期，丢弃")
            return
        if (
            seg_thresh != self._seg_thresh_slider.value()
            or size_thresh != self._size_thresh_slider.value()
        ):
            return

        result = self._results.get(roi_id)
        if not result:
            return

        images = result.get("images", {})
        images.update(processed_images)
        result["images"] = images
        result["scoring"] = scoring

        self._seg_thresh_lbl.setText(f"Intensity Threshold: {seg_thresh}")
        self._size_thresh_lbl.setText(f"Size Gating: {size_thresh}")
        for key in ("SegOverlaid", "SegRefined"):
            viewer = self._viewer_tabs.get(key)
            pil_img = images.get(key)
            if viewer is not None and pil_img is not None:
                viewer.set_image(_pil_to_qimage(pil_img), preserve_view=True)
        self._update_scoring(scoring or {})

    def _on_reprocess_error(self, request_id: int, roi_id: str, message: str):
        if request_id == self._reprocess_request_id and roi_id == self._current_roi_id:
            logger.warning("postprocess failed: %s", message)

    def _on_reprocess_thread_finished(self):
        self._reprocess_thread = None
        self._reprocess_worker = None
        if self._pending_reprocess:
            self._pending_reprocess = False
            self._reprocess_timer.start(0)

    def _open_website(self):
        """在系统浏览器中打开 DeepLIIF 官网。"""
        import webbrowser
        webbrowser.open("https://deepliif.org")
        QMessageBox.information(
            self, "官网调参",
            "已在浏览器中打开 DeepLIIF 官网。\n\n"
            "1. 上传 ROI 图像进行交互式分析\n"
            "2. 调整 Size Gating 和 Intensity Threshold\n"
            "3. 记下满意的参数值\n"
            "4. 回到此处用滑块输入相同参数\n"
            "   参数会自动应用到所有 ROI"
        )

    # ── 叠加到画布 ───────────────────────────────────

    def _request_overlay(self):
        if not self._current_roi_id:
            return
        result = self._results.get(self._current_roi_id)
        if not result:
            return

        images = result.get("images", {})
        seg_img = images.get("SegRefined") or images.get("Seg")
        if seg_img is None:
            QMessageBox.warning(self, "提示", "无可用的分割结果图像")
            return

        roi = result["roi"]
        qimg = _pil_to_qimage(seg_img)
        opacity = self._opacity_slider.value() / 100.0
        self.overlay_requested.emit(
            self._current_roi_id, qimg, roi.x, roi.y, roi.w, roi.h, opacity
        )

    # ── 导出 ─────────────────────────────────────────

    def _export_selected(self):
        if not self._current_roi_id:
            QMessageBox.information(self, "提示", "请先选择一个 ROI")
            return
        self._export_results([self._current_roi_id])

    def _export_all(self):
        self._export_results(list(self._results.keys()))

    def _export_results(self, roi_ids: list[str]):
        from PySide6.QtWidgets import QFileDialog

        out_dir = QFileDialog.getExistingDirectory(self, "选择导出目录")
        if not out_dir:
            return

        out_path = Path(out_dir)
        exported = 0

        for roi_id in roi_ids:
            result = self._results.get(roi_id)
            if not result:
                continue

            roi = result["roi"]
            images = result.get("images", {})
            scoring = result.get("scoring")
            prefix = f"{roi.slide_path.stem}_{roi_id}"

            for key, pil_img in images.items():
                if pil_img is not None:
                    fname = out_path / f"{prefix}_{key}.png"
                    pil_img.convert("RGB").save(str(fname), format="PNG")

            if scoring:
                fname = out_path / f"{prefix}_scoring.json"
                with open(fname, "w", encoding="utf-8") as f:
                    json.dump(scoring, f, indent=2, ensure_ascii=False)

            exported += 1

        QMessageBox.information(
            self, "导出完成",
            f"已导出 {exported} 个 ROI 的分析结果到:\n{out_dir}"
        )
