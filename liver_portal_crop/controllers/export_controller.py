"""导出控制器。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QDialog, QMessageBox

from liver_portal_crop.controllers.base import BaseController
from liver_portal_crop.dialogs import SettingsDialog
from liver_portal_crop.exporter import BatchExporter, CropConfig
from liver_portal_crop.preview_dialog import ROIPreviewDialog
from liver_portal_crop.roi import ROIModel

logger = logging.getLogger(__name__)

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
        logger.info("[导出] start_export 被调用")
        self.app._file_controller.cleanup_stale_rois()
        all_rois = self.roi_manager.all_rois()
        logger.info("[导出] cleanup 后有 %d 个 ROI", len(all_rois))

        if not all_rois:
            logger.info("[导出] 无 ROI，弹出提示")
            QMessageBox.information(self.app, "提示", "请先标注 ROI")
            return

        # 诊断：打印 ROI 的 slide_path 信息
        for i, roi in enumerate(all_rois[:3]):
            logger.info(
                "[导出] ROI #%d: slide_path=%r (type=%s), xywh=(%d,%d,%d,%d)",
                i, roi.slide_path, type(roi.slide_path).__name__,
                roi.x, roi.y, roi.w, roi.h,
            )
        if len(all_rois) > 3:
            logger.info("[导出] ... 共 %d 个 ROI", len(all_rois))

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
            logger.info("[导出] 用户取消")
            return

        logger.info("[导出] 用户确认，开始 run_export")
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
        logger.info("[导出] run_export 被调用, %d 个 ROI", len(rois))

        # 清理上次导出线程
        old_thread = getattr(self.app, '_export_thread', None)
        if old_thread is not None and old_thread.isRunning():
            logger.warning("[导出] 上次导出线程仍在运行，强制终止")
            self.app._exporter.cancel()
            old_thread.quit()
            if not old_thread.wait(3000):
                logger.warning("[导出] 旧线程 3s 未退出，强制 terminate")
                old_thread.terminate()
                old_thread.wait(2000)

        crop_w = self.app._frame_w_spin.value()
        crop_h = self.app._frame_h_spin.value()

        self.app._crop_config.crop_width = crop_w
        self.app._crop_config.crop_height = crop_h
        self.app._crop_config.mag_label = self.app._mag_cb.currentText().rstrip("xX")

        self._show_export_progress(len(rois))
        self.app._export_btn.setEnabled(False)
        self.app._preview_export_all_btn.setEnabled(False)
        self.app._preview_export_sel_btn.setEnabled(False)

        path_input = {}
        readers_keys = set(self.readers.keys())
        for path in set(roi.slide_path for roi in rois):
            if path in self.readers:
                path_input[path] = self.readers[path]
                logger.info(
                    "[导出] path_input: %s -> reader (复用已有)", path.name,
                )
            else:
                logger.warning(
                    "[导出] path_input: %r 不在 readers 中 (readers keys: %s)",
                    path, [k.name for k in readers_keys],
                )

        logger.info("[导出] path_input 共 %d 个文件", len(path_input))

        self.app._exporter = BatchExporter(self.app._crop_config)
        self.app._export_thread = QThread()
        self.app._exporter.moveToThread(self.app._export_thread)

        self.app._export_thread.started.connect(
            lambda: self.app._exporter.run(rois, path_input),
        )
        self.app._exporter.progress.connect(
            lambda v, t: self._update_export_progress(v, t),
        )
        self.app._exporter.file_done.connect(self.app._on_export_file_done)
        self.app._exporter.finished.connect(self.app._on_export_finished)
        self.app._exporter.finished.connect(self.app._export_thread.quit)

        self.app._export_thread.start()
        logger.info("[导出] 线程已启动")

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
        logger.info("[导出] file_done: %s -> %s", path, status)
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
        logger.info("[导出] 导出完成")
        self.hide_export_progress()
        self.app._export_btn.setEnabled(True)
        self.app._preview_export_all_btn.setEnabled(True)
        self.app._preview_export_sel_btn.setEnabled(True)
        self.app._status_label.setText("导出完成")
        QMessageBox.information(
            self.app, "导出完成",
            f"导出完成！\n输出目录: {self.app._crop_config.output_dir}",
        )
