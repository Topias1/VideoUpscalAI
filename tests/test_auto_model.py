"""Auto mode must pick the model from how damaged the source is.

Measured on real material: aggressive restoration (upscayl-standard-4x) wins
on degraded footage because there is genuine degradation to invert; on a clean
sharp source that same aggressiveness fabricates texture, and the conservative
high-fidelity-4x scored higher and flickered less between frames.
"""
import pytest

from upscaler.pipeline import is_source_degraded, select_auto_model
from upscaler.probe import VideoInfo


def info(width, height):
    return VideoInfo(
        width=width, height=height, fps="30/1", frame_count=100, duration=3.3,
        has_audio=False, has_subtitles=False, has_chapters=False,
        is_hdr=False, is_vfr=False, color_transfer="bt709",
        color_primaries="bt709",
    )


@pytest.mark.parametrize("w,h,expected", [
    (480, 360, "upscayl-standard-4x"),    # social-network export
    (720, 576, "upscayl-standard-4x"),    # PAL camcorder capture
    (544, 960, "upscayl-standard-4x"),    # portrait phone clip, short side 544
    (1920, 1080, "high-fidelity-4x"),     # clean HD
    (2160, 3840, "high-fidelity-4x"),     # clean 4K, portrait
])
def test_model_follows_source_condition(w, h, expected):
    assert select_auto_model(info(w, h), "cinema", True) == expected


def test_portrait_is_judged_on_its_short_side():
    """A 720-wide portrait clip is low resolution, not high."""
    assert is_source_degraded(info(720, 1280))
    assert select_auto_model(info(720, 1280), "cinema", True) == "upscayl-standard-4x"


def test_threshold_boundary():
    assert is_source_degraded(info(1280, 720))
    assert not is_source_degraded(info(1280, 721))


def test_animation_overrides_the_condition_rule():
    assert select_auto_model(info(480, 360), "animation", True) == "digital-art-4x"
    assert select_auto_model(info(3840, 2160), "animation", True) == "digital-art-4x"


def test_non_upscayl_binary_keeps_its_own_model():
    assert select_auto_model(info(480, 360), "cinema", False) == "realesrgan-x4plus"
    assert select_auto_model(info(480, 360), "animation", False) == "realesr-animevideov3"
