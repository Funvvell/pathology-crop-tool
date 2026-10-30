import re
from pathlib import Path

import numpy as np
import pytest

from liver_portal_crop.label_ocr_rename import (
    OcrError,
    apply_pattern_to_ocr,
    build_rename_plan,
    collect_label_text,
    execute_rename_plan,
    extract_with_pattern,
    normalize_ocr_text,
    sanitize_filename,
)


class TestSanitize:
    def test_replaces_illegal_chars(self):
        assert sanitize_filename('a<b>c:d"e/f\\g|h?i*j') == "a_b_c_d_e_f_g_h_i_j"

    def test_strips_trailing_dot_and_ws(self):
        assert sanitize_filename("  name.  ") == "name"

    def test_empty_becomes_untitled(self):
        assert sanitize_filename("   ") == "untitled"
        assert sanitize_filename("...") == "untitled"

    def test_max_len(self):
        s = sanitize_filename("x" * 300)
        assert len(s) == 180


class TestExtract:
    def test_group_capture(self):
        assert extract_with_pattern("病例号: A-123", r"病例号[：:\s]*(\S+)") == "A-123"

    def test_no_group_returns_match(self):
        assert extract_with_pattern("ID-99", r"ID-\d+") == "ID-99"

    def test_first_non_empty_group(self):
        assert extract_with_pattern("AB", r"(X)?(AB)") == "AB"

    def test_no_match(self):
        assert extract_with_pattern("hello", r"\d+") is None

    def test_invalid_pattern_raises(self):
        with pytest.raises(re.error):
            extract_with_pattern("x", r"(")


class TestApplyPattern:
    def test_normalizes_whitespace(self):
        text = "病例号\n\n  P2024-001"
        out = apply_pattern_to_ocr(text, r"病例号\s*(P\d{4}-\d+)")
        assert out == "P2024-001"

    def test_empty_pattern_uses_full_text(self):
        out = apply_pattern_to_ocr("Case  01", None)
        assert out == "Case 01"

    def test_no_match_returns_none(self):
        assert apply_pattern_to_ocr("nothing", r"\d{6}") is None

    def test_sanitize_after_extract(self):
        out = apply_pattern_to_ocr(r"ID: a/b", r"ID[：:\s]*(\S+)")
        assert out == "a_b"


class TestNormalize:
    def test_fold(self):
        assert normalize_ocr_text("a\n b\t c") == "a b c"


class TestPlan:
    def test_ready_when_pattern_matches(self, tmp_path: Path):
        p = tmp_path / "old.sdpc"
        p.write_bytes(b"x")
        items = build_rename_plan([p], {p: "病例号: P001"}, r"病例号[：:\s]*(\S+)")
        assert len(items) == 1
        assert items[0].status == "ready"
        assert items[0].new_name == "P001.sdpc"

    def test_skip_when_same_stem(self, tmp_path: Path):
        p = tmp_path / "P001.sdpc"
        p.write_bytes(b"x")
        items = build_rename_plan([p], {p: "P001"}, None)
        assert items[0].status == "skip"
        assert "相同" in items[0].error

    def test_skip_when_no_match(self, tmp_path: Path):
        p = tmp_path / "a.sdpc"
        p.write_bytes(b"x")
        items = build_rename_plan([p], {p: "no id here"}, r"\d{6,}")
        assert items[0].status == "skip"

    def test_error_when_missing_ocr(self, tmp_path: Path):
        p = tmp_path / "a.sdpc"
        p.write_bytes(b"x")
        items = build_rename_plan([p], {}, None)
        assert items[0].status == "error"
        assert "OCR" in items[0].error or "标签" in items[0].error

    def test_error_on_duplicate_targets(self, tmp_path: Path):
        a = tmp_path / "a.sdpc"
        b = tmp_path / "b.sdpc"
        a.write_bytes(b"1")
        b.write_bytes(b"2")
        ocr = {a: "病例号: SAME", b: "病例号: SAME"}
        items = build_rename_plan([a, b], ocr, r"病例号[：:\s]*(\S+)")
        statuses = sorted(i.status for i in items)
        assert statuses == ["error", "ready"]
        err = next(i for i in items if i.status == "error")
        assert "冲突" in err.error

    def test_error_when_target_exists_outside_batch(self, tmp_path: Path):
        src = tmp_path / "a.sdpc"
        occupied = tmp_path / "P001.sdpc"
        src.write_bytes(b"1")
        occupied.write_bytes(b"2")
        items = build_rename_plan(
            [src], {src: "P001"}, r"(\S+)"
        )
        # pattern P001 from full text
        items = build_rename_plan([src], {src: "病例号: P001"}, r"病例号[：:\s]*(\S+)")
        assert items[0].status == "error"
        assert "已存在" in items[0].error

    def test_swap_within_batch_allowed(self, tmp_path: Path):
        a = tmp_path / "a.sdpc"
        b = tmp_path / "b.sdpc"
        a.write_bytes(b"1")
        b.write_bytes(b"2")
        ocr = {a: "b", b: "a"}
        items = build_rename_plan([a, b], ocr, None)
        assert all(i.status == "ready" for i in items)
        assert items[0].new_name == "b.sdpc"
        assert items[1].new_name == "a.sdpc"

    def test_invalid_regex_marks_error(self, tmp_path: Path):
        p = tmp_path / "a.sdpc"
        p.write_bytes(b"x")
        items = build_rename_plan([p], {p: "x"}, r"(")
        assert items[0].status == "error"
        assert "正则" in items[0].error


class TestExecute:
    def test_successful_batch(self, tmp_path: Path):
        a = tmp_path / "a.sdpc"
        b = tmp_path / "b.sdpc"
        a.write_bytes(b"1")
        b.write_bytes(b"2")
        items = build_rename_plan(
            [a, b], {a: "病例号: P001", b: "病例号: P002"}, r"病例号[：:\s]*(\S+)"
        )
        summary = execute_rename_plan(items)
        assert summary.ok == 2
        assert (tmp_path / "P001.sdpc").exists()
        assert (tmp_path / "P002.sdpc").exists()
        assert not a.exists()
        assert not b.exists()

    def test_swap_atomic(self, tmp_path: Path):
        a = tmp_path / "a.sdpc"
        b = tmp_path / "b.sdpc"
        a.write_bytes(b"1")
        b.write_bytes(b"2")
        items = build_rename_plan([a, b], {a: "b", b: "a"}, None)
        summary = execute_rename_plan(items)
        assert summary.ok == 2
        assert (tmp_path / "b.sdpc").read_bytes() == b"1"
        assert (tmp_path / "a.sdpc").read_bytes() == b"2"
        leftovers = list(tmp_path.glob(".rename_tmp_*"))
        assert leftovers == []

    def test_missing_source_counts_error(self, tmp_path: Path):
        a = tmp_path / "a.sdpc"
        a.write_bytes(b"1")
        items = build_rename_plan([a], {a: "病例号: P001"}, r"病例号[：:\s]*(\S+)")
        assert items[0].status == "ready"
        a.unlink()
        summary = execute_rename_plan(items)
        assert summary.ok == 0
        assert summary.failed == 1
        assert items[0].status == "error"

    def test_skips_not_renamed(self, tmp_path: Path):
        a = tmp_path / "a.sdpc"
        a.write_bytes(b"1")
        items = build_rename_plan([a], {a: "nope"}, r"\d{9}")
        summary = execute_rename_plan(items)
        assert summary.ok == 0
        assert summary.skipped == 1
        assert a.exists()


class TestCollect:
    class _StubReader:
        def __init__(self, img):
            self.label_image = img

    def test_collects_ocr_text(self):
        path = Path("x.sdpc")
        readers = {path: self._StubReader(np.zeros((4, 4, 3), dtype=np.uint8))}
        calls = []

        def fake(img):
            calls.append(img.shape)
            return "病例号: P1"

        out = collect_label_text(readers, [path], ocr_fn=fake)
        assert out[path] == "病例号: P1"
        assert len(calls) == 1

    def test_skips_missing_label(self):
        path = Path("x.sdpc")
        readers = {path: self._StubReader(None)}
        out = collect_label_text(readers, [path], ocr_fn=lambda i: "t")
        assert path not in out

    def test_skips_unknown_path(self):
        path = Path("x.sdpc")
        out = collect_label_text({}, [path], ocr_fn=lambda i: "t")
        assert path not in out

    def test_progress_callback(self):
        path = Path("x.sdpc")
        readers = {path: self._StubReader(np.zeros((2, 2, 3), dtype=np.uint8))}
        seen = []
        collect_label_text(
            readers,
            [path],
            ocr_fn=lambda i: "t",
            progress_cb=lambda i, n, p: seen.append((i, n)),
        )
        assert seen[0] == (0, 1)
        assert seen[-1] == (1, 1)

    def test_ocr_fn_error_propagates_as_ocr_error(self):
        path = Path("x.sdpc")
        readers = {path: self._StubReader(np.zeros((2, 2, 3), dtype=np.uint8))}

        def boom(img):
            raise OcrError("bad")

        # OcrError 被 collect 捕获后跳过（不写入）
        out = collect_label_text(readers, [path], ocr_fn=boom)
        assert path not in out
