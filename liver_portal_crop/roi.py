"""ROI 数据模型和管理器。"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal


@dataclass
class ROIModel:
    """一个矩形标注区域（level 0 全分辨率坐标系）。"""

    slide_path: Path
    x: int
    y: int
    w: int
    h: int
    angle: float = 0.0
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: datetime = field(default_factory=datetime.now)


class ROIManager(QObject):
    """管理所有文件的 ROI 标注。"""

    roi_added = Signal(ROIModel)
    roi_removed = Signal(str)
    roi_cleared = Signal(Path)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rois: dict[str, ROIModel] = {}

    def add_roi(self, roi: ROIModel) -> None:
        self._rois[roi.id] = roi
        self.roi_added.emit(roi)

    def remove_roi(self, roi_id: str) -> None:
        if roi_id in self._rois:
            del self._rois[roi_id]
            self.roi_removed.emit(roi_id)

    def clear_slide_rois(self, slide_path: Path) -> None:
        to_remove = [
            rid for rid, r in self._rois.items()
            if r.slide_path == slide_path
        ]
        for rid in to_remove:
            del self._rois[rid]
        if to_remove:
            self.roi_cleared.emit(slide_path)

    def get_slide_rois(self, slide_path: Path) -> list[ROIModel]:
        return [r for r in self._rois.values() if r.slide_path == slide_path]

    def all_rois(self) -> list[ROIModel]:
        return list(self._rois.values())

    def to_json(self) -> dict[str, Any]:
        """序列化所有 ROI 为 JSON 兼容 dict。"""
        def _serialize(roi: ROIModel) -> dict:
            d = asdict(roi)
            d["x"] = roi.x
            d["y"] = roi.y
            d["w"] = roi.w
            d["h"] = roi.h
            d["slide_path"] = str(roi.slide_path)
            d["created_at"] = roi.created_at.isoformat()
            return d

        return {"rois": [_serialize(r) for r in self._rois.values()]}

    def from_json(self, data: dict[str, Any]) -> None:
        """从 JSON 数据恢复 ROI。兼容旧字段名 thumb_x/y/w/h。"""
        self._rois.clear()
        for item in data.get("rois", []):
            roi = ROIModel(
                id=item["id"],
                slide_path=Path(item["slide_path"]),
                x=item.get("x", item.get("thumb_x", 0)),
                y=item.get("y", item.get("thumb_y", 0)),
                w=item.get("w", item.get("thumb_w", 0)),
                h=item.get("h", item.get("thumb_h", 0)),
                angle=item.get("angle", 0.0),
                created_at=datetime.fromisoformat(item["created_at"]),
            )
            self._rois[roi.id] = roi
