"""NavigationWidget — 导航缩略图，显示当前视口在全图中的位置。"""

from __future__ import annotations

from PySide6.QtCore import Qt, QRectF, Signal
from PySide6.QtGui import (
    QBrush, QColor, QImage, QPainter, QPen, QPixmap,
)
from PySide6.QtWidgets import QWidget


class NavigationWidget(QWidget):
    """导航缩略图控件。

    显示整张切片的缩略图，并用红色矩形标出当前视口位置。
    """

    navigated = Signal(float, float)  # 点击位置在 level-0 坐标中的 (x, y) 中心点

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thumb_pixmap: QPixmap | None = None
        self._full_size: tuple[int, int] = (1, 1)
        self._viewport_rect: QRectF = QRectF()
        self.setMinimumHeight(120)
        self.setMaximumHeight(200)

    def set_thumbnail(self, pixmap: QPixmap, full_w: int, full_h: int) -> None:
        """设置缩略图和对应的全图尺寸。"""
        self._thumb_pixmap = pixmap
        self._full_size = (full_w, full_h)
        self.update()

    def update_viewport(self, scene_rect: QRectF) -> None:
        """更新视口矩形位置。"""
        self._viewport_rect = scene_rect
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 背景
        painter.fillRect(self.rect(), QColor(40, 40, 40))

        if self._thumb_pixmap is None or self._thumb_pixmap.isNull():
            painter.setPen(QColor(160, 160, 160))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "导航图")
            painter.end()
            return

        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        # 缩略图按比例缩放到控件宽度
        pw = self.width() - 4
        scaled = self._thumb_pixmap.scaledToWidth(
            pw, Qt.TransformationMode.SmoothTransformation
        )
        # 居中绘制
        ox = max(0, (self.width() - scaled.width()) / 2)
        oy = max(0, (self.height() - scaled.height()) / 2)
        painter.drawPixmap(int(ox), int(oy), scaled)

        # 绘制视口矩形
        if not self._viewport_rect.isNull() and self._full_size[0] > 0:
            sx = scaled.width() / self._full_size[0]
            sy = scaled.height() / self._full_size[1]

            vr = QRectF(
                self._viewport_rect.x() * sx + ox,
                self._viewport_rect.y() * sy + oy,
                max(4, self._viewport_rect.width() * sx),
                max(4, self._viewport_rect.height() * sy),
            )
            # 限制在缩略图范围内
            vr = vr.intersected(QRectF(ox, oy, scaled.width(), scaled.height()))

            if vr.width() > 2 and vr.height() > 2:
                painter.setPen(QPen(QColor(255, 50, 50), 2))
                painter.setBrush(QBrush(QColor(255, 50, 50, 40)))
                painter.drawRect(vr)

        painter.end()

    def mousePressEvent(self, event) -> None:
        """点击缩略图跳转到对应位置。"""
        if self._thumb_pixmap is None or self._full_size[0] <= 0:
            return
        pw = self.width() - 4
        scaled = self._thumb_pixmap.scaledToWidth(
            pw, Qt.TransformationMode.SmoothTransformation
        )
        ox = (self.width() - scaled.width()) / 2
        oy = (self.height() - scaled.height()) / 2

        mx = event.position().x() - ox
        my = event.position().y() - oy
        if 0 <= mx < scaled.width() and 0 <= my < scaled.height():
            sx = self._full_size[0] / scaled.width()
            sy = self._full_size[1] / scaled.height()
            scene_x = mx * sx
            scene_y = my * sy
            self.navigated.emit(scene_x, scene_y)
