<div align="center">

<img src="icon.svg" width="96" height="96" alt="Logo" />

# 病理裁剪工具

**Pathology Crop Tool**

SDPC 全切片病理图像浏览 · ROI 标注 · IHC 热点检测 · AI 分析 · 批量裁剪导出

[![Release](https://img.shields.io/github/v/release/Funvvell/pathology-crop-tool?style=flat-square&label=Latest)](https://github.com/Funvvell/pathology-crop-tool/releases)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?style=flat-square&logo=python&logoColor=white)](pyproject.toml)
[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows-0078D4?style=flat-square&logo=windows)](#)

[快速开始](#-快速开始) · [功能特性](#-功能特性) · [架构设计](#-架构设计) · [开发指南](#-开发指南)

</div>

---

## ✨ 功能特性

<table>
<tr>
<td width="50%" valign="top">

### 🔬 WSI 金字塔浏览

- 基于 QGraphicsView 的金字塔渲染引擎
- 支持超大切片（120,000 × 80,000+ px）
- LRU Tile 缓存（512 张），按需加载
- 导航缩略图实时定位 + 点击跳转
- 滚轮自动切换金字塔层级

</td>
<td width="50%" valign="top">

### ✏️ ROI 标注与管理

- 浮动框跟随鼠标，空格键快速创建
- 八方向缩放手柄拖拽调整
- 倍率 / 视野比例自动计算像素尺寸
- 右侧面板实时同步坐标，精确编辑
- ROI 角度旋转支持

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 🧬 组织区域检测

- HistoKit 三通道 Otsu 阈值算法
- 形态学处理：开/闭运算、孔洞填充、碎片移除
- **网格模式** — 组织区域内均匀生成 ROI
- **连通域模式** — 按区域大小排序生成
- 实时参数预览

</td>
<td width="50%" valign="top">

### 🎯 IHC 阳性热点检测

- Ruifrok & Johnston 色彩反卷积（H-DAB / H-AEC / H-E）
- OD 查找表预计算加速
- 分块 tile 读取，峰值内存 ~100 MB
- 滑动窗口密度图 + Top-N 峰值提取
- 组织覆盖率 ≥ 50% 自动过滤

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 🤖 DeepLIIF AI 分析

- 本地 PyTorch + 云端 API 双模式推理
- GPU (CUDA) 自动检测与加速
- 交互式阈值调整 + 评分表格
- ROI 选择与参数配置对话框

</td>
<td width="50%" valign="top">

### 🔗 ImageJ / Fiji 桥接

- numpy ↔ ImageJ2 双向无损转换
- GUI 可视化调参 + 参数自动捕获
- 无头批处理复现操作流程
- 测量结果批量导出 CSV

</td>
</tr>
</table>

### 📦 批量导出

- 全分辨率坐标映射 → 居中裁剪 → TIFF（zlib 压缩）
- QThread 后台运行，实时进度条，支持中断
- 复用已有 reader 对象，避免 DLL 死锁
- ROI 预览对话框 — 缩略图网格 + 选择性导出

### 🎨 专业 UI

- macOS Big Sur 风格深色 / 浅色主题一键切换
- Apple Blue `#007AFF` 强调色，毛玻璃面板
- PyInstaller 单文件打包（~200 MB）
- 自定义 SVG 图标，多分辨率渲染

---

## 🚀 快速开始

### 环境要求

| 项目 | 要求 |
|------|------|
| 操作系统 | Windows 10 / 11 |
| Python | ≥ 3.10 |
| VC++ Runtime | [下载](https://aka.ms/vs/17/release/vc_redist.x64.exe) |

### 安装与运行

```bash
# 克隆项目
git clone https://github.com/Funvvell/pathology-crop-tool.git
cd pathology-crop-tool

# 安装依赖
pip install -e .

# 启动
python main.py
```

### 打包为独立 exe

```bash
pip install pyinstaller
pyinstaller build.spec
# → dist/病理裁剪工具.exe
```

> [!TIP]
> 也可以直接从 [Releases](https://github.com/Funvvell/pathology-crop-tool/releases) 下载预编译的 exe 文件。

### 基本操作

```
打开 SDPC 文件 → 滚轮缩放浏览 → 空格键创建 ROI → 批量导出 TIFF
```

| 快捷键 | 功能 |
|:------:|------|
| `Space` | 创建 ROI / 切换 ROI 模式 |
| `Delete` | 删除选中的 ROI |
| 滚轮 | 缩放 WSI 视图 |
| 左键拖拽 | 平移浏览 / 拖拽 ROI |
| 右键拖拽 | 旋转视图角度 |

---

## 🏗️ 架构设计

### 项目结构

```
pathology-crop-tool/
├── main.py                            # 入口点 · 日志 · 主题加载
├── build.spec                         # PyInstaller 打包配置
├── pyproject.toml                     # 元数据与依赖
│
├── liver_portal_crop/
│   ├── app.py                         # MainWindow — 主窗口编排
│   ├── canvas.py                      # WSICanvas — 金字塔渲染 + ROI
│   ├── reader.py                      # SDPCReader — ctypes DLL 封装
│   ├── roi.py                         # ROIModel + ROIManager
│   ├── exporter.py                    # BatchExporter — 批量 TIFF 导出
│   ├── tissue_detect.py               # HistoKit Otsu 组织检测
│   ├── ihc_hotspot.py                 # IHC 热点检测算法 + UI
│   ├── deepliif_runner.py             # DeepLIIF 推理引擎
│   ├── analysis_dialog.py             # DeepLIIF 参数对话框
│   ├── results_viewer.py              # DeepLIIF 结果浏览器
│   ├── preview_dialog.py              # ROI 预览 · 选择性导出
│   ├── imagej_bridge.py               # ImageJ/Fiji 桥接
│   ├── navigator.py                   # 缩略图导航
│   ├── utils.py                       # 坐标映射工具
│   ├── constants.py                   # 全局常量
│   ├── theme.py / theme.qss           # 主题系统
│   │
│   └── controllers/
│       ├── base.py                    # BaseController 基类
│       ├── file_controller.py         # 文件管理
│       ├── roi_controller.py          # ROI 生命周期
│       ├── preset_controller.py       # 预设存储
│       └── export_controller.py       # 导出编排
│
└── tests/
    ├── test_roi.py                    # ROI 数据模型测试
    ├── test_utils.py                  # 坐标映射测试
    ├── test_exporter.py               # 导出功能测试
    └── test_ihc_hotspot.py            # IHC 算法完整测试
```

### 控制器架构

主窗口采用 **Controller 模式**，将领域逻辑委托给独立控制器：

```
                    ┌─────────────┐
                    │  MainWindow │  中央协调器
                    │   (app.py)  │
                    └──────┬──────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
    ┌─────────▼──┐  ┌─────▼──────┐  ┌──▼───────────┐
    │     File   │  │    ROI     │  │   Export     │
    │ Controller │  │ Controller │  │  Controller  │
    └────────────┘  └────────────┘  └──────────────┘
                           │
                  ┌────────▼────────┐
                  │    Preset       │
                  │   Controller    │
                  └─────────────────┘
```

各控制器共享 `app` / `canvas` / `roi_manager` / `readers` 等属性，实现松耦合协作。

### 关键流程

<details>
<summary><strong>ROI 生成 → 导出</strong></summary>

```
用户操作                          代码路径
──────────────────────────────────────────────────────────────
打开 SDPC 文件        →   app._add_files → SDPCReader
浏览切片              →   WSICanvas.load_slide → 金字塔 tile 渲染
切换 ROI 模式         →   set_roi_mode(True) → 浮动框
空格创建 ROI          →   _place_roi_at_frame → ROIController → ROIManager
组织检测自动生成      →   TissueDialog → detect_tissue → ROIManager
IHC 热点检测          →   IHCHotspotDialog → detect_ihc_hotspots_tiled
预览选择性导出        →   ROIPreviewDialog → ExportController.run_export
批量导出 TIFF         →   BatchExporter → QThread → tifffile.imwrite
```

</details>

<details>
<summary><strong>IHC 热点检测流水线</strong></summary>

```
IHCHotspotDialog（参数配置）
  │
  ▼ detect_ihc_hotspots_tiled（QThread 后台）
  │
  ├─ Phase 0  _get_tissue_tile_set
  │           组织区域 tile 集合 + 缩略图掩膜
  │
  ├─ Phase 1  Tile-by-tile 色彩反卷积
  │           OD LUT 预计算 → DAB 通道提取 → 阳性掩膜
  │
  ├─ Phase 2  阳性信号累积
  │           逐 tile 阳性像素 → 全局降采样掩膜（~17 MB）
  │
  ├─ Phase 3  compute_density_map + find_hotspots
  │           滑动窗口密度图 → Top-N 峰值检测
  │
  └─ Phase 4  _filter_hotspots_by_tissue_coverage
              组织覆盖率 ≥ 50% → 生成 ROI
```

</details>

<details>
<summary><strong>金字塔渲染</strong></summary>

```
_resizeEvent / _mouseRelease
  │
  ▼ _render_timer (200ms debounce)
  │
  ▼ _render_visible_tiles
    ├─ _get_best_level  →  根据缩放比选金字塔层级
    ├─ 分 tile (1024×1024) 读取
    └─ LRU Cache  →  QGraphicsPixmapItem 放置场景
```

</details>

---

## 📊 技术栈

| 组件 | 版本 | 用途 |
|------|:----:|------|
| **PySide6** | ≥ 6.6 | Qt6 GUI 框架 |
| **sdpc-for-python** | ≥ 1.0 | SDPC 格式解析（ctypes → DecodeSdpcDll.dll） |
| **numpy** | ≥ 1.24 | 图像数据处理 |
| **scipy** | ≥ 1.10 | 形态学运算 · 峰值检测 |
| **scikit-image** | ≥ 0.21 | 图像 resize · 连通域分析 |
| **tifffile** | ≥ 2024 | BigTIFF 写入（zlib） |
| **Pillow** | ≥ 10 | 图像格式转换 |
| **opencv-python** | — | 连通域标记 · 图像缩放 |
| **torch** | ≥ 2.0 | DeepLIIF 深度学习推理 |
| **deepliif** | ≥ 1.2.6 | DeepLIIF 模型推理引擎 |

---

## 🧪 开发指南

### 运行测试

```bash
# 基础单元测试
pytest tests/test_roi.py tests/test_utils.py tests/test_exporter.py

# IHC 热点检测算法测试（mock DLL，无需实际 SDPC 文件）
python tests/test_ihc_hotspot.py
```

IHC 测试覆盖：色彩反卷积正确性 · 阈值判定 · 密度图计算 · Top-N 热点提取 · 分块处理流水线 · 组织覆盖率过滤 · 染色矩阵标签。

### 模块可测试性

| 模块 | 职责 | 独立测试 |
|------|------|:--------:|
| `reader.py` | ctypes DLL 读取 SDPC 金字塔 | ⚠️ 需 DLL |
| `canvas.py` | WSI 渲染 + ROI 标注 | ⚠️ 需 Qt |
| `app.py` | 主窗口编排 | ⚠️ 需 Qt |
| `roi.py` | ROI 数据模型 + JSON 序列化 | ✅ |
| `exporter.py` | 批量 TIFF 导出 | ✅ |
| `tissue_detect.py` | HistoKit Otsu 组织检测 | ✅ |
| `ihc_hotspot.py` | IHC 热点检测算法 | ✅ mock DLL |
| `deepliif_runner.py` | DeepLIIF 推理 | ⚠️ 需模型 |
| `imagej_bridge.py` | ImageJ/Fiji 桥接 | ⚠️ 需 JVM |
| `utils.py` | 坐标映射 | ✅ |

### 日志

打包后的 exe 运行日志写入用户主目录：

```
%USERPROFILE%\pathology-crop-tool.log
```

如遇问题可查看该文件进行诊断。

---

## 📄 许可

本项目基于 [MIT License](LICENSE) 开源。

---

<div align="center">

**如果这个项目对你有帮助，请给一个 ⭐ Star！**

[Report Bug](https://github.com/Funvvell/pathology-crop-tool/issues) · [Request Feature](https://github.com/Funvvell/pathology-crop-tool/issues) · [Download Latest](https://github.com/Funvvell/pathology-crop-tool/releases)

</div>
