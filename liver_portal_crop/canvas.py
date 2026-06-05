"""WSICanvas — 基于 QGraphicsView 的金字塔 WSI 查看器 + ROI 标注工具。

直接在原图坐标系（level 0）上操作，根据缩放级别自动选择金字塔层级
并加载可见区域的 tiles，ROI 坐标即为 level 0 坐标，导出时无需转换。
"""

from __future__ import annotations

import math
import uuid
from collections import OrderedDict

import numpy as np

from PySide6.QtCore import Qt, QPointF, QRectF, QTimer, Signal
from PySide6.QtGui import (
    QBrush, QColor, QImage, QPainter, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QGraphicsEllipseItem, QGraphicsItem, QGraphicsPixmapItem, QGraphicsRectItem,
    QGraphicsScene, QGraphicsView,
)

from liver_portal_crop.reader import SDPCReader


class RotateHandle(QGraphicsEllipseItem):
    """ROI 旋转手柄（圆环，拖拽旋转）。

    放在矩形顶部中央上方，拖拽时绕矩形中心旋转。
    """

    HANDLE_SIZE = 12
    OFFSET = 20  # 距离顶边的偏移像素

    def __init__(self, parent_roi: QGraphicsRectItem):
        hs = self.HANDLE_SIZE
        super().__init__(-hs // 2, -hs // 2, hs, hs, parent_roi)
        self._parent_roi = parent_roi
        self.setAcceptHoverEvents(True)
        self.setBrush(QBrush(QColor(255, 165, 0)))  # 橙色
        self.setPen(QPen(QColor(200, 100, 0), 2))
        self.setZValue(2)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._drag_start_angle: float | None = None

    def _get_center_scene(self) -> QPointF:
        """获取父 ROI 中心的场景坐标。"""
        r = self._parent_roi.rect()
        center_local = QPointF(r.left() + r.width() / 2,
                                r.top() + r.height() / 2)
        return self._parent_roi.mapToScene(center_local)

    def mousePressEvent(self, event):
        center = self._get_center_scene()
        pos = event.scenePos()
        self._drag_start_angle = math.atan2(pos.y() - center.y(),
                                             pos.x() - center.x())
        self._drag_start_rotation = self._parent_roi.rotation()
        self._parent_roi.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_start_angle is None:
            return
        center = self._get_center_scene()
        pos = event.scenePos()
        current_angle = math.atan2(pos.y() - center.y(),
                                    pos.x() - center.x())
        delta_deg = math.degrees(current_angle - self._drag_start_angle)
        new_rotation = (self._drag_start_rotation + delta_deg) % 360
        self._parent_roi.setRotation(new_rotation)
        self._parent_roi._update_handle_positions()

    def mouseReleaseEvent(self, event):
        if self._drag_start_angle is not None:
            self._drag_start_angle = None
            self._parent_roi.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
            self._parent_roi._emit_changed()
        event.accept()


class ResizeHandle(QGraphicsEllipseItem):
    """ROI 缩放手柄（白色小圆点，拖拽缩放）。"""

    HANDLE_SIZE = 8

    def __init__(self, parent_roi: QGraphicsRectItem, pos_x: float, pos_y: float):
        hs = self.HANDLE_SIZE
        super().__init__(-hs // 2, -hs // 2, hs, hs, parent_roi)
        self._parent_roi = parent_roi
        self._pos_x = pos_x
        self._pos_y = pos_y
        self.setAcceptHoverEvents(True)
        self.setBrush(QBrush(Qt.GlobalColor.white))
        self.setPen(QPen(QColor(0, 120, 215), 2))
        self.setZValue(1)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        self._drag_start_rect: QRectF | None = None

    def _cursor_for_pos(self) -> Qt.CursorShape:
        """根据手柄位置返回鼠标形状。"""
        corners = {
            (0, 0): Qt.CursorShape.SizeFDiagCursor,
            (1, 0): Qt.CursorShape.SizeBDiagCursor,
            (0, 1): Qt.CursorShape.SizeBDiagCursor,
            (1, 1): Qt.CursorShape.SizeFDiagCursor,
        }
        key = (self._pos_x, self._pos_y)
        if key in corners:
            return corners[key]
        if self._pos_y == 0 or self._pos_y == 1:
            return Qt.CursorShape.SizeVerCursor
        return Qt.CursorShape.SizeHorCursor

    def hoverEnterEvent(self, event):
        self.setCursor(self._cursor_for_pos())
        super().hoverEnterEvent(event)

    def mousePressEvent(self, event):
        self._drag_start_rect = self._parent_roi.rect()
        self._drag_start_scene = event.scenePos()
        self._parent_roi.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_start_rect is None:
            return
        delta = event.scenePos() - self._drag_start_scene
        r = QRectF(self._drag_start_rect)

        if self._pos_x == 0:
            r.setLeft(r.left() + delta.x())
        elif self._pos_x == 1:
            r.setRight(r.right() + delta.x())
        if self._pos_y == 0:
            r.setTop(r.top() + delta.y())
        elif self._pos_y == 1:
            r.setBottom(r.bottom() + delta.y())

        if r.width() < 20 or r.height() < 20:
            return

        self._parent_roi.setRect(r)
        self._update_handles()

    def mouseReleaseEvent(self, event):
        self._parent_roi.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        if self._drag_start_rect is not None:
            self._drag_start_rect = None
            self._drag_start_scene = None
            self._parent_roi._emit_changed()
        event.accept()

    def _update_handles(self):
        """更新父项上所有手柄的位置。"""
        if hasattr(self._parent_roi, '_update_handle_positions'):
            self._parent_roi._update_handle_positions()


class ROIRectItem(QGraphicsRectItem):
    """可缩放/可移动/可旋转的 ROI 矩形。

    注意：QGraphicsRectItem 不是 QObject，不能使用 Signal。
    通过 on_rect_changed 回调通知父项。
    """

    def __init__(self, roi_id: str, rect: QRectF, angle: float = 0.0,
                 on_changed=None, *args, **kwargs):
        super().__init__(rect, *args, **kwargs)
        self._roi_id = roi_id
        self._on_changed = on_changed  # callback: (roi_id, QRectF, float) -> None
        self.setAcceptHoverEvents(True)
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setPen(QPen(QColor(0, 120, 215), 2))
        self.setBrush(QBrush(QColor(0, 120, 215, 30)))
        self._hover_pen = QPen(QColor(255, 0, 0), 3)
        self._block_sync = False
        # 旋转：以矩形中心为原点
        r = rect
        self.setTransformOriginPoint(r.left() + r.width() / 2,
                                      r.top() + r.height() / 2)
        self.setRotation(angle)
        self._create_handles()

    def _create_handles(self):
        positions = [
            (0, 0), (0.5, 0), (1, 0),
            (0, 0.5), (1, 0.5),
            (0, 1), (0.5, 1), (1, 1),
        ]
        for px, py in positions:
            ResizeHandle(self, px, py)
        # 旋转手柄
        self._rotate_handle = RotateHandle(self)
        self._update_handle_positions()
        # 初始未选中，手柄隐藏
        for child in self.childItems():
            child.setVisible(False)

    def _update_handle_positions(self):
        """将所有手柄移动到矩形对应的位置。"""
        r = self.rect()
        for child in self.childItems():
            if isinstance(child, ResizeHandle):
                x = r.left() + child._pos_x * r.width()
                y = r.top() + child._pos_y * r.height()
                child.setPos(x, y)
            elif isinstance(child, RotateHandle):
                # 放在顶部中央上方
                cx = r.left() + r.width() / 2
                child.setPos(cx, r.top() - child.OFFSET)

    def _emit_changed(self):
        """通知父项 ROI 变更（场景坐标 + 旋转角度）。"""
        if self._block_sync or self._on_changed is None:
            return
        self._on_changed(self._roi_id,
                         self.mapRectToScene(self.rect()),
                         self.rotation())

    @property
    def roi_id(self) -> str:
        return self._roi_id

    def set_rect_silent(self, rect: QRectF):
        """设置矩形但不触发回调（用于初始化/恢复）。"""
        self._block_sync = True
        self.setRect(rect)
        # 更新旋转原点
        self.setTransformOriginPoint(rect.left() + rect.width() / 2,
                                      rect.top() + rect.height() / 2)
        self._block_sync = False

    def set_selected_appearance(self, selected: bool):
        """选中/取消选中时的外观。"""
        for child in self.childItems():
            child.setVisible(selected)
        if selected:
            self.setPen(QPen(QColor(255, 120, 0), 3))  # 橙色
        else:
            self.setPen(QPen(QColor(0, 120, 215), 2))  # 蓝色

    def hoverEnterEvent(self, event):
        self.setPen(self._hover_pen)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        if not self.isSelected():
            self.setPen(QPen(QColor(0, 120, 215), 2))
        super().hoverLeaveEvent(event)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            scene = self.scene()
            if scene and scene.views():
                view = scene.views()[0]
                if getattr(view, '_drag_guard_active', False) and \
                   self in getattr(view, '_drag_guard_items', set()):
                    return True
            self.set_selected_appearance(bool(value))
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange:
            scene = self.scene()
            if scene and scene.views():
                view = scene.views()[0]
                if getattr(view, '_drag_guard_active', False) and \
                   self in getattr(view, '_drag_guard_items', set()):
                    return self.pos()
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged \
           and not self._block_sync:
            self._update_handle_positions()
            if hasattr(value, 'x') and self._on_changed:
                r = self.mapRectToScene(self.rect())
                self._on_changed(self._roi_id, r, self.rotation())
        return super().itemChange(change, value)


class WSICanvas(QGraphicsView):
    """金字塔 WSI 查看器 + ROI 标注。

    场景坐标 = level 0 全分辨率坐标。
    根据缩放级别自动选择金字塔层级，按需加载可见区域。
    """

    roi_created = Signal(str, QRectF, float)   # roi_id, rect, angle
    roi_selected = Signal(str)
    roi_rect_changed = Signal(str, QRectF, float)  # roi_id, new_rect, angle
    roi_selection_changed = Signal(str)
    viewport_changed = Signal(QRectF)
    frame_angle_changed = Signal(float)  # 浮选框角度被右键拖拽改变

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
        self._drag_guard_items: set[ROIRectItem] = set()
        self._drag_guard_active: bool = False

        # 浮动框
        self._frame_w: int = 1024
        self._frame_h: int = 1024
        self._frame_item: QGraphicsRectItem | None = None
        self._frame_visible: bool = False
        self._frame_angle: float = 0.0
        self._frame_angle_dragging: bool = False
        self._frame_angle_start_mouse: float = 0.0
        self._frame_angle_start_value: float = 0.0
        self._drag_roi_mode: QGraphicsView.DragMode | None = None

        self._scene.selectionChanged.connect(self._on_scene_selection_changed)

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
        self._frame_angle = 0.0

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

    def _on_scene_selection_changed(self) -> None:
        """场景选中项变化时发射信号。"""
        selected = self._scene.selectedItems()
        for item in selected:
            if isinstance(item, ROIRectItem):
                self.roi_selection_changed.emit(item.roi_id)
                return
        self.roi_selection_changed.emit("")

    def set_frame_size(self, w: int, h: int) -> None:
        self._frame_w = w
        self._frame_h = h
        if self._frame_item:
            self._frame_item.setRect(0, 0, w, h)
            self._frame_item.setTransformOriginPoint(w / 2, h / 2)

    def set_frame_angle(self, angle: float) -> None:
        """设置浮动框旋转角度。"""
        self._frame_angle = angle % 360
        if self._frame_item:
            self._frame_item.setRotation(self._frame_angle)

    def get_frame_angle(self) -> float:
        return self._frame_angle

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

    def add_roi_rect(self, roi_id: str, rect: QRectF, angle: float = 0.0) -> None:
        item = ROIRectItem(roi_id, rect, angle=angle,
                           on_changed=self._on_roi_rect_changed)
        self._scene.addItem(item)
        self._roi_items[roi_id] = item

    def _on_roi_rect_changed(self, roi_id: str, new_rect: QRectF,
                              angle: float) -> None:
        self.roi_rect_changed.emit(roi_id, new_rect, angle)

    def update_roi_rect(self, roi_id: str, rect: QRectF) -> None:
        """更新指定 ROI 的矩形（不触发回调）。"""
        item = self._roi_items.get(roi_id)
        if item:
            item.set_rect_silent(rect)

    def select_roi(self, roi_id: str) -> None:
        """选中指定 ROI（外部调用，如从列表选中）。"""
        self._scene.blockSignals(True)
        self._scene.clearSelection()
        self._scene.blockSignals(False)
        item = self._roi_items.get(roi_id)
        if item:
            item.setSelected(True)

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
            self._frame_item.setTransformOriginPoint(
                self._frame_w / 2, self._frame_h / 2)
            self._frame_item.setRotation(self._frame_angle)
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
        self.add_roi_rect(roi_id, rect, angle=self._frame_angle)
        self.roi_created.emit(roi_id, rect, self._frame_angle)

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
        # 右键拖拽旋转浮选框
        if self._frame_angle_dragging and self._frame_item:
            scene_pos = self.mapToScene(event.pos())
            center = self._frame_item.mapToScene(
                self._frame_w / 2, self._frame_h / 2
            )
            current = math.degrees(math.atan2(
                scene_pos.y() - center.y(),
                scene_pos.x() - center.x(),
            ))
            delta = current - self._frame_angle_start_mouse
            new_angle = (self._frame_angle_start_value + delta) % 360
            self._frame_angle = new_angle
            self._frame_item.setRotation(new_angle)
            self.frame_angle_changed.emit(new_angle)
            return
        # 无论是否 ROI 模式，都在 super 之前更新浮动框位置
        # 这样左键拖拽平移时浮动框也跟着动
        if self._roi_mode and self._frame_visible:
            self._update_frame_pos(self.mapToScene(event.pos()))
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        # 右键拖拽旋转浮选框（ROI 模式下）
        if (event.button() == Qt.MouseButton.RightButton
                and self._roi_mode and self._frame_visible and self._frame_item):
            scene_pos = self.mapToScene(event.pos())
            center = self._frame_item.mapToScene(
                self._frame_w / 2, self._frame_h / 2
            )
            self._frame_angle_start_mouse = math.degrees(math.atan2(
                scene_pos.y() - center.y(),
                scene_pos.x() - center.x(),
            ))
            self._frame_angle_start_value = self._frame_angle
            self._frame_angle_dragging = True
            event.accept()
            return
        clicked = self.itemAt(event.pos())
        if isinstance(clicked, ResizeHandle):
            super().mousePressEvent(event)
            return
        if isinstance(clicked, RotateHandle):
            super().mousePressEvent(event)
            return
        # Walk up parent chain: itemAt may return a child item (handle)
        # instead of the ROIRectItem itself.
        target = clicked
        while target is not None and not isinstance(target, ROIRectItem):
            target = target.parentItem()
        if target is not None and isinstance(target, ROIRectItem):
            # Block scene's selectionChanged so super().mousePressEvent
            # can't auto-select a different ROI during the drag setup.
            self._scene.blockSignals(True)
            self._scene.clearSelection()
            target.setSelected(True)
            # Guard: prevent other ROIs from being selected/moved during drag
            self._drag_others = [i for i in self._roi_items.values() if i is not target]
            self._drag_guard_items = set(self._drag_others)
            self._drag_guard_active = True
            for item in self._drag_others:
                item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
            self._drag_roi_mode = self.dragMode()
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            super().mousePressEvent(event)
            self._scene.blockSignals(False)
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        # 右键拖拽旋转结束
        if self._frame_angle_dragging:
            self._frame_angle_dragging = False
            event.accept()
            return
        super().mouseReleaseEvent(event)
        # 恢复其他 ROI 的 ItemIsSelectable
        if getattr(self, '_drag_others', None):
            for item in self._drag_others:
                item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
                item.set_selected_appearance(False)
            self._drag_others = None
        self._drag_guard_active = False
        self._drag_guard_items = set()
        if self._drag_roi_mode is not None:
            self.setDragMode(self._drag_roi_mode)
            self._drag_roi_mode = None
        self._emit_viewport()
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
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            if self._roi_mode:
                self._place_roi_at_frame()
            else:
                # 不在ROI模式时按空格 → 请求切换模式
                self.roi_selected.emit("__toggle_roi__")
            return
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and not event.isAutoRepeat():
            # 删除所有选中的 ROI（快照列表避免迭代时修改）
            for item in list(self._scene.selectedItems()):
                if isinstance(item, ROIRectItem):
                    self.roi_selected.emit(item.roi_id)
            return
        super().keyPressEvent(event)
