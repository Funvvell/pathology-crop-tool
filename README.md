# 病理裁剪工具 (Pathology Crop Tool)

SDPC 全切片病理图像浏览与 ROI 批量裁剪桌面工具。支持生强（ShengQiang）SDPC 格式的病理全玻片图像（WSI）浏览、手动/自动 ROI 标注、IHC 免疫组化阳性热点检测、DeepLIIF AI 分析、ImageJ/Fiji 图像处理桥接，以及批量裁剪导出为 TIFF。

**当前版本**：v0.7.1

---

## 功能概览

### 文件管理

- 拖入或打开多张 SDPC 文件，左侧文件列表切换显示
- 自动读取金字塔层级元数据（层级数、每级宽高、下采样比、微米/像素比）
- 文件切换时自动保存/恢复 ROI 状态
- 过期 ROI 自动清理（关联文件已移除时）

### WSI 金字塔浏览

- 基于 `QGraphicsView` 的金字塔渲染引擎，支持超大切片（120000×80000+ 像素）
- 缩略图全图预览，滚轮缩放自动切换金字塔层级（`_get_best_level`）
- LRU Tile 缓存（512 张），按需加载可见区域 tiles，避免内存溢出
- 平移拖拽浏览，流畅无卡顿
- 导航缩略图（`NavigationWidget`）实时显示当前视口在全图中的位置，支持点击跳转

### ROI 标注

- **浮动框标注**：在 ROI 模式下，固定尺寸绿色虚线框跟随鼠标，按空格键快速创建 ROI
- **缩放手柄**：选中 ROI 后在四角四边显示白色圆点手柄，拖拽缩放（`ResizeHandle`）
- **拖动位置**：选中后直接拖拽 ROI 主体移动
- **选中外观**：未选中蓝色边框，选中橙色边框 + 显示手柄
- **实时坐标同步**：右侧面板 X/Y/W/H 输入框实时显示选中 ROI 坐标，修改即更新
- **倍率/比例自动计算**：输入放大倍率和视野比例，自动计算像素尺寸
- **角度旋转**：支持 ROI 角度调整
- **ROI 列表管理**：右侧面板显示所有 ROI，支持选中、删除、清空

### 组织检测（Tissue Detection）

- 基于 **HistoKit** 三通道 Otsu 阈值的自动组织区域识别
- RGB 三通道直方图 → 两步 Otsu 阈值 → 三通道组合判定 → 形态学处理（开/闭运算、填充孔洞、移除小碎片）
- **网格模式**：在组织区域内均匀生成 ROI 网格
- **连通域模式**：按连通区域大小排序生成 ROI
- 实时预览 + 参数调节（开运算半径、闭运算半径、填充、碎片过滤）
- 支持多文件批量组织检测

### IHC 阳性热点检测

完整的免疫组化（IHC）阳性信号自动检测与 ROI 生成流程：

- **色彩反卷积**（Ruifrok & Johnston 方法）：从 RGB 图像中分离目标染色通道
  - 支持 H-DAB（棕色阳性，苏木精 + DAB）
  - 支持 H-AEC（红色阳性，苏木精 + AEC）
  - 支持 H-E（苏木精 + 伊红）
- **OD 查找表预计算**：256 条目 LUT 替代逐像素对数运算，加速反卷积
- **分块 WSI 读取**：每次读取 ~2048×2048 tile，峰值内存 ~100 MB，安全处理超大切片
- **阳性信号累积**：逐 tile 检测阳性像素，累积到降采样全局掩膜（~17 MB）
- **滑动窗口密度图**：在全局掩膜上计算局部阳性密度
- **Top-N 热点提取**：使用 `scipy.ndimage` 峰值检测，提取密度最高的 N 个候选区域
- **组织覆盖率过滤**：自动生成 ROI 前检查每个热点的组织面积占比 ≥ 50%，排除背景区域
- **全分辨率验证**：可选在 level-0 级别验证候选区域的阳性信号
- **可视化叠加图**：生成热点叠加预览图（`make_overlay_image`）
- **非模态对话框**：检测参数配置、进度显示、结果确认，允许同时操作主窗口

### DeepLIIF AI 分析

- **双模式推理**：本地 PyTorch 推理 + 云端 API 调用
- **分析对话框**：ROI 选择、推理参数配置、后台缩略图加载
- **结果浏览器**：模态图像浏览器，支持交互式阈值调整、评分表格、标签页导航
- **GPU 支持**：自动检测 CUDA 可用设备

### ImageJ/Fiji 桥接

通过 PyImageJ 实现与 ImageJ2/Fiji 的深度集成：

- **numpy ↔ ImageJ2** 双向无损转换（RGB / 灰度 / 多通道）
- **GUI 可视化调参**：加载样本到 Fiji，执行操作后自动捕获参数
- **参数配置存储/加载**：JSON 数值 + 宏文本双模式
- **无头批处理**：复现调参阶段的所有 ImageJ 操作
- **批量测量导出**：结果自动导出为 CSV

适用场景：IHC 染色、HE 染色、荧光染色、多通道图像 — 只需修改处理步骤配置。

### ROI 预览

- **缩略图网格**：自定义 `CheckIndicator` 复选框（无边框圆形设计）
- **复选框批量选择**：支持全选/反选，用于选择性导出
- **双击全分辨率预览**：查看导出前的实际裁剪效果
- **嵌入面板**：可嵌入主窗口右侧的 `ROIPreviewPanel`

### 批量导出

- 缩略图坐标 → 全分辨率坐标映射 → 居中裁剪 → TIFF 输出（zlib 压缩）
- `QThread` 后台运行，不阻塞 UI
- 进度条实时显示，支持中途中断
- 复用已打开的 reader 对象，避免 DLL 重复打开死锁
- 支持从预览对话框选择性导出

### UI 主题

- macOS Big Sur 风格深色主题（`theme.qss`），强调色 Apple Blue `#007AFF`
- 浅色主题（`theme_light.qss`）可一键切换
- 半透明毛玻璃面板效果
- 完整的滚动条、按钮、菜单、列表、对话框、进度条样式
- 自定义 SVG 应用图标，多分辨率渲染

---

## 技术栈

| 组件 | 版本 | 用途 |
|------|------|------|
| Python | ≥ 3.10 | 运行环境 |
| PySide6 | ≥ 6.6, < 7 | Qt6 GUI 框架 |
| sdpc-for-python | ≥ 1.0 | SDPC 格式解析（ctypes 调用 DecodeSdpcDll.dll） |
| numpy | ≥ 1.24 | 图像数据处理 |
| Pillow | ≥ 10, < 11 | 图像格式转换 |
| tifffile | ≥ 2024 | BigTIFF 写入（zlib 压缩） |
| scipy | ≥ 1.10 | 形态学运算、峰值检测 |
| scikit-image | ≥ 0.21 | 图像 resize、连通域分析 |
| scikit-learn | ≥ 1.3 | 辅助计算 |
| opencv-python | — | 连通域标记、图像缩放 |
| torch | ≥ 2.0 | DeepLIIF 深度学习推理 |
| torchvision | ≥ 0.15 | 图像预处理 |
| deepliif | ≥ 1.2.6 | DeepLIIF 模型推理引擎 |

---

## 项目结构

```
pathology-crop-tool/
├── main.py                          # 入口点，QApplication + 日志配置 + QSS 加载
├── pyproject.toml                   # 项目元数据与依赖
├── build.spec                       # PyInstaller 打包配置
├── icon.ico / icon.svg              # 应用图标
├── README.md
│
├── liver_portal_crop/
│   ├── __init__.py
│   ├── __main__.py                  # python -m 入口
│   ├── app.py                       # MainWindow — 主窗口布局 + 信号连接 + 交互编排
│   ├── canvas.py                    # WSICanvas — 金字塔 WSI 渲染 + ROI 标注 + 缩放手柄
│   ├── reader.py                    # SDPCReader — ctypes DLL 封装，金字塔读取
│   ├── roi.py                       # ROIModel + ROIManager — ROI 数据模型与管理器
│   ├── exporter.py                  # BatchExporter — QThread 后台批量导出 TIFF
│   ├── tissue_detect.py             # 组织检测算法（HistoKit Otsu）+ TissueDialog
│   ├── ihc_hotspot.py               # IHC 阳性热点检测 — 色彩反卷积 + 密度图 + Top-N
│   ├── deepliif_runner.py           # DeepLIIF 推理引擎（本地 + 云端双模式）
│   ├── analysis_dialog.py           # DeepLIIF 分析参数对话框
│   ├── results_viewer.py            # DeepLIIF 结果浏览器
│   ├── preview_dialog.py            # ROI 预览对话框 — 缩略图网格 + 选择性导出
│   ├── imagej_bridge.py             # ImageJ/Fiji 桥接 — PyImageJ 集成
│   ├── utils.py                     # 坐标映射工具函数
│   ├── navigator.py                 # NavigationWidget — 缩略图导航
│   ├── dialogs.py                   # SettingsDialog — 导出设置
│   ├── constants.py                 # 全局常量（tile 尺寸、缓存上限等）
│   ├── theme.py                     # QSS 主题加载器
│   ├── theme.qss                    # macOS Big Sur 深色主题
│   ├── theme_light.qss              # 浅色主题
│   │
│   └── controllers/
│       ├── __init__.py
│       ├── base.py                  # BaseController — 控制器基类
│       ├── file_controller.py       # FileController — 文件管理
│       ├── roi_controller.py        # ROIController — ROI 生命周期管理
│       ├── preset_controller.py     # PresetController — 预设保存/加载
│       └── export_controller.py     # ExportController — 导出编排
│
└── tests/
    ├── __init__.py
    ├── test_roi.py                  # ROIModel + ROIManager 测试
    ├── test_utils.py                # 坐标映射测试
    ├── test_exporter.py             # 导出功能测试
    └── test_ihc_hotspot.py          # IHC 热点检测算法完整测试
```

---

## 控制器架构

主窗口（`app.py`）采用 **Controller 模式** 将逻辑拆分到独立的控制器中：

```
BaseController (base.py)
  ├── app / canvas / roi_manager / readers / current_slide  属性
  │
  ├── FileController          文件增删切换、导航缩略图更新、过期 ROI 清理
  ├── ROIController           ROI 创建/选中/编辑/删除、倍率自动计算、列表管理
  ├── PresetController        预设保存/加载/应用 (~/.liver_portal_crop/presets.json)
  └── ExportController        导出设置、预览对话框、线程化导出、进度/取消
```

各控制器持有 `MainWindow` 引用，通过它操作 UI 状态。`MainWindow` 作为中央协调器，将领域逻辑委托给控制器。

---

## 安装

### 环境要求

- Windows 10/11（SDPC DLL 仅支持 Windows）
- Python ≥ 3.10

### 依赖安装

```bash
pip install -r requirements.txt
```

如果 `requirements.txt` 不存在，可通过 `pyproject.toml` 安装：

```bash
pip install -e .
```

### 注意事项

- 需要 `DecodeSdpcDll.dll`（随 `sdpc-for-python` 包安装，位于 `sdpc/WINDOWS/dll/`）
- 系统需安装 [Visual C++ Redistributable](https://aka.ms/vs/17/release/vc_redist.x64.exe)
- 文件路径建议避免中文字符（DLL 路径兼容性限制）

---

## 运行

```bash
python main.py
```

### 使用方法

1. **文件 → 添加文件...** 或直接拖入 SDPC 文件
2. 在左侧文件列表中选择切片，中央画布显示缩略图
3. **滚轮缩放** — 浏览切片各金字塔层级
4. **点击"ROI 绘制"** — 进入 ROI 模式，绿色虚线框跟随鼠标
5. **按空格键** — 在当前框位置创建 ROI
6. **点击 ROI** — 选中，显示缩放手柄（白色圆点）
7. **拖拽手柄** — 缩放 ROI；**拖拽主体** — 移动位置
8. **右侧面板修改 X/Y/W/H** — 精确调整选中 ROI
9. **组织检测 (HistoKit)** — 自动检测组织区域并生成 ROI
10. **IHC 热点检测** — 自动检测免疫组化阳性热点并生成 ROI
11. **批量导出** — 设置输出目录和裁剪尺寸，导出为 TIFF

### 快捷键

| 快捷键 | 功能 |
|--------|------|
| `Space` | 创建 ROI / 切换 ROI 模式 |
| `Delete` / `Backspace` | 删除选中的 ROI |
| 滚轮 | 缩放 WSI 视图 |
| 左键拖拽 | 平移浏览（默认）/ 拖拽 ROI（选中后） |
| 右键拖拽 | 旋转视图角度 |

---

## 关键流程

### ROI 生成 → 导出

```
用户操作                        →   代码路径
──────────────────────────────────────────────────────────
打开 SDPC 文件                  →   app._add_files → SDPCReader
浏览切片                        →   WSICanvas.load_slide → 金字塔 tile 渲染
切换 ROI 模式                   →   set_roi_mode(True) → 浮动框
空格创建 ROI                    →   _place_roi_at_frame → ROIController → ROIManager
组织检测自动生成                →   TissueDialog → detect_tissue → ROIManager
IHC 热点检测自动生成            →   IHCHotspotDialog → detect_ihc_hotspots_tiled → 回调添加 ROI
选中 ROI + 拖拽/缩放            →   ROIRectItem.itemChange → _on_roi_rect_changed
预览对话框选择性导出            →   ROIPreviewDialog → ExportController.run_export
批量导出 TIFF                   →   BatchExporter → QThread → SDPCReader.extract_region → tifffile.imwrite
```

### IHC 热点检测流程

```
IHCHotspotDialog（参数配置）
  → detect_ihc_hotspots_tiled（QThread 后台）
    → Phase 0: _get_tissue_tile_set     组织区域 tile 集合 + 缩略图掩膜
    → Phase 1: 逐 tile 读取 + 色彩反卷积（OD LUT + DAB 通道提取）
    → Phase 2: 阳性阈值判定 + 掩膜累积到全局降采样掩膜
    → Phase 3: compute_density_map       滑动窗口密度图
             → find_hotspots             Top-N 峰值检测
    → Phase 4: _filter_hotspots_by_tissue_coverage  组织覆盖率过滤 (≥ 50%)
  → 回调：生成 ROI 添加到画布
```

### 金字塔渲染

```
_resizeEvent / _mouseRelease
  → _render_timer (200ms debounce)
  → _render_visible_tiles
    → _get_best_level（根据缩放比选金字塔层级）
    → 分 tile (1024×1024) 读取
    → LRU Cache → QGraphicsPixmapItem 放置场景
```

---

## 模块职责

| 模块 | 职责 | 可独立测试 |
|------|------|-----------|
| `reader.py` | ctypes 调用 DLL 读取 SDPC 金字塔 | 需要 DLL |
| `canvas.py` | WSI 渲染 + ROI 标注 + 缩放手柄 | 需要 Qt |
| `app.py` | 主窗口布局、信号连接、交互编排 | 需要 Qt |
| `roi.py` | ROI 数据模型 + 管理器 + JSON 序列化 | 可测试 |
| `exporter.py` | 批量导出 TIFF（QThread 后台） | 可测试 |
| `tissue_detect.py` | HistoKit Otsu 组织检测 + TissueDialog | 可测试 |
| `ihc_hotspot.py` | IHC 热点检测算法 + IHCHotspotDialog | 可测试（mock DLL） |
| `deepliif_runner.py` | DeepLIIF 推理（本地 + 云端） | 需要模型 |
| `analysis_dialog.py` | DeepLIIF 分析参数对话框 | 需要 Qt |
| `results_viewer.py` | DeepLIIF 结果浏览器 | 需要 Qt |
| `preview_dialog.py` | ROI 预览网格 + 选择性导出 | 需要 Qt |
| `imagej_bridge.py` | ImageJ/Fiji 桥接（PyImageJ） | 需要 JVM |
| `utils.py` | 坐标映射工具函数 | 可测试 |
| `navigator.py` | 导航缩略图 | 需要 Qt |
| `constants.py` | 全局常量 | 可测试 |

---

## 测试

```bash
# 运行基础单元测试
pytest tests/test_roi.py tests/test_utils.py tests/test_exporter.py

# 运行 IHC 热点检测算法测试（mock DLL，不需要实际 SDPC 文件）
python tests/test_ihc_hotspot.py
```

IHC 测试覆盖：色彩反卷积正确性、阈值判定、密度图计算、Top-N 热点提取、分块处理流水线、组织覆盖率过滤、染色矩阵标签。

---

## 打包

```bash
pip install pyinstaller
pyinstaller build.spec
```

输出：`dist/病理裁剪工具.exe`（单文件，~200 MB）

`build.spec` 已配置：
- 自动搜索并打包 `DecodeSdpcDll.dll`
- 包含主题文件、图标、箭头图片等资源
- `hiddenimports` 覆盖 sdpc、PySide6、PIL、tifffile 等
- `console=False` 无控制台窗口

### 日志

打包后的 exe 运行日志写入用户主目录：`~/pathology-crop-tool.log`。如遇问题可查看该文件诊断。

---

## 许可

本项目基于 MIT 许可证开源。
