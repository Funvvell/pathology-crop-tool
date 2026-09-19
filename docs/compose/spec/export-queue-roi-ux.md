---
feature: export-queue-roi-ux
status: in-progress
updated: 2026-06-16
branch: feat/export-queue-roi-ux
commits: f905381..<head>
---

# Export Queue + ROI Annotation UX

## Report

## [S1] Problem

当前病理裁剪工具已具备 WSI 浏览、ROI 标注、组织/IHC 检测与 TIFF 导出，但批量导出与日常标注体验存在明显缺口：

1. **导出是“一键全量”**：`ExportController.start_export` 确认后对所有 ROI 串行导出，没有多文件任务队列、逐文件状态、失败重试或结果汇总；预览对话框也只能选中后一次性导出。
2. **标注操作不可撤销**：ROI 创建、拖动缩放、删除、清空均无 undo/redo，误删/误拖成本高。
3. **快捷键与导航不足**：仅有 Space / Delete；列表与画布多选、复制、跳转上下一个 ROI/文件、适配视野等高频操作缺失或不可用。
4. **精确编辑与信息反馈弱**：列表只显示坐标文本，不能双击改坐标；选中后缺少常驻的 ROI 详情信息。
5. **main 上有未提交基线改动**（QImage 安全、ImageJ 日志、可选 AI 依赖、package-data 等）需要纳入本次交付并保持可测。
6. 触点路径上仍有代码质量问题：`file_controller` 缩略图 `QImage` 未 `.copy()`（与 canvas 修过的同类悬垂风险）、`preset_controller` 静默 `except Exception: pass`。

## [S2] Design

### S2.1 Baseline（未提交改动作为交付基线）

Worktree `feat/export-queue-roi-ux` 已从 `main@f905381` 拉出，并带上下列基线（与 main 工作区一致）：

| 区域 | 行为 |
|------|------|
| `canvas.py` | 缩略图用 `QImage(region.data,...).copy()`，避免 `tobytes` 拷贝与悬垂引用 |
| `imagej_bridge.py` | Fiji 引导信息改 `logger.info`，打包无控制台时仍可见 |
| `pyproject.toml` | `build-backend=setuptools.build_meta`；`numpy`/`Pillow` 上界放宽；`package-data` 包含 `*.qss`/`*.png` |
| `requirements*.txt` | 核心依赖与 AI 可选依赖拆分 |
| `run.bat` / `setup_venv.bat` / `LICENSE` | 安装与许可文件 |

同批代码质量修复（触点路径）：

- `file_controller._update_nav_thumb`：与 canvas 一致，`QImage(...).copy()` 后再 `QPixmap.fromImage`。
- `preset_controller.load_presets` / `save_preset`：失败时 `logger.warning(..., exc_info=True)`，不再静默吞异常。

### S2.2 导出队列（仅导出，GUI 内增强）

**任务模型** `ExportQueueJob`（新模块 `export_queue.py`）：

```python
@dataclass
class ExportQueueJob:
    id: str
    slide_path: Path
    roi_ids: list[str]          # 该文件下待导出 ROI
    status: JobStatus           # pending | running | success | failed | cancelled
    done: int = 0
    total: int = 0
    error: str = ""
    # 入队时快照的裁剪配置，避免运行中被 UI 改动
    crop_width: int
    crop_height: int
    mag_label: str
    output_dir: Path
    compression: str = "zlib"
```

**队列控制器** `ExportQueueController`（可挂在 `ExportController` 旁或作为其扩展）：

| 操作 | 行为 |
|------|------|
| `enqueue_slides(paths, roi_manager)` | 按切片文件分组 ROI；无 ROI 的文件跳过并提示 |
| `enqueue_all_rois()` | 当前 `cleanup_stale_rois` 后按文件入队全部 ROI |
| `start()` | 串行执行：对每个 pending job 复用/打开 reader，调用现有 `BatchExporter` 逻辑导出该 job 的 ROI |
| `cancel()` | 当前 job 协作取消；剩余 pending → `cancelled` |
| `retry_failed()` | 仅将 `failed` 重置为 `pending` 并再次 `start()` |
| 信号 | `job_updated(job_id)`、`queue_finished(summary)`、`job_progress(job_id, done, total)` |

**行为契约**：

- 单 job 失败不中断后续 job。
- 输出文件命名保持：`{slide.stem}_ROI_{local:04d}{_mag}.tiff`（局部索引为该文件内 ROI 顺序）。
- 已打开的 `readers[path]` 优先复用；否则 job 内打开，结束后不强制关闭 GUI reader。
- 结束汇总：`ok_jobs / failed_jobs / cancelled_jobs / ok_rois / failed_rois / output_dir`。
- 现有「批量导出」「预览导出」按钮继续可用；「批量导出」可走队列（按文件拆 job）或保持一键全量——**交付时二者都支持**：一键全量复用旧路径；新按钮「导出队列」打开队列面板。

**队列 UI**（`ExportQueueDialog` 或侧栏）：

- 表格列：文件名、ROI 数、状态、进度（done/total）、错误摘要、操作（重试）。
- 按钮：开始 / 取消 / 重试失败项 / 打开输出目录。
- 运行中禁用危险操作；结束后弹汇总（可关闭后仅状态栏提示，避免与旧路径双重弹窗干扰——队列路径用汇总对话框，旧路径保持原完成框）。

### S2.3 ROI 标注体验

#### Undo / Redo

新模块 `roi_undo.py`，基于命令/逆操作栈（非整表深拷贝优先，大 ROI 数时更省内存）：

| 命令 | 逆操作 |
|------|--------|
| `add_roi(roi)` | `remove_roi(id)` |
| `remove_roi(roi_snapshot)` | `add_roi` 还原 |
| `remove_batch(list[snapshot])` | 逐个恢复为一次 redo 配对 |
| `update_roi(id, old, new)` | 回写 old 几何/角度 |
| `clear_slide(path, snapshots)` | 批量恢复 |

- 栈深度 50；快捷键 `Ctrl+Z` / `Ctrl+Y`（及 `Ctrl+Shift+Z`）。
- 拖动缩放在 **编辑结束**（mouse release）时压栈一条 `update_roi`，避免每个像素变化一条。
- 清空全部 ROI 作为一条 undo 项（快照）。
- 画布/列表/导出/删除统一通过 `ROIManager` 变更入口打点，保证与栈一致。

#### 快捷键

| 快捷键 | 行为 |
|--------|------|
| `Space` | ROI 模式下放置；浏览模式切换 ROI 模式（现有） |
| `Delete` / `Backspace` | 删除画布选中 ROI（多选则批量） |
| `Ctrl+Z` / `Ctrl+Y` / `Ctrl+Shift+Z` | 撤销 / 重做 |
| `Ctrl+C` | 复制选中 ROI（默认偏移 `+frame_w/2, +frame_h/2`，落在视野附近） |
| `Ctrl+D` | 复制选中 ROI（与 Ctrl+C 同逻辑，菜单“复制 ROI”） |
| `Ctrl+A` | 选中当前切片全部 ROI（列表 + 画布） |
| `N` 或 `Ctrl+Right` | 下一个 ROI：选中并 `centerOn` |
| `P` 或 `Ctrl+Left` | 上一个 ROI |
| `Ctrl+PgDown` / `Ctrl+Down` | 下一个切片文件 |
| `Ctrl+PgUp` / `Ctrl+Up` | 上一个切片文件 |
| `F` | 视野适配当前选中 ROI（无选中则适配全部 ROI 包围盒；再无则不动） |
| `Home` | 适配整张切片场景 |

快捷键在主窗口与画布均可触发（画布 `keyPressEvent` 未处理的交给主窗口，或主窗口用 `QShortcut` 注册避免抢文本框输入——**焦点在 spin/combo/lineedit 时不拦截字符快捷键**）。

#### 精确编辑与信息面板

- ROI 列表 `setSelectionMode(ExtendedSelection)`；**双击**弹出 `ROISpinDialog`：x/y/w/h/angle 可编辑，校验 `w,h>0`，应用后写 `ROIManager` + `canvas.update_roi_rect` + 列表文本刷新，压入 undo。
- 信息面板（右侧 ROI 列表上方或状态栏扩展）：选中时显示  
  `id前8位 | x,y | w×h | 角度 |（若有 mpp）物理尺寸`。  
  多选时显示 `已选 n 个 ROI` 与包围盒摘要。

#### ROI 导航与多选批量操作

- 列表选中 ↔ 画布选中双向同步；多选时画布 `setSelected` 多个 `ROIRectItem`，列表选中对应行。
- `N`/`P` 在当前切片 ROI 列表顺序上移动（按 ROIManager 添加顺序或 x,y 排序——**按 x,y 字典序稳定排序**，与列表展示顺序一致）。
- 批量删除：Delete 或按钮；一条 undo。
- 批量复制：`Ctrl+C`/`Ctrl+D` 对所有选中生效；复制出的新 ROI 组成一次 undo。

### S2.4 测试边界

| 模块 | 可自动化验收 |
|------|----------------|
| `roi_undo.py` | add/remove/update/clear 的 undo/redo 幂等与栈深 |
| `export_queue.py` | job 状态机、retry 只重置 failed、cancel 传播、按文件分组 |
| `roi.py` / list 逻辑 | 批量删除后 manager 状态；排序稳定性 |
| GUI | 无头环境不强制 E2E；控制器层逻辑可测 |

不在此范围：真实 SDPC DLL 读写的完整集成测试（沿用现有 mock/跳过策略）。

## [S3] Out of Scope

- 无头 CLI / Python API 批处理入口
- 队列内执行组织检测或 IHC 热点（仅导出）
- ImageJ headless 批量纳入同一队列 UI
- ROI 标签/分类体系、多切片对比项目管理
- 新 WSI 格式支持
- 主题大改或全新视觉语言
- 自动提交到 `main` / 发版

## Tasks

- [ ] T1: Baseline 收口与触点代码质量 — acceptance: worktree 内基线文件齐全可导入；`file_controller` 缩略图 `.copy()`；`preset_controller` 异常有日志；现有 pytest 不回归 (covers: S2.1)
- [ ] T2: ROI undo/redo 核心 — acceptance: `roi_undo` 单测覆盖 add/remove/update/clear 撤销重做，栈上限与批量一条命令 (covers: S2.3)
- [ ] T3: 导出队列模型与控制器 — acceptance: `export_queue` 单测：入队分组、串行完成、单 job 失败不阻断、cancel、`retry_failed` 仅重置 failed (covers: S2.2; depends: T1)
- [ ] T4: ROI 多选同步与批量操作 — acceptance: 列表 ExtendedSelection 与画布多选互同步；批量删除/复制可用；批量删除对应一条 undo (covers: S2.3; depends: T2)
- [ ] T5: 快捷键与 ROI 导航 — acceptance: N/P、Ctrl+PgUp/PgDown、F/Home、Ctrl+A/C/D/Z/Y 行为符合 S2.3 表；焦点在输入控件时字符快捷键不误触发 (covers: S2.3; depends: T4)
- [ ] T6: 精确编辑与信息面板 — acceptance: 双击改 x/y/w/h/angle 写回 model+canvas 并可 undo；选中/多选信息展示正确 (covers: S2.3; depends: T4)
- [ ] T7: 导出队列 UI — acceptance: 可入队多文件、显示状态/进度、取消、重试失败项、结束汇总；旧「批量导出」路径仍可用 (covers: S2.2; depends: T3)
- [ ] T8: 集成接线到 MainWindow — acceptance: 菜单/工具栏入口可打开队列；ROI 变更信号与 undo/列表/画布/信息面板一致 (covers: S2.2; depends: T5; T6; T7)
- [ ] T9: 全量验证与文档 — acceptance: 从 worktree 运行 pytest 全绿或标注 PRE-EXISTING；README 补充队列与快捷键说明 (covers: S2.1; depends: T8)
