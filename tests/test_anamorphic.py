"""Anamorphic sources must be encoded at their true displayed resolution.

A PAL DV capture is 720x576 stored and 4:3 displayed. Scaling by the stored
pixel dimensions produced 1350x1080 for the "1080p" preset — a 5:4 picture,
so faces come out narrowed.

The pixel aspect cannot be recovered from the frames at encode time: the
upscaler strips it from the PNGs it writes. An earlier attempt read it off the
frames and silently did nothing, because the test built its fixtures with
ffmpeg (which preserves the flag) instead of running them through the
upscaler. These tests therefore feed the encoder square-pixel frames, exactly
like the real pipeline does, and check that the width comes from the caller.
"""
import subprocess

import pytest

from upscaler.ffmpeg_cmds import build_encode_cmd
from upscaler.probe import VideoInfo
from upscaler.tools import get_ffmpeg_path, get_ffprobe_path


def target_width(info, target_h):
    """Same computation the pipeline performs before encoding."""
    if not info.display_aspect:
        return None
    return int(round(target_h * info.display_aspect / 2)) * 2


def make_frames(tmp_path, w, h, count=12):
    """Square-pixel frames, as the upscaler emits them."""
    pattern = tmp_path / "f_%08d.png"
    subprocess.run(
        [get_ffmpeg_path(), "-y", "-f", "lavfi",
         "-i", f"testsrc=duration=1:size={w}x{h}:rate=30",
         "-frames:v", str(count), "-vf", "setsar=1", str(pattern)],
        capture_output=True, check=True)
    return str(pattern)


def geometry(path):
    res = subprocess.run(
        [get_ffprobe_path(), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,sample_aspect_ratio",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True)
    w, h, sar = res.stdout.strip().rstrip(",").split(",")[:3]
    return int(w), int(h), sar


def encode(frames, out, preset, target_w):
    subprocess.run(
        build_encode_cmd(frames, str(out), "25", preset, "libx265", 60, None,
                         target_w=target_w),
        capture_output=True, check=True)


def info(width, height, display_aspect):
    return VideoInfo(
        width=width, height=height, fps="25/1", frame_count=500, duration=20.0,
        has_audio=False, has_subtitles=False, has_chapters=False, is_hdr=False,
        is_vfr=False, color_transfer="bt709", color_primaries="bt709",
        display_aspect=display_aspect,
    )


def test_pal_dv_reaches_its_displayed_width(tmp_path):
    """720x576 displayed 4:3, upscaled 4x -> 1080p must be 1440 wide."""
    src = info(720, 576, 4 / 3)
    assert target_width(src, 1080) == 1440
    frames = make_frames(tmp_path, 2880, 2304)   # 4x, square pixels
    out = tmp_path / "out.mp4"
    encode(frames, out, "1080p", target_width(src, 1080))
    assert geometry(out)[:2] == (1440, 1080)


def test_anamorphic_widescreen_reaches_full_width(tmp_path):
    """720x480 displayed 16:9 -> 1920x1080, not 1620x1080."""
    src = info(720, 480, 16 / 9)
    frames = make_frames(tmp_path, 2880, 1920)
    out = tmp_path / "out.mp4"
    encode(frames, out, "1080p", target_width(src, 1080))
    assert geometry(out)[:2] == (1920, 1080)


def test_square_pixel_source_is_unchanged(tmp_path):
    src = info(1920, 1440, 1920 / 1440)
    frames = make_frames(tmp_path, 1920, 1440)
    out = tmp_path / "out.mp4"
    encode(frames, out, "720p", target_width(src, 720))
    assert geometry(out)[:2] == (960, 720)


def test_rotated_portrait_source_keeps_its_shape(tmp_path):
    """A 480x360 clip rotated a quarter turn displays 3:4, so 810x1080."""
    src = info(480, 360, 0.75)
    assert target_width(src, 1080) == 810
    frames = make_frames(tmp_path, 1440, 1920)
    out = tmp_path / "out.mp4"
    encode(frames, out, "1080p", target_width(src, 1080))
    assert geometry(out)[:2] == (810, 1080)


def test_width_is_always_even(tmp_path):
    """Encoders reject odd dimensions; 1080 x 9:16 lands on 607.5."""
    src = info(3840, 2160, 9 / 16)
    assert target_width(src, 1080) % 2 == 0
