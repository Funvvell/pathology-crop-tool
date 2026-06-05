"""组织检测 — 基于 HistoKit 三通道阈值算法的组织区域识别。"""

from __future__ import annotations

import warnings

import cv2
import numpy as np
from scipy import ndimage as ndi
from skimage import measure, morphology

from liver_portal_crop.reader import SDPCReader


# ═══════════════════════════════════════════════════
#  RGB 直方图 + 阈值
# ═══════════════════════════════════════════════════

def _get_pixel_distribution(img: np.ndarray):
    bins = np.arange(-0.5, 256.5, 1)
    R, _ = np.histogram(img[:, :, 0].ravel(), bins=bins)
    G, _ = np.histogram(img[:, :, 1].ravel(), bins=bins)
    B, _ = np.histogram(img[:, :, 2].ravel(), bins=bins)
    # 排除纯白像素(254-255) — 这些是扫描仪饱和伪影/玻璃边缘反光，
    # 不是组织颜色信息，会拉高阈值导致组织区域缩小
    R[254:] = 0; G[254:] = 0; B[254:] = 0
    return R, G, B


def _otsuthresh(counts: np.ndarray) -> float:
    counts = np.asarray(counts, dtype=np.float64).ravel()
    nb = counts.size
    p = counts / (counts.sum() + 1e-10)
    omega = np.cumsum(p)
    mu = np.cumsum(p * np.arange(1, nb + 1))
    mu_t = mu[-1]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        sb = (mu_t * omega - mu) ** 2 / (omega * (1 - omega) + 1e-10)
    sb = np.nan_to_num(sb, nan=-np.inf)
    mx = sb.max()
    if np.isfinite(mx) and mx > 0:
        return (np.mean(np.where(sb == mx)[0]) + 0.5) / nb
    return 0.0


def _two_step_otsu(hist: np.ndarray) -> int:
    t1 = _otsuthresh(hist)
    t1 = int(t1 * 255)
    # 在尾部分布上再做一次 Otsu；防止切片全零时返回 255
    tail_start = min(max(t1 - 1, 0), 252)
    t2 = _otsuthresh(hist[tail_start:])
    return int(t1 + (255 - t1) * t2 + 0.5)


def _get_thr_image(img: np.ndarray, thr_min: float = 0.7 * 255):
    R, G, B = _get_pixel_distribution(img)
    thr = {}
    for ch_name, ch_hist in [("R", R), ("G", G), ("B", B)]:
        v = float(_two_step_otsu(ch_hist))
        if v < thr_min:
            # 阈值过低时回退到简单 Otsu
            counts = np.asarray(ch_hist, dtype=np.float64).ravel()
            if counts.sum() > 0:
                v = float(_otsuthresh(counts) * 255)
        thr[ch_name] = v
    return thr, R, G, B


# ═══════════════════════════════════════════════════
#  形态学
# ═══════════════════════════════════════════════════

def _get_strel_disk(radius: int) -> np.ndarray:
    d = 2 * radius + 1
    Y, X = np.ogrid[:d, :d]
    dist = np.sqrt((X - radius) ** 2 + (Y - radius) ** 2)
    return (dist <= radius).astype(np.uint8)


def _remove_small_objects(mask: np.ndarray, min_pct: float = 0.02) -> np.ndarray:
    props = measure.regionprops(measure.label(mask.astype(bool)))
    areas = np.array([p.area for p in props])
    if len(areas) == 0:
        return mask
    thr_area = np.max(areas) * min_pct
    return morphology.remove_small_objects(
        mask.astype(bool), max_size=int(thr_area), connectivity=2
    ).astype(np.uint8) * 255


# ═══════════════════════════════════════════════════
#  主函数
# ═══════════════════════════════════════════════════

def detect_tissue(
    image: np.ndarray,
    open_radius: int = 3,
    close_radius: int = 5,
    fill_holes: bool = True,
    remove_small: bool = True,
    min_area_pct: float = 0.05,
) -> dict:
    """组织区域检测（基于 HistoKit 三通道阈值法）。

    Args:
        image: RGB 图像 (H, W, 3) uint8
        open_radius: 开运算磁盘半径
        close_radius: 闭运算磁盘半径
        fill_holes: 填充组织内部孔洞
        remove_small: 移除小碎片
        min_area_pct: 最小组织面积占比（相对最大区域）

    Returns:
        {"mask": np.uint8, "thr": dict, "pct": float}
    """
    img = np.asarray(image)
    thr, _, _, _ = _get_thr_image(img)

    # 三通道组合判定：至少两通道高于阈值 → 背景（白色/亮区）
    # 否则 → 组织
    bright = (img[:, :, 0] > thr["R"]).astype(np.uint8) + \
             (img[:, :, 1] > thr["G"]).astype(np.uint8) + \
             (img[:, :, 2] > thr["B"]).astype(np.uint8)
    mask = bright < 2

    se_close = _get_strel_disk(close_radius)
    se_open = _get_strel_disk(open_radius)
    mask = ndi.binary_closing(mask, se_close)
    mask = ndi.binary_opening(mask, se_open)

    if fill_holes:
        mask = ndi.binary_fill_holes(mask)

    mask_out = (mask.astype(np.uint8)) * 255

    if remove_small:
        mask_out = _remove_small_objects(mask_out, min_pct=min_area_pct)

    pct = float(mask_out.sum() / 255 / mask_out.size * 100)
    return {"mask": mask_out, "thr": thr, "pct": pct}


def tissue_regions_to_rois_grid(
    mask: np.ndarray,
    scale_x: float, scale_y: float,
    tile_w: int, tile_h: int,
    stride_x_px: int, stride_y_px: int,
    max_count: int = 50,
) -> list[tuple[int, int, int, int]]:
    """网格模式：在组织区域内按步长均匀生成 ROI。

    步长 = 框尺寸 × 步长比
    """
    mask_h, mask_w = mask.shape[:2]
    s_thumb_x = max(1, int(stride_x_px / scale_x))
    s_thumb_y = max(1, int(stride_y_px / scale_y))
    t_thumb_w = max(1, int(tile_w / scale_x))
    t_thumb_h = max(1, int(tile_h / scale_y))

    candidates: list[tuple[float, int, int]] = []  # (tissue_ratio, cx, cy)
    y = 0
    while y + t_thumb_h <= mask_h:
        x = 0
        while x + t_thumb_w <= mask_w:
            patch = mask[y:y + t_thumb_h, x:x + t_thumb_w]
            ratio = patch.mean() / 255.0
            if ratio > 0.1:
                cx = int((x + t_thumb_w // 2) * scale_x)
                cy = int((y + t_thumb_h // 2) * scale_y)
                candidates.append((ratio, cx, cy))
            x += s_thumb_x
        y += s_thumb_y

    candidates.sort(key=lambda c: -c[0])
    limit = len(candidates) if max_count <= 0 else min(max_count, len(candidates))
    rois = []
    for _, cx, cy in candidates[:limit]:
        rois.append((max(0, cx - tile_w // 2), max(0, cy - tile_h // 2), tile_w, tile_h))
    return rois


def tissue_regions_to_rois(
    mask: np.ndarray,
    scale_x: float,
    scale_y: float,
    tile_w: int,
    tile_h: int,
    max_count: int = 10,
) -> list[tuple[int, int, int, int]]:
    mask_bin = (mask > 0).astype(np.uint8)
    _, labels, stats, centroids = cv2.connectedComponentsWithStats(mask_bin, connectivity=8)
    regions = []
    for i in range(1, len(stats)):
        regions.append({
            "area": stats[i, cv2.CC_STAT_AREA],
            "cx": int(centroids[i][0] * scale_x),
            "cy": int(centroids[i][1] * scale_y),
        })
    regions.sort(key=lambda r: -r["area"])
    limit = len(regions) if max_count <= 0 else min(max_count, len(regions))
    rois = []
    for r in regions[:limit]:
        rois.append((max(0, r["cx"] - tile_w // 2), max(0, r["cy"] - tile_h // 2), tile_w, tile_h))
    return rois


# ═══════════════════════════════════════════════════
#  参数调节对话框
# ═══════════════════════════════════════════════════

from PySide6.QtCore import Qt, QRectF, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QLabel, QPushButton, QSlider,
    QSpinBox, QVBoxLayout,
)


class TissueDialog(QDialog):
    """组织检测参数对话框（含实时预览）。"""

    def __init__(self, reader: SDPCReader, tile_w: int, tile_h: int, parent=None,
                 readers: dict = None, current_slide=None):
        super().__init__(parent)
        self.setWindowTitle("组织检测参数")
        self.setMinimumSize(540, 620)
        self._reader = reader
        self._tile_w = tile_w
        self._tile_h = tile_h
        self._thumb = reader.thumbnail.copy()
        self._mpp = reader.mpp or 0.0
        self._readers = readers or {}
        self._current_slide = current_slide
        self._result: dict | None = None
        self._setup_ui()
        self._recalc_frame()
        # 延迟到对话框显示后再计算预览，避免 __init__ 中同步运行
        # detect_tissue()（CPU密集型）阻塞主线程导致窗口闪烁
        QTimer.singleShot(0, self._update_preview)

    def _setup_ui(self):
        vl = QVBoxLayout(self)

        # 预览图
        self._preview_lbl = QLabel()
        self._preview_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._info_lbl = QLabel()
        self._info_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        vl.addWidget(self._preview_lbl)
        vl.addWidget(self._info_lbl)

        # 参数表单
        form = QFormLayout()

        # 倍率
        self._mag_cb = QComboBox()
        self._mag_cb.addItems(["4x", "10x", "20x", "40x", "80x"])
        self._mag_cb.setCurrentText("20x")
        self._mag_cb.currentTextChanged.connect(self._recalc_frame)
        self._mag_cb.currentTextChanged.connect(self._update_preview)
        form.addRow("倍率:", self._mag_cb)

        # 比例
        self._ratio_cb = QComboBox()
        self._ratio_cb.addItems(["Free", "1:1", "4:3", "3:2", "16:9"])
        self._ratio_cb.setCurrentText("16:9")
        self._ratio_cb.currentTextChanged.connect(self._recalc_frame)
        self._ratio_cb.currentTextChanged.connect(self._update_preview)
        form.addRow("比例:", self._ratio_cb)

        # 框尺寸（只读）
        self._frame_lbl = QLabel()
        self._frame_lbl.setStyleSheet("color: #c1c2c5; font-size: 12px;")
        form.addRow("框尺寸:", self._frame_lbl)

        # 适用范围（多文件时显示）
        self._scope_cb = QComboBox()
        self._scope_cb.addItem("当前切片")
        if len(self._readers) > 1:
            self._scope_cb.addItem(f"全部 {len(self._readers)} 个切片")
        form.addRow("适用范围:", self._scope_cb)

        form.addRow("", QLabel(""))

        # 模式选择
        self._mode_cb = QComboBox()
        self._mode_cb.addItems(["网格（均匀分布）", "连通域（大块组织）"])
        self._mode_cb.setCurrentText("网格（均匀分布）")
        self._mode_cb.currentTextChanged.connect(self._update_preview)
        form.addRow("ROI 生成模式:", self._mode_cb)

        self._open_spin = QSpinBox()
        self._open_spin.setRange(0, 15)
        self._open_spin.setValue(3)
        self._open_spin.valueChanged.connect(self._update_preview)
        form.addRow("开运算半径:", self._open_spin)

        self._close_spin = QSpinBox()
        self._close_spin.setRange(0, 15)
        self._close_spin.setValue(5)
        self._close_spin.valueChanged.connect(self._update_preview)
        form.addRow("闭运算半径:", self._close_spin)

        self._fill_cb = QCheckBox("填充孔洞")
        self._fill_cb.setChecked(True)
        self._fill_cb.toggled.connect(self._update_preview)
        form.addRow("", self._fill_cb)

        self._remove_cb = QCheckBox("移除小碎片")
        self._remove_cb.setChecked(True)
        self._remove_cb.toggled.connect(self._update_preview)
        form.addRow("", self._remove_cb)

        self._min_area_spin = QSpinBox()
        self._min_area_spin.setRange(1, 50)
        self._min_area_spin.setValue(5)
        self._min_area_spin.setSuffix("%")
        self._min_area_spin.valueChanged.connect(self._update_preview)
        form.addRow("最小面积占比:", self._min_area_spin)

        self._max_count_spin = QSpinBox()
        self._max_count_spin.setRange(1, 9999)
        self._max_count_spin.setValue(5)
        form.addRow("最大 ROI 数:", self._max_count_spin)

        self._stride_spin = QSpinBox()
        self._stride_spin.setRange(1, 20)
        self._stride_spin.setValue(1)
        self._stride_spin.setSuffix(" × 框尺寸")
        self._stride_spin.valueChanged.connect(self._update_preview)
        form.addRow("网格间距:", self._stride_spin)

        vl.addLayout(form)

        # 底部按钮
        btn_lay = QHBoxLayout()
        self._gen_btn = QPushButton("生成 ROI")
        self._gen_btn.clicked.connect(self.accept)
        self._gen_btn.setDefault(True)
        btn_lay.addWidget(self._gen_btn)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_lay.addWidget(cancel_btn)
        vl.addLayout(btn_lay)

    def _recalc_frame(self):
        """倍率/比例变化时重新计算框尺寸（预览由信号链单独触发）。"""
        mag_text = self._mag_cb.currentText()
        ratio_text = self._ratio_cb.currentText()
        if self._mpp <= 0 or mag_text == "自定义" or ratio_text == "Free":
            self._frame_lbl.setText(f"{self._tile_w} × {self._tile_h}")
            return
        mag = float(mag_text.rstrip("x"))
        FN = 22.0; fov_mm = FN / mag
        d = (16**2 + 9**2)**0.5
        if ratio_text == "1:1": w = h = fov_mm / 1.4142
        elif ratio_text == "4:3": w = fov_mm*4/5; h = fov_mm*3/5
        elif ratio_text == "3:2": w = fov_mm*3/3.606; h = fov_mm*2/3.606
        elif ratio_text == "16:9": w = fov_mm*16/d; h = fov_mm*9/d
        else: return
        self._tile_w = round(w * 1000 / self._mpp)
        self._tile_h = round(h * 1000 / self._mpp)
        self._frame_lbl.setText(f"{self._tile_w} × {self._tile_h}")

    def _update_preview(self, _=None):
        result = detect_tissue(
            self._thumb,
            open_radius=self._open_spin.value(),
            close_radius=self._close_spin.value(),
            fill_holes=self._fill_cb.isChecked(),
            remove_small=self._remove_cb.isChecked(),
            min_area_pct=self._min_area_spin.value() / 100.0,
        )
        self._result = result
        mask = result["mask"]

        # 预估 ROI 数
        scale_x = self._reader.full_width / self._thumb.shape[1]
        scale_y = self._reader.full_height / self._thumb.shape[0]
        tw, th = self._tile_w, self._tile_h
        is_grid = "网格" in self._mode_cb.currentText()
        if is_grid:
            stride = self._stride_spin.value()
            rois = tissue_regions_to_rois_grid(mask, scale_x, scale_y, tw, th,
                                                tw * stride, th * stride,
                                                self._max_count_spin.value())
        else:
            rois = tissue_regions_to_rois(mask, scale_x, scale_y, tw, th,
                                           self._max_count_spin.value())

        # 红色叠加
        overlay = self._thumb.copy()
        m3 = np.stack([mask] * 3, axis=-1) // 255
        overlay = np.where(
            m3 == 1,
            overlay * 0.4 + np.full_like(self._thumb, [[180, 30, 30]], dtype=np.uint8) * 0.6,
            overlay,
        ).clip(0, 255).astype(np.uint8)

        h, w = overlay.shape[:2]
        img = QImage(overlay.data, w, h, w * 3, QImage.Format.Format_RGB888)
        pix = QPixmap.fromImage(img).scaled(500, 300, Qt.AspectRatioMode.KeepAspectRatio)
        self._preview_lbl.setPixmap(pix)
        self._info_lbl.setText(
            f"组织: {result['pct']:.1f}%  |  预估 ROI: {len(rois)} 个  |  "
            f"模式: {'网格' if is_grid else '连通域'}"
        )

    def get_params(self) -> dict:
        is_grid = "网格" in self._mode_cb.currentText()
        scope_text = self._scope_cb.currentText()
        return {
            "mode": "grid" if is_grid else "region",
            "scope": "all" if "全部" in scope_text else "current",
            "open_radius": self._open_spin.value(),
            "close_radius": self._close_spin.value(),
            "fill_holes": self._fill_cb.isChecked(),
            "remove_small": self._remove_cb.isChecked(),
            "min_area_pct": self._min_area_spin.value() / 100.0,
            "max_count": self._max_count_spin.value(),
            "stride": self._stride_spin.value(),
            "mag": self._mag_cb.currentText(),
            "ratio": self._ratio_cb.currentText(),
            "tile_w": self._tile_w,
            "tile_h": self._tile_h,
        }
