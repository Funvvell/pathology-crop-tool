"""标签图 OCR 批量重命名：设置 + 预览确认对话框。"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from liver_portal_crop.label_ocr_rename import (
    DEFAULT_PATTERN,
    OcrUnavailableError,
    RenameItem,
    RenameSummary,
    build_rename_plan,
    collect_label_text,
    execute_rename_plan,
    ocr_engine_available,
)

logger = logging.getLogger(__name__)

STATUS_TEXT = {
    "pending": "等待",
    "ready": "可重命名",
    "skip": "跳过",
    "error": "错误",
}


class LabelOcrRenameDialog(QDialog):
    """配置正则 → OCR → 预览表 → 确认后执行重命名计划。"""

    def __init__(
        self,
        readers: Mapping[Path, object],
        paths: Sequence[Path],
        parent=None,
        *,
        collect_fn: Callable[..., dict[Path, str]] | None = None,
        progress_cb: Callable[[int, int, Path], None] | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("按标签图 OCR 重命名")
        self.resize(860, 480)
        self._readers = dict(readers)
        self._paths = list(paths)
        self._collect_fn = collect_fn
        self._progress_cb = progress_cb
        self._items: list[RenameItem] = []
        self._summary: RenameSummary | None = None
        self._build_ui()

    # ── UI ──────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        self._hint = QLabel(
            f"目标文件：{len(self._paths)} 个。"
            "识别标签图文字后按正则提取新文件名，应用前可预览。"
        )
        self._hint.setWordWrap(True)
        layout.addWidget(self._hint)

        row = QHBoxLayout()
        row.addWidget(QLabel("正则（组 1 为文件名；留空用全文）:"))
        self._pattern_edit = QLineEdit(DEFAULT_PATTERN)
        self._pattern_edit.setPlaceholderText(DEFAULT_PATTERN)
        self._pattern_edit.setClearButtonEnabled(True)
        row.addWidget(self._pattern_edit, 1)
        self._ocr_btn = QPushButton("开始识别")
        self._ocr_btn.clicked.connect(self._on_run_ocr)
        row.addWidget(self._ocr_btn)
        layout.addLayout(row)

        self._status = QLabel("尚未识别")
        layout.addWidget(self._status)

        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            ["原文件名", "识别摘要", "提取结果", "新文件名", "状态 / 错误"]
        )
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self._table, 1)

        btn_row = QHBoxLayout()
        self._apply_btn = QPushButton("应用重命名")
        self._apply_btn.setEnabled(False)
        self._apply_btn.clicked.connect(self._on_apply)
        self._close_btn = QPushButton("关闭")
        self._close_btn.clicked.connect(self.accept)
        btn_row.addWidget(self._apply_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self._close_btn)
        layout.addLayout(btn_row)

    # ── OCR ─────────────────────────────────────────────

    def _on_run_ocr(self) -> None:
        if not ocr_engine_available() and self._collect_fn is None:
            QMessageBox.information(
                self,
                "缺少 OCR 依赖",
                "未检测到 rapidocr / onnxruntime。\n\n"
                "请安装：\n"
                '  pip install rapidocr onnxruntime\n'
                '或\n'
                '  pip install -e ".[ocr]"',
            )
            return

        pattern = self._pattern_edit.text()
        if pattern.strip():
            try:
                re.compile(pattern.strip())
            except re.error as e:
                QMessageBox.warning(self, "无效正则", str(e))
                return

        self._ocr_btn.setEnabled(False)
        self._status.setText("正在读取标签图并识别…")
        try:
            if self._collect_fn is not None:
                ocr_map = self._collect_fn(
                    self._readers, self._paths, progress_cb=self._progress_cb
                )
            else:
                ocr_map = collect_label_text(
                    self._readers, self._paths, progress_cb=self._progress_cb
                )
        except OcrUnavailableError as e:
            QMessageBox.information(self, "缺少 OCR 依赖", str(e))
            self._status.setText("识别未完成")
            return
        except Exception as e:
            logger.exception("OCR 收集失败")
            QMessageBox.warning(self, "识别失败", str(e))
            self._status.setText("识别失败")
            return
        finally:
            self._ocr_btn.setEnabled(True)

        self._items = build_rename_plan(self._paths, ocr_map, pattern)
        self._fill_table()
        ready = sum(1 for i in self._items if i.status == "ready")
        self._status.setText(
            f"识别完成：{len(ocr_map)} 有文本 · {ready} 项可重命名"
        )
        self._apply_btn.setEnabled(ready > 0)

    def _fill_table(self) -> None:
        self._table.setRowCount(len(self._items))
        for row, it in enumerate(self._items):
            summary = (it.ocr_text or "").replace("\n", " ").strip()
            if len(summary) > 80:
                summary = summary[:80] + "…"
            status = STATUS_TEXT.get(it.status, it.status)
            if it.error:
                status = f"{status}: {it.error}"

            check = QTableWidgetItem()
            check.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            check.setCheckState(
                Qt.CheckState.Checked
                if it.status == "ready"
                else Qt.CheckState.Unchecked
            )
            if it.status != "ready":
                check.setFlags(Qt.ItemFlag.ItemIsEnabled)
            check.setText(it.path.name)

            self._table.setItem(row, 0, check)
            self._table.setItem(row, 1, QTableWidgetItem(summary))
            self._table.setItem(row, 2, QTableWidgetItem(it.extracted or ""))
            self._table.setItem(row, 3, QTableWidgetItem(it.new_name or ""))
            self._table.setItem(row, 4, QTableWidgetItem(status))

    # ── Apply ───────────────────────────────────────────

    def _on_apply(self) -> None:
        selected: list[RenameItem] = []
        for row, it in enumerate(self._items):
            if it.status != "ready":
                continue
            check = self._table.item(row, 0)
            if check is not None and check.checkState() == Qt.CheckState.Checked:
                selected.append(it)

        if not selected:
            QMessageBox.information(self, "重命名", "没有勾选任何可重命名项。")
            return

        names = "\n".join(
            f"{it.path.name}  →  {it.new_name}" for it in selected[:20]
        )
        more = "" if len(selected) <= 20 else f"\n… 另有 {len(selected) - 20} 项"
        reply = QMessageBox.question(
            self,
            "确认重命名",
            f"将重命名 {len(selected)} 个文件：\n\n{names}{more}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._summary = execute_rename_plan(selected)
        self._fill_table()
        ok = self._summary.ok
        self._status.setText(
            f"重命名完成：成功 {ok} / 跳过 {self._summary.skipped} "
            f"/ 失败 {self._summary.failed}"
        )
        ready_left = sum(1 for i in self._items if i.status == "ready")
        self._apply_btn.setEnabled(ready_left > 0)

        if self._summary.errors:
            QMessageBox.warning(
                self,
                "部分失败",
                "\n".join(self._summary.errors[:15]),
            )
        elif ok:
            QMessageBox.information(
                self, "完成", f"已成功重命名 {ok} 个文件。"
            )

    def summary(self) -> RenameSummary | None:
        return self._summary

    def items(self) -> list[RenameItem]:
        return self._items
