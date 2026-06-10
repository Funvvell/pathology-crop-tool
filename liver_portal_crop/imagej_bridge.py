"""PyImageJ / Fiji 桥接模块 — 将 pathology-crop-tool 的 numpy ROI 图像
无缝接入 ImageJ / Fiji 全量生态。

功能概述:
  1. numpy ↔ ImageJ2 Dataset 双向无损转换（RGB / 灰度 / 多通道堆栈）
  2. GUI 可视化调参模式 — 载入单张样本到 Fiji，任意操作后自动抓取参数
  3. 参数配置保存 / 加载（JSON 数值参数 + 宏文本双模式）
  4. headless 无界面批量处理 — 完整复刻调参阶段全部 ImageJ 操作步骤
  5. 批量测量结果自动汇总导出 CSV

使用场景:
  - IHC 免疫组化（默认示例）
  - HE 染色 / 荧光 / 多通道 — 仅需修改处理步骤配置

依赖:
  pip install imagej openjdk scyjava numpy pandas tifffile

作者: pathology-crop-tool 集成模块
"""

from __future__ import annotations

import csv
import json
import logging
import os
import shutil
import tempfile
import time
import traceback
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np

# ── 日志配置 ──────────────────────────────────────────────────────────
logger = logging.getLogger("imagej_bridge")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(name)s %(levelname)s — %(message)s",
        datefmt="%H:%M:%S",
    ))
    logger.addHandler(_handler)


# ── 用户等待钩子（可被调用方覆盖）──
def _wait_for_user():
    """等待用户确认完成调参操作。默认使用 input() 阻塞，
    调用方可设置 `imagej_bridge._wait_for_user` 为自定义实现。"""
    input(">>> 按 Enter 键抓取当前参数并保存配置…")


# ── 依赖可用性检测 ────────────────────────────────────────────────────

def check_imagej_available() -> tuple[bool, str]:
    """检查 PyImageJ 及其依赖是否已安装。

    使用 importlib.util.find_spec() 只检查包元数据，不触发实际 import
    （避免 imagej 首次 import 时的 Maven 下载卡死）。

    Returns:
        (available, message)
        available=True  时 message 为版本信息
        available=False 时 message 为缺失包列表和安装命令
    """
    import importlib.util
    import shutil
    from pathlib import Path

    missing = []
    versions = {}

    # 用 find_spec 检查包是否安装（不触发 import）
    # 如果 find_spec 找不到，再尝试实际 import（某些 Python 发行版 site-packages 注册不完整）
    for pkg in ("imagej", "scyjava", "jpype"):
        spec = importlib.util.find_spec(pkg)
        if spec is None:
            # 二次确认：尝试实际导入
            try:
                __import__(pkg)
                # import 成功说明包已安装，只是 find_spec 没找到
                try:
                    from importlib.metadata import version as _get_ver
                    versions[pkg] = _get_ver(pkg)
                except Exception:
                    versions[pkg] = "installed"
                continue
            except ImportError:
                pass
            missing.append(pkg)
        else:
            # 只读 dist 元数据版本，不 import 模块
            try:
                from importlib.metadata import version as _get_ver
                versions[pkg] = _get_ver(pkg)
            except Exception:
                versions[pkg] = "installed"

    # JDK 检查：多路径检测
    # 1. Fiji 自带的 JDK
    fiji_java = Path.home() / "Fiji.app" / "java" / "win64" / "jdk" / "bin" / "java.exe"
    # 2. 系统 PATH 中的 java
    system_java = shutil.which("java")
    # 3. 常见的 Temurin/Adoptium JDK 安装路径
    common_jdk_paths = [
        Path.home() / ".sdkman" / "candidates" / "java" / "current" / "bin" / "java",  # SDKMAN
        Path.home() / ".jabba" / "jdk" / "*",  # Jabba (通配符)
    ]
    # 检查 Windows 常见的 JDK 安装位置
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    program_files_x86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    for base in [program_files, program_files_x86]:
        for pattern in ["Eclipse Adoptium", "AdoptOpenJDK", "Java"]:
            jdk_dir = base / pattern
            if jdk_dir.exists():
                for java_exe in jdk_dir.rglob("bin/java.exe"):
                    common_jdk_paths.append(java_exe)

    jdk_ok = False
    jdk_found_at = None
    if fiji_java.exists():
        jdk_ok = True
        jdk_found_at = f"Fiji 自带 JDK: {fiji_java}"
    elif system_java:
        jdk_ok = True
        jdk_found_at = f"系统 PATH: {system_java}"
    else:
        # 检查常见安装路径
        for jdk_path in common_jdk_paths:
            # 处理通配符
            if "*" in str(jdk_path):
                import glob
                matches = glob.glob(str(jdk_path))
                for match in matches:
                    if Path(match).exists():
                        jdk_ok = True
                        jdk_found_at = f"常见 JDK 路径: {match}"
                        break
            elif jdk_path.exists():
                jdk_ok = True
                jdk_found_at = f"常见 JDK 路径: {jdk_path}"
                break
            if jdk_ok:
                break

    if not missing and jdk_ok:
        msg = "PyImageJ 可用 — " + ", ".join(
            f"{k}={v}" for k, v in versions.items()
        )
        if jdk_found_at:
            msg += f"\nJDK 位置: {jdk_found_at}"
        return True, msg

    # 构造安装指引
    parts = []
    if missing:
        parts.append(f"缺少 Python 包: {', '.join(missing)}")
        parts.append(f"已安装的包: {', '.join(versions.keys()) or '无'}")
    if not jdk_ok:
        parts.append("缺少 JDK (Java Development Kit 17+)")
        parts.append(f"已检查的路径:")
        parts.append(f"  - Fiji JDK: {fiji_java} ({'存在' if fiji_java.exists() else '不存在'})")
        parts.append(f"  - 系统 PATH java: {system_java or '未找到'}")

    install_cmd = f"pip install {' '.join(missing)}" if missing else ""
    if not jdk_ok:
        if install_cmd:
            install_cmd += "\n还需安装 JDK: https://adoptium.net/"
        else:
            install_cmd = "请安装 JDK: https://adoptium.net/ \n（或下载 Fiji，Fiji 自带 JDK）"

    return False, "\n".join(parts) + (f"\n\n安装命令: {install_cmd}" if install_cmd else "")


def install_imagej_deps() -> tuple[bool, str]:
    """通过 pip 安装 PyImageJ 核心依赖。

    Returns:
        (success, message)
    """
    import subprocess
    import sys

    packages = ["imagej", "scyjava"]
    logger.info("开始安装 PyImageJ 依赖: %s", packages)

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install"] + packages,
            capture_output=True,
            text=True,
            timeout=600,  # 10 分钟超时
        )
        if result.returncode == 0:
            logger.info("PyImageJ 依赖安装成功")
            return True, "安装成功:\n" + result.stdout[-500:]
        else:
            logger.error("pip install 失败: %s", result.stderr)
            return False, "安装失败:\n" + result.stderr[-500:]
    except subprocess.TimeoutExpired:
        return False, "安装超时（>10 分钟），请手动安装"
    except Exception as e:
        return False, f"安装出错: {e}"


# ═══════════════════════════════════════════════════════════════════════
#  第一部分: 配置数据结构
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class AnalysisConfig:
    """ImageJ 分析流程配置。

    包含两种参数记录方案:
      方案 A (numeric_params): 结构化数值参数 — 阈值、粒子尺寸、形态学半径等
      方案 B (macro_text):     完整 ImageJ 宏 / 脚本文本 — 适配复杂多插件联动

    两种方案可同时保存，批量模式优先使用 macro_text（若非空）。
    """

    # ── 基础信息 ──
    config_name: str = "default"          # 配置名称
    description: str = ""                 # 描述（如 "IHC DAB 阳性分析"）
    created_at: str = ""                  # 创建时间 ISO 格式
    ij_version: str = ""                  # ImageJ 版本信息

    # ── 方案 A: 结构化数值参数 ──
    # 通道处理
    channel_index: int = 0                # 目标通道索引（0-based, -1=全部）
    color_deconvolution: bool = False     # 是否启用颜色反卷积
    stain_vectors: str = ""               # 反卷积染色向量（如 "H DAB"）

    # 阈值
    threshold_method: str = "Default"     # 阈值方法名（Default/Otsu/Li/Huang…）
    threshold_min: int = 0                # 手动阈值下限
    threshold_max: int = 255              # 手动阈值上限
    auto_threshold: bool = True           # True=自动阈值, False=手动

    # 形态学
    morph_operation: str = "open"         # open/close/dilate/erode/fill_holes
    morph_radius: int = 2                 # 结构元素半径（像素）
    morph_iterations: int = 1             # 迭代次数

    # 粒子分析
    particle_min_size: float = 50.0       # 最小粒子面积（像素² 或 µm²）
    particle_max_size: float = float("inf")  # 最大粒子面积
    particle_circularity_min: float = 0.0 # 圆度下限 (0-1)
    particle_circularity_max: float = 1.0 # 圆度上限 (0-1)
    exclude_edge_particles: bool = True   # 排除边缘粒子

    # 测量项（ImageJ Measure 勾选项）
    measurements: List[str] = field(default_factory=lambda: [
        "Area", "Mean", "StdDev", "Min", "Max",
        "IntegratedDensity", "RawIntegratedDensity",
    ])
    # 可选值: Area, Mean, StdDev, Min, Max, IntegratedDensity,
    #   RawIntegratedDensity, Skewness, Kurtosis, AreaFraction,
    #   DisplayLabel, Slice, Feret, Circularity, Shape descriptors…

    # 标尺校准
    pixel_width: float = 1.0              # 像素宽度（µm）
    pixel_height: float = 1.0             # 像素高度（µm）
    unit: str = "pixel"                   # 单位（pixel/µm/mm）
    scale_set: bool = False               # 是否已设标尺

    # 粒子叠加选项
    overlay_results: bool = True          # 结果叠加到原图
    add_to_manager: bool = True           # 粒子添加到 ROI Manager

    # ── 方案 B: 宏文本 ──
    macro_text: str = ""                  # 完整 ImageJ 宏脚本文本

    # ── 通用设置 ──
    output_format: str = "csv"            # 测量结果输出格式
    batch_timeout_sec: int = 300          # 单张超时秒数

    def to_dict(self) -> Dict[str, Any]:
        """转为可序列化 dict。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AnalysisConfig":
        """从 dict 恢复。忽略未知键。"""
        valid = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in valid})

    def save(self, path: Union[str, Path]) -> None:
        """保存为 JSON 文件。"""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
        logger.info("配置已保存 → %s", path)

    @classmethod
    def load(cls, path: Union[str, Path]) -> "AnalysisConfig":
        """从 JSON 文件加载。"""
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        cfg = cls.from_dict(data)
        logger.info("配置已加载 ← %s", path)
        return cfg


# ═══════════════════════════════════════════════════════════════════════
#  第二部分: numpy ↔ ImageJ 双向转换
# ═══════════════════════════════════════════════════════════════════════

def numpy_to_imagej_dataset(
    ij,
    arr: np.ndarray,
    title: str = "image",
    dim_order: Optional[List[str]] = None,
):
    """将 numpy 数组转为 ImageJ2 Dataset 对象。

    利用 PyImageJ 的 ij.py.to_dataset() 实现零拷贝桥接。

    支持格式:
      - 灰度:     (H, W)              → dim_order=['y','x']
      - RGB:      (H, W, 3) uint8     → dim_order=['y','x','c']
      - 多通道:   (H, W, C)           → dim_order=['y','x','c']
      - 3D堆栈:   (H, W, Z)           → dim_order=['y','x','z']
      - 4D:       (H, W, C, Z)        → dim_order=['y','x','c','z']
      - 5D:       (H, W, C, Z, T)     → dim_order=['y','x','c','z','t']

    Args:
        ij:         已初始化的 PyImageJ 实例
        arr:        numpy 数组 (uint8/uint16/float32/float64)
        title:      图像标题（在 Fiji 窗口标题栏显示）
        dim_order:  轴标签列表, 如 ['y','x'] 或 ['y','x','c']
                    若为 None 则自动推断

    Returns:
        ImageJ2 net.imglib2.img.Img 对象（可直接 ij.ui().show() 显示）
    """
    arr = np.ascontiguousarray(arr)
    ndim = arr.ndim

    # ── 自动推断维度顺序 ──
    if dim_order is None:
        if ndim == 2:
            dim_order = ["y", "x"]
        elif ndim == 3:
            c_dim = arr.shape[2]
            if c_dim in (3, 4) and arr.dtype == np.uint8:
                dim_order = ["y", "x", "c"]  # RGB / RGBA
            else:
                dim_order = ["y", "x", "c"]  # 多通道
        elif ndim == 4:
            dim_order = ["y", "x", "c", "z"]
        elif ndim == 5:
            dim_order = ["y", "x", "c", "z", "t"]
        else:
            raise ValueError(f"不支持 {ndim} 维数组, 最多 5 维")

    logger.debug(
        "numpy→Dataset: shape=%s, dtype=%s, dim_order=%s, title='%s'",
        arr.shape, arr.dtype, dim_order, title,
    )

    # ── 使用 PyImageJ 的 py.to_dataset 转换 ──
    # 该函数直接接受 numpy 数组和 dim_order 列表
    # 对于 RGB uint8 (H,W,3) + dim_order=['y','x','c'],
    # PyImageJ 自动识别为 RGB Color image
    dataset = ij.py.to_dataset(arr, dim_order=dim_order)
    dataset.setName(title)

    return dataset


def imagej_dataset_to_numpy(ij, dataset) -> np.ndarray:
    """将 ImageJ2 Dataset 对象转回 numpy 数组。

    利用 PyImageJ 的 ij.py.from_java() 实现转换。
    自动处理维度排列回 numpy 标准 (H,W,C) 格式。

    Args:
        ij:       PyImageJ 实例
        dataset:  ImageJ2 Dataset 对象

    Returns:
        numpy 数组 (H,W) / (H,W,3) / (H,W,C) 等
    """
    np_arr = ij.py.from_java(dataset)
    np_arr = np.ascontiguousarray(np_arr)

    logger.debug("Dataset→numpy: shape=%s, dtype=%s", np_arr.shape, np_arr.dtype)
    return np_arr


def numpy_to_imageplus_via_ij2(ij, arr: np.ndarray, title: str = "image"):
    """将 numpy 数组转为 ImageJ1 ImagePlus（通过 ImageJ2 桥接层）。

    流程: numpy → Dataset → ImagePlus
    兼容所有 ImageJ1 旧接口和依赖 ImagePlus 的第三方插件。

    Args:
        ij:    PyImageJ 实例
        arr:   numpy 数组
        title: 图像标题

    Returns:
        ImageJ1 ImagePlus 对象
    """
    # 先转 Dataset
    dataset = numpy_to_imagej_dataset(ij, arr, title=title)
    # 再通过 ImageJ2 的转换服务转为 ImagePlus
    # 使用 ij.convert().convert() 或 ij.dataset().convertToImagePlus()
    try:
        # ImageJ2 → ImageJ1 官方桥接
        imp = ij.dataset().convertToImagePlus(dataset)
    except Exception:
        # 回退: 通过临时 TIFF 文件
        logger.debug("convertToImagePlus 失败, 使用 TIFF 回退")
        imp = _dataset_to_imageplus_via_tiff(ij, dataset, title)

    logger.debug("numpy→ImagePlus: shape=%s, title='%s'", arr.shape, title)
    return imp


def imageplus_to_numpy_via_tiff(ij, imp) -> np.ndarray:
    """将 ImageJ1 ImagePlus 转回 numpy（通过 TIFF 中转）。

    兼容所有 ImagePlus 子类型（RGB/灰度/堆栈/超维）。

    Args:
        ij:  PyImageJ 实例
        imp: ImageJ1 ImagePlus 对象

    Returns:
        numpy 数组
    """
    import tifffile

    tmp = tempfile.NamedTemporaryFile(suffix=".tiff", delete=False)
    tmp_path = tmp.name
    tmp.close()

    try:
        # 用 ImageJ 保存为 TIFF
        ij.IJ.saveAs(imp, "Tiff", tmp_path)
        np_arr = tifffile.imread(tmp_path)
        np_arr = np.ascontiguousarray(np_arr)
        logger.debug("ImagePlus→numpy(via TIFF): shape=%s", np_arr.shape)
        return np_arr
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _dataset_to_imageplus_via_tiff(ij, dataset, title: str = "image"):
    """Dataset → ImagePlus 通过 TIFF 文件中转（兜底方案）。"""
    import scyjava
    _IJ = scyjava.jimport('ij.IJ')

    tmp = tempfile.NamedTemporaryFile(suffix=".tiff", delete=False)
    tmp_path = tmp.name
    tmp.close()

    try:
        ij.io().save(dataset, tmp_path)
        imp = _IJ.openImage(tmp_path)
        if imp:
            imp.setTitle(title)
            return imp
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    return None


def imageplus_to_dataset(ij, imp):
    """ImageJ1 ImagePlus → ImageJ2 Dataset。"""
    # 使用 ImageJ2 的转换服务
    try:
        return ij.convert().convert(imp, ij.dataset().getDataType())
    except Exception:
        # 回退: 通过 numpy 中转
        np_arr = imageplus_to_numpy_via_tiff(ij, imp)
        return numpy_to_imagej_dataset(ij, np_arr, title=imp.getTitle() or "converted")


# ═══════════════════════════════════════════════════════════════════════
#  第三部分: ImageJ / Fiji 初始化
# ═══════════════════════════════════════════════════════════════════════

# 全局 ImageJ 实例缓存（避免重复初始化 JVM）
_ij_instance = None
_ij_mode: Optional[str] = None  # "gui" 或 "headless"


def init_imagej(
    mode: str = "headless",
    fiji_path: Optional[str] = None,
    update_sites: Optional[List[str]] = None,
    max_heap: str = "4g",
    plugins_dir: Optional[str] = None,
):
    """初始化 PyImageJ / Fiji 实例。

    两种加载方式:
      方式 1 (推荐): fiji_path 指向本地已安装 Fiji 文件夹
                     自动加载该 Fiji 的全部插件（包括第三方如 WEKA、StarDist）
      方式 2 (在线):  fiji_path=None, 自动从 Maven 下载纯净 Fiji 包
                     可通过 update_sites 指定额外更新站点

    ⚠️ 同一 Python 进程只能初始化一次 JVM。如需切换 GUI/headless 模式，
    必须重启 Python 进程。

    Args:
        mode:         "gui" = 带界面的 Fiji / "headless" = 无界面后台
        fiji_path:    本地 Fiji 安装路径（如 "C:/Fiji.app"）
        update_sites: 额外更新站点名称列表（仅在线模式生效）
        max_heap:     JVM 最大堆内存（大图建议 "8g"）
        plugins_dir:  额外插件目录（可选）

    Returns:
        imagej 实例
    """
    global _ij_instance, _ij_mode

    # 如果已初始化，返回缓存实例
    if _ij_instance is not None:
        if _ij_mode != mode:
            logger.warning(
                "ImageJ 已在 %s 模式下初始化，无法切换到 %s 模式。"
                "请重启 Python 进程。返回现有实例。",
                _ij_mode, mode,
            )
        return _ij_instance

    # ── JVM 内存配置 ──
    import scyjava
    scyjava.config.add_option(f"-Xmx{max_heap}")

    if plugins_dir:
        scyjava.config.add_option(f"-Dplugins.dir={plugins_dir}")

    # ── 初始化 ──
    import imagej

    if fiji_path is not None:
        # ── 方式 1: 本地 Fiji ──
        fiji_path = Path(fiji_path)
        if not fiji_path.exists():
            raise FileNotFoundError(f"本地 Fiji 路径不存在: {fiji_path}")

        logger.info("加载本地 Fiji: %s", fiji_path)
        # mode: "interactive" = GUI 可见, "headless" = 无界面
        ij_mode = "interactive" if mode == "gui" else "headless"
        _ij_instance = imagej.init(str(fiji_path), mode=ij_mode)

    else:
        # ── 方式 2: 在线下载 ──
        logger.info("在线加载 Fiji (scijava)")
        if update_sites:
            for site in update_sites:
                scyjava.config.add_option(
                    f"-Dscijava.update.site.{site}=true"
                )
        ij_mode = "interactive" if mode == "gui" else "headless"
        _ij_instance = imagej.init("sc.fiji:fiji", mode=ij_mode)

    _ij_mode = mode

    # 打印版本信息
    version = str(_ij_instance.getVersion())
    logger.info(
        "ImageJ 初始化完成 [version=%s, mode=%s, heap=%s]",
        version, mode, max_heap,
    )

    return _ij_instance


def get_ij():
    """获取已初始化的 ImageJ 实例。未初始化则以 headless 模式自动初始化。"""
    global _ij_instance
    if _ij_instance is None:
        return init_imagej(mode="headless")
    return _ij_instance


def is_gui_mode() -> bool:
    """当前 ImageJ 实例是否为 GUI 模式。"""
    return _ij_mode == "gui"


# ═══════════════════════════════════════════════════════════════════════
#  第四部分: ImageJ 操作执行引擎
# ═══════════════════════════════════════════════════════════════════════

# ── Java 类缓存（通过 scyjava.jimport 获取，JVM 初始化后可用）──
_jclasses: Dict[str, Any] = {}


def _get_jclass(fqn: str):
    """获取 Java 类，带缓存。JVM 必须已初始化。"""
    if fqn not in _jclasses:
        import scyjava
        _jclasses[fqn] = scyjava.jimport(fqn)
    return _jclasses[fqn]


# 常用 Java 类快捷函数
def _IJ():       return _get_jclass('ij.IJ')
def _ResultsTable(): return _get_jclass('ij.measure.ResultsTable')
def _RoiManager():   return _get_jclass('ij.plugin.frame.RoiManager')
def _WindowManager(): return _get_jclass('ij.WindowManager')
def _Calibration():  return _get_jclass('ij.measure.Calibration')

def apply_analysis_steps(
    ij,
    dataset,
    config: AnalysisConfig,
    imp=None,
):
    """在 ImageJ 图像上执行配置中定义的全部分析步骤。

    如果 config.macro_text 非空，优先执行宏脚本（完整复刻复杂流程）。
    否则按结构化参数依次执行：通道处理→阈值→形态学→粒子分析。

    Args:
        ij:       PyImageJ 实例
        dataset:  ImageJ2 Dataset（输入）
        config:   AnalysisConfig 配置
        imp:      可选 ImagePlus（某些 IJ1 插件需要）

    Returns:
        dict: {
            "dataset":        处理后的 Dataset (可能为 None),
            "imp":            处理后的 ImagePlus,
            "results_table":  ImageJ ResultsTable（如有）,
            "overlay":        Overlay 对象 (如有),
            "roi_manager":    ROI Manager 引用 (如有),
        }
    """
    result = {
        "dataset": dataset,
        "imp": imp,
        "results_table": None,
        "overlay": None,
        "roi_manager": None,
    }

    # ── 如果有宏文本，优先执行宏 ──
    if config.macro_text and config.macro_text.strip():
        logger.info("执行宏脚本 (%d 字符)", len(config.macro_text))
        try:
            # 确保有 ImagePlus
            if imp is None:
                imp = numpy_to_imageplus_via_ij2(ij, imagej_dataset_to_numpy(ij, dataset))
                result["imp"] = imp

            if imp is not None:
                imp.show()
                # 将 ImagePlus 设为当前活动图像
                imp.getWindow().toFront()

            # 执行宏
            macro_result = ij.IJ().runMacro(config.macro_text)
            logger.info("宏执行完成, 返回值: %s", str(macro_result))

            # 尝试获取结果
            result["results_table"] = _get_results_table(ij)
            result["imp"] = _get_current_imageplus(ij)

            return result

        except Exception as e:
            logger.warning("宏执行失败, 回退到结构化参数模式: %s", e)
            traceback.print_exc()

    # ── 结构化参数模式: 逐步执行 ──
    try:
        # 确保有 ImagePlus
        if imp is None:
            np_arr = imagej_dataset_to_numpy(ij, dataset)
            imp = numpy_to_imageplus_via_ij2(ij, np_arr)
            result["imp"] = imp

        if imp is None:
            logger.error("无法创建 ImagePlus, 跳过分析")
            return result

        imp.show()
        title = imp.getTitle() or "image"

        # ── 步骤 1: 通道处理 ──
        if config.channel_index >= 0:
            _run_channel_split(ij, imp, config)

        # ── 步骤 2: 颜色反卷积（IHC DAB 染色常用）──
        if config.color_deconvolution and config.stain_vectors:
            _run_color_deconvolution(ij, imp, config)

        # ── 步骤 3: 标尺校准 ──
        if config.scale_set:
            _run_scale_calibration(ij, imp, config)

        # ── 步骤 4: 阈值分割 ──
        _run_threshold(ij, imp, config)

        # ── 步骤 5: 形态学运算 ──
        _run_morphology(ij, imp, config)

        # ── 步骤 6: 粒子分析 ──
        rt, overlay, rm = _run_particle_analysis(ij, imp, config)
        result["results_table"] = rt
        result["overlay"] = overlay
        result["roi_manager"] = rm

        logger.info("结构化分析步骤全部完成")

    except Exception as e:
        logger.error("分析步骤执行异常: %s", e)
        traceback.print_exc()

    return result


# ── 内部操作函数 ─────────────────────────────────────────────────────

def _run_channel_split(ij, imp, config: AnalysisConfig):
    """提取指定通道。使用 IJ.run 宏命令。"""
    n_channels = imp.getNChannels()
    if n_channels <= 1:
        return

    ch = min(config.channel_index, n_channels - 1)
    logger.debug("提取通道 %d/%d", ch + 1, n_channels)

    try:
        # Image > Color > Split Channels
        ij.IJ().run(imp, "Split Channels", "")

        # 查找目标通道窗口
        WM = _WindowManager()
        for w_title in WM.getImageTitles():
            if w_title.startswith(f"C{ch + 1}-"):
                selected = WM.getImage(w_title)
                if selected is not None:
                    imp.setImage(selected)
                    break
    except Exception as e:
        logger.debug("通道分割失败: %s", e)


def _run_color_deconvolution(ij, imp, config: AnalysisConfig):
    """应用颜色反卷积（IHC DAB 染色常用）。"""
    logger.debug("颜色反卷积: stain_vectors='%s'", config.stain_vectors)
    try:
        # 需要 Colour Deconvolution 插件（Fiji 内置）
        ij.IJ().run(
            imp,
            "Colour Deconvolution",
            f"vectors=[{config.stain_vectors}]",
        )
    except Exception as e:
        logger.warning("颜色反卷积失败 (插件可能未安装): %s", e)


def _run_scale_calibration(ij, imp, config: AnalysisConfig):
    """设置空间标尺校准。"""
    cal = imp.getCalibration()
    cal.pixelWidth = config.pixel_width
    cal.pixelHeight = config.pixel_height
    cal.setUnit(config.unit)
    logger.debug(
        "标尺校准: %.4f × %.4f %s/px",
        config.pixel_width, config.pixel_height, config.unit,
    )


def _run_threshold(ij, imp, config: AnalysisConfig):
    """应用阈值分割。"""
    if config.auto_threshold:
        method = config.threshold_method
        logger.debug("自动阈值: method=%s", method)
        # 先设为 8-bit 灰度（若原图是彩色）
        ij.IJ().run(imp, "8-bit", "")
        # 执行自动阈值
        ij.IJ().run(imp, "Auto Threshold", f"method={method} white")
    else:
        logger.debug(
            "手动阈值: min=%d, max=%d",
            config.threshold_min, config.threshold_max,
        )
        ij.IJ().run(imp, "8-bit", "")
        ij.IJ().setAutoThreshold(imp, "Default dark")
        # 使用 Set Threshold 设置精确范围
        ip = imp.getProcessor()
        ip.setThreshold(config.threshold_min, config.threshold_max)
        ij.IJ().run(imp, "Convert to Mask", "background=Dark black")


def _run_morphology(ij, imp, config: AnalysisConfig):
    """应用形态学运算。"""
    op = config.morph_operation
    radius = config.morph_radius
    iterations = config.morph_iterations

    logger.debug("形态学: %s, radius=%d, iterations=%d", op, radius, iterations)

    if op == "fill_holes":
        ij.IJ().run(imp, "Fill Holes", "")
    elif op == "skeletonize":
        ij.IJ().run(imp, "Skeletonize", "")
    elif op in ("open", "close", "dilate", "erode"):
        # 优先用 MorphoLibJ（Fiji 内置），回退到内置 morpho
        op_capitalized = op.capitalize()
        try:
            # MorphoLibJ 磁盘结构元素
            ij.IJ().run(
                imp,
                "Morphological Filters",
                f"operation={op_capitalized} element=Disk radius={radius}",
            )
        except Exception:
            # 回退到内置二值形态学
            logger.debug("MorphoLibJ 不可用, 使用内置形态学")
            for _ in range(iterations):
                ij.IJ().run(imp, op_capitalized, "")
    else:
        logger.warning("未知形态学操作: %s, 跳过", op)


def _run_particle_analysis(
    ij, imp, config: AnalysisConfig,
) -> Tuple[Any, Any, Any]:
    """执行粒子分析（Analyze Particles）。"""
    RT = _ResultsTable()

    # ── 设置测量项 ──
    measure_cmd = _build_measure_command(config.measurements)
    ij.IJ().run("Set Measurements...", measure_cmd)

    # ── 清空旧结果 ──
    ij.IJ().run("Clear Results", "")

    # ── 获取或创建 ROI Manager ──
    try:
        RM = _RoiManager()
        rm = RM.getInstance2()  # getInstance2 不会创建新实例
        if rm is None:
            rm = RM()
        rm.reset()
    except Exception:
        rm = None

    # ── 构造 Analyze Particles 参数 ──
    size_str = f"{config.particle_min_size}-{config.particle_max_size}"
    if config.particle_max_size == float("inf"):
        size_str = f"{config.particle_min_size}-Infinity"

    circ_str = f"{config.particle_circularity_min}-{config.particle_circularity_max}"

    options_parts = []
    if config.exclude_edge_particles:
        options_parts.append("exclude")
    if config.overlay_results:
        options_parts.append("overlay")
    options_str = " ".join(options_parts)

    add_str = "add" if config.add_to_manager else ""

    cmd = f"size={size_str} circularity={circ_str} {options_str} {add_str} show=Outlines display clear"
    logger.debug("Analyze Particles: %s", cmd)

    try:
        ij.IJ().run(imp, "Analyze Particles...", cmd)
    except Exception as e:
        logger.error("Analyze Particles 失败: %s", e)
        return None, None, rm

    # ── 获取结果 ──
    rt = RT.getResultsTable()
    overlay = imp.getOverlay()

    n = rt.size() if rt is not None else 0
    logger.info("粒子分析完成: 检测到 %d 个粒子", n)

    return rt, overlay, rm


def _build_measure_command(measurements: List[str]) -> str:
    """将可读测量项名称转为 ImageJ 'Set Measurements' 命令字符串。"""
    flag_map = {
        "Area": "area",
        "Mean": "mean",
        "StdDev": "std",
        "Min": "min",
        "Max": "max",
        "IntegratedDensity": "integrated",
        "RawIntegratedDensity": "rawintegrated display",
        "Median": "median",
        "Skewness": "skew",
        "Kurtosis": "kurt",
        "AreaFraction": "area_fraction",
        "DisplayLabel": "display label",
        "Slice": "slice",
        "Feret": "feret's",
        "Circularity": "shape",
        "Shape": "shape",
        "StackPosition": "stack",
        "ScientificNotation": "decimal=3",
    }
    flags = []
    for m in measurements:
        f = flag_map.get(m, m.lower())
        if f not in flags:
            flags.append(f)
    return " ".join(flags)


def _get_results_table(ij):
    """获取当前 ImageJ ResultsTable。"""
    try:
        RT = _ResultsTable()
        return RT.getResultsTable()
    except Exception:
        return None


def _get_current_imageplus(ij):
    """获取当前活动 ImagePlus。"""
    try:
        IJ = _IJ()
        return IJ.getImage()
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════
#  第五部分: GUI 可视化调参模式
# ═══════════════════════════════════════════════════════════════════════

def launch_gui_tuning(
    sample_image: np.ndarray,
    fiji_path: Optional[str] = None,
    title: str = "ROI调参样本",
    config_save_path: Optional[str] = None,
    initial_config: Optional[AnalysisConfig] = None,
    scale: Optional[Tuple[float, str]] = None,
) -> AnalysisConfig:
    """启动 GUI 版 Fiji 进行交互式参数调优。

    完整流程:
      1. 初始化 GUI 模式 ImageJ（自动在线拉取或读本地 Fiji）
      2. 将 numpy 数组转为 ImageJ 图像，在 Fiji 窗口打开
      3. 用户可自由操作 Fiji 全部功能（阈值、滤镜、粒子分析、Weka…）
      4. 用户操作完成后，在 Python 终端按 Enter
      5. 程序自动抓取当前图像的所有生效参数
      6. 保存到 JSON 配置文件

    ⚠️ 调用此函数前请确保尚未初始化 headless 模式的 ImageJ。
       同一进程只能初始化一次 JVM。

    Args:
        sample_image:      numpy 数组 (H,W) 或 (H,W,3) uint8 RGB
        fiji_path:         本地 Fiji 路径（None=在线下载）
        title:             Fiji 窗口标题
        config_save_path:  配置保存路径（None=自动生成）
        initial_config:    初始配置（预设参数，用户操作后更新）
        scale:             标尺 (pixel_size, unit), 如 (0.5, "um")

    Returns:
        捕获的 AnalysisConfig
    """
    if sample_image is None or sample_image.size == 0:
        raise ValueError("sample_image 不能为空")

    logger.info(
        "═══ 启动 GUI 调参模式 ═══\n"
        "  图像 shape=%s, dtype=%s\n"
        "  Fiji路径=%s",
        sample_image.shape, sample_image.dtype,
        fiji_path or "(在线下载)",
    )

    # ── 初始化 GUI ImageJ ──
    ij = init_imagej(mode="gui", fiji_path=fiji_path)

    # ── 转换并显示图像 ──
    dataset = numpy_to_imagej_dataset(ij, sample_image, title=title)
    ij.ui().show(title, dataset)

    # 也生成 ImagePlus 供 IJ1 插件使用
    imp = numpy_to_imageplus_via_ij2(ij, sample_image, title=title)
    if imp is not None:
        imp.show()

    # ── 设置标尺 ──
    if scale is not None and imp is not None:
        pixel_size, unit = scale
        cal = imp.getCalibration()
        cal.pixelWidth = pixel_size
        cal.pixelHeight = pixel_size
        cal.setUnit(unit)
        logger.info("已设置标尺: %.4f %s/px", pixel_size, unit)

    # ── 等待用户操作 ──
    print("\n" + "=" * 60)
    print("  Fiji 已启动，图像已打开。")
    print("  请在 Fiji 中进行所有调参操作：")
    print("    • Image > Adjust > Threshold  →  调阈值")
    print("    • Process > Filters / Binary   →  形态学处理")
    print("    • Analyze > Measure            →  粒子分析")
    print("    • Plugins > WEKA / StarDist    →  机器学习分割")
    print("    • Plugins > Macros > Record    →  录制宏")
    print("  操作完成后，等待弹出确认对话框。")
    print("=" * 60 + "\n")

    # 等待用户确认信号（由调用方设置，默认用 input 阻塞）
    _wait_for_user()

    # ── 抓取参数 ──
    # 尝试从 ImageJ Recorder 获取宏文本
    macro_text = ""
    try:
        macro_text = _read_recorder_text(ij)
        if macro_text:
            logger.info("从 Recorder 获取宏文本: %d 字符", len(macro_text))
    except Exception:
        pass

    config = capture_current_config(
        ij=ij,
        imp=imp,
        initial_config=initial_config or AnalysisConfig(),
        macro_text=macro_text,
    )

    # ── 保存配置 ──
    if config_save_path is None:
        config_save_path = os.path.join(
            os.getcwd(), "imagej_analysis_config.json"
        )
    config.save(config_save_path)

    return config


def _read_recorder_text(ij) -> str:
    """尝试从 ImageJ Macro Recorder 窗口读取录制文本。"""
    try:
        WM = _WindowManager()
        recorder = WM.getWindow("Recorder")
        if recorder is None:
            return ""

        # 遍历组件查找文本区域
        def _find_textarea(component):
            cls_name = component.getClass().getSimpleName()
            if cls_name in ("JTextArea", "Text"):
                return component.getText()
            for child in component.getComponents():
                result = _find_textarea(child)
                if result is not None:
                    return result
            return None

        text = _find_textarea(recorder)
        return text or ""
    except Exception:
        return ""


def capture_current_config(
    ij=None,
    imp=None,
    initial_config: Optional[AnalysisConfig] = None,
    macro_text: str = "",
) -> AnalysisConfig:
    """从当前 ImageJ 状态中抓取所有生效参数。

    自动检测并提取:
      - 当前图像的阈值范围
      - 标尺校准信息
      - 图像类型和通道信息
      - 宏录制器中的脚本

    Args:
        ij:             PyImageJ 实例（None 则使用全局实例）
        imp:            当前活动 ImagePlus（None 则自动获取）
        initial_config: 基础配置（在此基础上更新）
        macro_text:     已录制的宏脚本文本（手动传入或从 Recorder 读取）

    Returns:
        更新后的 AnalysisConfig
    """
    if ij is None:
        ij = get_ij()

    config = initial_config or AnalysisConfig()
    config.created_at = time.strftime("%Y-%m-%dT%H:%M:%S")

    try:
        version = str(ij.getVersion())
        config.ij_version = version
    except Exception:
        pass

    try:
        IJ = _IJ()

        # ── 获取当前 ImagePlus ──
        if imp is None:
            imp = IJ.getImage()
        if imp is None:
            logger.warning("无活动图像，仅保存基础配置")
            if macro_text:
                config.macro_text = macro_text
            return config

        # ── 抓取阈值 ──
        try:
            ip = imp.getProcessor()
            min_t = ip.getMinThreshold()
            max_t = ip.getMaxThreshold()
            # ImageJ 用 -808080 表示"无阈值"
            if min_t != -808080.0:
                config.threshold_min = int(min_t)
                config.threshold_max = int(max_t)
                config.auto_threshold = False
                logger.info("抓取阈值: [%d, %d]", int(min_t), int(max_t))
            else:
                logger.info("当前无阈值设置")
        except Exception as e:
            logger.debug("抓取阈值失败: %s", e)

        # ── 抓取标尺校准 ──
        try:
            cal = imp.getCalibration()
            if cal and cal.pixelWidth != 1.0:
                config.pixel_width = cal.pixelWidth
                config.pixel_height = cal.pixelHeight
                config.unit = cal.getUnit()
                config.scale_set = True
                logger.info(
                    "抓取标尺: %.6f × %.6f %s",
                    cal.pixelWidth, cal.pixelHeight, cal.getUnit(),
                )
        except Exception as e:
            logger.debug("抓取标尺失败: %s", e)

        # ── 通道信息 ──
        try:
            config.channel_index = imp.getChannel() - 1
        except Exception:
            pass

        # ── 保存宏文本 ──
        if macro_text:
            config.macro_text = macro_text
            logger.info("保存宏文本: %d 字符", len(macro_text))

        logger.info("参数抓取完成")

    except Exception as e:
        logger.error("参数抓取异常: %s", e)
        traceback.print_exc()

    return config


# ═══════════════════════════════════════════════════════════════════════
#  第六部分: Headless 批量处理
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class BatchResult:
    """单张图像的批量处理结果。"""
    image_id: str                 # 图像标识（文件名或 ROI ID）
    success: bool                 # 是否成功
    error_message: str = ""       # 错误信息（成功时为空）
    duration_sec: float = 0.0     # 处理耗时

    # 测量数据: {"Area": [120.5, 85.3], "Mean": [145.2, 130.1], ...}
    measurements: Dict[str, Any] = field(default_factory=dict)

    # 汇总统计: {"n_particles": 2, "Area_sum": 205.8, ...}
    summary: Dict[str, float] = field(default_factory=dict)

    particle_count: int = 0       # 粒子计数
    roi_save_path: str = ""       # ROI zip 路径
    overlay_save_path: str = ""   # 叠加图 TIFF 路径
    mask_save_path: str = ""      # 掩码 TIFF 路径


def run_headless_batch(
    images: List[Tuple[str, np.ndarray]],
    config: Union[AnalysisConfig, str, Path],
    output_dir: Union[str, Path],
    fiji_path: Optional[str] = None,
    save_masks: bool = True,
    save_overlays: bool = True,
    save_rois: bool = True,
    max_heap: str = "4g",
) -> List[BatchResult]:
    """Headless 批量处理 — 完整复刻调参阶段所有 ImageJ 操作。

    ⚠️ 此函数使用 headless 模式，不能与 GUI 模式混用。

    Args:
        images:         [(image_id, numpy_array), ...] 图像列表
        config:         AnalysisConfig 或 JSON 配置文件路径
        output_dir:     输出目录（测量 CSV、掩码、ROI 等）
        fiji_path:      本地 Fiji 路径（None=在线下载）
        save_masks:     是否保存阈值/分割掩码 TIFF
        save_overlays:  是否保存叠加图像 TIFF
        save_rois:      是否保存 ROI zip 文件
        max_heap:       JVM 堆内存

    Returns:
        List[BatchResult] 每张图像的处理结果
    """
    # ── 加载配置 ──
    if isinstance(config, (str, Path)):
        config = AnalysisConfig.load(config)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 创建子目录
    masks_dir = output_dir / "masks"
    overlays_dir = output_dir / "overlays"
    rois_dir = output_dir / "rois"
    if save_masks:
        masks_dir.mkdir(exist_ok=True)
    if save_overlays:
        overlays_dir.mkdir(exist_ok=True)
    if save_rois:
        rois_dir.mkdir(exist_ok=True)

    # ── 初始化 headless ImageJ ──
    ij = init_imagej(mode="headless", fiji_path=fiji_path, max_heap=max_heap)

    results: List[BatchResult] = []
    total = len(images)
    global_start = time.time()

    logger.info(
        "═══ 开始 headless 批量处理 ═══\n"
        "  配置: %s\n"
        "  图像数: %d\n"
        "  输出目录: %s",
        config.config_name, total, output_dir,
    )

    for idx, (image_id, img_array) in enumerate(images, 1):
        t0 = time.time()
        br = BatchResult(image_id=image_id, success=False)

        try:
            logger.info(
                "[%d/%d] 处理: %s (shape=%s)",
                idx, total, image_id, img_array.shape,
            )

            # ── 超时检查 ──
            if time.time() - global_start > config.batch_timeout_sec * total:
                raise TimeoutError("总处理时间超过上限")

            # ── numpy → ImageJ ──
            safe_title = _sanitize_title(image_id)
            dataset = numpy_to_imagej_dataset(ij, img_array, title=safe_title)

            # ── 执行分析步骤 ──
            analysis = apply_analysis_steps(ij, dataset, config)
            imp = analysis["imp"]
            rt = analysis["results_table"]
            overlay = analysis["overlay"]
            rm = analysis["roi_manager"]

            # ── 收集测量数据 ──
            if rt is not None and rt.size() > 0:
                br.particle_count = rt.size()
                br.measurements = _extract_results_table_data(rt)
                br.summary = _compute_summary(br.measurements, br.particle_count)
            else:
                br.particle_count = 0
                br.summary = {"n_particles": 0}

            # ── 保存掩码 ──
            if save_masks and imp is not None:
                mask_path = masks_dir / f"{_sanitize_filename(image_id)}_mask.tiff"
                ij.IJ().saveAs(imp, "Tiff", str(mask_path))
                br.mask_save_path = str(mask_path)

            # ── 保存叠加层 ──
            if save_overlays and imp is not None:
                overlay_path = overlays_dir / f"{_sanitize_filename(image_id)}_overlay.tiff"
                try:
                    flat_imp = imp.flatten()
                    ij.IJ().saveAs(flat_imp, "Tiff", str(overlay_path))
                    br.overlay_save_path = str(overlay_path)
                except Exception as e:
                    logger.debug("保存叠加层失败: %s", e)

            # ── 保存 ROI ──
            if save_rois and rm is not None:
                roi_path = rois_dir / f"{_sanitize_filename(image_id)}_rois.zip"
                try:
                    rm.runCommand("Save", str(roi_path))
                    br.roi_save_path = str(roi_path)
                except Exception as e:
                    logger.debug("保存 ROI 失败: %s", e)

            # ── 关闭窗口释放内存 ──
            if imp is not None:
                try:
                    imp.close()
                except Exception:
                    pass

            br.success = True
            br.duration_sec = time.time() - t0
            logger.info(
                "[%d/%d] ✓ %s — %d 粒子, %.2fs",
                idx, total, image_id, br.particle_count, br.duration_sec,
            )

        except TimeoutError as e:
            br.error_message = str(e)
            br.duration_sec = time.time() - t0
            logger.warning("[%d/%d] ⏰ 超时: %s", idx, total, image_id)

        except Exception as e:
            br.error_message = f"{type(e).__name__}: {e}"
            br.duration_sec = time.time() - t0
            logger.error(
                "[%d/%d] ✗ 错误: %s — %s",
                idx, total, image_id, br.error_message,
            )
            traceback.print_exc()

        results.append(br)

    # ── 汇总导出 CSV ──
    csv_path = output_dir / "measurements_summary.csv"
    export_measurements_csv(results, csv_path, config.measurements)

    # ── 导出 JSON 汇总 ──
    json_path = output_dir / "batch_results.json"
    _export_batch_results_json(results, json_path, config)

    total_time = time.time() - global_start
    success_count = sum(1 for r in results if r.success)
    logger.info(
        "═══ 批量处理完成 ═══\n"
        "  成功: %d/%d\n"
        "  总耗时: %.1fs\n"
        "  CSV:  %s\n"
        "  JSON: %s",
        success_count, total, total_time, csv_path, json_path,
    )

    return results


# ═══════════════════════════════════════════════════════════════════════
#  第七部分: 测量结果导出
# ═══════════════════════════════════════════════════════════════════════

def _extract_results_table_data(rt) -> Dict[str, List]:
    """从 ImageJ ResultsTable 提取所有列数据为 dict。"""
    columns: Dict[str, List] = {}
    try:
        headings = rt.getHeadings()
        for heading in headings:
            heading_str = str(heading)
            try:
                if rt.columnExists(heading_str):
                    values = []
                    for row in range(rt.size()):
                        val = rt.getValue(heading_str, row)
                        values.append(float(val))
                    columns[heading_str] = values
                else:
                    # 字符串列（如 Label）
                    values = []
                    for row in range(rt.size()):
                        val = rt.getStringValue(heading_str, row)
                        values.append(str(val))
                    columns[heading_str] = values
            except Exception:
                pass
    except Exception as e:
        logger.debug("读取 ResultsTable 失败: %s", e)

    return columns


def _compute_summary(measurements: Dict, n_particles: int) -> Dict[str, float]:
    """从逐粒子测量数据中计算汇总统计。"""
    summary: Dict[str, float] = {"n_particles": n_particles}

    for key, values in measurements.items():
        if not values:
            continue
        # 仅处理数值列
        try:
            arr = np.array(values, dtype=float)
            summary[f"{key}_mean"] = float(np.mean(arr))
            summary[f"{key}_sum"] = float(np.sum(arr))
            summary[f"{key}_std"] = float(np.std(arr))
            summary[f"{key}_min"] = float(np.min(arr))
            summary[f"{key}_max"] = float(np.max(arr))
        except (ValueError, TypeError):
            pass

    return summary


def export_measurements_csv(
    results: List[BatchResult],
    csv_path: Union[str, Path],
    measurement_keys: Optional[List[str]] = None,
) -> Path:
    """将批量处理结果导出为标准 CSV 表格。

    CSV 结构:
      第一部分: 每个粒子一行 (image_id, particle_idx, 各测量值…)
      第二部分: 每张图像一行汇总 (空行分隔)

    Args:
        results:          BatchResult 列表
        csv_path:         CSV 输出路径
        measurement_keys: 要导出的测量项（None=全部）

    Returns:
        CSV 文件路径
    """
    csv_path = Path(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    # ── 确定所有出现的测量列 ──
    all_keys: set = set()
    for r in results:
        all_keys.update(r.measurements.keys())
    if measurement_keys:
        all_keys = all_keys.intersection(set(measurement_keys))
    all_keys = sorted(all_keys)

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)

        # ── 表头 ──
        header = ["image_id", "particle_idx", "success", "error"] + all_keys
        writer.writerow(header)

        # ── 逐粒子行 ──
        for result in results:
            if not result.success:
                row = [result.image_id, -1, "False", result.error_message]
                row += [""] * len(all_keys)
                writer.writerow(row)
                continue

            if result.particle_count == 0:
                row = [result.image_id, 0, "True", ""]
                for key in all_keys:
                    val = result.summary.get(f"{key}_mean", "")
                    row.append(val)
                writer.writerow(row)
            else:
                max_len = 0
                for v in result.measurements.values():
                    if isinstance(v, list):
                        max_len = max(max_len, len(v))

                for pid in range(max_len):
                    row = [result.image_id, pid, "True", ""]
                    for key in all_keys:
                        values = result.measurements.get(key, [])
                        if pid < len(values):
                            row.append(values[pid])
                        else:
                            row.append("")
                    writer.writerow(row)

        # ── 汇总区 ──
        writer.writerow([])
        writer.writerow(["=== 汇总统计 ==="])
        summary_header = ["image_id", "n_particles"] + [
            f"{k}_mean" for k in all_keys
        ]
        writer.writerow(summary_header)

        for result in results:
            if not result.success:
                continue
            row = [result.image_id, result.particle_count]
            for key in all_keys:
                val = result.summary.get(f"{key}_mean", "")
                row.append(val)
            writer.writerow(row)

    logger.info("CSV 已导出: %s", csv_path)
    return csv_path


def _export_batch_results_json(
    results: List[BatchResult],
    json_path: Path,
    config: AnalysisConfig,
):
    """导出批量结果为 JSON（包含完整配置和逐图像统计）。"""
    data = {
        "config": config.to_dict(),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total_images": len(results),
        "success_count": sum(1 for r in results if r.success),
        "results": [],
    }

    for r in results:
        entry = {
            "image_id": r.image_id,
            "success": r.success,
            "error": r.error_message,
            "duration_sec": round(r.duration_sec, 3),
            "particle_count": r.particle_count,
            "summary": r.summary,
            "mask_path": r.mask_save_path,
            "overlay_path": r.overlay_save_path,
            "roi_path": r.roi_save_path,
        }
        data["results"].append(entry)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════════
#  第八部分: ROI / 叠加层 保存与加载
# ═══════════════════════════════════════════════════════════════════════

def save_roi_overlay(
    ij,
    imp,
    save_path: Union[str, Path],
    roi_manager=None,
    overlay=None,
) -> None:
    """将 ImagePlus、ROI 和叠加层信息一起保存。

    保存为 TIFF + ROI .zip 组合，支持后续批量复用。

    Args:
        ij:          PyImageJ 实例
        imp:         ImagePlus 对象
        save_path:   保存路径（不含扩展名）
        roi_manager: ROI Manager（可选）
        overlay:     Overlay 对象（可选）
    """
    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)

    if overlay is not None:
        imp.setOverlay(overlay)

    # 保存带叠加层的合并图像
    flat = imp.flatten()
    ij.IJ().saveAs(flat, "Tiff", str(save_path.with_suffix(".tiff")))

    # 保存 ROI Manager
    if roi_manager is not None:
        roi_manager.runCommand("Save", str(save_path.with_suffix(".zip")))

    logger.debug("ROI/叠加层已保存: %s", save_path)


def load_roi_overlay(
    ij,
    save_path: Union[str, Path],
):
    """加载之前保存的 ROI 和叠加层。

    Args:
        ij:        PyImageJ 实例
        save_path: 保存路径（不含扩展名）

    Returns:
        (imp, roi_manager) 元组
    """
    RM = _RoiManager()

    save_path = Path(save_path)
    tiff_path = save_path.with_suffix(".tiff")
    roi_zip_path = save_path.with_suffix(".zip")

    imp = None
    if tiff_path.exists():
        imp = ij.IJ().openImage(str(tiff_path))
        if imp:
            imp.show()

    if roi_zip_path.exists():
        rm = RM.getInstance2()
        if rm is None:
            rm = RM()
        rm.runCommand("Open", str(roi_zip_path))
        return imp, rm

    return imp, None


# ═══════════════════════════════════════════════════════════════════════
#  第九部分: 与 pathology-crop-tool 集成接口
# ═══════════════════════════════════════════════════════════════════════

def process_crop_rois(
    rois_data: List[Tuple[str, np.ndarray]],
    config: Union[AnalysisConfig, str, Path],
    output_dir: Union[str, Path],
    fiji_path: Optional[str] = None,
) -> List[BatchResult]:
    """一键处理 pathology-crop-tool 导出的 ROI 图像。

    此函数为 crop 工具集成的主入口。接收 crop 工具输出的
    (ROI标识, numpy数组) 列表，执行完整的 ImageJ 分析流水线。

    Args:
        rois_data:  [(roi_id, numpy_array), ...]
                    roi_id 通常为 "{slide_stem}_ROI_{idx:04d}"
                    numpy_array 为 RGB uint8 (H, W, 3)
        config:     AnalysisConfig 或 JSON 路径
        output_dir: 输出目录
        fiji_path:  本地 Fiji 路径（None=在线）

    Returns:
        BatchResult 列表

    ══════════════════════════════════════════════════════════════
    【接入示例 — 在你的 crop 工具中这样调用】

    from liver_portal_crop.imagej_bridge import process_crop_rois

    # 收集裁剪后的 numpy 数组
    rois_data = []
    for idx, roi in enumerate(all_rois):
        region = reader.extract_region(roi.x, roi.y, roi.w, roi.h, level=0)
        roi_id = f"{roi.slide_path.stem}_ROI_{idx:04d}"
        rois_data.append((roi_id, region))

    # 一键分析
    results = process_crop_rois(
        rois_data=rois_data,
        config="imagej_analysis_config.json",
        output_dir="analysis_results/",
        fiji_path="C:/Fiji.app",   # 本地 Fiji（可选）
    )

    # 查看结果
    for r in results:
        print(f"{r.image_id}: {r.particle_count} 粒子")
    ══════════════════════════════════════════════════════════════
    """
    return run_headless_batch(
        images=rois_data,
        config=config,
        output_dir=output_dir,
        fiji_path=fiji_path,
    )


def tune_single_roi(
    sample_image: np.ndarray,
    roi_label: str = "样本",
    fiji_path: Optional[str] = None,
    config_save_path: Optional[str] = None,
    pixel_size: float = 1.0,
    unit: str = "pixel",
) -> AnalysisConfig:
    """对单张 ROI 样本启动 GUI 调参。

    在你的 ROI 预览面板中，双击某个 ROI 时可调用此函数启动调参。

    Args:
        sample_image:     numpy (H,W,3) uint8 RGB
        roi_label:        标签（显示在 Fiji 标题栏）
        fiji_path:        本地 Fiji 路径
        config_save_path: 配置保存路径
        pixel_size:       像素物理尺寸（µm/px）
        unit:             单位字符串

    Returns:
        AnalysisConfig
    """
    scale = (pixel_size, unit) if unit != "pixel" else None
    return launch_gui_tuning(
        sample_image=sample_image,
        fiji_path=fiji_path,
        title=f"调参 — {roi_label}",
        config_save_path=config_save_path,
        scale=scale,
    )


# ═══════════════════════════════════════════════════════════════════════
#  第十部分: 工具函数
# ═══════════════════════════════════════════════════════════════════════

def _sanitize_title(name: str) -> str:
    """清理图像标题（去除非法字符）。"""
    return name.replace("\n", " ").replace("\r", "")[:200]


def _sanitize_filename(name: str) -> str:
    """将任意字符串转为安全的文件名。"""
    invalid = '<>:"/\\|?*\x00'
    safe = name
    for c in invalid:
        safe = safe.replace(c, "_")
    return safe[:100]


# ═══════════════════════════════════════════════════════════════════════
#  第十一部分: 独立运行入口（演示 + 测试）
# ═══════════════════════════════════════════════════════════════════════

def main(cli_args=None):
    """主入口函数，可从命令行或子进程调用。"""
    import argparse

    parser = argparse.ArgumentParser(
        description="PyImageJ 病理图像分析桥接工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 生成演示图像并启动 GUI 调参
  python imagej_bridge.py --gui --demo

  # 对单张图像调参（指定本地 Fiji）
  python imagej_bridge.py --gui --input sample.tiff --fiji "C:/Fiji.app"

  # 批量处理目录中的所有图像
  python imagej_bridge.py --batch --input ./rois/ --config config.json --output ./results/

  # 创建默认 IHC 配置文件
  python imagej_bridge.py --create-default-config ihc_config.json
        """,
    )

    parser.add_argument("--gui", action="store_true", help="启动 GUI 调参模式")
    parser.add_argument("--batch", action="store_true", help="headless 批量处理模式")
    parser.add_argument("--input", type=str, default=None, help="输入路径（文件/目录）")
    parser.add_argument("--output", type=str, default="./imagej_output", help="输出目录")
    parser.add_argument("--config", type=str, default=None, help="配置文件路径")
    parser.add_argument("--save-config", type=str, default=None, help="GUI 调参配置保存路径")
    parser.add_argument("--fiji", type=str, default=None, help="本地 Fiji 路径")
    parser.add_argument("--demo", action="store_true", help="使用演示图像")
    parser.add_argument("--show-config", type=str, default=None, help="查看配置文件")
    parser.add_argument("--create-default-config", type=str, default=None, help="创建默认配置")

    # 支持从环境变量读取参数（用于子进程调用）
    env_gui = os.environ.pop("IMAGEJ_BRIDGE_GUI", None)
    env_input = os.environ.pop("IMAGEJ_BRIDGE_INPUT", None)
    env_fiji = os.environ.pop("IMAGEJ_BRIDGE_FIJI", None)
    env_save_config = os.environ.pop("IMAGEJ_BRIDGE_SAVE_CONFIG", None)
    env_batch = os.environ.pop("IMAGEJ_BRIDGE_BATCH", None)
    env_demo = os.environ.pop("IMAGEJ_BRIDGE_DEMO", None)

    args = parser.parse_args(cli_args)

    # 环境变量优先级高于命令行参数（用于子进程传参）
    if env_gui is not None:
        args.gui = env_gui == "1"
    if env_input is not None:
        args.input = env_input
    if env_fiji is not None:
        args.fiji = env_fiji
    if env_save_config is not None:
        args.save_config = env_save_config
    if env_batch is not None:
        args.batch = env_batch == "1"
    if env_demo is not None:
        args.demo = env_demo == "1"

    # ── 查看配置 ──
    if args.show_config:
        cfg = AnalysisConfig.load(args.show_config)
        print(json.dumps(cfg.to_dict(), indent=2, ensure_ascii=False))
        exit(0)

    # ── 创建默认配置 ──
    if args.create_default_config:
        cfg = AnalysisConfig(
            config_name="IHC_DAB_默认",
            description="免疫组化 DAB 染色阳性分析默认配置",
            threshold_method="Default",
            auto_threshold=True,
            morph_operation="open",
            morph_radius=2,
            morph_iterations=1,
            particle_min_size=50,
            particle_max_size=50000,
            particle_circularity_min=0.1,
            particle_circularity_max=1.0,
            exclude_edge_particles=True,
            measurements=[
                "Area", "Mean", "StdDev", "Min", "Max",
                "IntegratedDensity", "Circularity", "Feret",
            ],
        )
        cfg.save(args.create_default_config)
        print(f"默认配置已创建: {args.create_default_config}")
        exit(0)

    # ── 加载图像 ──
    def _load_images(input_path: str, demo: bool) -> List[Tuple[str, np.ndarray]]:
        images = []
        if demo:
            logger.info("生成演示 IHC 图像…")
            rng = np.random.RandomState(42)
            for i in range(3):
                h, w = 512, 512
                img = np.ones((h, w, 3), dtype=np.uint8) * 230  # 浅背景
                n_spots = rng.randint(5, 25)
                for _ in range(n_spots):
                    cy, cx = rng.randint(50, h - 50), rng.randint(50, w - 50)
                    r = rng.randint(10, 40)
                    y, x = np.ogrid[:h, :w]
                    mask = ((y - cy) ** 2 + (x - cx) ** 2) <= r ** 2
                    img[mask, 0] = rng.randint(80, 140)
                    img[mask, 1] = rng.randint(50, 100)
                    img[mask, 2] = rng.randint(20, 60)
                images.append((f"demo_ihc_{i:03d}", img))
        elif input_path:
            p = Path(input_path)
            if p.is_file():
                import tifffile
                arr = tifffile.imread(str(p))
                if arr.ndim == 2:
                    arr = np.stack([arr] * 3, axis=-1)
                images.append((p.stem, arr.astype(np.uint8)))
            elif p.is_dir():
                import tifffile
                exts = {".tiff", ".tif", ".png", ".jpg", ".jpeg", ".bmp"}
                files = sorted(f for f in p.iterdir() if f.suffix.lower() in exts)
                for f in files:
                    try:
                        arr = tifffile.imread(str(f))
                        if arr.ndim == 2:
                            arr = np.stack([arr] * 3, axis=-1)
                        images.append((f.stem, arr.astype(np.uint8)))
                    except Exception as e:
                        logger.warning("加载失败 %s: %s", f, e)
        return images

    images = _load_images(args.input or "", args.demo)

    # ── GUI 模式 ──
    if args.gui:
        if not images:
            logger.error("GUI 模式需要至少一张图像（--input 或 --demo）")
            exit(1)
        sample_id, sample_img = images[0]
        config = tune_single_roi(
            sample_image=sample_img,
            roi_label=sample_id,
            fiji_path=args.fiji,
            config_save_path=args.save_config,
        )
        print("\n配置内容:")
        print(json.dumps(config.to_dict(), indent=2, ensure_ascii=False))

    # ── 批量模式 ──
    elif args.batch:
        if not images:
            logger.error("批量模式需要输入图像（--input 或 --demo）")
            exit(1)
        config = (
            AnalysisConfig.load(args.config) if args.config
            else AnalysisConfig(config_name="默认IHC分析")
        )
        results = run_headless_batch(
            images=images,
            config=config,
            output_dir=args.output,
            fiji_path=args.fiji,
        )
        print("\n" + "=" * 60)
        for r in results:
            s = "✓" if r.success else "✗"
            n = r.particle_count if r.success else "N/A"
            print(f"  {s} {r.image_id}: {n} 粒子, {r.duration_sec:.1f}s")
            if r.error_message:
                print(f"    错误: {r.error_message}")
        print("=" * 60)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
