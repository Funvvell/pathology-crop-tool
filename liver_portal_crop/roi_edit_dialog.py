"""ROI 精确坐标编辑对话框。"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
)

from liver_portal_crop.constants import MIN_ROI_SIZE
from liver_portal_crop.roi import ROIModel


class ROIEditDialog(QDialog):
    """编辑单个 ROI 的 x/y/w/h/angle。"""

    def __init__(self, roi: ROIModel, parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑 ROI")
        self._roi = roi
        self._result: ROIModel | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"ID: {roi.id[:8]}  文件: {roi.slide_path.name}"))
        form = QFormLayout()

        self._x = QSpinBox()
        self._x.setRange(0, 10_000_000)
        self._x.setValue(roi.x)
        form.addRow("X", self._x)

        self._y = QSpinBox()
        self._y.setRange(0, 10_000_000)
        self._y.setValue(roi.y)
        form.addRow("Y", self._y)

        self._w = QSpinBox()
        self._w.setRange(MIN_ROI_SIZE, 10_000_000)
        self._w.setValue(roi.w)
        form.addRow("宽 W", self._w)

        self._h = QSpinBox()
        self._h.setRange(MIN_ROI_SIZE, 10_000_000)
        self._h.setValue(roi.h)
        form.addRow("高 H", self._h)

        self._angle = QDoubleSpinBox()
        self._angle.setRange(0.0, 359.9)
        self._angle.setDecimals(1)
        self._angle.setValue(roi.angle)
        form.addRow("角度°", self._angle)

        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self) -> None:
        w = self._w.value()
        h = self._h.value()
        if w < MIN_ROI_SIZE or h < MIN_ROI_SIZE:
            return
        new = self._roi.snapshot()
        new.x = self._x.value()
        new.y = self._y.value()
        new.w = w
        new.h = h
        new.angle = float(self._angle.value())
        self._result = new
        self.accept()

    def get_roi(self) -> ROIModel | None:
        return self._result
