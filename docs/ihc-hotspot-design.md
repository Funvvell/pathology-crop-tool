## IHC 阳性热点自动检测与 ROI 生成 — 技术方案

### 功能概述

在现有病理裁剪工具的"分析工具"面板中新增一个"IHC 热点检测"按钮，用户点击后弹出参数对话框，系统对当前切片（或已有 ROI 区域）进行免疫组化阳性信号的颜色反卷积分离，自动找到阳性面积密度最高的 N 个热点区域，并生成 ROI 框。

参考了 QuPath 的 Positive Cell Detection 算法、ImageJ IHC Profiler 插件、scikit-image 的 `separate_stains` 颜色反卷积实现，以及学术论文中的 Ki-67 热点检测滑动窗口方法。

---

### 算法流程

整体采用 **两阶段策略**：缩略图快速候选 → 全分辨率精确分析。

```
阶段 1：缩略图粗定位（毫秒级）
┌──────────────────────────────────────────────────────┐
│  缩略图 RGB → 颜色反卷积 → 分离阳性通道              │
│         → Otsu 阈值 → 阳性像素二值图                 │
│         → 滑动窗口计算密度图                          │
│         → 取 Top-K 候选区域（K = 3~5 × N）           │
└──────────────────────────────────────────────────────┘

阶段 2：全分辨率精确分析（秒级，QThread 后台）
┌──────────────────────────────────────────────────────┐
│  对每个候选区域：                                     │
│    从 WSI 金字塔读取全分辨率图像块                    │
│    → 颜色反卷积 → DAB/AEC 通道分离                  │
│    → 自适应阈值分割阳性像素                          │
│    → 形态学处理（去噪、连接）                         │
│    → 计算精确阳性面积比                               │
│  按精确密度重新排序，取 Top-N                         │
│  → 生成 ROIModel，坐标映射回 level-0 空间            │
└──────────────────────────────────────────────────────┘
```

---

### 核心算法模块设计

#### 1. 颜色反卷积（Color Deconvolution）

基于 Ruifrok & Johnston (2001) 算法，使用 scikit-image 内置实现。

```python
from skimage.color import separate_stains, hdx_from_rgb, hax_from_rgb

# 支持的染色组合及对应的反卷积矩阵
STAIN_MATRICES = {
    "H-DAB": hdx_from_rgb,    # 苏木精 + DAB（棕色），最常用
    "H-AEC": hax_from_rgb,    # 苏木精 + AEC（红色）
    "H-E":   hed_from_rgb,    # 苏木精 + 伊红（粉/蓝）
    # 自定义：用户可手动输入 3×3 矩阵
}

def color_deconvolution(img_rgb: np.ndarray, stain_type: str = "H-DAB") -> dict:
    """
    颜色反卷积，分离染色通道。

    Args:
        img_rgb: RGB 图像 (H, W, 3), uint8
        stain_type: 染色类型

    Returns:
        {
            "positive": np.float64,  # 阳性通道（DAB/AEC）的浓度图
            "counterstain": np.float64,  # 对照通道（苏木精）的浓度图
            "residual": np.float64,  # 残余通道
        }
    """
    matrix = STAIN_MATRICES[stain_type]
    stains = separate_stains(img_rgb, matrix)
    return {
        "positive": stains[:, :, 1],       # 第2通道 = 阳性信号
        "counterstain": stains[:, :, 0],   # 第1通道 = 苏木精
        "residual": stains[:, :, 2],       # 第3通道 = 残余
    }
```

#### 2. 阳性区域阈值分割

```python
def threshold_positive(
    positive_channel: np.ndarray,
    method: str = "otsu",      # "otsu" | "manual"
    manual_threshold: float = 0.3,
    min_area: int = 50,        # 最小阳性连通域面积（像素）
) -> np.ndarray:
    """
    对阳性通道进行阈值分割，生成二值阳性 mask。

    Args:
        positive_channel: 颜色反卷积后的阳性通道浓度图
        method: 阈值方法（Otsu 自适应 或 手动）
        manual_threshold: 手动阈值（仅 manual 模式）
        min_area: 过滤小于该面积的碎片

    Returns:
        二值 mask (H, W), bool
    """
    # 归一化到 0-255
    ch = positive_channel.copy()
    ch_min, ch_max = ch.min(), ch.max()
    if ch_max > ch_min:
        ch_norm = ((ch - ch_min) / (ch_max - ch_min) * 255).astype(np.uint8)
    else:
        ch_norm = np.zeros_like(ch, dtype=np.uint8)

    if method == "otsu":
        _, binary = cv2.threshold(ch_norm, 0, 255,
                                   cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    else:
        thr_val = int(manual_threshold * 255)
        _, binary = cv2.threshold(ch_norm, thr_val, 255, cv2.THRESH_BINARY)

    # 形态学去噪
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)

    # 移除小碎片
    if min_area > 0:
        num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(binary)
        for i in range(1, num_labels):
            if stats[i, cv2.CC_STAT_AREA] < min_area:
                binary[labels == i] = 0

    return binary > 0
```

#### 3. 热点密度计算（滑动窗口）

参考学术论文中 Ki-67 热点检测的滑动窗口方法：

```python
def compute_density_map(
    positive_mask: np.ndarray,
    window_size: int,
    stride: int = None,
) -> np.ndarray:
    """
    用滑动窗口计算阳性像素密度图。

    Args:
        positive_mask: 二值阳性 mask (H, W), bool
        window_size: 滑动窗口尺寸（像素）
        stride: 步长，默认 = window_size // 2

    Returns:
        密度图 (H', W'), float, 值域 [0, 1]
    """
    if stride is None:
        stride = max(1, window_size // 2)

    h, w = positive_mask.shape
    # 使用 scipy.ndimage.uniform_filter 加速（等价于滑动窗口均值）
    density_full = ndimage.uniform_filter(
        positive_mask.astype(np.float32),
        size=window_size,
        mode='constant'
    )
    # 降采样到 stride 步长（减少候选点数量）
    density_map = density_full[::stride, ::stride]
    return density_map
```

#### 4. 热点区域提取

```python
def find_hotspots(
    density_map: np.ndarray,
    stride: int,
    roi_w: int, roi_h: int,
    scale_x: float, scale_y: float,
    n_hotspots: int = 5,
    min_density: float = 0.05,  # 最低阳性密度阈值
    nms_distance: int = None,   # 非极大值抑制距离
) -> list[tuple[int, int, int, int, float]]:
    """
    从密度图中提取 Top-N 热点区域。

    Returns:
        [(x, y, w, h, density), ...]  坐标为 level-0 全分辨率空间
    """
    if nms_distance is None:
        nms_distance = min(roi_w, roi_h) // 2

    # 按密度降序排列所有候选位置
    flat_indices = np.argsort(-density_map.ravel())
    candidates = []
    selected = []

    for idx in flat_indices:
        row, col = divmod(int(idx), density_map.shape[1])
        d = density_map[row, col]
        if d < min_density:
            break

        # 映射回原图坐标
        full_x = int(col * stride * scale_x)
        full_y = int(row * stride * scale_y)

        # 非极大值抑制：与已选热点距离太近的跳过
        too_close = False
        for sx, sy, _, _, _ in selected:
            if abs(full_x - sx) < nms_distance and abs(full_y - sy) < nms_distance:
                too_close = True
                break
        if too_close:
            continue

        # 以热点为中心生成 ROI
        cx = full_x + roi_w // 2
        cy = full_y + roi_h // 2
        roi = (max(0, cx - roi_w // 2), max(0, cy - roi_h // 2),
               roi_w, roi_h, float(d))
        selected.append(roi)
        if len(selected) >= n_hotspots:
            break

    return selected
```

---

### 新增文件结构

```
liver_portal_crop/
├── ihc_hotspot.py          # 新增：IHC 阳性热点检测核心算法 + 对话框
│   ├── color_deconvolution()     # 颜色反卷积
│   ├── threshold_positive()      # 阳性阈值分割
│   ├── compute_density_map()     # 密度图计算
│   ├── find_hotspots()           # 热点区域提取
│   ├── detect_ihc_hotspots()     # 主入口（组合以上步骤）
│   ├── refine_hotspots_fullres() # 全分辨率精确分析
│   ├── _IHCPreviewView           # 可缩放预览组件 (QGraphicsView)
│   └── IHCHotspotDialog          # 参数对话框（QDialog，含可缩放预览）
```

不新增其他文件，所有逻辑集中在 `ihc_hotspot.py` 中。`_IHCPreviewView` 是内部类，参考 `results_viewer.py` 的 `_ImageViewer` 实现。

---

### UI 对话框设计

对话框采用 **左右分栏布局**：左侧为可缩放的图像预览区，右侧为参数面板。

#### 可缩放预览区（核心改进）

**不复用 TissueDialog 的 QLabel 静态预览**，而是基于项目已有的 `QGraphicsView` + `QGraphicsScene` 模式（参考 `results_viewer.py` 的 `_ImageViewer` 类和 `preview_dialog.py` 的 `FullResPreviewDialog`），实现一个支持滚轮缩放和拖拽平移的预览查看器。

预览查看器 `_IHCPreviewView` 的实现要点：

```python
class _IHCPreviewView(QGraphicsView):
    """IHC 热点预览查看器 — 支持滚轮缩放 + 拖拽平移 + 叠加层绘制。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setBackgroundBrush(Qt.GlobalColor.black)

        self._base_item: QGraphicsPixmapItem | None = None  # 底图
        self._overlay_item: QGraphicsPixmapItem | None = None  # 阳性叠加
        self._marker_items: list = []  # 热点标记（十字 + ROI 框）

    def set_image(self, qimage: QImage):
        """设置底图并 fit to view。"""
        self._scene.clear()
        self._base_item = self._scene.addPixmap(QPixmap.fromImage(qimage))
        self._scene.setSceneRect(qimage.rect())
        self.fitInView(self._scene.sceneRect(),
                       Qt.AspectRatioMode.KeepAspectRatio)

    def update_overlay(self, overlay_qimage: QImage,
                       hotspots: list, roi_thumb_w: int, roi_thumb_h: int):
        """更新阳性区域叠加层和热点标记。"""
        # 移除旧的叠加
        if self._overlay_item:
            self._scene.removeItem(self._overlay_item)
        for item in self._marker_items:
            self._scene.removeItem(item)
        self._marker_items.clear()

        # 叠加半透明阳性区域
        self._overlay_item = self._scene.addPixmap(
            QPixmap.fromImage(overlay_qimage))
        self._overlay_item.setOpacity(0.5)

        # 绘制热点标记
        for (x, y, w, h, density) in hotspots:
            # 黄色十字
            cx, cy = x + w // 2, y + h // 2
            cross_size = max(4, min(w, h) // 10)
            pen = QPen(QColor(255, 215, 0), 2)
            self._marker_items.append(
                self._scene.addLine(cx - cross_size, cy,
                                    cx + cross_size, cy, pen))
            self._marker_items.append(
                self._scene.addLine(cx, cy - cross_size,
                                    cx, cy + cross_size, pen))
            # 黄色虚线 ROI 框
            roi_pen = QPen(QColor(255, 215, 0), 1.5)
            roi_pen.setStyle(Qt.PenStyle.DashLine)
            rect_item = self._scene.addRect(x, y, w, h, roi_pen)
            self._marker_items.append(rect_item)
            # 密度标签
            text = self._scene.addSimpleText(
                f"#{len(self._marker_items)//3}  {density:.1%}")
            text.setPos(x + 2, y + 2)
            text.setBrush(QColor(255, 215, 0))
            self._marker_items.append(text)

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_0 or event.key() == Qt.Key.Key_F:
            self.resetTransform()
            self.fitInView(self._scene.sceneRect(),
                           Qt.AspectRatioMode.KeepAspectRatio)
        else:
            super().keyPressEvent(event)
```

#### 对话框布局

```
┌───────────────────────────────────────────────────────────────┐
│  IHC 热点检测参数                                              │
│                                                               │
│  ┌──────────────────────────────────┬─────────────────────┐   │
│  │                                  │  染色类型: [H-DAB ▼]│   │
│  │                                  │  倍率:     [20x  ▼] │   │
│  │    可缩放预览区                   │  比例:     [16:9 ▼] │   │
│  │    (QGraphicsView)               │  框尺寸:  4096×2304 │   │
│  │                                  │                     │   │
│  │    • 滚轮缩放                    │  ── 检测参数 ──      │   │
│  │    • 拖拽平移                    │  最大热点数: [5]     │   │
│  │    • 阳性区域红色半透明叠加      │  窗口大小:   [200]   │   │
│  │    • 热点黄色十字 + ROI 虚线框   │  阈值方法: [Otsu ▼] │   │
│  │                                  │  手动阈值:  [0.30]   │   │
│  │    按 0/F 键重置视图             │  最小面积:   [50] px │   │
│  │                                  │                     │   │
│  │                                  │  ── 范围 ──         │   │
│  │                                  │  检测范围: [切片 ▼] │   │
│  │                                  │  [□] 全分辨率精确   │   │
│  │                                  │                     │   │
│  │                                  │  阳性: 12.3% | N=5  │   │
│  └──────────────────────────────────┴─────────────────────┘   │
│                                                               │
│  [生成 ROI]                          [取消]                    │
└───────────────────────────────────────────────────────────────┘
```

对话框用 `QSplitter` 左右分割：
- **左侧（约 65% 宽度）**：`_IHCPreviewView` 可缩放预览区
- **右侧（约 35% 宽度）**：参数面板 `QFormLayout`，底部按钮

#### 预览更新逻辑

用户调整任何参数时，`_update_preview()` 被触发：

1. 在缩略图上运行颜色反卷积 + 阈值分割 → 得到阳性 mask
2. 生成红色半透明叠加图（阳性区域涂红）
3. 运行滑动窗口热点检测 → 得到候选热点列表
4. 调用 `_IHCPreviewView.update_overlay(overlay, hotspots, ...)` 更新叠加层和标记
5. 底部状态标签显示阳性面积百分比和热点数

由于预览区是 QGraphicsView，用户放大后查看热点细节时，叠加层和标记会自动跟随缩放，无需额外处理。

#### 对话框参数说明

- **染色类型**：H-DAB / H-AEC / H-E，决定颜色反卷积矩阵
- **倍率/比例/框尺寸**：与组织检测一致，计算 ROI 实际像素尺寸
- **最大热点数**：Top-N 热点数量
- **窗口大小**：滑动窗口的像素大小，控制热点的粒度（越大越平滑）
- **阈值方法**：Otsu 自适应 / 手动阈值
- **最小阳性面积**：过滤碎片噪声
- **检测范围**：整个切片 / 现有 ROI 区域内
- **全分辨率精确分析**：勾选后执行两阶段分析（缩略图粗定位 → 全分辨率精确验证）

实时预览显示内容：
- 阳性区域用半透明红色叠加
- 热点中心位置用黄色十字标记 + 编号 + 密度百分比标签
- 预估 ROI 框用黄色虚线框
- 支持滚轮放大查看热点细节，拖拽平移浏览全图

---

### 集成方案

#### app.py 修改

1. **新增 import**：
```python
from liver_portal_crop.ihc_hotspot import (
    detect_ihc_hotspots, refine_hotspots_fullres, IHCHotspotDialog,
)
```

2. **右侧面板"分析工具"组新增按钮**（在 `_setup_ui` 方法中，紧跟 `_tissue_btn` 后面）：
```python
self._ihc_hotspot_btn = QPushButton("IHC 热点检测")
self._ihc_hotspot_btn.clicked.connect(self._detect_ihc_hotspot)
analysis_layout.addWidget(self._ihc_hotspot_btn)
```

3. **"分析"菜单新增条目**（在 `_setup_menu` 方法中）：
```python
ihc_action = QAction("IHC 热点检测...", self)
ihc_action.triggered.connect(self._detect_ihc_hotspot)
analysis_menu.addAction(ihc_action)
```

4. **新增处理方法 `_detect_ihc_hotspot`**（与 `_detect_tissue` 同构）：
```python
def _detect_ihc_hotspot(self):
    """IHC 阳性热点检测 → 生成 ROI。"""
    if not self._readers:
        QMessageBox.warning(self, "提示", "请先打开切片文件")
        return

    reader = self._readers[self._current_slide]
    tile_w = self._frame_w_spin.value()
    tile_h = self._frame_h_spin.value()

    dlg = IHCHotspotDialog(
        reader, tile_w, tile_h, self,
        readers=self._readers,
        current_slide=self._current_slide,
        roi_manager=self._roi_manager,  # 传入以支持"现有 ROI 区域内"模式
    )
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return

    params = dlg.get_params()
    # ... 执行检测，生成 ROIModel，刷新画布（与组织检测流程一致）
```

#### 依赖

无需新增依赖。所有算法基于已有库：

| 库 | 用途 |
|---|---|
| scikit-image (`skimage.color.separate_stains`) | 颜色反卷积 |
| OpenCV (`cv2`) | 阈值分割、形态学、连通域分析 |
| scipy (`scipy.ndimage`) | 均匀滤波（密度图加速） |
| numpy | 数组运算 |
| PySide6 | 对话框 UI |

---

### 全分辨率精确分析的实现细节

当用户勾选"全分辨率精确分析"时，采用两阶段流程：

**阶段 1** — 缩略图粗定位：
- 在缩略图上运行完整的颜色反卷积 + 阈值分割 + 滑动窗口
- 取 Top-K 候选区域（K = 3 × N，多取一些供精确阶段筛选）

**阶段 2** — 全分辨率验证（QThread 后台线程）：
- 对每个候选区域，调用 `reader.extract_region(x, y, w, h, level=0)` 读取全分辨率图像块
- 在全分辨率图像块上重新运行颜色反卷积 + 阈值分割
- 计算精确的阳性面积比
- 按精确密度重新排序，取 Top-N

**性能考虑**：
- 全分辨率读取受 DLL 全局锁限制，必须串行化
- 每个候选区域的图像块大小约为 ROI 框尺寸（如 4096×2304），约 28MB RGB
- 颜色反卷积是纯数值运算，4096×2304 约 0.1~0.3 秒
- 总计 5~15 个候选区域，后台运行约 2~8 秒
- 使用 QThread + 进度条，不阻塞 UI

```python
def refine_hotspots_fullres(
    reader: SDPCReader,
    candidates: list[tuple],   # 阶段1的候选列表
    roi_w: int, roi_h: int,
    stain_type: str,
    threshold_method: str,
    threshold_value: float,
    min_area: int,
    n_hotspots: int,
    progress_callback=None,
) -> list[tuple[int, int, int, int, float]]:
    """
    在全分辨率上精确验证候选热点区域。
    """
    refined = []
    for i, (x, y, w, h, _) in enumerate(candidates):
        if progress_callback:
            progress_callback(i, len(candidates))

        # 从 WSI 读取全分辨率图像块
        try:
            patch = reader.extract_region(x, y, w, h, level=0)
        except Exception:
            continue

        # 颜色反卷积
        deconv = color_deconvolution(patch, stain_type)

        # 阈值分割
        pos_mask = threshold_positive(
            deconv["positive"], threshold_method, threshold_value, min_area
        )

        # 计算精确阳性面积比
        density = pos_mask.sum() / pos_mask.size
        refined.append((x, y, roi_w, roi_h, density))

    # 按精确密度降序排列，取 Top-N
    refined.sort(key=lambda r: -r[4])
    return refined[:n_hotspots]
```

---

### 代码参考来源

| 来源 | 参考内容 |
|---|---|
| scikit-image `separate_stains` | Ruifrok & Johnston 颜色反卷积算法实现，内置 H-DAB / H-AEC 矩阵 |
| QuPath Positive Cell Detection | DAB OD 阈值分割策略（thresholdPositive / thresholdNegative） |
| IHC Profiler (ImageJ) | 阳性强度分级标准（High Positive / Positive / Low Positive / Negative） |
| HistomicsTK (DigitalSlideArchive) | Macenko PCA 自动染色向量估计方法 |
| Ki-67 热点检测论文 (MDPI 2020) | 滑动窗口密度图 + 非极大值抑制的热点定位方法 |
| IJ-Colour_Deconvolution2 (Landini) | "Optimize" 自适应染色向量估计策略 |

---

### 实现步骤

1. **创建 `ihc_hotspot.py`** — 核心算法函数 + IHCHotspotDialog 对话框类
2. **修改 `app.py`** — 新增按钮、菜单项、处理方法
3. **修改 `constants.py`** — 添加 IHC 热点检测默认常量（可选）
4. **测试** — 用实际 IHC 切片验证颜色反卷积效果和热点定位准确性
