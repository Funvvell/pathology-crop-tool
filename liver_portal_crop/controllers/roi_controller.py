"""ROI 管理控制器。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QRectF
from PySide6.QtWidgets import QListWidgetItem, QMessageBox

from liver_portal_crop.controllers.base import BaseController
from liver_portal_crop.constants import FIELD_NUMBER_MM
from liver_portal_crop.roi import ROIModel

if TYPE_CHECKING:
    from liver_portal_crop.app import MainWindow


class ROIController(BaseController):
    """ROI 管理：创建、选中、编辑、删除、列表刷新。"""

    def auto_calc_frame(self) -> None:
        mag_text = self.app._mag_cb.currentText()
        ratio_text = self.app._ratio_cb.currentText()
        if mag_text == "自定义" or ratio_text == "Free":
            return
        mpp = None
        if self.current_slide and self.current_slide in self.readers:
            mpp = self.readers[self.current_slide].mpp
        if not mpp or mpp <= 0:
            return
        mag = float(mag_text.rstrip("x"))
        FN = FIELD_NUMBER_MM
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
        self.app._frame_w_spin.blockSignals(True)
        self.app._frame_h_spin.blockSignals(True)
        self.app._frame_w_spin.setValue(px_w)
        self.app._frame_h_spin.setValue(px_h)
        self.app._frame_w_spin.blockSignals(False)
        self.app._frame_h_spin.blockSignals(False)
        self._update_frame_size()

    def _update_frame_size(self) -> None:
        w = self.app._frame_w_spin.value()
        h = self.app._frame_h_spin.value()
        self.canvas.set_frame_size(w, h)
        sender = self.app.sender()
        if sender in (self.app._frame_w_spin, self.app._frame_h_spin):
            self.app._mag_cb.blockSignals(True)
            self.app._mag_cb.setCurrentText("自定义")
            self.app._mag_cb.blockSignals(False)
            self.app._ratio_cb.blockSignals(True)
            self.app._ratio_cb.setCurrentText("Free")
            self.app._ratio_cb.blockSignals(False)

    def on_frame_angle_changed(self, value: int) -> None:
        self.app._frame_angle_label.setText(f"{value}°")
        self.canvas.set_frame_angle(float(value))

    def on_canvas_frame_angle_changed(self, angle: float) -> None:
        self.app._frame_angle_slider.blockSignals(True)
        self.app._frame_angle_slider.setValue(int(round(angle)) % 360)
        self.app._frame_angle_slider.blockSignals(False)
        self.app._frame_angle_label.setText(f"{int(round(angle)) % 360}°")

    def toggle_roi_mode(self, checked: bool) -> None:
        self.canvas.set_roi_mode(checked)
        if checked:
            self._update_frame_size()
            angle = self.app._frame_angle_slider.value()
            self.app._status_label.setText(
                f"ROI 模式 | 框 {self.app._frame_w_spin.value()}×{self.app._frame_h_spin.value()}"
                f" | 角度 {angle}° | 空格创建"
            )
            self.canvas.setFocus()
        else:
            self.app._status_label.setText("浏览模式")

    def on_canvas_roi_created(self, roi_id: str, rect: QRectF, angle: float = 0.0) -> None:
        if self.current_slide is None or self.current_slide not in self.readers:
            return
        roi = ROIModel(
            slide_path=self.current_slide,
            x=int(rect.x()),
            y=int(rect.y()),
            w=int(rect.width()),
            h=int(rect.height()),
            angle=round(angle, 2),
            id=roi_id,
        )
        self.roi_manager.add_roi(roi)

    def on_canvas_roi_selected(self, roi_id: str) -> None:
        if roi_id == "__toggle_roi__":
            new_state = not self.app._roi_mode_btn.isChecked()
            self.app._roi_mode_btn.setChecked(new_state)
            self.toggle_roi_mode(new_state)
            return
        self.roi_manager.remove_roi(roi_id)
        if self.app._selected_roi_id == roi_id:
            self.app._selected_roi_id = None
            self.on_roi_selection_changed("")

    def on_roi_rect_changed(self, roi_id: str, new_rect: QRectF, angle: float = 0.0) -> None:
        for roi in self.roi_manager.all_rois():
            if roi.id == roi_id:
                roi.x = int(new_rect.x())
                roi.y = int(new_rect.y())
                roi.w = int(new_rect.width())
                roi.h = int(new_rect.height())
                roi.angle = round(angle, 2)
                self.app._refresh_roi_list()
                break
        self.app._notify_preview_rois_changed()

    def on_roi_selection_changed(self, roi_id: str) -> None:
        self.app._selected_roi_id = roi_id if roi_id else None

        if roi_id:
            self.app._roi_list.blockSignals(True)
            for i in range(self.app._roi_list.count()):
                item = self.app._roi_list.item(i)
                if item and item.data(Qt.ItemDataRole.UserRole) == roi_id:
                    self.app._roi_list.setCurrentRow(i)
                    break
            self.app._roi_list.blockSignals(False)
        else:
            self.app._roi_list.blockSignals(True)
            self.app._roi_list.clearSelection()
            self.app._roi_list.blockSignals(False)

        if self.app._preview_panel and roi_id:
            self.app._preview_panel.on_roi_selected(roi_id)

    def on_roi_list_selected(self, row: int) -> None:
        if row < 0:
            return
        item = self.app._roi_list.item(row)
        if item is None:
            return
        roi_id = item.data(Qt.ItemDataRole.UserRole)
        if roi_id:
            self.canvas.select_roi(roi_id)

    def on_roi_added(self, roi: ROIModel) -> None:
        self.app._refresh_roi_list()
        self.app._notify_preview_rois_changed()

    def on_roi_removed(self, roi_id: str) -> None:
        self.canvas.remove_roi_rect(roi_id)
        self.app._refresh_roi_list()
        self.app._notify_preview_rois_changed()

    def restore_roi_on_canvas(self) -> None:
        if not self.current_slide:
            return
        for roi in self.roi_manager.get_slide_rois(self.current_slide):
            self.canvas.add_roi_rect(roi.id, QRectF(roi.x, roi.y, roi.w, roi.h),
                                     angle=roi.angle)

    def refresh_roi_list(self) -> None:
        self.app._roi_list.clear()
        if self.current_slide is None:
            return
        rois = self.roi_manager.get_slide_rois(self.current_slide)
        for roi in rois:
            item = QListWidgetItem(
                f"ROI ({roi.x}, {roi.y}) "
                f"{roi.w}×{roi.h}"
            )
            item.setData(Qt.ItemDataRole.UserRole, roi.id)
            self.app._roi_list.addItem(item)

    def delete_selected_roi(self) -> None:
        item = self.app._roi_list.currentItem()
        if item is None:
            return
        roi_id = item.data(Qt.ItemDataRole.UserRole)
        self.roi_manager.remove_roi(roi_id)

    def clear_current_roi(self) -> None:
        if self.current_slide is None:
            return
        self.roi_manager.clear_slide_rois(self.current_slide)
        self.canvas.clear_roi_rects()
        self.app._refresh_roi_list()

    def clear_all_rois(self) -> None:
        reply = QMessageBox.question(
            self.app, "确认",
            f"将删除全部 {len(self.roi_manager.all_rois())} 个 ROI，确定？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        for roi in list(self.roi_manager.all_rois()):
            self.roi_manager.remove_roi(roi.id)
        self.canvas.clear_roi_rects()
        self.app._refresh_roi_list()
