"""ROI 撤销/重做栈。"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from liver_portal_crop.roi import ROIModel

logger = logging.getLogger(__name__)

DEFAULT_MAX_DEPTH = 50


class RoiManagerLike(Protocol):
    def add_roi(self, roi: ROIModel) -> None: ...
    def remove_roi(self, roi_id: str) -> None: ...
    def replace_roi(self, roi: ROIModel) -> None: ...
    def get_roi(self, roi_id: str) -> ROIModel | None: ...
    def clear_slide_rois(self, slide_path: Path) -> None: ...


def _snap(roi: ROIModel) -> ROIModel:
    return copy.deepcopy(roi)


@dataclass(frozen=True)
class AddRoiCommand:
    roi: ROIModel

    def apply(self, manager: RoiManagerLike) -> None:
        manager.add_roi(_snap(self.roi))

    def revert(self, manager: RoiManagerLike) -> None:
        manager.remove_roi(self.roi.id)


@dataclass(frozen=True)
class RemoveRoiCommand:
    roi: ROIModel

    def apply(self, manager: RoiManagerLike) -> None:
        manager.remove_roi(self.roi.id)

    def revert(self, manager: RoiManagerLike) -> None:
        manager.add_roi(_snap(self.roi))


@dataclass(frozen=True)
class BatchRemoveCommand:
    rois: tuple[ROIModel, ...]

    def apply(self, manager: RoiManagerLike) -> None:
        for roi in self.rois:
            manager.remove_roi(roi.id)

    def revert(self, manager: RoiManagerLike) -> None:
        for roi in self.rois:
            manager.add_roi(_snap(roi))


@dataclass(frozen=True)
class BatchAddCommand:
    rois: tuple[ROIModel, ...]

    def apply(self, manager: RoiManagerLike) -> None:
        for roi in self.rois:
            manager.add_roi(_snap(roi))

    def revert(self, manager: RoiManagerLike) -> None:
        for roi in self.rois:
            manager.remove_roi(roi.id)


@dataclass(frozen=True)
class UpdateRoiCommand:
    old: ROIModel
    new: ROIModel

    def apply(self, manager: RoiManagerLike) -> None:
        manager.replace_roi(_snap(self.new))

    def revert(self, manager: RoiManagerLike) -> None:
        manager.replace_roi(_snap(self.old))


@dataclass(frozen=True)
class ClearSlideCommand:
    slide_path: Path
    rois: tuple[ROIModel, ...]

    def apply(self, manager: RoiManagerLike) -> None:
        manager.clear_slide_rois(self.slide_path)

    def revert(self, manager: RoiManagerLike) -> None:
        for roi in self.rois:
            manager.add_roi(_snap(roi))


RoiCommand = (
    AddRoiCommand
    | RemoveRoiCommand
    | BatchRemoveCommand
    | BatchAddCommand
    | UpdateRoiCommand
    | ClearSlideCommand
)


class RoiUndoStack:
    """有限深度的 undo/redo 栈；批量操作作为单条命令。"""

    def __init__(self, manager: RoiManagerLike, max_depth: int = DEFAULT_MAX_DEPTH):
        self._manager = manager
        self._max_depth = max(1, max_depth)
        self._undo: list[RoiCommand] = []
        self._redo: list[RoiCommand] = []
        self._suspend = False

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def undo_depth(self) -> int:
        return len(self._undo)

    @property
    def redo_depth(self) -> int:
        return len(self._redo)

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()

    def push(self, command: RoiCommand) -> None:
        """记录一条已执行（或待执行后由调用方保证一致）的命令。

        约定：控制器先对 manager 执行变更，再 push 对应命令；
        undo 时调用 revert，redo 时调用 apply。
        """
        if self._suspend:
            return
        self._undo.append(command)
        if len(self._undo) > self._max_depth:
            self._undo.pop(0)
        self._redo.clear()

    def push_and_apply(self, command: RoiCommand) -> None:
        """先应用命令再入栈（用于尚未写入 manager 的操作）。"""
        self._suspend = True
        try:
            command.apply(self._manager)
        finally:
            self._suspend = False
        self.push(command)

    def undo(self) -> bool:
        if not self._undo:
            return False
        command = self._undo.pop()
        self._suspend = True
        try:
            command.revert(self._manager)
        except Exception:
            logger.exception("undo 失败: %s", command)
            self._suspend = False
            # 失败命令放回 undo 栈顶，避免静默丢失
            self._undo.append(command)
            return False
        finally:
            self._suspend = False
        self._redo.append(command)
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        command = self._redo.pop()
        self._suspend = True
        try:
            command.apply(self._manager)
        except Exception:
            logger.exception("redo 失败: %s", command)
            self._suspend = False
            self._redo.append(command)
            return False
        finally:
            self._suspend = False
        self._undo.append(command)
        return True
