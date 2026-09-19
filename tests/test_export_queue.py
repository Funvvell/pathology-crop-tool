from pathlib import Path

from liver_portal_crop.export_queue import (
    ExportQueueController,
    ExportQueueJob,
    JobStatus,
)
from liver_portal_crop.exporter import CropConfig
from liver_portal_crop.roi import ROIManager, ROIModel


def setup_manager_with_rois(n_per_slide: dict[str, int]) -> ROIManager:
    manager = ROIManager()
    idx = 0
    for slide, n in n_per_slide.items():
        for i in range(n):
            manager.add_roi(ROIModel(
                slide_path=Path(slide),
                x=i * 10,
                y=0,
                w=20,
                h=20,
                id=f"{slide}-{i}",
            ))
            idx += 1
    return manager


def test_enqueue_groups_by_slide():
    manager = setup_manager_with_rois({"a.sdpc": 2, "b.sdpc": 1})
    qc = ExportQueueController()
    config = CropConfig(output_dir=Path("out"), crop_width=64, crop_height=64, mag_label="20x")
    jobs = qc.enqueue_from_manager(manager, config)
    assert len(jobs) == 2
    by_name = {j.label: j for j in jobs}
    assert by_name["a.sdpc"].total == 2
    assert by_name["b.sdpc"].total == 1
    assert by_name["a.sdpc"].crop_width == 64
    assert by_name["a.sdpc"].mag_label == "20x"


def test_enqueue_filter_slide_paths():
    manager = setup_manager_with_rois({"a.sdpc": 1, "b.sdpc": 1})
    qc = ExportQueueController()
    config = CropConfig(output_dir=Path("out"))
    jobs = qc.enqueue_from_manager(manager, config, slide_paths=[Path("b.sdpc")])
    assert len(jobs) == 1
    assert jobs[0].label == "b.sdpc"


def test_serial_run_success(tmp_path):
    manager = setup_manager_with_rois({"a.sdpc": 2, "b.sdpc": 3})
    qc = ExportQueueController()
    config = CropConfig(output_dir=tmp_path)
    qc.enqueue_from_manager(manager, config)

    seen: list[str] = []

    def export_fn(job, rois, mapping):
        seen.append(job.label)
        for r in rois:
            job.ok_outputs.append(str(tmp_path / f"{job.label}_{r.id}.tiff"))
        job.done = len(rois)

    qc.set_export_fn(export_fn)
    summary = qc.start()
    assert seen == ["a.sdpc", "b.sdpc"]
    assert summary.ok_jobs == 2
    assert summary.failed_jobs == 0
    assert all(j.status == JobStatus.SUCCESS for j in qc.jobs())


def test_one_failure_does_not_block_others(tmp_path):
    manager = setup_manager_with_rois({"bad.sdpc": 1, "good.sdpc": 2})
    qc = ExportQueueController()
    config = CropConfig(output_dir=tmp_path)
    # 保证 bad 先于 good（按文件名排序：bad < good）
    qc.enqueue_from_manager(manager, config)

    def export_fn(job, rois, mapping):
        if job.label == "bad.sdpc":
            raise RuntimeError("boom")
        job.ok_outputs.append("ok.tiff")
        job.done = len(rois)

    qc.set_export_fn(export_fn)
    summary = qc.start()
    jobs = {j.label: j for j in qc.jobs()}
    assert jobs["bad.sdpc"].status == JobStatus.FAILED
    assert "boom" in jobs["bad.sdpc"].error
    assert jobs["good.sdpc"].status == JobStatus.SUCCESS
    assert summary.failed_jobs == 1
    assert summary.ok_jobs == 1


def test_retry_failed_only_resets_failed(tmp_path):
    manager = setup_manager_with_rois({"bad.sdpc": 1, "good.sdpc": 1})
    qc = ExportQueueController()
    config = CropConfig(output_dir=tmp_path)
    qc.enqueue_from_manager(manager, config)

    def export_fn(job, rois, mapping):
        if job.label == "bad.sdpc":
            raise RuntimeError("boom")
        job.ok_outputs.append("ok.tiff")
        job.done = len(rois)

    qc.set_export_fn(export_fn)
    qc.start()
    jobs = {j.label: j for j in qc.jobs()}
    assert jobs["good.sdpc"].status == JobStatus.SUCCESS
    assert jobs["bad.sdpc"].status == JobStatus.FAILED

    reset = qc.retry_failed()
    assert len(reset) == 1
    assert reset[0].label == "bad.sdpc"
    assert jobs["bad.sdpc"].status == JobStatus.PENDING
    assert jobs["good.sdpc"].status == JobStatus.SUCCESS

    def export_fn2(job, rois, mapping):
        job.ok_outputs.append("ok2.tiff")
        job.done = len(rois)

    qc.set_export_fn(export_fn2)
    summary = qc.start()
    assert jobs["bad.sdpc"].status == JobStatus.SUCCESS
    assert summary.ok_jobs == 2


def test_cancel_marks_remaining_pending(tmp_path):
    manager = setup_manager_with_rois({"a.sdpc": 1, "b.sdpc": 1, "c.sdpc": 1})
    qc = ExportQueueController()
    config = CropConfig(output_dir=tmp_path)
    jobs = qc.enqueue_from_manager(manager, config)

    calls = {"n": 0}

    def export_fn(job, rois, mapping):
        calls["n"] += 1
        if calls["n"] == 1:
            job.ok_outputs.append("ok.tiff")
            job.done = len(rois)
            qc.cancel()
        else:
            raise AssertionError("should not run after cancel")

    qc.set_export_fn(export_fn)
    summary = qc.start()
    statuses = [j.status for j in jobs]
    assert statuses[0] == JobStatus.SUCCESS
    assert statuses[1] == JobStatus.CANCELLED
    assert statuses[2] == JobStatus.CANCELLED
    assert summary.cancelled_jobs == 2
    assert summary.ok_jobs == 1


def test_empty_roi_job_fails(tmp_path):
    manager = ROIManager()
    qc = ExportQueueController()
    config = CropConfig(output_dir=tmp_path)
    job = ExportQueueJob(
        slide_path=Path("empty.sdpc"),
        roi_ids=[],
        crop_width=16,
        crop_height=16,
        output_dir=tmp_path,
        total=0,
    )
    qc.jobs().append(job) if False else None
    # 直接塞入内部列表
    qc._jobs.append(job)
    qc.set_export_fn(lambda j, r, m: None)
    summary = qc.start()
    assert job.status == JobStatus.FAILED
    assert job.error == "no rois"
    assert summary.failed_jobs == 1


def test_partial_failure_marks_job_failed(tmp_path):
    manager = setup_manager_with_rois({"m.sdpc": 2})
    qc = ExportQueueController()
    config = CropConfig(output_dir=tmp_path)
    qc.enqueue_from_manager(manager, config)

    def export_fn(job, rois, mapping):
        job.ok_outputs.append("ok.tiff")
        job.fail_outputs.append("bad.tiff")
        job.error = "error:disk"
        job.done = 2

    qc.set_export_fn(export_fn)
    qc.start()
    job = qc.jobs()[0]
    assert job.status == JobStatus.FAILED
    assert "disk" in job.error or "rois failed" in job.error
