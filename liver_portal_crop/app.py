"""MainWindow — 主窗口，组装所有模块。"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, QRectF, QThread
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMainWindow, QMenuBar, QMessageBox,
    QPushButton, QProgressDialog, QSpinBox, QSplitter,
    QStatusBar, QVBoxLayout, QWidget,
)

from liver_portal_crop.canvas import WSICanvas
from liver_portal_crop.dialogs import SettingsDialog
from liver_portal_crop.exporter import BatchExporter, CropConfig
from liver_portal_crop.navigator import NavigationWidget
from liver_portal_crop.reader import SDPCReader, SDPCReadError
from liver_portal_crop.roi import ROIManager, ROIModel

SESSION_DIR = Path.home() / ".liver_portal_crop"
SESSION_FILE = SESSION_DIR / "session.json"


class MainWindow(QMainWindow):
    """应用程序主窗口。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("病理裁剪工具")
        self.resize(1200, 800)

        # 数据层
        self._readers: dict[Path, SDPCReader] = {}
        self._roi_manager = ROIManager()
        self._crop_config = CropConfig(
            output_dir=Path.home() / "liver_crop_output",
        )
        self._current_slide: Path | None = None

        # UI
        self._setup_ui()
        self._connect_signals()
        self._setup_menu()

        # 恢复会话
        self._load_session()

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(4, 4, 4, 4)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # ── 左侧：导航缩略图 + 文件列表 ──
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)

        self._nav = NavigationWidget()
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
        splitter.addWidget(left_panel)

        # ── 中央：WSI 画布 ──
        self._canvas = WSICanvas()
        splitter.addWidget(self._canvas)

        # ── 右侧：ROI 列表 ──
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)

        right_layout.addWidget(QLabel("ROI 列表"))
        self._roi_list = QListWidget()
        right_layout.addWidget(self._roi_list)

        self._delete_roi_btn = QPushButton("删除选中 ROI")
        self._delete_roi_btn.clicked.connect(self._delete_selected_roi)
        self._clear_roi_btn = QPushButton("清空当前 ROI")
        self._clear_roi_btn.clicked.connect(self._clear_current_roi)
        right_layout.addWidget(self._delete_roi_btn)
        right_layout.addWidget(self._clear_roi_btn)
        splitter.addWidget(right_panel)

        splitter.setSizes([200, 700, 200])
        main_layout.addWidget(splitter)

        # ── 底栏 ──
        status = QStatusBar()
        self.setStatusBar(status)

        self._status_label = QLabel("就绪")
        self._roi_mode_btn = QPushButton("ROI 绘制")
        self._roi_mode_btn.setCheckable(True)
        self._roi_mode_btn.clicked.connect(self._toggle_roi_mode)

        # 浮动框尺寸输入
        status.addPermanentWidget(QLabel("  框宽:"))
        self._frame_w_spin = QSpinBox()
        self._frame_w_spin.setRange(64, 99999)
        self._frame_w_spin.setSingleStep(64)
        self._frame_w_spin.setValue(512)
        self._frame_w_spin.valueChanged.connect(self._update_frame_size)
        status.addPermanentWidget(self._frame_w_spin)

        status.addPermanentWidget(QLabel("框高:"))
        self._frame_h_spin = QSpinBox()
        self._frame_h_spin.setRange(64, 99999)
        self._frame_h_spin.setSingleStep(64)
        self._frame_h_spin.setValue(512)
        self._frame_h_spin.valueChanged.connect(self._update_frame_size)
        status.addPermanentWidget(self._frame_h_spin)

        status.addPermanentWidget(QLabel("  [空格]创建ROI  "))

        self._settings_btn = QPushButton("导出设置...")
        self._settings_btn.clicked.connect(self._show_settings)
        self._export_btn = QPushButton("批量导出")
        self._export_btn.clicked.connect(self._start_export)

        status.addPermanentWidget(self._status_label)
        status.addPermanentWidget(self._roi_mode_btn)
        status.addPermanentWidget(self._settings_btn)
        status.addPermanentWidget(self._export_btn)

    def _connect_signals(self) -> None:
        self._file_list.currentRowChanged.connect(self._on_file_selected)
        self._roi_manager.roi_added.connect(self._on_roi_added)
        self._roi_manager.roi_removed.connect(self._on_roi_removed)
        self._canvas.roi_created.connect(self._on_canvas_roi_created)
        self._canvas.roi_selected.connect(self._on_canvas_roi_selected)
        # 导航缩略图
        self._canvas.viewport_changed.connect(self._nav.update_viewport)
        self._nav.navigated.connect(self._on_nav_clicked)

    def _setup_menu(self) -> None:
        menubar = self.menuBar()
        file_menu = menubar.addMenu("文件")
        file_menu.addAction("添加文件...", self._add_files)
        file_menu.addSeparator()
        file_menu.addAction("退出", self.close)

        help_menu = menubar.addMenu("帮助")
        help_menu.addAction("关于", lambda: QMessageBox.about(
            self, "关于",
            "病理裁剪工具 v0.1\n\n"
            "批量裁剪 SDPC 病理切片中的汇管区"

            "读取 SDPC 格式，标记 ROI，批量导出 TIFF。",
        ))

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
                self._file_list.addItem(path.name)
            except SDPCReadError as e:
                QMessageBox.warning(self, "打开失败", str(e))

    def _remove_selected_file(self) -> None:
        row = self._file_list.currentRow()
        if row < 0:
            return
        item = self._file_list.takeItem(row)
        # 找到对应的 path
        for path in list(self._readers.keys()):
            if path.name == item.text():
                self._roi_manager.clear_slide_rois(path)
                # 注意：不调用 close()（DLL 限制——关闭后无法再 open）
                del self._readers[path]
                break

    def _on_file_selected(self, row: int) -> None:
        if row < 0:
            return
        item = self._file_list.item(row)
        for path, reader in self._readers.items():
            if path.name == item.text():
                self._current_slide = path
                self._canvas.load_slide(reader)
                self._refresh_roi_list()
                self._status_label.setText(f"当前: {path.name}")
                # 更新导航缩略图
                self._update_nav_thumb(reader)
                break

    def _update_nav_thumb(self, reader) -> None:
        """从 reader 的缩略图创建 QPixmap 传给导航控件。"""
        thumb = reader.thumbnail
        h, w, ch = thumb.shape
        img_bytes = thumb.tobytes()
        img = QImage(img_bytes, w, h, w * ch, QImage.Format.Format_RGB888)
        pix = QPixmap.fromImage(img)
        self._nav.set_thumbnail(pix, reader.full_width, reader.full_height)

    def _on_nav_clicked(self, scene_x: float, scene_y: float) -> None:
        """点击导航缩略图 → 将主画布中心移到该位置。"""
        self._canvas.centerOn(scene_x, scene_y)
        self._canvas._emit_viewport()

    # ── ROI 交互 ──────────────────────────────────────

    def _update_frame_size(self) -> None:
        """将底栏的宽高值同步到画布浮动框。"""
        w = self._frame_w_spin.value()
        h = self._frame_h_spin.value()
        self._canvas.set_frame_size(w, h)

    def _toggle_roi_mode(self, checked: bool) -> None:
        self._canvas.set_roi_mode(checked)
        if checked:
            # 进入标注模式时同步当前宽高
            self._update_frame_size()
            self._status_label.setText(
                f"ROI 模式 | 框 {self._frame_w_spin.value()}×{self._frame_h_spin.value()} | 空格创建"
            )
            # 把键盘焦点给画布，空格键立即生效
            self._canvas.setFocus()
        else:
            self._status_label.setText("浏览模式")

    def _on_canvas_roi_created(self, roi_id: str, rect) -> None:
        if self._current_slide is None or self._current_slide not in self._readers:
            return
        roi = ROIModel(
            slide_path=self._current_slide,
            thumb_x=int(rect.x()),
            thumb_y=int(rect.y()),
            thumb_w=int(rect.width()),
            thumb_h=int(rect.height()),
            id=roi_id,
        )
        self._roi_manager.add_roi(roi)

    def _on_canvas_roi_selected(self, roi_id: str) -> None:
        """删除键按下 → 从画布和列表中移除该 ROI。"""
        self._roi_manager.remove_roi(roi_id)
        # 画布上的矩形由 _on_roi_removed 处理

    def _on_roi_added(self, roi: ROIModel) -> None:
        self._refresh_roi_list()

    def _on_roi_removed(self, roi_id: str) -> None:
        self._canvas.remove_roi_rect(roi_id)
        self._refresh_roi_list()

    def _refresh_roi_list(self) -> None:
        self._roi_list.clear()
        if self._current_slide is None:
            return
        rois = self._roi_manager.get_slide_rois(self._current_slide)
        for roi in rois:
            item = QListWidgetItem(
                f"ROI ({roi.thumb_x}, {roi.thumb_y}) "
                f"{roi.thumb_w}×{roi.thumb_h}"
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

    # ── 导出配置与执行 ────────────────────────────────

    def _show_settings(self) -> None:
        dialog = SettingsDialog(self._crop_config, self)
        if dialog.exec():
            self._crop_config = dialog.get_config()
            # 同步裁剪尺寸到底栏浮动框
            self._frame_w_spin.setValue(self._crop_config.crop_width)
            self._frame_h_spin.setValue(self._crop_config.crop_height)
            self._status_label.setText(
                f"输出: {self._crop_config.output_dir}"
            )

    def _start_export(self) -> None:
        # 清理无对应文件的 ROI（如上一会话遗留）
        self._cleanup_stale_rois()

        all_rois = self._roi_manager.all_rois()

        # 按文件分组统计
        from collections import Counter
        file_counts = Counter(r.slide_path.name for r in all_rois)
        detail = "\n".join(f"  {f}: {n} 个" for f, n in file_counts.items())

        if not all_rois:
            QMessageBox.information(self, "提示", "请先标注 ROI")
            return

        # 直接用底栏浮动框的尺寸作为导出尺寸
        crop_w = self._frame_w_spin.value()
        crop_h = self._frame_h_spin.value()

        reply = QMessageBox.question(
            self, "确认导出",
            f"将导出 {len(all_rois)} 个 ROI:\n{detail}\n\n"
            f"输出目录: {self._crop_config.output_dir}\n"
            f"尺寸: {crop_w}×{crop_h}\n"
            f"继续吗？",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # 更新 crop_config 尺寸
        self._crop_config.crop_width = crop_w
        self._crop_config.crop_height = crop_h

        # 进度对话框
        self._progress = QProgressDialog(
            "导出中...", "取消", 0, len(all_rois), self,
        )
        self._progress.setWindowTitle("批量导出")
        self._progress.setWindowModality(
            Qt.WindowModality.WindowModal
        )

        # 启动导出线程
        self._exporter = BatchExporter(self._crop_config)
        self._export_thread = QThread()
        self._exporter.moveToThread(self._export_thread)

        self._export_thread.started.connect(
            lambda: self._exporter.run(all_rois, self._readers)
        )
        self._exporter.progress.connect(self._progress.setValue)
        self._exporter.file_done.connect(self._on_export_file_done)
        self._exporter.finished.connect(self._on_export_finished)
        self._exporter.finished.connect(self._export_thread.quit)
        self._progress.canceled.connect(self._exporter.cancel)

        self._export_thread.start()

    def _on_export_file_done(self, path: str, status: str) -> None:
        if status != "ok":
            self._status_label.setText(f"导出失败: {path}")

    def _on_export_finished(self) -> None:
        self._progress.close()
        self._status_label.setText("导出完成")
        QMessageBox.information(
            self, "导出完成",
            f"导出完成！\n输出目录: {self._crop_config.output_dir}",
        )

    # ── 会话管理 ──────────────────────────────────────

    def _session_path(self) -> Path:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        return SESSION_FILE

    def _save_session(self) -> None:
        """保存会话（ROI + 配置）。"""
        data = {
            "rois": self._roi_manager.to_json(),
            "config": {
                "crop_width": self._crop_config.crop_width,
                "crop_height": self._crop_config.crop_height,
                "output_dir": str(self._crop_config.output_dir),
            },
        }
        try:
            self._session_path().write_text(
                json.dumps(data, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def _load_session(self) -> None:
        """恢复会话。"""
        path = self._session_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._roi_manager.from_json(
                data.get("rois", {"rois": []})
            )
            cfg = data.get("config", {})
            self._crop_config = CropConfig(
                output_dir=Path(
                    cfg.get(
                        "output_dir",
                        str(Path.home() / "liver_crop_output"),
                    )
                ),
                crop_width=cfg.get("crop_width", 1024),
                crop_height=cfg.get("crop_height", 1024),
            )
            # 同步到底栏浮动框
            self._frame_w_spin.setValue(self._crop_config.crop_width)
            self._frame_h_spin.setValue(self._crop_config.crop_height)
        except Exception:
            pass  # 损坏的会话文件忽略

    def _cleanup_stale_rois(self) -> None:
        """清除不再打开的文件的 ROI。"""
        active = set(self._readers.keys())
        stale = [r for r in self._roi_manager.all_rois()
                 if r.slide_path not in active]
        for r in stale:
            self._roi_manager.remove_roi(r.id)

    # ── 退出处理 ──────────────────────────────────────

    def closeEvent(self, event) -> None:
        """关闭时保存会话并关闭文件句柄。"""
        if self._roi_manager.all_rois():
            reply = QMessageBox.question(
                self, "确认退出",
                "有未导出的 ROI，保存标注后退出？",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
            )
            if reply == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
        self._save_session()
        # 不逐个 close reader（DLL 限制），OS 会在进程退出时清理
        event.accept()
