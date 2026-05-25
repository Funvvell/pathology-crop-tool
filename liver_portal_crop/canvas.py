"""WSICanvas — 基于 QGraphicsView 的金字塔 WSI 查看器 + ROI 标注工具。

直接在原图坐标系（level 0）上操作，根据缩放级别自动选择金字塔层级
并加载可见区域的 tiles，ROI 坐标即为 level 0 坐标，导出时无需转换。
"""

from __future__ import annotations

import math
import uuid
from collections import OrderedDict

import numpy as np

from PySide6.QtCore import Qt, QRectF, QTimer, Signal
from PySide6.QtGui import (
    QBrush, QColor, QImage, QPainter, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QGraphicsItem, QGraphicsPixmapItem, QGraphicsRectItem,
    QGraphicsScene, QGraphicsView,
)

from liver_portal_crop.reader import SDPCReader


class ROIRectItem(QGraphicsRectItem):
    """已创建的 ROI 矩形项。"""

    def __init__(self, roi_id: str, rect: QRectF, *args, **kwargs):
        super().__init__(rect, *args, **kwargs)
        self._roi_id = roi_id
        self.setAcceptHoverEvents(True)
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemIsMovable
        )
        self.setPen(QPen(QColor(0, 120, 215), 2))
        self.setBrush(QBrush(QColor(0, 120, 215, 30)))
        self._hover_pen = QPen(QColor(255, 0, 0), 3)

    @property
    def roi_id(self) -> str:
        return self._roi_id

    def hoverEnterEvent(self, event):
        self.setPen(self._hover_pen)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.setPen(QPen(QColor(0, 120, 215), 2))
        super().hoverLeaveEvent(event)


class WSICanvas(QGraphicsView):
    """金字塔 WSI 查看器 + ROI 标注。

    场景坐标 = level 0 全分辨率坐标。
    根据缩放级别自动选择金字塔层级，按需加载可见区域。
    """

    roi_created = Signal(str, QRectF)   # roi_id, rect in level-0 coords
    roi_selected = Signal(str)
    viewport_changed = Signal(QRectF)    # visible rect in level-0 coords

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)

        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse
        )
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setViewportUpdateMode(
            QGraphicsView.ViewportUpdateMode.FullViewportUpdate
        )
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMouseTracking(True)

        # 状态
        self._reader: SDPCReader | None = None
        self._tile_cache: OrderedDict[tuple, QPixmap] = OrderedDict()
        self._tile_items: dict[tuple, QGraphicsPixmapItem] = {}
        self._max_cache = 512

        # ROI
        self._roi_items: dict[str, ROIRectItem] = {}
        self._roi_mode = False

        # 浮动框
        self._frame_w: int = 1024
        self._frame_h: int = 1024
        self._frame_item: QGraphicsRectItem | None = None
        self._frame_visible: bool = False

        # 渲染防抖
        self._render_timer = QTimer()
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._render_visible_tiles)

    # ── 公共接口 ──────────────────────────────────────

    def load_slide(self, reader: SDPCReader) -> None:
        """加载 WSI，场景设为全分辨率坐标系。"""
        self._reader = reader
        self._scene.clear()
        self._roi_items.clear()
        self._frame_item = None
        self._tile_cache.clear()
        self._tile_items.clear()

        w, h = reader.full_width, reader.full_height
        self._scene.setSceneRect(0, 0, w, h)

        # 加载缩略图作为底层背景
        self._load_background_thumb()
        self.fitInView(self._scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

        # 通知导航缩略图
        self.viewport_changed.emit(QRectF(0, 0, w, h))
        # 延迟触发 tile 加载
        self._render_timer.start(200)

    def _load_background_thumb(self) -> None:
        """加载缩略图作为底层背景（无缩放的 QGraphicsPixmapItem）。"""
        if self._reader is None:
            return
        r = self._reader
        thumb_level = r.level_count - 1
        tw, th = r.thumbnail_size
        region = r._read_level_region(thumb_level, 0, 0, tw, th)
        img_bytes = region.tobytes()
        img = QImage(img_bytes, tw, th, tw * 3, QImage.Format.Format_RGB888)
        pix = QPixmap.fromImage(img)

        scale = r.levels[thumb_level].downsample
        item = QGraphicsPixmapItem(pix)
        item.setPos(0, 0)
        item.setScale(scale)  # 缩放到 level-0 全尺寸
        item.setZValue(-10000)  # 最底层
        self._scene.addItem(item)
        self._tile_items[('thumb',)] = item

    def set_frame_size(self, w: int, h: int) -> None:
        self._frame_w = w
        self._frame_h = h
        if self._frame_item:
            self._frame_item.setRect(0, 0, w, h)

    def set_roi_mode(self, active: bool) -> None:
        self._roi_mode = active
        if active:
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self.viewport().setCursor(Qt.CursorShape.CrossCursor)
            self._show_frame()
        else:
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self.viewport().setCursor(Qt.CursorShape.ArrowCursor)
            self._hide_frame()

    def add_roi_rect(self, roi_id: str, rect: QRectF) -> None:
        item = ROIRectItem(roi_id, rect)
        self._scene.addItem(item)
        self._roi_items[roi_id] = item

    def remove_roi_rect(self, roi_id: str) -> None:
        item = self._roi_items.pop(roi_id, None)
        if item:
            self._scene.removeItem(item)

    def clear_roi_rects(self) -> None:
        for item in self._roi_items.values():
            self._scene.removeItem(item)
        self._roi_items.clear()

    def current_reader(self) -> SDPCReader | None:
        return self._reader

    # ── 浮动框 ────────────────────────────────────────

    def _show_frame(self) -> None:
        if self._frame_item is None:
            pen = QPen(QColor(0, 255, 0), 3)
            pen.setStyle(Qt.PenStyle.DashLine)
            self._frame_item = QGraphicsRectItem(0, 0, self._frame_w, self._frame_h)
            self._frame_item.setPen(pen)
            self._frame_item.setBrush(QBrush(QColor(0, 255, 0, 12)))
            self._frame_item.setZValue(1000)
            self._scene.addItem(self._frame_item)
        # 初始位置：鼠标当前位置
        cursor_pos = self.mapFromGlobal(self.cursor().pos())
        scene_pos = self.mapToScene(cursor_pos)
        self._frame_item.setPos(
            scene_pos.x() - self._frame_w / 2,
            scene_pos.y() - self._frame_h / 2,
        )
        self._frame_item.setVisible(True)
        self._frame_visible = True

    def _hide_frame(self) -> None:
        if self._frame_item:
            self._frame_item.setVisible(False)
        self._frame_visible = False

    def _update_frame_pos(self, scene_pos) -> None:
        if self._frame_item and self._frame_visible:
            self._frame_item.setPos(
                scene_pos.x() - self._frame_w / 2,
                scene_pos.y() - self._frame_h / 2,
            )

    def _place_roi_at_frame(self) -> None:
        if not self._frame_item or not self._frame_visible or self._reader is None:
            return
        pos = self._frame_item.pos()
        rect = QRectF(pos.x(), pos.y(), self._frame_w, self._frame_h)
        if not self._scene.sceneRect().intersects(rect):
            return
        roi_id = uuid.uuid4().hex[:12]
        self.add_roi_rect(roi_id, rect)
        self.roi_created.emit(roi_id, rect)

    # ── 金字塔 Tile 渲染 ─────────────────────────────

    def _get_best_level(self) -> int:
        """根据当前缩放比选择最佳金字塔层级。"""
        if self._reader is None:
            return 0
        zoom = abs(self.transform().m11())
        # 目标：1 屏幕像素 ≈ 1 图像像素
        # zoom = screen_pixels / scene_pixels (level 0)
        # 需要的下采样比 = 1/zoom
        target_downsample = 1.0 / zoom if zoom > 0 else 1.0
        levels = self._reader.levels
        best = 0
        for lv in levels:
            if lv.downsample <= target_downsample:
                best = lv.level
            else:
                break
        return best

    def _render_visible_tiles(self) -> None:
        """按需加载可见区域的 tiles。"""
        if self._reader is None:
            return

        r = self._reader
        level = self._get_best_level()
        thumb_level = r.level_count - 1

        # 如果当前层级就是缩略图层级，不需要额外 tile
        if level >= thumb_level:
            self._cleanup_non_thumb_tiles()
            return

        scale = r.levels[level].downsample

        # 可见区域（level 0 坐标），向外扩展 30% 做预加载 margin
        view_rect = self.viewport().rect()
        margin_x = view_rect.width() * 0.3
        margin_y = view_rect.height() * 0.3
        margin_rect = view_rect.adjusted(-margin_x, -margin_y,
                                          margin_x, margin_y)
        scene_rect = self.mapToScene(margin_rect).boundingRect()

        # 转成 target level 坐标
        lx = max(0, int(scene_rect.x() / scale))
        ly = max(0, int(scene_rect.y() / scale))
        lw = min(int(scene_rect.width() / scale) + 2,
                 r.levels[level].width - lx)
        lh = min(int(scene_rect.height() / scale) + 2,
                 r.levels[level].height - ly)

        if lw <= 0 or lh <= 0:
            return

        # 分块读取
        tile_w = min(lw, 1024)
        tile_h = min(lh, 1024)

        needed_tiles = set()
        for ty in range(ly, ly + lh, tile_h):
            for tx in range(lx, lx + lw, tile_w):
                tw = min(tile_w, lx + lw - tx)
                th = min(tile_h, ly + lh - ty)
                key = (level, tx, ty, tw, th)
                needed_tiles.add(key)

        # 第一步：加载需要的 tiles（先于清理，避免空窗期）
        for key in needed_tiles:
            if key in self._tile_items:
                continue  # 已加载
            if key in self._tile_cache:
                pix = self._tile_cache.pop(key)
                self._tile_cache[key] = pix
                self._add_tile_item(key, pix)
                continue

            _level, tx, ty, tw, th = key
            try:
                region = r._read_level_region(_level, tx, ty, tw, th)
                img_bytes = region.tobytes()
                img = QImage(img_bytes, tw, th, tw * 3,
                             QImage.Format.Format_RGB888)
                if img.isNull():
                    continue
                pix = QPixmap.fromImage(img)

                self._tile_cache[key] = pix
                if len(self._tile_cache) > self._max_cache:
                    self._tile_cache.popitem(last=False)

                self._add_tile_item(key, pix)
            except Exception:
                pass

        # 第二步：移除不再需要的 tiles（比 visible 区域更远的才移除）
        visible_scene = self.mapToScene(self.viewport().rect()).boundingRect()
        for key in list(self._tile_items.keys()):
            if key == ('thumb',):
                continue
            if key not in needed_tiles:
                # 检查是否在可视区域内或紧邻
                _l, tx, ty, tw, th = key
                key_rect = QRectF(tx * scale, ty * scale,
                                  tw * scale, th * scale)
                if not visible_scene.intersects(key_rect):
                    item = self._tile_items.pop(key)
                    self._scene.removeItem(item)

    def _cleanup_non_thumb_tiles(self) -> None:
        """移除所有非缩略图的 tile 项（缩小时清理高分辨率 tile）。"""
        for key in list(self._tile_items.keys()):
            if key != ('thumb',):
                item = self._tile_items.pop(key)
                self._scene.removeItem(item)

    def _add_tile_item(self, key: tuple, pix: QPixmap) -> None:
        """在场景中添加 tile，放到正确位置。"""
        level, tx, ty, tw, th = key
        if self._reader is None:
            return
        scale = self._reader.levels[level].downsample

        item = QGraphicsPixmapItem(pix)
        # tile tx,ty 是 target level 坐标 → 转成 level 0 坐标
        item.setPos(tx * scale, ty * scale)
        # 缩放到 level 0 尺寸
        item.setScale(scale)
        item.setZValue(-level)  # 高层级在前
        self._scene.addItem(item)
        self._tile_items[key] = item

    # ── 事件处理 ──────────────────────────────────────

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._emit_viewport()
        self._render_timer.start(200)

    def mouseMoveEvent(self, event):
        # 无论是否 ROI 模式，都在 super 之前更新浮动框位置
        # 这样左键拖拽平移时浮动框也跟着动
        if self._roi_mode and self._frame_visible:
            self._update_frame_pos(self.mapToScene(event.pos()))
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        # 拖动中不加载 tiles，避免同步 DLL 调用阻塞 UI
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self._emit_viewport()
        # 松手后才刷新 tiles
        self._render_timer.start(100)

    def wheelEvent(self, event):
        scale_factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse
        )
        self.scale(scale_factor, scale_factor)
        self._emit_viewport()
        # 缩放后异步刷新 tiles
        self._render_timer.start(100)

    def _emit_viewport(self) -> None:
        """发射当前视口可见区域信号。"""
        if self._reader is None:
            return
        rect = self.mapToScene(self.viewport().rect()).boundingRect()
        self.viewport_changed.emit(rect)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Space and self._roi_mode:
            self._place_roi_at_frame()
            return
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and not event.isAutoRepeat():
            # 删除所有选中的 ROI（快照列表避免迭代时修改）
            for item in list(self._scene.selectedItems()):
                if isinstance(item, ROIRectItem):
                    self.roi_selected.emit(item.roi_id)
            return
        super().keyPressEvent(event)
