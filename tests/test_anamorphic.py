"""Anamorphic sources must be encoded at their true display resolution.

A 720x480 source with SAR 32:27 displays as 16:9. Scaling by stored pixel
dimensions produced a 3240x2160 anamorphic file for the "4K" preset: correct
on screen, but 16% short of 4K horizontally — the player stretches it back,
undoing part of what the upscaler just computed.
"""
import subprocess

import pytest

from upscaler.ffmpeg_cmds import build_encode_cmd
from upscaler.tools import get_ffmpeg_path, get_ffprobe_path


def encode(frames_pattern, out_path, preset="4k"):
    cmd = build_encode_cmd(frames_pattern, str(out_path), "30", preset,
                           "libx265", 60, None)
    subprocess.run(cmd, capture_output=True, check=True)


def geometry(path):
    res = subprocess.run(
        [get_ffprobe_path(), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,sample_aspect_ratio",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True)
    w, h, sar = res.stdout.strip().rstrip(",").split(",")[:3]
    return int(w), int(h), sar


def make_frames(tmp_path, w, h, sar, count=12):
    """Synthesise PNG frames carrying a sample aspect ratio.

    Keep the count above x265's lookahead delay: a 2-frame encode fails in
    the encoder itself, which has nothing to do with what these tests check.
    """
    pattern = tmp_path / "f_%08d.png"
    subprocess.run(
        [get_ffmpeg_path(), "-y", "-f", "lavfi",
         "-i", f"testsrc=duration=1:size={w}x{h}:rate=30",
         "-frames:v", str(count), "-vf", f"setsar={sar}", str(pattern)],
        capture_output=True, check=True)
    return str(pattern)


def test_anamorphic_source_reaches_full_width(tmp_path):
    """720x480 SAR 32:27 upscaled 4x -> 4K must be 3840 wide, square pixels."""
    frames = make_frames(tmp_path, 2880, 1920, "32/27")
    out = tmp_path / "out.mp4"
    encode(frames, out)
    w, h, sar = geometry(out)
    assert (w, h) == (3840, 2160)
    assert sar in ("1:1", "N/A")


def test_square_pixel_source_is_unchanged(tmp_path):
    """The common case must keep its existing geometry."""
    frames = make_frames(tmp_path, 1920, 1440, "1")
    out = tmp_path / "out.mp4"
    encode(frames, out, preset="720p")
    w, h, sar = geometry(out)
    assert (w, h) == (960, 720)
    assert sar in ("1:1", "N/A")


def test_widescreen_square_pixel_source(tmp_path):
    frames = make_frames(tmp_path, 1280, 720, "1")
    out = tmp_path / "out.mp4"
    encode(frames, out, preset="1080p")
    w, h, _ = geometry(out)
    assert (w, h) == (1920, 1080)
