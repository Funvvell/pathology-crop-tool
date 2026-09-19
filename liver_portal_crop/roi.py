"""ROI 数据模型和管理器。"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from PySide6.QtCore import QObject, Signal

from liver_portal_crop.constants import ROI_ID_LENGTH


@dataclass
class ROIModel:
    """一个矩形标注区域（level 0 全分辨率坐标系）。"""

    slide_path: Path
    x: int
    y: int
    w: int
    h: int
    angle: float = 0.0
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:ROI_ID_LENGTH])
    created_at: datetime = field(default_factory=datetime.now)

    def snapshot(self) -> ROIModel:
        """返回独立副本，避免 undo 栈持有同一可变对象。"""
        return ROIModel(
            slide_path=self.slide_path,
            x=self.x,
            y=self.y,
            w=self.w,
            h=self.h,
            angle=self.angle,
            id=self.id,
            created_at=self.created_at,
        )


class ROIManager(QObject):
    """管理所有文件的 ROI 标注。"""

    roi_added = Signal(ROIModel)
    roi_removed = Signal(str)
    roi_updated = Signal(ROIModel)
    roi_cleared = Signal(Path)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rois: dict[str, ROIModel] = {}

    def get_roi(self, roi_id: str) -> ROIModel | None:
        return self._rois.get(roi_id)

    def add_roi(self, roi: ROIModel) -> None:
        self._rois[roi.id] = roi
        self.roi_added.emit(roi)

    def remove_roi(self, roi_id: str) -> None:
        if roi_id in self._rois:
            del self._rois[roi_id]
            self.roi_removed.emit(roi_id)

    def replace_roi(self, roi: ROIModel) -> None:
        """以给定 ROI 覆盖同 id 条目（精确编辑 / undo 回写）。"""
        self._rois[roi.id] = roi
        self.roi_updated.emit(roi)

    def update_roi_geometry(
        self,
        roi_id: str,
        x: int,
        y: int,
        w: int,
        h: int,
        angle: float = 0.0,
    ) -> ROIModel | None:
        roi = self._rois.get(roi_id)
        if roi is None:
            return None
        roi.x = int(x)
        roi.y = int(y)
        roi.w = int(w)
        roi.h = int(h)
        roi.angle = float(angle)
        self.roi_updated.emit(roi)
        return roi

    def clear_slide_rois(self, slide_path: Path) -> None:
        norm = slide_path.resolve()
        to_remove = [
            rid for rid, r in self._rois.items()
            if r.slide_path.resolve() == norm
        ]
        for rid in to_remove:
            del self._rois[rid]
        if to_remove:
            self.roi_cleared.emit(slide_path)

    def get_slide_rois(self, slide_path: Path) -> list[ROIModel]:
        return [r for r in self._rois.values() if r.slide_path == slide_path]

    def sorted_slide_rois(self, slide_path: Path) -> list[ROIModel]:
        """当前切片 ROI，按 (x, y) 稳定排序，供列表与 N/P 导航共用。"""
        return sorted(
            self.get_slide_rois(slide_path),
            key=lambda r: (r.x, r.y, r.id),
        )

    def all_rois(self) -> list[ROIModel]:
        return list(self._rois.values())

    def to_json(self) -> dict[str, Any]:
        """序列化所有 ROI 为 JSON 兼容 dict。"""
        def _serialize(roi: ROIModel) -> dict:
            d = asdict(roi)
            d["slide_path"] = str(roi.slide_path)
            d["created_at"] = roi.created_at.isoformat()
            return d

        return {"rois": [_serialize(r) for r in self._rois.values()]}

    def from_json(self, data: dict[str, Any]) -> None:
        """从 JSON 数据恢复 ROI。兼容旧字段名 thumb_x/y/w/h。"""
        self._rois.clear()
        for item in data.get("rois", []):
            try:
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
            except (KeyError, ValueError, TypeError) as e:
                logger.warning("跳过无效 ROI 条目 (%s): %s", e, item.get("id", "?"))
