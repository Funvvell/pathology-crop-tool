from datetime import datetime
from pathlib import Path

from liver_portal_crop.roi import ROIManager, ROIModel
from liver_portal_crop.roi_undo import (
    AddRoiCommand,
    BatchAddCommand,
    BatchRemoveCommand,
    ClearSlideCommand,
    RemoveRoiCommand,
    RoiUndoStack,
    UpdateRoiCommand,
)


def make_roi(slide: str = "a.sdpc", x: int = 0, y: int = 0, w: int = 10, h: int = 10, id_: str | None = None) -> ROIModel:
    kwargs = {
        "slide_path": Path(slide),
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "angle": 0.0,
        "created_at": datetime(2026, 1, 1),
    }
    if id_:
        kwargs["id"] = id_
    return ROIModel(**kwargs)


def test_add_undo_redo():
    manager = ROIManager()
    stack = RoiUndoStack(manager)
    roi = make_roi(id_="r1")
    stack.push_and_apply(AddRoiCommand(roi))
    assert manager.get_roi("r1") is not None
    assert stack.undo() is True
    assert manager.get_roi("r1") is None
    assert stack.redo() is True
    assert manager.get_roi("r1") is not None
    assert manager.get_roi("r1").x == 0


def test_remove_undo_restores_snapshot():
    manager = ROIManager()
    stack = RoiUndoStack(manager)
    roi = make_roi(id_="r2", x=5, y=6, w=20, h=30)
    manager.add_roi(roi)
    stack.push(RemoveRoiCommand(roi.snapshot()))
    manager.remove_roi("r2")
    assert manager.get_roi("r2") is None
    stack.undo()
    restored = manager.get_roi("r2")
    assert restored is not None
    assert (restored.x, restored.y, restored.w, restored.h) == (5, 6, 20, 30)


def test_batch_remove_is_single_undo_step():
    manager = ROIManager()
    stack = RoiUndoStack(manager)
    rois = [make_roi(id_=f"b{i}", x=i) for i in range(3)]
    for r in rois:
        manager.add_roi(r)
    snaps = tuple(r.snapshot() for r in rois)
    stack.push(BatchRemoveCommand(snaps))
    for r in rois:
        manager.remove_roi(r.id)
    assert manager.all_rois() == []
    assert stack.undo() is True
    assert len(manager.all_rois()) == 3
    assert stack.can_undo is False


def test_update_roi_undo():
    manager = ROIManager()
    stack = RoiUndoStack(manager)
    old = make_roi(id_="u1", x=1, y=2, w=3, h=4, )
    manager.add_roi(old)
    new = old.snapshot()
    new.x, new.y, new.w, new.h, new.angle = 10, 20, 30, 40, 15.0
    stack.push(UpdateRoiCommand(old.snapshot(), new.snapshot()))
    manager.replace_roi(new.snapshot())
    assert manager.get_roi("u1").x == 10
    stack.undo()
    cur = manager.get_roi("u1")
    assert (cur.x, cur.y, cur.w, cur.h, cur.angle) == (1, 2, 3, 4, 0.0)
    stack.redo()
    cur = manager.get_roi("u1")
    assert (cur.x, cur.w, cur.angle) == (10, 30, 15.0)


def test_clear_slide_undo():
    manager = ROIManager()
    stack = RoiUndoStack(manager)
    slide = Path("s.sdpc")
    rois = [make_roi(slide="s.sdpc", id_=f"c{i}") for i in range(2)]
    other = make_roi(slide="o.sdpc", id_="other")
    for r in rois + [other]:
        manager.add_roi(r)
    snaps = tuple(r.snapshot() for r in rois)
    stack.push(ClearSlideCommand(slide, snaps))
    manager.clear_slide_rois(slide)
    assert manager.get_slide_rois(slide) == []
    assert manager.get_roi("other") is not None
    stack.undo()
    assert len(manager.get_slide_rois(slide)) == 2
    assert manager.get_roi("other") is not None


def test_stack_depth_limit():
    manager = ROIManager()
    stack = RoiUndoStack(manager, max_depth=3)
    for i in range(5):
        stack.push_and_apply(AddRoiCommand(make_roi(id_=f"d{i}", x=i)))
    assert stack.undo_depth == 3
    # 最早两条被挤出，只能撤销最近 3 次
    for _ in range(3):
        assert stack.undo() is True
    assert stack.undo() is False
    assert manager.get_roi("d0") is not None
    assert manager.get_roi("d1") is not None
    assert manager.get_roi("d2") is None


def test_redo_cleared_on_new_command():
    manager = ROIManager()
    stack = RoiUndoStack(manager)
    stack.push_and_apply(AddRoiCommand(make_roi(id_="x1")))
    stack.undo()
    assert stack.can_redo
    stack.push_and_apply(AddRoiCommand(make_roi(id_="x2")))
    assert stack.can_redo is False


def test_batch_add_copy_undo():
    manager = ROIManager()
    stack = RoiUndoStack(manager)
    src = make_roi(id_="src", x=100, y=100)
    manager.add_roi(src)
    copies = (
        make_roi(id_="cp1", x=150, y=150),
        make_roi(id_="cp2", x=200, y=200),
    )
    stack.push(BatchAddCommand(copies))
    for c in copies:
        manager.add_roi(c)
    assert len(manager.all_rois()) == 3
    stack.undo()
    assert manager.get_roi("src") is not None
    assert manager.get_roi("cp1") is None
    assert manager.get_roi("cp2") is None


def test_mutations_after_undo_receive_fresh_objects():
    """undo 恢复的 ROI 必须是独立对象，后续编辑不影响栈内快照。"""
    manager = ROIManager()
    stack = RoiUndoStack(manager)
    roi = make_roi(id_="iso", x=0)
    stack.push_and_apply(AddRoiCommand(roi))
    stack.undo()
    stack.redo()
    live = manager.get_roi("iso")
    live.x = 999
    stack.undo()
    stack.redo()
    assert manager.get_roi("iso").x == 0
