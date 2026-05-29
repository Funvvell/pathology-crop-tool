# ROI 预览选择导出 — 设计文档

## 概述

为病理裁剪工具增加 ROI 预览、选择、批量导出功能。用户可通过独立预览对话框查看所有 ROI 的缩略图，勾选目标 ROI 后仅导出选中项。

## 用户流程

```
用户点击 "预览导出" 按钮
  → 弹出 ROIPreviewDialog
  → 后台 QThread 生成所有 ROI 缩略图（显示加载进度）
  → 缩略图网格展示，每个带 checkbox
  → 用户勾选目标 ROI（支持全选/反选/按文件筛选）
  → 双击缩略图弹出大图预览（QDialog + QGraphicsView，level 0 全分辨率）
  → 点击 "导出选中"
  → 对话框关闭，返回选中 ROI ID 列表
  → MainWindow 复用 BatchExporter 仅导出选中 ROI
```

现有的 "批量导出" 按钮保留不变（导出全部），新增 "预览导出" 按钮。

## 对话框布局

```
┌─────────────────────────────────────────────────────────┐
│  ROI 预览与导出                                    [X]  │
├─────────────────────────────────────────────────────────┤
│  [全选] [反选] [全不选]   筛选: [全部文件 ▼]  已选: 0/0 │
├─────────────────────────────────────────────────────────┤
│  ┌─ slide1.sdpc ──────────────────────────────────────┐ │
│  │ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐     │ │
│  │ │  ☑   │ │  ☑   │ │  ☐   │ │  ☑   │ │  ☐   │     │ │
│  │ │ img  │ │ img  │ │ img  │ │ img  │ │ img  │     │ │
│  │ │      │ │      │ │      │ │      │ │      │     │ │
│  │ │120×80│ │120×80│ │120×80│ │120×80│ │120×80│     │ │
│  │ └──────┘ └──────┘ └──────┘ └──────┘ └──────┘     │ │
│  └────────────────────────────────────────────────────┘ │
│  ┌─ slide2.sdpc ──────────────────────────────────────┐ │
│  │ ┌──────┐ ┌──────┐ ┌──────┐                         │ │
│  │ │  ☑   │ │  ☐   │ │  ☑   │                         │ │
│  │ │ img  │ │ img  │ │ img  │                         │ │
│  │ └──────┘ └──────┘ └──────┘                         │ │
│  └────────────────────────────────────────────────────┘ │
├─────────────────────────────────────────────────────────┤
│  缩略图大小: [━━━●━━━] 120px               [导出选中]  │
└─────────────────────────────────────────────────────────┘
```

### 交互细节

- **缩略图卡片**：正方形，内含缩略图 + 左上角 checkbox + 底部尺寸标签
- **双击预览**：弹出独立 `QDialog`，使用 `QGraphicsView` 显示 level 0 全分辨率裁剪图，可缩放/平移，标题显示坐标和尺寸
- **文件筛选下拉框**：选项为 "全部文件" + 各文件名，切换时过滤显示但保留选中状态
- **缩略图大小滑块**：80~200px，调整网格密度
- **已选计数**：实时更新 "已选: 3/12"
- **导出选中按钮**：关闭对话框，返回选中 ROI ID 列表给 MainWindow

## 缩略图生成

- 使用 `SDPCReader.extract_region()` 读取 ROI 区域
- 选择合适的金字塔层级（不使用 level 0，避免慢）：取能满足缩略图尺寸的最高层级
- 缩放为目标缩略图尺寸（如 120×120），保持宽高比
- 后台 `QThread`（`ThumbnailWorker`）逐个生成，发射 `thumbnail_ready(roi_id, QPixmap)` 信号
- 对话框打开时显示加载状态，缩略图逐个出现

### 层级选择策略

```python
def _pick_thumb_level(reader, roi_w, roi_h, target_size):
    """选择能满足缩略图尺寸的最高金字塔层级。"""
    for level in range(reader.level_count - 1, -1, -1):
        ds = reader.levels[level].downsample
        lw = int(roi_w / ds)
        lh = int(roi_h / ds)
        if lw >= target_size and lh >= target_size:
            return level
    return 0
```

## 选中状态管理

- 选中状态存储在 `ROIPreviewDialog` 内部：`_selected_ids: set[str]`
- checkbox 变更时更新 set 和已选计数
- 全选/反选/全不选按钮操作当前可见项（受文件筛选影响）
- 文件筛选切换不影响已选状态
- 对话框关闭时通过 `get_selected_ids()` 返回选中 ID 集合

## 与现有导出流程的集成

MainWindow 新增方法 `_start_export_selected(selected_ids: list[str])`：

```python
def _start_export_selected(self, selected_ids: list[str]) -> None:
    """仅导出选中的 ROI。"""
    all_rois = self._roi_manager.all_rois()
    selected_rois = [r for r in all_rois if r.id in set(selected_ids)]
    if not selected_rois:
        return
    # 复用现有导出逻辑（进度条、BatchExporter、取消等）
    self._run_export(selected_rois)
```

将现有 `_start_export` 中的导出逻辑提取为 `_run_export(rois)` 共用方法，`_start_export` 调用 `_run_export(all_rois)`，`_start_export_selected` 调用 `_run_export(selected_rois)`。

## 文件变更

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `liver_portal_crop/preview_dialog.py` | 新增 | ROIPreviewDialog — 预览选择对话框 |
| `liver_portal_crop/app.py` | 修改 | 添加 "预览导出" 按钮 + 提取 `_run_export` + `_start_export_selected` |
| `liver_portal_crop/theme.qss` | 修改 | 添加预览对话框样式 |
| `liver_portal_crop/theme_light.qss` | 修改 | 同步浅色主题样式 |

## 边界情况

- **无 ROI 时**：点击 "预览导出" 提示 "请先标注 ROI"
- **大量 ROI（>200）**：缩略图生成期间显示进度，支持取消
- **文件筛选为空**：不显示任何卡片，已选计数不变
- **双击预览时 reader 不可用**：显示错误提示
- **对话框打开期间 ROI 被修改**：以打开时的 ROI 快照为准
