"""设置对话框。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QDialog, QFileDialog, QFormLayout, QHBoxLayout,
    QLineEdit, QPushButton, QSpinBox, QVBoxLayout,
    QDialogButtonBox,
)

from liver_portal_crop.exporter import CropConfig


class SettingsDialog(QDialog):
    """裁剪设置对话框。"""

    def __init__(self, config: CropConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("导出设置")
        self.resize(400, 200)
        self._config = config
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        form = QFormLayout()

        # 裁剪宽度
        self._width_spin = QSpinBox()
        self._width_spin.setRange(64, 99999)
        self._width_spin.setSingleStep(64)
        self._width_spin.setValue(self._config.crop_width)
        form.addRow("裁剪宽度 (px):", self._width_spin)

        # 裁剪高度
        self._height_spin = QSpinBox()
        self._height_spin.setRange(64, 99999)
        self._height_spin.setSingleStep(64)
        self._height_spin.setValue(self._config.crop_height)
        form.addRow("裁剪高度 (px):", self._height_spin)

        # 输出目录
        dir_layout = QHBoxLayout()
        self._dir_edit = QLineEdit(str(self._config.output_dir))
        self._dir_edit.setReadOnly(True)
        browse_btn = QPushButton("浏览...")
        browse_btn.clicked.connect(self._browse_dir)
        dir_layout.addWidget(self._dir_edit)
        dir_layout.addWidget(browse_btn)
        form.addRow("输出目录:", dir_layout)

        layout.addLayout(form)

        # 按钮
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse_dir(self) -> None:
        dir_path = QFileDialog.getExistingDirectory(
            self, "选择输出目录", self._dir_edit.text(),
        )
        if dir_path:
            self._dir_edit.setText(dir_path)

    def get_config(self) -> CropConfig:
        return CropConfig(
            output_dir=Path(self._dir_edit.text()),
            crop_width=self._width_spin.value(),
            crop_height=self._height_spin.value(),
        )
