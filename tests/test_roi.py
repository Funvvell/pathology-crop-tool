import json
from pathlib import Path

import pytest
from liver_portal_crop.roi import ROIModel, ROIManager


class TestROIModel:
    def test_create_roi(self):
        roi = ROIModel(
            slide_path=Path("/test/slide.sdpc"),
            x=10, y=20, w=100, h=200,
        )
        assert roi.id is not None
        assert len(roi.id) == 12
        assert roi.slide_path == Path("/test/slide.sdpc")
        assert roi.x == 10
        assert roi.y == 20
        assert roi.w == 100
        assert roi.h == 200
        assert roi.created_at is not None

    def test_auto_id_unique(self):
        roi1 = ROIModel(
            slide_path=Path("a.sdpc"), x=0, y=0, w=10, h=10,
        )
        roi2 = ROIModel(
            slide_path=Path("a.sdpc"), x=0, y=0, w=10, h=10,
        )
        assert roi1.id != roi2.id

    def test_default_slide_path(self):
        roi = ROIModel(
            slide_path=Path("test.sdpc"), x=0, y=0, w=10, h=10,
        )
        assert isinstance(roi.slide_path, Path)

    def test_snapshot_is_independent(self):
        roi = ROIModel(slide_path=Path("a.sdpc"), x=1, y=2, w=3, h=4, id="snap")
        snap = roi.snapshot()
        roi.x = 99
        assert snap.x == 1
        assert snap.id == "snap"


class TestROIManager:
    def setup_method(self):
        self.manager = ROIManager()

    def _make_roi(self, slide="a.sdpc", x=0, y=0, w=10, h=10):
        return ROIModel(
            slide_path=Path(slide), x=x, y=y, w=w, h=h,
        )

    def test_add_and_count(self):
        roi = self._make_roi()
        self.manager.add_roi(roi)
        assert len(self.manager.all_rois()) == 1

    def test_add_roi_signal(self):
        received = []
        self.manager.roi_added.connect(received.append)
        roi = self._make_roi()
        self.manager.add_roi(roi)
        assert len(received) == 1
        assert received[0] == roi

    def test_remove_roi(self):
        roi = self._make_roi()
        self.manager.add_roi(roi)
        self.manager.remove_roi(roi.id)
        assert len(self.manager.all_rois()) == 0

    def test_remove_roi_signal(self):
        received = []
        self.manager.roi_removed.connect(received.append)
        roi = self._make_roi()
        self.manager.add_roi(roi)
        self.manager.remove_roi(roi.id)
        assert received == [roi.id]

    def test_clear_slide_rois(self):
        self.manager.add_roi(self._make_roi(slide="a.sdpc"))
        self.manager.add_roi(self._make_roi(slide="a.sdpc"))
        self.manager.add_roi(self._make_roi(slide="b.sdpc"))
        self.manager.clear_slide_rois(Path("a.sdpc"))
        assert len(self.manager.get_slide_rois(Path("a.sdpc"))) == 0
        assert len(self.manager.get_slide_rois(Path("b.sdpc"))) == 1

    def test_clear_slide_rois_signal(self):
        received = []
        self.manager.roi_cleared.connect(received.append)
        self.manager.add_roi(self._make_roi(slide="a.sdpc"))
        self.manager.clear_slide_rois(Path("a.sdpc"))
        assert received == [Path("a.sdpc")]

    def test_get_slide_rois(self):
        self.manager.add_roi(self._make_roi(slide="a.sdpc"))
        self.manager.add_roi(self._make_roi(slide="b.sdpc"))
        rois_a = self.manager.get_slide_rois(Path("a.sdpc"))
        assert len(rois_a) == 1
        assert rois_a[0].slide_path == Path("a.sdpc")

    def test_to_json_empty(self):
        data = self.manager.to_json()
        assert data == {"rois": []}

    def test_to_json_roundtrip(self):
        roi = self._make_roi(slide="a.sdpc", x=10, y=20, w=100, h=200)
        self.manager.add_roi(roi)
        data = self.manager.to_json()

        manager2 = ROIManager()
        manager2.from_json(data)
        rois2 = manager2.all_rois()
        assert len(rois2) == 1
        r = rois2[0]
        assert r.id == roi.id
        assert r.slide_path == Path("a.sdpc")
        assert r.x == 10
        assert r.y == 20
        assert r.w == 100
        assert r.h == 200

    def test_get_roi(self):
        roi = self._make_roi()
        self.manager.add_roi(roi)
        assert self.manager.get_roi(roi.id) is roi
        assert self.manager.get_roi("missing") is None

    def test_sorted_slide_rois(self):
        slide = Path("s.sdpc")
        self.manager.add_roi(ROIModel(slide_path=slide, x=50, y=0, w=1, h=1, id="b"))
        self.manager.add_roi(ROIModel(slide_path=slide, x=10, y=5, w=1, h=1, id="a"))
        self.manager.add_roi(ROIModel(slide_path=slide, x=10, y=1, w=1, h=1, id="c"))
        ids = [r.id for r in self.manager.sorted_slide_rois(slide)]
        assert ids == ["c", "a", "b"]

    def test_update_roi_geometry_emits_updated(self):
        m = ROIManager()
        seen = []
        m.roi_updated.connect(lambda r: seen.append(r.id))
        m.add_roi(ROIModel(slide_path=Path("a.sdpc"), x=0, y=0, w=1, h=1, id="u"))
        m.update_roi_geometry("u", 5, 6, 7, 8, 12.5)
        roi = m.get_roi("u")
        assert (roi.x, roi.y, roi.w, roi.h, roi.angle) == (5, 6, 7, 8, 12.5)
        assert seen == ["u"]

    def test_replace_roi(self):
        m = ROIManager()
        m.add_roi(ROIModel(slide_path=Path("a.sdpc"), x=0, y=0, w=1, h=1, id="p"))
        new = ROIModel(slide_path=Path("a.sdpc"), x=9, y=9, w=2, h=2, id="p", angle=30)
        m.replace_roi(new)
        assert m.get_roi("p").x == 9
        assert m.get_roi("p").angle == 30
