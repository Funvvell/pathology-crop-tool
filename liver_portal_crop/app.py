"""MainWindow — 主窗口，组装所有模块。"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, QRectF, QThread
from PySide6.QtWidgets import QDialog
from PySide6.QtGui import QAction, QImage, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMainWindow, QMenuBar, QMessageBox,
    QProgressBar, QPushButton, QSpinBox, QSplitter,
    QVBoxLayout, QWidget,
)

from liver_portal_crop.theme import load_theme
from liver_portal_crop.canvas import WSICanvas
from liver_portal_crop.dialogs import SettingsDialog
from liver_portal_crop.exporter import BatchExporter, CropConfig
from liver_portal_crop.navigator import NavigationWidget
from liver_portal_crop.tissue_detect import (
    detect_tissue, tissue_regions_to_rois, tissue_regions_to_rois_grid, TissueDialog,
)
from liver_portal_crop.reader import SDPCReader, SDPCReadError
from liver_portal_crop.roi import ROIManager, ROIModel

SESSION_DIR = Path.home() / ".liver_portal_crop"
SESSION_FILE = SESSION_DIR / "session.json"
PRESETS_FILE = SESSION_DIR / "presets.json"


class MainWindow(QMainWindow):
    """应用程序主窗口。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("病理裁剪工具")
        self.resize(1200, 800)

        self._readers: dict[Path, SDPCReader] = {}
        self._roi_manager = ROIManager()
        self._crop_config = CropConfig(
            output_dir=Path.home() / "liver_crop_output",
        )
        self._current_slide: Path | None = None
        self._current_theme: str = "dark"

        self._setup_ui()
        self._connect_signals()
        self._setup_menu()
        self._load_session()
        self._load_presets()

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ── 顶部工具栏 ──
        toolbar = QWidget()
        toolbar.setObjectName("topToolbar")
        toolbar.setFixedHeight(36)
        tbar = QHBoxLayout(toolbar)
        tbar.setContentsMargins(8, 0, 8, 0)
        tbar.setSpacing(4)

        self._status_label = QLabel("就绪")
        self._status_label.setObjectName("statusLabel")
        tbar.addWidget(self._status_label)

        # 预设
        self._preset_cb = QComboBox()
        self._preset_cb.setObjectName("presetCb")
        self._preset_cb.setMinimumWidth(90)
        self._preset_cb.currentTextChanged.connect(self._apply_preset)
        tbar.addWidget(self._preset_cb)

        self._save_preset_btn = QPushButton("💾")
        self._save_preset_btn.setFixedSize(26, 24)
        self._save_preset_btn.setObjectName("savePresetBtn")
        self._save_preset_btn.clicked.connect(self._save_preset)
        tbar.addWidget(self._save_preset_btn)

        tbar.addSpacing(12)

        self._roi_mode_btn = QPushButton("ROI 绘制")
        self._roi_mode_btn.setObjectName("roiBtn")
        self._roi_mode_btn.setCheckable(True)
        self._roi_mode_btn.clicked.connect(self._toggle_roi_mode)
        tbar.addWidget(self._roi_mode_btn)

        tbar.addSpacing(8)

        tbar.addWidget(QLabel("倍率:"))
        self._mag_cb = QComboBox()
        self._mag_cb.addItems(["4x", "10x", "20x", "40x", "80x", "自定义"])
        self._mag_cb.setCurrentText("20x")
        self._mag_cb.currentTextChanged.connect(self._auto_calc_frame)
        tbar.addWidget(self._mag_cb)

        tbar.addWidget(QLabel("比例:"))
        self._ratio_cb = QComboBox()
        self._ratio_cb.addItems(["Free", "1:1", "4:3", "3:2", "16:9"])
        self._ratio_cb.setCurrentText("16:9")
        self._ratio_cb.currentTextChanged.connect(self._auto_calc_frame)
        tbar.addWidget(self._ratio_cb)

        tbar.addWidget(QLabel("框宽:"))
        self._frame_w_spin = QSpinBox()
        self._frame_w_spin.setRange(64, 999999)
        self._frame_w_spin.setSingleStep(64)
        self._frame_w_spin.setValue(512)
        self._frame_w_spin.valueChanged.connect(self._update_frame_size)
        tbar.addWidget(self._frame_w_spin)

        tbar.addWidget(QLabel("框高:"))
        self._frame_h_spin = QSpinBox()
        self._frame_h_spin.setRange(64, 999999)
        self._frame_h_spin.setSingleStep(64)
        self._frame_h_spin.setValue(512)
        self._frame_h_spin.valueChanged.connect(self._update_frame_size)
        tbar.addWidget(self._frame_h_spin)

        # 进度条
        self._progress_bar = QProgressBar()
        self._progress_bar.setObjectName("exportProgress")
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setFixedWidth(160)
        self._progress_bar.setFixedHeight(18)
        self._progress_bar.hide()
        tbar.addWidget(self._progress_bar)

        self._cancel_btn = QPushButton("✕")
        self._cancel_btn.setFixedSize(22, 22)
        self._cancel_btn.setObjectName("cancelBtn")
        self._cancel_btn.clicked.connect(self._cancel_export)
        self._cancel_btn.hide()
        tbar.addWidget(self._cancel_btn)

        tbar.addStretch()

        self._settings_btn = QPushButton("输出目录")
        self._settings_btn.setObjectName("dirBtn")
        self._settings_btn.clicked.connect(self._show_settings)
        tbar.addWidget(self._settings_btn)

        self._export_btn = QPushButton("批量导出")
        self._export_btn.setObjectName("exportBtn")
        self._export_btn.clicked.connect(self._start_export)
        tbar.addWidget(self._export_btn)

        main_layout.addWidget(toolbar)

        # ── 分割线 ──
        sep = QWidget()
        sep.setFixedHeight(1)
        sep.setStyleSheet("background: #2c2e33;")
        main_layout.addWidget(sep)

        # ── 内容区 ──
        body = QSplitter(Qt.Orientation.Horizontal)
        body.setHandleWidth(6)

        # 左侧：导航缩略图 + 文件列表
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(4, 4, 4, 4)

        self._nav = NavigationWidget()
        self._nav.setObjectName("navWidget")
        left_layout.addWidget(self._nav)
        left_layout.addWidget(QLabel("文件列表"))
        self._file_list = QListWidget()
        left_layout.addWidget(self._file_list)

        self._add_file_btn = QPushButton("添加文件...")
        self._add_file_btn.clicked.connect(self._add_files)
        self._remove_file_btn = QPushButton("移除选中")
        self._remove_file_btn.clicked.connect(self._remove_selected_file)
        left_layout.addWidget(self._add_file_btn)
        left_layout.addWidget(self._remove_file_btn)
        body.addWidget(left_panel)

        # 中央：WSI 画布
        self._canvas = WSICanvas()
        body.addWidget(self._canvas)

        # 右侧：ROI 列表
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(4, 4, 4, 4)

        self._tissue_btn = QPushButton("组织检测 (HistoKit)")
        self._tissue_btn.clicked.connect(self._detect_tissue)
        right_layout.addWidget(self._tissue_btn)

        # ROI 位置编辑（选中后启用）
        roi_form = QFormLayout()
        self._roi_x_spin = QSpinBox()
        self._roi_x_spin.setRange(0, 9999999)
        self._roi_x_spin.setEnabled(False)
        roi_form.addRow("X:", self._roi_x_spin)
        self._roi_y_spin = QSpinBox()
        self._roi_y_spin.setRange(0, 9999999)
        self._roi_y_spin.setEnabled(False)
        roi_form.addRow("Y:", self._roi_y_spin)
        self._roi_w_spin = QSpinBox()
        self._roi_w_spin.setRange(1, 9999999)
        self._roi_w_spin.setEnabled(False)
        roi_form.addRow("W:", self._roi_w_spin)
        self._roi_h_spin = QSpinBox()
        self._roi_h_spin.setRange(1, 9999999)
        self._roi_h_spin.setEnabled(False)
        roi_form.addRow("H:", self._roi_h_spin)
        right_layout.addLayout(roi_form)

        self._roi_x_spin.valueChanged.connect(self._on_roi_spin_changed)
        self._roi_y_spin.valueChanged.connect(self._on_roi_spin_changed)
        self._roi_w_spin.valueChanged.connect(self._on_roi_spin_changed)
        self._roi_h_spin.valueChanged.connect(self._on_roi_spin_changed)

        right_layout.addWidget(QLabel("ROI 列表"))
        self._roi_list = QListWidget()
        right_layout.addWidget(self._roi_list)

        self._delete_roi_btn = QPushButton("删除选中 ROI")
        self._delete_roi_btn.clicked.connect(self._delete_selected_roi)
        self._clear_current_btn = QPushButton("清空当前文件")
        self._clear_current_btn.clicked.connect(self._clear_current_roi)
        self._clear_all_btn = QPushButton("清空全部 ROI")
        self._clear_all_btn.clicked.connect(self._clear_all_rois)
        right_layout.addWidget(self._delete_roi_btn)
        right_layout.addWidget(self._clear_current_btn)
        right_layout.addWidget(self._clear_all_btn)

        body.addWidget(right_panel)

        body.setSizes([200, 700, 200])
        main_layout.addWidget(body, 1)

    def _connect_signals(self) -> None:
        self._file_list.currentRowChanged.connect(self._on_file_selected)
        self._roi_manager.roi_added.connect(self._on_roi_added)
        self._roi_manager.roi_removed.connect(self._on_roi_removed)
        self._canvas.roi_created.connect(self._on_canvas_roi_created)
        self._canvas.roi_selected.connect(self._on_canvas_roi_selected)
        self._canvas.roi_rect_changed.connect(self._on_roi_rect_changed)
        self._canvas.roi_selection_changed.connect(self._on_roi_selection_changed)
        self._canvas.viewport_changed.connect(self._nav.update_viewport)
        self._nav.navigated.connect(self._on_nav_clicked)

    def _setup_menu(self) -> None:
        menubar = self.menuBar()
        file_menu = menubar.addMenu("文件")
        file_menu.addAction("添加文件...", self._add_files)
        file_menu.addSeparator()
        file_menu.addAction("退出", self.close)

        view_menu = menubar.addMenu("显示")
        self._theme_action = QAction("浅色模式", self)
        self._theme_action.setCheckable(True)
        self._theme_action.setChecked(False)
        self._theme_action.triggered.connect(self._toggle_theme)
        view_menu.addAction(self._theme_action)

        help_menu = menubar.addMenu("帮助")
        help_menu.addAction("关于", lambda: QMessageBox.about(
            self, "关于",
            "病理裁剪工具 v0.2\n\n"
            "作者：Funvvell\n"
            "SDPC 病理切片批量裁剪与导出",
        ))

    def _apply_theme(self, name: str) -> None:
        qss = load_theme(name)
        if qss:
            from PySide6.QtWidgets import QApplication
            QApplication.instance().setStyleSheet(qss)
        self._current_theme = name

    def _toggle_theme(self) -> None:
        new_theme = "light" if self._current_theme == "dark" else "dark"
        self._apply_theme(new_theme)
        self._theme_action.setText("深色模式" if new_theme == "light" else "浅色模式")
        self._theme_action.setChecked(new_theme == "light")

    # ── 文件管理 ──────────────────────────────────────

    # ── 预设 ──────────────────────────────────────────

    def _load_presets(self) -> None:
        """加载预设列表到下拉框。"""
        self._presets: dict[str, dict] = {}
        if PRESETS_FILE.exists():
            try:
                self._presets = json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
            except Exception:
                self._presets = {}
        # 确保有默认预设
        if "默认" not in self._presets:
            self._presets["默认"] = {"mag": "20x", "ratio": "16:9", "w": 512, "h": 512}
        self._preset_cb.blockSignals(True)
        self._preset_cb.clear()
        self._preset_cb.addItems(list(self._presets.keys()))
        self._preset_cb.setCurrentText("默认")
        self._preset_cb.blockSignals(False)
        self._apply_preset("默认")

    def _save_preset(self) -> None:
        """保存当前配置为预设。"""
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "保存预设", "预设名称：")
        if not ok or not name.strip():
            return
        name = name.strip()
        self._presets[name] = {
            "mag": self._mag_cb.currentText(),
            "ratio": self._ratio_cb.currentText(),
            "w": self._frame_w_spin.value(),
            "h": self._frame_h_spin.value(),
        }
        try:
            PRESETS_FILE.parent.mkdir(parents=True, exist_ok=True)
            PRESETS_FILE.write_text(json.dumps(self._presets, indent=2), encoding="utf-8")
        except Exception:
            pass
        self._preset_cb.blockSignals(True)
        self._preset_cb.clear()
        self._preset_cb.addItems(list(self._presets.keys()))
        self._preset_cb.setCurrentText(name)
        self._preset_cb.blockSignals(False)

    def _apply_preset(self, name: str) -> None:
        """应用预设。"""
        preset = self._presets.get(name)
        if not preset:
            return
        self._mag_cb.blockSignals(True)
        self._ratio_cb.blockSignals(True)
        self._frame_w_spin.blockSignals(True)
        self._frame_h_spin.blockSignals(True)
        self._mag_cb.setCurrentText(preset.get("mag", "20x"))
        self._ratio_cb.setCurrentText(preset.get("ratio", "16:9"))
        self._frame_w_spin.setValue(preset.get("w", 512))
        self._frame_h_spin.setValue(preset.get("h", 512))
        self._mag_cb.blockSignals(False)
        self._ratio_cb.blockSignals(False)
        self._frame_w_spin.blockSignals(False)
        self._frame_h_spin.blockSignals(False)
        self._canvas.set_frame_size(preset.get("w", 512), preset.get("h", 512))

    # ── 文件管理 ──────────────────────────────────────

    def _add_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择 SDPC 文件", "",
            "SDPC 文件 (*.sdpc);;所有文件 (*)",
        )
        for fp in files:
            path = Path(fp)
            if path in self._readers:
                continue
            try:
                reader = SDPCReader(path)
                self._readers[path] = reader
                item = QListWidgetItem(path.name)
                item.setData(Qt.ItemDataRole.UserRole, str(path))
                self._file_list.addItem(item)
            except SDPCReadError as e:
                QMessageBox.warning(self, "打开失败", str(e))

    def _remove_selected_file(self) -> None:
        row = self._file_list.currentRow()
        if row < 0:
            return
        item = self._file_list.takeItem(row)
        path_str = item.data(Qt.ItemDataRole.UserRole)
        path = Path(path_str) if path_str else None
        if path and path in self._readers:
            self._roi_manager.clear_slide_rois(path)
            del self._readers[path]
            if self._current_slide == path:
                self._current_slide = None

    def _on_file_selected(self, row: int) -> None:
        if row < 0:
            return
        item = self._file_list.item(row)
        path_str = item.data(Qt.ItemDataRole.UserRole)
        path = Path(path_str) if path_str else None
        if path and path in self._readers:
            reader = self._readers[path]
            self._current_slide = path
            self._canvas.load_slide(reader)
            self._refresh_roi_list()
            self._restore_roi_on_canvas()
            self._status_label.setText(f"当前: {path.name}")
            self._update_nav_thumb(reader)
            if self._mag_cb.currentText() != "自定义":
                self._auto_calc_frame()

    def _update_nav_thumb(self, reader) -> None:
        thumb = reader.thumbnail
        h, w, ch = thumb.shape
        img_bytes = thumb.tobytes()
        img = QImage(img_bytes, w, h, w * ch, QImage.Format.Format_RGB888)
        pix = QPixmap.fromImage(img)
        self._nav.set_thumbnail(pix, reader.full_width, reader.full_height)

    def _on_nav_clicked(self, scene_x: float, scene_y: float) -> None:
        self._canvas.centerOn(scene_x, scene_y)
        self._canvas._emit_viewport()

    # ── ROI 交互 ──────────────────────────────────────

    def _auto_calc_frame(self) -> None:
        mag_text = self._mag_cb.currentText()
        ratio_text = self._ratio_cb.currentText()
        if mag_text == "自定义" or ratio_text == "Free":
            return
        mpp = None
        if self._current_slide and self._current_slide in self._readers:
            mpp = self._readers[self._current_slide].mpp
        if not mpp or mpp <= 0:
            return
        mag = float(mag_text.rstrip("x"))
        FN = 22.0
        fov_mm = FN / mag
        if ratio_text == "1:1":
            w_mm = h_mm = fov_mm / 1.4142
        elif ratio_text == "4:3":
            w_mm = fov_mm * 4 / 5
            h_mm = fov_mm * 3 / 5
        elif ratio_text == "3:2":
            w_mm = fov_mm * 3 / 3.606
            h_mm = fov_mm * 2 / 3.606
        elif ratio_text == "16:9":
            diag = (16 ** 2 + 9 ** 2) ** 0.5
            w_mm = fov_mm * 16 / diag
            h_mm = fov_mm * 9 / diag
        else:
            return
        px_w = round(w_mm * 1000 / mpp)
        px_h = round(h_mm * 1000 / mpp)
        self._frame_w_spin.blockSignals(True)
        self._frame_h_spin.blockSignals(True)
        self._frame_w_spin.setValue(px_w)
        self._frame_h_spin.setValue(px_h)
        self._frame_w_spin.blockSignals(False)
        self._frame_h_spin.blockSignals(False)
        self._update_frame_size()

    def _update_frame_size(self) -> None:
        w = self._frame_w_spin.value()
        h = self._frame_h_spin.value()
        self._canvas.set_frame_size(w, h)
        sender = self.sender()
        if sender in (self._frame_w_spin, self._frame_h_spin):
            self._mag_cb.blockSignals(True)
            self._mag_cb.setCurrentText("自定义")
            self._mag_cb.blockSignals(False)
            self._ratio_cb.blockSignals(True)
            self._ratio_cb.setCurrentText("Free")
            self._ratio_cb.blockSignals(False)

    def _toggle_roi_mode(self, checked: bool) -> None:
        self._canvas.set_roi_mode(checked)
        if checked:
            self._update_frame_size()
            self._status_label.setText(
                f"ROI 模式 | 框 {self._frame_w_spin.value()}×{self._frame_h_spin.value()} | 空格创建"
            )
            self._canvas.setFocus()
        else:
            self._status_label.setText("浏览模式")

    def _on_canvas_roi_created(self, roi_id: str, rect) -> None:
        if self._current_slide is None or self._current_slide not in self._readers:
            return
        roi = ROIModel(
            slide_path=self._current_slide,
            x=int(rect.x()),
            y=int(rect.y()),
            w=int(rect.width()),
            h=int(rect.height()),
            id=roi_id,
        )
        self._roi_manager.add_roi(roi)

    def _on_canvas_roi_selected(self, roi_id: str) -> None:
        if roi_id == "__toggle_roi__":
            new_state = not self._roi_mode_btn.isChecked()
            self._roi_mode_btn.setChecked(new_state)
            self._toggle_roi_mode(new_state)
            return
        self._roi_manager.remove_roi(roi_id)
        if self._selected_roi_id == roi_id:
            self._selected_roi_id = None
            self._on_roi_selection_changed("")

    def _on_roi_rect_changed(self, roi_id: str, new_rect) -> None:
        """ROI 被鼠标拖拽/缩放后更新 ROIModel 坐标。"""
        for roi in self._roi_manager.all_rois():
            if roi.id == roi_id:
                roi.x = int(new_rect.x())
                roi.y = int(new_rect.y())
                roi.w = int(new_rect.width())
                roi.h = int(new_rect.height())
                self._refresh_roi_list()
                self._update_roi_spins()
                break

    def _on_roi_selection_changed(self, roi_id: str) -> None:
        """ROI 选中状态变化时更新工具栏。"""
        self._selected_roi_id = roi_id if roi_id else None
        enabled = bool(roi_id)
        self._roi_x_spin.setEnabled(enabled)
        self._roi_y_spin.setEnabled(enabled)
        self._roi_w_spin.setEnabled(enabled)
        self._roi_h_spin.setEnabled(enabled)
        self._update_roi_spins()

    def _on_roi_spin_changed(self) -> None:
        """工具栏数值变化时更新选中 ROI。"""
        if self._block_roi_spin or not self._selected_roi_id:
            return
        for roi in self._roi_manager.all_rois():
            if roi.id == self._selected_roi_id:
                new_rect = QRectF(
                    self._roi_x_spin.value(), self._roi_y_spin.value(),
                    self._roi_w_spin.value(), self._roi_h_spin.value(),
                )
                roi.x = int(new_rect.x())
                roi.y = int(new_rect.y())
                roi.w = int(new_rect.width())
                roi.h = int(new_rect.height())
                # 更新画布上的 ROI 矩形
                self._canvas.update_roi_rect(self._selected_roi_id, new_rect)
                self._refresh_roi_list()
                break

    def _update_roi_spins(self) -> None:
        """同步工具栏 ROI 数值到选中 ROI 的坐标。"""
        if not self._selected_roi_id:
            return
        for roi in self._roi_manager.all_rois():
            if roi.id == self._selected_roi_id:
                self._block_roi_spin = True
                self._roi_x_spin.setValue(roi.x)
                self._roi_y_spin.setValue(roi.y)
                self._roi_w_spin.setValue(roi.w)
                self._roi_h_spin.setValue(roi.h)
                self._block_roi_spin = False
                break

    def _on_roi_added(self, roi: ROIModel) -> None:
        self._refresh_roi_list()

    def _on_roi_removed(self, roi_id: str) -> None:
        self._canvas.remove_roi_rect(roi_id)
        self._refresh_roi_list()

    def _restore_roi_on_canvas(self) -> None:
        """切换文件后在画布上恢复当前文件的 ROI 矩形。"""
        if not self._current_slide:
            return
        for roi in self._roi_manager.get_slide_rois(self._current_slide):
            from PySide6.QtCore import QRectF
            self._canvas.add_roi_rect(roi.id, QRectF(roi.x, roi.y, roi.w, roi.h))

    def _refresh_roi_list(self) -> None:
        self._roi_list.clear()
        if self._current_slide is None:
            return
        rois = self._roi_manager.get_slide_rois(self._current_slide)
        for roi in rois:
            item = QListWidgetItem(
                f"ROI ({roi.x}, {roi.y}) "
                f"{roi.w}×{roi.h}"
            )
            item.setData(Qt.ItemDataRole.UserRole, roi.id)
            self._roi_list.addItem(item)

    def _delete_selected_roi(self) -> None:
        item = self._roi_list.currentItem()
        if item is None:
            return
        roi_id = item.data(Qt.ItemDataRole.UserRole)
        self._roi_manager.remove_roi(roi_id)

    def _clear_current_roi(self) -> None:
        if self._current_slide is None:
            return
        self._roi_manager.clear_slide_rois(self._current_slide)
        self._canvas.clear_roi_rects()
        self._refresh_roi_list()

    def _clear_all_rois(self) -> None:
        reply = QMessageBox.question(
            self, "确认",
            f"将删除全部 {len(self._roi_manager.all_rois())} 个 ROI，确定？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        for roi in list(self._roi_manager.all_rois()):
            self._roi_manager.remove_roi(roi.id)
        self._canvas.clear_roi_rects()
        self._refresh_roi_list()

    # ── 导出 ──────────────────────────────────────────

    def _show_settings(self) -> None:
        dialog = SettingsDialog(self._crop_config, self)
        if dialog.exec():
            self._crop_config = dialog.get_config()
            self._status_label.setText(
                f"输出: {self._crop_config.output_dir}"
            )

    def _start_export(self) -> None:
        # 清理上次导出线程
        if hasattr(self, '_export_thread') and self._export_thread.isRunning():
            self._exporter.cancel()
            self._export_thread.quit()
            self._export_thread.wait(3000)

        self._cleanup_stale_rois()
        all_rois = self._roi_manager.all_rois()

        if not all_rois:
            QMessageBox.information(self, "提示", "请先标注 ROI")
            return

        file_count = len(set(r.slide_path.name for r in all_rois))
        crop_w = self._frame_w_spin.value()
        crop_h = self._frame_h_spin.value()

        reply = QMessageBox.question(
            self, "确认导出",
            f"将导出 {len(all_rois)} 个 ROI（来自 {file_count} 个文件）\n\n"
            f"输出目录: {self._crop_config.output_dir}\n"
            f"尺寸: {crop_w}×{crop_h}\n"
            f"继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._crop_config.crop_width = crop_w
        self._crop_config.crop_height = crop_h
        self._crop_config.mag_label = self._mag_cb.currentText().rstrip("xX")

        # 显示进度条
        self._progress_bar.setRange(0, len(all_rois))
        self._progress_bar.setValue(0)
        self._progress_bar.setFormat(f"0/{len(all_rois)}")
        self._progress_bar.show()
        self._cancel_btn.show()
        self._export_btn.setEnabled(False)

        # 传路径字典给线程，由线程在后台打开 reader（不阻塞主线程）
        path_input: dict[Path, str | SDPCReader] = {}
        for path in set(roi.slide_path for roi in all_rois):
            if path in self._readers:
                path_input[path] = str(path)  # 传路径，线程内再打开

        self._exporter = BatchExporter(self._crop_config)
        self._export_thread = QThread()
        self._exporter.moveToThread(self._export_thread)

        self._export_thread.started.connect(
            lambda: self._exporter.run(all_rois, path_input),
            Qt.DirectConnection,
        )
        total_rois = len(all_rois)
        self._exporter.progress.connect(lambda v, t: (
            self._progress_bar.setValue(v),
            self._progress_bar.setFormat(f"{v}/{t}"),
        ))
        self._exporter.file_done.connect(self._on_export_file_done)
        self._exporter.finished.connect(self._on_export_finished)
        self._exporter.finished.connect(self._export_thread.quit)

        self._export_thread.start()

    def _on_export_file_done(self, path: str, status: str) -> None:
        if status != "ok":
            self._status_label.setText(f"导出失败: {path}")

    def _cancel_export(self) -> None:
        if hasattr(self, '_exporter'):
            self._exporter.cancel()

    def _on_export_finished(self) -> None:
        self._progress_bar.hide()
        self._cancel_btn.hide()
        self._export_btn.setEnabled(True)
        self._status_label.setText("导出完成")
        QMessageBox.information(
            self, "导出完成",
            f"导出完成！\n输出目录: {self._crop_config.output_dir}",
        )

    # ── 会话 ──────────────────────────────────────────

    def _session_path(self) -> Path:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        return SESSION_FILE

    def _save_session(self) -> None:
        data = {
            "rois": self._roi_manager.to_json(),
            "config": {
                "crop_width": self._crop_config.crop_width,
                "crop_height": self._crop_config.crop_height,
                "output_dir": str(self._crop_config.output_dir),
                "theme": self._current_theme,
            },
        }
        try:
            self._session_path().write_text(
                json.dumps(data, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def _load_session(self) -> None:
        path = self._session_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._roi_manager.from_json(data.get("rois", {"rois": []}))
            # 恢复 ROI 到画布（当前已加载的切片）
            if self._current_slide and self._current_slide in self._readers:
                self._refresh_roi_list()
                for roi in self._roi_manager.get_slide_rois(self._current_slide):
                    from PySide6.QtCore import QRectF
                    self._canvas.add_roi_rect(roi.id, QRectF(roi.x, roi.y, roi.w, roi.h))
            cfg = data.get("config", {})
            self._crop_config = CropConfig(
                output_dir=Path(
                    cfg.get("output_dir", str(Path.home() / "liver_crop_output")),
                ),
                crop_width=cfg.get("crop_width", 1024),
                crop_height=cfg.get("crop_height", 1024),
            )
            self._frame_w_spin.setValue(self._crop_config.crop_width)
            self._frame_h_spin.setValue(self._crop_config.crop_height)
            saved_theme = cfg.get("theme", "dark")
            if saved_theme != self._current_theme:
                self._apply_theme(saved_theme)
                self._theme_action.setText("深色模式" if saved_theme == "light" else "浅色模式")
                self._theme_action.setChecked(saved_theme == "light")
        except Exception:
            pass

    # ── 组织检测 ──────────────────────────────────────

    def _detect_tissue(self) -> None:
        """组织检测 → 对所选文件生成 ROI。"""
        if not self._readers:
            QMessageBox.information(self, "提示", "请先加载切片")
            return

        reader = self._readers.get(self._current_slide) or next(iter(self._readers.values()))
        tile_w = self._frame_w_spin.value()
        tile_h = self._frame_h_spin.value()

        dlg = TissueDialog(reader, tile_w, tile_h, self,
                           readers=self._readers, current_slide=self._current_slide)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        params = dlg.get_params()
        tile_w = params.get("tile_w", tile_w)
        tile_h = params.get("tile_h", tile_h)
        slides = list(self._readers.keys()) if params.get("scope") == "all" else [self._current_slide]

        tissue_kw = {k: v for k, v in params.items()
                     if k in ("open_radius", "close_radius", "fill_holes",
                              "remove_small", "min_area_pct")}

        from PySide6.QtCore import QRectF
        import uuid

        total_all = 0
        for slide_path in slides:
            if slide_path not in self._readers:
                continue
            reader = self._readers[slide_path]
            thumb = reader.thumbnail
            result = detect_tissue(thumb, **tissue_kw)

            scale_x = reader.full_width / thumb.shape[1]
            scale_y = reader.full_height / thumb.shape[0]

            if params.get("mode") == "grid":
                stride = params.get("stride", 2)
                rois_list = tissue_regions_to_rois_grid(
                    result["mask"], scale_x, scale_y, tile_w, tile_h,
                    tile_w * stride, tile_h * stride,
                    max_count=params["max_count"],
                )
            else:
                rois_list = tissue_regions_to_rois(
                    result["mask"], scale_x, scale_y, tile_w, tile_h,
                    max_count=params["max_count"],
                )

            for x, y, w, h in rois_list:
                roi = ROIModel(
                    slide_path=slide_path,
                    x=x, y=y, w=w, h=h, id=uuid.uuid4().hex[:12],
                )
                self._roi_manager.add_roi(roi)

            total_all += len(rois_list)

        # 刷新当前画布显示
        self._canvas.clear_roi_rects()
        if self._current_slide and self._current_slide in self._readers:
            for roi in self._roi_manager.get_slide_rois(self._current_slide):
                self._canvas.add_roi_rect(roi.id, QRectF(roi.x, roi.y, roi.w, roi.h))

        self._refresh_roi_list()
        self._status_label.setText(f"组织检测: 共 {total_all} 个 ROI")

    def _cleanup_stale_rois(self) -> None:
        active = set(self._readers.keys())
        stale = [r for r in self._roi_manager.all_rois() if r.slide_path not in active]
        for r in stale:
            self._roi_manager.remove_roi(r.id)

    # ── 退出 ──────────────────────────────────────────

    def closeEvent(self, event) -> None:
        if self._roi_manager.all_rois():
            reply = QMessageBox.question(
                self, "确认退出",
                "有未导出的 ROI，保存标注后退出？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
            )
            if reply == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
        self._save_session()
        event.accept()
