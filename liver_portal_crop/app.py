"""MainWindow — 主窗口，组装所有模块。"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

from PySide6.QtCore import Qt, QRectF, QThread, QTimer, QObject, Signal
from PySide6.QtWidgets import QDialog
from PySide6.QtGui import QAction, QImage, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMainWindow, QMenuBar, QMessageBox,
    QProgressBar, QProgressDialog, QPushButton, QSlider, QSpinBox,
    QSplitter, QStackedWidget, QVBoxLayout, QWidget,
)

from liver_portal_crop.theme import load_theme
from liver_portal_crop.canvas import WSICanvas
from liver_portal_crop.dialogs import SettingsDialog
from liver_portal_crop.exporter import BatchExporter, CropConfig
from liver_portal_crop.navigator import NavigationWidget
from liver_portal_crop.tissue_detect import (
    detect_tissue, tissue_regions_to_rois, tissue_regions_to_rois_grid, TissueDialog,
)
from liver_portal_crop.ihc_hotspot import IHCHotspotDialog
from liver_portal_crop.reader import SDPCReader, SDPCReadError
from liver_portal_crop.roi import ROIManager, ROIModel
from liver_portal_crop.preview_dialog import ROIPreviewDialog, ROIPreviewPanel
from liver_portal_crop.analysis_dialog import DeepLIIFAnalysisDialog
from liver_portal_crop.results_viewer import DeepLIIFResultsDialog
from liver_portal_crop.deepliif_runner import (
    DeepLIIFMode, DeepLIIFWorker, check_model_available, get_default_model_dir,
)
from liver_portal_crop.constants import (
    SESSION_DIR_NAME, DEFAULT_OUTPUT_DIR_NAME, PREVIEW_REFRESH_MS,
    IMAGEJ_CHECK_INTERVAL_MS, FIELD_NUMBER_MM, ROI_ID_LENGTH,
)
from liver_portal_crop.controllers import (
    FileController, ROIController, ExportController, PresetController,
)

SESSION_DIR = Path.home() / SESSION_DIR_NAME
SESSION_FILE = SESSION_DIR / "session.json"
PRESETS_FILE = SESSION_DIR / "presets.json"


class _ImageJBatchWorker(QObject):
    """在 QThread 中运行 ImageJ headless 批量分析。"""

    progress = Signal(int, int)           # current, total
    finished = Signal(str, str)           # csv_path, summary_text
    error = Signal(str)                   # error_message

    def __init__(
        self,
        images: list,
        config_path: str,
        output_dir: str,
        fiji_path: str | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self._images = images
        self._config_path = config_path
        self._output_dir = output_dir
        self._fiji_path = fiji_path

    def run(self) -> None:
        try:
            from liver_portal_crop.imagej_bridge import (
                run_headless_batch, AnalysisConfig,
            )

            config = AnalysisConfig.load(self._config_path)

            # 包装进度回调
            total = len(self._images)
            self.progress.emit(0, total)

            results = run_headless_batch(
                images=self._images,
                config=config,
                output_dir=self._output_dir,
                fiji_path=self._fiji_path,
            )

            # 汇总
            success = sum(1 for r in results if r.success)
            total_particles = sum(r.particle_count for r in results if r.success)
            csv_path = str(Path(self._output_dir) / "measurements_summary.csv")
            summary = (
                f"成功: {success}/{total}\n"
                f"检出粒子总数: {total_particles}\n"
                f"结果目录: {self._output_dir}"
            )

            self.finished.emit(csv_path, summary)

        except Exception as e:
            logger.error("ImageJ 批量分析失败: %s", e, exc_info=True)
            self.error.emit(str(e))


class _ImageJInstallWorker(QObject):
    """在 QThread 中执行 PyImageJ 依赖安装，支持进度回调和取消。"""

    progress = Signal(str)          # 进度消息
    finished = Signal(bool, str)    # success, message

    def __init__(self, parent=None):
        super().__init__(parent)

    def run(self) -> None:
        import subprocess
        import time

        packages = ["imagej", "scyjava"]
        self.progress.emit(f"pip install {' '.join(packages)} …")

        # 总超时时间（秒）— 防止 pip 因网络问题永久卡死
        INSTALL_TIMEOUT = 600  # 10 分钟

        try:
            # 用临时文件接收输出，避免管道缓冲区满导致 pip 阻塞
            tmp_path = tempfile.mktemp(suffix=".log", prefix="pip_install_")
            with open(tmp_path, "wb") as tmp:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "pip", "install"] + packages,
                    stdout=tmp,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )

                self.progress.emit("正在下载安装依赖，请稍候…")
                start_time = time.time()

                while proc.poll() is None:
                    # 检查取消
                    if QThread.currentThread().isInterruptionRequested():
                        proc.terminate()
                        try:
                            proc.wait(timeout=10)
                        except Exception:
                            proc.kill()
                            proc.wait(timeout=5)
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass
                        self.finished.emit(False, "用户取消安装")
                        return

                    # 检查总超时
                    if time.time() - start_time > INSTALL_TIMEOUT:
                        proc.kill()
                        try:
                            proc.wait(timeout=10)
                        except Exception:
                            pass
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass
                        self.finished.emit(False, f"安装超时（{INSTALL_TIMEOUT}秒）")
                        return

                    time.sleep(0.5)

                    # 定期更新已等待时间
                    elapsed = int(time.time() - start_time)
                    if elapsed > 0 and elapsed % 10 == 0:
                        self.progress.emit(f"正在下载安装依赖，已等待 {elapsed}s…")

                # 确保文件写入完成
                tmp.flush()
                os.fsync(tmp.fileno())
            # 文件已关闭，现在可以安全读取

            rc = proc.wait()

            # 从临时文件读取输出
            error_msg = ""
            try:
                if os.path.exists(tmp_path):
                    with open(tmp_path, "rb") as f:
                        raw = f.read()
                    content = raw.decode("utf-8", errors="replace")
                    if content:
                        lines = content.splitlines()
                        # 取最后 20 行
                        error_msg = "\n".join(lines[-20:])
            except OSError:
                pass

            # 清理临时文件
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

            if rc == 0:
                self.finished.emit(True, "安装成功")
            else:
                tail = error_msg.strip()[-800:] if error_msg else "无详细输出"
                self.finished.emit(False, f"pip 返回错误码 {rc}\n\n{tail}")

        except FileNotFoundError:
            self.finished.emit(False, "找不到 Python/pip，请确保已加入 PATH")
        except Exception as e:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            self.finished.emit(False, f"安装出错: {e}")


class _FijiDownloadWorker(QObject):
    """在 QThread 中下载 Fiji 到本地目录。"""

    progress = Signal(str)          # 进度消息
    finished = Signal(bool, str)    # success, message

    # Windows 版本 Fiji 下载链接（2025 年新版，包含 JDK）
    # 多镜像源，按优先级尝试（第一个不通自动切换下一个）
    _FIJI_URLS = [
        "https://downloads.imagej.net/fiji/latest/fiji-latest-win64-jdk.zip",      # US 主站
        "https://downloads.micron.ox.ac.uk/fiji_update/mirrors/fiji-latest/fiji-latest-win64-jdk.zip",  # UK
        "https://mirrors.pasteur.fr/fiji/downloads/latest/fiji-latest-win64-jdk.zip",  # FR
    ]
    _TARGET_DIR = Path.home() / "Fiji.app"

    def __init__(self, parent=None):
        super().__init__(parent)

    def run(self) -> None:
        import zipfile
        import time

        target_dir = self._TARGET_DIR

        try:
            if target_dir.exists():
                self.finished.emit(
                    False,
                    f"目标目录已存在: {target_dir}\n请先删除旧版本再下载，或在「分析」菜单中手动设置路径。",
                )
                return

            tmp_dir = tempfile.gettempdir()
            zip_path = os.path.join(tmp_dir, "fiji_download.zip")

            # 尝试所有镜像源，第一个成功的就用
            last_error = ""
            for url in self._FIJI_URLS:
                try:
                    mirror_name = url.split("/")[2] if "/" in url else url
                    self.progress.emit(f"正在下载 Fiji（约 680MB），尝试镜像: {mirror_name}…")
                    self._download_with_retry(url, zip_path)
                    break  # 成功，跳出循环
                except Exception as e:
                    last_error = str(e)
                    self.progress.emit(f"镜像 {mirror_name} 不可用，尝试下一个…")
                    # 清理失败的下载文件
                    try:
                        os.unlink(zip_path)
                    except OSError:
                        pass
            else:
                raise RuntimeError(f"所有镜像均不可用。最后错误: {last_error}")

            self.progress.emit("下载完成，正在解压（可能需要几分钟）…")

            with zipfile.ZipFile(zip_path, "r") as zf:
                names = zf.namelist()
                top_dirs = set()
                for name in names:
                    if "/" in name:
                        top_dirs.add(name.split("/")[0])
                top_dir = list(top_dirs)[0] if top_dirs else "Fiji.app"

                extract_base = Path.home()
                zf.extractall(extract_base)

                extracted_path = extract_base / top_dir
                if extracted_path != target_dir and extracted_path.exists():
                    extracted_path.rename(target_dir)

            try:
                os.unlink(zip_path)
            except OSError:
                pass

            self.progress.emit("设置完成")
            self.finished.emit(True, str(target_dir))

        except Exception as e:
            try:
                os.unlink(zip_path)
            except OSError:
                pass
            self.finished.emit(False, f"下载失败: {e}")

    def _download_with_retry(self, url: str, dest: str, max_retries: int = 3) -> None:
        """分块下载文件，支持断点续传和自动重试。"""
        import urllib.request
        import http.client
        import time

        chunk_size = 1024 * 1024  # 1MB per chunk
        max_retries = 3

        for attempt in range(1, max_retries + 1):
            try:
                # 检查已下载大小（断点续传）
                start_pos = 0
                if os.path.exists(dest):
                    start_pos = os.path.getsize(dest)

                headers = {}
                if start_pos > 0:
                    headers["Range"] = f"bytes={start_pos}-"

                req = urllib.request.Request(url, headers=headers)
                response = urllib.request.urlopen(req, timeout=30)

                total_size = int(response.headers.get("Content-Length", 0))
                if start_pos > 0 and response.status == 206:
                    # 服务器支持断点续传
                    total_size = int(response.headers.get("Content-Range", "").split("/")[-1] or total_size)
                    downloaded = start_pos
                else:
                    # 从头下载
                    downloaded = 0
                    start_pos = 0

                mode = "ab" if start_pos > 0 else "wb"
                with open(dest, mode) as f:
                    while True:
                        chunk = response.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)

                        if total_size > 0:
                            pct = min(100, int(downloaded * 100 / total_size))
                            if downloaded > 1024 * 1024:
                                speed_str = f"{downloaded / 1024 / 1024:.1f} MB"
                            else:
                                speed_str = f"{downloaded / 1024:.1f} KB"
                            self.progress.emit(f"下载中… {pct}% ({speed_str} / {total_size / 1024 / 1024:.0f} MB)")

                # 验证文件大小
                actual_size = os.path.getsize(dest)
                if total_size > 0 and actual_size < total_size:
                    raise ConnectionError(f"下载不完整: 期望 {total_size} 字节，实际 {actual_size} 字节")

                self.progress.emit(f"下载完成 ({downloaded / 1024 / 1024:.0f} MB)")
                return  # 成功

            except (ConnectionError, urllib.error.URLError, http.client.IncompleteRead, OSError) as e:
                if attempt < max_retries:
                    wait_time = attempt * 5
                    self.progress.emit(f"下载中断，{wait_time}秒后重试 ({attempt}/{max_retries})…")
                    time.sleep(wait_time)
                else:
                    raise RuntimeError(f"下载失败（已重试 {max_retries} 次）: {e}")


class _PatchWorker(QObject):
    """小块测试推理 Worker。"""
    from PySide6.QtCore import Signal as QSignal
    finished = QSignal(dict)
    error = QSignal(str)

    def __init__(self, patch, mode, model_dir, tile_size, seg_only, patch_roi, parent=None):
        super().__init__(parent)
        self._patch = patch
        self._mode = mode
        self._model_dir = model_dir
        self._tile_size = tile_size
        self._seg_only = seg_only
        self._patch_roi = patch_roi

    def run(self) -> None:
        try:
            from liver_portal_crop.deepliif_runner import (
                infer_local, infer_cloud, DeepLIIFMode,
            )
            if self._mode == DeepLIIFMode.LOCAL:
                images, scoring = infer_local(
                    self._patch, self._model_dir, self._tile_size, self._seg_only,
                )
            else:
                images, scoring = infer_cloud(
                    self._patch, resolution="40x", seg_only=self._seg_only,
                )
            images["IHC"] = self._patch
            self.finished.emit({
                "roi_id": "patch_test",
                "roi": self._patch_roi,
                "images": images,
                "scoring": scoring,
                "tile_size": self._tile_size,
            })
        except Exception as e:
            self.error.emit(str(e))


class MainWindow(QMainWindow):
    """应用程序主窗口。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("病理裁剪工具")
        self.resize(1200, 800)

        self._readers: dict[Path, SDPCReader] = {}
        self._roi_manager = ROIManager()
        self._crop_config = CropConfig(
            output_dir=Path.home() / DEFAULT_OUTPUT_DIR_NAME,
        )
        self._current_slide: Path | None = None
        self._current_theme: str = "dark"
        self._selected_roi_id: str | None = None
        self._preview_refresh_timer = QTimer()
        self._preview_refresh_timer.setSingleShot(True)
        self._preview_refresh_timer.setInterval(PREVIEW_REFRESH_MS)
        self._preview_refresh_timer.timeout.connect(self._do_preview_refresh)

        # ── ImageJ 桥接状态 ──
        self._imagej_config_path = SESSION_DIR / "imagej_config.json"
        self._imagej_fiji_path: str = ""          # 本地 Fiji 路径（从 session 恢复）
        self._imagej_subprocess = None            # GUI 调参子进程
        self._imagej_check_timer = QTimer()       # 轮询子进程完成
        self._imagej_check_timer.setInterval(2000)
        self._imagej_check_timer.timeout.connect(self._check_imagej_subprocess)

        # ── Controllers ──
        self._preset_controller = PresetController(self)
        self._file_controller = FileController(self)
        self._roi_controller = ROIController(self)
        self._export_controller = ExportController(self)

        self._setup_ui()
        self._connect_signals()
        self._setup_menu()
        self._load_session()
        self._preset_controller.load_presets()

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ── 顶部工具栏（QStackedWidget：画布工具栏 / 预览工具栏）──
        self._toolbar_stack = QStackedWidget()
        self._toolbar_stack.setFixedHeight(40)

        # --- 画布工具栏 (index 0) ---
        canvas_tb = QWidget()
        canvas_tb.setObjectName("topToolbar")
        tbar = QHBoxLayout(canvas_tb)
        tbar.setContentsMargins(12, 6, 12, 6)
        tbar.setSpacing(8)

        self._status_label = QLabel("就绪")
        self._status_label.setObjectName("statusLabel")
        tbar.addWidget(self._status_label)

        self._preset_cb = QComboBox()
        self._preset_cb.setObjectName("presetCb")
        self._preset_cb.setMinimumWidth(90)
        self._preset_cb.currentTextChanged.connect(self._apply_preset)
        tbar.addWidget(self._preset_cb)

        self._save_preset_btn = QPushButton("保存")
        self._save_preset_btn.setFixedSize(40, 24)
        self._save_preset_btn.setObjectName("savePresetBtn")
        self._save_preset_btn.clicked.connect(self._save_preset)
        tbar.addWidget(self._save_preset_btn)

        tbar.addSpacing(16)

        self._roi_mode_btn = QPushButton("ROI 绘制")
        self._roi_mode_btn.setObjectName("roiBtn")
        self._roi_mode_btn.setCheckable(True)
        self._roi_mode_btn.clicked.connect(self._toggle_roi_mode)
        tbar.addWidget(self._roi_mode_btn)

        tbar.addSpacing(8)

        tbar.addWidget(QLabel("倍率:"))
        self._mag_cb = QComboBox()
        self._mag_cb.addItems(["4x", "10x", "20x", "40x", "80x", "自定义"])
        self._mag_cb.setCurrentText("20x")
        self._mag_cb.currentTextChanged.connect(self._auto_calc_frame)
        tbar.addWidget(self._mag_cb)

        tbar.addWidget(QLabel("比例:"))
        self._ratio_cb = QComboBox()
        self._ratio_cb.addItems(["Free", "1:1", "4:3", "3:2", "16:9"])
        self._ratio_cb.setCurrentText("16:9")
        self._ratio_cb.currentTextChanged.connect(self._auto_calc_frame)
        tbar.addWidget(self._ratio_cb)

        tbar.addWidget(QLabel("框宽:"))
        self._frame_w_spin = QSpinBox()
        self._frame_w_spin.setRange(64, 999999)
        self._frame_w_spin.setSingleStep(64)
        self._frame_w_spin.setValue(512)
        self._frame_w_spin.valueChanged.connect(self._update_frame_size)
        tbar.addWidget(self._frame_w_spin)

        tbar.addWidget(QLabel("框高:"))
        self._frame_h_spin = QSpinBox()
        self._frame_h_spin.setRange(64, 999999)
        self._frame_h_spin.setSingleStep(64)
        self._frame_h_spin.setValue(512)
        self._frame_h_spin.valueChanged.connect(self._update_frame_size)
        tbar.addWidget(self._frame_h_spin)

        tbar.addWidget(QLabel("角度:"))
        self._frame_angle_slider = QSlider(Qt.Orientation.Horizontal)
        self._frame_angle_slider.setRange(0, 359)
        self._frame_angle_slider.setSingleStep(5)
        self._frame_angle_slider.setPageStep(15)
        self._frame_angle_slider.setFixedWidth(110)
        self._frame_angle_slider.valueChanged.connect(self._on_frame_angle_changed)
        tbar.addWidget(self._frame_angle_slider)
        self._frame_angle_label = QLabel("0°")
        self._frame_angle_label.setFixedWidth(32)
        self._frame_angle_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        tbar.addWidget(self._frame_angle_label)

        self._progress_bar = QProgressBar()
        self._progress_bar.setObjectName("exportProgress")
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setFixedWidth(160)
        self._progress_bar.setFixedHeight(18)
        self._progress_bar.hide()
        tbar.addWidget(self._progress_bar)

        self._cancel_btn = QPushButton("×")
        self._cancel_btn.setFixedSize(20, 20)
        self._cancel_btn.setObjectName("cancelBtn")
        self._cancel_op = None
        self._patch_worker = None
        self._patch_thread = None
        self._patch_results = None  # 缓存最近一次小块测试结果
        self._deepliif_results = None  # 缓存最近一次批量分析结果
        self._deepliif_worker = None
        self._deepliif_thread = None
        self._active_result_dlg = None  # 当前打开的结果对话框引用
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)
        self._cancel_btn.hide()
        tbar.addWidget(self._cancel_btn)

        tbar.addStretch()

        self._settings_btn = QPushButton("输出目录")
        self._settings_btn.setObjectName("dirBtn")
        self._settings_btn.clicked.connect(self._show_settings)
        tbar.addWidget(self._settings_btn)

        self._export_btn = QPushButton("批量导出")
        self._export_btn.setObjectName("exportBtn")
        self._export_btn.clicked.connect(self._start_export)
        tbar.addWidget(self._export_btn)

        self._preview_win_btn = QPushButton("ROI 预览")
        self._preview_win_btn.setToolTip("切换到 ROI 缩略图预览视图")
        self._preview_win_btn.clicked.connect(self._toggle_preview_view)
        tbar.addWidget(self._preview_win_btn)

        self._toolbar_stack.addWidget(canvas_tb)  # index 0

        # --- 预览工具栏 (index 1) ---
        preview_tb = QWidget()
        preview_tb.setObjectName("topToolbar")
        ptbar = QHBoxLayout(preview_tb)
        ptbar.setContentsMargins(12, 6, 12, 6)
        ptbar.setSpacing(8)

        self._preview_status_label = QLabel("ROI 预览")
        self._preview_status_label.setObjectName("statusLabel")
        ptbar.addWidget(self._preview_status_label)

        ptbar.addSpacing(16)

        self._preview_select_all_btn = QPushButton("全选")
        self._preview_deselect_btn = QPushButton("全不选")
        self._preview_invert_btn = QPushButton("反选")
        ptbar.addWidget(self._preview_select_all_btn)
        ptbar.addWidget(self._preview_deselect_btn)
        ptbar.addWidget(self._preview_invert_btn)

        ptbar.addSpacing(16)
        ptbar.addWidget(QLabel("筛选:"))
        self._preview_filter_cb = QComboBox()
        self._preview_filter_cb.addItem("全部文件")
        self._preview_filter_cb.setMinimumWidth(100)
        ptbar.addWidget(self._preview_filter_cb)

        ptbar.addSpacing(16)

        self._imagej_tune_btn = QPushButton("ImageJ 调参")
        self._imagej_tune_btn.setToolTip("启动 Fiji 对选中 ROI 进行交互式调参")
        self._imagej_tune_btn.clicked.connect(self._on_imagej_tune_clicked)
        ptbar.addWidget(self._imagej_tune_btn)

        self._imagej_batch_btn = QPushButton("ImageJ 批量分析")
        self._imagej_batch_btn.setToolTip("用已保存的配置对所有 ROI 执行 headless 批量分析")
        self._imagej_batch_btn.clicked.connect(self._on_imagej_batch_clicked)
        ptbar.addWidget(self._imagej_batch_btn)

        ptbar.addStretch()

        self._preview_count_label = QLabel("已选: 0/0")
        ptbar.addWidget(self._preview_count_label)

        self._preview_progress = QProgressBar()
        self._preview_progress.setObjectName("exportProgress")
        self._preview_progress.setRange(0, 100)
        self._preview_progress.setValue(0)
        self._preview_progress.setFixedWidth(160)
        self._preview_progress.setFixedHeight(18)
        self._preview_progress.hide()
        ptbar.addWidget(self._preview_progress)

        self._preview_cancel_btn = QPushButton("×")
        self._preview_cancel_btn.setFixedSize(20, 20)
        self._preview_cancel_btn.setObjectName("cancelBtn")
        self._preview_cancel_btn.clicked.connect(self._on_cancel_clicked)
        self._preview_cancel_btn.hide()
        ptbar.addWidget(self._preview_cancel_btn)

        self._preview_settings_btn = QPushButton("输出目录")
        self._preview_settings_btn.setObjectName("dirBtn")
        self._preview_settings_btn.clicked.connect(self._show_settings)
        ptbar.addWidget(self._preview_settings_btn)

        self._preview_export_all_btn = QPushButton("批量导出")
        self._preview_export_all_btn.setObjectName("exportBtn")
        self._preview_export_all_btn.clicked.connect(self._preview_export_all)
        ptbar.addWidget(self._preview_export_all_btn)

        self._preview_export_sel_btn = QPushButton("导出选中")
        self._preview_export_sel_btn.setObjectName("exportBtn")
        self._preview_export_sel_btn.clicked.connect(self._preview_export_selected)
        ptbar.addWidget(self._preview_export_sel_btn)

        self._preview_back_btn = QPushButton("返回画布")
        self._preview_back_btn.clicked.connect(self._toggle_preview_view)
        ptbar.addWidget(self._preview_back_btn)

        self._toolbar_stack.addWidget(preview_tb)  # index 1

        main_layout.addWidget(self._toolbar_stack)

        # ── 分割线 ──
        sep = QWidget()
        sep.setFixedHeight(1)
        sep.setStyleSheet("background: transparent;")
        main_layout.addWidget(sep)

        # ── 内容区 ──
        self._body = QSplitter(Qt.Orientation.Horizontal)
        self._body.setHandleWidth(2)

        # 左侧：导航缩略图 + 文件列表（预览模式下隐藏）
        self._left_panel = QWidget()
        self._left_panel.setObjectName("sidePanel")
        left_layout = QVBoxLayout(self._left_panel)
        left_layout.setContentsMargins(12, 12, 8, 12)
        left_layout.setSpacing(8)

        self._nav = NavigationWidget()
        self._nav.setObjectName("navWidget")
        left_layout.addWidget(self._nav)

        file_list_header = QLabel("文件列表")
        file_list_header.setObjectName("sectionHeader")
        left_layout.addWidget(file_list_header)
        self._file_list = QListWidget()
        left_layout.addWidget(self._file_list)

        file_btn_layout = QHBoxLayout()
        file_btn_layout.setSpacing(6)
        self._add_file_btn = QPushButton("添加文件...")
        self._add_file_btn.clicked.connect(self._add_files)
        self._remove_file_btn = QPushButton("移除选中")
        self._remove_file_btn.clicked.connect(self._remove_selected_file)
        file_btn_layout.addWidget(self._add_file_btn)
        file_btn_layout.addWidget(self._remove_file_btn)
        left_layout.addLayout(file_btn_layout)
        self._body.addWidget(self._left_panel)

        # 中央：QStackedWidget（画布 / 预览面板 切换）
        self._canvas = WSICanvas()
        self._stack = QStackedWidget()
        self._stack.addWidget(self._canvas)  # index 0 = 画布
        # 预览面板在首次切换时延迟创建
        self._preview_panel: ROIPreviewPanel | None = None
        self._body.addWidget(self._stack)

        # 右侧：ROI 列表
        right_panel = QWidget()
        right_panel.setObjectName("sidePanel")
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(8, 12, 12, 12)
        right_layout.setSpacing(8)

        # 分析工具组
        analysis_group = QWidget()
        analysis_group.setObjectName("analysisGroup")
        analysis_layout = QVBoxLayout(analysis_group)
        analysis_layout.setContentsMargins(10, 8, 10, 10)
        analysis_layout.setSpacing(6)

        analysis_header = QLabel("分析工具")
        analysis_header.setObjectName("sectionHeader")
        analysis_layout.addWidget(analysis_header)

        self._tissue_btn = QPushButton("组织检测 (HistoKit)")
        self._tissue_btn.setObjectName("toolActionBtn")
        self._tissue_btn.clicked.connect(self._detect_tissue)
        analysis_layout.addWidget(self._tissue_btn)

        self._ihc_hotspot_btn = QPushButton("IHC 热点检测")
        self._ihc_hotspot_btn.setObjectName("toolActionBtn")
        self._ihc_hotspot_btn.setToolTip(
            "自动识别免疫组化阳性区域密度最高的热点区域并生成 ROI"
        )
        self._ihc_hotspot_btn.clicked.connect(self._detect_ihc_hotspot)
        analysis_layout.addWidget(self._ihc_hotspot_btn)

        self._deepliif_btn = QPushButton("DeepLIIF 分析")
        self._deepliif_btn.setObjectName("toolActionBtn")
        self._deepliif_btn.setToolTip("使用 DeepLIIF 进行 IHC 染色分析和细胞分割")
        self._deepliif_btn.clicked.connect(self._on_deepliif_btn_clicked)
        self._deepliif_btn.setVisible(False)
        analysis_layout.addWidget(self._deepliif_btn)

        self._clear_overlay_btn = QPushButton("清除分析叠加")
        self._clear_overlay_btn.clicked.connect(self._clear_analysis_overlay)
        self._clear_overlay_btn.setVisible(False)
        analysis_layout.addWidget(self._clear_overlay_btn)

        right_layout.addWidget(analysis_group)

        # ROI 位置编辑（选中后启用）
        roi_pos_header = QLabel("ROI 位置")
        roi_pos_header.setObjectName("sectionHeader")
        right_layout.addWidget(roi_pos_header)

        roi_form = QFormLayout()
        roi_form.setSpacing(6)
        roi_form.setContentsMargins(0, 0, 0, 0)
        self._roi_x_spin = QSpinBox()
        self._roi_x_spin.setRange(0, 9999999)
        self._roi_x_spin.setEnabled(False)
        roi_form.addRow("X:", self._roi_x_spin)
        self._roi_y_spin = QSpinBox()
        self._roi_y_spin.setRange(0, 9999999)
        self._roi_y_spin.setEnabled(False)
        roi_form.addRow("Y:", self._roi_y_spin)
        self._roi_w_spin = QSpinBox()
        self._roi_w_spin.setRange(1, 9999999)
        self._roi_w_spin.setEnabled(False)
        roi_form.addRow("W:", self._roi_w_spin)
        self._roi_h_spin = QSpinBox()
        self._roi_h_spin.setRange(1, 9999999)
        self._roi_h_spin.setEnabled(False)
        roi_form.addRow("H:", self._roi_h_spin)
        right_layout.addLayout(roi_form)

        self._roi_x_spin.valueChanged.connect(self._on_roi_spin_changed)
        self._roi_y_spin.valueChanged.connect(self._on_roi_spin_changed)
        self._roi_w_spin.valueChanged.connect(self._on_roi_spin_changed)
        self._roi_h_spin.valueChanged.connect(self._on_roi_spin_changed)

        roi_list_header = QLabel("ROI 列表")
        roi_list_header.setObjectName("sectionHeader")
        right_layout.addWidget(roi_list_header)
        self._roi_list = QListWidget()
        right_layout.addWidget(self._roi_list)

        roi_btn_layout = QVBoxLayout()
        roi_btn_layout.setSpacing(6)
        self._delete_roi_btn = QPushButton("删除选中 ROI")
        self._delete_roi_btn.setObjectName("dangerBtn")
        self._delete_roi_btn.clicked.connect(self._delete_selected_roi)
        self._clear_current_btn = QPushButton("清空当前文件")
        self._clear_current_btn.setObjectName("dangerBtn")
        self._clear_current_btn.clicked.connect(self._clear_current_roi)
        self._clear_all_btn = QPushButton("清空全部 ROI")
        self._clear_all_btn.setObjectName("dangerBtn")
        self._clear_all_btn.clicked.connect(self._clear_all_rois)
        roi_btn_layout.addWidget(self._delete_roi_btn)
        roi_btn_layout.addWidget(self._clear_current_btn)
        roi_btn_layout.addWidget(self._clear_all_btn)
        right_layout.addLayout(roi_btn_layout)

        self._body.addWidget(right_panel)

        self._body.setSizes([240, 680, 240])
        main_layout.addWidget(self._body, 1)

    def _connect_signals(self) -> None:
        self._file_list.currentRowChanged.connect(self._on_file_selected)
        self._roi_list.currentRowChanged.connect(self._on_roi_list_selected)
        self._roi_manager.roi_added.connect(self._on_roi_added)
        self._roi_manager.roi_removed.connect(self._on_roi_removed)
        self._canvas.roi_created.connect(self._on_canvas_roi_created)
        self._canvas.roi_selected.connect(self._on_canvas_roi_selected)
        self._canvas.roi_rect_changed.connect(self._on_roi_rect_changed)
        self._canvas.roi_selection_changed.connect(self._on_roi_selection_changed)
        self._canvas.viewport_changed.connect(self._nav.update_viewport)
        self._canvas.frame_angle_changed.connect(self._on_canvas_frame_angle_changed)
        self._nav.navigated.connect(self._on_nav_clicked)

    def _setup_menu(self) -> None:
        menubar = self.menuBar()
        file_menu = menubar.addMenu("文件")
        file_menu.addAction("添加文件...", self._add_files)
        file_menu.addSeparator()
        file_menu.addAction("退出", self.close)

        view_menu = menubar.addMenu("显示")
        self._theme_action = QAction("浅色模式", self)
        self._theme_action.setCheckable(True)
        self._theme_action.setChecked(False)
        self._theme_action.triggered.connect(self._toggle_theme)
        view_menu.addAction(self._theme_action)

        analysis_menu = menubar.addMenu("分析")
        analysis_menu.addAction("IHC 热点检测...", self._detect_ihc_hotspot)
        analysis_menu.addAction("DeepLIIF 分析...", self._run_deepliif)
        analysis_menu.addSeparator()
        analysis_menu.addAction("设置模型路径...", self._set_deepliif_model_dir)
        analysis_menu.addSeparator()
        analysis_menu.addAction("ImageJ 调参...", self._on_imagej_tune_clicked)
        analysis_menu.addAction("ImageJ 批量分析...", self._on_imagej_batch_clicked)
        analysis_menu.addAction("设置 Fiji 路径...", self._set_fiji_path)
        analysis_menu.addAction("下载 Fiji...", self._download_fiji)
        analysis_menu.addAction("保存 ImageJ 配置...", self._save_imagej_config)
        analysis_menu.addSeparator()
        analysis_menu.addAction("安装/检查 PyImageJ...", self._install_imagej_from_menu)

        help_menu = menubar.addMenu("帮助")
        help_menu.addAction("关于", lambda: QMessageBox.about(
            self, "关于",
            "病理裁剪工具 v0.7.1\n\n"
            "作者：Funvvell\n\n"
            "功能：\n"
            "• SDPC 病理切片浏览与 ROI 标注\n"
            "• 汇管区自动检测与批量裁剪导出\n"
            "• IHC 阳性热点检测与 ROI 自动生成\n"
            "• DeepLIIF 免疫组化分析",
        ))

    def _apply_theme(self, name: str) -> None:
        qss = load_theme(name)
        if qss:
            from PySide6.QtWidgets import QApplication
            QApplication.instance().setStyleSheet(qss)
        self._current_theme = name

    def _toggle_theme(self) -> None:
        new_theme = "light" if self._current_theme == "dark" else "dark"
        self._apply_theme(new_theme)
        self._theme_action.setText("深色模式" if new_theme == "light" else "浅色模式")
        self._theme_action.setChecked(new_theme == "light")

    # ── 预设 & 文件管理 ────────────────────────────────

    def _load_presets(self) -> None:
        self._preset_controller.load_presets()

    def _save_preset(self) -> None:
        self._preset_controller.save_preset()

    def _apply_preset(self, name: str) -> None:
        self._preset_controller._apply_preset(name)

    # ── 文件管理 ──────────────────────────────────────

    def _add_files(self) -> None:
        self._file_controller.add_files()

    def _remove_selected_file(self) -> None:
        self._file_controller.remove_selected_file()

    def _on_file_selected(self, row: int) -> None:
        self._file_controller.on_file_selected(row)

    def _update_nav_thumb(self, reader) -> None:
        self._file_controller._update_nav_thumb(reader)

    def _on_nav_clicked(self, scene_x: float, scene_y: float) -> None:
        self._file_controller.on_nav_clicked(scene_x, scene_y)

    # ── ROI 交互 ──────────────────────────────────────

    def _auto_calc_frame(self) -> None:
        self._roi_controller.auto_calc_frame()

    def _update_frame_size(self) -> None:
        self._roi_controller._update_frame_size()

    def _on_frame_angle_changed(self, value: int) -> None:
        self._roi_controller.on_frame_angle_changed(value)

    def _on_canvas_frame_angle_changed(self, angle: float) -> None:
        self._roi_controller.on_canvas_frame_angle_changed(angle)

    def _toggle_roi_mode(self, checked: bool) -> None:
        self._roi_controller.toggle_roi_mode(checked)

    def _on_canvas_roi_created(self, roi_id: str, rect, angle: float = 0.0) -> None:
        self._roi_controller.on_canvas_roi_created(roi_id, rect, angle)

    def _on_canvas_roi_selected(self, roi_id: str) -> None:
        self._roi_controller.on_canvas_roi_selected(roi_id)

    def _on_roi_rect_changed(self, roi_id: str, new_rect, angle: float = 0.0) -> None:
        self._roi_controller.on_roi_rect_changed(roi_id, new_rect, angle)

    def _on_roi_selection_changed(self, roi_id: str) -> None:
        self._roi_controller.on_roi_selection_changed(roi_id)

    def _on_roi_list_selected(self, row: int) -> None:
        self._roi_controller.on_roi_list_selected(row)

    def _on_roi_spin_changed(self) -> None:
        self._roi_controller.on_roi_spin_changed()

    def _update_roi_spins(self) -> None:
        self._roi_controller.update_roi_spins()

    def _on_roi_added(self, roi: ROIModel) -> None:
        self._roi_controller.on_roi_added(roi)

    def _on_roi_removed(self, roi_id: str) -> None:
        self._roi_controller.on_roi_removed(roi_id)

    def _restore_roi_on_canvas(self) -> None:
        self._roi_controller.restore_roi_on_canvas()

    def _refresh_roi_list(self) -> None:
        self._roi_controller.refresh_roi_list()

    def _delete_selected_roi(self) -> None:
        self._roi_controller.delete_selected_roi()

    def _clear_current_roi(self) -> None:
        self._roi_controller.clear_current_roi()

    def _clear_all_rois(self) -> None:
        self._roi_controller.clear_all_rois()

    # ── 导出 ──────────────────────────────────────────

    def _show_settings(self) -> None:
        self._export_controller.show_settings()

    def _start_export(self) -> None:
        self._export_controller.start_export()

    def _show_preview_dialog(self) -> None:
        self._export_controller.show_preview_dialog()

    # ── 画布/预览面板切换 ─────────────────────────────

    def _toggle_preview_view(self) -> None:
        """切换中心区域：画布 ↔ 预览面板，同步切换工具栏和左侧面板。"""
        if self._stack.currentIndex() == 1:
            # 切回画布：保存预览模式的 splitter，恢复画布模式的
            self._preview_splitter_sizes = self._body.sizes()
            self._stack.setCurrentIndex(0)
            self._toolbar_stack.setCurrentIndex(0)
            self._left_panel.show()
            self._tissue_btn.show()
            self._ihc_hotspot_btn.show()
            self._deepliif_btn.hide()
            self._clear_overlay_btn.hide()
            if hasattr(self, '_canvas_splitter_sizes'):
                self._body.setSizes(self._canvas_splitter_sizes)
        else:
            # 切到预览面板：保存画布模式的 splitter，恢复预览模式的
            self._canvas_splitter_sizes = self._body.sizes()
            if self._preview_panel is None:
                self._file_controller.cleanup_stale_rois()
                all_rois = self._roi_manager.all_rois()
                self._preview_panel = ROIPreviewPanel(
                    all_rois, self._readers,
                    toolbar_buttons=(
                        self._preview_select_all_btn,
                        self._preview_deselect_btn,
                        self._preview_invert_btn,
                    ),
                    filter_cb=self._preview_filter_cb,
                    count_label=self._preview_count_label,
                )
                self._preview_panel.roi_selected.connect(self._on_preview_roi_selected)
                self._stack.addWidget(self._preview_panel)
            self._stack.setCurrentIndex(1)
            self._toolbar_stack.setCurrentIndex(1)
            self._left_panel.hide()
            self._tissue_btn.hide()
            self._ihc_hotspot_btn.hide()
            self._deepliif_btn.show()
            if hasattr(self, '_preview_splitter_sizes'):
                self._body.setSizes(self._preview_splitter_sizes)
            # 同步当前选中
            if self._selected_roi_id:
                self._preview_panel.on_roi_selected(self._selected_roi_id)

    def _on_preview_roi_selected(self, roi_id: str) -> None:
        """预览面板选中 ROI → 同步到画布和列表。"""
        self._canvas.select_roi(roi_id)

    def _preview_export_all(self) -> None:
        """预览模式：批量导出当前文件的所有 ROI。"""
        self._file_controller.cleanup_stale_rois()
        if self._current_slide:
            rois = self._roi_manager.get_slide_rois(self._current_slide)
        else:
            rois = self._roi_manager.all_rois()
        if not rois:
            QMessageBox.information(self, "提示", "没有可导出的 ROI")
            return
        self._export_controller.run_export(rois)

    def _preview_export_selected(self) -> None:
        """预览模式：导出预览面板中勾选的 ROI。"""
        if not self._preview_panel:
            return
        selected_ids = set(self._preview_panel.get_selected_ids())
        if not selected_ids:
            QMessageBox.information(self, "提示", "请先勾选要导出的 ROI")
            return
        all_rois = self._roi_manager.all_rois()
        selected_rois = [r for r in all_rois if r.id in selected_ids]
        self._export_controller.run_export(selected_rois)

    def _notify_preview_rois_changed(self) -> None:
        """通知预览面板刷新 ROI 缩略图（防抖）。"""
        if self._preview_panel:
            self._preview_refresh_timer.start()

    def _do_preview_refresh(self) -> None:
        """实际执行预览面板刷新。"""
        if self._preview_panel:
            self._preview_panel.on_rois_changed(
                self._roi_manager.all_rois(), self._readers
            )

    def _run_export(self, rois: list) -> None:
        self._export_controller.run_export(rois)

    def _show_export_progress(self, total: int) -> None:
        self._export_controller._show_export_progress(total)

    def _update_export_progress(self, current: int, total: int) -> None:
        self._export_controller._update_export_progress(current, total)

    def _hide_export_progress(self) -> None:
        self._export_controller.hide_export_progress()

    def _on_export_file_done(self, path: str, status: str) -> None:
        self._export_controller._on_export_file_done(path, status)

    def _cancel_export(self) -> None:
        self._export_controller.cancel_export()

    def _on_cancel_clicked(self) -> None:
        self._export_controller.on_cancel_clicked()

    def _on_export_finished(self) -> None:
        self._export_controller.on_export_finished()

    # ── 会话 ──────────────────────────────────────────

    def _session_path(self) -> Path:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        return SESSION_FILE

    def _save_session(self) -> None:
        data = {
            "rois": self._roi_manager.to_json(),
            "config": {
                "crop_width": self._crop_config.crop_width,
                "crop_height": self._crop_config.crop_height,
                "output_dir": str(self._crop_config.output_dir),
                "theme": self._current_theme,
                "fiji_path": self._imagej_fiji_path,
                "imagej_installed": getattr(self, '_imagej_installed', False),
            },
        }
        try:
            self._session_path().write_text(
                json.dumps(data, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def _load_session(self) -> None:
        path = self._session_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._roi_manager.from_json(data.get("rois", {"rois": []}))
            # 恢复 ROI 到画布（当前已加载的切片）
            if self._current_slide and self._current_slide in self._readers:
                self._refresh_roi_list()
                for roi in self._roi_manager.get_slide_rois(self._current_slide):
                    from PySide6.QtCore import QRectF
                    self._canvas.add_roi_rect(roi.id, QRectF(roi.x, roi.y, roi.w, roi.h),
                                               angle=roi.angle)
            cfg = data.get("config", {})
            self._crop_config = CropConfig(
                output_dir=Path(
                    cfg.get("output_dir", str(Path.home() / "liver_crop_output")),
                ),
                crop_width=cfg.get("crop_width", 1024),
                crop_height=cfg.get("crop_height", 1024),
            )
            self._frame_w_spin.setValue(self._crop_config.crop_width)
            self._frame_h_spin.setValue(self._crop_config.crop_height)
            saved_theme = cfg.get("theme", "dark")
            if saved_theme != self._current_theme:
                self._apply_theme(saved_theme)
                self._theme_action.setText("深色模式" if saved_theme == "light" else "浅色模式")
                self._theme_action.setChecked(saved_theme == "light")
            # 恢复 Fiji 路径
            self._imagej_fiji_path = cfg.get("fiji_path", "")
            # 恢复 PyImageJ 安装标记（避免每次重启都重新检测）
            if cfg.get("imagej_installed", False):
                self._imagej_installed = True
        except Exception:
            pass

    # ── 组织检测 ──────────────────────────────────────

    def _detect_tissue(self) -> None:
        """组织检测 → 对所选文件生成 ROI。"""
        if not self._readers:
            QMessageBox.information(self, "提示", "请先加载切片")
            return

        reader = self._readers.get(self._current_slide) or next(iter(self._readers.values()))
        tile_w = self._frame_w_spin.value()
        tile_h = self._frame_h_spin.value()

        dlg = TissueDialog(reader, tile_w, tile_h, self,
                           readers=self._readers, current_slide=self._current_slide)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        params = dlg.get_params()
        tile_w = params.get("tile_w", tile_w)
        tile_h = params.get("tile_h", tile_h)
        slides = list(self._readers.keys()) if params.get("scope") == "all" else [self._current_slide]

        tissue_kw = {k: v for k, v in params.items()
                     if k in ("open_radius", "close_radius", "fill_holes",
                              "remove_small", "min_area_pct")}

        from PySide6.QtCore import QRectF
        import uuid

        total_all = 0
        for slide_path in slides:
            if slide_path not in self._readers:
                continue
            reader = self._readers[slide_path]
            thumb = reader.thumbnail
            result = detect_tissue(thumb, **tissue_kw)

            scale_x = reader.full_width / thumb.shape[1]
            scale_y = reader.full_height / thumb.shape[0]

            if params.get("mode") == "grid":
                stride = params.get("stride", 2)
                rois_list = tissue_regions_to_rois_grid(
                    result["mask"], scale_x, scale_y, tile_w, tile_h,
                    tile_w * stride, tile_h * stride,
                    max_count=params["max_count"],
                )
            else:
                rois_list = tissue_regions_to_rois(
                    result["mask"], scale_x, scale_y, tile_w, tile_h,
                    max_count=params["max_count"],
                )

            for x, y, w, h in rois_list:
                roi = ROIModel(
                    slide_path=slide_path,
                    x=x, y=y, w=w, h=h, id=uuid.uuid4().hex[:12],
                )
                self._roi_manager.add_roi(roi)

            total_all += len(rois_list)

        # 刷新当前画布显示
        self._canvas.clear_roi_rects()
        if self._current_slide and self._current_slide in self._readers:
            for roi in self._roi_manager.get_slide_rois(self._current_slide):
                self._canvas.add_roi_rect(roi.id, QRectF(roi.x, roi.y, roi.w, roi.h),
                                           angle=roi.angle)

        self._refresh_roi_list()
        self._status_label.setText(f"组织检测: 共 {total_all} 个 ROI")

    # ── IHC 热点检测 ────────────────────────────────────

    def _detect_ihc_hotspot(self) -> None:
        """IHC 阳性热点检测 → 通过回调直接添加 ROI 到画布。"""
        if not self._readers:
            QMessageBox.information(self, "提示", "请先加载切片")
            return

        reader = self._readers.get(self._current_slide) or next(iter(self._readers.values()))
        tile_w = self._frame_w_spin.value()
        tile_h = self._frame_h_spin.value()

        def _add_rois_to_canvas(roi_list: list[tuple[int, int, int, int]]) -> None:
            """回调：将 [(x, y, w, h), ...] 直接添加到 ROI 管理器 + 画布。
            注意：使用 self._current_slide 而非闭包捕获值，防止用户切换切片后过期。
            """
            current = self._current_slide
            cur_reader = self._readers.get(current)
            full_w = getattr(cur_reader, 'full_width', 0) or 0 if cur_reader else 0
            full_h = getattr(cur_reader, 'full_height', 0) or 0 if cur_reader else 0

            created = 0
            for x, y, w, h in roi_list:
                try:
                    x, y, w, h = int(x), int(y), int(w), int(h)
                    if full_w > 0 and full_h > 0:
                        x = max(0, min(x, full_w - 1))
                        y = max(0, min(y, full_h - 1))
                        w = min(w, full_w - x)
                        h = min(h, full_h - y)
                    if w < 1 or h < 1:
                        continue
                    roi = ROIModel(
                        slide_path=current,
                        x=x, y=y, w=w, h=h,
                        id=uuid.uuid4().hex[:12],
                    )
                    self._roi_manager.add_roi(roi)
                    created += 1
                except Exception as exc:
                    logger.warning(
                        "创建 IHC ROI 失败 (%d,%d,%d,%d): %s", x, y, w, h, exc,
                    )

            # 刷新画布
            try:
                self._canvas.clear_roi_rects()
                if current and current in self._readers:
                    for roi in self._roi_manager.get_slide_rois(current):
                        self._canvas.add_roi_rect(
                            roi.id,
                            QRectF(roi.x, roi.y, roi.w, roi.h),
                            angle=roi.angle,
                        )
            except Exception as exc:
                logger.warning("刷新画布 ROI 失败: %s", exc)

            self._refresh_roi_list()
            self._status_label.setText(f"IHC 热点检测: 共 {created} 个 ROI")

        # 如果已有打开的 IHC 对话框，先关闭
        old_dlg = getattr(self, '_ihc_dlg', None)
        if old_dlg is not None and old_dlg.isVisible():
            old_dlg.close()

        dlg = IHCHotspotDialog(
            reader, tile_w, tile_h, self,
            readers=self._readers,
            current_slide=self._current_slide,
            roi_callback=_add_rois_to_canvas,
        )
        self._ihc_dlg = dlg  # 保持引用防止 GC
        dlg.show()

    # ── DeepLIIF 分析 ──────────────────────────────────

    def _on_deepliif_btn_clicked(self) -> None:
        """DeepLIIF 按钮点击 — 根据状态分派。"""
        # 有打开的结果窗口 → 切换显示/隐藏
        if self._active_result_dlg is not None:
            if self._active_result_dlg.isVisible():
                self._active_result_dlg.hide()
            else:
                self._active_result_dlg.show()
                self._active_result_dlg.raise_()
                self._active_result_dlg.activateWindow()
            return

        if self._patch_results:
            self._show_patch_results()
        elif self._deepliif_results:
            self._show_deepliif_results()
        elif self._patch_worker is not None or self._deepliif_worker is not None:
            pass  # 推理进行中，无操作
        else:
            self._run_deepliif()

    def _run_deepliif(self) -> None:
        """启动 DeepLIIF 分析流程。"""
        if not self._readers:
            QMessageBox.information(self, "提示", "请先加载切片")
            return

        all_rois = self._roi_manager.all_rois()
        if not all_rois:
            QMessageBox.information(self, "提示", "请先标注或生成 ROI")
            return

        # 获取当前倍率
        mag_text = self._mag_cb.currentText() if hasattr(self, '_mag_cb') else "40x"

        dlg = DeepLIIFAnalysisDialog(
            rois=all_rois,
            readers=self._readers,
            current_slide=self._current_slide,
            magnification=mag_text,
            parent=self,
        )
        dlg.confirmed.connect(lambda: self._on_deepliif_confirmed(dlg))
        dlg.patch_confirmed.connect(lambda: self._on_patch_confirmed(dlg))
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.show()

    def _on_deepliif_confirmed(self, dlg: DeepLIIFAnalysisDialog):
        """分析对话框确认 — 启动后台推理。"""
        params = getattr(dlg, '_confirmed_params', None)
        selected_rois = getattr(dlg, '_confirmed_rois', None)
        if not params or not selected_rois:
            return

        try:
            self._start_deepliif_worker(params, selected_rois)
        except Exception as e:
            logger.error("启动 DeepLIIF 分析失败: %s", e, exc_info=True)
            QMessageBox.critical(self, "DeepLIIF 启动失败", str(e))

    def _start_deepliif_worker(self, params: dict, selected_rois: list):
        """创建并启动 DeepLIIF 推理线程。"""
        self._deepliif_results = None
        if params["mode"] == "local":
            mode = DeepLIIFMode.LOCAL
            model_dir = params["model_dir"]
            if not model_dir:
                QMessageBox.warning(self, "错误", "本地模式需要指定模型目录")
                return
            ok, msg = check_model_available(model_dir)
            if not ok:
                QMessageBox.warning(self, "模型不可用", msg)
                return
        else:
            mode = DeepLIIFMode.CLOUD
            model_dir = None

        # 显示进度（在预览工具栏上，因为 DeepLIIF 只在预览模式可用）
        self._preview_progress.setRange(0, len(selected_rois))
        self._preview_progress.setValue(0)
        self._preview_progress.setFormat(f"0/{len(selected_rois)}")
        self._preview_progress.show()
        self._preview_cancel_btn.show()
        self._deepliif_btn.setText("分析中...")
        self._deepliif_btn.setToolTip("DeepLIIF 批量推理进行中…")
        self._status_label.setText("DeepLIIF 分析中...")

        # 创建 Worker 和线程（不能有 parent，否则无法 moveToThread）
        self._deepliif_worker = DeepLIIFWorker(
            mode=mode,
            rois=selected_rois,
            readers=self._readers,
            model_dir=model_dir,
            tile_size=params["tile_size"],
            seg_only=params["seg_only"],
        )
        self._deepliif_thread = QThread()
        self._deepliif_worker.moveToThread(self._deepliif_thread)

        # 信号连接
        self._deepliif_thread.started.connect(self._deepliif_worker.run)
        self._deepliif_worker.progress.connect(self._on_deepliif_progress)
        self._deepliif_worker.all_finished.connect(self._on_deepliif_finished)
        self._deepliif_worker.error.connect(self._on_deepliif_error)
        self._deepliif_worker.all_finished.connect(self._deepliif_cleanup)
        self._deepliif_worker.error.connect(self._deepliif_cleanup)

        # 设置取消委托
        self._cancel_op = lambda: self._deepliif_worker.cancel() if self._deepliif_worker else None

        self._deepliif_thread.start()

    def _on_deepliif_progress(self, msg: str, current: int, total: int):
        """DeepLIIF 推理进度更新。"""
        self._status_label.setText(msg)
        self._preview_progress.setMaximum(total)
        self._preview_progress.setValue(current)
        self._deepliif_btn.setText(f"{current}/{total}")

    def _deepliif_cleanup(self):
        """线程结束后清理 worker 和 thread。"""
        thread = getattr(self, '_deepliif_thread', None)
        worker = getattr(self, '_deepliif_worker', None)
        if thread is not None:
            thread.quit()
            thread.wait(3000)
        if worker is not None:
            worker.deleteLater()
        if thread is not None:
            thread.deleteLater()
        self._deepliif_thread = None
        self._deepliif_worker = None

    def _on_deepliif_finished(self, results: list):
        """DeepLIIF 推理完成 — 缓存结果，更新按钮。"""
        self._patch_results = None
        self._deepliif_btn.setEnabled(True)
        self._preview_progress.hide()
        self._preview_cancel_btn.hide()
        self._cancel_op = None

        if not results:
            self._deepliif_btn.setText("DeepLIIF 分析")
            self._deepliif_btn.setToolTip("使用 DeepLIIF 进行 IHC 染色分析和细胞分割")
            self._status_label.setText("DeepLIIF 分析: 无结果")
            return

        self._deepliif_results = results
        self._deepliif_btn.setText("查看分析结果")
        self._deepliif_btn.setToolTip("点击打开 DeepLIIF 分析结果")
        self._status_label.setText(f"DeepLIIF 分析完成: {len(results)} 个 ROI — 点击按钮查看结果")

    def _on_deepliif_error(self, msg: str):
        """DeepLIIF 推理错误。"""
        self._patch_results = None
        self._deepliif_results = None
        self._deepliif_btn.setEnabled(True)
        self._deepliif_btn.setText("DeepLIIF 分析")
        self._deepliif_btn.setToolTip("使用 DeepLIIF 进行 IHC 染色分析和细胞分割")
        self._preview_progress.hide()
        self._preview_cancel_btn.hide()
        self._cancel_op = None
        self._status_label.setText("DeepLIIF 分析出错")
        QMessageBox.warning(self, "DeepLIIF 错误", msg)

    def _show_deepliif_results(self):
        """打开缓存的批量分析结果对话框。"""
        if not self._deepliif_results:
            return
        tile_size = self._deepliif_results[0].get("tile_size", 512)
        dlg = DeepLIIFResultsDialog(
            self._deepliif_results, tile_size=tile_size, parent=self,
        )
        dlg.overlay_requested.connect(self._apply_overlay_to_canvas)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._active_result_dlg = dlg
        dlg.show()
        # 保存引用防止 GC
        self._deepliif_result_dialogs = getattr(self, '_deepliif_result_dialogs', [])
        self._deepliif_result_dialogs.append(dlg)
        # 对话框关闭后：移除引用，若无剩余则重置按钮
        def _on_closed():
            if dlg in self._deepliif_result_dialogs:
                self._deepliif_result_dialogs.remove(dlg)
            if self._active_result_dlg is dlg:
                self._active_result_dlg = None
            if not self._deepliif_result_dialogs:
                self._deepliif_results = None
                self._deepliif_btn.setText("DeepLIIF 分析")
                self._deepliif_btn.setToolTip("使用 DeepLIIF 进行 IHC 染色分析和细胞分割")
        dlg.destroyed.connect(_on_closed)

    # ── ImageJ 桥接 ─────────────────────────────────────

    def _get_selected_roi_image(self) -> tuple | None:
        """从预览面板获取第一个勾选 ROI 的 numpy 图像。

        Returns:
            (ROIModel, numpy_array) 或 None
        """
        if not self._preview_panel:
            return None

        selected_ids = self._preview_panel.get_selected_ids()
        if not selected_ids:
            # 没有勾选，尝试取当前 ROI 列表选中的
            if self._selected_roi_id:
                selected_ids = [self._selected_roi_id]
            else:
                return None

        roi_id = selected_ids[0]
        roi = next(
            (r for r in self._roi_manager.all_rois() if r.id == roi_id), None
        )
        if roi is None:
            return None

        reader = self._readers.get(roi.slide_path)
        if reader is None:
            return None

        try:
            # 使用与 exporter.py 相同的裁剪逻辑
            from liver_portal_crop.utils import center_crop_rect
            cx = roi.x + roi.w // 2
            cy = roi.y + roi.h // 2
            crop_x, crop_y, crop_w, crop_h = center_crop_rect(
                cx, cy, roi.w, roi.h, reader.full_width, reader.full_height,
            )
            region = reader.extract_region(crop_x, crop_y, crop_w, crop_h, level=0)
            return roi, region
        except Exception as e:
            logger.error("提取 ROI 图像失败: %s", e)
            return None

    def _ensure_imagej_available(self) -> bool:
        """检查 PyImageJ 是否已安装。未安装则弹出安装对话框。

        Returns:
            True  = 可用，可立即执行后续操作
            False = 不可用（用户拒绝安装 或 安装已启动需等待完成后再试）
        """
        from liver_portal_crop.imagej_bridge import check_imagej_available

        # 如果有缓存的成功标记，直接返回
        if getattr(self, '_imagej_installed', False):
            return True

        available, message = check_imagej_available()
        if available:
            self._imagej_installed = True
            # 首次检测到已安装时也持久化，后续启动跳过检测
            self._save_session()
            return True

        # 弹出安装对话框
        reply = QMessageBox.question(
            self, "PyImageJ 未安装",
            f"ImageJ 分析功能需要安装 PyImageJ 及其依赖。\n\n"
            f"{message}\n\n"
            "是否自动安装？\n"
            "（需要网络连接，约需 2-5 分钟）",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )

        if reply != QMessageBox.StandardButton.Yes:
            return False

        # 异步安装，安装完成后可重新点击按钮
        self._do_install_imagej()
        return False

    def _do_install_imagej(self) -> None:
        """后台安装 PyImageJ 依赖，不阻塞主 UI。"""
        # 禁用相关按钮，防止重复点击
        if hasattr(self, '_imagej_tune_btn'):
            self._imagej_tune_btn.setEnabled(False)
        if hasattr(self, '_imagej_batch_btn'):
            self._imagej_batch_btn.setEnabled(False)

        # 状态栏提示
        self._status_label.setText("🔧 正在后台安装 PyImageJ 依赖，安装完成后可继续使用 ImageJ 功能…")

        # 后台线程
        self._install_thread = QThread()
        self._install_worker = _ImageJInstallWorker()
        self._install_worker.moveToThread(self._install_thread)

        self._install_thread.started.connect(self._install_worker.run)
        self._install_worker.progress.connect(
            lambda msg: self._status_label.setText(f"🔧 {msg}")
        )
        self._install_worker.finished.connect(self._on_install_finished)
        self._install_worker.finished.connect(self._install_thread.quit)
        self._install_worker.finished.connect(self._install_worker.deleteLater)
        self._install_thread.finished.connect(self._install_thread.deleteLater)
        self._install_thread.start()

    def _on_install_finished(self, ok: bool, msg: str) -> None:
        """安装完成回调。"""
        # 恢复按钮
        if hasattr(self, '_imagej_tune_btn'):
            self._imagej_tune_btn.setEnabled(True)
        if hasattr(self, '_imagej_batch_btn'):
            self._imagej_batch_btn.setEnabled(True)

        if ok:
            # 直接标记已安装，避免 find_spec 缓存问题
            self._imagej_installed = True
            # 持久化到 session，避免下次重启重新检测
            self._save_session()

            self._status_label.setText("✅ PyImageJ 安装成功，可以使用 ImageJ 功能了")
            QMessageBox.information(
                self, "安装成功",
                "PyImageJ 依赖已安装成功。\n\n"
                "现在可以使用 ImageJ 调参和批量分析功能。\n\n"
                " 提示：如需使用本地 Fiji，请在「分析」菜单中\n"
                "选择「下载 Fiji」或「设置 Fiji 路径」。",
            )
        else:
            self._status_label.setText("❌ PyImageJ 安装失败")
            QMessageBox.critical(
                self, "安装失败",
                f"PyImageJ 安装失败:\n\n{msg}\n\n"
                "请手动安装:\n"
                "pip install imagej scyjava\n\n"
                "并确保系统已安装 JDK 17+:\n"
                "https://adoptium.net/",
            )

    def _on_imagej_tune_clicked(self) -> None:
        """ImageJ 调参 — 在独立子进程中启动 Fiji GUI 对选中 ROI 进行交互式调参。"""
        if not self._ensure_imagej_available():
            return

        result = self._get_selected_roi_image()
        if result is None:
            QMessageBox.information(
                self, "提示",
                "请先在预览面板中勾选或选中一个 ROI，再点击 ImageJ 调参。\n\n"
                "操作步骤：\n"
                "1. 点击「ROI 预览」切换到预览模式\n"
                "2. 单击某个 ROI 缩略图（或勾选）\n"
                "3. 点击「ImageJ 调参」",
            )
            return

        roi, region = result

        # 检查 Fiji 路径
        fiji_path = self._imagej_fiji_path
        if not fiji_path:
            QMessageBox.information(
                self, "设置 Fiji 路径",
                "首次使用请先设置 Fiji 安装路径。\n"
                "点击「分析」菜单 →「设置 Fiji 路径...」",
            )
            self._set_fiji_path()
            fiji_path = self._imagej_fiji_path
            if not fiji_path:
                return

        # 将 numpy 图像写入临时 TIFF
        import tempfile
        import tifffile
        tmp_dir = Path(tempfile.gettempdir()) / "imagej_bridge"
        tmp_dir.mkdir(exist_ok=True)
        tmp_path = tmp_dir / f"{roi.id}_tune.tiff"
        tifffile.imwrite(str(tmp_path), region)

        # 配置保存路径
        config_path = self._imagej_config_path
        roi_label = f"{roi.slide_path.stem}_ROI_{roi.id[:8]}"

        # 构造子进程命令 — 在主进程中启动 PyImageJ GUI
        import os
        import pathlib

        # 将参数保存为实例变量，供后续使用
        self._tuning_image = region
        self._tuning_roi_label = roi_label
        self._tuning_config_path = config_path
        self._tuning_fiji_path = fiji_path

        # 在主线程的 QThread 中启动 PyImageJ GUI
        self._start_imagej_gui_tuning()

    def _start_imagej_gui_tuning(self) -> None:
        """在 QThread 中启动 PyImageJ GUI 调参。"""
        from PySide6.QtCore import QObject, Signal as QSignal, QThread
        from PySide6.QtWidgets import QMessageBox
        import threading

        # 用于跨线程同步用户确认
        user_confirmed_event = threading.Event()
        user_confirmed_result = [True]  # [bool] 存储结果

        class _ImageJGuiTuningWorker(QObject):
            """在 QThread 中初始化 PyImageJ GUI 并等待用户操作完成。"""
            fiji_started = QSignal(str)       # Fiji 已启动消息
            show_confirm_dialog = QSignal()   # 请求主线程显示确认对话框
            config_saved = QSignal(str, str)  # config_path, summary
            error = QSignal(str)

            def __init__(self, image, roi_label, config_path, fiji_path):
                super().__init__()
                self._image = image
                self._roi_label = roi_label
                self._config_path = config_path
                self._fiji_path = fiji_path

            def run(self) -> None:
                import traceback
                import liver_portal_crop.imagej_bridge as ij_bridge
                from liver_portal_crop.imagej_bridge import launch_gui_tuning

                # 覆盖等待钩子：通过信号让主线程显示对话框
                original_wait = ij_bridge._wait_for_user
                def qt_wait():
                    # 通过信号请求主线程显示对话框
                    user_confirmed_event.clear()
                    self.show_confirm_dialog.emit()
                    # 阻塞等待用户确认
                    user_confirmed_event.wait()
                    if not user_confirmed_result[0]:
                        raise KeyboardInterrupt("用户取消了配置保存")

                ij_bridge._wait_for_user = qt_wait

                try:
                    self.fiji_started.emit(self._roi_label)

                    # launch_gui_tuning 会启动 Fiji GUI，然后通过信号等待用户确认
                    config = launch_gui_tuning(
                        sample_image=self._image,
                        fiji_path=self._fiji_path,
                        title=f"调参 — {self._roi_label}",
                        config_save_path=str(self._config_path),
                    )

                    summary = (
                        f"配置名称: {config.config_name}\n"
                        f"阈值方法: {config.threshold_method}\n"
                        f"粒子大小: {config.particle_min_size} - {config.particle_max_size}\n"
                        f"形态学: {config.morph_operation} (r={config.morph_radius})"
                    )
                    self.config_saved.emit(str(self._config_path), summary)

                except KeyboardInterrupt:
                    self.config_saved.emit("", "用户取消了配置保存")
                except Exception as e:
                    self.error.emit(f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
                finally:
                    ij_bridge._wait_for_user = original_wait

        self._imagej_tune_btn.setEnabled(False)
        self._imagej_tune_btn.setText("Fiji 启动中...")

        self._tuning_thread = QThread()
        self._tuning_worker = _ImageJGuiTuningWorker(
            self._tuning_image,
            self._tuning_roi_label,
            self._tuning_config_path,
            self._tuning_fiji_path,
        )
        self._tuning_worker.moveToThread(self._tuning_thread)

        self._tuning_thread.started.connect(self._tuning_worker.run)
        self._tuning_worker.fiji_started.connect(self._on_tuning_fiji_started)
        self._tuning_worker.show_confirm_dialog.connect(
            lambda: self._show_tuning_confirm_dialog(user_confirmed_event, user_confirmed_result)
        )
        self._tuning_worker.config_saved.connect(self._on_tuning_config_saved)
        self._tuning_worker.error.connect(self._on_tuning_error)

        for sig in (self._tuning_worker.config_saved, self._tuning_worker.error):
            sig.connect(self._tuning_thread.quit)
        self._tuning_worker.error.connect(self._tuning_worker.deleteLater)
        self._tuning_worker.config_saved.connect(self._tuning_worker.deleteLater)
        self._tuning_thread.finished.connect(self._tuning_thread.deleteLater)
        self._tuning_thread.start()

    def _show_tuning_confirm_dialog(self, event, result_holder):
        """在主线程中显示调参确认对话框。"""
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton

        dlg = QDialog(self)
        dlg.setWindowTitle("Fiji 调参")
        dlg.setMinimumWidth(450)
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel(
            "请在 Fiji 中完成调参操作。\n\n"
            "• Image > Adjust > Threshold → 调阈值\n"
            "• Process > Filters / Binary → 形态学\n"
            "• Analyze > Analyze Particles → 粒子分析\n"
            "• Plugins > Macros > Record → 录制宏\n\n"
            "完成后点击「保存配置」，或点击「取消」放弃。"
        ))
        btn_layout = QHBoxLayout()
        save_btn = QPushButton("保存配置")
        save_btn.setDefault(True)
        save_btn.setObjectName("primaryBtn")
        cancel_btn = QPushButton("取消")
        btn_layout.addStretch()
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(save_btn)
        layout.addLayout(btn_layout)

        def on_save():
            result_holder[0] = True
            event.set()
            dlg.accept()

        def on_cancel():
            result_holder[0] = False
            event.set()
            dlg.reject()

        save_btn.clicked.connect(on_save)
        cancel_btn.clicked.connect(on_cancel)
        dlg.exec()

    def _on_tuning_fiji_started(self, roi_label: str) -> None:
        self._imagej_tune_btn.setText("Fiji 运行中...")
        QMessageBox.information(
            self, "Fiji 已启动",
            f"Fiji 已启动，图像: {roi_label}\n\n"
            f"请在 Fiji 中进行调参操作：\n"
            f"  • Image > Adjust > Threshold → 调阈值\n"
            f"  • Process > Filters / Binary → 形态学\n"
            f"  • Analyze > Analyze Particles → 粒子分析\n"
            f"  • Plugins > Macros > Record → 录制宏（推荐）\n\n"
            f"操作完成后，回到此窗口点击「确定」保存配置。",
        )

    def _on_tuning_config_saved(self, config_path: str, summary: str) -> None:
        self._imagej_tune_btn.setEnabled(True)
        self._imagej_tune_btn.setText("ImageJ 调参")

        if not config_path:
            # 用户取消了
            self._status_label.setText("ImageJ 调参已取消")
            return

        QMessageBox.information(
            self, "调参完成",
            f"ImageJ 调参已完成，配置已保存。\n\n"
            f"配置文件: {config_path}\n\n"
            f"{summary}\n\n"
            f"现在可以使用「ImageJ 批量分析」\n"
            f"对所有 ROI 执行批量分析。",
        )

    def _on_tuning_error(self, message: str) -> None:
        self._imagej_tune_btn.setEnabled(True)
        self._imagej_tune_btn.setText("ImageJ 调参")
        QMessageBox.critical(self, "调参失败", f"ImageJ 调参过程中出错:\n\n{message}")

    def _check_imagej_subprocess(self) -> None:
        """定时检查 ImageJ GUI 调参子进程是否完成。"""
        if self._imagej_subprocess is None:
            self._imagej_check_timer.stop()
            return

        ret = self._imagej_subprocess.poll()
        if ret is not None:
            # 子进程已退出
            self._imagej_check_timer.stop()
            self._imagej_subprocess = None
            self._imagej_tune_btn.setEnabled(True)
            self._imagej_tune_btn.setText("ImageJ 调参")

            if ret == 0 and self._imagej_config_path.exists():
                QMessageBox.information(
                    self, "调参完成",
                    f"ImageJ 调参已完成，配置已保存。\n\n"
                    f"配置文件: {self._imagej_config_path}\n\n"
                    "现在可以点击「ImageJ 批量分析」\n"
                    "对所有 ROI 执行批量分析。",
                )
            else:
                # 读取错误日志
                error_log = getattr(self, '_imagej_stderr_log', None)
                error_detail = ""
                if error_log and os.path.exists(error_log.name):
                    try:
                        with open(error_log.name, "r", encoding="utf-8", errors="replace") as f:
                            error_detail = f.read().strip()
                    except Exception:
                        pass

                msg = f"ImageJ 调参进程已退出 (返回码: {ret})。"
                if error_detail:
                    msg += f"\n\n错误信息:\n{error_detail[-1000:]}"
                QMessageBox.warning(self, "调参结束", msg)

    def _on_imagej_batch_clicked(self) -> None:
        """ImageJ 批量分析 — headless 模式对所有 ROI 执行 ImageJ 分析。"""
        if not self._ensure_imagej_available():
            return

        # 检查配置
        config_path = self._imagej_config_path
        if not config_path.exists():
            QMessageBox.information(
                self, "无配置文件",
                f"未找到 ImageJ 分析配置:\n{config_path}\n\n"
                "请先使用「ImageJ 调参」进行交互式调参，\n"
                "或手动创建配置文件后重试。",
            )
            return

        # 收集所有 ROI
        self._cleanup_stale_rois()
        all_rois = self._roi_manager.all_rois()
        if not all_rois:
            QMessageBox.information(self, "提示", "请先标注或生成 ROI")
            return

        # 询问用户：分析全部还是选中
        if self._preview_panel:
            selected_ids = set(self._preview_panel.get_selected_ids())
        else:
            selected_ids = set()

        if selected_ids:
            rois = [r for r in all_rois if r.id in selected_ids]
            scope_msg = f"选中的 {len(rois)} 个 ROI"
        else:
            rois = all_rois
            scope_msg = f"全部 {len(rois)} 个 ROI"

        reply = QMessageBox.question(
            self, "ImageJ 批量分析",
            f"即将对 {scope_msg} 执行 headless ImageJ 分析。\n\n"
            f"配置文件: {config_path}\n\n"
            "这可能需要较长时间，确认开始？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # 提取所有 ROI 的 numpy 图像
        images = []
        for roi in rois:
            reader = self._readers.get(roi.slide_path)
            if reader is None:
                continue
            try:
                from liver_portal_crop.utils import center_crop_rect
                cx = roi.x + roi.w // 2
                cy = roi.y + roi.h // 2
                crop_x, crop_y, crop_w, crop_h = center_crop_rect(
                    cx, cy, roi.w, roi.h, reader.full_width, reader.full_height,
                )
                region = reader.extract_region(
                    crop_x, crop_y, crop_w, crop_h, level=0,
                )
                roi_id = f"{roi.slide_path.stem}_ROI_{roi.id[:8]}"
                images.append((roi_id, region))
            except Exception as e:
                logger.warning("提取 ROI 图像失败 %s: %s", roi.id, e)

        if not images:
            QMessageBox.warning(self, "错误", "无法提取任何 ROI 图像")
            return

        # 输出目录
        output_dir = self._crop_config.output_dir / "imagej_results"

        # 显示进度条
        self._preview_progress.show()
        self._preview_progress.setRange(0, len(images))
        self._preview_progress.setValue(0)
        self._preview_progress.setFormat("ImageJ 分析: %v/%m")
        self._imagej_batch_btn.setEnabled(False)
        self._imagej_batch_btn.setText("分析中...")

        # 在 QThread 中运行 headless 批量分析
        self._imagej_batch_thread = QThread()
        self._imagej_batch_worker = _ImageJBatchWorker(
            images=images,
            config_path=str(config_path),
            output_dir=str(output_dir),
            fiji_path=self._imagej_fiji_path or None,
        )
        self._imagej_batch_worker.moveToThread(self._imagej_batch_thread)
        self._imagej_batch_thread.started.connect(self._imagej_batch_worker.run)
        self._imagej_batch_worker.progress.connect(self._on_imagej_batch_progress)
        self._imagej_batch_worker.finished.connect(self._on_imagej_batch_done)
        self._imagej_batch_worker.error.connect(self._on_imagej_batch_error)
        self._imagej_batch_worker.finished.connect(self._imagej_batch_thread.quit)
        self._imagej_batch_worker.error.connect(self._imagej_batch_thread.quit)
        self._imagej_batch_worker.finished.connect(
            self._imagej_batch_worker.deleteLater
        )
        self._imagej_batch_worker.error.connect(
            self._imagej_batch_worker.deleteLater
        )
        self._imagej_batch_thread.finished.connect(
            self._imagej_batch_thread.deleteLater
        )
        self._imagej_batch_thread.start()

    def _on_imagej_batch_progress(self, current: int, total: int) -> None:
        """ImageJ 批量分析进度更新。"""
        self._preview_progress.setMaximum(total)
        self._preview_progress.setValue(current)

    def _on_imagej_batch_done(self, csv_path: str, summary: str) -> None:
        """ImageJ 批量分析完成。"""
        self._preview_progress.hide()
        self._imagej_batch_btn.setEnabled(True)
        self._imagej_batch_btn.setText("ImageJ 批量分析")
        self._imagej_batch_thread = None
        self._imagej_batch_worker = None

        QMessageBox.information(
            self, "ImageJ 分析完成",
            f"批量分析已完成。\n\n{summary}\n\nCSV 结果: {csv_path}",
        )

    def _on_imagej_batch_error(self, message: str) -> None:
        """ImageJ 批量分析出错。"""
        self._preview_progress.hide()
        self._imagej_batch_btn.setEnabled(True)
        self._imagej_batch_btn.setText("ImageJ 批量分析")
        self._imagej_batch_thread = None
        self._imagej_batch_worker = None

        QMessageBox.critical(self, "ImageJ 分析失败", f"批量分析出错:\n{message}")

    def _set_fiji_path(self) -> None:
        """弹出对话框选择本地 Fiji 安装路径。"""
        from PySide6.QtWidgets import QFileDialog

        path = QFileDialog.getExistingDirectory(
            self,
            "选择 Fiji.app 安装目录",
            self._imagej_fiji_path or str(Path.home()),
        )
        if not path:
            return

        # 验证路径看起来像 Fiji
        fiji_path = Path(path)
        if not (fiji_path / "ImageJ-win64.exe").exists() and \
           not (fiji_path / "jars" / "ij.jar").exists() and \
           not (fiji_path / "ImageJ.exe").exists():
            reply = QMessageBox.question(
                self, "路径验证",
                f"该目录未检测到 Fiji 可执行文件:\n{path}\n\n"
                "确认仍要使用此路径？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        self._imagej_fiji_path = str(fiji_path)
        logger.info("Fiji 路径已设置: %s", self._imagej_fiji_path)

        QMessageBox.information(
            self, "设置成功",
            f"Fiji 路径已设置:\n{self._imagej_fiji_path}",
        )

    def _download_fiji(self) -> None:
        """自动下载 Fiji 到本地目录。"""
        target = _FijiDownloadWorker._TARGET_DIR
        if target.exists():
            QMessageBox.information(
                self, "Fiji 已存在",
                f"Fiji 已下载到: {target}\n\n如需重新下载，请先删除该目录。\n"
                f"如需使用其他路径，请在「分析」菜单中「设置 Fiji 路径」。",
            )
            return

        reply = QMessageBox.question(
            self, "下载 Fiji",
            "将从官方服务器下载 Fiji (约 300MB) 到:\n"
            f"{target}\n\n"
            "下载完成后将自动解压并设置为默认 Fiji 路径。\n\n"
            "是否开始下载？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # 禁用按钮
        self._imagej_tune_btn.setEnabled(False)
        self._imagej_batch_btn.setEnabled(False)
        self._status_label.setText(" 正在下载 Fiji，请稍候…")

        self._fiji_download_thread = QThread()
        self._fiji_download_worker = _FijiDownloadWorker()
        self._fiji_download_worker.moveToThread(self._fiji_download_thread)

        self._fiji_download_thread.started.connect(self._fiji_download_worker.run)
        self._fiji_download_worker.progress.connect(
            lambda msg: self._status_label.setText(f"📦 {msg}")
        )
        self._fiji_download_worker.finished.connect(self._on_fiji_download_finished)
        self._fiji_download_worker.finished.connect(self._fiji_download_thread.quit)
        self._fiji_download_worker.finished.connect(self._fiji_download_worker.deleteLater)
        self._fiji_download_thread.finished.connect(self._fiji_download_thread.deleteLater)
        self._fiji_download_thread.start()

    def _on_fiji_download_finished(self, ok: bool, msg: str) -> None:
        """Fiji 下载完成回调。"""
        # 恢复按钮
        self._imagej_tune_btn.setEnabled(True)
        self._imagej_batch_btn.setEnabled(True)

        if ok:
            self._imagej_fiji_path = msg
            self._status_label.setText(f"✅ Fiji 已下载到: {msg}")
            QMessageBox.information(
                self, "下载成功",
                f"Fiji 已下载并解压到:\n{msg}\n\n"
                "现在可以在预览面板中使用 ImageJ 调参和批量分析功能。",
            )
        else:
            self._status_label.setText("❌ Fiji 下载失败")
            QMessageBox.critical(
                self, "下载失败",
                f"Fiji 下载失败:\n\n{msg}\n\n"
                "请尝试手动下载 Fiji:\n"
                "https://imagej.net/software/fiji/downloads\n\n"
                "或在「分析」菜单中手动设置 Fiji 路径。",
            )

    def _install_imagej_from_menu(self) -> None:
        """菜单入口：检查 / 安装 PyImageJ 依赖。"""
        from liver_portal_crop.imagej_bridge import check_imagej_available

        available, message = check_imagej_available()
        if available:
            QMessageBox.information(
                self, "PyImageJ 已就绪",
                f"PyImageJ 及其依赖已安装。\n\n{message}",
            )
            return

        # 未安装 → 弹出安装确认
        reply = QMessageBox.question(
            self, "安装 PyImageJ",
            f"检测到以下问题:\n\n{message}\n\n"
            "是否自动安装 PyImageJ 核心包？\n"
            "（需要网络连接，约需 2-5 分钟）\n\n"
            "⚠️ 安装完成后还需确保系统有 JDK 17+。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )

        if reply == QMessageBox.StandardButton.Yes:
            self._do_install_imagej()

    def _save_imagej_config(self) -> None:
        """弹出对话框，让用户手动输入 ImageJ 分析配置参数。"""
        from PySide6.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout, QLabel,
            QLineEdit, QPushButton, QTextEdit, QFormLayout, QSpinBox,
        )
        from PySide6.QtCore import Qt

        dlg = QDialog(self)
        dlg.setWindowTitle("保存 ImageJ 分析配置")
        dlg.setMinimumWidth(500)
        dlg.setMinimumHeight(400)

        layout = QVBoxLayout(dlg)

        # 配置名称
        form = QFormLayout()
        name_edit = QLineEdit("IHC_DAB_默认")
        form.addRow("配置名称:", name_edit)

        # 阈值方法
        threshold_edit = QLineEdit("Default")
        threshold_edit.setToolTip("Default / Otsu / Li / Huang 等")
        form.addRow("阈值方法:", threshold_edit)

        # 粒子最小大小
        min_size = QSpinBox()
        min_size.setRange(1, 100000)
        min_size.setValue(50)
        form.addRow("最小粒子面积:", min_size)

        # 粒子最大大小
        max_size = QSpinBox()
        max_size.setRange(1, 1000000)
        max_size.setValue(50000)
        form.addRow("最大粒子面积:", max_size)

        # 形态学操作
        morph_edit = QLineEdit("open")
        morph_edit.setToolTip("open / close / dilate / erode / fill_holes")
        form.addRow("形态学操作:", morph_edit)

        # 形态学半径
        morph_radius = QSpinBox()
        morph_radius.setRange(1, 50)
        morph_radius.setValue(2)
        form.addRow("形态学半径:", morph_radius)

        layout.addLayout(form)

        # 宏文本（可选）
        layout.addWidget(QLabel("宏脚本（可选，从 Fiji Macro Recorder 复制）:"))
        macro_edit = QTextEdit()
        macro_edit.setPlaceholderText("Plugins > Macros > Record 后复制粘贴到这里...")
        macro_edit.setMaximumHeight(150)
        layout.addWidget(macro_edit)

        # 按钮
        btn_layout = QHBoxLayout()
        save_btn = QPushButton("保存")
        save_btn.setDefault(True)
        cancel_btn = QPushButton("取消")
        btn_layout.addWidget(save_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

        save_btn.clicked.connect(dlg.accept)
        cancel_btn.clicked.connect(dlg.reject)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            from liver_portal_crop.imagej_bridge import AnalysisConfig
            import time

            config = AnalysisConfig(
                config_name=name_edit.text() or "自定义配置",
                description="手动创建的配置",
                threshold_method=threshold_edit.text() or "Default",
                auto_threshold=True,
                morph_operation=morph_edit.text() or "open",
                morph_radius=morph_radius.value(),
                morph_iterations=1,
                particle_min_size=min_size.value(),
                particle_max_size=max_size.value(),
                particle_circularity_min=0.1,
                particle_circularity_max=1.0,
                macro_text=macro_edit.toPlainText(),
                measurements=[
                    "Area", "Mean", "StdDev", "Min", "Max",
                    "IntegratedDensity", "Circularity", "Feret",
                ],
            )

            config_path = self._imagej_config_path
            config.created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
            config.save(config_path)

            QMessageBox.information(
                self, "配置已保存",
                f"ImageJ 分析配置已保存到:\n{config_path}\n\n"
                f"配置名称: {config.config_name}\n"
                f"阈值方法: {config.threshold_method}\n"
                f"粒子大小: {config.particle_min_size} - {config.particle_max_size}\n\n"
                f"现在可以使用「ImageJ 批量分析」功能了。",
            )

    # ── 小块测试（主窗口生命周期管理）──────────────────────

    def _on_patch_confirmed(self, dlg: DeepLIIFAnalysisDialog):
        """小块测试对话框确认 — 启动后台推理。"""
        patch_data = getattr(dlg, '_patch_data', None)
        if not patch_data:
            return
        try:
            self._start_patch_test(patch_data)
        except Exception as e:
            logger.error("启动小块测试失败: %s", e, exc_info=True)
            QMessageBox.critical(self, "小块测试启动失败", str(e))

    def _start_patch_test(self, data: dict):
        """创建并启动小块测试推理线程。"""
        from PySide6.QtCore import QObject, Signal as QSignal

        self._patch_results = None
        self._deepliif_btn.setText("小块测试中...")
        self._deepliif_btn.setToolTip("小块推理进行中…")
        self._status_label.setText("小块测试推理中...")

        self._patch_thread = QThread()

        self._patch_worker = _PatchWorker(
            patch=data["patch"],
            mode=data["mode"],
            model_dir=data["model_dir"],
            tile_size=data["tile_size"],
            seg_only=data["seg_only"],
            patch_roi=data["patch_roi"],
        )
        self._patch_worker.moveToThread(self._patch_thread)
        self._patch_thread.started.connect(self._patch_worker.run)
        self._patch_worker.finished.connect(self._on_patch_done)
        self._patch_worker.error.connect(self._on_patch_error)
        self._patch_worker.finished.connect(self._patch_test_cleanup)
        self._patch_worker.error.connect(self._patch_test_cleanup)
        self._cancel_op = lambda: None  # 小块测试暂不支持取消
        self._patch_thread.start()

    def _on_patch_done(self, result: dict):
        """小块推理完成 — 缓存结果，更新按钮。"""
        self._patch_results = [result]
        self._deepliif_btn.setText("查看小块结果")
        self._deepliif_btn.setToolTip("点击重新打开小块测试结果")
        self._status_label.setText("小块测试完成 — 点击按钮查看结果")
        self._cancel_op = None

    def _on_patch_error(self, msg: str):
        """小块推理失败。"""
        self._deepliif_btn.setText("DeepLIIF 分析")
        self._deepliif_btn.setToolTip("使用 DeepLIIF 进行 IHC 染色分析和细胞分割")
        self._status_label.setText("小块测试出错")
        self._cancel_op = None
        QMessageBox.warning(self, "小块测试失败", msg)

    def _patch_test_cleanup(self):
        """小块测试线程结束后清理。"""
        thread = self._patch_thread
        worker = self._patch_worker
        if thread is not None:
            thread.quit()
            thread.wait(3000)
        if worker is not None:
            worker.deleteLater()
        if thread is not None:
            thread.deleteLater()
        self._patch_thread = None
        self._patch_worker = None

    def _show_patch_results(self):
        """打开缓存的小块测试结果对话框。"""
        if not self._patch_results:
            return
        tile_size = self._patch_results[0].get("tile_size", 512)
        dlg = DeepLIIFResultsDialog(
            self._patch_results, tile_size=tile_size, parent=self,
        )
        dlg.setWindowTitle("小块测试 — 调好参数后关闭，再点「开始分析」批量处理")
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._active_result_dlg = dlg
        dlg.show()
        # 保存引用防止 GC
        self._deepliif_result_dialogs = getattr(self, '_deepliif_result_dialogs', [])
        self._deepliif_result_dialogs.append(dlg)
        # 对话框关闭后清除缓存，按钮恢复
        def _on_closed():
            if dlg in self._deepliif_result_dialogs:
                self._deepliif_result_dialogs.remove(dlg)
            if self._active_result_dlg is dlg:
                self._active_result_dlg = None
            self._patch_results = None
            self._deepliif_btn.setText("DeepLIIF 分析")
            self._deepliif_btn.setToolTip("使用 DeepLIIF 进行 IHC 染色分析和细胞分割")
        dlg.destroyed.connect(_on_closed)

    def _apply_overlay_to_canvas(self, roi_id: str, qimage: QImage,
                                  x: int, y: int, w: int, h: int,
                                  opacity: float) -> None:
        """将分割结果叠加到画布上。"""
        self._canvas.set_overlay_opacity(opacity)
        self._canvas.add_overlay(roi_id, qimage, x, y, w, h)
        self._clear_overlay_btn.setVisible(True)
        self._status_label.setText(f"已叠加 ROI {roi_id[:8]} 的分割结果到画布")

    def _clear_analysis_overlay(self) -> None:
        """清除画布上的分析叠加。"""
        self._canvas.clear_overlays()
        self._clear_overlay_btn.setVisible(False)
        self._status_label.setText("已清除分析叠加")

    def _set_deepliif_model_dir(self) -> None:
        """设置 DeepLIIF 模型目录。"""
        current = str(get_default_model_dir())
        d = QFileDialog.getExistingDirectory(
            self, "选择 DeepLIIF 模型目录", current,
        )
        if d:
            ok, msg = check_model_available(d)
            if ok:
                QMessageBox.information(self, "模型就绪", msg)
            else:
                reply = QMessageBox.question(
                    self, "模型未就绪",
                    f"{msg}\n\n是否立即下载模型？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if reply == QMessageBox.StandardButton.Yes:
                    self._download_deepliif_model(d)

    def _download_deepliif_model(self, model_dir: str) -> None:
        """在 QThread 中下载 DeepLIIF 模型。"""
        from liver_portal_crop.deepliif_runner import ModelDownloadWorker
        from PySide6.QtWidgets import QProgressDialog

        # 进度对话框
        self._dl_progress = QProgressDialog("正在准备下载...", "取消", 0, 0, self)
        self._dl_progress.setWindowTitle("下载 DeepLIIF 模型")
        self._dl_progress.setMinimumDuration(0)
        self._dl_progress.setAutoClose(False)
        self._dl_progress.setAutoReset(False)
        self._dl_progress.setCancelButton(None)
        self._dl_progress.show()

        # Worker + Thread
        self._dl_worker = ModelDownloadWorker(model_dir)
        self._dl_thread = QThread()
        self._dl_worker.moveToThread(self._dl_thread)

        self._dl_thread.started.connect(self._dl_worker.run)
        self._dl_worker.progress.connect(self._on_dl_progress)
        self._dl_worker.status.connect(
            lambda msg: self._dl_progress.setLabelText(msg)
        )
        self._dl_worker.finished.connect(self._on_dl_finished_app)
        self._dl_worker.finished.connect(self._dl_thread.quit)
        self._dl_worker.finished.connect(self._dl_worker.deleteLater)
        self._dl_thread.finished.connect(self._dl_thread.deleteLater)

        self._dl_thread.start()

    def _on_dl_progress(self, pct: int, dl_mb: int, total_mb: int):
        """更新下载进度。"""
        if total_mb <= 0:
            return
        if self._dl_progress.maximum() == 0:
            self._dl_progress.setMaximum(100)
            cancel_btn = QPushButton("取消")
            self._dl_progress.setCancelButton(cancel_btn)
            self._dl_progress.canceled.connect(self._dl_worker.cancel)
        self._dl_progress.setValue(pct)
        self._dl_progress.setLabelText(
            f"正在下载... {dl_mb} / {total_mb} MB  ({pct}%)"
        )

    def _on_dl_finished_app(self, ok: bool, msg: str):
        """下载完成（菜单触发）。"""
        self._dl_progress.close()
        if ok:
            QMessageBox.information(self, "下载完成", msg)
        elif "取消" not in msg:
            QMessageBox.warning(self, "下载失败", msg)

    def _cleanup_stale_rois(self) -> None:
        self._file_controller.cleanup_stale_rois()

    # ── 退出 ──────────────────────────────────────────

    def closeEvent(self, event) -> None:
        if self._roi_manager.all_rois():
            reply = QMessageBox.question(
                self, "确认退出",
                "有未导出的 ROI，保存标注后退出？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
            )
            if reply == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
        self._save_session()
        event.accept()
