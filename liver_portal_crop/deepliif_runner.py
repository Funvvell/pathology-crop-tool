"""DeepLIIF 推理引擎 — 封装本地 PyTorch 和云端 API 双模式推理。"""

from __future__ import annotations

import base64
import logging
import os
import sys
import types
from enum import Enum
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image
from PySide6.QtCore import QObject, Signal


# ═══════════════════════════════════════════════════
#  Mock bioformats/javabridge（需要 Java，一般未安装）
#  DeepLIIF 的 WSI 读取功能依赖这些，但推理不需要
# ═══════════════════════════════════════════════════

def _ensure_deepliif_deps():
    """为 deepliif.models 导入提供缺失依赖的 stub。"""
    mocks = {}
    if 'javabridge' not in sys.modules:
        m = types.ModuleType('javabridge')
        m.start_vm = lambda *a, **kw: None
        m.kill_vm = lambda *a: None
        m.JBException = Exception
        mocks['javabridge'] = m
    if 'bioformats' not in sys.modules:
        m = types.ModuleType('bioformats')
        m.set_log_level = lambda *a: None
        m.omexml = types.ModuleType('bioformats.omexml')
        m.omexml.OMEXML = type('OMEXML', (), {})
        mocks['bioformats'] = m
        mocks['bioformats.omexml'] = m.omexml
    for name, mod in mocks.items():
        sys.modules[name] = mod

_ensure_deepliif_deps()

from liver_portal_crop.roi import ROIModel

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════
#  枚举与常量
# ═══════════════════════════════════════════════════

class DeepLIIFMode(Enum):
    """推理模式。"""
    LOCAL = "local"   # 本地 PyTorch 推理
    CLOUD = "cloud"   # deepliif.org 云端 API


# Tile Size → 推荐倍率映射
_MAG_TILE_MAP = {
    "40x": 512, "80x": 512,
    "20x": 256,
    "10x": 128, "4x": 128,
}


# ═══════════════════════════════════════════════════
#  辅助函数
# ═══════════════════════════════════════════════════

def tile_size_for_magnification(magnification: str) -> int:
    """根据倍率推荐 tile size。"""
    return _MAG_TILE_MAP.get(magnification, 512)


def resolution_for_tile_size(tile_size: int) -> str:
    if tile_size > 384:
        return "40x"
    if tile_size > 192:
        return "20x"
    return "10x"


def get_default_model_dir() -> Path:
    """获取默认模型目录 (~/.deepliif/models)，不存在则创建。"""
    d = Path.home() / ".deepliif" / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def check_model_available(model_dir: str | Path) -> tuple[bool, str]:
    """检查模型文件是否可用。

    Returns:
        (available, message)
    """
    model_path = Path(model_dir)
    if not model_path.exists():
        return False, (
            f"模型目录不存在: {model_path}\n\n"
            f"请先下载模型:\n{MODEL_DOWNLOAD_URL}"
        )

    opt_file = model_path / "train_opt.txt"
    if not opt_file.exists():
        opt_file = model_path / "test_opt.txt"

    pt_files = list(model_path.glob("*.pt"))
    pth_files = list(model_path.glob("*_net_*.pth"))

    if not opt_file and not pt_files and not pth_files:
        # 目录存在但为空 — 可能是首次使用
        return False, (
            "模型目录为空，需要下载预训练模型文件。\n\n"
            f"下载地址:\n{MODEL_DOWNLOAD_URL}\n\n"
            f"下载后解压到:\n{model_path}"
        )

    if not opt_file:
        return False, "缺少 train_opt.txt / test_opt.txt 配置文件"

    if not pt_files and not pth_files:
        return False, "未找到模型文件 (.pt 或 .pth)"

    return True, f"模型就绪 ({len(pt_files)} 个 .pt 文件)"


MODEL_DOWNLOAD_URL = "https://zenodo.org/record/4751737/files/DeepLIIF_Latest_Model.zip"


# ═══════════════════════════════════════════════════
#  后处理（绕过 deepliif.models 避免导入 bioformats）
# ═══════════════════════════════════════════════════

def reprocess(
    orig: Image.Image,
    images: dict[str, Image.Image],
    tile_size: int = 512,
    seg_thresh: int = 120,
    size_thresh: int = 7,
    marker_thresh: int | None = None,
    size_thresh_upper: int | None = None,
    resolution: str | None = None,
) -> tuple[dict[str, Image.Image], dict]:
    """用新阈值重新计算分割结果和 IHC 评分。

    直接调用 deepliif.postprocessing.compute_final_results，
    不经过 deepliif.models（避免触发 bioformats 导入）。

    Args:
        orig: 原始 IHC 图像 (PIL.Image)
        images: 推理结果 dict，需要 'Seg'，可选 'Marker'
        tile_size: 推理时使用的 tile size
        seg_thresh: 分割概率阈值 (0-254)
        size_thresh: 细胞大小过滤阈值 (sqrt 值)
        marker_thresh: marker 强度阈值（可选）
        size_thresh_upper: 最大细胞大小阈值（可选）

    Returns:
        (processed_images, scoring)
    """
    from deepliif.postprocessing import compute_final_results

    # 分辨率推断
    if resolution is None:
        resolution = resolution_for_tile_size(tile_size)

    # 找到 Marker 图像
    target_size = orig.size
    working_images = dict(images)
    seg_img = working_images.get("Seg")
    if seg_img is not None and seg_img.size != target_size:
        working_images["Seg"] = seg_img.resize(target_size, Image.BILINEAR)

    marker_key = None
    for k in working_images:
        if k.endswith("Marker"):
            marker_key = k
            break
    marker_img = working_images.get(marker_key) if marker_key else None
    if marker_img is not None and marker_img.size != target_size:
        marker_img = marker_img.resize(target_size, Image.BILINEAR)

    overlay, refined, scoring = compute_final_results(
        orig, working_images["Seg"], marker_img, resolution,
        size_thresh, marker_thresh, size_thresh_upper, seg_thresh,
    )

    processed_images = {
        "SegOverlaid": Image.fromarray(overlay),
        "SegRefined": Image.fromarray(refined),
    }
    return processed_images, scoring


class ModelDownloadWorker(QObject):
    """模型下载 Worker，在 QThread 中运行。"""

    progress = Signal(int, int, int)  # percent(0-100), downloaded_MB, total_MB
    status = Signal(str)              # 状态消息
    finished = Signal(bool, str)      # success, message
    _cancel = False

    def __init__(self, model_dir: str | Path, parent=None):
        super().__init__(parent)
        self._model_dir = Path(model_dir)

    def cancel(self):
        self._cancel = True

    def run(self):
        """执行下载。在 QThread 中调用。"""
        import requests
        import zipfile

        model_path = self._model_dir
        model_path.mkdir(parents=True, exist_ok=True)
        zip_path = model_path / "DeepLIIF_Latest_Model.zip"

        try:
            self.status.emit("正在连接服务器...")
            resp = requests.get(MODEL_DOWNLOAD_URL, stream=True, timeout=30)
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))
            downloaded = 0

            self.status.emit(f"开始下载 ({total / 1024 / 1024:.0f} MB)...")
            total_mb = total // (1024 * 1024)
            with open(zip_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if self._cancel:
                        f.close()
                        zip_path.unlink(missing_ok=True)
                        self.finished.emit(False, "下载已取消")
                        return
                    f.write(chunk)
                    downloaded += len(chunk)
                    pct = int(downloaded * 100 / total) if total else 0
                    dl_mb = downloaded // (1024 * 1024)
                    self.progress.emit(pct, dl_mb, total_mb)

            self.status.emit("下载完成，正在解压...")
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(model_path)

            zip_path.unlink(missing_ok=True)

            # 处理嵌套目录
            subdirs = [d for d in model_path.iterdir() if d.is_dir()]
            if len(subdirs) == 1 and not any(model_path.glob("*.pt")):
                nested = subdirs[0]
                for item in nested.iterdir():
                    dest = model_path / item.name
                    if not dest.exists():
                        item.rename(dest)
                try:
                    nested.rmdir()
                except OSError:
                    pass

            self.status.emit("正在验证模型...")
            ok, msg = check_model_available(model_path)
            if ok:
                self.finished.emit(True, f"模型下载完成: {msg}")
            else:
                self.finished.emit(False, f"下载完成但验证失败: {msg}")

        except requests.ConnectionError:
            self.finished.emit(False, "网络连接失败，请检查网络后重试")
        except requests.Timeout:
            self.finished.emit(False, "连接服务器超时，请稍后重试")
        except Exception as e:
            self.finished.emit(False, f"下载失败: {e}")


def extract_roi_as_pil(reader, roi: ROIModel) -> Image.Image:
    """从 SDPCReader 提取 ROI 区域并转换为 PIL.Image (RGB)。

    Args:
        reader: SDPCReader 实例
        roi: ROIModel 数据

    Returns:
        PIL.Image in RGB mode
    """
    region = reader.extract_region(roi.x, roi.y, roi.w, roi.h, level=0)
    return Image.fromarray(region, mode="RGB")


# ═══════════════════════════════════════════════════
#  本地推理
# ═══════════════════════════════════════════════════

def infer_local(
    img: Image.Image,
    model_dir: str | Path,
    tile_size: int = 512,
    seg_only: bool = False,
) -> tuple[dict[str, Image.Image], dict | None]:
    """使用本地 PyTorch 模型运行 DeepLIIF 推理。

    Args:
        img: 输入 IHC 图像 (PIL.Image, RGB)
        model_dir: 模型目录路径
        tile_size: 推理 tile 大小
        seg_only: 仅运行分割（更快）

    Returns:
        (images, scoring) — 与 deepliif.models.infer_modalities 返回值相同
    """
    from deepliif.models import infer_modalities

    model_dir = str(model_dir)
    logger.info("本地推理: model=%s, tile=%d, seg_only=%s", model_dir, tile_size, seg_only)

    images, scoring = infer_modalities(
        img=img,
        tile_size=tile_size,
        model_dir=model_dir,
        eager_mode=False,
        seg_only=seg_only,
    )
    return images, scoring


# ═══════════════════════════════════════════════════
#  云端推理
# ═══════════════════════════════════════════════════

def infer_cloud(
    img: Image.Image,
    resolution: str = "40x",
    seg_only: bool = False,
    max_size: int = 2048,
) -> tuple[dict[str, Image.Image], dict | None]:
    """使用 deepliif.org 云端 API 运行推理。

    Args:
        img: 输入 IHC 图像 (PIL.Image, RGB)
        resolution: 扫描倍率 (10x/20x/40x)
        seg_only: 仅返回分割结果
        max_size: 上传图像最大边长（超过则缩放，避免超时）

    Returns:
        (images, scoring) — 与本地推理返回格式一致

    Raises:
        ConnectionError: 网络请求失败
        RuntimeError: API 返回错误
    """
    import requests

    # 大图缩放，避免云端超时
    send_img = img.convert("RGB")
    w, h = send_img.size
    original_size = (w, h)
    if max(w, h) > max_size:
        ratio = max_size / max(w, h)
        send_img = send_img.resize(
            (int(w * ratio), int(h * ratio)), Image.LANCZOS,
        )
        logger.info("图像过大 (%dx%d)，缩放至 %dx%d 上传", w, h, *send_img.size)

    # 将 PIL Image 转为 PNG bytes
    buf = BytesIO()
    send_img.save(buf, format="PNG", optimize=True)
    img_bytes = buf.getvalue()

    params = {"resolution": resolution}
    if seg_only:
        params["slim"] = "true"

    logger.info("云端推理: resolution=%s, seg_only=%s, size=%dx%d, upload=%dKB",
                resolution, seg_only, *send_img.size, len(img_bytes) // 1024)

    try:
        resp = requests.post(
            url="https://deepliif.org/api/infer",
            files={"img": ("roi.png", img_bytes, "image/png")},
            params=params,
            timeout=300,  # 5 分钟超时
        )
        resp.raise_for_status()
    except requests.ConnectionError as e:
        raise ConnectionError(f"无法连接 DeepLIIF 云端服务，请检查网络: {e}") from e
    except requests.Timeout as e:
        raise ConnectionError("云端推理超时 (5 分钟)") from e
    except requests.HTTPError as e:
        raise RuntimeError(f"云端 API 返回错误: {resp.status_code} {resp.text[:200]}") from e

    data = resp.json()

    # 解码 base64 图像
    images: dict[str, Image.Image] = {}
    for name, b64_str in data.get("images", {}).items():
        try:
            img_data = base64.b64decode(b64_str)
            decoded = Image.open(BytesIO(img_data)).convert("RGB")
            if decoded.size != original_size:
                resample = Image.NEAREST if "Mask" in name else Image.BILINEAR
                decoded = decoded.resize(original_size, resample)
            images[name] = decoded
        except Exception as e:
            logger.warning("解码模态图像 %s 失败: %s", name, e)

    scoring = data.get("scoring")
    return images, scoring


# ═══════════════════════════════════════════════════
#  小块裁剪 + 分块拼接推理
# ═══════════════════════════════════════════════════

def crop_test_patch(img: Image.Image, patch_size: int = 512) -> Image.Image:
    """从图像中心裁剪一小块用于参数测试。

    Args:
        img: 原始 ROI 图像
        patch_size: 小块边长（像素）

    Returns:
        中心裁剪的小块图像
    """
    w, h = img.size
    cx, cy = w // 2, h // 2
    half = patch_size // 2
    left = max(0, cx - half)
    top = max(0, cy - half)
    right = min(w, left + patch_size)
    bottom = min(h, top + patch_size)
    return img.crop((left, top, right, bottom))


def infer_tiled(
    img: Image.Image,
    mode: DeepLIIFMode,
    tile_size: int = 512,
    overlap: int = 64,
    model_dir: str | None = None,
    resolution: str = "40x",
    seg_only: bool = False,
    progress_cb=None,
    max_workers: int | None = None,
) -> tuple[dict[str, Image.Image], dict | None]:
    """分块推理 + 拼接。云端模式用大块 (2000px)，本地模式由 DeepLIIF 内部处理。

    Args:
        img: 原始 ROI 图像
        mode: 推理模式
        tile_size: 本地模型内部 tile size（512/256/128）
        overlap: 块间重叠像素数
        model_dir: 本地模型路径
        resolution: 云端倍率
        seg_only: 仅分割
        progress_cb: progress_cb(current, total)
        max_workers: 并发线程数
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    w, h = img.size

    # 小图直接推理
    if w <= 2048 and h <= 2048:
        if mode == DeepLIIFMode.LOCAL:
            return infer_local(img, model_dir, tile_size, seg_only)
        else:
            return infer_cloud(img, resolution, seg_only)

    # 云端模式：用 2000px 大块（与小块预览效果一致）
    # 本地模式：用 tile_size（DeepLIIF 内部自动处理）
    if mode == DeepLIIFMode.LOCAL:
        return infer_local(img, model_dir, tile_size, seg_only)

    api_tile = 2000
    api_overlap = 200
    if max_workers is None:
        max_workers = 6

    stride = api_tile - api_overlap
    xs = list(range(0, max(1, w - api_tile + 1), stride))
    if not xs or xs[-1] + api_tile < w:
        xs.append(max(0, w - api_tile))
    ys = list(range(0, max(1, h - api_tile + 1), stride))
    if not ys or ys[-1] + api_tile < h:
        ys.append(max(0, h - api_tile))

    total_tiles = len(xs) * len(ys)
    logger.info("云端分块: %dx%d -> %d 块 (%dpx, overlap=%d), %d 线程",
                w, h, total_tiles, api_tile, api_overlap, max_workers)

    tiles = []
    for y in ys:
        for x in xs:
            tiles.append((x, y, img.crop((x, y, x + api_tile, y + api_tile))))

    def _infer_one(args):
        x, y, tile = args
        images, scoring = infer_cloud(tile, resolution, seg_only)
        return x, y, images, scoring

    completed = 0
    lock = threading.Lock()
    results_map: dict[tuple[int, int], dict] = {}
    accum_scoring = {"num_total": 0, "num_pos": 0, "num_neg": 0}

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_infer_one, t): t for t in tiles}
        for future in as_completed(futures):
            with lock:
                completed += 1
            try:
                x, y, t_images, t_scoring = future.result()
                results_map[(x, y)] = {"images": t_images, "scoring": t_scoring}
                if t_scoring:
                    accum_scoring["num_total"] += t_scoring.get("num_total", 0)
                    accum_scoring["num_pos"] += t_scoring.get("num_pos", 0)
                    accum_scoring["num_neg"] += t_scoring.get("num_neg", 0)
            except Exception as e:
                ox, oy, _ = futures[future]
                logger.warning("分块 (%d,%d) 失败: %s", ox, oy, e)
            if progress_cb:
                progress_cb(completed, total_tiles)

    # 拼接
    result_images: dict[str, Image.Image] = {}
    half_ov = api_overlap // 2

    for y in ys:
        for x in xs:
            tile_result = results_map.get((x, y))
            if tile_result is None:
                continue
            t_images = tile_result["images"]
            for key, t_img in t_images.items():
                if key not in result_images:
                    result_images[key] = Image.new("RGB", (w, h), (0, 0, 0))
                tile_left = 0 if x == 0 else half_ov
                tile_top = 0 if y == 0 else half_ov
                tile_right = api_tile if x + api_tile >= w else api_tile - half_ov
                tile_bottom = api_tile if y + api_tile >= h else api_tile - half_ov
                px_left = x if x == 0 else x + half_ov
                px_top = y if y == 0 else y + half_ov
                result_images[key].paste(
                    t_img.crop((tile_left, tile_top, tile_right, tile_bottom)),
                    (px_left, px_top),
                )

    total = accum_scoring["num_total"]
    if total > 0:
        accum_scoring["percent_pos"] = round(accum_scoring["num_pos"] * 100.0 / total, 1)
    scoring = accum_scoring if total > 0 else None
    return result_images, scoring


# ═══════════════════════════════════════════════════
#  批量推理 Worker（在 QThread 中运行）
# ═══════════════════════════════════════════════════

class DeepLIIFWorker(QObject):
    """DeepLIIF 批量推理 Worker。

    在 QThread 中运行，通过信号报告进度和结果。

    Signals:
        progress(message, current, total)
        result_ready(roi_id, result_dict)
        all_finished(list_of_results)
        error(error_message)
    """

    progress = Signal(str, int, int)
    result_ready = Signal(str, dict)
    all_finished = Signal(list)
    error = Signal(str)

    def __init__(
        self,
        mode: DeepLIIFMode,
        rois: list[ROIModel],
        readers: dict,
        model_dir: str | None = None,
        tile_size: int = 512,
        seg_only: bool = False,
        resolution: str | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self._mode = mode
        self._rois = rois
        self._readers = readers  # slide_path -> SDPCReader
        self._model_dir = model_dir
        self._tile_size = tile_size
        self._seg_only = seg_only
        self._resolution = resolution or resolution_for_tile_size(tile_size)
        self._cancel = False

    def cancel(self):
        """请求取消推理。"""
        self._cancel = True

    def run(self):
        """执行批量推理。应在 QThread 中调用。"""
        results: list[dict] = []
        total = len(self._rois)

        # 本地模式：验证模型
        if self._mode == DeepLIIFMode.LOCAL:
            if not self._model_dir:
                self.error.emit("本地模式需要指定模型目录")
                return
            ok, msg = check_model_available(self._model_dir)
            if not ok:
                self.error.emit(f"模型不可用: {msg}")
                return

        for i, roi in enumerate(self._rois):
            if self._cancel:
                self.progress.emit("已取消", i, total)
                break

            slide_name = roi.slide_path.stem
            self.progress.emit(
                f"[{i+1}/{total}] 分析 {slide_name} ROI...",
                i, total,
            )

            # 提取 ROI 图像
            reader = self._readers.get(roi.slide_path)
            if reader is None:
                logger.warning("未找到 reader: %s", roi.slide_path)
                continue

            try:
                img = extract_roi_as_pil(reader, roi)
            except Exception as e:
                logger.error("提取 ROI 失败 (roi=%s): %s", roi.id, e)
                continue

            # 运行推理
            try:
                if self._mode == DeepLIIFMode.LOCAL:
                    # 本地模型无大小限制，直接跑原图
                    self.progress.emit(
                        f"[{i+1}/{total}] {slide_name} 本地推理中...", i, total,
                    )
                    images, scoring = infer_local(
                        img, self._model_dir, self._tile_size, self._seg_only,
                    )
                else:
                    # 云端 API 有大小限制，分块拼接
                    def _tile_progress(cur, tot):
                        self.progress.emit(
                            f"[{i+1}/{total}] {slide_name} 分块 {cur}/{tot}...",
                            i * tot + cur, total * tot,
                        )
                    images, scoring = infer_tiled(
                        img=img, mode=self._mode,
                        tile_size=self._tile_size, overlap=64,
                        model_dir=self._model_dir, resolution=self._resolution,
                        seg_only=self._seg_only, progress_cb=_tile_progress,
                    )
            except Exception as e:
                logger.error("推理失败 (roi=%s): %s", roi.id, e)
                self.error.emit(f"ROI {roi.id} 推理失败: {e}")
                continue

            # 保存原始 IHC 图像供 postprocess 使用
            images["IHC"] = img

            result = {
                "roi_id": roi.id,
                "roi": roi,
                "images": images,
                "scoring": scoring,
                "tile_size": self._tile_size,
            }
            results.append(result)
            self.result_ready.emit(roi.id, result)

        self.progress.emit("完成", total, total)
        self.all_finished.emit(results)
