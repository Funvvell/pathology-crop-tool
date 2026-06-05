"""MainWindow — 主窗口，组装所有模块。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

from PySide6.QtCore import Qt, QRectF, QThread, QTimer
from PySide6.QtWidgets import QDialog
from PySide6.QtGui import QAction, QImage, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMainWindow, QMenuBar, QMessageBox,
    QProgressBar, QPushButton, QSlider, QSpinBox, QSplitter, QStackedWidget,
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
from liver_portal_crop.preview_dialog import ROIPreviewDialog, ROIPreviewPanel
from liver_portal_crop.analysis_dialog import DeepLIIFAnalysisDialog
from liver_portal_crop.results_viewer import DeepLIIFResultsDialog
from liver_portal_crop.deepliif_runner import (
    DeepLIIFMode, DeepLIIFWorker, check_model_available, get_default_model_dir,
)

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
        self._selected_roi_id: str | None = None
        self._preview_refresh_timer = QTimer()
        self._preview_refresh_timer.setSingleShot(True)
        self._preview_refresh_timer.setInterval(500)
        self._preview_refresh_timer.timeout.connect(self._do_preview_refresh)

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

        # ── 顶部工具栏（QStackedWidget：画布工具栏 / 预览工具栏）──
        self._toolbar_stack = QStackedWidget()
        self._toolbar_stack.setFixedHeight(36)

        # --- 画布工具栏 (index 0) ---
        canvas_tb = QWidget()
        canvas_tb.setObjectName("topToolbar")
        tbar = QHBoxLayout(canvas_tb)
        tbar.setContentsMargins(8, 0, 8, 0)
        tbar.setSpacing(4)

        self._status_label = QLabel("就绪")
        self._status_label.setObjectName("statusLabel")
        tbar.addWidget(self._status_label)

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

        tbar.addWidget(QLabel("角度:"))
        self._frame_angle_slider = QSlider(Qt.Orientation.Horizontal)
        self._frame_angle_slider.setRange(0, 359)
        self._frame_angle_slider.setSingleStep(5)
        self._frame_angle_slider.setPageStep(15)
        self._frame_angle_slider.setFixedWidth(100)
        self._frame_angle_slider.valueChanged.connect(self._on_frame_angle_changed)
        tbar.addWidget(self._frame_angle_slider)
        self._frame_angle_label = QLabel("0°")
        self._frame_angle_label.setFixedWidth(32)
        self._frame_angle_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        tbar.addWidget(self._frame_angle_label)

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
        self._cancel_op = None
        self._patch_worker = None
        self._patch_thread = None
        self._patch_results = None  # 缓存最近一次小块测试结果
        self._deepliif_results = None  # 缓存最近一次批量分析结果
        self._deepliif_worker = None
        self._deepliif_thread = None
        self._active_result_dlg = None  # 当前打开的结果对话框引用
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)
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

        self._preview_win_btn = QPushButton("👁 ROI 预览")
        self._preview_win_btn.setToolTip("切换到 ROI 缩略图预览视图")
        self._preview_win_btn.clicked.connect(self._toggle_preview_view)
        tbar.addWidget(self._preview_win_btn)

        self._toolbar_stack.addWidget(canvas_tb)  # index 0

        # --- 预览工具栏 (index 1) ---
        preview_tb = QWidget()
        preview_tb.setObjectName("topToolbar")
        ptbar = QHBoxLayout(preview_tb)
        ptbar.setContentsMargins(8, 0, 8, 0)
        ptbar.setSpacing(4)

        self._preview_status_label = QLabel("ROI 预览")
        self._preview_status_label.setObjectName("statusLabel")
        ptbar.addWidget(self._preview_status_label)

        ptbar.addSpacing(16)

        self._preview_select_all_btn = QPushButton("全选")
        self._preview_deselect_btn = QPushButton("全不选")
        self._preview_invert_btn = QPushButton("反选")
        ptbar.addWidget(self._preview_select_all_btn)
        ptbar.addWidget(self._preview_deselect_btn)
        ptbar.addWidget(self._preview_invert_btn)

        ptbar.addSpacing(16)
        ptbar.addWidget(QLabel("筛选:"))
        self._preview_filter_cb = QComboBox()
        self._preview_filter_cb.addItem("全部文件")
        self._preview_filter_cb.setMinimumWidth(100)
        ptbar.addWidget(self._preview_filter_cb)

        ptbar.addStretch()

        self._preview_count_label = QLabel("已选: 0/0")
        ptbar.addWidget(self._preview_count_label)

        self._preview_progress = QProgressBar()
        self._preview_progress.setObjectName("exportProgress")
        self._preview_progress.setRange(0, 100)
        self._preview_progress.setValue(0)
        self._preview_progress.setFixedWidth(160)
        self._preview_progress.setFixedHeight(18)
        self._preview_progress.hide()
        ptbar.addWidget(self._preview_progress)

        self._preview_cancel_btn = QPushButton("✕")
        self._preview_cancel_btn.setFixedSize(22, 22)
        self._preview_cancel_btn.setObjectName("cancelBtn")
        self._preview_cancel_btn.clicked.connect(self._on_cancel_clicked)
        self._preview_cancel_btn.hide()
        ptbar.addWidget(self._preview_cancel_btn)

        self._preview_settings_btn = QPushButton("输出目录")
        self._preview_settings_btn.setObjectName("dirBtn")
        self._preview_settings_btn.clicked.connect(self._show_settings)
        ptbar.addWidget(self._preview_settings_btn)

        self._preview_export_all_btn = QPushButton("批量导出")
        self._preview_export_all_btn.setObjectName("exportBtn")
        self._preview_export_all_btn.clicked.connect(self._preview_export_all)
        ptbar.addWidget(self._preview_export_all_btn)

        self._preview_export_sel_btn = QPushButton("导出选中")
        self._preview_export_sel_btn.setObjectName("exportBtn")
        self._preview_export_sel_btn.clicked.connect(self._preview_export_selected)
        ptbar.addWidget(self._preview_export_sel_btn)

        self._preview_back_btn = QPushButton("← 返回画布")
        self._preview_back_btn.clicked.connect(self._toggle_preview_view)
        ptbar.addWidget(self._preview_back_btn)

        self._toolbar_stack.addWidget(preview_tb)  # index 1

        main_layout.addWidget(self._toolbar_stack)

        # ── 分割线 ──
        sep = QWidget()
        sep.setFixedHeight(1)
        sep.setStyleSheet("background: #2c2e33;")
        main_layout.addWidget(sep)

        # ── 内容区 ──
        self._body = QSplitter(Qt.Orientation.Horizontal)
        self._body.setHandleWidth(6)

        # 左侧：导航缩略图 + 文件列表（预览模式下隐藏）
        self._left_panel = QWidget()
        left_layout = QVBoxLayout(self._left_panel)
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
        self._body.addWidget(self._left_panel)

        # 中央：QStackedWidget（画布 / 预览面板 切换）
        self._canvas = WSICanvas()
        self._stack = QStackedWidget()
        self._stack.addWidget(self._canvas)  # index 0 = 画布
        # 预览面板在首次切换时延迟创建
        self._preview_panel: ROIPreviewPanel | None = None
        self._body.addWidget(self._stack)

        # 右侧：ROI 列表
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(4, 4, 4, 4)

        self._tissue_btn = QPushButton("组织检测 (HistoKit)")
        self._tissue_btn.clicked.connect(self._detect_tissue)
        right_layout.addWidget(self._tissue_btn)

        self._deepliif_btn = QPushButton("🔬 DeepLIIF 分析")
        self._deepliif_btn.setToolTip("使用 DeepLIIF 进行 IHC 染色分析和细胞分割")
        self._deepliif_btn.clicked.connect(self._on_deepliif_btn_clicked)
        self._deepliif_btn.setVisible(False)
        right_layout.addWidget(self._deepliif_btn)

        self._clear_overlay_btn = QPushButton("清除分析叠加")
        self._clear_overlay_btn.clicked.connect(self._clear_analysis_overlay)
        self._clear_overlay_btn.setVisible(False)
        right_layout.addWidget(self._clear_overlay_btn)

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

        self._body.addWidget(right_panel)

        self._body.setSizes([200, 700, 200])
        main_layout.addWidget(self._body, 1)

    def _connect_signals(self) -> None:
        self._file_list.currentRowChanged.connect(self._on_file_selected)
        self._roi_list.currentRowChanged.connect(self._on_roi_list_selected)
        self._roi_manager.roi_added.connect(self._on_roi_added)
        self._roi_manager.roi_removed.connect(self._on_roi_removed)
        self._canvas.roi_created.connect(self._on_canvas_roi_created)
        self._canvas.roi_selected.connect(self._on_canvas_roi_selected)
        self._canvas.roi_rect_changed.connect(self._on_roi_rect_changed)
        self._canvas.roi_selection_changed.connect(self._on_roi_selection_changed)
        self._canvas.viewport_changed.connect(self._nav.update_viewport)
        self._canvas.frame_angle_changed.connect(self._on_canvas_frame_angle_changed)
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

        analysis_menu = menubar.addMenu("分析")
        analysis_menu.addAction("DeepLIIF 分析...", self._run_deepliif)
        analysis_menu.addSeparator()
        analysis_menu.addAction("设置模型路径...", self._set_deepliif_model_dir)

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
            self._presets["默认"] = {"mag": "20x", "ratio": "16:9", "w": 512, "h": 512, "angle": 0}
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
            "angle": self._frame_angle_slider.value(),
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
        self._frame_angle_slider.blockSignals(True)
        self._mag_cb.setCurrentText(preset.get("mag", "20x"))
        self._ratio_cb.setCurrentText(preset.get("ratio", "16:9"))
        self._frame_w_spin.setValue(preset.get("w", 512))
        self._frame_h_spin.setValue(preset.get("h", 512))
        self._frame_angle_slider.setValue(preset.get("angle", 0))
        self._frame_angle_label.setText(f"{self._frame_angle_slider.value()}°")
        self._mag_cb.blockSignals(False)
        self._ratio_cb.blockSignals(False)
        self._frame_w_spin.blockSignals(False)
        self._frame_h_spin.blockSignals(False)
        self._frame_angle_slider.blockSignals(False)
        self._canvas.set_frame_size(preset.get("w", 512), preset.get("h", 512))
        self._canvas.set_frame_angle(float(preset.get("angle", 0)))

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

    def _on_frame_angle_changed(self, value: int) -> None:
        """浮选框角度变化 → 同步到画布和标签。"""
        self._frame_angle_label.setText(f"{value}°")
        self._canvas.set_frame_angle(float(value))

    def _on_canvas_frame_angle_changed(self, angle: float) -> None:
        """画布右键拖拽改变角度 → 同步滑块和标签。"""
        self._frame_angle_slider.blockSignals(True)
        self._frame_angle_slider.setValue(int(round(angle)) % 360)
        self._frame_angle_slider.blockSignals(False)
        self._frame_angle_label.setText(f"{int(round(angle)) % 360}°")

    def _toggle_roi_mode(self, checked: bool) -> None:
        self._canvas.set_roi_mode(checked)
        if checked:
            self._update_frame_size()
            angle = self._frame_angle_slider.value()
            self._status_label.setText(
                f"ROI 模式 | 框 {self._frame_w_spin.value()}×{self._frame_h_spin.value()}"
                f" | 角度 {angle}° | 空格创建"
            )
            self._canvas.setFocus()
        else:
            self._status_label.setText("浏览模式")

    def _on_canvas_roi_created(self, roi_id: str, rect, angle: float = 0.0) -> None:
        if self._current_slide is None or self._current_slide not in self._readers:
            return
        roi = ROIModel(
            slide_path=self._current_slide,
            x=int(rect.x()),
            y=int(rect.y()),
            w=int(rect.width()),
            h=int(rect.height()),
            angle=round(angle, 2),
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

    def _on_roi_rect_changed(self, roi_id: str, new_rect, angle: float = 0.0) -> None:
        """ROI 被鼠标拖拽/缩放/旋转后更新 ROIModel 坐标。"""
        for roi in self._roi_manager.all_rois():
            if roi.id == roi_id:
                roi.x = int(new_rect.x())
                roi.y = int(new_rect.y())
                roi.w = int(new_rect.width())
                roi.h = int(new_rect.height())
                roi.angle = round(angle, 2)
                self._refresh_roi_list()
                self._update_roi_spins()
                break
        self._notify_preview_rois_changed()

    def _on_roi_selection_changed(self, roi_id: str) -> None:
        """ROI 选中状态变化时更新工具栏和列表。"""
        self._selected_roi_id = roi_id if roi_id else None
        enabled = bool(roi_id)
        self._roi_x_spin.setEnabled(enabled)
        self._roi_y_spin.setEnabled(enabled)
        self._roi_w_spin.setEnabled(enabled)
        self._roi_h_spin.setEnabled(enabled)
        self._update_roi_spins()

        # 同步右侧列表选中状态（阻止信号避免循环）
        if roi_id:
            self._roi_list.blockSignals(True)
            for i in range(self._roi_list.count()):
                item = self._roi_list.item(i)
                if item and item.data(Qt.ItemDataRole.UserRole) == roi_id:
                    self._roi_list.setCurrentRow(i)
                    break
            self._roi_list.blockSignals(False)
        else:
            self._roi_list.blockSignals(True)
            self._roi_list.clearSelection()
            self._roi_list.blockSignals(False)

        # 同步预览面板高亮
        if self._preview_panel and roi_id:
            self._preview_panel.on_roi_selected(roi_id)

    def _on_roi_list_selected(self, row: int) -> None:
        """右侧列表选中 ROI 时，画布同步选中。"""
        if row < 0:
            return
        item = self._roi_list.item(row)
        if item is None:
            return
        roi_id = item.data(Qt.ItemDataRole.UserRole)
        if roi_id:
            self._canvas.select_roi(roi_id)

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
        self._notify_preview_rois_changed()

    def _on_roi_removed(self, roi_id: str) -> None:
        self._canvas.remove_roi_rect(roi_id)
        self._refresh_roi_list()
        self._notify_preview_rois_changed()

    def _restore_roi_on_canvas(self) -> None:
        """切换文件后在画布上恢复当前文件的 ROI 矩形。"""
        if not self._current_slide:
            return
        for roi in self._roi_manager.get_slide_rois(self._current_slide):
            from PySide6.QtCore import QRectF
            self._canvas.add_roi_rect(roi.id, QRectF(roi.x, roi.y, roi.w, roi.h),
                                       angle=roi.angle)

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
        """批量导出全部 ROI。"""
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
            f"尺寸: {crop_w}x{crop_h}\n"
            f"继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._run_export(all_rois)

    def _show_preview_dialog(self) -> None:
        """打开 ROI 预览对话框，选择后导出选中项。"""
        self._cleanup_stale_rois()
        all_rois = self._roi_manager.all_rois()

        if not all_rois:
            QMessageBox.information(self, "提示", "请先标注 ROI")
            return

        dlg = ROIPreviewDialog(
            all_rois,
            self._readers,
            self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        selected_ids = set(dlg.get_selected_ids())
        selected_rois = [r for r in all_rois if r.id in selected_ids]
        if selected_rois:
            self._run_export(selected_rois)

    # ── 画布/预览面板切换 ─────────────────────────────

    def _toggle_preview_view(self) -> None:
        """切换中心区域：画布 ↔ 预览面板，同步切换工具栏和左侧面板。"""
        if self._stack.currentIndex() == 1:
            # 切回画布：保存预览模式的 splitter，恢复画布模式的
            self._preview_splitter_sizes = self._body.sizes()
            self._stack.setCurrentIndex(0)
            self._toolbar_stack.setCurrentIndex(0)
            self._left_panel.show()
            self._tissue_btn.show()
            self._deepliif_btn.hide()
            self._clear_overlay_btn.hide()
            if hasattr(self, '_canvas_splitter_sizes'):
                self._body.setSizes(self._canvas_splitter_sizes)
        else:
            # 切到预览面板：保存画布模式的 splitter，恢复预览模式的
            self._canvas_splitter_sizes = self._body.sizes()
            if self._preview_panel is None:
                self._cleanup_stale_rois()
                all_rois = self._roi_manager.all_rois()
                self._preview_panel = ROIPreviewPanel(
                    all_rois, self._readers,
                    toolbar_buttons=(
                        self._preview_select_all_btn,
                        self._preview_deselect_btn,
                        self._preview_invert_btn,
                    ),
                    filter_cb=self._preview_filter_cb,
                    count_label=self._preview_count_label,
                )
                self._preview_panel.roi_selected.connect(self._on_preview_roi_selected)
                self._stack.addWidget(self._preview_panel)
            self._stack.setCurrentIndex(1)
            self._toolbar_stack.setCurrentIndex(1)
            self._left_panel.hide()
            self._tissue_btn.hide()
            self._deepliif_btn.show()
            if hasattr(self, '_preview_splitter_sizes'):
                self._body.setSizes(self._preview_splitter_sizes)
            # 同步当前选中
            if self._selected_roi_id:
                self._preview_panel.on_roi_selected(self._selected_roi_id)

    def _on_preview_roi_selected(self, roi_id: str) -> None:
        """预览面板选中 ROI → 同步到画布和列表。"""
        self._canvas.select_roi(roi_id)

    def _preview_export_all(self) -> None:
        """预览模式：批量导出当前文件的所有 ROI。"""
        self._cleanup_stale_rois()
        if self._current_slide:
            rois = self._roi_manager.get_slide_rois(self._current_slide)
        else:
            rois = self._roi_manager.all_rois()
        if not rois:
            QMessageBox.information(self, "提示", "没有可导出的 ROI")
            return
        self._run_export(rois)

    def _preview_export_selected(self) -> None:
        """预览模式：导出预览面板中勾选的 ROI。"""
        if not self._preview_panel:
            return
        selected_ids = set(self._preview_panel.get_selected_ids())
        if not selected_ids:
            QMessageBox.information(self, "提示", "请先勾选要导出的 ROI")
            return
        all_rois = self._roi_manager.all_rois()
        selected_rois = [r for r in all_rois if r.id in selected_ids]
        self._run_export(selected_rois)

    def _notify_preview_rois_changed(self) -> None:
        """通知预览面板刷新 ROI 缩略图（防抖）。"""
        if self._preview_panel:
            self._preview_refresh_timer.start()

    def _do_preview_refresh(self) -> None:
        """实际执行预览面板刷新。"""
        if self._preview_panel:
            self._preview_panel.on_rois_changed(
                self._roi_manager.all_rois(), self._readers
            )

    def _run_export(self, rois: list) -> None:
        """执行批量导出（共用逻辑）。"""
        # 清理上次导出线程
        if hasattr(self, '_export_thread') and self._export_thread.isRunning():
            self._exporter.cancel()
            self._export_thread.quit()
            self._export_thread.wait(3000)

        crop_w = self._frame_w_spin.value()
        crop_h = self._frame_h_spin.value()

        self._crop_config.crop_width = crop_w
        self._crop_config.crop_height = crop_h
        self._crop_config.mag_label = self._mag_cb.currentText().rstrip("xX")

        # 显示进度条（在当前激活的工具栏上）
        self._show_export_progress(len(rois))
        self._export_btn.setEnabled(False)
        self._preview_export_all_btn.setEnabled(False)
        self._preview_export_sel_btn.setEnabled(False)

        # 传路径字典给线程
        path_input: dict[Path, str | SDPCReader] = {}
        for path in set(roi.slide_path for roi in rois):
            if path in self._readers:
                path_input[path] = str(path)

        self._exporter = BatchExporter(self._crop_config)
        self._export_thread = QThread()
        self._exporter.moveToThread(self._export_thread)

        self._export_thread.started.connect(
            lambda: self._exporter.run(rois, path_input),
            Qt.DirectConnection,
        )
        self._exporter.progress.connect(lambda v, t: self._update_export_progress(v, t))
        self._exporter.file_done.connect(self._on_export_file_done)
        self._exporter.finished.connect(self._on_export_finished)
        self._exporter.finished.connect(self._export_thread.quit)

        self._export_thread.start()

    def _show_export_progress(self, total: int) -> None:
        """在当前激活的工具栏上显示进度条，并记录导出发起的工具栏。"""
        self._export_toolbar_index = self._stack.currentIndex()
        if self._export_toolbar_index == 0:
            bar, cancel = self._progress_bar, self._cancel_btn
        else:
            bar, cancel = self._preview_progress, self._preview_cancel_btn
        bar.setRange(0, total)
        bar.setValue(0)
        bar.setFormat(f"0/{total}")
        bar.show()
        cancel.show()

    def _update_export_progress(self, current: int, total: int) -> None:
        """更新导出发起时的工具栏进度条（不随视图切换改变）。"""
        if getattr(self, '_export_toolbar_index', 0) == 0:
            bar = self._progress_bar
        else:
            bar = self._preview_progress
        bar.setValue(current)
        bar.setFormat(f"{current}/{total}")

    def _hide_export_progress(self) -> None:
        """隐藏两个工具栏的进度条。"""
        self._progress_bar.hide()
        self._cancel_btn.hide()
        self._preview_progress.hide()
        self._preview_cancel_btn.hide()

    def _on_export_file_done(self, path: str, status: str) -> None:
        if status != "ok":
            self._status_label.setText(f"导出失败: {path}")

    def _cancel_export(self) -> None:
        if hasattr(self, '_exporter'):
            self._exporter.cancel()

    def _on_cancel_clicked(self) -> None:
        """取消按钮点击 — 委托给当前操作。"""
        if self._cancel_op is not None:
            self._cancel_op()
        else:
            self._cancel_export()

    def _on_export_finished(self) -> None:
        self._hide_export_progress()
        self._export_btn.setEnabled(True)
        self._preview_export_all_btn.setEnabled(True)
        self._preview_export_sel_btn.setEnabled(True)
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
                    self._canvas.add_roi_rect(roi.id, QRectF(roi.x, roi.y, roi.w, roi.h),
                                               angle=roi.angle)
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
                self._canvas.add_roi_rect(roi.id, QRectF(roi.x, roi.y, roi.w, roi.h),
                                           angle=roi.angle)

        self._refresh_roi_list()
        self._status_label.setText(f"组织检测: 共 {total_all} 个 ROI")

    # ── DeepLIIF 分析 ──────────────────────────────────

    def _on_deepliif_btn_clicked(self) -> None:
        """DeepLIIF 按钮点击 — 根据状态分派。"""
        # 有打开的结果窗口 → 切换显示/隐藏
        if self._active_result_dlg is not None:
            if self._active_result_dlg.isVisible():
                self._active_result_dlg.hide()
            else:
                self._active_result_dlg.show()
                self._active_result_dlg.raise_()
                self._active_result_dlg.activateWindow()
            return

        if self._patch_results:
            self._show_patch_results()
        elif self._deepliif_results:
            self._show_deepliif_results()
        elif self._patch_worker is not None or self._deepliif_worker is not None:
            pass  # 推理进行中，无操作
        else:
            self._run_deepliif()

    def _run_deepliif(self) -> None:
        """启动 DeepLIIF 分析流程。"""
        if not self._readers:
            QMessageBox.information(self, "提示", "请先加载切片")
            return

        all_rois = self._roi_manager.all_rois()
        if not all_rois:
            QMessageBox.information(self, "提示", "请先标注或生成 ROI")
            return

        # 获取当前倍率
        mag_text = self._mag_cb.currentText() if hasattr(self, '_mag_cb') else "40x"

        dlg = DeepLIIFAnalysisDialog(
            rois=all_rois,
            readers=self._readers,
            current_slide=self._current_slide,
            magnification=mag_text,
            parent=self,
        )
        dlg.confirmed.connect(lambda: self._on_deepliif_confirmed(dlg))
        dlg.patch_confirmed.connect(lambda: self._on_patch_confirmed(dlg))
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.show()

    def _on_deepliif_confirmed(self, dlg: DeepLIIFAnalysisDialog):
        """分析对话框确认 — 启动后台推理。"""
        params = getattr(dlg, '_confirmed_params', None)
        selected_rois = getattr(dlg, '_confirmed_rois', None)
        if not params or not selected_rois:
            return

        try:
            self._start_deepliif_worker(params, selected_rois)
        except Exception as e:
            logger.error("启动 DeepLIIF 分析失败: %s", e, exc_info=True)
            QMessageBox.critical(self, "DeepLIIF 启动失败", str(e))

    def _start_deepliif_worker(self, params: dict, selected_rois: list):
        """创建并启动 DeepLIIF 推理线程。"""
        self._deepliif_results = None
        if params["mode"] == "local":
            mode = DeepLIIFMode.LOCAL
            model_dir = params["model_dir"]
            if not model_dir:
                QMessageBox.warning(self, "错误", "本地模式需要指定模型目录")
                return
            ok, msg = check_model_available(model_dir)
            if not ok:
                QMessageBox.warning(self, "模型不可用", msg)
                return
        else:
            mode = DeepLIIFMode.CLOUD
            model_dir = None

        # 显示进度（在预览工具栏上，因为 DeepLIIF 只在预览模式可用）
        self._preview_progress.setRange(0, len(selected_rois))
        self._preview_progress.setValue(0)
        self._preview_progress.setFormat(f"0/{len(selected_rois)}")
        self._preview_progress.show()
        self._preview_cancel_btn.show()
        self._deepliif_btn.setText("⏳ 分析中...")
        self._deepliif_btn.setToolTip("DeepLIIF 批量推理进行中…")
        self._status_label.setText("DeepLIIF 分析中...")

        # 创建 Worker 和线程（不能有 parent，否则无法 moveToThread）
        self._deepliif_worker = DeepLIIFWorker(
            mode=mode,
            rois=selected_rois,
            readers=self._readers,
            model_dir=model_dir,
            tile_size=params["tile_size"],
            seg_only=params["seg_only"],
        )
        self._deepliif_thread = QThread()
        self._deepliif_worker.moveToThread(self._deepliif_thread)

        # 信号连接
        self._deepliif_thread.started.connect(self._deepliif_worker.run)
        self._deepliif_worker.progress.connect(self._on_deepliif_progress)
        self._deepliif_worker.all_finished.connect(self._on_deepliif_finished)
        self._deepliif_worker.error.connect(self._on_deepliif_error)
        self._deepliif_worker.all_finished.connect(self._deepliif_cleanup)
        self._deepliif_worker.error.connect(self._deepliif_cleanup)

        # 设置取消委托
        self._cancel_op = lambda: self._deepliif_worker.cancel() if self._deepliif_worker else None

        self._deepliif_thread.start()

    def _on_deepliif_progress(self, msg: str, current: int, total: int):
        """DeepLIIF 推理进度更新。"""
        self._status_label.setText(msg)
        self._preview_progress.setMaximum(total)
        self._preview_progress.setValue(current)
        self._deepliif_btn.setText(f"⏳ {current}/{total}")

    def _deepliif_cleanup(self):
        """线程结束后清理 worker 和 thread。"""
        thread = getattr(self, '_deepliif_thread', None)
        worker = getattr(self, '_deepliif_worker', None)
        if thread is not None:
            thread.quit()
            thread.wait(3000)
        if worker is not None:
            worker.deleteLater()
        if thread is not None:
            thread.deleteLater()
        self._deepliif_thread = None
        self._deepliif_worker = None

    def _on_deepliif_finished(self, results: list):
        """DeepLIIF 推理完成 — 缓存结果，更新按钮。"""
        self._patch_results = None
        self._deepliif_btn.setEnabled(True)
        self._preview_progress.hide()
        self._preview_cancel_btn.hide()
        self._cancel_op = None

        if not results:
            self._deepliif_btn.setText("🔬 DeepLIIF 分析")
            self._deepliif_btn.setToolTip("使用 DeepLIIF 进行 IHC 染色分析和细胞分割")
            self._status_label.setText("DeepLIIF 分析: 无结果")
            return

        self._deepliif_results = results
        self._deepliif_btn.setText("🔬 查看分析结果")
        self._deepliif_btn.setToolTip("点击打开 DeepLIIF 分析结果")
        self._status_label.setText(f"DeepLIIF 分析完成: {len(results)} 个 ROI — 点击按钮查看结果")

    def _on_deepliif_error(self, msg: str):
        """DeepLIIF 推理错误。"""
        self._patch_results = None
        self._deepliif_results = None
        self._deepliif_btn.setEnabled(True)
        self._deepliif_btn.setText("🔬 DeepLIIF 分析")
        self._deepliif_btn.setToolTip("使用 DeepLIIF 进行 IHC 染色分析和细胞分割")
        self._preview_progress.hide()
        self._preview_cancel_btn.hide()
        self._cancel_op = None
        self._status_label.setText("DeepLIIF 分析出错")
        QMessageBox.warning(self, "DeepLIIF 错误", msg)

    def _show_deepliif_results(self):
        """打开缓存的批量分析结果对话框。"""
        if not self._deepliif_results:
            return
        tile_size = self._deepliif_results[0].get("tile_size", 512)
        dlg = DeepLIIFResultsDialog(
            self._deepliif_results, tile_size=tile_size, parent=self,
        )
        dlg.overlay_requested.connect(self._apply_overlay_to_canvas)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._active_result_dlg = dlg
        dlg.show()
        # 保存引用防止 GC
        self._deepliif_result_dialogs = getattr(self, '_deepliif_result_dialogs', [])
        self._deepliif_result_dialogs.append(dlg)
        # 对话框关闭后：移除引用，若无剩余则重置按钮
        def _on_closed():
            if dlg in self._deepliif_result_dialogs:
                self._deepliif_result_dialogs.remove(dlg)
            if self._active_result_dlg is dlg:
                self._active_result_dlg = None
            if not self._deepliif_result_dialogs:
                self._deepliif_results = None
                self._deepliif_btn.setText("🔬 DeepLIIF 分析")
                self._deepliif_btn.setToolTip("使用 DeepLIIF 进行 IHC 染色分析和细胞分割")
        dlg.destroyed.connect(_on_closed)

    # ── 小块测试（主窗口生命周期管理）──────────────────────

    def _on_patch_confirmed(self, dlg: DeepLIIFAnalysisDialog):
        """小块测试对话框确认 — 启动后台推理。"""
        patch_data = getattr(dlg, '_patch_data', None)
        if not patch_data:
            return
        try:
            self._start_patch_test(patch_data)
        except Exception as e:
            logger.error("启动小块测试失败: %s", e, exc_info=True)
            QMessageBox.critical(self, "小块测试启动失败", str(e))

    def _start_patch_test(self, data: dict):
        """创建并启动小块测试推理线程。"""
        from PySide6.QtCore import QObject, Signal as QSignal

        self._patch_results = None
        self._deepliif_btn.setText("⏳ 小块测试中...")
        self._deepliif_btn.setToolTip("小块推理进行中…")
        self._status_label.setText("小块测试推理中...")

        self._patch_thread = QThread()

        patch = data["patch"]
        mode = data["mode"]
        model_dir = data["model_dir"]
        tile_size = data["tile_size"]
        seg_only = data["seg_only"]
        patch_roi = data["patch_roi"]

        class _PatchWorker(QObject):
            finished = QSignal(dict)
            error = QSignal(str)
            def run(self_):
                try:
                    from liver_portal_crop.deepliif_runner import (
                        infer_local, infer_cloud, DeepLIIFMode,
                    )
                    if mode == DeepLIIFMode.LOCAL:
                        images, scoring = infer_local(
                            patch, model_dir, tile_size, seg_only,
                        )
                    else:
                        images, scoring = infer_cloud(
                            patch, resolution="40x", seg_only=seg_only,
                        )
                    images["IHC"] = patch
                    self_.finished.emit({
                        "roi_id": "patch_test",
                        "roi": patch_roi,
                        "images": images,
                        "scoring": scoring,
                        "tile_size": tile_size,
                    })
                except Exception as e:
                    self_.error.emit(str(e))

        self._patch_worker = _PatchWorker()
        self._patch_worker.moveToThread(self._patch_thread)
        self._patch_thread.started.connect(self._patch_worker.run)
        self._patch_worker.finished.connect(self._on_patch_done)
        self._patch_worker.error.connect(self._on_patch_error)
        self._patch_worker.finished.connect(self._patch_test_cleanup)
        self._patch_worker.error.connect(self._patch_test_cleanup)
        self._cancel_op = lambda: None  # 小块测试暂不支持取消
        self._patch_thread.start()

    def _on_patch_done(self, result: dict):
        """小块推理完成 — 缓存结果，更新按钮。"""
        self._patch_results = [result]
        self._deepliif_btn.setText("🔬 查看小块结果")
        self._deepliif_btn.setToolTip("点击重新打开小块测试结果")
        self._status_label.setText("小块测试完成 — 点击按钮查看结果")
        self._cancel_op = None

    def _on_patch_error(self, msg: str):
        """小块推理失败。"""
        self._deepliif_btn.setText("🔬 DeepLIIF 分析")
        self._deepliif_btn.setToolTip("使用 DeepLIIF 进行 IHC 染色分析和细胞分割")
        self._status_label.setText("小块测试出错")
        self._cancel_op = None
        QMessageBox.warning(self, "小块测试失败", msg)

    def _patch_test_cleanup(self):
        """小块测试线程结束后清理。"""
        thread = self._patch_thread
        worker = self._patch_worker
        if thread is not None:
            thread.quit()
            thread.wait(3000)
        if worker is not None:
            worker.deleteLater()
        if thread is not None:
            thread.deleteLater()
        self._patch_thread = None
        self._patch_worker = None

    def _show_patch_results(self):
        """打开缓存的小块测试结果对话框。"""
        if not self._patch_results:
            return
        tile_size = self._patch_results[0].get("tile_size", 512)
        dlg = DeepLIIFResultsDialog(
            self._patch_results, tile_size=tile_size, parent=self,
        )
        dlg.setWindowTitle("小块测试 — 调好参数后关闭，再点「开始分析」批量处理")
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._active_result_dlg = dlg
        dlg.show()
        # 保存引用防止 GC
        self._deepliif_result_dialogs = getattr(self, '_deepliif_result_dialogs', [])
        self._deepliif_result_dialogs.append(dlg)
        # 对话框关闭后清除缓存，按钮恢复
        def _on_closed():
            if dlg in self._deepliif_result_dialogs:
                self._deepliif_result_dialogs.remove(dlg)
            if self._active_result_dlg is dlg:
                self._active_result_dlg = None
            self._patch_results = None
            self._deepliif_btn.setText("🔬 DeepLIIF 分析")
            self._deepliif_btn.setToolTip("使用 DeepLIIF 进行 IHC 染色分析和细胞分割")
        dlg.destroyed.connect(_on_closed)

    def _apply_overlay_to_canvas(self, roi_id: str, qimage: QImage,
                                  x: int, y: int, w: int, h: int,
                                  opacity: float) -> None:
        """将分割结果叠加到画布上。"""
        self._canvas.set_overlay_opacity(opacity)
        self._canvas.add_overlay(roi_id, qimage, x, y, w, h)
        self._clear_overlay_btn.setVisible(True)
        self._status_label.setText(f"已叠加 ROI {roi_id[:8]} 的分割结果到画布")

    def _clear_analysis_overlay(self) -> None:
        """清除画布上的分析叠加。"""
        self._canvas.clear_overlays()
        self._clear_overlay_btn.setVisible(False)
        self._status_label.setText("已清除分析叠加")

    def _set_deepliif_model_dir(self) -> None:
        """设置 DeepLIIF 模型目录。"""
        current = str(get_default_model_dir())
        d = QFileDialog.getExistingDirectory(
            self, "选择 DeepLIIF 模型目录", current,
        )
        if d:
            ok, msg = check_model_available(d)
            if ok:
                QMessageBox.information(self, "模型就绪", msg)
            else:
                reply = QMessageBox.question(
                    self, "模型未就绪",
                    f"{msg}\n\n是否立即下载模型？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if reply == QMessageBox.StandardButton.Yes:
                    self._download_deepliif_model(d)

    def _download_deepliif_model(self, model_dir: str) -> None:
        """在 QThread 中下载 DeepLIIF 模型。"""
        from liver_portal_crop.deepliif_runner import ModelDownloadWorker
        from PySide6.QtWidgets import QProgressDialog

        # 进度对话框
        self._dl_progress = QProgressDialog("正在准备下载...", "取消", 0, 0, self)
        self._dl_progress.setWindowTitle("下载 DeepLIIF 模型")
        self._dl_progress.setMinimumDuration(0)
        self._dl_progress.setAutoClose(False)
        self._dl_progress.setAutoReset(False)
        self._dl_progress.setCancelButton(None)
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
        self._dl_worker.finished.connect(self._on_dl_finished_app)
        self._dl_worker.finished.connect(self._dl_thread.quit)
        self._dl_worker.finished.connect(self._dl_worker.deleteLater)
        self._dl_thread.finished.connect(self._dl_thread.deleteLater)

        self._dl_thread.start()

    def _on_dl_progress(self, pct: int, dl_mb: int, total_mb: int):
        """更新下载进度。"""
        if total_mb <= 0:
            return
        if self._dl_progress.maximum() == 0:
            self._dl_progress.setMaximum(100)
            cancel_btn = QPushButton("取消")
            self._dl_progress.setCancelButton(cancel_btn)
            self._dl_progress.canceled.connect(self._dl_worker.cancel)
        self._dl_progress.setValue(pct)
        self._dl_progress.setLabelText(
            f"正在下载... {dl_mb} / {total_mb} MB  ({pct}%)"
        )

    def _on_dl_finished_app(self, ok: bool, msg: str):
        """下载完成（菜单触发）。"""
        self._dl_progress.close()
        if ok:
            QMessageBox.information(self, "下载完成", msg)
        elif "取消" not in msg:
            QMessageBox.warning(self, "下载失败", msg)

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
