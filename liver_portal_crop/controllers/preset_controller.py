"""预设控制器。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QInputDialog

from liver_portal_crop.controllers.base import BaseController
from liver_portal_crop.constants import SESSION_DIR_NAME

if TYPE_CHECKING:
    from liver_portal_crop.app import MainWindow

PRESETS_FILE = Path.home() / SESSION_DIR_NAME / "presets.json"


class PresetController(BaseController):
    """预设管理：加载、保存、应用。"""

    def load_presets(self) -> None:
        self.app._presets: dict[str, dict] = {}
        if PRESETS_FILE.exists():
            try:
                self.app._presets = json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
            except Exception:
                self.app._presets = {}
        if "默认" not in self.app._presets:
            self.app._presets["默认"] = {"mag": "20x", "ratio": "16:9", "w": 512, "h": 512, "angle": 0}
        self.app._preset_cb.blockSignals(True)
        self.app._preset_cb.clear()
        self.app._preset_cb.addItems(list(self.app._presets.keys()))
        self.app._preset_cb.setCurrentText("默认")
        self.app._preset_cb.blockSignals(False)
        self._apply_preset("默认")

    def save_preset(self) -> None:
        name, ok = QInputDialog.getText(self.app, "保存预设", "预设名称：")
        if not ok or not name.strip():
            return
        name = name.strip()
        self.app._presets[name] = {
            "mag": self.app._mag_cb.currentText(),
            "ratio": self.app._ratio_cb.currentText(),
            "w": self.app._frame_w_spin.value(),
            "h": self.app._frame_h_spin.value(),
            "angle": self.app._frame_angle_slider.value(),
        }
        try:
            PRESETS_FILE.parent.mkdir(parents=True, exist_ok=True)
            PRESETS_FILE.write_text(json.dumps(self.app._presets, indent=2), encoding="utf-8")
        except Exception:
            pass
        self.app._preset_cb.blockSignals(True)
        self.app._preset_cb.clear()
        self.app._preset_cb.addItems(list(self.app._presets.keys()))
        self.app._preset_cb.setCurrentText(name)
        self.app._preset_cb.blockSignals(False)

    def _apply_preset(self, name: str) -> None:
        preset = self.app._presets.get(name)
        if not preset:
            return
        self.app._mag_cb.blockSignals(True)
        self.app._ratio_cb.blockSignals(True)
        self.app._frame_w_spin.blockSignals(True)
        self.app._frame_h_spin.blockSignals(True)
        self.app._frame_angle_slider.blockSignals(True)
        self.app._mag_cb.setCurrentText(preset.get("mag", "20x"))
        self.app._ratio_cb.setCurrentText(preset.get("ratio", "16:9"))
        self.app._frame_w_spin.setValue(preset.get("w", 512))
        self.app._frame_h_spin.setValue(preset.get("h", 512))
        self.app._frame_angle_slider.setValue(preset.get("angle", 0))
        self.app._frame_angle_label.setText(f"{self.app._frame_angle_slider.value()}°")
        self.app._mag_cb.blockSignals(False)
        self.app._ratio_cb.blockSignals(False)
        self.app._frame_w_spin.blockSignals(False)
        self.app._frame_h_spin.blockSignals(False)
        self.app._frame_angle_slider.blockSignals(False)
        self.canvas.set_frame_size(preset.get("w", 512), preset.get("h", 512))
        self.canvas.set_frame_angle(float(preset.get("angle", 0)))
