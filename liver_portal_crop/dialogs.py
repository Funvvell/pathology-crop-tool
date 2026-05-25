"""设置对话框 — 仅设置输出目录。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QDialog, QFileDialog, QFormLayout, QHBoxLayout,
    QLineEdit, QPushButton, QVBoxLayout,
    QDialogButtonBox,
)

from liver_portal_crop.exporter import CropConfig


class SettingsDialog(QDialog):
    """导出设置对话框（仅输出目录，尺寸由底栏浮动框决定）。"""

    def __init__(self, config: CropConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("导出设置")
        self.resize(400, 120)
        self._config = config
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        form = QFormLayout()

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
        )
