"""导出控制器。"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QDialog, QMessageBox

from liver_portal_crop.controllers.base import BaseController
from liver_portal_crop.dialogs import SettingsDialog
from liver_portal_crop.exporter import BatchExporter, CropConfig
from liver_portal_crop.preview_dialog import ROIPreviewDialog
from liver_portal_crop.roi import ROIModel

if TYPE_CHECKING:
    from liver_portal_crop.app import MainWindow


class ExportController(BaseController):
    """批量导出：设置、执行、进度管理、取消。"""

    def show_settings(self) -> None:
        dialog = SettingsDialog(self.app._crop_config, self.app)
        if dialog.exec():
            self.app._crop_config = dialog.get_config()
            self.app._status_label.setText(
                f"输出: {self.app._crop_config.output_dir}"
            )

    def start_export(self) -> None:
        self.app._file_controller.cleanup_stale_rois()
        all_rois = self.roi_manager.all_rois()

        if not all_rois:
            QMessageBox.information(self.app, "提示", "请先标注 ROI")
            return

        file_count = len(set(r.slide_path.name for r in all_rois))
        crop_w = self.app._frame_w_spin.value()
        crop_h = self.app._frame_h_spin.value()

        reply = QMessageBox.question(
            self.app, "确认导出",
            f"将导出 {len(all_rois)} 个 ROI（来自 {file_count} 个文件）\n\n"
            f"输出目录: {self.app._crop_config.output_dir}\n"
            f"尺寸: {crop_w}x{crop_h}\n"
            f"继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.run_export(all_rois)

    def show_preview_dialog(self) -> None:
        self.app._file_controller.cleanup_stale_rois()
        all_rois = self.roi_manager.all_rois()

        if not all_rois:
            QMessageBox.information(self.app, "提示", "请先标注 ROI")
            return

        dlg = ROIPreviewDialog(
            all_rois,
            self.readers,
            self.app,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        selected_ids = set(dlg.get_selected_ids())
        selected_rois = [r for r in all_rois if r.id in selected_ids]
        if selected_rois:
            self.run_export(selected_rois)

    def run_export(self, rois: list[ROIModel]) -> None:
        # 清理上次导出线程
        if hasattr(self.app, '_export_thread') and self.app._export_thread.isRunning():
            self.app._exporter.cancel()
            self.app._export_thread.quit()
            self.app._export_thread.wait(3000)

        crop_w = self.app._frame_w_spin.value()
        crop_h = self.app._frame_h_spin.value()

        self.app._crop_config.crop_width = crop_w
        self.app._crop_config.crop_height = crop_h
        self.app._crop_config.mag_label = self.app._mag_cb.currentText().rstrip("xX")

        self._show_export_progress(len(rois))
        self.app._export_btn.setEnabled(False)
        self.app._preview_export_all_btn.setEnabled(False)
        self.app._preview_export_sel_btn.setEnabled(False)

        path_input: dict[Path, str | "SDPCReader"] = {}
        for path in set(roi.slide_path for roi in rois):
            if path in self.readers:
                # 直接传已打开的 reader 对象，避免 DLL 重复打开同一文件导致死锁
                path_input[path] = self.readers[path]

        self.app._exporter = BatchExporter(self.app._crop_config)
        self.app._export_thread = QThread()
        self.app._exporter.moveToThread(self.app._export_thread)

        self.app._export_thread.started.connect(
            lambda: self.app._exporter.run(rois, path_input),
        )
        self.app._exporter.progress.connect(lambda v, t: self._update_export_progress(v, t))
        self.app._exporter.file_done.connect(self._on_export_file_done)
        self.app._exporter.finished.connect(self._on_export_finished)
        self.app._exporter.finished.connect(self.app._export_thread.quit)

        self.app._export_thread.start()

    def _show_export_progress(self, total: int) -> None:
        self.app._export_toolbar_index = self.app._stack.currentIndex()
        if self.app._export_toolbar_index == 0:
            bar, cancel = self.app._progress_bar, self.app._cancel_btn
        else:
            bar, cancel = self.app._preview_progress, self.app._preview_cancel_btn
        bar.setRange(0, total)
        bar.setValue(0)
        bar.setFormat(f"0/{total}")
        bar.show()
        cancel.show()

    def _update_export_progress(self, current: int, total: int) -> None:
        if getattr(self.app, '_export_toolbar_index', 0) == 0:
            bar = self.app._progress_bar
        else:
            bar = self.app._preview_progress
        bar.setValue(current)
        bar.setFormat(f"{current}/{total}")

    def hide_export_progress(self) -> None:
        self.app._progress_bar.hide()
        self.app._cancel_btn.hide()
        self.app._preview_progress.hide()
        self.app._preview_cancel_btn.hide()

    def _on_export_file_done(self, path: str, status: str) -> None:
        if status != "ok":
            self.app._status_label.setText(f"导出失败: {path}")

    def cancel_export(self) -> None:
        if hasattr(self.app, '_exporter'):
            self.app._exporter.cancel()

    def on_cancel_clicked(self) -> None:
        if self.app._cancel_op is not None:
            self.app._cancel_op()
        else:
            self.cancel_export()

    def on_export_finished(self) -> None:
        self.hide_export_progress()
        self.app._export_btn.setEnabled(True)
        self.app._preview_export_all_btn.setEnabled(True)
        self.app._preview_export_sel_btn.setEnabled(True)
        self.app._status_label.setText("导出完成")
        QMessageBox.information(
            self.app, "导出完成",
            f"导出完成！\n输出目录: {self.app._crop_config.output_dir}",
        )
