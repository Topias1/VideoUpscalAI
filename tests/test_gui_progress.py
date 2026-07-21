"""Tests for the GUI's overall/per-worker progress accounting."""
import pytest

from gui import ProgressTracker
from upscaler.progress import segment_pct


def make_tracker(total_files=1, total_segs=1):
    t = ProgressTracker()
    t.handle({"t": "file", "idx": 1, "total": total_files, "name": "a.mov"})
    t.handle({"t": "segs", "total": total_segs, "workers": 4})
    return t


def test_segment_pct_spans_all_stages():
    assert segment_pct("extract", 0.0) == 0.0
    assert segment_pct("extract", 100.0) == pytest.approx(5.0)
    assert segment_pct("upscale", 0.0) == pytest.approx(5.0)
    assert segment_pct("upscale", 100.0) == pytest.approx(90.0)
    assert segment_pct("encode", 100.0) == pytest.approx(100.0)


def test_parallel_segments_each_contribute_their_share():
    t = make_tracker(total_segs=4)
    t.handle({"t": "seg", "seg": "seg_0000.mkv", "stage": "upscale", "pct": 50.0})
    t.handle({"t": "seg", "seg": "seg_0001.mkv", "stage": "upscale", "pct": 50.0})
    # Two of four segments at half → 25% of the file.
    assert t.overall_progress() == pytest.approx(25.0)
    assert [w["seg"] for w in t.worker_bars()] == ["seg_0000.mkv", "seg_0001.mkv"]


def test_progress_never_regresses_when_a_segment_finishes():
    t = make_tracker(total_segs=2)
    t.handle({"t": "seg", "seg": "seg_0000.mkv", "stage": "upscale", "pct": 90.0})
    before = t.overall_progress()
    t.handle({"t": "seg_done", "seg": "seg_0000.mkv"})
    assert t.overall_progress() >= before
    assert t.overall_progress() == pytest.approx(50.0)
    # A completed segment leaves the worker list.
    assert t.worker_bars() == []


def test_late_event_for_finished_segment_is_ignored():
    """realesrgan output can lag behind the completion event."""
    t = make_tracker(total_segs=2)
    t.handle({"t": "seg_done", "seg": "seg_0000.mkv"})
    t.handle({"t": "seg", "seg": "seg_0000.mkv", "stage": "upscale", "pct": 20.0})
    assert t.overall_progress() == pytest.approx(50.0)
    assert t.worker_bars() == []


def test_progress_is_weighted_across_files():
    t = ProgressTracker()
    t.handle({"t": "file", "idx": 2, "total": 4, "name": "b.mov"})
    t.handle({"t": "segs", "total": 2, "workers": 2})
    t.handle({"t": "seg_done", "seg": "seg_0000.mkv"})
    # One file done (25%) + half of the second (12.5%).
    assert t.overall_progress() == pytest.approx(37.5)


def test_new_file_resets_segment_tracking():
    t = make_tracker(total_files=2, total_segs=1)
    t.handle({"t": "seg_done", "seg": "seg_0000.mkv"})
    assert t.overall_progress() == pytest.approx(50.0)
    t.handle({"t": "file", "idx": 2, "total": 2, "name": "b.mov"})
    assert t.overall_progress() == pytest.approx(50.0)
    assert t.worker_bars() == []


def test_progress_is_clamped_to_100():
    t = make_tracker(total_segs=1)
    t.handle({"t": "seg_done", "seg": "seg_0000.mkv"})
    t.handle({"t": "seg_done", "seg": "seg_0001.mkv"})  # more than announced
    assert t.overall_progress() == pytest.approx(100.0)
