"""IHC 阳性热点自动检测与 ROI 生成。

算法流程（逐块处理，内存安全）：
  1. 从 WSI 金字塔的指定层级 **逐块** 读取（每次 ~2048×2048）
  2. 对每块做颜色反卷积（Ruifrok & Johnston）+ 阈值分割
  3. 将阳性 mask 累加到一个 **降采样** 的全局 mask（~17 MB）
  4. 同时构建一个适度降采样的预览图供 UI 显示
  5. 全局 mask 上做滑动窗口密度图 → Top-N 热点提取
  6. 可选：在 level-0 全分辨率上对候选区域做精确验证

峰值内存 ≈ 单块 float64 (~100 MB) + 全局 mask (~17 MB) + 预览图 (~5 MB)
"""

from __future__ import annotations

import logging
import math
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

import cv2
import numpy as np
from scipy import ndimage as ndi
from skimage.color import (
    separate_stains,
    hed_from_rgb,
    hdx_from_rgb,
    hax_from_rgb,
)

from liver_portal_crop.tissue_detect import detect_tissue

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
#  染色矩阵注册表
# ═══════════════════════════════════════════════════════════════

STAIN_MATRICES: dict[str, np.ndarray] = {
    "H-DAB": hdx_from_rgb,   # Hematoxylin + DAB (棕色阳性)
    "H-AEC": hax_from_rgb,   # Hematoxylin + AEC (红色阳性)
    "H-E":   hed_from_rgb,   # Hematoxylin + Eosin (粉/蓝)
}

STAIN_LABELS = list(STAIN_MATRICES.keys())


# ═══════════════════════════════════════════════════════════════
#  快速单通道反卷积（QuPath LUT 策略）
# ═══════════════════════════════════════════════════════════════

def _make_od_lut() -> np.ndarray:
    """预计算 256 项 OD 查表（匹配 skimage separate_stains 公式）。

    公式: OD(v) = ln(max(v/255, 1e-6)) / ln(1e-6)
    返回: float32 数组 shape (256,)
    """
    log_adjust = np.float64(np.log(1e-6))
    lut = np.zeros(256, dtype=np.float32)
    for v in range(256):
        fv = max(v / 255.0, 1e-6)
        lut[v] = np.float32(np.log(fv) / log_adjust)
    return lut

# 模块级缓存 — 只算一次
_OD_LUT: np.ndarray | None = None


def _get_od_lut() -> np.ndarray:
    global _OD_LUT
    if _OD_LUT is None:
        _OD_LUT = _make_od_lut()
    return _OD_LUT


def _get_positive_weights(stain_type: str) -> np.ndarray:
    """返回提取阳性通道（第 2 通道）的权重向量。

    对应 skimage: stains[:, :, 1] = OD_norm @ conv_matrix[:, 1]
    """
    matrix = STAIN_MATRICES.get(stain_type)
    if matrix is None:
        raise ValueError(f"未知染色类型: {stain_type}")
    return matrix[:, 1].astype(np.float32).copy()


def _get_h_weights(stain_type: str) -> np.ndarray:
    """返回提取苏木精通道（第 1 通道）的权重向量。

    对应 skimage: stains[:, :, 0] = OD_norm @ conv_matrix[:, 0]
    用于折叠区域检测：折叠区域 H 通道 OD 高但 DAB 通道 OD 低。
    """
    matrix = STAIN_MATRICES.get(stain_type)
    if matrix is None:
        raise ValueError(f"未知染色类型: {stain_type}")
    return matrix[:, 0].astype(np.float32).copy()


def _fast_positive_od(img_rgb: np.ndarray, dab_weights: np.ndarray) -> np.ndarray:
    """快速提取阳性通道浓度图（仅 1 个通道）。

    用预计算 OD 查表 + 单通道矩阵点积替代 skimage separate_stains。
    速度 ~3.8x，内存 ~6x 低于 separate_stains（只算 1 通道 float32）。

    Args:
        img_rgb: uint8 RGB 图像 (H, W, 3)
        dab_weights: 阳性通道权重 (3,) float32，来自 _get_positive_weights

    Returns:
        阳性通道浓度 (H, W) float32，值 ≥ 0
    """
    lut = _get_od_lut()
    od = lut[img_rgb]  # (H, W, 3) float32 — 仅查表，无 log 运算
    pos = np.dot(od, dab_weights)  # (H, W) float32
    np.maximum(pos, 0.0, out=pos)
    return pos


# ═══════════════════════════════════════════════════════════════
#  1. 颜色反卷积
# ═══════════════════════════════════════════════════════════════

def color_deconvolution(
    img_rgb: np.ndarray,
    stain_type: str = "H-DAB",
) -> dict[str, np.ndarray]:
    """颜色反卷积，分离染色通道。

    Args:
        img_rgb: RGB 图像 (H, W, 3), uint8
        stain_type: 染色类型，见 STAIN_LABELS

    Returns:
        {
            "positive":     阳性通道浓度图 (float64),
            "counterstain": 对照通道浓度图 (float64),
            "residual":     残余通道浓度图 (float64),
        }
    """
    matrix = STAIN_MATRICES.get(stain_type)
    if matrix is None:
        raise ValueError(f"未知染色类型: {stain_type}，可选: {STAIN_LABELS}")

    stains = separate_stains(img_rgb, matrix)
    return {
        "counterstain": stains[:, :, 0],
        "positive":     stains[:, :, 1],
        "residual":     stains[:, :, 2],
    }


# ═══════════════════════════════════════════════════════════════
#  2. 阳性区域阈值分割
# ═══════════════════════════════════════════════════════════════

def threshold_positive(
    positive_channel: np.ndarray,
    method: str = "otsu",
    manual_threshold: float = 0.3,
    min_area: int = 50,
) -> np.ndarray:
    """对阳性通道做阈值分割，生成二值阳性 mask。

    Args:
        positive_channel: 颜色反卷积后的阳性通道浓度图 (float64)
        method: "otsu" 自适应 或 "manual" 手动
        manual_threshold: 手动阈值 0~1（仅 manual 模式）
        min_area: 最小阳性连通域面积（像素），碎片过滤

    Returns:
        二值 mask (H, W), bool
    """
    ch = positive_channel.copy()
    ch_min, ch_max = ch.min(), ch.max()
    if ch_max > ch_min:
        ch_norm = ((ch - ch_min) / (ch_max - ch_min) * 255).astype(np.uint8)
    else:
        return np.zeros(ch.shape, dtype=bool)

    if method == "otsu":
        _, binary = cv2.threshold(
            ch_norm, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU,
        )
    else:
        thr_val = int(manual_threshold * 255)
        _, binary = cv2.threshold(ch_norm, thr_val, 255, cv2.THRESH_BINARY)

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)

    if min_area > 0:
        num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(binary)
        for i in range(1, num_labels):
            if stats[i, cv2.CC_STAT_AREA] < min_area:
                binary[labels == i] = 0

    return binary > 0


# ═══════════════════════════════════════════════════════════════
#  3. 密度图计算（滑动窗口）
# ═══════════════════════════════════════════════════════════════

def compute_density_map(
    positive_mask: np.ndarray,
    window_size: int,
    stride: int | None = None,
) -> np.ndarray:
    """用滑动窗口（uniform_filter 加速）计算阳性像素密度图。

    Args:
        positive_mask: 二值阳性 mask (H, W), bool
        window_size: 滑动窗口尺寸（像素）
        stride: 步长，默认 = window_size // 2

    Returns:
        密度图 (H', W'), float32, 值域 [0, 1]
    """
    if stride is None:
        stride = max(1, window_size // 2)

    density_full = ndi.uniform_filter(
        positive_mask.astype(np.float32),
        size=window_size,
        mode="constant",
    )
    return density_full[::stride, ::stride]


# ═══════════════════════════════════════════════════════════════
#  4. 热点区域提取
# ═══════════════════════════════════════════════════════════════

def find_hotspots(
    density_map: np.ndarray,
    stride: int,
    roi_w: int,
    roi_h: int,
    scale_x: float,
    scale_y: float,
    n_hotspots: int = 5,
    min_density: float = 0.05,
    nms_distance: int | None = None,
) -> list[tuple[int, int, int, int, float]]:
    """从密度图中提取 Top-N 热点区域。

    Args:
        density_map: 密度图 (H', W')
        stride: 密度图相对原图的步长
        roi_w, roi_h: ROI 框尺寸（全分辨率像素）
        scale_x, scale_y: 密度图坐标 → 全分辨率坐标的比例因子
        n_hotspots: 返回的最大热点数
        min_density: 最低阳性密度阈值
        nms_distance: 非极大值抑制距离（全分辨率像素）

    Returns:
        [(x, y, w, h, density), ...] 坐标为 level-0 全分辨率空间
    """
    if nms_distance is None:
        nms_distance = max(min(roi_w, roi_h) // 2, 1)

    flat_indices = np.argsort(-density_map.ravel())
    selected: list[tuple[int, int, int, int, float]] = []

    roi_w_scan = int(roi_w / scale_x)
    roi_h_scan = int(roi_h / scale_y)

    for idx in flat_indices:
        row, col = divmod(int(idx), density_map.shape[1])
        d = float(density_map[row, col])
        if d < min_density:
            break

        # 密度图坐标 → 扫描图坐标 → 全分辨率坐标
        scan_cx = int(col * stride + roi_w_scan // 2)
        scan_cy = int(row * stride + roi_h_scan // 2)
        full_cx = int(scan_cx * scale_x)
        full_cy = int(scan_cy * scale_y)

        too_close = False
        for sx, sy, sw, sh, _ in selected:
            if (abs(full_cx - (sx + sw // 2)) < nms_distance
                    and abs(full_cy - (sy + sh // 2)) < nms_distance):
                too_close = True
                break
        if too_close:
            continue

        rx = max(0, full_cx - roi_w // 2)
        ry = max(0, full_cy - roi_h // 2)
        selected.append((rx, ry, roi_w, roi_h, d))
        if len(selected) >= n_hotspots:
            break

    return selected


# ═══════════════════════════════════════════════════════════════
#  5. 全分辨率精确验证
# ═══════════════════════════════════════════════════════════════

def refine_hotspots_fullres(
    reader,
    candidates: list[tuple[int, int, int, int, float]],
    stain_type: str,
    threshold_method: str,
    threshold_value: float,
    min_area: int,
    n_hotspots: int,
    progress_callback: Callable[[int, int], None] | None = None,
) -> list[tuple[int, int, int, int, float]]:
    """在全分辨率上精确验证候选热点区域。"""
    refined: list[tuple[int, int, int, int, float]] = []
    total = len(candidates)

    for i, (x, y, w, h, _) in enumerate(candidates):
        if progress_callback:
            progress_callback(i, total)
        try:
            patch = reader.extract_region(x, y, w, h, level=0)
        except Exception as exc:
            logger.warning("读取候选区域 (%d,%d,%d,%d) 失败: %s", x, y, w, h, exc)
            continue
        deconv = color_deconvolution(patch, stain_type)
        pos_mask = threshold_positive(
            deconv["positive"], threshold_method, threshold_value, min_area,
        )
        density = float(pos_mask.sum() / pos_mask.size)
        refined.append((x, y, w, h, density))

    if progress_callback:
        progress_callback(total, total)
    refined.sort(key=lambda r: -r[4])
    return refined[:n_hotspots]


# ═══════════════════════════════════════════════════════════════
#  6. 可视化辅助
# ═══════════════════════════════════════════════════════════════

def make_overlay_image(
    image: np.ndarray,
    positive_mask: np.ndarray,
    color: tuple[int, int, int] = (180, 30, 30),
    alpha: float = 0.5,
) -> np.ndarray:
    """在图像上叠加阳性区域半透明色。"""
    overlay = image.copy()
    m3 = np.stack([positive_mask] * 3, axis=-1)
    blend = (overlay.astype(np.float32) * (1 - alpha)
             + np.array(color, dtype=np.float32) * alpha)
    overlay = np.where(m3, blend.clip(0, 255).astype(np.uint8), overlay)
    return overlay



# ═══════════════════════════════════════════════════════════════
#  8. 逐块处理（内存安全核心）
# ═══════════════════════════════════════════════════════════════

def _sample_otsu_threshold(
    reader,
    level: int,
    stain_type: str,
    tile_size: int = 2048,
    n_samples: int = 8,
) -> float:
    """从若干采样 tile 估计 Otsu 阈值（归一化后的 0-255 空间）。

    使用快速 LUT 反卷积 + 单通道提取，峰值内存 ≈ 单块 float32 (~50 MB)。
    返回所有有效采样 tile 的 Otsu 阈值中位数。
    """
    lv = reader.levels[level]
    lv_w, lv_h = lv.width, lv.height
    dab_w = _get_positive_weights(stain_type)

    tiles_x = max(1, math.ceil(lv_w / tile_size))
    tiles_y = max(1, math.ceil(lv_h / tile_size))

    # 均匀选取 n_samples 个 tile 位置
    all_positions = [(r, c) for r in range(tiles_y) for c in range(tiles_x)]
    step = max(1, len(all_positions) // n_samples)
    sample_positions = all_positions[::step][:n_samples]

    thresholds: list[float] = []
    for row, col in sample_positions:
        lx = col * tile_size
        ly = row * tile_size
        lw = min(tile_size, lv_w - lx)
        lh = min(tile_size, lv_h - ly)

        tile = reader._read_level_region(level, lx, ly, lw, lh)
        if tile is None or tile.size == 0:
            continue

        pos = _fast_positive_od(tile, dab_w)  # float32, 单通道

        ch_min, ch_max = pos.min(), pos.max()
        if ch_max - ch_min < 0.05:
            # 低对比度 tile（均匀背景）— Otsu 无意义，跳过
            continue
        ch_norm = ((pos - ch_min) / (ch_max - ch_min) * 255).astype(np.uint8)

        thr_val, _ = cv2.threshold(
            ch_norm, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU,
        )
        thresholds.append(float(thr_val))

    if not thresholds:
        return 80.0  # 安全默认值

    return float(np.median(thresholds))


def _get_tissue_tile_set(
    reader,
    level: int,
    tile_size: int,
    min_tissue_pct: float = 0.1,
) -> set[tuple[int, int]]:
    """利用缩略图检测组织区域，返回含组织的 (row, col) tile 索引集合。

    仅在含组织面积 > min_tissue_pct 的 tile 上做阳性检测，
    跳过纯背景 tile 以大幅加速扫描。

    Args:
        reader: SDPCReader 实例
        level: 扫描层级
        tile_size: tile 尺寸（层级像素）
        min_tissue_pct: 最低组织面积占比 (0~1)，低于此值的 tile 跳过

    Returns:
        {(row, col), ...} 含组织的 tile 索引集合
    """
    lv = reader.levels[level]
    lv_w, lv_h = lv.width, lv.height
    ds = lv.downsample

    # 获取缩略图及其降采样因子
    thumb = reader.thumbnail  # (H, W, 3) uint8
    thumb_h, thumb_w = thumb.shape[:2]
    full_w, full_h = lv_w * ds, lv_h * ds
    thumb_ds_x = full_w / thumb_w
    thumb_ds_y = full_h / thumb_h

    # 组织检测（启用亮度上限排除近白色背景像素）
    result = detect_tissue(thumb, max_brightness=230)
    tissue_mask = result["mask"]  # uint8, 0/255

    # 遍历所有 tile，映射到缩略图坐标，计算组织覆盖率
    tiles_x = max(1, math.ceil(lv_w / tile_size))
    tiles_y = max(1, math.ceil(lv_h / tile_size))

    tissue_set: set[tuple[int, int]] = set()
    for row in range(tiles_y):
        for col in range(tiles_x):
            # tile 在层级坐标 → level-0 坐标 → 缩略图坐标
            lx0 = col * tile_size * ds
            ly0 = row * tile_size * ds
            lw0 = min(tile_size, lv_w - col * tile_size) * ds
            lh0 = min(tile_size, lv_h - row * tile_size) * ds

            tx = int(lx0 / thumb_ds_x)
            ty = int(ly0 / thumb_ds_y)
            tw = max(1, int(lw0 / thumb_ds_x))
            th = max(1, int(lh0 / thumb_ds_y))

            # 钳制到缩略图边界
            tx = max(0, min(tx, thumb_w - 1))
            ty = max(0, min(ty, thumb_h - 1))
            tw = min(tw, thumb_w - tx)
            th = min(th, thumb_h - ty)

            if tw < 1 or th < 1:
                continue

            patch = tissue_mask[ty:ty + th, tx:tx + tw]
            tissue_ratio = patch.mean() / 255.0

            if tissue_ratio >= min_tissue_pct:
                tissue_set.add((row, col))

    return tissue_set


def _process_single_tile(
    reader,
    level: int,
    lx: int,
    ly: int,
    lw: int,
    lh: int,
    dab_w: np.ndarray,
    h_w: np.ndarray | None,
    threshold_method: str,
    estimated_otsu: float,
    fixed_thr: float,
    fold_ratio_threshold: float,
) -> tuple[np.ndarray | None, np.ndarray | None]:
    """处理单个 tile：读取 → 反卷积 → 阈值 → 折叠过滤。

    线程安全：仅使用只读共享数据（reader、weights、LUT），
    不访问任何可变共享状态。

    Returns:
        (binary_mask, None) if tile has valid signal,
        (None, None) if tile is empty or low-contrast.
    """
    tile = reader._read_level_region(level, lx, ly, lw, lh)
    if tile is None or tile.size == 0:
        return None, None

    # ── 过滤白色/空白区域（非组织像素） ──
    # 使用亮度 + 饱和度双重判定：亮且低饱和度 = 背景
    ch_max_px = tile.max(axis=2)
    ch_min_px = tile.min(axis=2)
    gray = (ch_max_px.astype(np.int16) + ch_min_px.astype(np.int16)) >> 1
    non_white = (gray < 220) | ((ch_max_px.astype(np.int16) - ch_min_px.astype(np.int16)) > 40)
    del gray, ch_max_px, ch_min_px
    non_white_count = int(non_white.sum())
    if non_white_count < non_white.size * 0.05:
        del tile, non_white
        return None, None

    # ── 快速单通道反卷积（LUT + 点积） ──
    lut = _get_od_lut()
    od = lut[tile]  # (H, W, 3) float32 — 仅查表
    pos = np.dot(od, dab_w)   # DAB 通道 (H, W) float32
    np.maximum(pos, 0.0, out=pos)

    # ── 折叠区域检测：计算 H 通道 OD ──
    h_od = None
    if h_w is not None:
        h_od = np.dot(od, h_w)  # H 通道 (H, W) float32
        np.maximum(h_od, 0.0, out=h_od)
    del od, lut

    # ── 归一化到 0-255（低对比度 tile 跳过） ──
    ch_min, ch_max = pos.min(), pos.max()
    if ch_max - ch_min < 0.05:
        del pos, h_od
        return None, None

    ch_norm = (
        (pos - ch_min) / (ch_max - ch_min) * 255
    ).astype(np.uint8)

    # ── 阈值分割 ──
    thr = int(estimated_otsu) if threshold_method == "otsu" else int(fixed_thr)
    _, binary = cv2.threshold(ch_norm, thr, 255, cv2.THRESH_BINARY)
    del ch_norm

    # ── 去除白色/空白区域的阳性像素 ──
    binary[~non_white] = 0
    del non_white

    # ── 折叠区域过滤：DAB/(DAB+H) 比值 ──
    if h_od is not None and fold_ratio_threshold > 0:
        dab_frac = pos / (pos + h_od + 1e-10)
        fold_mask = dab_frac < fold_ratio_threshold
        binary[fold_mask] = 0
        del fold_mask, dab_frac, h_od
    del pos

    # ── 预览 tile ──
    preview_tile = tile  # 返回原始 tile 用于预览合成

    return binary, preview_tile


def _accumulate_tiled(
    reader,
    level: int,
    stain_type: str,
    threshold_method: str,
    manual_threshold: float,
    estimated_otsu: float,
    min_area: int,
    tile_size: int,
    analysis_ds: int,
    preview_ds: int,
    fold_ratio_threshold: float = 0.0,
    n_workers: int = 1,
    tissue_tile_set: set[tuple[int, int]] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> tuple[np.ndarray, np.ndarray, float, int, int]:
    """逐块读取 → 快速 LUT 反卷积 → 阈值 → 累加降采样 mask + 预览图。

    优化点（参考 QuPath）：
      - OD 查表替代逐像素 log 运算
      - 只计算阳性单通道 float32（~50 MB/tile vs ~300 MB）
      - 去掉逐块形态学和连通域（改在全局降采样 mask 上统一做）
      - 支持多块并行处理（n_workers > 1）
      - 组织预检测跳过纯背景 tile

    折叠过滤：
      - 计算 DAB/(DAB+H) 比值，折叠区域该比值低（H高DAB低）
      - 对阈值后的阳性 mask 中 fold_ratio < fold_ratio_threshold 的像素置零

    Args:
        fold_ratio_threshold: 折叠阈值 (0~1)，>0 时启用折叠过滤，
                              默认 0.0 表示不过滤。推荐 0.3~0.5
        n_workers: 并行处理线程数，1=串行，>1=并行
        cancel_callback: 取消检查回调，返回 True 表示应取消

    Returns:
        (accumulated_mask, preview_image, fixed_threshold, tiles_done, total_tiles)
    """
    lv = reader.levels[level]
    lv_w, lv_h = lv.width, lv.height
    dab_w = _get_positive_weights(stain_type)
    h_w = _get_h_weights(stain_type) if fold_ratio_threshold > 0 else None

    mask_h = math.ceil(lv_h / analysis_ds)
    mask_w = math.ceil(lv_w / analysis_ds)

    prev_h = math.ceil(lv_h / preview_ds)
    prev_w = math.ceil(lv_w / preview_ds)

    accumulated_mask = np.zeros((mask_h, mask_w), dtype=np.uint8)
    preview_canvas = np.zeros((prev_h, prev_w, 3), dtype=np.uint8)

    tiles_x = max(1, math.ceil(lv_w / tile_size))
    tiles_y = max(1, math.ceil(lv_h / tile_size))
    done = 0

    fixed_thr = 0.0
    if threshold_method == "manual":
        fixed_thr = manual_threshold * 255.0

    # 生成 tile 坐标（如有组织预检测，仅保留含组织的 tile）
    tile_coords = []
    for row in range(tiles_y):
        for col in range(tiles_x):
            if tissue_tile_set is not None and (row, col) not in tissue_tile_set:
                continue
            lx = col * tile_size
            ly = row * tile_size
            lw = min(tile_size, lv_w - lx)
            lh = min(tile_size, lv_h - ly)
            tile_coords.append((lx, ly, lw, lh))

    total_tiles = len(tile_coords)

    # 合并锁 — 保护 accumulated_mask 和 preview_canvas 的写入
    merge_lock = threading.Lock()

    def _merge_tile(lx, ly, lw, lh, binary, tile):
        """将单块结果合并到全局 mask 和预览画布（线程安全）。"""
        if binary is not None:
            m_y0 = ly // analysis_ds
            m_x0 = lx // analysis_ds
            m_y1 = min(mask_h, (ly + lh) // analysis_ds + 1)
            m_x1 = min(mask_w, (lx + lw) // analysis_ds + 1)
            resized = cv2.resize(
                binary,
                (m_x1 - m_x0, m_y1 - m_y0),
                interpolation=cv2.INTER_NEAREST,
            )
            with merge_lock:
                accumulated_mask[m_y0:m_y1, m_x0:m_x1] = np.maximum(
                    accumulated_mask[m_y0:m_y1, m_x0:m_x1], resized,
                )
            del resized

        if tile is not None:
            p_y0 = ly // preview_ds
            p_x0 = lx // preview_ds
            p_y1 = min(prev_h, (ly + lh) // preview_ds + 1)
            p_x1 = min(prev_w, (lx + lw) // preview_ds + 1)
            if p_y1 > p_y0 and p_x1 > p_x0:
                preview_tile = cv2.resize(
                    tile, (p_x1 - p_x0, p_y1 - p_y0),
                    interpolation=cv2.INTER_AREA,
                )
                with merge_lock:
                    preview_canvas[p_y0:p_y1, p_x0:p_x1] = preview_tile
                del preview_tile

    # ── 串行或并行处理 ──
    if n_workers <= 1:
        # 串行模式
        for lx, ly, lw, lh in tile_coords:
            if cancel_callback and cancel_callback():
                break
            binary, tile = _process_single_tile(
                reader, level, lx, ly, lw, lh,
                dab_w, h_w, threshold_method,
                estimated_otsu, fixed_thr, fold_ratio_threshold,
            )
            _merge_tile(lx, ly, lw, lh, binary, tile)
            del binary, tile
            done += 1
            if progress_callback:
                progress_callback(done, total_tiles)
    else:
        # 并行模式 — 多线程
        done_counter = threading.Lock()

        def _worker(lx, ly, lw, lh):
            binary, tile = _process_single_tile(
                reader, level, lx, ly, lw, lh,
                dab_w, h_w, threshold_method,
                estimated_otsu, fixed_thr, fold_ratio_threshold,
            )
            _merge_tile(lx, ly, lw, lh, binary, tile)
            del binary, tile
            with done_counter:
                nonlocal done
                done += 1
                current_done = done
            if progress_callback:
                progress_callback(current_done, total_tiles)

        with ThreadPoolExecutor(max_workers=n_workers) as executor:
            futures = {}
            for lx, ly, lw, lh in tile_coords:
                if cancel_callback and cancel_callback():
                    break
                future = executor.submit(_worker, lx, ly, lw, lh)
                futures[future] = True

            # 等待所有已提交的 future 完成
            for future in as_completed(futures):
                if cancel_callback and cancel_callback():
                    # 取消尚未开始的 future
                    for f in futures:
                        f.cancel()
                    break
                try:
                    future.result()
                except Exception:
                    logger.exception("tile 处理失败")

    return accumulated_mask, preview_canvas, fixed_thr, done, total_tiles


def detect_ihc_hotspots_tiled(
    reader,
    level: int,
    stain_type: str = "H-DAB",
    threshold_method: str = "otsu",
    manual_threshold: float = 0.3,
    min_area: int = 50,
    window_size: int = 200,
    n_hotspots: int = 5,
    min_density: float = 0.05,
    roi_w: int = 1024,
    roi_h: int = 1024,
    tile_size: int = 2048,
    analysis_ds: int = 8,
    max_preview_dim: int = 2048,
    fold_ratio_threshold: float = 0.0,
    n_workers: int = 1,
    cancel_callback: Callable[[], bool] | None = None,
    stage_callback: Callable[[str], None] | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> dict:
    """逐块 IHC 热点检测主入口（内存安全）。

    流程：
      1. 缩略图组织检测 → 确定含组织的 tile 集合（可选）
      2. 从组织 tile 中采样估计 Otsu 阈值
      3. 仅处理含组织的 tile → 反卷积 → 阈值 → 折叠过滤 → 累加 mask
      4. 全局形态学 + 密度图 + 热点提取

    峰值内存 ≈ 单块 float64 (~100 MB) + mask (~17 MB) + preview (~5 MB)

    Args:
        reader: SDPCReader 实例
        level: 金字塔层级索引
        stain_type: 染色类型
        threshold_method: "otsu" 或 "manual"
        manual_threshold: 手动阈值
        min_area: 最小阳性面积
        window_size: 滑动窗口尺寸（层级像素，自动缩放到 mask 空间）
        n_hotspots: 最大热点数
        min_density: 最低密度阈值
        roi_w, roi_h: ROI 框尺寸（全分辨率像素）
        tile_size: 每次读取 tile 尺寸
        analysis_ds: mask 降采样因子
        max_preview_dim: 预览图最大维度（像素）
        fold_ratio_threshold: 折叠过滤阈值 (0~1)，>0 时启用折叠过滤，
                              DAB/(DAB+H) < 该阈值的阳性像素会被过滤
        n_workers: 并行处理线程数，1=串行，>1=并行（推荐 2~4）
        cancel_callback: 取消检查回调，返回 True 表示应取消
        stage_callback: 阶段回调
        progress_callback: 进度回调

    Returns:
        {
            "hotspots":      [(x, y, w, h, density), ...],  # level-0 坐标
            "positive_mask": np.ndarray (uint8),            # 降采样 mask
            "density_map":   np.ndarray (float32),
            "positive_pct":  float,
            "preview_image": np.ndarray (uint8, RGB),       # 预览用
            "preview_ds":    float,                         # 预览降采样
            "scan_ds":       float,                         # 层级降采样
            "analysis_ds":   int,                           # mask 降采样
            "estimated_otsu": float,                        # 估计的 Otsu 值
            "lv_w":          int,                           # 层级宽度
            "lv_h":          int,                           # 层级高度
        }
    """
    lv = reader.levels[level]
    lv_w, lv_h = lv.width, lv.height
    ds = lv.downsample
    scale_x = ds  # 层级坐标 × ds = level-0 坐标
    scale_y = ds

    # 预览降采样：独立计算，保证预览图最大维度不超过 max_preview_dim
    preview_ds = max(1, math.ceil(max(lv_w, lv_h) / max_preview_dim))
    # 确保 preview_ds 不大于 analysis_ds（预览分辨率 ≥ mask 分辨率）
    preview_ds = min(preview_ds, analysis_ds)

    # ── 阶段 0：组织预检测 — 跳过纯背景 tile ──
    tissue_tile_set: set[tuple[int, int]] | None = None
    tiles_x = max(1, math.ceil(lv_w / tile_size))
    tiles_y = max(1, math.ceil(lv_h / tile_size))
    all_tiles = tiles_x * tiles_y

    if stage_callback:
        stage_callback("正在检测组织区域...")
    try:
        tissue_tile_set = _get_tissue_tile_set(
            reader, level, tile_size, min_tissue_pct=0.2,
        )
        logger.info(
            "组织预检测: %d/%d 个 tile 含组织 (%.1f%%)",
            len(tissue_tile_set), all_tiles,
            len(tissue_tile_set) / all_tiles * 100 if all_tiles else 0,
        )
    except Exception:
        logger.warning("组织预检测失败，将处理全部 tile", exc_info=True)
        tissue_tile_set = None

    # ── 阶段 1：估计 Otsu 阈值 ──
    estimated_otsu = 80.0
    if threshold_method == "otsu":
        if stage_callback:
            stage_callback("正在采样估计阈值...")
        estimated_otsu = _sample_otsu_threshold(
            reader, level, stain_type, tile_size, n_samples=8,
        )
        logger.info("估计 Otsu 阈值: %.1f", estimated_otsu)

    # ── 阶段 2：逐块处理 ──
    if stage_callback:
        stage_callback("正在逐块扫描...")

    (accumulated_mask, preview_image, fixed_thr,
     tiles_done, total_tiles) = _accumulate_tiled(
        reader=reader,
        level=level,
        stain_type=stain_type,
        threshold_method=threshold_method,
        manual_threshold=manual_threshold,
        estimated_otsu=estimated_otsu,
        min_area=min_area,
        tile_size=tile_size,
        analysis_ds=analysis_ds,
        preview_ds=preview_ds,
        fold_ratio_threshold=fold_ratio_threshold,
        n_workers=n_workers,
        tissue_tile_set=tissue_tile_set,
        cancel_callback=cancel_callback,
        progress_callback=progress_callback,
    )

    # ── 阶段 3：全局形态学 + 密度图 + 热点提取 ──
    if stage_callback:
        stage_callback("正在提取热点...")

    # 全局形态学去噪（替代逐块形态学，只需处理降采样后的小 mask）
    mask_binary = accumulated_mask > 0
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    mask_binary_uint8 = mask_binary.astype(np.uint8) * 255
    mask_binary_uint8 = cv2.morphologyEx(mask_binary_uint8, cv2.MORPH_OPEN, kernel)

    # 全局小碎片过滤（min_area 缩放到降采样空间）
    ds_min_area = max(1, min_area // (analysis_ds * analysis_ds))
    if ds_min_area > 1:
        num_labels, labels, stats, _ = (
            cv2.connectedComponentsWithStats(mask_binary_uint8)
        )
        for i in range(1, num_labels):
            if stats[i, cv2.CC_STAT_AREA] < ds_min_area:
                mask_binary_uint8[labels == i] = 0

    accumulated_mask = mask_binary_uint8  # uint8 (0/255)
    del mask_binary, mask_binary_uint8, kernel

    # 窗口大小从层级空间缩放到 mask 空间
    mask_window = max(2, window_size // analysis_ds)
    mask_stride = max(1, mask_window // 2)

    # mask 坐标 → 层级坐标 → level-0 坐标
    mask_to_level = float(analysis_ds)
    eff_scale_x = mask_to_level * scale_x
    eff_scale_y = mask_to_level * scale_y

    density_map = compute_density_map(
        accumulated_mask > 0, mask_window, mask_stride,
    )

    hotspots = find_hotspots(
        density_map, mask_stride, roi_w, roi_h,
        eff_scale_x, eff_scale_y,
        n_hotspots, min_density,
    )

    pos_pct = float(
        (accumulated_mask > 0).sum() / accumulated_mask.size * 100
    )

    return {
        "hotspots":       hotspots,
        "positive_mask":  accumulated_mask,
        "density_map":    density_map,
        "positive_pct":   pos_pct,
        "preview_image":  preview_image,
        "preview_ds":     float(preview_ds),
        "scan_ds":        ds,
        "analysis_ds":    analysis_ds,
        "estimated_otsu": estimated_otsu,
        "lv_w":           lv_w,
        "lv_h":           lv_h,
        "tissue_tiles":   len(tissue_tile_set) if tissue_tile_set is not None else all_tiles,
        "total_tiles":    all_tiles,
    }


# ═══════════════════════════════════════════════════════════════
#  8. UI
# ═══════════════════════════════════════════════════════════════

from PySide6.QtCore import Qt, QRectF, QTimer, QThread, QObject, Signal
from PySide6.QtGui import (
    QImage, QPixmap, QPainter, QPen, QColor, QBrush, QFont,
    QCursor,
)
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout,
    QGraphicsItem, QGraphicsPixmapItem, QGraphicsRectItem, QGraphicsScene,
    QGraphicsSimpleTextItem, QGraphicsView, QHBoxLayout, QLabel,
    QPushButton, QSizePolicy, QSlider, QSpinBox, QSplitter, QVBoxLayout,
    QWidget, QProgressBar, QMessageBox, QMenu,
)

from liver_portal_crop.reader import SDPCReader
from liver_portal_crop.constants import FIELD_NUMBER_MM


# ═══════════════════════════════════════════════════════════════
#  交互式 ROI 矩形（预览图上可拖拽 / 缩放 / 删除）
# ═══════════════════════════════════════════════════════════════

class _PreviewROIItem(QGraphicsRectItem):
    """预览图上的交互式 ROI 矩形。

    支持：拖拽移动、角点缩放、右键删除、选中高亮。
    """

    _HANDLE_RATIO = 0.02  # 手柄占矩形短边的比例

    def __init__(self, rect: QRectF, rank: int = 0, density: float = 0.0,
                 parent=None):
        super().__init__(rect, parent)
        self._rank = rank
        self._density = density
        self.setAcceptHoverEvents(True)
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self._pen_normal = QPen(QColor(255, 215, 0), 2)
        self._pen_normal.setStyle(Qt.PenStyle.DashLine)
        self._pen_selected = QPen(QColor(10, 132, 255), 2.5)
        self.setPen(self._pen_normal)
        self.setBrush(QBrush(QColor(255, 215, 0, 25)))
        self._label_item: QGraphicsSimpleTextItem | None = None
        self._handle_items: list[QGraphicsRectItem] = []
        self._active_handle: int = -1  # -1 = none
        self._drag_origin = None
        self._drag_rect_origin = None
        self._create_label()
        self._create_handles()

    def _create_label(self):
        text = f"#{self._rank}"
        if self._density > 0:
            text += f" {self._density:.0%}"
        self._label_item = QGraphicsSimpleTextItem(text, self)
        font = QFont()
        font.setPixelSize(max(8, int(self.rect().height() * 0.08)))
        font.setBold(True)
        self._label_item.setFont(font)
        self._label_item.setBrush(QBrush(QColor(255, 215, 0)))
        self._label_item.setPos(
            self.rect().left() + 2, self.rect().top() + 2,
        )

    def _create_handles(self):
        """在 4 个角创建缩放手柄。"""
        for child in self._handle_items:
            child.setParentItem(None)
        self._handle_items.clear()
        r = self.rect()
        hs = max(4, min(r.width(), r.height()) * self._HANDLE_RATIO)
        corners = [
            (r.left(), r.top()),        # 0: top-left
            (r.right(), r.top()),       # 1: top-right
            (r.left(), r.bottom()),     # 2: bottom-left
            (r.right(), r.bottom()),    # 3: bottom-right
        ]
        for cx, cy in corners:
            h = QGraphicsRectItem(cx - hs / 2, cy - hs / 2, hs, hs, self)
            h.setPen(QPen(QColor(255, 215, 0), 1))
            h.setBrush(QBrush(QColor(255, 215, 0, 120)))
            h.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
            h.setVisible(False)
            self._handle_items.append(h)

    def _update_handle_positions(self):
        r = self.rect()
        hs = max(4, min(r.width(), r.height()) * self._HANDLE_RATIO)
        corners = [
            (r.left(), r.top()),
            (r.right(), r.top()),
            (r.left(), r.bottom()),
            (r.right(), r.bottom()),
        ]
        for h, (cx, cy) in zip(self._handle_items, corners):
            h.setRect(cx - hs / 2, cy - hs / 2, hs, hs)

    def _hit_handle(self, pos) -> int:
        """检查鼠标是否命中某个手柄，返回手柄索引 (-1 = none)。"""
        for i, h in enumerate(self._handle_items):
            if h.rect().translated(h.pos()).contains(pos):
                return i
        return -1

    # ── 事件处理 ──

    def hoverEnterEvent(self, event):
        for h in self._handle_items:
            h.setVisible(True)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        if not self.isSelected():
            for h in self._handle_items:
                h.setVisible(False)
        super().hoverLeaveEvent(event)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self.setPen(self._pen_selected if value else self._pen_normal)
            for h in self._handle_items:
                h.setVisible(bool(value))
        return super().itemChange(change, value)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            # 右键菜单：删除
            menu = QMenu()
            menu.addAction("删除此 ROI")
            action = menu.exec(QCursor.pos())
            if action is not None:
                scene = self.scene()
                if scene:
                    scene.removeItem(self)
            return
        handle_idx = self._hit_handle(event.pos())
        if handle_idx >= 0 and self.isSelected():
            self._active_handle = handle_idx
            self._drag_origin = event.pos()
            self._drag_rect_origin = QRectF(self.rect())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._active_handle >= 0 and self._drag_origin is not None:
            delta = event.pos() - self._drag_origin
            r = QRectF(self._drag_rect_origin)
            h = self._active_handle
            if h == 0:  # top-left
                r.setTopLeft(r.topLeft() + delta)
            elif h == 1:  # top-right
                r.setTopRight(r.topRight() + delta)
            elif h == 2:  # bottom-left
                r.setBottomLeft(r.bottomLeft() + delta)
            elif h == 3:  # bottom-right
                r.setBottomRight(r.bottomRight() + delta)
            # 保证最小尺寸
            if r.width() > 2 and r.height() > 2:
                self.setRect(r.normalized())
                self._update_handle_positions()
                if self._label_item:
                    self._label_item.setPos(r.left() + 2, r.top() + 2)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._active_handle = -1
        self._drag_origin = None
        self._drag_rect_origin = None
        super().mouseReleaseEvent(event)


# ═══════════════════════════════════════════════════════════════
#  预览查看器
# ═══════════════════════════════════════════════════════════════

class _IHCPreviewView(QGraphicsView):
    """IHC 热点预览查看器 — 滚轮缩放 + 拖拽平移 + 交互式 ROI 编辑。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse,
        )
        self.setStyleSheet(
            "QGraphicsView { background: #1c1c1e; border: none; }"
        )
        self._base_item: QGraphicsPixmapItem | None = None
        self._overlay_item: QGraphicsPixmapItem | None = None
        self._marker_items: list = []
        self._roi_items: list[_PreviewROIItem] = []
        self._zoom_factor: float = 1.0
        # 框选模式：鼠标拖拽创建新 ROI
        self._draw_mode: bool = False
        self._draw_start: QPointF | None = None
        self._draw_rect_item: QGraphicsRectItem | None = None
        # 中键 / 右键拖拽平移
        self._pan_origin: QPointF | None = None

    def set_image(self, qimage: QImage):
        self._scene.clear()
        self._marker_items.clear()
        self._roi_items.clear()
        self._overlay_item = None
        pix = QPixmap.fromImage(qimage)
        self._base_item = self._scene.addPixmap(pix)
        self._scene.setSceneRect(pix.rect())
        self.resetTransform()
        self.fitInView(
            self._scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio,
        )
        self._zoom_factor = self.transform().m11()

    def update_overlay(
        self,
        overlay_qimage: QImage,
        hotspots_preview: list[tuple[int, int, int, int, float]],
    ):
        """更新叠加层 + 静态热点标记 + 可编辑 ROI 矩形。"""
        # 清理旧叠加层、标记和 ROI 项
        if self._overlay_item is not None:
            self._scene.removeItem(self._overlay_item)
            self._overlay_item = None
        for item in self._marker_items:
            self._scene.removeItem(item)
        self._marker_items.clear()
        for item in self._roi_items:
            self._scene.removeItem(item)
        self._roi_items.clear()

        self._overlay_item = self._scene.addPixmap(
            QPixmap.fromImage(overlay_qimage),
        )
        self._overlay_item.setOpacity(0.45)

        if not hotspots_preview:
            return

        scene_rect = self._scene.sceneRect()
        img_w = scene_rect.width()
        img_h = scene_rect.height()
        cross_size = max(3.0, min(img_w, img_h) * 0.02)
        pen_width = max(1.0, min(img_w, img_h) * 0.004)
        font_size = max(6.0, min(img_w, img_h) * 0.025)

        sorted_hs = sorted(hotspots_preview, key=lambda h: -h[4])
        for rank, (x, y, w, h, density) in enumerate(sorted_hs, 1):
            cx = x + w / 2
            cy = y + h / 2

            # ── 静态标记（十字 + 标签 + 虚线框）── 始终可见
            pen = QPen(QColor(255, 215, 0))
            pen.setWidthF(pen_width)
            self._marker_items.append(
                self._scene.addLine(cx - cross_size, cy, cx + cross_size, cy, pen))
            self._marker_items.append(
                self._scene.addLine(cx, cy - cross_size, cx, cy + cross_size, pen))

            text = self._scene.addSimpleText(f"#{rank}  {density:.1%}")
            font = QFont()
            font.setPixelSize(int(font_size))
            font.setBold(True)
            text.setFont(font)
            text.setBrush(QBrush(QColor(255, 215, 0)))
            text.setPos(x + pen_width * 2, y + pen_width * 2)
            self._marker_items.append(text)

            # ── 交互式 ROI 矩形（可拖拽编辑）──
            roi_rect = QRectF(float(x), float(y), float(w), float(h))
            roi_item = _PreviewROIItem(roi_rect, rank=rank, density=density)
            self._scene.addItem(roi_item)
            self._roi_items.append(roi_item)

    def get_roi_rects(self) -> list[QRectF]:
        """获取所有交互式 ROI 的当前矩形（场景坐标 = 预览图坐标）。"""
        rects = []
        for item in self._roi_items:
            rects.append(item.mapRectToScene(item.rect()))
        return rects

    def set_draw_mode(self, enabled: bool):
        """切换框选绘制模式。

        启用时: 鼠标拖拽创建新 ROI（RubberBandDrag + 十字光标）。
        禁用时: 可拖拽平移 + 点击选中已有 ROI 进行编辑。
        """
        self._draw_mode = enabled
        # 确保所有 ROI 项始终可见
        for item in self._roi_items:
            item.setVisible(True)
        if enabled:
            self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
            self.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.unsetCursor()

    def _add_drawn_roi(self, rect: QRectF):
        """从框选创建新 ROI 项。"""
        rank = len(self._roi_items) + 1
        roi_item = _PreviewROIItem(rect, rank=rank, density=0.0)
        self._scene.addItem(roi_item)
        self._roi_items.append(roi_item)

    def mousePressEvent(self, event):
        # 中键拖拽平移视图
        if event.button() == Qt.MouseButton.MiddleButton:
            self._pan_origin = event.pos()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        if self._draw_mode and event.button() == Qt.MouseButton.LeftButton:
            self._draw_start = self.mapToScene(event.pos())
            pen = QPen(QColor(10, 132, 255), 1.5)
            pen.setStyle(Qt.PenStyle.DashLine)
            self._draw_rect_item = QGraphicsRectItem(
                QRectF(self._draw_start, self._draw_start),
            )
            self._draw_rect_item.setPen(pen)
            self._draw_rect_item.setBrush(QBrush(QColor(10, 132, 255, 30)))
            self._scene.addItem(self._draw_rect_item)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        # 中键拖拽平移视图
        if self._pan_origin is not None:
            delta = event.pos() - self._pan_origin
            self._pan_origin = event.pos()
            h_bar = self.horizontalScrollBar()
            v_bar = self.verticalScrollBar()
            h_bar.setValue(h_bar.value() - delta.x())
            v_bar.setValue(v_bar.value() - delta.y())
            event.accept()
            return
        if self._draw_mode and self._draw_start is not None and self._draw_rect_item:
            current = self.mapToScene(event.pos())
            rect = QRectF(self._draw_start, current).normalized()
            self._draw_rect_item.setRect(rect)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        # 中键释放结束平移
        if event.button() == Qt.MouseButton.MiddleButton:
            self._pan_origin = None
            self.unsetCursor()
            event.accept()
            return
        if self._draw_mode and self._draw_start is not None and self._draw_rect_item:
            end = self.mapToScene(event.pos())
            rect = QRectF(self._draw_start, end).normalized()
            # 移除临时矩形
            self._scene.removeItem(self._draw_rect_item)
            self._draw_rect_item = None
            self._draw_start = None
            # 只添加有足够面积的 ROI
            if rect.width() > 3 and rect.height() > 3:
                self._add_drawn_roi(rect)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self._zoom_factor *= factor
        self.scale(factor, factor)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_0, Qt.Key.Key_F):
            self.resetTransform()
            self.fitInView(
                self._scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio,
            )
            self._zoom_factor = self.transform().m11()
        elif event.key() == Qt.Key.Key_Delete:
            for item in list(self._roi_items):
                if item.isSelected():
                    self._scene.removeItem(item)
                    self._roi_items.remove(item)
        else:
            super().keyPressEvent(event)


# ═══════════════════════════════════════════════════════════════
#  扫描 + 检测 Worker（QThread 后台）
# ═══════════════════════════════════════════════════════════════

class _ScanWorker(QObject):
    """后台线程：逐块读取 → 颜色反卷积 → 累加 mask → 热点检测。

    峰值内存 ≈ 单块 float64 (~100 MB) + 全局 mask + 预览图

    Signals:
        stage:       阶段文字
        progress:    (current, total) 进度
        finished:    (preview_image, result_dict) 完成
        error:       错误信息
    """

    stage = Signal(str)
    progress = Signal(int, int)
    finished = Signal(object, dict)   # (preview_image_rgb, result_dict)
    error = Signal(str)

    def __init__(
        self,
        reader: SDPCReader,
        scan_level: int,
        stain_type: str,
        threshold_method: str,
        manual_threshold: float,
        min_area: int,
        window_size: int,
        n_hotspots: int,
        min_density: float,
        roi_w: int,
        roi_h: int,
        fold_ratio_threshold: float = 0.0,
        n_workers: int = 2,
        full_res_refine: bool = False,
        tile_read_size: int = 2048,
        analysis_ds: int = 8,
        max_preview_dim: int = 2048,
        parent=None,
    ):
        super().__init__(parent)
        self._reader = reader
        self._scan_level = scan_level
        self._stain_type = stain_type
        self._threshold_method = threshold_method
        self._manual_threshold = manual_threshold
        self._min_area = min_area
        self._window_size = window_size
        self._n_hotspots = n_hotspots
        self._min_density = min_density
        self._roi_w = roi_w
        self._roi_h = roi_h
        self._fold_ratio_threshold = fold_ratio_threshold
        self._n_workers = n_workers
        self._full_res_refine = full_res_refine
        self._tile_read_size = tile_read_size
        self._analysis_ds = analysis_ds
        self._max_preview_dim = max_preview_dim
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def _is_cancelled(self) -> bool:
        return self._cancel

    def run(self):
        try:
            # ── 逐块扫描 + 检测（内存安全） ──
            result = detect_ihc_hotspots_tiled(
                reader=self._reader,
                level=self._scan_level,
                stain_type=self._stain_type,
                threshold_method=self._threshold_method,
                manual_threshold=self._manual_threshold,
                min_area=self._min_area,
                window_size=self._window_size,
                n_hotspots=self._n_hotspots,
                min_density=self._min_density,
                roi_w=self._roi_w,
                roi_h=self._roi_h,
                tile_size=self._tile_read_size,
                analysis_ds=self._analysis_ds,
                max_preview_dim=self._max_preview_dim,
                fold_ratio_threshold=self._fold_ratio_threshold,
                n_workers=self._n_workers,
                cancel_callback=self._is_cancelled,
                stage_callback=self._emit_stage,
                progress_callback=self._emit_progress,
            )

            if self._cancel:
                return

            # ── 可选：全分辨率精确验证 ──
            if self._full_res_refine and result["hotspots"]:
                self._emit_stage("正在全分辨率精确验证...")
                result["hotspots"] = refine_hotspots_fullres(
                    reader=self._reader,
                    candidates=result["hotspots"],
                    stain_type=self._stain_type,
                    threshold_method=self._threshold_method,
                    threshold_value=self._manual_threshold,
                    min_area=self._min_area,
                    n_hotspots=self._n_hotspots,
                    progress_callback=self._emit_progress,
                )

            if not self._cancel:
                preview = result["preview_image"]
                self.finished.emit(preview, result)

        except Exception as exc:
            if not self._cancel:
                self.error.emit(str(exc))

    def _emit_stage(self, text: str):
        self.stage.emit(text)

    def _emit_progress(self, current: int, total: int):
        self.progress.emit(current, total)


# ═══════════════════════════════════════════════════════════════
#  对话框
# ═══════════════════════════════════════════════════════════════

class IHCHotspotDialog(QDialog):
    """IHC 阳性热点检测对话框。

    流程：
      1. 用户配置参数
      2. 点击「开始扫描」→ 后台逐块读取 + 检测（内存安全）
      3. 预览区显示结果（可缩放查看）
      4. 确认后点击「生成 ROI」
    """

    def __init__(
        self,
        reader: SDPCReader,
        tile_w: int,
        tile_h: int,
        parent=None,
        readers: dict | None = None,
        current_slide=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("IHC 热点检测")
        self.setMinimumSize(800, 600)
        self.resize(960, 680)

        self._reader = reader
        self._tile_w = tile_w
        self._tile_h = tile_h
        self._mpp = reader.mpp or 0.0
        self._readers = readers or {}
        self._current_slide = current_slide

        # 检测结果缓存
        self._preview_image: np.ndarray | None = None
        self._last_result: dict | None = None
        self._preview_ds: float = 1.0  # 预览图降采样

        # Worker
        self._worker: _ScanWorker | None = None
        self._thread: QThread | None = None

        self._setup_ui()
        self._recalc_frame()
        self._populate_level_cb()

    # ────────────────────────────────────────────────────────
    #  UI 构建
    # ────────────────────────────────────────────────────────

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(8)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(splitter, 1)

        # ── 左侧：可缩放预览 ──
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)

        self._preview_view = _IHCPreviewView()
        self._preview_view.setMinimumWidth(300)
        left_layout.addWidget(self._preview_view, 1)

        self._info_lbl = QLabel("点击下方「开始扫描」执行检测")
        self._info_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._info_lbl.setStyleSheet("color: #86868b; font-size: 11px;")
        left_layout.addWidget(self._info_lbl)

        self._stage_lbl = QLabel()
        self._stage_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._stage_lbl.setStyleSheet("color: #4c9aff; font-size: 11px;")
        left_layout.addWidget(self._stage_lbl)

        self._scan_progress = QProgressBar()
        self._scan_progress.setVisible(False)
        self._scan_progress.setFixedHeight(6)
        self._scan_progress.setTextVisible(False)
        left_layout.addWidget(self._scan_progress)

        hint_lbl = QLabel(
            "滚轮缩放 · 中键平移 · 拖拽移动 ROI · 角点缩放 · Del/右键删除 · 按 0/F 重置"
        )
        hint_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint_lbl.setStyleSheet("color: #636366; font-size: 10px;")
        left_layout.addWidget(hint_lbl)

        splitter.addWidget(left_widget)

        # ── 右侧：参数面板 ──
        right_widget = QWidget()
        right_widget.setMinimumWidth(260)
        right_widget.setMaximumWidth(340)
        right_scroll = QVBoxLayout(right_widget)
        right_scroll.setContentsMargins(8, 0, 0, 0)
        right_scroll.setSpacing(0)

        form = QFormLayout()
        form.setSpacing(6)
        form.setContentsMargins(0, 0, 0, 0)

        # ─ 染色设置 ─
        form.addRow(self._section_label("染色设置"))

        self._stain_cb = QComboBox()
        self._stain_cb.addItems(STAIN_LABELS)
        self._stain_cb.setCurrentText("H-DAB")
        form.addRow("染色类型:", self._stain_cb)

        # ─ 扫描层级 ─
        form.addRow(self._section_label("扫描层级"))

        self._level_cb = QComboBox()
        # 在 _populate_level_cb() 中填充
        form.addRow("金字塔层级:", self._level_cb)

        self._level_info_lbl = QLabel()
        self._level_info_lbl.setStyleSheet("color: #86868b; font-size: 10px;")
        self._level_info_lbl.setWordWrap(True)
        form.addRow("", self._level_info_lbl)

        # ─ ROI 框设置 ─
        form.addRow(self._section_label("ROI 框设置"))

        self._mag_cb = QComboBox()
        self._mag_cb.addItems(["4x", "10x", "20x", "40x", "80x"])
        self._mag_cb.setCurrentText("20x")
        self._mag_cb.currentTextChanged.connect(self._recalc_frame)
        form.addRow("倍率:", self._mag_cb)

        self._ratio_cb = QComboBox()
        self._ratio_cb.addItems(["Free", "1:1", "4:3", "3:2", "16:9"])
        self._ratio_cb.setCurrentText("16:9")
        self._ratio_cb.currentTextChanged.connect(self._recalc_frame)
        form.addRow("比例:", self._ratio_cb)

        self._frame_lbl = QLabel()
        self._frame_lbl.setStyleSheet("color: #aeaeb2; font-size: 11px;")
        form.addRow("框尺寸:", self._frame_lbl)

        # ─ 检测参数 ─
        form.addRow(self._section_label("检测参数"))

        self._n_spin = QSpinBox()
        self._n_spin.setRange(1, 100)
        self._n_spin.setValue(5)
        form.addRow("最大热点数:", self._n_spin)

        self._window_spin = QSpinBox()
        self._window_spin.setRange(10, 5000)
        self._window_spin.setValue(500)
        self._window_spin.setSingleStep(50)
        self._window_spin.setSuffix(" px")
        form.addRow("窗口大小:", self._window_spin)

        self._thr_method_cb = QComboBox()
        self._thr_method_cb.addItems(["Otsu 自适应", "手动阈值"])
        self._thr_method_cb.currentTextChanged.connect(self._on_threshold_method)
        form.addRow("阈值方法:", self._thr_method_cb)

        self._manual_thr_spin = QDoubleSpinBox()
        self._manual_thr_spin.setRange(0.0, 1.0)
        self._manual_thr_spin.setValue(0.3)
        self._manual_thr_spin.setSingleStep(0.05)
        self._manual_thr_spin.setDecimals(2)
        self._manual_thr_spin.setEnabled(False)
        form.addRow("手动阈值:", self._manual_thr_spin)

        self._min_area_spin = QSpinBox()
        self._min_area_spin.setRange(0, 10000)
        self._min_area_spin.setValue(100)
        self._min_area_spin.setSingleStep(50)
        self._min_area_spin.setSuffix(" px")
        form.addRow("最小阳性面积:", self._min_area_spin)

        self._min_density_spin = QDoubleSpinBox()
        self._min_density_spin.setRange(0.0, 1.0)
        self._min_density_spin.setValue(0.05)
        self._min_density_spin.setSingleStep(0.01)
        self._min_density_spin.setDecimals(2)
        form.addRow("最低密度:", self._min_density_spin)

        self._fold_ratio_spin = QDoubleSpinBox()
        self._fold_ratio_spin.setRange(0.0, 1.0)
        self._fold_ratio_spin.setValue(0.35)
        self._fold_ratio_spin.setSingleStep(0.05)
        self._fold_ratio_spin.setDecimals(2)
        self._fold_ratio_spin.setToolTip(
            "自动识别并过滤组织折叠区域\n"
            "基于 DAB/(DAB+H) 比值：折叠区域 H 通道高但 DAB 低\n"
            "低于该阈值的阳性像素会被过滤（0 = 不过滤，推荐 0.3~0.5）"
        )
        form.addRow("折叠过滤阈值:", self._fold_ratio_spin)

        # ─ 范围 ─
        form.addRow(self._section_label("检测范围"))

        self._scope_cb = QComboBox()
        self._scope_cb.addItem("当前切片")
        if len(self._readers) > 1:
            self._scope_cb.addItem(f"全部 {len(self._readers)} 个切片")
        form.addRow("适用范围:", self._scope_cb)

        self._fullres_cb = QCheckBox("level-0 精确验证 (更慢)")
        self._fullres_cb.setToolTip(
            "检测完成后，再读取 level-0 全分辨率图像块\n"
            "做精确的阳性像素分析（更准确但耗时更长）"
        )
        form.addRow("", self._fullres_cb)

        right_scroll.addLayout(form)
        right_scroll.addStretch()

        splitter.addWidget(right_widget)
        splitter.setSizes([550, 300])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)

        # ── 底部按钮 ──
        # ROI 编辑工具栏
        tool_layout = QHBoxLayout()
        tool_layout.setSpacing(6)

        self._draw_btn = QPushButton("框选 ROI")
        self._draw_btn.setCheckable(True)
        self._draw_btn.setMinimumHeight(28)
        self._draw_btn.setEnabled(False)
        self._draw_btn.setToolTip(
            "开启后在预览图上拖拽框选新的 ROI 区域\n"
            "开启时无法拖拽平移，关闭后恢复"
        )
        self._draw_btn.toggled.connect(self._on_draw_mode_toggled)
        tool_layout.addWidget(self._draw_btn)

        self._del_btn = QPushButton("删除选中")
        self._del_btn.setMinimumHeight(28)
        self._del_btn.setEnabled(False)
        self._del_btn.setToolTip("删除预览图上选中的 ROI (也可按 Delete 键)")
        self._del_btn.clicked.connect(self._on_delete_selected_roi)
        tool_layout.addWidget(self._del_btn)

        tool_layout.addStretch()
        main_layout.addLayout(tool_layout)

        # 主按钮栏
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(8)

        self._scan_btn = QPushButton("开始扫描")
        self._scan_btn.setObjectName("primaryBtn")
        self._scan_btn.setMinimumHeight(32)
        self._scan_btn.clicked.connect(self._start_scan)
        btn_layout.addWidget(self._scan_btn)

        self._gen_btn = QPushButton("生成 ROI")
        self._gen_btn.setMinimumHeight(32)
        self._gen_btn.setEnabled(False)  # 扫描完成前不可用
        self._gen_btn.clicked.connect(self._on_generate_roi)
        btn_layout.addWidget(self._gen_btn)

        cancel_btn = QPushButton("取消")
        cancel_btn.setMinimumHeight(32)
        cancel_btn.clicked.connect(self._on_cancel)
        btn_layout.addWidget(cancel_btn)

        main_layout.addLayout(btn_layout)

    @staticmethod
    def _section_label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            "font-weight: 600; color: #f5f5f7; font-size: 12px;"
        )
        return lbl

    # ────────────────────────────────────────────────────────
    #  层级选择
    # ────────────────────────────────────────────────────────

    def _populate_level_cb(self):
        """根据 reader 的金字塔层级信息填充下拉框。"""
        self._level_cb.blockSignals(True)
        self._level_cb.clear()
        levels = self._reader.levels
        # 推荐层级：downsample ≈ 4~8 的层级（画质足够且不太大）
        best_idx = 0
        for i, lv in enumerate(levels):
            label = f"Level {i}  ({lv.width}×{lv.height}, ×{lv.downsample:.1f})"
            self._level_cb.addItem(label, i)
            # 选 downsample 最接近 4 的层级作为默认
            if abs(lv.downsample - 4) < abs(levels[best_idx].downsample - 4):
                best_idx = i
        self._level_cb.setCurrentIndex(best_idx)
        self._level_cb.currentIndexChanged.connect(self._on_level_changed)
        self._level_cb.blockSignals(False)
        self._on_level_changed()

    def _on_level_changed(self, _=None):
        idx = self._level_cb.currentIndex()
        if idx < 0:
            return
        level_idx = self._level_cb.itemData(idx)
        if level_idx is None:
            return
        lv = self._reader.levels[level_idx]
        tile_size = 2048
        ds = lv.downsample
        analysis_ds = 8
        # 逐块处理峰值内存估算
        tile_mb = tile_size * tile_size * 3 * 8 / 1024 / 1024  # float64
        mask_mb = (lv.width / analysis_ds) * (lv.height / analysis_ds) / 1024 / 1024
        prev_ds = min(max(1, math.ceil(max(lv.width, lv.height) / 2048)), analysis_ds)
        prev_mb = (lv.width / prev_ds) * (lv.height / prev_ds) * 3 / 1024 / 1024
        peak_mb = tile_mb + mask_mb + prev_mb + 30  # 30 MB 开销
        self._level_info_lbl.setText(
            f"下采样 ×{ds:.1f}  |  逐块处理峰值 ~{peak_mb:.0f} MB"
        )

    def _get_scan_level(self) -> int:
        idx = self._level_cb.currentIndex()
        return self._level_cb.itemData(idx) if idx >= 0 else 1

    # ────────────────────────────────────────────────────────
    #  框尺寸计算
    # ────────────────────────────────────────────────────────

    def _recalc_frame(self, _=None):
        mag_text = self._mag_cb.currentText()
        ratio_text = self._ratio_cb.currentText()
        if self._mpp <= 0 or ratio_text == "Free":
            self._frame_lbl.setText(f"{self._tile_w} × {self._tile_h}")
            return
        mag = float(mag_text.rstrip("x"))
        fov_mm = FIELD_NUMBER_MM / mag
        d = (16 ** 2 + 9 ** 2) ** 0.5
        if ratio_text == "1:1":
            w = h = fov_mm / 1.4142
        elif ratio_text == "4:3":
            w = fov_mm * 4 / 5; h = fov_mm * 3 / 5
        elif ratio_text == "3:2":
            w = fov_mm * 3 / 3.606; h = fov_mm * 2 / 3.606
        elif ratio_text == "16:9":
            w = fov_mm * 16 / d; h = fov_mm * 9 / d
        else:
            return
        self._tile_w = round(w * 1000 / self._mpp)
        self._tile_h = round(h * 1000 / self._mpp)
        self._frame_lbl.setText(f"{self._tile_w} × {self._tile_h}")

    # ────────────────────────────────────────────────────────
    #  阈值方法切换
    # ────────────────────────────────────────────────────────

    def _on_threshold_method(self, text: str):
        self._manual_thr_spin.setEnabled("手动" in text)

    # ────────────────────────────────────────────────────────
    #  启动扫描
    # ────────────────────────────────────────────────────────

    def _start_scan(self):
        """启动后台逐块扫描 + 检测。"""
        if self._thread is not None and self._thread.isRunning():
            return  # 已经在运行

        # 重置绘制模式
        self._draw_btn.setChecked(False)
        self._preview_view.set_draw_mode(False)

        scan_level = self._get_scan_level()
        thr_method = "manual" if "手动" in self._thr_method_cb.currentText() else "otsu"

        self._scan_btn.setEnabled(False)
        self._gen_btn.setEnabled(False)
        self._scan_progress.setVisible(True)
        self._scan_progress.setMaximum(0)  # indeterminate
        self._stage_lbl.setText("正在准备...")

        self._worker = _ScanWorker(
            reader=self._reader,
            scan_level=scan_level,
            stain_type=self._stain_cb.currentText(),
            threshold_method=thr_method,
            manual_threshold=self._manual_thr_spin.value(),
            min_area=self._min_area_spin.value(),
            window_size=self._window_spin.value(),
            n_hotspots=self._n_spin.value(),
            min_density=self._min_density_spin.value(),
            roi_w=self._tile_w,
            roi_h=self._tile_h,
            fold_ratio_threshold=self._fold_ratio_spin.value(),
            n_workers=2,
            full_res_refine=self._fullres_cb.isChecked(),
        )
        self._thread = QThread()
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.stage.connect(self._on_stage)
        self._worker.progress.connect(self._on_scan_progress)
        self._worker.finished.connect(self._on_scan_finished)
        self._worker.error.connect(self._on_scan_error)

        self._thread.start()

    def _on_stage(self, text: str):
        self._stage_lbl.setText(text)

    def _on_scan_progress(self, current: int, total: int):
        if total > 0:
            self._scan_progress.setMaximum(total)
            self._scan_progress.setValue(current)

    def _on_scan_finished(self, preview_image: np.ndarray, result: dict):
        """扫描完成 — 更新预览。"""
        self._preview_image = preview_image
        self._last_result = result
        self._preview_ds = result["preview_ds"]

        self._scan_progress.setVisible(False)
        self._scan_btn.setEnabled(True)
        self._gen_btn.setEnabled(True)
        self._draw_btn.setEnabled(True)
        self._del_btn.setEnabled(True)
        self._scan_btn.setText("重新扫描")

        # 切换到编辑模式（NoDrag），允许用户直接拖拽/编辑 ROI 矩形
        self._draw_btn.setChecked(False)
        self._preview_view.set_draw_mode(False)

        prev_h, prev_w = preview_image.shape[:2]
        ds = result["scan_ds"]

        # 生成叠加图（预览图 + 降采样 mask）
        pos_mask = result["positive_mask"]
        # 将 mask 缩放到预览图尺寸
        mask_preview = cv2.resize(
            pos_mask, (prev_w, prev_h),
            interpolation=cv2.INTER_NEAREST,
        )
        overlay = make_overlay_image(preview_image, mask_preview > 0)

        qimg = QImage(
            overlay.data, prev_w, prev_h, prev_w * 3,
            QImage.Format.Format_RGB888,
        ).copy()
        self._preview_view.set_image(qimg)

        # 热点坐标 (level-0) → 预览图空间
        preview_ds = self._preview_ds
        hotspots_preview = []
        for (x, y, w, h, d) in result["hotspots"]:
            sx = int(x / preview_ds)
            sy = int(y / preview_ds)
            sw = max(1, int(w / preview_ds))
            sh = max(1, int(h / preview_ds))
            hotspots_preview.append((sx, sy, sw, sh, d))

        self._preview_view.update_overlay(qimg, hotspots_preview)

        info_parts = [
            f"阳性面积: {result['positive_pct']:.1f}%",
            f"热点: {len(result['hotspots'])} 个",
            f"层级: ×{ds:.1f}",
            f"预览: {prev_w}×{prev_h}",
        ]
        self._info_lbl.setText("  |  ".join(info_parts))
        n_hs = len(result["hotspots"])
        if n_hs > 0:
            self._stage_lbl.setText(
                f"扫描完成 — 检测到 {n_hs} 个热点，可拖拽调整后点击「生成 ROI」"
            )
        else:
            self._stage_lbl.setText("扫描完成 — 未检测到热点，可尝试调整检测参数")

    # ────────────────────────────────────────────────────────
    #  ROI 编辑交互
    # ────────────────────────────────────────────────────────

    def _on_draw_mode_toggled(self, checked: bool):
        self._preview_view.set_draw_mode(checked)
        self._draw_btn.setText("关闭框选" if checked else "框选 ROI")
        if checked:
            self._info_lbl.setText(
                "框选模式: 拖拽创建新 ROI  |  滚轮缩放  |  按 0/F 重置视图"
            )
        else:
            self._info_lbl.setText(
                "编辑模式: 点击选中 ROI → 拖拽移动 / 角点缩放 / Del 删除  |  滚轮缩放"
            )

    def _on_delete_selected_roi(self):
        """删除预览图上选中的 ROI。"""
        scene = self._preview_view._scene
        removed = []
        for item in list(self._preview_view._roi_items):
            if item.isSelected():
                scene.removeItem(item)
                removed.append(item)
        for item in removed:
            self._preview_view._roi_items.remove(item)

    def _on_generate_roi(self):
        """生成 ROI — 从预览图上的交互式矩形取坐标，转换到 level-0 空间。

        坐标转换：
          热点在 level-0 空间 → 预览空间 = / preview_ds
          反向：预览空间 → level-0 = × preview_ds
        """
        preview_ds = self._preview_ds

        roi_rects_preview = self._preview_view.get_roi_rects()

        # 回退：如果预览图上没有编辑过的 ROI，使用原始检测结果
        if not roi_rects_preview and self._last_result is not None:
            hotspots = self._last_result.get("hotspots", [])
            if hotspots:
                self._stage_lbl.setText(f"已生成 {len(hotspots)} 个 ROI")
                self.accept()
                return

        if not roi_rects_preview:
            self._info_lbl.setText("没有 ROI — 请先完成扫描检测热点")
            return

        # 预览坐标 → level-0 坐标
        hotspots_level0 = []
        for rect in roi_rects_preview:
            x0 = max(0, int(rect.left() * preview_ds))
            y0 = max(0, int(rect.top() * preview_ds))
            w0 = max(1, int(rect.width() * preview_ds))
            h0 = max(1, int(rect.height() * preview_ds))
            hotspots_level0.append((x0, y0, w0, h0, 0.0))

        if self._last_result is not None:
            self._last_result["hotspots"] = hotspots_level0
        else:
            self._last_result = {"hotspots": hotspots_level0}

        self._stage_lbl.setText(f"已生成 {len(hotspots_level0)} 个 ROI")
        self.accept()

    def _on_scan_error(self, msg: str):
        self._scan_progress.setVisible(False)
        self._scan_btn.setEnabled(True)
        self._stage_lbl.setText("")
        self._info_lbl.setText(f"检测失败: {msg}")

    def _on_cancel(self):
        self._stop_worker_thread()
        self.reject()

    def _stop_worker_thread(self):
        """安全停止后台工作线程，确保线程完全退出后再继续。"""
        if self._thread is None or not self._thread.isRunning():
            return
        try:
            self._worker.stage.disconnect()
            self._worker.progress.disconnect()
            self._worker.finished.disconnect()
            self._worker.error.disconnect()
        except (RuntimeError, TypeError):
            pass
        self._worker.cancel()
        # 轮询等待线程退出，保持事件循环响应
        for _ in range(20):  # 最多等 10 秒
            self._thread.quit()
            if self._thread.wait(500):
                break
        else:
            # 超时强制终止（最后手段）
            logger.warning("工作线程未响应，强制终止")
            self._thread.terminate()
            self._thread.wait(2000)

    # ────────────────────────────────────────────────────────
    #  公共接口
    # ────────────────────────────────────────────────────────

    def get_params(self) -> dict:
        scope_text = self._scope_cb.currentText()
        thr_method = "manual" if "手动" in self._thr_method_cb.currentText() else "otsu"
        return {
            "stain_type":       self._stain_cb.currentText(),
            "tile_w":           self._tile_w,
            "tile_h":           self._tile_h,
            "n_hotspots":       self._n_spin.value(),
            "window_size":      self._window_spin.value(),
            "threshold_method": thr_method,
            "manual_threshold": self._manual_thr_spin.value(),
            "min_area":         self._min_area_spin.value(),
            "min_density":      self._min_density_spin.value(),
            "fold_ratio_threshold": self._fold_ratio_spin.value(),
            "scope":            "all" if "全部" in scope_text else "current",
            "full_res":         self._fullres_cb.isChecked(),
            "scan_level":       self._get_scan_level(),
            "mag":              self._mag_cb.currentText(),
            "ratio":            self._ratio_cb.currentText(),
        }

    def get_last_result(self) -> dict | None:
        return self._last_result

    def closeEvent(self, event):
        self._stop_worker_thread()
        super().closeEvent(event)
