# 病理裁剪工具

SDPC 全景病理切片查看与汇管区批量裁剪桌面工具。支持生强（ShengQiang）SDPC 格式的病理全玻片图像（WSI）浏览、ROI 标注、组织自动检测、批量裁剪为 TIFF。

---

## 功能

### 📂 文件管理
- 拖入/打开多张 SDPC 文件，列表切换显示
- 自动读取金字塔层级元数据（层级数、每级宽高、下采样比）

### 🔍 WSI 金字塔浏览
- 基于 `QGraphicsView` 的金字塔渲染引擎
- 缩略图全图预览 → 滚轮缩放自动切换层级（`_get_best_level`）
- LRU Tile 缓存（512 张），按需加载可见区域 tiles，避免 OOM
- 平移拖拽浏览
- 导航缩略图（`NavigationWidget`）显示当前视口在全图中的位置，点击跳转

### ✏️ ROI 标注
- **浮动框标注**：在 ROI 模式下，固定尺寸绿色虚线框跟随鼠标，按空格键快速创建 ROI
- **缩放手柄**：选中 ROI 后在四角四边显示白色圆点手柄，拖拽即可缩放（`ResizeHandle`）
- **拖动位置**：选中后直接拖拽 ROI 主体移动位置
- **选中外观**：未选中蓝色边框，选中橙色边框 + 显示手柄
- **实时坐标同步**：右侧面板 X/Y/W/H 输入框实时显示选中 ROI 坐标，修改即更新

### 🧬 组织检测
- 基于 **HistoKit** 三通道 Otsu 阈值的自动组织区域识别（`tissue_detect.py`）
- RGB 三通道直方图 → 两步 Otsu 阈值 → 三通道组合判定 → 形态学处理（开/闭运算、填充孔洞、移除小碎片）
- **网格模式**：在组织区域内均匀生成 ROI
- **连通域模式**：按连通区域大小排序生成 ROI
- 实时预览 + 参数调节（开运算半径、闭运算半径、填充、碎片过滤）

### 📤 批量导出
- 缩略图坐标 → 全分辨率坐标映射 → 居中裁剪 → TIFF 输出（`exporter.py`）
- `QThread` 后台运行，不阻塞 UI
- 进度条实时显示
- 支持中途中断

### 🎨 UI 主题
- 深色专业主题（`theme.qss`）
- 蓝靛强调色（`#4c9aff`），医疗级视觉风格
- 完整的滚动条、按钮、菜单、列表、对话框、进度条样式
- 支持 PyInstaller 打包为单文件 `.exe`

---

## 技术栈

| 组件 | 版本 | 用途 |
|------|------|------|
| Python | ≥ 3.10 | 运行环境 |
| PySide6 | ≥ 6.6, < 7 | Qt6 GUI 框架 |
| sdpc-for-python | ≥ 1.0 | SDPC 格式解析（通过 ctypes 调用 DecodeSdpcDll.dll） |
| numpy | ≥ 1.24 | 图像数据处理 |
| Pillow | ≥ 10 | 图像格式转换 |
| tifffile | ≥ 2024 | BigTIFF 写入 |
| scipy | ≥ 1.10 | 形态学运算 |
| scikit-image | ≥ 0.21 | 图像 resize、连通域分析 |
| scikit-learn | ≥ 1.3 | 辅助计算 |
| opencv-python | — | 连通域标记（`cv2.connectedComponentsWithStats`） |

---

## 架构

```
LiverPortalCrop/
├── main.py                      # 入口点，QApplication 启动 + QSS 加载
├── pyproject.toml               # 项目元数据
├── requirements.txt             # 依赖清单
├── build.spec                   # PyInstaller 打包配置
├── icon.ico                     # 应用图标
├── README.md
│
├── liver_portal_crop/
│   ├── __init__.py
│   ├── app.py                   # MainWindow — 主窗口布局 + 信号连接 + 全部交互逻辑
│   ├── canvas.py                # WSICanvas — 金字塔 WSI 渲染 + ROI 标注 + 缩放手柄
│   ├── reader.py                # SDPCReader — ctypes DLL 封装，金字塔读取
│   ├── roi.py                   # ROIModel + ROIManager — ROI 数据模型与管理器
│   ├── exporter.py              # BatchExporter — QThread 后台批量导出 TIFF
│   ├── tissue_detect.py         # 组织检测算法（HistoKit Otsu）+ TissueDialog
│   ├── utils.py                 # 坐标映射工具函数
│   ├── navigator.py             # NavigationWidget — 缩略图导航
│   ├── dialogs.py               # SettingsDialog — 导出设置
│   ├── theme.qss                # 深色专业主题样式表
│   ├── arrow_up.png             # QSS 引用箭头图标
│   └── arrow_down.png
│
└── tests/
    ├── __init__.py
    ├── test_utils.py            # 坐标映射测试
    ├── test_roi.py              # ROIManager 测试
    └── test_exporter.py         # 导出功能测试
```

## 关键路径

### ROI 生成 → 导出 流程

```
用户操作                      →     代码路径
──────────────────────────────────────────────────
打开 SDPC 文件                → app._add_files → SDPCReader
浏览切片                      → WSICanvas.load_slide → 金字塔 tile 渲染
切换 ROI 模式                 → set_roi_mode(True) → 浮动框
空格创建 ROI                  → _place_roi_at_frame → add_roi_rect → ROIManager
组织检测自动生成              → TissueDialog → detect_tissue → ROIManager
选中 ROI + 拖拽/缩放          → ROIRectItem.itemChange → _on_roi_rect_changed → ROIModel 更新
批量导出 TIFF                 → BatchExporter → QThread → tifffile.imwrite
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

## 安装

```bash
pip install -r requirements.txt
```

### 注意事项

- 需要 `DecodeSdpcDll.dll`（随 sdpc-for-python 包安装，位于 `sdpc/WINDOWS/dll/`）
- 路径不能包含中文字符？建议放在纯英文路径下

---

## 运行

```bash
python main.py
```

### 使用方法

1. **文件 → 添加文件...** 加载 SDPC 切片
2. 在文件列表中选择切片，中央画布显示缩略图
3. **滚轮缩放** — 浏览切片各层级
4. **点击"ROI 绘制"** — 进入 ROI 模式，绿色虚线框跟随鼠标
5. **按空格键** — 在当前框位置创建 ROI
6. **点击 ROI** — 选中，显示缩放手柄（白色圆点）
7. **拖拽手柄** — 缩放 ROI；**拖拽主体** — 移动位置
8. **右侧面板修改 X/Y/W/H** — 精确调整选中 ROI
9. **组织检测 (HistoKit)** — 自动检测组织区域生成 ROI
10. **批量导出** — 设置输出目录，导出为 TIFF

### 快捷键

| 快捷键 | 功能 |
|--------|------|
| 空格 | 创建 ROI / 切换 ROI 模式 |
| Delete / Backspace | 删除选中的 ROI |
| 滚轮 | 缩放 WSI 视图 |
| 左键拖拽 | 平移浏览（默认） / 拖拽 ROI（选中后） |

---

## 打包

```bash
pip install pyinstaller
pyinstaller build.spec
# dist/病理裁剪工具.exe
```

打包后需确保 `DecodeSdpcDll.dll` 在 `sdpc/WINDOWS/dll/` 目录下，`build.spec` 已配置自动搜索和打包。

---

## 模块职责

| 模块 | 职责 | 可独立测试 |
|------|------|-----------|
| `reader.py` | 通过 ctypes 调用 DLL 读取 SDPC 金字塔 | ❌ 需要 DLL |
| `canvas.py` | WSI 渲染 + ROI 标注 + 缩放手柄 | ❌ 需要 Qt |
| `app.py` | 主窗口布局、信号连接、交互逻辑 | ❌ 需要 Qt |
| `roi.py` | ROI 数据模型 + 管理器 + JSON 序列化 | ✅ |
| `exporter.py` | 批量导出 TIFF | ✅ |
| `utils.py` | 坐标映射工具函数 | ✅ |
| `tissue_detect.py` | 组织检测算法 + 参数对话框 | ✅ |
| `navigator.py` | 导航缩略图 | ❌ 需要 Qt |

---

## 许可

本项目基于 MIT 许可证开源。
