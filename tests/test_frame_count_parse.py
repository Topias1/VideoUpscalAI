"""Regression tests for ffprobe CSV scalar parsing.

ffprobe 8.x emits a trailing field separator with `-of csv=p=0`
(e.g. "59,\n"). Naive int() parsing raises ValueError, which used to be
swallowed and silently downgraded the exact packet count to a
duration * fps estimate — producing off-by-one frame counts and bogus
ReconciliationError failures on MKV segments.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from upscaler.probe import parse_ffprobe_scalar_int, probe_video
from upscaler.pipeline import get_exact_frame_count


def test_parse_ffprobe_scalar_int_strips_trailing_separator():
    assert parse_ffprobe_scalar_int("59,\n") == 59
    assert parse_ffprobe_scalar_int("59\n") == 59
    assert parse_ffprobe_scalar_int("59,60,\n") == 59
    assert parse_ffprobe_scalar_int("N/A,\n") is None
    assert parse_ffprobe_scalar_int("") is None
    assert parse_ffprobe_scalar_int("\n") is None


@patch("upscaler.pipeline.get_ffprobe_path", return_value="/fake/ffprobe")
@patch("upscaler.pipeline.subprocess.run")
def test_get_exact_frame_count_handles_trailing_comma(mock_run, _mock_path):
    mock_run.return_value = MagicMock(returncode=0, stdout="59,\n", stderr="")
    assert get_exact_frame_count("/fake/seg_0000.mkv") == 59


@patch("upscaler.probe.get_ffprobe_path", return_value="/fake/ffprobe")
@patch("upscaler.probe.subprocess.run")
def test_probe_video_counts_packets_when_nb_frames_missing(mock_run, _mock_path):
    """MKV segments have no nb_frames; the packet count must be used verbatim."""
    mock_data = {
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "width": 480,
                "height": 360,
                "r_frame_rate": "30/1",
                "avg_frame_rate": "30/1",
                # no nb_frames, as with Matroska
            }
        ],
        "format": {"duration": "1.999"},
        "chapters": [],
    }

    def side_effect(cmd, *args, **kwargs):
        if "-count_packets" in cmd:
            return MagicMock(returncode=0, stdout="59,\n", stderr="")
        return MagicMock(returncode=0, stdout=json.dumps(mock_data), stderr="")

    mock_run.side_effect = side_effect

    info = probe_video("/fake/seg_0000.mkv")
    # duration * fps would round to 60 — the exact packet count is 59.
    assert info.frame_count == 59
