"""控制器基类。"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from liver_portal_crop.app import MainWindow


class BaseController:
    """所有控制器的基类。"""

    def __init__(self, app: MainWindow):
        self._app = app

    @property
    def app(self) -> MainWindow:
        return self._app

    @property
    def canvas(self):
        return self._app._canvas

    @property
    def roi_manager(self):
        return self._app._roi_manager

    @property
    def readers(self) -> dict[Path, "SDPCReader"]:
        return self._app._readers

    @property
    def current_slide(self) -> Path | None:
        return self._app._current_slide
