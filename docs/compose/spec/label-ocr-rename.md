---
feature: label-ocr-rename
status: delivered
updated: 2026-10-30
branch: feat/label-ocr-rename
commits: d944a06..3faaf29
---

# 标签图 OCR 批量重命名

## Report

**What was built** — 在 v0.8.1 基线上新增「文件 → 按标签图 OCR 重命名」：`label_ocr_rename.py` 承载纯逻辑（`sanitize_filename` / `extract_with_pattern` / `apply_pattern_to_ocr` / `build_rename_plan` / `execute_rename_plan`，含批内交换名两阶段落盘）与 OCR 适配层（RapidOCR 单例、`collect_label_text` 可注入 `ocr_fn`）；`label_ocr_rename_dialog.py` 提供正则配置 + 计划预览表 + 勾选应用；`app.py` 接线菜单项、`_ocr_rename_targets` 目标解析与 `_sync_rename_state` 状态同步（readers / ROI.slide_path / 当前切片 / 文件列表）。OCR 为可选 extra `[ocr]`，未安装时 `ocr_engine_available()` 返回 False 并给出安装指引，核心浏览与导出不受影响。

**Verification** — worktree 内执行（Python 3.12 + PySide6 6.11.1）：
- `py -3.12 -m pytest tests/ -q` → **PASS**，81 passed（50 基线 + 31 新增 `test_label_ocr_rename.py`）
- `py -3.12 -m compileall liver_portal_crop tests` → 通过，无语法/编码错误
- 无头冒烟（`QT_QPA_PLATFORM=offscreen`）：`MainWindow()` 构造成功、`LabelOcrRenameDialog` 可实例化、`ocr_engine_available()` 在缺 rapidocr 时返回 `False` 且不抛栈
- **未覆盖**：真实 RapidOCR 模型 + 真实 SDPC 标签图的端到端改名（本机未安装 `rapidocr`/`onnxruntime`），需安装 `[ocr]` extra 后手工冒烟

**Known limitations**
- PyInstaller 单文件未捆绑 rapidocr 模型目录，打包版 OCR 路径未验证；源码 / pip 运行为准（已写入 README 更新日志）

**Follow-up** — 交付后修复多选链路（见 `fix` 提交）：
- `_file_list` 改为 `ExtendedSelection`，原「多选则仅处理选中」分支至此才可达；`selectedItems()` 顺序不定，`_ocr_rename_targets` 改为按行号排序，稳定批量冲突判定
- `remove_selected_file` 支持批量移除：倒序 `takeItem`、屏蔽 `currentRowChanged` 避免逐个加载中间切片、当前切片被移除时回落邻近项并只重载一次
- `_sync_rename_states` 改为两阶段（先摘出 reader 与列表项、再按新键写回），修复批内交换名 `A→B / B→A` 时后一项命中前一项刚改过的对象导致覆盖的问题；`_apply_rename_batch` 改用 status 判定成功项，`_rename_file` 复用同一 helper
- 验证：offscreen 行为脚本 24 项断言全过（多选顺序、交换名同步、批量移除、回落重载、清空、单文件重命名回归）；`pytest tests/ -q` 仍 81 passed

## [S1] Problem

病理切片 SDPC 文件常以机器名/序号命名，真正可检索的病例号、条码等写在扫描标签图（macrograph[0]）上。当前只能右键单文件手工重命名（`app._rename_file`），无法批量读取标签图文字并映射为文件名；元数据 `PersonInfo`/`ExtraInfo` 未必与物理标签一致，故需要以标签图 OCR 为准。

## [S2] Design

### S2.1 开源选型（调研结论）

| 候选 | 结论 |
|------|------|
| **RapidOCR**（[RapidAI/RapidOCR](https://github.com/RapidAI/RapidOCR)，Apache-2.0，~7.9k stars） | **采用**。ONNX Runtime 离线、中英默认、PyPI `rapidocr` 3.9.2 要求 Python `>=3.8,<4`，与本项目 `>=3.10` 兼容；模型由 PaddleOCR 转 ONNX 再分发 |
| `rapidocr-onnxruntime` 1.4.4 | 旧包，要求 Python `<3.13`，不采用 |
| PaddleOCR / paddlepaddle | 中文强但 paddle 运行时重、PyInstaller 打包成本高，不采用 |
| Windows 系统 OCR | 依赖平台 WinRT 能力与打包路径，本轮不采用 |
| Tesseract / EasyOCR | 分别偏工程配置与 torch 重依赖，不采用 |

安装：`pip install rapidocr onnxruntime`（README 官方路径）。集成时 **lazy import**：未安装时菜单可点但弹出安装指引，不阻断核心浏览/导出。

### S2.2 纯逻辑层（可测、无 Qt/无 OCR）

新模块 `liver_portal_crop/label_ocr_rename.py`：

| 符号 | 契约 |
|------|------|
| `INVALID_FILENAME_CHARS` | `<>:"/\|?*` + 控制字符 |
| `sanitize_filename(s) -> str` | 替换非法字符为 `_`；去首尾空白与末尾 `.`；空则返回 `"untitled"`；长度截断至 180 字符（不含扩展名策略由调用方拼接） |
| `extract_with_pattern(text, pattern) -> str \| None` | `re.search`；有组则取 group(1)（多组取第一个非 None），无组取整段 match；失败返回 `None`；非法 pattern 抛 `re.error` |
| `apply_pattern_to_ocr(text, pattern) -> str \| None` | 先对全文做轻量归一（换行→空格、连续空白折叠），再 `extract_with_pattern`，命中后 `sanitize_filename` |
| `RenameItem` dataclass | `path: Path`, `ocr_text: str`, `extracted: str \| None`, `new_name: str \| None`, `status: Literal["pending","ready","skip","error"]`, `error: str` |
| `build_rename_plan(paths, ocr_map, pattern) -> list[RenameItem]` | 对每个 path：无 OCR 结果/异常 → `error`；pattern 无命中或提取空 → `skip`；提取成功但与原名相同（忽略扩展名）→ `skip`；否则生成 `new_name`（保留原扩展名）。同批内目标名冲突：后者 `status=error`，`error="目标名冲突"`；目标在磁盘已存在且非本批源路径 → `error` |
| `RenameSummary` | `ready`, `skipped`, `errors` 计数 |

**扩展名**：始终保留原文件 `suffix`；`new_name` 含扩展名。

**同批交换/覆盖**：计划阶段允许目标等于批内另一源路径（批内原子重命名时先统一改为临时名再落最终名）；磁盘上**批外**已存在则拒绝。

### S2.3 OCR 适配层

同模块或紧邻 `label_ocr_rename.py` 内：

| 符号 | 契约 |
|------|------|
| `ocr_engine_available() -> bool` | 尝试 `import rapidocr`（及必要 onnxruntime） |
| `get_engine()` | 进程内单例 `RapidOCR()`；导入失败抛 `OcrUnavailableError` |
| `run_ocr_on_image(rgb: np.ndarray) -> str` | numpy RGB → RapidOCR 调用；拼接所有识别行文本（按行 `\n`）；单图异常捕获后向上抛带文件上下文的 `OcrError` |
| `collect_label_text(readers: Mapping[Path, SDPCReader], paths: Sequence[Path], *, ocr_fn=None, progress_cb=None) -> dict[Path, str]` | `ocr_fn` 默认 `run_ocr_on_image`，测试注入假 OCR。`reader.label_image is None` → 不写入该 path（后续 plan 记 skip/error）。`progress_cb(i, n, path)` 可选 |

OCR 引擎与 `ocr_fn` 均可注入，保证无模型环境下单测可跑。

### S2.4 UI：批量预览确认

新模块 `liver_portal_crop/label_ocr_rename_dialog.py`：

1. **入口**：菜单 `文件 → 按标签图 OCR 重命名...`（`app` 中新建 `QAction`）。
2. **目标文件**：若 `_file_list` 有多选则仅处理选中；否则处理列表全部已打开文件。无文件则提示。
3. **步骤 A — 设置**（对话框顶部）：
   - 正则输入框（默认样例：`(?:病例号|病理号|ID)[：:\s]*([A-Za-z0-9\-]+)`，可编辑、可清空——清空表示用归一化全文）；
   - 「开始识别」按钮 → 后台或同步收集 OCR（本轮：**同步 + 进度状态栏**，避免引入新线程类；单文件标签图较小，可接受；若实现中发现卡顿，允许改用 `QThread`，不改验收语义）。
4. **步骤 B — 预览表**（`QTableWidget`）：列 = 原文件名 / 识别摘要（截断）/ 提取结果 / 新文件名 / 状态 / 错误。`ready` 行可勾选；默认全选 `ready`。
5. **步骤 C — 应用**：`应用重命名` 仅执行勾选且 `ready` 的项；二次确认 `QMessageBox`（列出条数）。取消则不改磁盘。
6. **未安装 OCR**：`ocr_engine_available()` 为假时，点击「开始识别」弹 `QMessageBox`，说明 `pip install rapidocr onnxruntime` 或 `pip install -e ".[ocr]"`，不抛未处理异常。
7. **结果**：应用后状态栏 `重命名完成：成功 n / 跳过 / 失败`；失败项留在表内可见。

### S2.5 与既有重命名路径的状态同步

磁盘 `Path.rename` 成功后必须与 `app._rename_file` 一致地更新：

- `self._readers` 键与 `reader._path`
- `roi_manager` 中所有 `slide_path == old` 的 ROI
- `_current_slide`
- `_file_list` 对应 item 文本与 `UserRole`

实现为 `MainWindow._apply_rename_batch(items) -> RenameSummary`，单文件手工重命名可继续走原路径（本轮不要求强制重构合并，但若能复用 helper 则复用）。

批内交换名：先 `old → unique_tmp`（如 `.rename_tmp_<uuid>_<name>`），再 `tmp → final`，避免 Windows 覆盖限制。

### S2.6 依赖与打包

- `pyproject.toml` / `requirements-ocr.txt`（或写入 `requirements.txt` 注释 + extra）：新增 optional-dependencies `ocr = ["rapidocr>=3.0,<4", "onnxruntime>=1.16"]`。
- 核心 `dependencies` **不**强制引入 onnxruntime，保持核心安装体积；功能通过 extra 安装。
- `build.spec`：若时间允许，将 `rapidocr` 模型目录列入 collect；本轮以「源码/pip 运行可用」为验收，exe 打包模型路径记为已知限制（写入 README）。

### S2.7 测试边界

| 对象 | 方式 |
|------|------|
| `sanitize_filename` / `extract_with_pattern` / `apply_pattern_to_ocr` | 纯单测 |
| `build_rename_plan` | 假 `ocr_map`：命中、无命中、同名 skip、批内冲突、磁盘占用、保留扩展名 |
| `collect_label_text` | 注入 `ocr_fn` + stub readers（`label_image` 属性 mock） |
| 批内交换重命名 | 对 `tmp_path` 内临时文件调用实际 rename helper（纯函数 `execute_rename_plan`） |
| 真实 RapidOCR 模型 / 真实 SDPC DLL | **不在**自动化范围；GUI 手动冒烟 |

新增 `execute_rename_plan(items: list[RenameItem]) -> RenameSummary`（仅 `ready`）：临时名两阶段 + 逐项 `OSError` 记入 error，不中断其余项。

## [S3] Out of Scope

- 自动打开文件时无确认自动改名
- 优先使用 `PersonInfo.pathology_id` / barcode 元数据（可作后续增强）
- 对导出 TIFF 命名规则的修改
- PyInstaller 单文件完整捆绑 OCR 模型的保证（仅记录限制）
- 云 OCR / GPU 推理
- 标签图区域手动框选（整图 OCR + 正则即可）

## Tasks

- [x] T1: 纯逻辑模块与单测 — acceptance: `label_ocr_rename` 中 sanitize/extract/plan/execute 在 pytest 下覆盖 S2.2+S2.7 各分支且全绿 (covers: S2.2; S2.7)
- [x] T2: OCR 适配与 collect — acceptance: `collect_label_text` 注入假 `ocr_fn` 测试通过；`ocr_engine_available` 缺依赖时 False 不抛栈 (covers: S2.3)
- [x] T3: 预览确认对话框 — acceptance: 对话框可配置正则、展示计划表、仅应用勾选 ready 项；缺 OCR 时有安装指引 (covers: S2.4; depends: T1; T2)
- [x] T4: MainWindow 接线与状态同步 — acceptance: 菜单入口可用；应用后 readers/ROI/列表/当前切片与 `_rename_file` 语义一致；批内交换不丢文件 (covers: S2.4; S2.5; depends: T3)
- [x] T5: 依赖 extra、README、全量验证 — acceptance: `pip install -e ".[ocr]"` 元数据正确；`pytest tests/ -q` 全绿或标注 PRE-EXISTING；README 增补功能说明与已知打包限制 (covers: S2.6; depends: T4)
