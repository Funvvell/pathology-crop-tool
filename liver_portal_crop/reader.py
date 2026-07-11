"""SDPCReader — 直接通过 ctypes 调用 DecodeSdpcDll.dll 读取 WSI 金字塔。"""

from __future__ import annotations

import io
import logging
import os
import struct
import sys
import threading
from pathlib import Path

logger = logging.getLogger(__name__)
from typing import NamedTuple

import numpy as np
from ctypes import *
from PIL import Image as PILImage

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

# 增强错误处理：DLL 加载失败时提供清晰的指导信息
_dll_path = _DLL_DIR / "DecodeSdpcDll.dll"
try:
    _so = cdll.LoadLibrary(str(_dll_path))
except OSError as e:
    raise RuntimeError(
        f"无法加载 SDPC DLL: {_dll_path}\n"
        f"错误信息: {e}\n\n"
        "请确保:\n"
        "1. 已安装 sdpc-for-python 包: pip install sdpc-for-python\n"
        f"2. DLL 目录存在: {_DLL_DIR}\n"
        "3. 系统已安装 Visual C++ Redistributable"
    ) from e

# DLL 全局锁 — DecodeSdpcDll.dll 不是线程安全的（有全局状态），
# 所有跨实例的 DLL 调用必须串行化
_dll_lock = threading.Lock()

# 恢复原始 CWD（sdpc 的 Sdpc.py 会 chdir 到 DLL 目录）
os.chdir(_old_cwd)

# ── DLL 函数签名 ────────────────────────────────────────────────────

# SqSdpcInfo 结构体引用
from sdpc.Sdpc_struct import (  # type: ignore
    SqSdpcInfo, SqPicHead, SqPersonInfo, SqExtraInfo,
)


# ── MacrographInfo 结构体（sdpc 包未导出，自行定义） ─────────────────
# 参照 ImageViewer 反编译的 Slide.Sdpc.MacrographInfo
class _SqMacrographInfo(Structure):
    _pack_ = 1
    _fields_ = [
        ("flag", c_ushort),
        ("rgb", c_uint64),
        ("width", c_uint32),
        ("height", c_uint32),
        ("chance", c_uint32),
        ("step", c_uint32),
        ("rgbSize", c_uint64),
        ("encodeSize", c_uint64),
        ("quality", c_ubyte),
        ("nextLayerOffset", c_uint64),
        ("headSpace_1", c_uint32),
        ("headSpace_2", c_uint32),
        ("headSpace", c_ubyte * 64),
    ]

# ── SDPC 文件格式结构体大小（C# DLL 实际写入的字节数） ──────────────
# Python ctypes 的 SqPicHead 缺少 vender(1B) + 2B headSpace，
# sizeof(SqPicHead)=153 但文件实际写入 160 字节。
# 必须使用这些常量计算文件偏移，而非 ctypes sizeof。
_PIC_HEAD_FILE_SIZE = 160        # C# Marshal.SizeOf(typeof(PicHead))
_PERSON_INFO_FILE_SIZE = 6808    # C# Marshal.SizeOf(typeof(PersonInfo))
_MACRO_INFO_FILE_SIZE = sizeof(_SqMacrographInfo)  # 121B, 与 C# 一致

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
        with _dll_lock:
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
            logger.debug("无法读取 ruler (mpp)", exc_info=True)
            self._mpp = None
        self._full_w, self._full_h = self._dims[0]
        self._thumbnail: np.ndarray | None = None
        self._thumbnail_size: tuple[int, int] | None = None
        self._macrographs: list[np.ndarray] | None = None  # 标签图/宏观图缓存

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

    # ── 元数据读取 ─────────────────────────────────────────────────

    @property
    def metadata(self) -> dict:
        """返回 SDPC 文件的全部元数据（PicHead / PersonInfo / ExtraInfo）。"""
        if self._handle is None:
            return {}

        head = self._handle.contents.picHead.contents
        info: dict = {
            "pic_head": {
                "version": bytes(head.version).split(b"\x00")[0].decode("ascii", errors="replace"),
                "file_size": head.fileSize,
                "src_width": head.srcWidth,
                "src_height": head.srcHeight,
                "slice_width": head.sliceWidth,
                "slice_height": head.sliceHeight,
                "hierarchy": head.hierarchy,
                "scale": head.scale,
                "ruler": head.ruler,
                "rate": head.rate,
                "quality": head.quality,
                "slice_format": head.sliceFormat,
                "person_infor": head.personInfor,
                "macrograph": head.macrograph,
            },
            "person_info": None,
            "extra_info": None,
        }

        # PersonInfo（仅当 personInfor == 1）
        if head.personInfor == 1 and self._handle.contents.personInfo:
            try:
                pi = self._handle.contents.personInfo.contents
                info["person_info"] = {
                    "pathology_id": _decode_ub(pi.pathologyID),
                    "name": _decode_ub(pi.name),
                    "sex": pi.sex,
                    "age": pi.age,
                    "departments": _decode_ub(pi.departments),
                    "hospital": _decode_ub(pi.hospital),
                    "submitted_samples": _decode_ub(pi.submittedSamples),
                    "clinical_diagnosis": _decode_ub(pi.clinicalDiagnosis),
                    "pathological_diagnosis": _decode_ub(pi.pathologicalDiagnosis),
                    "report_date": _decode_ub(pi.reportDate),
                    "attending_doctor": _decode_ub(pi.attendingDoctor),
                    "remark": _decode_ub(pi.remark),
                }
            except Exception:
                logger.debug("PersonInfo 解析失败", exc_info=True)

        # ExtraInfo（仅当 extraOffset != 0）
        if head.extraOffset != 0 and self._handle.contents.extra:
            try:
                ex = self._handle.contents.extra.contents
                info["extra_info"] = {
                    "model": _decode_ub(ex.model),
                    "serial": _decode_ub(ex.serial),
                    "barcode": _decode_ub(ex.barCode),
                    "fusion_layer": ex.fusionLayer,
                    "step": ex.step,
                    "scan_time": ex.scanTime,
                    "step_time": [ex.stepTime[i] for i in range(10)],
                    "camera_gamma": ex.cameraGamma,
                    "camera_exposure": ex.cameraExposure,
                    "camera_gain": ex.cameraExposure,
                }
            except Exception:
                logger.debug("ExtraInfo 解析失败", exc_info=True)

        return info

    # ── 标签图 / 宏观图（内嵌 macrograph） ────────────────────────

    @property
    def label_image(self) -> np.ndarray | None:
        """标签图（macrograph[0]），RGB numpy array (H, W, 3)。无则返回 None。"""
        imgs = self._read_macrographs()
        return imgs[0] if imgs and len(imgs) > 0 else None

    @property
    def macro_image(self) -> np.ndarray | None:
        """宏观图（macrograph[1]），RGB numpy array (H, W, 3)。无则返回 None。"""
        imgs = self._read_macrographs()
        return imgs[1] if imgs and len(imgs) > 1 else None

    def _read_macrographs(self) -> list[np.ndarray] | None:
        """从 DLL handle 的 macrograph 指针读取内嵌标签图/宏观图。

        SqImageInfo 内存布局（pack=1, 64-bit）：
          offset  0: stream    (char*, 8B) — JPEG 编码数据指针
          offset  8: bgr       (char*, 8B) — 解码后的 BGR 像素指针
          offset 16: width     (int,   4B)
          offset 20: height    (int,   4B)
          offset 24: channel   (int,   4B)
          offset 28: format    (ubyte, 1B)
          offset 29: colorSpace(ubyte[4], 4B)
          offset 33: streamSize(int,   4B) — JPEG 数据大小

        由于 sdpc 包的 SqImageInfo 有 _fileds_ 拼写错误，无法通过 ctypes
        访问字段名，因此直接用 string_at 按偏移读取原始内存。
        """
        if self._macrographs is not None:
            return self._macrographs

        head = self._handle.contents.picHead.contents
        macro_count = head.macrograph
        if macro_count <= 0:
            self._macrographs = []
            return self._macrographs

        images: list[np.ndarray] = []

        try:
            macro_arr = self._handle.contents.macrograph  # POINTER(POINTER(SqImageInfo))
            if not macro_arr:
                logger.debug("macrograph 指针为空，回退到文件读取")
                return self._try_read_macrographs_from_file()

            for i in range(macro_count):
                entry = macro_arr[i]  # POINTER(SqImageInfo)
                if not entry:
                    logger.debug("macrograph[%d] 为空指针", i)
                    continue

                # 获取 SqImageInfo 结构体在内存中的地址
                entry_addr = cast(entry, c_void_p).value
                if not entry_addr:
                    continue

                # 读取 37 字节原始内存
                buf = string_at(entry_addr, 37)

                # 按偏移解析各字段
                w = int.from_bytes(buf[16:20], "little", signed=True)
                h = int.from_bytes(buf[20:24], "little", signed=True)
                ch = int.from_bytes(buf[24:28], "little", signed=True)

                # 读取 bgr 指针值（offset 8, 8 bytes）
                bgr_ptr = int.from_bytes(buf[8:16], "little")
                # 读取 stream 指针值（offset 0, 8 bytes）
                stream_ptr = int.from_bytes(buf[0:8], "little")
                # 读取 streamSize（offset 33, 4 bytes）
                stream_size = int.from_bytes(buf[33:37], "little", signed=True)
                # 备用的 streamSize（offset 29，无 colorSpace 的情况）
                alt_stream_size = int.from_bytes(buf[29:33], "little", signed=True)

                logger.debug(
                    "macrograph[%d]: %dx%d ch=%d bgr=0x%x stream=0x%x "
                    "streamSize=%d altStreamSize=%d",
                    i, w, h, ch, bgr_ptr, stream_ptr, stream_size, alt_stream_size,
                )

                if w <= 0 or h <= 0 or w > 50000 or h > 50000:
                    logger.debug("macrograph[%d]: 维度异常，跳过", i)
                    continue

                img = None

                # 策略 1: 从 bgr 指针读取原始像素数据（DLL 已解码）
                if bgr_ptr and ch in (3, 4) and w * h * ch < 200_000_000:
                    try:
                        pixel_size = w * h * ch
                        pixel_data = string_at(bgr_ptr, pixel_size)
                        arr = np.frombuffer(pixel_data, dtype=np.uint8).reshape(h, w, ch)
                        if ch == 4:
                            # BGRA → RGB
                            img = arr[..., [2, 1, 0]].copy()
                        else:
                            # BGR → RGB
                            img = arr[..., ::-1].copy()
                        logger.debug("macrograph[%d]: 从 bgr 指针读取成功 (%dx%d)", i, w, h)
                    except Exception as e:
                        logger.debug("macrograph[%d]: bgr 读取失败: %s", i, e)
                        img = None

                # 策略 2: 从 stream 指针读取 JPEG 数据
                if img is None and stream_ptr and 0 < stream_size < 100_000_000:
                    try:
                        jpeg_data = string_at(stream_ptr, stream_size)
                        pil_img = PILImage.open(io.BytesIO(jpeg_data))
                        pil_img = pil_img.convert("RGB")
                        img = np.array(pil_img)
                        logger.debug("macrograph[%d]: 从 stream JPEG 读取成功 (size=%d)", i, stream_size)
                    except Exception as e:
                        logger.debug("macrograph[%d]: stream JPEG 读取失败 (size=%d): %s", i, stream_size, e)
                        img = None

                # 策略 3: streamSize 可能在 offset 29（无 colorSpace 的旧版 DLL）
                if img is None and stream_ptr and 0 < alt_stream_size < 100_000_000:
                    try:
                        jpeg_data = string_at(stream_ptr, alt_stream_size)
                        pil_img = PILImage.open(io.BytesIO(jpeg_data))
                        pil_img = pil_img.convert("RGB")
                        img = np.array(pil_img)
                        logger.debug("macrograph[%d]: 从 alt stream JPEG 读取成功 (size=%d)", i, alt_stream_size)
                    except Exception as e:
                        logger.debug("macrograph[%d]: alt stream JPEG 读取失败 (size=%d): %s", i, alt_stream_size, e)
                        img = None

                if img is not None:
                    images.append(img)

        except Exception:
            logger.debug("DLL handle macrograph 读取异常", exc_info=True)

        # 如果 DLL handle 方式失败，回退到二进制文件读取
        if not images:
            logger.debug("回退到文件二进制读取")
            images = self._try_read_macrographs_from_file()

        logger.debug("macrograph 读取完成: %d 张图像", len(images))
        self._macrographs = images
        return self._macrographs

    def _try_read_macrographs_from_file(self) -> list[np.ndarray]:
        """从 SDPC 二进制文件中读取 macrograph（回退方案）。

        文件布局（参照 ImageViewer SdpcImage.cs）：
          PicHead → PersonInfo → MacrographInfo[0] → Data[0]
                                       → MacrographInfo[1] → Data[1]
        """
        head = self._handle.contents.picHead.contents
        if head.personInfor != 1 or head.macrograph != 2:
            return []

        pic_head_offset = head.headSize
        if pic_head_offset <= 0:
            pic_head_offset = _PIC_HEAD_FILE_SIZE

        try:
            with open(self._path, "rb") as f:
                f.seek(pic_head_offset)
                pi_data = f.read(_PERSON_INFO_FILE_SIZE)
                if len(pi_data) < _PERSON_INFO_FILE_SIZE:
                    return []

                pi_flag = int.from_bytes(pi_data[0:2], "little")
                if pi_flag != 18768:
                    return []

                nex_offset_pos = 4536
                macro_base = int.from_bytes(
                    pi_data[nex_offset_pos:nex_offset_pos + 8], "little"
                )
                if macro_base <= 0:
                    return []

                images: list[np.ndarray] = []
                offset = macro_base

                for _ in range(head.macrograph):
                    f.seek(offset)
                    mi_data = f.read(_MACRO_INFO_FILE_SIZE)
                    if len(mi_data) < _MACRO_INFO_FILE_SIZE:
                        break
                    mi = _SqMacrographInfo()
                    memmove(addressof(mi), mi_data, _MACRO_INFO_FILE_SIZE)

                    if mi.flag != 18765:
                        break

                    data_offset = offset + _MACRO_INFO_FILE_SIZE
                    encode_size = mi.encodeSize
                    if encode_size <= 0 or encode_size > 100_000_000:
                        break

                    f.seek(data_offset)
                    jpeg_data = f.read(int(encode_size))
                    if len(jpeg_data) < encode_size:
                        break

                    try:
                        img = PILImage.open(io.BytesIO(jpeg_data))
                        img = img.convert("RGB")
                        images.append(np.array(img))
                    except Exception:
                        logger.warning("macrograph[%d]: JPEG 解码失败，使用占位图", len(images), exc_info=True)
                        images.append(np.zeros((1, 1, 3), dtype=np.uint8))

                    offset = mi.nextLayerOffset

                return images

        except Exception:
            logger.warning("标签图读取异常", exc_info=True)
            return []

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
            try:
                ret = _so.SqGetRoiRgbOfSpecifyLayer(
                    self._handle, rgb_ptr, w, h, lx, ly, level,
                )
                if ret != 0:
                    raise SDPCReadError(f"SqGetRoiRgbOfSpecifyLayer 返回 {ret}")
                arr = np.ctypeslib.as_array(rgb_pos, (h, w, 3)).copy()
                rgb = arr[..., ::-1].copy()
                return rgb
            finally:
                _so.Dispose(rgb_pos)

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
            try:
                ret = _so.SqGetRoiRgbOfSpecifyLayer(
                    self._handle, rgb_ptr, lw, lh, lx, ly, level,
                )
                if ret != 0:
                    raise SDPCReadError(f"SqGetRoiRgbOfSpecifyLayer 返回 {ret}")
                # BGR→RGB 反转 + 从 DLL 内存拷贝到独立 numpy 数组（一步完成）
                arr = np.ctypeslib.as_array(rgb_pos, (lh, lw, 3))
                rgb = np.ascontiguousarray(arr[..., ::-1])
                return rgb
            finally:
                _so.Dispose(rgb_pos)

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
                logger.debug("SqCloseSdpc 关闭时异常", exc_info=True)
            self._handle = None

    def __enter__(self) -> SDPCReader:
        return self

    def __exit__(self, *args) -> None:
        # 不在此处调用 close()（避免影响同一进程中的后续 open）
        pass


# ── 内部工具 ────────────────────────────────────────────────────────

def _decode_ub(arr) -> str:
    """将 c_ubyte 数组解码为字符串（GBK 优先，UTF-8 回退）。"""
    try:
        raw = bytes(arr)
    except Exception:
        logger.debug("_decode_ub: bytes() 转换失败", exc_info=True)
        return ""
    # 截断到第一个 null byte
    idx = raw.find(b"\x00")
    if idx >= 0:
        raw = raw[:idx]
    if not raw:
        return ""
    try:
        return raw.decode("gbk")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


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
