"""ROI 管理控制器。"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QRectF
from PySide6.QtWidgets import QListWidgetItem, QMessageBox

from liver_portal_crop.controllers.base import BaseController
from liver_portal_crop.constants import FIELD_NUMBER_MM
from liver_portal_crop.roi import ROIModel
from liver_portal_crop.roi_edit_dialog import ROIEditDialog
from liver_portal_crop.roi_undo import (
    AddRoiCommand,
    BatchAddCommand,
    BatchRemoveCommand,
    ClearSlideCommand,
    RemoveRoiCommand,
    RoiUndoStack,
    UpdateRoiCommand,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from liver_portal_crop.app import MainWindow


class ROIController(BaseController):
    """ROI 管理：创建、选中、编辑、删除、列表刷新、撤销与导航。"""

    def __init__(self, app: MainWindow):
        super().__init__(app)
        self.undo_stack = RoiUndoStack(self.roi_manager)
        self._syncing_selection = False
        self._selected_ids: list[str] = []

    # ── 框参数 ────────────────────────────────────────

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

    # ── 创建 / 变更 ───────────────────────────────────

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
        self.undo_stack.push(AddRoiCommand(roi.snapshot()))
        self._update_roi_info_panel()

    def add_rois_bulk(self, rois: list[ROIModel]) -> None:
        """批量写入 ROI（组织检测 / IHC 等），并作为一条 undo 记录。"""
        if not rois:
            return
        snaps = tuple(r.snapshot() for r in rois)
        self.undo_stack.push(BatchAddCommand(snaps))
        for roi in rois:
            self.roi_manager.add_roi(roi)
        self._update_roi_info_panel()

    def on_canvas_roi_selected(self, roi_id: str) -> None:
        if roi_id == "__toggle_roi__":
            new_state = not self.app._roi_mode_btn.isChecked()
            self.app._roi_mode_btn.setChecked(new_state)
            self.toggle_roi_mode(new_state)
            return
        # 兼容单删除信号路径
        self.delete_rois([roi_id])

    def on_canvas_roi_batch_delete(self, ids: list) -> None:
        self.delete_rois([str(i) for i in ids])

    def on_roi_rect_changed(self, roi_id: str, new_rect: QRectF, angle: float = 0.0) -> None:
        roi = self.roi_manager.get_roi(roi_id)
        if roi is None:
            return
        self.roi_manager.update_roi_geometry(
            roi_id,
            int(new_rect.x()),
            int(new_rect.y()),
            int(new_rect.width()),
            int(new_rect.height()),
            round(angle, 2),
        )
        self._update_roi_list_item_text(roi_id)
        self.app._notify_preview_rois_changed()
        self._update_roi_info_panel()

    def on_roi_edit_finished(self, roi_id: str, old_rect: QRectF, old_angle: float) -> None:
        """画布手势结束后，用旧几何压入一条 undo。"""
        roi = self.roi_manager.get_roi(roi_id)
        if roi is None:
            return
        old = roi.snapshot()
        old.x = int(old_rect.x())
        old.y = int(old_rect.y())
        old.w = int(old_rect.width())
        old.h = int(old_rect.height())
        old.angle = float(old_angle)
        new = roi.snapshot()
        if (old.x, old.y, old.w, old.h, old.angle) == (new.x, new.y, new.w, new.h, new.angle):
            return
        self.undo_stack.push(UpdateRoiCommand(old, new))

    def on_roi_updated(self, roi: ROIModel) -> None:
        self.canvas.update_roi_rect(roi.id, QRectF(roi.x, roi.y, roi.w, roi.h))
        item = self.canvas._roi_items.get(roi.id)
        if item is not None:
            item.setRotation(roi.angle)
        self._update_roi_list_item_text(roi.id)
        self._update_roi_info_panel()

    # ── 选择同步 ──────────────────────────────────────

    @property
    def selected_ids(self) -> list[str]:
        return list(self._selected_ids)

    def on_canvas_selection_multi(self, ids: list) -> None:
        if self._syncing_selection:
            return
        self._selected_ids = [str(i) for i in ids]
        self._syncing_selection = True
        try:
            self._apply_list_selection(self._selected_ids)
            if len(self._selected_ids) == 1:
                self.app._selected_roi_id = self._selected_ids[0]
            else:
                self.app._selected_roi_id = self._selected_ids[0] if self._selected_ids else None
            if self.app._preview_panel and self._selected_ids:
                self.app._preview_panel.on_roi_selected(self._selected_ids[0])
        finally:
            self._syncing_selection = False
        self._update_roi_info_panel()

    def on_roi_selection_changed(self, roi_id: str) -> None:
        # 保留旧接口：单 id 选择
        if roi_id:
            self.on_canvas_selection_multi([roi_id])
        else:
            self.on_canvas_selection_multi([])

    def on_roi_list_selection_changed(self) -> None:
        if self._syncing_selection:
            return
        ids = self._ids_from_list_selection()
        self._selected_ids = ids
        self._syncing_selection = True
        try:
            self.canvas.select_rois(ids)
            self.app._selected_roi_id = ids[0] if ids else None
        finally:
            self._syncing_selection = False
        if self.app._preview_panel and ids:
            self.app._preview_panel.on_roi_selected(ids[0])
        self._update_roi_info_panel()

    def _ids_from_list_selection(self) -> list[str]:
        ids: list[str] = []
        for item in self.app._roi_list.selectedItems():
            rid = item.data(Qt.ItemDataRole.UserRole)
            if rid:
                ids.append(rid)
        return ids

    def _apply_list_selection(self, ids: list[str]) -> None:
        id_set = set(ids)
        self.app._roi_list.blockSignals(True)
        for i in range(self.app._roi_list.count()):
            item = self.app._roi_list.item(i)
            if item is None:
                continue
            rid = item.data(Qt.ItemDataRole.UserRole)
            item.setSelected(rid in id_set)
        if ids:
            for i in range(self.app._roi_list.count()):
                item = self.app._roi_list.item(i)
                if item and item.data(Qt.ItemDataRole.UserRole) == ids[0]:
                    self.app._roi_list.scrollToItem(item)
                    break
        else:
            self.app._roi_list.clearSelection()
        self.app._roi_list.blockSignals(False)

    def on_roi_list_selected(self, row: int) -> None:
        # 兼容 currentRowChanged；多选以 selectionChanged 为准
        if row < 0:
            return
        if self._syncing_selection:
            return
        self.on_roi_list_selection_changed()

    # ── 列表刷新 ──────────────────────────────────────

    def on_roi_added(self, roi: ROIModel) -> None:
        self.app._refresh_roi_list()
        self.app._notify_preview_rois_changed()
        self.canvas.add_roi_rect(roi.id, QRectF(roi.x, roi.y, roi.w, roi.h), angle=roi.angle)

    def on_roi_removed(self, roi_id: str) -> None:
        self.canvas.remove_roi_rect(roi_id)
        self.app._refresh_roi_list()
        self.app._notify_preview_rois_changed()
        self._update_roi_info_panel()

    def on_roi_cleared(self, slide_path) -> None:
        if self.current_slide and slide_path == self.current_slide:
            self.canvas.clear_roi_rects()
        self.app._refresh_roi_list()
        self.app._notify_preview_rois_changed()
        self._update_roi_info_panel()

    def restore_roi_on_canvas(self) -> None:
        if not self.current_slide:
            return
        for roi in self.roi_manager.get_slide_rois(self.current_slide):
            self.canvas.add_roi_rect(roi.id, QRectF(roi.x, roi.y, roi.w, roi.h),
                                     angle=roi.angle)

    def refresh_roi_list(self) -> None:
        selected_ids = set(self._selected_ids) or self._ids_from_list_selection()

        self.app._roi_list.blockSignals(True)
        self.app._roi_list.clear()
        if self.current_slide is None:
            self.app._roi_list.blockSignals(False)
            self._selected_ids = []
            self._update_roi_info_panel()
            return
        rois = self.roi_manager.sorted_slide_rois(self.current_slide)
        for roi in rois:
            item = QListWidgetItem(self._format_roi_item(roi))
            item.setData(Qt.ItemDataRole.UserRole, roi.id)
            self.app._roi_list.addItem(item)
            if roi.id in selected_ids:
                item.setSelected(True)
        self.app._roi_list.blockSignals(False)
        self._selected_ids = self._ids_from_list_selection()
        self._update_roi_info_panel()

    @staticmethod
    def _format_roi_item(roi: ROIModel) -> str:
        text = f"ROI ({roi.x}, {roi.y}) {roi.w}×{roi.h}"
        if roi.angle:
            text += f" {roi.angle:g}°"
        return text

    def _update_roi_list_item_text(self, roi_id: str) -> None:
        roi = self.roi_manager.get_roi(roi_id)
        if roi is None:
            return
        for i in range(self.app._roi_list.count()):
            item = self.app._roi_list.item(i)
            if item and item.data(Qt.ItemDataRole.UserRole) == roi_id:
                item.setText(self._format_roi_item(roi))
                break

    def _update_roi_info_panel(self) -> None:
        label = getattr(self.app, "_roi_info_label", None)
        if label is None:
            return
        ids = self._selected_ids
        if not ids:
            total = len(self.roi_manager.get_slide_rois(self.current_slide)) if self.current_slide else 0
            label.setText(f"未选中 ROI | 当前切片 {total} 个")
            return
        if len(ids) > 1:
            rois = [self.roi_manager.get_roi(i) for i in ids]
            rois = [r for r in rois if r]
            if not rois:
                label.setText("未选中 ROI")
                return
            x0 = min(r.x for r in rois)
            y0 = min(r.y for r in rois)
            x1 = max(r.x + r.w for r in rois)
            y1 = max(r.y + r.h for r in rois)
            label.setText(f"已选 {len(rois)} 个 ROI | 包围盒 ({x0},{y0}) {x1-x0}×{y1-y0}")
            return
        roi = self.roi_manager.get_roi(ids[0])
        if roi is None:
            label.setText("未选中 ROI")
            return
        parts = [
            roi.id[:8],
            f"({roi.x}, {roi.y})",
            f"{roi.w}×{roi.h}",
            f"{roi.angle:g}°",
        ]
        mpp = None
        if self.current_slide and self.current_slide in self.readers:
            mpp = self.readers[self.current_slide].mpp
        if mpp and mpp > 0:
            parts.append(f"{roi.w * mpp:.1f}×{roi.h * mpp:.1f} µm")
        label.setText(" | ".join(parts))

    # ── 删除 / 复制 / 清空 ────────────────────────────

    def delete_selected_roi(self) -> None:
        ids = self._ids_from_list_selection() or list(self._selected_ids)
        if not ids:
            item = self.app._roi_list.currentItem()
            if item:
                rid = item.data(Qt.ItemDataRole.UserRole)
                if rid:
                    ids = [rid]
        self.delete_rois(ids)

    def delete_rois(self, ids: list[str]) -> None:
        ids = [i for i in ids if self.roi_manager.get_roi(i) is not None]
        if not ids:
            return
        snapshots = tuple(self.roi_manager.get_roi(i).snapshot() for i in ids)
        self.undo_stack.push(BatchRemoveCommand(snapshots))
        for rid in ids:
            self.roi_manager.remove_roi(rid)
        if self.app._selected_roi_id in ids:
            self.app._selected_roi_id = None
        self._selected_ids = [i for i in self._selected_ids if i not in ids]
        self._update_roi_info_panel()

    def copy_selected_rois(self) -> None:
        ids = self._ids_from_list_selection() or list(self._selected_ids)
        if not ids:
            ids = self.canvas.selected_roi_ids()
        rois = [self.roi_manager.get_roi(i) for i in ids]
        rois = [r for r in rois if r]
        if not rois:
            return
        offset_x = max(self.app._frame_w_spin.value() // 2, 16)
        offset_y = max(self.app._frame_h_spin.value() // 2, 16)
        copies = []
        for roi in rois:
            new = ROIModel(
                slide_path=roi.slide_path,
                x=roi.x + offset_x,
                y=roi.y + offset_y,
                w=roi.w,
                h=roi.h,
                angle=roi.angle,
            )
            copies.append(new)
        self.undo_stack.push(BatchAddCommand(tuple(c.snapshot() for c in copies)))
        for c in copies:
            self.roi_manager.add_roi(c)
        self._selected_ids = [c.id for c in copies]
        self.canvas.select_rois(self._selected_ids)
        self._apply_list_selection(self._selected_ids)
        self._update_roi_info_panel()

    def select_all_rois(self) -> None:
        if not self.current_slide:
            return
        ids = [r.id for r in self.roi_manager.sorted_slide_rois(self.current_slide)]
        self._selected_ids = ids
        self._syncing_selection = True
        try:
            self._apply_list_selection(ids)
            self.canvas.select_rois(ids)
        finally:
            self._syncing_selection = False
        self._update_roi_info_panel()

    def clear_current_roi(self) -> None:
        if self.current_slide is None:
            return
        rois = self.roi_manager.get_slide_rois(self.current_slide)
        snaps = tuple(r.snapshot() for r in rois)
        self.undo_stack.push(ClearSlideCommand(self.current_slide, snaps))
        self.roi_manager.clear_slide_rois(self.current_slide)
        self.canvas.clear_roi_rects()
        self.app._refresh_roi_list()
        self._update_roi_info_panel()

    def clear_all_rois(self) -> None:
        reply = QMessageBox.question(
            self.app, "确认",
            f"将删除全部 {len(self.roi_manager.all_rois())} 个 ROI，确定？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        rois = self.roi_manager.all_rois()
        snaps = tuple(r.snapshot() for r in rois)
        self.undo_stack.push(BatchRemoveCommand(snaps))
        for r in rois:
            self.roi_manager.remove_roi(r.id)
        self.canvas.clear_roi_rects()
        self.app._refresh_roi_list()
        self._update_roi_info_panel()

    # ── 精确编辑 / 导航 / 撤销 ────────────────────────

    def edit_roi(self, roi_id: str | None = None) -> None:
        if roi_id is None:
            ids = self._ids_from_list_selection() or self._selected_ids
            if not ids:
                ids = self.canvas.selected_roi_ids()
            if not ids:
                return
            roi_id = ids[0]
        roi = self.roi_manager.get_roi(roi_id)
        if roi is None:
            return
        dlg = ROIEditDialog(roi, self.app)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        new = dlg.get_roi()
        if new is None:
            return
        old = roi.snapshot()
        self.undo_stack.push(UpdateRoiCommand(old, new.snapshot()))
        self.roi_manager.replace_roi(new)
        self._update_roi_info_panel()

    def navigate_roi(self, step: int) -> None:
        if not self.current_slide:
            return
        rois = self.roi_manager.sorted_slide_rois(self.current_slide)
        if not rois:
            return
        ids = [r.id for r in rois]
        current = self._selected_ids[0] if self._selected_ids else None
        if current not in ids:
            idx = 0 if step > 0 else len(ids) - 1
        else:
            idx = (ids.index(current) + step) % len(ids)
        target = ids[idx]
        self._selected_ids = [target]
        self._syncing_selection = True
        try:
            self._apply_list_selection([target])
            self.canvas.select_rois([target])
        finally:
            self._syncing_selection = False
        self.canvas.center_on_roi(target)
        self.app._selected_roi_id = target
        self._update_roi_info_panel()

    def navigate_file(self, step: int) -> None:
        count = self.app._file_list.count()
        if count <= 0:
            return
        row = self.app._file_list.currentRow()
        if row < 0:
            row = 0 if step > 0 else count - 1
        else:
            row = (row + step) % count
        self.app._file_list.setCurrentRow(row)

    def fit_view(self) -> None:
        ids = self._selected_ids or self.canvas.selected_roi_ids()
        self.canvas.fit_to_rois(ids if ids else None)

    def fit_slide(self) -> None:
        self.canvas.fit_to_slide()

    def undo(self) -> None:
        if self.undo_stack.undo():
            self.app._status_label.setText("已撤销")
        else:
            self.app._status_label.setText("没有可撤销的操作")

    def redo(self) -> None:
        if self.undo_stack.redo():
            self.app._status_label.setText("已重做")
        else:
            self.app._status_label.setText("没有可重做的操作")
