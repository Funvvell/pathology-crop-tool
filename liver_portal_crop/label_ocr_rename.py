"""标签图 OCR 文本提取 → 批量重命名计划（纯逻辑，可无 Qt/无模型测试）。"""
from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import numpy as np

if TYPE_CHECKING:
    from liver_portal_crop.reader import SDPCReader

INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

RenameStatus = Literal["pending", "ready", "skip", "error"]

DEFAULT_PATTERN = r"(?:病例号|病理号|ID)[：:\s]*([A-Za-z0-9\-]+)"


class OcrUnavailableError(RuntimeError):
    """RapidOCR / onnxruntime 未安装或无法导入。"""


class OcrError(RuntimeError):
    """单次 OCR 调用失败。"""


def sanitize_filename(name: str, *, max_len: int = 180) -> str:
    """去掉非法文件名字符与首尾空白/末尾点；空结果返回 untitled。"""
    cleaned = INVALID_FILENAME_CHARS.sub("_", name).strip()
    cleaned = cleaned.rstrip(".")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return "untitled"
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip(". ")
        if not cleaned:
            return "untitled"
    return cleaned


def normalize_ocr_text(text: str) -> str:
    """轻量归一：换行与多余空白折叠为单空格。"""
    return re.sub(r"\s+", " ", text).strip()


def extract_with_pattern(text: str, pattern: str) -> str | None:
    """在 text 上 re.search；有组取第一个非 None 组，否则整段 match。"""
    if not pattern:
        raise ValueError("pattern must be non-empty")
    m = re.search(pattern, text)
    if not m:
        return None
    if m.groups():
        for g in m.groups():
            if g is not None and g != "":
                return g
        return m.group(0)
    return m.group(0)


def apply_pattern_to_ocr(text: str, pattern: str | None) -> str | None:
    """归一化全文后按 pattern 提取并 sanitize；空 pattern 用全文。"""
    normalized = normalize_ocr_text(text)
    if not normalized:
        return None
    if not pattern or not pattern.strip():
        return sanitize_filename(normalized)
    matched = extract_with_pattern(normalized, pattern.strip())
    if matched is None:
        return None
    cleaned = sanitize_filename(matched)
    if cleaned == "untitled" and not matched.strip():
        return None
    return cleaned


@dataclass
class RenameItem:
    path: Path
    ocr_text: str = ""
    extracted: str | None = None
    new_name: str | None = None
    status: RenameStatus = "pending"
    error: str = ""


@dataclass
class RenameSummary:
    ok: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def _stem(name: str) -> str:
    return Path(name).stem


def build_rename_plan(
    paths: Sequence[Path],
    ocr_map: Mapping[Path, str],
    pattern: str | None,
) -> list[RenameItem]:
    """根据 OCR 映射与正则生成重命名计划；不触碰磁盘。"""
    items: list[RenameItem] = []
    ready_by_target: dict[str, Path] = {}

    for path in paths:
        if path not in ocr_map:
            items.append(
                RenameItem(
                    path=path,
                    status="error",
                    error="无标签图或 OCR 结果",
                )
            )
            continue

        text = ocr_map[path]
        item = RenameItem(path=path, ocr_text=text)
        try:
            extracted = apply_pattern_to_ocr(text, pattern)
        except re.error as e:
            item.status = "error"
            item.error = f"无效正则: {e}"
            items.append(item)
            continue

        if extracted is None:
            item.status = "skip"
            item.error = "正则未匹配"
            items.append(item)
            continue

        item.extracted = extracted
        new_name = f"{extracted}{path.suffix}"
        item.new_name = new_name

        if Path(new_name).stem == path.stem:
            item.status = "skip"
            item.error = "与原名相同"
            items.append(item)
            continue

        # 批内：两个不同源 → 同一目标名
        owner = ready_by_target.get(new_name)
        if owner is not None and owner != path:
            item.status = "error"
            item.error = "目标名冲突"
            items.append(item)
            continue

        # 批内交换：目标名等于另一源文件名 — 允许（execute 两阶段）
        batch_names = {p.name for p in paths}
        target = path.with_name(new_name)
        if target.exists() and new_name not in batch_names:
            item.status = "error"
            item.error = "目标文件已存在"
            items.append(item)
            continue

        item.status = "ready"
        items.append(item)
        ready_by_target[new_name] = path

    return items


def execute_rename_plan(items: Sequence[RenameItem]) -> RenameSummary:
    """两阶段执行 ready 项：统一临时名 → 最终名；单项失败不中断。"""
    summary = RenameSummary()
    ready = [it for it in items if it.status == "ready" and it.new_name]

    for it in items:
        if it.status == "error":
            summary.failed += 1
            summary.errors.append(f"{it.path.name}: {it.error}")
        elif it.status == "skip":
            summary.skipped += 1

    if not ready:
        return summary

    tmp_map: dict[Path, Path] = {}
    phase1_failed: set[Path] = set()

    for it in ready:
        old = it.path
        tmp = old.with_name(f".rename_tmp_{uuid.uuid4().hex[:8]}_{old.name}")
        try:
            if not old.exists():
                raise OSError(f"源文件不存在: {old}")
            old.rename(tmp)
            tmp_map[old] = tmp
        except OSError as e:
            phase1_failed.add(old)
            it.status = "error"
            it.error = str(e)
            # 修正 summary 中尚未计入的 phase1 失败（尚不在 error 列表）
            summary.failed += 1
            summary.errors.append(f"{old.name}: {e}")

    for it in ready:
        if it.path in phase1_failed:
            continue
        tmp = tmp_map[it.path]
        assert it.new_name is not None
        final = it.path.with_name(it.new_name)
        try:
            if final.exists():
                raise OSError(f"目标文件已存在: {final.name}")
            tmp.rename(final)
            summary.ok += 1
        except OSError as e:
            try:
                tmp.rename(it.path)
            except OSError:
                pass
            it.status = "error"
            it.error = str(e)
            summary.failed += 1
            summary.errors.append(f"{it.path.name}: {e}")

    return summary


# ── OCR 适配 ──────────────────────────────────────────────

_engine: Any = None
_engine_checked = False


def ocr_engine_available() -> bool:
    """rapidocr 可导入则为 True（结果缓存；失败后再成功调用可传 force）。"""
    global _engine, _engine_checked
    if _engine is not None:
        return True
    try:
        import rapidocr  # noqa: F401
        import onnxruntime  # noqa: F401

        return True
    except ImportError:
        return False


def get_engine() -> Any:
    global _engine, _engine_checked
    if _engine is not None:
        return _engine
    try:
        from rapidocr import RapidOCR
    except ImportError as e:
        raise OcrUnavailableError(
            "未安装 OCR 依赖。请执行: pip install rapidocr onnxruntime "
            "或 pip install -e \".[ocr]\""
        ) from e
    _engine = RapidOCR()
    return _engine


def reset_engine_for_tests() -> None:
    global _engine, _engine_checked
    _engine = None
    _engine_checked = False


def run_ocr_on_image(rgb: np.ndarray) -> str:
    """RGB ndarray → 多行识别文本。"""
    engine = get_engine()
    try:
        result = engine(rgb)
    except Exception as e:
        raise OcrError(f"OCR 调用失败: {e}") from e

    # rapidocr 3.x 返回结构可能是 Result 对象或旧版 (boxes, txts, scores)
    lines: list[str] = []
    texts = _extract_texts(result)
    lines.extend(texts)
    return "\n".join(lines)


def _extract_texts(result: Any) -> list[str]:
    if result is None:
        return []
    # 新版 Result：.txts 或 el
    for attr in ("txts", "texts"):
        if hasattr(result, attr):
            val = getattr(result, attr)
            if val:
                return [str(t) for t in val if t]
    # 旧版 tuple: (boxes, txts, scores)
    if isinstance(result, (tuple, list)) and len(result) >= 2:
        second = result[1]
        if isinstance(second, (tuple, list)):
            return [str(t) for t in second if t]
    # dict-like
    if isinstance(result, dict):
        for key in ("txts", "texts", "rec_texts"):
            if key in result and result[key]:
                return [str(t) for t in result[key] if t]
    # rapidocr 新版 may return list of dicts
    if isinstance(result, list) and result:
        if isinstance(result[0], dict):
            out = []
            for row in result:
                for key in ("text", "txt", "rec_text"):
                    if key in row:
                        out.append(str(row[key]))
                        break
            return out
        if all(isinstance(x, str) for x in result):
            return list(result)
    return []


def collect_label_text(
    readers: Mapping[Any, Any],
    paths: Sequence[Path],
    *,
    ocr_fn: Callable[[np.ndarray], str] | None = None,
    progress_cb: Callable[[int, int, Path], None] | None = None,
) -> dict[Path, str]:
    """读取各文件 label_image 并 OCR；无标签图的 path 不写入结果。"""
    fn = ocr_fn if ocr_fn is not None else run_ocr_on_image
    out: dict[Path, str] = {}
    total = len(paths)
    for i, path in enumerate(paths):
        if progress_cb is not None:
            progress_cb(i, total, path)
        reader = readers.get(path)
        if reader is None:
            continue
        img = getattr(reader, "label_image", None)
        if img is None:
            continue
        try:
            text = fn(img)
        except OcrUnavailableError:
            raise
        except OcrError:
            # 单文件失败：跳过，plan 会标 error/无结果
            continue
        except Exception as e:
            raise OcrError(f"{path.name}: {e}") from e
        out[path] = text
    if progress_cb is not None:
        progress_cb(total, total, paths[-1] if paths else Path("."))
    return out
