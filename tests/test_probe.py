import json
import pytest
from unittest.mock import patch, MagicMock
import subprocess

from upscaler import ProbeError
from upscaler.probe import probe_video, parse_rational

def test_parse_rational():
    from fractions import Fraction
    assert parse_rational("24000/1001") == Fraction(24000, 1001)
    assert parse_rational("30/1") == Fraction(30, 1)
    assert parse_rational("0/0") is None
    assert parse_rational("") is None
    assert parse_rational("invalid") is None

@patch("subprocess.run")
def test_probe_video_sdr_cfr(mock_run):
    # Mock ffprobe output for an SDR, CFR, 1080p video with audio, subtitles, and chapters
    mock_data = {
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "width": 1920,
                "height": 1080,
                "r_frame_rate": "24/1",
                "avg_frame_rate": "24/1",
                "nb_frames": "240"
            },
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "aac",
                "channels": 2
            },
            {
                "index": 2,
                "codec_type": "subtitle",
                "codec_name": "subrip"
            }
        ],
        "format": {
            "duration": "10.0"
        },
        "chapters": [
            {"id": 0, "start": "0.0", "end": "5.0"}
        ]
    }
    
    mock_res = MagicMock()
    mock_res.stdout = json.dumps(mock_data)
    mock_res.returncode = 0
    mock_run.return_value = mock_res
    
    info = probe_video("dummy.mp4")
    
    assert info.width == 1920
    assert info.height == 1080
    assert info.fps == "24/1"
    assert info.frame_count == 240
    assert info.duration == 10.0
    assert info.has_audio is True
    assert info.has_subtitles is True
    assert info.has_chapters is True
    assert info.is_hdr is False
    assert info.is_vfr is False

@patch("subprocess.run")
def test_probe_video_hdr_vfr(mock_run):
    # Mock ffprobe output for an HDR, VFR video (r_frame_rate != avg_frame_rate)
    # and HDR primaries / transfer function
    mock_data = {
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "width": 3840,
                "height": 2160,
                "r_frame_rate": "30000/1001",
                "avg_frame_rate": "2997/100",  # 29.97 vs 29.9700299...
                "nb_frames": "300",
                "color_transfer": "smpte2084",
                "color_primaries": "bt2020"
            }
        ],
        "format": {
            "duration": "10.0"
        },
        "chapters": []
    }
    
    mock_res = MagicMock()
    mock_res.stdout = json.dumps(mock_data)
    mock_res.returncode = 0
    mock_run.return_value = mock_res
    
    info = probe_video("dummy.mp4")
    
    assert info.width == 3840
    assert info.height == 2160
    assert info.is_hdr is True
    assert info.is_vfr is True
    assert info.color_transfer == "smpte2084"
    assert info.color_primaries == "bt2020"

@patch("subprocess.run")
def test_probe_video_no_video_stream(mock_run):
    # Mock audio-only file
    mock_data = {
        "streams": [
            {
                "index": 0,
                "codec_type": "audio",
                "codec_name": "mp3"
            }
        ],
        "format": {
            "duration": "10.0"
        }
    }
    mock_res = MagicMock()
    mock_res.stdout = json.dumps(mock_data)
    mock_res.returncode = 0
    mock_run.return_value = mock_res
    
    with pytest.raises(ProbeError) as exc:
        probe_video("dummy.mp3")
    assert "No video stream found" in str(exc.value)

@patch("subprocess.run")
def test_probe_video_error_status(mock_run):
    # ffprobe fails
    mock_run.side_effect = subprocess.CalledProcessError(
        returncode=1,
        cmd="ffprobe",
        stderr="Corrupt file"
    )
    
    with pytest.raises(ProbeError) as exc:
        probe_video("corrupt.mp4")
    assert "ffprobe failed to read metadata" in str(exc.value)
