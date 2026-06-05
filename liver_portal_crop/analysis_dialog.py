"""DeepLIIF 分析配置对话框 — 选择 ROI、配置推理参数。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QProgressDialog,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from liver_portal_crop.roi import ROIModel


# ═══════════════════════════════════════════════════
#  缩略图加载 Worker
# ═══════════════════════════════════════════════════

class _ThumbWorker(QThread):
    """后台加载 ROI 缩略图。"""

    thumb_ready = Signal(str, QPixmap)  # roi_id, pixmap
    progress = Signal(int, int)         # current, total
    finished_all = Signal()

    def __init__(self, rois: list[ROIModel], readers: dict,
                 thumb_size: int = 100, parent=None):
        super().__init__(parent)
        self._rois = rois
        self._readers = readers
        self._thumb_size = thumb_size
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        total = len(self._rois)
        for i, roi in enumerate(self._rois):
            if self._cancel:
                break
            reader = self._readers.get(roi.slide_path)
            if reader is None:
                continue
            try:
                pix = self._make_thumb(reader, roi)
                self.thumb_ready.emit(roi.id, pix)
            except Exception:
                pass  # 跳过无法读取的 ROI
            self.progress.emit(i + 1, total)
        self.finished_all.emit()

    def _make_thumb(self, reader, roi: ROIModel) -> QPixmap:
        """读取 ROI 区域并生成缩略图。"""
        # 选择合适的金字塔层级
        target = self._thumb_size
        ds_list = [lv.downsample for lv in reader.levels]
        level = 0
        for lv_idx in range(reader.level_count - 1, -1, -1):
            ds = ds_list[lv_idx]
            w_lv = int(roi.w / ds)
            h_lv = int(roi.h / ds)
            if w_lv >= target and h_lv >= target:
                level = lv_idx
                break

        ds = ds_list[level]
        lx = int(roi.x / ds)
        ly = int(roi.y / ds)
        lw = max(1, int(roi.w / ds))
        lh = max(1, int(roi.h / ds))

        # clamp to level bounds
        lv = reader.levels[level]
        lx = max(0, min(lx, lv.width - 1))
        ly = max(0, min(ly, lv.height - 1))
        lw = min(lw, lv.width - lx)
        lh = min(lh, lv.height - ly)

        region = reader._read_level_region(level, lx, ly, lw, lh)
        pil_img = Image.fromarray(region)
        pil_img.thumbnail((self._thumb_size, self._thumb_size), Image.LANCZOS)

        # PIL -> QPixmap
        rgb_data = pil_img.tobytes()
        w, h = pil_img.size
        qimg = QImage(rgb_data, w, h, w * 3, QImage.Format.Format_RGB888)
        return QPixmap.fromImage(qimg)


# ═══════════════════════════════════════════════════
#  ROI 选择卡片
# ═══════════════════════════════════════════════════

class _ROICard(QWidget):
    """可勾选的 ROI 缩略图卡片。"""

    check_toggled = Signal(str, bool)  # roi_id, checked

    def __init__(self, roi: ROIModel, thumb_size: int = 100, parent=None):
        super().__init__(parent)
        self._roi = roi
        self._checked = True  # 默认选中
        self._thumb_size = thumb_size

        self.setFixedSize(thumb_size + 16, thumb_size + 36)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        vl = QVBoxLayout(self)
        vl.setContentsMargins(4, 4, 4, 2)
        vl.setSpacing(2)

        # 缩略图容器
        self._container = QWidget()
        self._container.setFixedSize(thumb_size, thumb_size)
        self._container.setObjectName("analysisCardThumb")
        self._img_label = QLabel(self._container)
        self._img_label.setGeometry(0, 0, thumb_size, thumb_size)
        self._img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_label.setStyleSheet("background: #1e293b; border-radius: 4px;")
        self._check_lbl = QLabel("✓" if self._checked else "", self._container)
        self._check_lbl.setGeometry(4, 4, 18, 18)
        self._check_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._update_check_style()
        vl.addWidget(self._container)

        # 尺寸标签
        self._size_label = QLabel(f"{roi.w}×{roi.h}")
        self._size_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._size_label.setStyleSheet("color: #94a3b8; font-size: 10px;")
        vl.addWidget(self._size_label)

    @property
    def roi_id(self) -> str:
        return self._roi.id

    @property
    def checked(self) -> bool:
        return self._checked

    def set_checked(self, checked: bool):
        self._checked = checked
        self._check_lbl.setText("✓" if checked else "")
        self._update_check_style()

    def set_pixmap(self, pix: QPixmap):
        scaled = pix.scaled(
            self._thumb_size, self._thumb_size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._img_label.setPixmap(scaled)

    def _update_check_style(self):
        if self._checked:
            self._check_lbl.setStyleSheet(
                "background: #0891b2; color: white; border-radius: 3px;"
                "font-size: 12px; font-weight: bold;"
            )
        else:
            self._check_lbl.setStyleSheet(
                "background: #1e293b; color: #475569; border-radius: 3px;"
                "font-size: 12px;"
            )

    def mousePressEvent(self, event):
        self._checked = not self._checked
        self._check_lbl.setText("✓" if self._checked else "")
        self._update_check_style()
        self.check_toggled.emit(self._roi.id, self._checked)
        super().mousePressEvent(event)


# ═══════════════════════════════════════════════════
#  分析配置对话框
# ═══════════════════════════════════════════════════

class DeepLIIFAnalysisDialog(QDialog):
    """DeepLIIF 分析配置对话框。"""

    confirmed = Signal()  # 无参数；params/selected_rois 通过实例属性传递
    patch_confirmed = Signal()  # 无参数；patch_data 通过实例属性传递

    def __init__(self, rois: list[ROIModel], readers: dict,
                 current_slide: Path | None = None,
                 magnification: str = "40x",
                 parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
        )
        self.setWindowTitle("DeepLIIF 分析")
        self.setMinimumSize(600, 650)
        self.resize(700, 700)

        self._rois = rois
        self._readers = readers
        self._current_slide = current_slide
        self._magnification = magnification
        self._cards: dict[str, _ROICard] = {}
        self._thumb_worker: _ThumbWorker | None = None

        self._setup_ui()
        self._start_thumb_loading()

    def _setup_ui(self):
        vl = QVBoxLayout(self)
        vl.setSpacing(8)

        # ── 参数区域 ──
        form = QFormLayout()

        # 推理模式
        mode_lay = QHBoxLayout()
        self._mode_local = QPushButton("🖥 本地模型")
        self._mode_local.setCheckable(True)
        self._mode_cloud = QPushButton("☁ 云端 API")
        self._mode_cloud.setCheckable(True)
        self._mode_cloud.setChecked(True)  # 默认云端（无需模型文件）
        self._mode_local.clicked.connect(lambda: self._set_mode("local"))
        self._mode_cloud.clicked.connect(lambda: self._set_mode("cloud"))
        mode_lay.addWidget(self._mode_local)
        mode_lay.addWidget(self._mode_cloud)
        form.addRow("推理模式:", mode_lay)

        # 模型路径（仅本地模式）
        model_lay = QHBoxLayout()
        self._model_dir_edit = QLineEdit()
        self._model_dir_edit.setPlaceholderText("DeepLIIF 模型目录路径...")
        self._model_dir_edit.setText(str(Path.home() / ".deepliif" / "models"))
        self._model_dir_edit.textChanged.connect(self._check_model_status)
        self._model_browse_btn = QPushButton("浏览...")
        self._model_browse_btn.clicked.connect(self._browse_model_dir)
        model_lay.addWidget(self._model_dir_edit)
        model_lay.addWidget(self._model_browse_btn)
        self._model_row_widget = QWidget()
        self._model_row_widget.setLayout(model_lay)
        form.addRow("模型路径:", self._model_row_widget)
        self._model_row_widget.setVisible(False)  # 默认隐藏（云端模式）

        # 模型状态 + 下载按钮
        self._model_status_lbl = QLabel()
        self._model_status_lbl.setWordWrap(True)
        self._model_status_lbl.setStyleSheet("color: #94a3b8; font-size: 11px;")
        self._model_status_lbl.setVisible(False)
        form.addRow("", self._model_status_lbl)

        self._download_btn = QPushButton("⬇ 下载模型 (~500MB)")
        self._download_btn.setToolTip("从 Zenodo 下载 DeepLIIF 预训练模型")
        self._download_btn.clicked.connect(self._download_model)
        self._download_btn.setVisible(False)
        form.addRow("", self._download_btn)

        # Tile Size（仅本地模式使用）
        self._tile_size_cb = QComboBox()
        self._tile_size_cb.addItems(["512 (40x)", "256 (20x)", "128 (10x)"])
        mag_tile_map = {"40x": 0, "80x": 0, "20x": 1, "10x": 2, "4x": 2}
        idx = mag_tile_map.get(self._magnification, 0)
        self._tile_size_cb.setCurrentIndex(idx)
        form.addRow("Tile Size:", self._tile_size_cb)

        # 处理模式说明
        self._mode_info = QLabel()
        self._mode_info.setWordWrap(True)
        self._mode_info.setStyleSheet("color: #64748b; font-size: 11px;")
        form.addRow("", self._mode_info)

        # 分析范围
        self._scope_cb = QComboBox()
        self._scope_cb.addItem("当前文件 ROI")
        if len(self._readers) > 1:
            all_count = sum(
                len([r for r in self._rois if r.slide_path == sp])
                for sp in self._readers
            )
            self._scope_cb.addItem(f"所有文件 ROI ({all_count} 个)")
        form.addRow("分析范围:", self._scope_cb)

        # 仅分割模式
        self._seg_only_cb = QCheckBox("仅分割 (seg_only, 更快)")
        self._seg_only_cb.setToolTip(
            "跳过模态图像生成，仅运行细胞分割。\n"
            "CPU 推理时建议开启以加快速度。"
        )
        form.addRow("", self._seg_only_cb)

        vl.addLayout(form)

        # ── ROI 选择网格 ──
        vl.addWidget(QLabel("选择要分析的 ROI:"))

        # 工具栏
        toolbar = QHBoxLayout()
        btn_select_all = QPushButton("全选")
        btn_select_all.clicked.connect(self._select_all)
        btn_invert = QPushButton("反选")
        btn_invert.clicked.connect(self._invert_selection)
        btn_deselect = QPushButton("取消全选")
        btn_deselect.clicked.connect(self._deselect_all)
        self._count_label = QLabel(f"已选: {len(self._rois)}/{len(self._rois)}")
        self._count_label.setStyleSheet("color: #94a3b8;")
        toolbar.addWidget(btn_select_all)
        toolbar.addWidget(btn_invert)
        toolbar.addWidget(btn_deselect)
        toolbar.addStretch()
        toolbar.addWidget(self._count_label)
        vl.addLayout(toolbar)

        # 缩略图进度
        from PySide6.QtWidgets import QProgressBar
        self._thumb_progress = QProgressBar()
        self._thumb_progress.setMaximum(len(self._rois) if self._rois else 1)
        self._thumb_progress.setTextVisible(False)
        self._thumb_progress.setFixedHeight(3)
        self._thumb_progress.setVisible(bool(self._rois))
        vl.addWidget(self._thumb_progress)

        # 滚动区域 + 网格
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._grid_container = QWidget()
        self._grid_layout = QGridLayout(self._grid_container)
        self._grid_layout.setSpacing(8)
        scroll.setWidget(self._grid_container)
        vl.addWidget(scroll, 1)  # stretch=1

        # 创建卡片
        for i, roi in enumerate(self._rois):
            card = _ROICard(roi, thumb_size=80)
            card.check_toggled.connect(self._on_card_toggled)
            self._cards[roi.id] = card
            row, col = divmod(i, 6)
            self._grid_layout.addWidget(card, row, col)

        # ── 底部按钮 ──
        btn_lay = QHBoxLayout()
        self._patch_btn = QPushButton("✂ 小块测试 (2000px)")
        self._patch_btn.setToolTip(
            "从 ROI 中心裁 2000×2000 原像素小块直接推理\n"
            "用于在结果窗口中交互式调参，确认参数后再批量分析"
        )
        self._patch_btn.clicked.connect(self._export_test_patch)
        self._patch_btn.setMinimumHeight(32)
        btn_lay.addWidget(self._patch_btn)

        self._start_btn = QPushButton("🚀 开始分析")
        self._start_btn.setDefault(True)
        self._start_btn.clicked.connect(self._on_confirm)
        self._start_btn.setMinimumHeight(32)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.close)
        cancel_btn.setMinimumHeight(32)
        btn_lay.addWidget(self._start_btn)
        btn_lay.addWidget(cancel_btn)
        vl.addLayout(btn_lay)

        # 初始状态
        self._set_mode("cloud")

    def _set_mode(self, mode: str):
        is_local = mode == "local"
        self._mode_local.setChecked(is_local)
        self._mode_cloud.setChecked(not is_local)
        self._model_row_widget.setVisible(is_local)
        self._model_status_lbl.setVisible(is_local)
        self._download_btn.setVisible(False)
        if is_local:
            self._check_model_status()
            self._mode_info.setText(
                "本地模式：直接推理整张 ROI 原图，无大小限制"
            )
            self._tile_size_cb.setEnabled(True)
        else:
            self._mode_info.setText(
                "云端模式：ROI ≤ 2048px 直接推理；> 2048px 切成 2000px 大块并发处理后拼接\n"
                "Tile Size 决定发送给 API 的分辨率倍率"
            )
            self._tile_size_cb.setEnabled(True)

    def _check_model_status(self, _=None):
        """检查当前模型路径的可用状态。"""
        from liver_portal_crop.deepliif_runner import check_model_available
        model_dir = self._model_dir_edit.text().strip()
        if not model_dir:
            self._model_status_lbl.setText("")
            self._download_btn.setVisible(False)
            return
        ok, msg = check_model_available(model_dir)
        if ok:
            self._model_status_lbl.setText(f"✓ {msg}")
            self._model_status_lbl.setStyleSheet("color: #22c55e; font-size: 11px;")
            self._download_btn.setVisible(False)
        else:
            self._model_status_lbl.setText(msg)
            self._model_status_lbl.setStyleSheet("color: #f59e0b; font-size: 11px;")
            self._download_btn.setVisible(True)

    def _download_model(self):
        """在 QThread 中下载 DeepLIIF 预训练模型。"""
        from liver_portal_crop.deepliif_runner import ModelDownloadWorker

        model_dir = self._model_dir_edit.text().strip()
        if not model_dir:
            return

        self._download_btn.setEnabled(False)
        self._download_btn.setText("下载中...")

        # 创建进度对话框
        self._dl_progress = QProgressDialog("正在准备下载...", "取消", 0, 0, self)
        self._dl_progress.setWindowTitle("下载 DeepLIIF 模型")
        self._dl_progress.setMinimumDuration(0)
        self._dl_progress.setAutoClose(False)
        self._dl_progress.setAutoReset(False)
        self._dl_progress.setCancelButton(None)  # 先隐藏取消，等知道总大小再启用
        self._dl_progress.show()

        # Worker + Thread
        self._dl_worker = ModelDownloadWorker(model_dir)
        self._dl_thread = QThread()
        self._dl_worker.moveToThread(self._dl_thread)

        self._dl_thread.started.connect(self._dl_worker.run)
        self._dl_worker.progress.connect(self._on_dl_progress)
        self._dl_worker.status.connect(
            lambda msg: self._dl_progress.setLabelText(msg)
        )
        self._dl_worker.finished.connect(self._on_dl_finished)
        self._dl_worker.finished.connect(self._dl_thread.quit)
        self._dl_worker.finished.connect(self._dl_worker.deleteLater)
        self._dl_thread.finished.connect(self._dl_thread.deleteLater)

        self._dl_thread.start()

    def _on_dl_progress(self, pct: int, dl_mb: int, total_mb: int):
        """更新下载进度条。"""
        if total_mb <= 0:
            return
        # 首次知道总大小时设置范围和取消按钮
        if self._dl_progress.maximum() == 0:
            self._dl_progress.setMaximum(100)
            cancel_btn = QPushButton("取消")
            self._dl_progress.setCancelButton(cancel_btn)
            self._dl_progress.canceled.connect(self._on_dl_cancel)
        self._dl_progress.setValue(pct)
        self._dl_progress.setLabelText(
            f"正在下载... {dl_mb} / {total_mb} MB  ({pct}%)"
        )

    def _on_dl_cancel(self):
        """取消下载。"""
        if hasattr(self, '_dl_worker') and self._dl_worker:
            self._dl_worker.cancel()

    def _on_dl_finished(self, ok: bool, msg: str):
        """下载完成。"""
        self._dl_progress.close()
        self._download_btn.setEnabled(True)
        self._download_btn.setText("⬇ 下载模型 (~500MB)")
        if ok:
            self._model_status_lbl.setText(f"✓ {msg}")
            self._model_status_lbl.setStyleSheet("color: #22c55e; font-size: 11px;")
            self._download_btn.setVisible(False)
        else:
            self._model_status_lbl.setText(msg)
            color = "#94a3b8" if "取消" in msg else "#ef4444"
            self._model_status_lbl.setStyleSheet(
                f"color: {color}; font-size: 11px;"
            )
        self._check_model_status()

    def _browse_model_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择 DeepLIIF 模型目录")
        if d:
            self._model_dir_edit.setText(d)

    def _on_card_toggled(self, roi_id: str, checked: bool):
        self._update_count()

    def _select_all(self):
        for card in self._cards.values():
            card.set_checked(True)
        self._update_count()

    def _deselect_all(self):
        for card in self._cards.values():
            card.set_checked(False)
        self._update_count()

    def _invert_selection(self):
        for card in self._cards.values():
            card.set_checked(not card.checked)
        self._update_count()

    def _update_count(self):
        selected = sum(1 for c in self._cards.values() if c.checked)
        self._count_label.setText(f"已选: {selected}/{len(self._rois)}")
        self._start_btn.setEnabled(selected > 0)

    def _start_thumb_loading(self):
        if not self._rois:
            return
        self._thumb_worker = _ThumbWorker(
            self._rois, self._readers, thumb_size=80, parent=self,
        )
        self._thumb_worker.thumb_ready.connect(self._on_thumb_ready)
        self._thumb_worker.progress.connect(self._on_thumb_progress)
        self._thumb_worker.finished_all.connect(self._on_thumbs_done)
        self._thumb_worker.start()

    def _on_thumb_ready(self, roi_id: str, pix: QPixmap):
        card = self._cards.get(roi_id)
        if card:
            card.set_pixmap(pix)

    def _on_thumb_progress(self, current: int, total: int):
        self._thumb_progress.setValue(current)

    def _on_thumbs_done(self):
        self._thumb_progress.setVisible(False)

    # ── 小块测试 ──

    def _export_test_patch(self):
        """裁剪小块 → 准备数据 → 发射信号通知主窗口执行推理。"""
        selected = self.get_selected_rois()
        if not selected:
            QMessageBox.information(self, "提示", "请先选择至少一个 ROI")
            return

        roi = selected[0]
        reader = self._readers.get(roi.slide_path)
        if reader is None:
            QMessageBox.warning(self, "错误", "无法读取切片文件")
            return

        try:
            from liver_portal_crop.deepliif_runner import extract_roi_as_pil, crop_test_patch
            img = extract_roi_as_pil(reader, roi)
            patch = crop_test_patch(img, patch_size=2000)
        except Exception as e:
            QMessageBox.warning(self, "错误", f"裁剪失败: {e}")
            return

        # 确定推理模式和参数
        is_local = self._mode_local.isChecked()
        if is_local:
            from liver_portal_crop.deepliif_runner import DeepLIIFMode, check_model_available
            model_dir = self._model_dir_edit.text().strip()
            ok, msg = check_model_available(model_dir)
            if not ok:
                QMessageBox.warning(self, "模型不可用", msg)
                return
            mode = DeepLIIFMode.LOCAL
        else:
            from liver_portal_crop.deepliif_runner import DeepLIIFMode
            mode = DeepLIIFMode.CLOUD
            model_dir = None

        tile_text = self._tile_size_cb.currentText()
        tile_size = int(tile_text.split()[0])

        from liver_portal_crop.roi import ROIModel
        patch_roi = ROIModel(
            slide_path=roi.slide_path, x=roi.x, y=roi.y,
            w=roi.w, h=roi.h, id="patch_test",
        )

        # 将数据保存在实例上，由主窗口通过 dlg 引用读取
        self._patch_data = {
            "patch": patch,
            "patch_roi": patch_roi,
            "mode": mode,
            "model_dir": model_dir,
            "tile_size": tile_size,
            "seg_only": self._seg_only_cb.isChecked(),
        }
        self.patch_confirmed.emit()
        self.close()

    # ── 公共接口 ──

    def get_params(self) -> dict:
        """返回推理配置参数。"""
        is_local = self._mode_local.isChecked()
        tile_text = self._tile_size_cb.currentText()
        tile_size = int(tile_text.split()[0])

        scope_text = self._scope_cb.currentText()
        scope = "all" if "所有" in scope_text else "current"

        return {
            "mode": "local" if is_local else "cloud",
            "model_dir": self._model_dir_edit.text().strip() if is_local else None,
            "tile_size": tile_size,
            "scope": scope,
            "seg_only": self._seg_only_cb.isChecked(),
        }

    def get_selected_rois(self) -> list[ROIModel]:
        """返回用户选中的 ROI 列表。"""
        return [roi for roi in self._rois if self._cards[roi.id].checked]

    def _on_confirm(self):
        """开始分析 — 发射信号并关闭对话框。"""
        params = self.get_params()
        selected = self.get_selected_rois()
        if not selected:
            QMessageBox.information(self, "提示", "请先选择至少一个 ROI")
            return
        # 将参数保存在实例上，由主窗口通过 dlg 引用读取
        self._confirmed_params = params
        self._confirmed_rois = selected
        self.confirmed.emit()
        self.close()

    def closeEvent(self, event):
        if self._thumb_worker and self._thumb_worker.isRunning():
            self._thumb_worker.cancel()
            self._thumb_worker.wait(3000)
        if hasattr(self, '_dl_thread') and self._dl_thread and self._dl_thread.isRunning():
            if hasattr(self, '_dl_worker') and self._dl_worker:
                self._dl_worker.cancel()
            self._dl_thread.quit()
            self._dl_thread.wait(3000)
        super().closeEvent(event)
