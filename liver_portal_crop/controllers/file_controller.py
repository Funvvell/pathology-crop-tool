"""文件管理控制器。"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QFileDialog, QListWidgetItem, QMessageBox

from liver_portal_crop.controllers.base import BaseController
from liver_portal_crop.reader import SDPCReader, SDPCReadError

if TYPE_CHECKING:
    from liver_portal_crop.app import MainWindow


class FileController(BaseController):
    """文件管理：添加、移除、切换、导航缩略图。"""

    def add_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self.app, "选择 SDPC 文件", "",
            "SDPC 文件 (*.sdpc);;所有文件 (*)",
        )
        for fp in files:
            path = Path(fp)
            if path in self.readers:
                continue
            try:
                reader = SDPCReader(path)
                self.readers[path] = reader
                item = QListWidgetItem(path.name)
                item.setData(Qt.ItemDataRole.UserRole, str(path))
                self.app._file_list.addItem(item)
            except SDPCReadError as e:
                QMessageBox.warning(self.app, "打开失败", str(e))

    def remove_selected_file(self) -> None:
        row = self.app._file_list.currentRow()
        if row < 0:
            return
        item = self.app._file_list.takeItem(row)
        path_str = item.data(Qt.ItemDataRole.UserRole)
        path = Path(path_str) if path_str else None
        if path and path in self.readers:
            self.roi_manager.clear_slide_rois(path)
            del self.readers[path]
            if self.current_slide == path:
                self.app._current_slide = None

    def on_file_selected(self, row: int) -> None:
        if row < 0:
            return
        item = self.app._file_list.item(row)
        path_str = item.data(Qt.ItemDataRole.UserRole)
        path = Path(path_str) if path_str else None
        if path and path in self.readers:
            reader = self.readers[path]
            self.app._current_slide = path
            self.canvas.load_slide(reader)
            self.app._refresh_roi_list()
            self.app._restore_roi_on_canvas()
            self.app._status_label.setText(f"当前: {path.name}")
            self._update_nav_thumb(reader)
            if self.app._mag_cb.currentText() != "自定义":
                self.app._auto_calc_frame()

    def _update_nav_thumb(self, reader: SDPCReader) -> None:
        thumb = reader.thumbnail
        h, w, ch = thumb.shape
        img_bytes = thumb.tobytes()
        img = QImage(img_bytes, w, h, w * ch, QImage.Format.Format_RGB888)
        pix = QPixmap.fromImage(img)
        self.app._nav.set_thumbnail(pix, reader.full_width, reader.full_height)

    def on_nav_clicked(self, scene_x: float, scene_y: float) -> None:
        self.canvas.centerOn(scene_x, scene_y)
        self.canvas._emit_viewport()

    def cleanup_stale_rois(self) -> None:
        active = set(self.readers.keys())
        stale = [r for r in self.roi_manager.all_rois() if r.slide_path not in active]
        for r in stale:
            self.roi_manager.remove_roi(r.id)
