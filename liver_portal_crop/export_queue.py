"""GUI 内导出队列：按切片文件排队、串行导出、失败重试。"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from liver_portal_crop.exporter import BatchExporter, CropConfig
from liver_portal_crop.roi import ROIManager, ROIModel
from liver_portal_crop.utils import center_crop_rect

logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class ExportQueueJob:
    """单个切片文件的导出任务。"""

    slide_path: Path
    roi_ids: list[str]
    crop_width: int
    crop_height: int
    output_dir: Path
    mag_label: str = ""
    compression: str = "zlib"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: JobStatus = JobStatus.PENDING
    done: int = 0
    total: int = 0
    error: str = ""
    ok_outputs: list[str] = field(default_factory=list)
    fail_outputs: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.slide_path.name


@dataclass
class QueueSummary:
    ok_jobs: int = 0
    failed_jobs: int = 0
    cancelled_jobs: int = 0
    pending_jobs: int = 0
    ok_rois: int = 0
    failed_rois: int = 0
    output_dir: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok_jobs": self.ok_jobs,
            "failed_jobs": self.failed_jobs,
            "cancelled_jobs": self.cancelled_jobs,
            "pending_jobs": self.pending_jobs,
            "ok_rois": self.ok_rois,
            "failed_rois": self.failed_rois,
            "output_dir": self.output_dir,
        }


class ExportQueueController(QObject):
    """串行导出队列控制器。

    可注入 `export_fn` 以便单测；默认对每个 job 使用 BatchExporter.run。
    """

    job_updated = Signal(str)
    job_progress = Signal(str, int, int)
    queue_started = Signal()
    queue_finished = Signal(object)  # QueueSummary
    queue_cancelled = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._jobs: list[ExportQueueJob] = []
        self._roi_by_id: dict[str, ROIModel] = {}
        self._path_to_reader: dict[Path, Any] = {}
        self._cancel_event = threading.Event()
        self._running = False
        self._export_fn: Callable[[ExportQueueJob, list[ROIModel], dict], None] | None = None
        self._last_summary: QueueSummary | None = None

    # ── 入队 ────────────────────────────────────────────

    def enqueue_from_manager(
        self,
        roi_manager: ROIManager,
        crop_config: CropConfig,
        path_to_reader: dict[Path, Any] | None = None,
        crop_width: int | None = None,
        crop_height: int | None = None,
        slide_paths: list[Path] | None = None,
    ) -> list[ExportQueueJob]:
        """按切片文件将 ROI 入队。slide_paths 为 None 时对全部有 ROI 的文件入队。"""
        if self._running:
            raise RuntimeError("队列运行中，不能入队")

        self._path_to_reader = dict(path_to_reader or {})
        w = crop_width if crop_width is not None else crop_config.crop_width
        h = crop_height if crop_height is not None else crop_config.crop_height
        out_dir = Path(crop_config.output_dir)

        all_rois = roi_manager.all_rois()
        self._roi_by_id = {r.id: r for r in all_rois}

        by_slide: dict[Path, list[ROIModel]] = {}
        for roi in all_rois:
            if slide_paths is not None and roi.slide_path not in slide_paths:
                continue
            by_slide.setdefault(roi.slide_path, []).append(roi)

        created: list[ExportQueueJob] = []
        for path in sorted(by_slide.keys(), key=lambda p: p.name):
            rois = by_slide[path]
            # 稳定局部索引顺序与导出命名一致
            rois_sorted = sorted(rois, key=lambda r: (r.x, r.y, r.id))
            job = ExportQueueJob(
                slide_path=path,
                roi_ids=[r.id for r in rois_sorted],
                crop_width=w,
                crop_height=h,
                output_dir=out_dir,
                mag_label=crop_config.mag_label,
                compression=crop_config.compression,
                total=len(rois_sorted),
            )
            self._jobs.append(job)
            created.append(job)
            self.job_updated.emit(job.id)
        return created

    def clear_jobs(self) -> None:
        if self._running:
            raise RuntimeError("队列运行中，不能清空")
        self._jobs.clear()
        self._last_summary = None

    def jobs(self) -> list[ExportQueueJob]:
        return list(self._jobs)

    def get_job(self, job_id: str) -> ExportQueueJob | None:
        for job in self._jobs:
            if job.id == job_id:
                return job
        return None

    def set_export_fn(self, fn: Callable[[ExportQueueJob, list[ROIModel], dict], None] | None) -> None:
        """测试注入：fn(job, rois, path_to_reader) 应抛错表示失败或调用 progress 回调。"""
        self._export_fn = fn

    def set_path_to_reader(self, mapping: dict[Path, Any]) -> None:
        self._path_to_reader = dict(mapping)

    # ── 执行 ────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def last_summary(self) -> QueueSummary | None:
        return self._last_summary

    def cancel(self) -> None:
        self._cancel_event.set()

    def retry_failed(self) -> list[ExportQueueJob]:
        """仅将 failed 重置为 pending。返回被重置的 job 列表。"""
        if self._running:
            raise RuntimeError("队列运行中，不能重试")
        reset: list[ExportQueueJob] = []
        for job in self._jobs:
            if job.status == JobStatus.FAILED:
                job.status = JobStatus.PENDING
                job.done = 0
                job.error = ""
                job.ok_outputs.clear()
                job.fail_outputs.clear()
                reset.append(job)
                self.job_updated.emit(job.id)
        return reset

    def start(self) -> QueueSummary:
        """同步串行执行 pending 任务（UI 层应放到 QThread 中调用）。"""
        if self._running:
            raise RuntimeError("队列已在运行")
        self._cancel_event.clear()
        self._running = True
        self.queue_started.emit()
        logger.info("[导出队列] 开始，共 %d 个任务", len(self._jobs))
        try:
            for job in self._jobs:
                if self._cancel_event.is_set():
                    if job.status == JobStatus.PENDING:
                        job.status = JobStatus.CANCELLED
                        self.job_updated.emit(job.id)
                    continue
                if job.status != JobStatus.PENDING:
                    continue
                self._run_job(job)
            summary = self._build_summary()
            self._last_summary = summary
            if self._cancel_event.is_set():
                self.queue_cancelled.emit()
            self.queue_finished.emit(summary)
            logger.info("[导出队列] 结束: %s", summary.as_dict())
            return summary
        finally:
            self._running = False

    def _run_job(self, job: ExportQueueJob) -> None:
        job.status = JobStatus.RUNNING
        job.done = 0
        job.error = ""
        self.job_updated.emit(job.id)

        rois = [self._roi_by_id[rid] for rid in job.roi_ids if rid in self._roi_by_id]
        job.total = len(rois)
        if not rois:
            job.status = JobStatus.FAILED
            job.error = "no rois"
            self.job_updated.emit(job.id)
            return

        job.output_dir.mkdir(parents=True, exist_ok=True)

        def on_progress(current: int, total: int) -> None:
            job.done = current
            self.job_progress.emit(job.id, current, total)
            self.job_updated.emit(job.id)

        try:
            if self._export_fn is not None:
                self._export_fn(job, rois, self._path_to_reader)
            else:
                self._export_with_batch(job, rois, on_progress)
            # 任务若已完整跑完，即使随后取消也记为 success；剩余 pending 在 start 循环里标 cancelled
            if job.fail_outputs and not job.ok_outputs:
                job.status = JobStatus.FAILED
                job.error = job.error or "all outputs failed"
            elif job.fail_outputs:
                job.status = JobStatus.FAILED
                job.error = job.error or f"{len(job.fail_outputs)} rois failed"
            else:
                job.status = JobStatus.SUCCESS
                job.done = job.total
        except Exception as e:
            job.status = JobStatus.FAILED
            job.error = str(e)
            logger.warning("[导出队列] job 失败 %s: %s", job.label, e, exc_info=True)
        self.job_updated.emit(job.id)

    def _export_with_batch(self, job: ExportQueueJob, rois: list[ROIModel], on_progress) -> None:
        config = CropConfig(
            output_dir=job.output_dir,
            crop_width=job.crop_width,
            crop_height=job.crop_height,
            compression=job.compression,
            mag_label=job.mag_label,
        )
        exporter = BatchExporter(config)
        collected: list[tuple[str, str]] = []
        exporter.file_done.connect(lambda p, s: collected.append((p, s)))
        exporter.progress.connect(on_progress)

        path_input: dict[Path, Any] = {}
        for path in {r.slide_path for r in rois}:
            val = self._path_to_reader.get(path)
            if val is not None:
                path_input[path] = val

        if self._cancel_event.is_set():
            exporter.cancel()
        exporter.run(rois, path_input)

        for out_path, status in collected:
            if status == "ok":
                job.ok_outputs.append(out_path)
            else:
                job.fail_outputs.append(out_path)
                if not job.error:
                    job.error = status
        job.done = len(job.ok_outputs) + len(job.fail_outputs)

    def _build_summary(self) -> QueueSummary:
        s = QueueSummary()
        for job in self._jobs:
            if job.status == JobStatus.SUCCESS:
                s.ok_jobs += 1
                s.ok_rois += len(job.ok_outputs) or job.total
            elif job.status == JobStatus.FAILED:
                s.failed_jobs += 1
                s.failed_rois += len(job.fail_outputs) or max(job.total - job.done, 1)
                s.ok_rois += len(job.ok_outputs)
            elif job.status == JobStatus.CANCELLED:
                s.cancelled_jobs += 1
            elif job.status == JobStatus.PENDING:
                s.pending_jobs += 1
            if not s.output_dir and job.output_dir:
                s.output_dir = str(job.output_dir)
        return s


def make_default_export_fn(
    readers: dict[Path, Any],
) -> Callable[[ExportQueueJob, list[ROIModel], dict], None]:
    """工厂：使用调用时 readers 快照的默认导出（测试/脚本可选）。"""

    def _fn(job: ExportQueueJob, rois: list[ROIModel], path_to_reader: dict) -> None:
        merged = dict(path_to_reader)
        merged.update(readers)
        config = CropConfig(
            output_dir=job.output_dir,
            crop_width=job.crop_width,
            crop_height=job.crop_height,
            compression=job.compression,
            mag_label=job.mag_label,
        )
        exporter = BatchExporter(config)
        collected: list[tuple[str, str]] = []
        exporter.file_done.connect(lambda p, s: collected.append((p, s)))
        exporter.run(rois, merged)
        for p, s in collected:
            if s == "ok":
                job.ok_outputs.append(p)
            else:
                job.fail_outputs.append(p)
                job.error = s
        job.done = len(collected)

    return _fn


__all__ = [
    "JobStatus",
    "ExportQueueJob",
    "ExportQueueController",
    "QueueSummary",
    "make_default_export_fn",
    "center_crop_rect",
]
