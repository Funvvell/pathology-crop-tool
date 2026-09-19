"""导出队列对话框：多文件任务状态、进度、取消与重试。"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from liver_portal_crop.export_queue import (
    ExportQueueController,
    ExportQueueJob,
    JobStatus,
    QueueSummary,
)

logger = logging.getLogger(__name__)

STATUS_TEXT = {
    JobStatus.PENDING: "等待中",
    JobStatus.RUNNING: "进行中",
    JobStatus.SUCCESS: "成功",
    JobStatus.FAILED: "失败",
    JobStatus.CANCELLED: "已取消",
}


class _QueueWorker(QThread):
    finished_summary = Signal(object)
    failed = Signal(str)

    def __init__(self, controller: ExportQueueController, parent=None):
        super().__init__(parent)
        self._controller = controller

    def run(self) -> None:
        try:
            summary = self._controller.start()
            self.finished_summary.emit(summary)
        except Exception as e:
            logger.exception("导出队列线程异常")
            self.failed.emit(str(e))


class ExportQueueDialog(QDialog):
    """展示并驱动 ExportQueueController。"""

    def __init__(self, controller: ExportQueueController, parent=None):
        super().__init__(parent)
        self._controller = controller
        self._worker: _QueueWorker | None = None
        self.setWindowTitle("导出队列")
        self.resize(720, 420)
        self._build_ui()
        self._controller.job_updated.connect(self._on_job_updated)
        self._controller.job_progress.connect(self._on_job_progress)
        self._controller.queue_finished.connect(self._on_queue_finished)
        self.refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        self._summary_label = QLabel("尚未入队")
        layout.addWidget(self._summary_label)

        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(["文件", "ROI 数", "状态", "进度", "错误"])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self._table, 1)

        btn_row = QHBoxLayout()
        self._start_btn = QPushButton("开始")
        self._cancel_btn = QPushButton("取消")
        self._retry_btn = QPushButton("重试失败项")
        self._open_dir_btn = QPushButton("打开输出目录")
        self._close_btn = QPushButton("关闭")
        self._cancel_btn.setEnabled(False)
        self._start_btn.clicked.connect(self._on_start)
        self._cancel_btn.clicked.connect(self._on_cancel)
        self._retry_btn.clicked.connect(self._on_retry)
        self._open_dir_btn.clicked.connect(self._on_open_output)
        self._close_btn.clicked.connect(self.close)
        for b in (
            self._start_btn,
            self._cancel_btn,
            self._retry_btn,
            self._open_dir_btn,
            self._close_btn,
        ):
            btn_row.addWidget(b)
        layout.addLayout(btn_row)

    def refresh(self) -> None:
        jobs = self._controller.jobs()
        self._table.setRowCount(len(jobs))
        for row, job in enumerate(jobs):
            self._fill_row(row, job)
        pending = sum(1 for j in jobs if j.status == JobStatus.PENDING)
        failed = sum(1 for j in jobs if j.status == JobStatus.FAILED)
        ok = sum(1 for j in jobs if j.status == JobStatus.SUCCESS)
        self._summary_label.setText(
            f"任务 {len(jobs)} | 成功 {ok} | 失败 {failed} | 等待 {pending}"
            + (f" | {self._controller.last_summary.output_dir}" if self._controller.last_summary else "")
        )
        self._start_btn.setEnabled(not self._controller.is_running and pending > 0)
        self._cancel_btn.setEnabled(self._controller.is_running)
        self._retry_btn.setEnabled(not self._controller.is_running and failed > 0)

    def _fill_row(self, row: int, job: ExportQueueJob) -> None:
        items = [
            job.label,
            str(job.total),
            STATUS_TEXT.get(job.status, job.status.value),
            f"{job.done}/{job.total}",
            job.error,
        ]
        for col, text in enumerate(items):
            item = self._table.item(row, col)
            if item is None:
                item = QTableWidgetItem(text)
                self._table.setItem(row, col, item)
            else:
                item.setText(text)

    def _row_of(self, job_id: str) -> int:
        for i, job in enumerate(self._controller.jobs()):
            if job.id == job_id:
                return i
        return -1

    def _on_job_updated(self, job_id: str) -> None:
        row = self._row_of(job_id)
        if row < 0:
            self.refresh()
            return
        job = self._controller.get_job(job_id)
        if job:
            self._fill_row(row, job)
        self.refresh()

    def _on_job_progress(self, job_id: str, done: int, total: int) -> None:
        row = self._row_of(job_id)
        if row < 0:
            return
        item = self._table.item(row, 3)
        if item:
            item.setText(f"{done}/{total}")

    def _on_start(self) -> None:
        if self._controller.is_running:
            return
        if not any(j.status == JobStatus.PENDING for j in self._controller.jobs()):
            QMessageBox.information(self, "提示", "没有等待中的任务")
            return
        self._start_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        self._retry_btn.setEnabled(False)
        self._worker = _QueueWorker(self._controller, self)
        self._worker.failed.connect(self._on_worker_failed)
        self._worker.finished_summary.connect(self._on_worker_summary)
        self._worker.start()

    def _on_cancel(self) -> None:
        self._controller.cancel()
        self._cancel_btn.setEnabled(False)

    def _on_retry(self) -> None:
        try:
            reset = self._controller.retry_failed()
        except RuntimeError as e:
            QMessageBox.warning(self, "无法重试", str(e))
            return
        self.refresh()
        if reset:
            self._on_start()

    def _on_open_output(self) -> None:
        summary = self._controller.last_summary
        out = Path(summary.output_dir) if summary and summary.output_dir else None
        if out is None:
            jobs = self._controller.jobs()
            out = jobs[0].output_dir if jobs else None
        if out and out.exists():
            import os
            import subprocess
            subprocess.Popen(["explorer", str(out)])
        else:
            QMessageBox.information(self, "提示", "输出目录尚不存在")

    def _on_worker_failed(self, message: str) -> None:
        self._start_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        QMessageBox.warning(self, "导出队列失败", message)

    def _on_worker_summary(self, summary: object) -> None:
        if not isinstance(summary, QueueSummary):
            self.refresh()
            return
        self.refresh()
        QMessageBox.information(
            self,
            "导出队列完成",
            (
                f"成功文件: {summary.ok_jobs}\n"
                f"失败文件: {summary.failed_jobs}\n"
                f"取消文件: {summary.cancelled_jobs}\n"
                f"成功 ROI: {summary.ok_rois}\n"
                f"失败 ROI: {summary.failed_rois}\n"
                f"输出目录: {summary.output_dir}"
            ),
        )

    def closeEvent(self, event) -> None:
        if self._controller.is_running:
            self._controller.cancel()
            if self._worker is not None:
                self._worker.wait(3000)
        super().closeEvent(event)
