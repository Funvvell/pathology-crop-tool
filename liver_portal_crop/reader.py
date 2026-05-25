"""SDPCReader — 直接通过 ctypes 调用 DecodeSdpcDll.dll 读取 WSI 金字塔。"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import NamedTuple

import numpy as np
from ctypes import *

# ── 加载 DecodeSdpcDll.dll ──────────────────────────────────────────
# 注意：必须在 import sdpc 之前捕获 CWD，
# 因为 sdpc.Sdpc.py 的模块级代码会执行 os.chdir() 改变工作目录。

_old_cwd = os.getcwd()

import sdpc as _sdpc_pkg
_SDPC_PKG_DIR = Path(_sdpc_pkg.__file__).parent
_DLL_DIR = _SDPC_PKG_DIR / "WINDOWS" / "dll"

# 将 DLL 目录加入 PATH（Windows DLL 搜索路径）
os.environ["PATH"] = str(_DLL_DIR) + os.pathsep + os.environ.get("PATH", "")
os.chdir(str(_DLL_DIR))

_so = cdll.LoadLibrary(str(_DLL_DIR / "DecodeSdpcDll.dll"))

# DLL 全局锁 — DecodeSdpcDll.dll 不是线程安全的（有全局状态），
# 所有跨实例的 DLL 调用必须串行化
_dll_lock = threading.Lock()

# 恢复原始 CWD（sdpc 的 Sdpc.py 会 chdir 到 DLL 目录）
os.chdir(_old_cwd)

# ── DLL 函数签名 ────────────────────────────────────────────────────

# SqSdpcInfo 结构体引用
from sdpc.Sdpc_struct import SqSdpcInfo  # type: ignore

_so.SqOpenSdpc.restype = POINTER(SqSdpcInfo)
_so.SqOpenSdpc.argtypes = [c_char_p]

_so.SqCloseSdpc.restype = None
_so.SqCloseSdpc.argtypes = [POINTER(SqSdpcInfo)]

_so.GetLayerInfo.restype = POINTER(c_char)
_so.GetLayerInfo.argtypes = [POINTER(SqSdpcInfo), c_int]

_so.SqGetRoiRgbOfSpecifyLayer.restype = c_int
_so.SqGetRoiRgbOfSpecifyLayer.argtypes = [
    POINTER(SqSdpcInfo),
    POINTER(POINTER(c_uint8)),
    c_int, c_int, c_uint, c_uint, c_int,
]

_so.Dispose.restype = None
_so.Dispose.argtypes = [POINTER(c_uint8)]


# ── 公共类型 ────────────────────────────────────────────────────────

class LevelInfo(NamedTuple):
    level: int
    width: int
    height: int
    downsample: float


class SDPCReadError(Exception):
    """SDPC 文件读取错误。"""


# ── SDPCReader ──────────────────────────────────────────────────────

class SDPCReader:
    """通过 ctypes 直接调用 DecodeSdpcDll.dll 读取 SDPC 文件。

    提供金字塔层级信息、缩略图获取、任意区域提取功能。
    绕过了 sdpc.Sdpc 类中的多 handle 打开 bug。
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        if not self._path.exists():
            raise SDPCReadError(f"文件不存在: {self._path}")

        # 用 GBK 编码路径（Windows 本地编码，支持中文）
        path_bytes = str(self._path).encode("gbk")
        self._handle = _so.SqOpenSdpc(c_char_p(path_bytes))
        if not self._handle:
            raise SDPCReadError(f"无法打开 SDPC 文件: {self._path}")

        # 读取元数据
        self._level_count: int = self._handle.contents.picHead.contents.hierarchy
        scale: float = self._handle.contents.picHead.contents.scale
        self._downsample_rate: float = 1.0 / scale if scale != 0 else 1.0

        # 读取各级维度
        self._dims: list[tuple[int, int]] = []
        self._downsamples: list[float] = []
        for level in range(self._level_count):
            raw_info = _so.GetLayerInfo(self._handle, level)
            info_str = _parse_layer_info(raw_info)
            w, h = _extract_dimensions(info_str)
            self._dims.append((w, h))
            self._downsamples.append(self._downsample_rate ** level)

        try:
            self._mpp: float | None = float(self._handle.contents.picHead.contents.ruler)
        except Exception:
            self._mpp = None
        self._full_w, self._full_h = self._dims[0]
        self._thumbnail: np.ndarray | None = None
        self._thumbnail_size: tuple[int, int] | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def levels(self) -> list[LevelInfo]:
        return [
            LevelInfo(i, w, h, self._downsamples[i])
            for i, (w, h) in enumerate(self._dims)
        ]

    @property
    def full_width(self) -> int:
        return self._full_w

    @property
    def full_height(self) -> int:
        return self._full_h

    @property
    def mpp(self) -> float | None:
        """微米/像素，用于计算物理尺寸对应的像素数。"""
        return self._mpp

    @property
    def level_count(self) -> int:
        return self._level_count

    @property
    def thumbnail(self) -> np.ndarray:
        """最低分辨率全图 RGB (H, W, 3)。"""
        if self._thumbnail is None:
            thumb_level = self._level_count - 1
            tw, th = self._dims[thumb_level]
            self._thumbnail = self.extract_region(0, 0, tw, th, level=thumb_level)
        return self._thumbnail

    @property
    def thumbnail_size(self) -> tuple[int, int]:
        if self._thumbnail_size is None:
            h, w = self.thumbnail.shape[:2]
            self._thumbnail_size = (w, h)
        return self._thumbnail_size

    def extract_region(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        level: int = 0,
    ) -> np.ndarray:
        """提取指定区域。

        Args:
            x, y: level 0 坐标（左上角）
            w, h: 目标宽度和高度（pixels）
            level: 金字塔层级（0 = 最高分辨率）

        Returns:
            RGB numpy array (H, W, 3), dtype=uint8
        """
        # clamp to image bounds (level 0)
        x = max(0, min(x, self._full_w - 1))
        y = max(0, min(y, self._full_h - 1))
        w = min(w, self._full_w - x)
        h = min(h, self._full_h - y)

        if w <= 0 or h <= 0:
            raise SDPCReadError(f"无效区域: ({x}, {y}, {w}, {h})")

        # 将 level 0 坐标转为 target level 坐标
        scale = self._downsamples[level]
        lx = int(x / scale) if scale != 0 else 0
        ly = int(y / scale) if scale != 0 else 0

        with _dll_lock:
            rgb_pos = POINTER(c_uint8)()
            rgb_ptr = byref(rgb_pos)

            ret = _so.SqGetRoiRgbOfSpecifyLayer(
                self._handle, rgb_ptr, w, h, lx, ly, level,
            )
            if ret != 0:
                raise SDPCReadError(f"SqGetRoiRgbOfSpecifyLayer 返回 {ret}")

            arr = np.ctypeslib.as_array(rgb_pos, (h, w, 3)).copy()
            rgb = arr[..., ::-1].copy()
            _so.Dispose(rgb_pos)
            return rgb

    def _read_level_region(
        self, level: int, lx: int, ly: int, lw: int, lh: int,
    ) -> np.ndarray:
        """读取指定金字塔层级的矩形区域（坐标和尺寸均为该层级本地坐标）。

        Args:
            level: 金字塔层级
            lx, ly: 该层级内的左上角坐标
            lw, lh: 该层级内的宽高

        Returns:
            RGB numpy array (H, W, 3), dtype=uint8
        """
        if self._handle is None:
            raise SDPCReadError("文件已关闭")
        lw = min(lw, self._dims[level][0] - lx)
        lh = min(lh, self._dims[level][1] - ly)
        if lw <= 0 or lh <= 0:
            raise SDPCReadError(f"无效区域: lv{level} ({lx},{ly},{lw},{lh})")

        with _dll_lock:
            rgb_pos = POINTER(c_uint8)()
            rgb_ptr = byref(rgb_pos)
            ret = _so.SqGetRoiRgbOfSpecifyLayer(
                self._handle, rgb_ptr, lw, lh, lx, ly, level,
            )
            if ret != 0:
                raise SDPCReadError(f"SqGetRoiRgbOfSpecifyLayer 返回 {ret}")

            arr = np.ctypeslib.as_array(rgb_pos, (lh, lw, 3)).copy()
            rgb = arr[..., ::-1].copy()
            _so.Dispose(rgb_pos)
            return rgb

    def close(self) -> None:
        """关闭文件句柄。

        注意：DecodeSdpcDll.dll 存在已知限制——在同一个进程中
        关闭后再次调用 SqOpenSdpc 会导致访问冲突。
        因此：
        - 尽量保持文件打开，直到应用退出（OS 会自动清理）
        - 多次 open/close 是安全的，只要不再次 open
        - 不要在 close() 之后再次调用 open()
        """
        if self._handle is not None:
            try:
                _so.SqCloseSdpc(self._handle)
            except Exception:
                pass  # 忽略关闭时的异常
            self._handle = None

    def __enter__(self) -> SDPCReader:
        return self

    def __exit__(self, *args) -> None:
        # 不在此处调用 close()（避免影响同一进程中的后续 open）
        pass


# ── 内部工具 ────────────────────────────────────────────────────────

def _parse_layer_info(raw_ptr: POINTER(c_char)) -> str:
    """将 GetLayerInfo 返回的 c_char 指针解析为字符串。"""
    chars = []
    i = 0
    while True:
        c = raw_ptr[i]
        if c == b"\x00" or c == b"":
            break
        chars.append(c.decode("utf-8", errors="replace"))
        i += 1
    return "".join(chars)


def _extract_dimensions(info_str: str) -> tuple[int, int]:
    """从图层信息字符串中提取 (width, height)。

    格式示例: rawWidth=10000|rawHeight=8000|boundWidth=0|boundHeight=0
    """
    parts = info_str.split("|")
    if len(parts) < 4:
        raise SDPCReadError(f"无法解析图层信息: {info_str}")
    try:
        raw_w = int(parts[0].split("=")[1])
        raw_h = int(parts[1].split("=")[1])
        bound_w = int(parts[2].split("=")[1])
        bound_h = int(parts[3].split("=")[1])
    except (IndexError, ValueError) as e:
        raise SDPCReadError(f"解析维度失败: {info_str}") from e
    return (raw_w - bound_w, raw_h - bound_h)
