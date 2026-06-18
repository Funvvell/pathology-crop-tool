"""测试 IHC 热点检测核心算法（不需要 SDPC DLL）。"""
import sys
import os
import types
import math
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock DLL 依赖
fake_reader = types.ModuleType("liver_portal_crop.reader")
fake_reader.SDPCReader = object
fake_reader.SDPCReadError = Exception
sys.modules["liver_portal_crop.reader"] = fake_reader
fake_constants = types.ModuleType("liver_portal_crop.constants")
fake_constants.FIELD_NUMBER_MM = 22.0
sys.modules["liver_portal_crop.constants"] = fake_constants

from liver_portal_crop.ihc_hotspot import (
    color_deconvolution,
    threshold_positive,
    compute_density_map,
    find_hotspots,
    detect_ihc_hotspots_tiled,
    make_overlay_image,
    _sample_otsu_threshold,
    _accumulate_tiled,
    _get_h_weights,
    _process_single_tile,
    _fit_hotspots_to_tissue,
    _filter_hotspots_by_tissue_coverage,
    STAIN_LABELS,
)

print("=== 算法导入 OK ===")
print(f"染色类型: {STAIN_LABELS}")

# ─── 测试 1: 颜色反卷积 ───
img = np.full((300, 400, 3), [180, 180, 220], dtype=np.uint8)
img[50:150, 50:150]   = [130, 75, 35]   # 大 DAB 区域
img[80:120, 200:280]  = [140, 80, 40]   # 中 DAB 区域
img[200:240, 300:360] = [145, 85, 45]   # 小 DAB 区域

for stain in STAIN_LABELS:
    deconv = color_deconvolution(img, stain)
    assert deconv["positive"].shape == (300, 400), f"{stain} shape mismatch"
    print(f"  {stain}: deconv OK")

# ─── 测试 2: 阈值分割 ───
deconv = color_deconvolution(img, "H-DAB")
pos_mask = threshold_positive(deconv["positive"], method="otsu", min_area=20)
assert pos_mask.shape == (300, 400)
assert pos_mask.dtype == bool
pct = pos_mask.sum() / pos_mask.size * 100
print(f"\nOtsu 阈值分割: 阳性面积 {pct:.1f}%")
assert pct > 0

# ─── 测试 3: 密度图 + 热点提取 ───
density = compute_density_map(pos_mask, window_size=50, stride=25)
assert density.shape[0] > 0 and density.shape[1] > 0
print(f"密度图: {density.shape}, max={density.max():.3f}")

hotspots = find_hotspots(
    density, stride=25, roi_w=80, roi_h=80,
    scale_x=1.0, scale_y=1.0,
    n_hotspots=3, min_density=0.01,
)
print(f"热点数: {len(hotspots)}")
for i, (x, y, w, h, d) in enumerate(hotspots):
    print(f"  #{i+1}: ({x},{y}) {w}x{h} density={d:.2%}")
assert len(hotspots) > 0

# ─── 测试 4: 叠加图 ───
overlay = make_overlay_image(img, pos_mask)
assert overlay.shape == img.shape
print(f"叠加图: {overlay.shape}")

# ─── 测试 5: Mock Reader + 逐块检测 ───
# 创建合成大图 (模拟金字塔层级)
BIG_H, BIG_W = 2000, 3000
big_image = np.random.randint(180, 230, (BIG_H, BIG_W, 3), dtype=np.uint8)
big_image[200:600, 200:600] = [125, 70, 30]    # 大阳性区域
big_image[800:1200, 1500:2000] = [135, 75, 35]  # 中阳性区域
big_image[1400:1700, 2200:2700] = [140, 80, 40]  # 小阳性区域


class _LevelInfo:
    def __init__(self, w, h, ds):
        self.width = w
        self.height = h
        self.downsample = ds


class MockReader:
    """模拟 SDPCReader — 从内存中的大图中裁剪 tile。"""

    def __init__(self, image, downsample=1.0):
        self._image = image
        h, w = image.shape[:2]
        self.levels = [_LevelInfo(w, h, downsample)]
        self.mpp = 0.5
        # 生成缩略图（组织检测需要）
        thumb_scale = max(1, max(w, h) // 512)
        import cv2 as _cv2
        self._thumbnail = _cv2.resize(
            image, (w // thumb_scale, h // thumb_scale),
            interpolation=_cv2.INTER_AREA,
        )

    @property
    def thumbnail(self):
        return self._thumbnail

    def _read_level_region(self, level, lx, ly, lw, lh):
        return self._image[ly:ly + lh, lx:lx + lw].copy()

    def extract_region(self, x, y, w, h, level=0):
        return self._image[y:y + h, x:x + w].copy()


mock = MockReader(big_image, downsample=4.0)

# 测试 _sample_otsu_threshold
otsu_val = _sample_otsu_threshold(mock, level=0, stain_type="H-DAB", tile_size=1024)
print(f"\n估计 Otsu 阈值: {otsu_val:.1f}")
assert 0 < otsu_val < 256

# 测试 _accumulate_tiled
acc_mask, preview, thr, done, total = _accumulate_tiled(
    reader=mock, level=0, stain_type="H-DAB",
    threshold_method="otsu", manual_threshold=0.3,
    estimated_otsu=otsu_val, min_area=50,
    tile_size=1024, analysis_ds=4, preview_ds=8,
)
print(f"累加 mask: {acc_mask.shape}, 预览: {preview.shape}")
print(f"处理 tile: {done}/{total}")
assert acc_mask.shape == (math.ceil(BIG_H / 4), math.ceil(BIG_W / 4))
assert preview.shape[:2] == (math.ceil(BIG_H / 8), math.ceil(BIG_W / 8))
assert done == total
pos_pct = (acc_mask > 0).sum() / acc_mask.size * 100
print(f"累加 mask 阳性: {pos_pct:.1f}%")

# 测试 detect_ihc_hotspots_tiled（完整流程）
result = detect_ihc_hotspots_tiled(
    reader=mock, level=0, stain_type="H-DAB",
    threshold_method="otsu", min_area=50,
    window_size=200, n_hotspots=5, min_density=0.01,
    roi_w=200, roi_h=200, tile_size=1024,
    analysis_ds=4, max_preview_dim=1024,
)
print(f"\n逐块检测完整流程:")
print(f"  阳性面积: {result['positive_pct']:.1f}%")
print(f"  热点: {len(result['hotspots'])}")
print(f"  预览图: {result['preview_image'].shape}")
print(f"  preview_ds: {result['preview_ds']}")
print(f"  scan_ds: {result['scan_ds']}")
for i, (x, y, w, h, d) in enumerate(result["hotspots"]):
    print(f"  #{i+1}: ({x},{y}) {w}x{h} density={d:.2%}")

assert result["positive_pct"] > 0
assert result["preview_image"].ndim == 3
assert result["preview_ds"] <= result["analysis_ds"]  # 预览分辨率 ≥ mask
assert result["tissue_tiles"] <= result["total_tiles"]
print(f"  组织 tile: {result['tissue_tiles']}/{result['total_tiles']}")

# 测试手动阈值模式
result_manual = detect_ihc_hotspots_tiled(
    reader=mock, level=0, stain_type="H-DAB",
    threshold_method="manual", manual_threshold=0.25,
    min_area=50, window_size=200, n_hotspots=3,
    min_density=0.01, roi_w=200, roi_h=200,
    tile_size=1024, analysis_ds=4, max_preview_dim=1024,
)
print(f"\n手动阈值模式: 阳性 {result_manual['positive_pct']:.1f}%")

# ─── 测试 6: 折叠区域过滤 ───
print("\n--- 折叠过滤测试 ---")

# 6a: _get_h_weights 返回正确的权重向量
h_w = _get_h_weights("H-DAB")
assert h_w.shape == (3,), f"H 权重 shape 错误: {h_w.shape}"
print(f"H-DAB H 通道权重: {h_w}")

# 6b: 创建含折叠区域的合成图
# 折叠区域特征：DAB 有一定信号（Otsu 会判为阳性）但 H 通道更高 → dab_frac 低
FOLD_H, FOLD_W = 2000, 3000
fold_image = np.random.randint(200, 230, (FOLD_H, FOLD_W, 3), dtype=np.uint8)
# 真阳性区域（棕色，DAB 高，dab_frac ≈ 1.0）
fold_image[200:600, 200:600] = [125, 70, 30]
# 模拟折叠区域（暗蓝紫色，DAB=0.055 H=0.196, frac≈0.22）
# Otsu 会判为阳性（有DAB信号），但 dab_frac < 0.35 → 被折叠过滤器移除
fold_image[800:1200, 800:1200] = [30, 30, 60]

mock_fold = MockReader(fold_image, downsample=4.0)
otsu_fold = _sample_otsu_threshold(mock_fold, 0, "H-DAB", 1024)

# 不过滤
acc_no_filter, _, _, _, _ = _accumulate_tiled(
    reader=mock_fold, level=0, stain_type="H-DAB",
    threshold_method="otsu", manual_threshold=0.3,
    estimated_otsu=otsu_fold, min_area=50,
    tile_size=1024, analysis_ds=4, preview_ds=8,
    fold_ratio_threshold=0.0,
)
pct_no_filter = (acc_no_filter > 0).sum() / acc_no_filter.size * 100

# 过滤阈值 0.35
acc_filter, _, _, _, _ = _accumulate_tiled(
    reader=mock_fold, level=0, stain_type="H-DAB",
    threshold_method="otsu", manual_threshold=0.3,
    estimated_otsu=otsu_fold, min_area=50,
    tile_size=1024, analysis_ds=4, preview_ds=8,
    fold_ratio_threshold=0.35,
)
pct_filter = (acc_filter > 0).sum() / acc_filter.size * 100

print(f"不过滤阳性面积: {pct_no_filter:.1f}%")
print(f"折叠过滤(0.35)阳性面积: {pct_filter:.1f}%")
# 过滤后阳性面积应减少（折叠区域被移除）
assert pct_filter <= pct_no_filter, "折叠过滤应减少阳性面积"
removed_pct = (pct_no_filter - pct_filter) / pct_no_filter * 100 if pct_no_filter > 0 else 0
print(f"折叠区域被移除: {removed_pct:.1f}%")

# 6c: 完整流程带折叠过滤
result_fold = detect_ihc_hotspots_tiled(
    reader=mock_fold, level=0, stain_type="H-DAB",
    threshold_method="otsu", min_area=50,
    window_size=200, n_hotspots=5, min_density=0.01,
    roi_w=200, roi_h=200, tile_size=1024,
    analysis_ds=4, max_preview_dim=1024,
    fold_ratio_threshold=0.35,
)
print(f"完整流程（折叠过滤）: 阳性 {result_fold['positive_pct']:.1f}%")
print(f"  热点数: {len(result_fold['hotspots'])}")

# ─── 测试 7: 并行处理 ───
print("\n--- 并行处理测试 ---")

# 7a: 并行 _accumulate_tiled
acc_parallel, preview_parallel, _, done_p, total_p = _accumulate_tiled(
    reader=mock, level=0, stain_type="H-DAB",
    threshold_method="otsu", manual_threshold=0.3,
    estimated_otsu=otsu_val, min_area=50,
    tile_size=1024, analysis_ds=4, preview_ds=8,
    n_workers=2,
)
pct_parallel = (acc_parallel > 0).sum() / acc_parallel.size * 100
print(f"并行(2线程) mask: {acc_parallel.shape}, 阳性: {pct_parallel:.1f}%")
print(f"  处理 tile: {done_p}/{total_p}")
assert done_p == total_p

# 7b: 并行 vs 串行结果应一致
pct_serial = (acc_mask > 0).sum() / acc_mask.size * 100
diff = abs(pct_parallel - pct_serial)
print(f"串行 vs 并行差异: {diff:.2f}%")
assert diff < 1.0, f"并行与串行结果差异过大: {diff:.2f}%"

# 7c: 完整流程带并行
result_parallel = detect_ihc_hotspots_tiled(
    reader=mock, level=0, stain_type="H-DAB",
    threshold_method="otsu", min_area=50,
    window_size=200, n_hotspots=5, min_density=0.01,
    roi_w=200, roi_h=200, tile_size=1024,
    analysis_ds=4, max_preview_dim=1024,
    n_workers=2,
)
print(f"并行完整流程: 阳性 {result_parallel['positive_pct']:.1f}%, 热点 {len(result_parallel['hotspots'])}")

# ─── 测试 8: 取消回调 ───
print("\n--- 取消回调测试 ---")

cancel_after = 2
call_count = [0]
def cancel_cb():
    call_count[0] += 1
    return call_count[0] > cancel_after

acc_cancel, _, _, done_c, total_c = _accumulate_tiled(
    reader=mock, level=0, stain_type="H-DAB",
    threshold_method="otsu", manual_threshold=0.3,
    estimated_otsu=otsu_val, min_area=50,
    tile_size=1024, analysis_ds=4, preview_ds=8,
    cancel_callback=cancel_cb,
)
print(f"取消后处理 tile: {done_c}/{total_c}")
assert done_c < total_c, f"取消应提前停止: done={done_c}, total={total_c}"
print(f"提前停止成功: {done_c} < {total_c}")

# ─── 测试 9: 组织预检测跳过背景 tile ───
print("\n--- 组织预检测测试 ---")

from liver_portal_crop.ihc_hotspot import _get_tissue_tile_set

# 9a: 直接构造 tissue_tile_set 验证过滤逻辑
# 使用现有 big_image（3000×2000），tile_size=1024 → 3×2 = 6 tiles
# 只保留前 3 个 tile，模拟组织预检测跳过一半
partial_tset = {(0, 0), (0, 1), (1, 0)}  # 3 out of 6

acc_partial, _, _, done_part, total_part = _accumulate_tiled(
    reader=mock, level=0, stain_type="H-DAB",
    threshold_method="otsu", manual_threshold=0.3,
    estimated_otsu=otsu_val, min_area=50,
    tile_size=1024, analysis_ds=4, preview_ds=8,
    tissue_tile_set=partial_tset,
)
print(f"组织过滤(3/6 tile): done={done_part}, total={total_part}")
assert total_part == 3, f"应只处理 3 个组织 tile，实际 {total_part}"
assert done_part == 3

# 9b: 完整流程带 tissue_tile_set
# 传入 None 时走自动组织检测
result_auto = detect_ihc_hotspots_tiled(
    reader=mock, level=0, stain_type="H-DAB",
    threshold_method="otsu", min_area=50,
    window_size=200, n_hotspots=5, min_density=0.01,
    roi_w=200, roi_h=200, tile_size=1024,
    analysis_ds=4, max_preview_dim=1024,
)
print(f"自动组织检测: tile {result_auto['tissue_tiles']}/{result_auto['total_tiles']}")
assert "tissue_tiles" in result_auto
assert "total_tiles" in result_auto
assert result_auto["tissue_tiles"] <= result_auto["total_tiles"]

# ─── 测试 10: 白色/空白区域像素过滤 ───
print("\n--- 白色/空白区域过滤测试 ---")

from liver_portal_crop.ihc_hotspot import _get_positive_weights

# 10a: 全白 tile 应返回 None
white_tile = np.full((512, 512, 3), 250, dtype=np.uint8)
white_reader = MockReader(white_tile, downsample=1.0)
dab_w = _get_positive_weights("H-DAB")
binary_w, _ = _process_single_tile(
    white_reader, 0, 0, 0, 512, 512,
    dab_w, None, "otsu", 80.0, 0.0, 0.0,
)
assert binary_w is None, "全白 tile 应返回 None"
print("全白 tile: 正确跳过")

# 10b: 大部分白色 + 小阳性区域的 tile
mixed_tile = np.full((512, 512, 3), 245, dtype=np.uint8)
mixed_tile[200:300, 200:300] = [120, 60, 25]  # DAB 阳性小块
mixed_reader = MockReader(mixed_tile, downsample=1.0)
binary_m, _ = _process_single_tile(
    mixed_reader, 0, 0, 0, 512, 512,
    dab_w, None, "otsu", 80.0, 0.0, 0.0,
)
if binary_m is not None:
    # 白色区域的阳性像素应被过滤
    # 检查白色区域（非 DAB 小块）的阳性像素
    white_region_mask = binary_m.copy()
    white_region_mask[200:300, 200:300] = 0  # 移除已知的 DAB 区域
    white_positive = (white_region_mask > 0).sum()
    total_positive = (binary_m > 0).sum()
    print(f"混合 tile: 总阳性像素 {total_positive}, 白色区域残留 {white_positive}")
    # 白色区域应几乎没有阳性像素
    if total_positive > 0:
        white_ratio = white_positive / total_positive
        print(f"白色区域阳性占比: {white_ratio:.2%}")
        assert white_ratio < 0.1, f"白色区域残留阳性过多: {white_ratio:.2%}"
else:
    print("混合 tile: 信号不足，整体跳过（可接受）")

# 10c: 完整流程中白色区域不产生热点
white_bg = np.full((2000, 3000, 3), 248, dtype=np.uint8)
# 添加轻微噪声模拟真实扫描背景（但仍保持高亮度）
noise = np.random.randint(235, 255, (2000, 3000, 3), dtype=np.uint8)
white_bg = np.where(white_bg > 240, noise, white_bg).astype(np.uint8)
white_bg[400:800, 400:800] = [125, 70, 30]  # 唯一的真阳性区域
mock_white = MockReader(white_bg, downsample=4.0)
result_white = detect_ihc_hotspots_tiled(
    reader=mock_white, level=0, stain_type="H-DAB",
    threshold_method="otsu", min_area=50,
    window_size=200, n_hotspots=5, min_density=0.01,
    roi_w=200, roi_h=200, tile_size=1024,
    analysis_ds=4, max_preview_dim=1024,
)
print(f"白底图: 阳性面积 {result_white['positive_pct']:.1f}%, 热点 {len(result_white['hotspots'])}")
# 热点应集中在阳性区域附近，不应出现在纯白色区域
if result_white["hotspots"]:
    for i, (x, y, w, h, d) in enumerate(result_white["hotspots"]):
        cx, cy = x + w // 2, y + h // 2
        print(f"  #{i+1}: center=({cx},{cy})")
        # 热点中心应在 DAB 区域附近 (400-800) * downsample=4
        # 即 level-0 坐标 (1600-3200) 附近
        assert cx < 5000, f"热点 #{i+1} x={cx} 远离组织区域"
        assert cy < 5000, f"热点 #{i+1} y={cy} 远离组织区域"

# ─── 测试 11: 热点 ROI 自适应组织区域尺寸 ───
print("\n--- 热点适配组织区域测试 ---")

# 创建合成组织 mask（缩略图尺寸 200x300）
# 一个大的连通区域: 缩略图 (30,30)-(150,180)，对应 level-0 (300,300)-(1500,1800)
tissue_mask = np.zeros((200, 300), dtype=np.uint8)
tissue_mask[30:150, 30:180] = 255  # 大组织区域

# 固定尺寸热点（在组织区域内）
hotspots_fixed = [
    (600, 600, 200, 200, 0.8),   # 中心在组织区域内
    (1000, 1000, 200, 200, 0.5), # 中心在组织区域内
]

# thumb_to_level0 = 10 (缩略图 200x300 → level-0 2000x3000)
fitted = _fit_hotspots_to_tissue(
    hotspots_fixed, tissue_mask, thumb_to_level0=10.0,
    level0_w=3000, level0_h=2000,
)

print(f"固定尺寸热点: {len(hotspots_fixed)} 个")
for i, (x, y, w, h, d) in enumerate(hotspots_fixed):
    print(f"  #{i+1}: ({x},{y}) {w}x{h} density={d:.1%}")

print(f"适配组织后: {len(fitted)} 个")
for i, (x, y, w, h, d) in enumerate(fitted):
    print(f"  #{i+1}: ({x},{y}) {w}x{h} density={d:.1%}")
    # ROI 应覆盖整个组织区域 (300,300)-(1500,1800)
    assert x <= 350, f"ROI x={x} 应接近组织区域左边界 300"
    assert y <= 350, f"ROI y={y} 应接近组织区域上边界 300"
    assert w >= 1100, f"ROI w={w} 应接近组织区域宽度 ~1200"
    assert h >= 1100, f"ROI h={h} 应接近组织区域高度 ~1200"

# 不在组织区域内的热点应保持原始尺寸
hotspots_outside = [(2500, 1800, 200, 200, 0.3)]
fitted_outside = _fit_hotspots_to_tissue(
    hotspots_outside, tissue_mask, thumb_to_level0=10.0,
    level0_w=3000, level0_h=2000,
)
x_o, y_o, w_o, h_o, d_o = fitted_outside[0]
assert w_o == 200 and h_o == 200, f"组织外热点应保持原始尺寸: ({w_o},{h_o})"
print(f"组织外热点保持原始尺寸: ({w_o},{h_o}) OK")

print("\n--- 组织覆盖率过滤测试 ---")

# 12a: 构造组织 mask，部分热点在组织上，部分不在
mask_l0 = np.zeros((2000, 3000), dtype=np.uint8)
# 左侧大块组织
mask_l0[200:1800, 100:1400] = 1
# 右上小块组织
mask_l0[100:500, 2000:2600] = 1

# 热点1: 在左侧组织上 (覆盖率高)
# 热点2: 在右上组织上 (覆盖率高)
# 热点3: 在空白区域 (覆盖率=0)
# 热点4: 部分在组织上 (覆盖率约 25%，不够)
hotspots_cov = [
    (200, 300, 400, 400, 0.9),    # 完全在左侧组织内
    (2100, 150, 400, 300, 0.8),   # 完全在右上组织内
    (1500, 1000, 400, 400, 0.7),  # 空白区域
    (1300, 300, 400, 400, 0.6),   # 组织边缘，约25%覆盖率
]

filtered_cov = _filter_hotspots_by_tissue_coverage(hotspots_cov, mask_l0, min_tissue_pct=0.5)
print(f"覆盖率过滤: {len(hotspots_cov)} -> {len(filtered_cov)} 个热点")
assert len(filtered_cov) == 2, f"应保留 2 个高覆盖率热点，实际 {len(filtered_cov)}"
assert filtered_cov[0][4] == 0.9
assert filtered_cov[1][4] == 0.8

# 12b: 空列表
filtered_empty = _filter_hotspots_by_tissue_coverage([], mask_l0, min_tissue_pct=0.5)
assert filtered_empty == []

# 12c: tissue_mask_l0 = None 时不过滤
filtered_none = _filter_hotspots_by_tissue_coverage(hotspots_cov, None, min_tissue_pct=0.5)
assert len(filtered_none) == 4, "mask=None 时不应过滤"

# 12d: 检测流程集成 — 验证 tissue_mask_l0 在结果中
assert "tissue_mask_l0" in result_auto, "结果中应包含 tissue_mask_l0"
print(f"tissue_mask_l0 类型: {type(result_auto['tissue_mask_l0'])}")

print("组织覆盖率过滤: OK")

print("\n=== 全部测试通过 ===")
