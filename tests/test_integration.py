import os
import subprocess
import sys
from pathlib import Path
import pytest

from upscaler.probe import probe_video
from upscaler import PresetGuardError

# Mark integration tests as slow
pytestmark = pytest.mark.slow

@pytest.fixture
def test_clip(tmp_path):
    """Generates a 2-second synthetic video clip with video and audio."""
    clip_path = tmp_path / "src_clip.mp4"
    cmd = [
        "ffmpeg",
        "-y",
        "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=24",
        "-f", "lavfi", "-i", "sine=duration=2:frequency=1000",
        "-c:v", "libx264",
        "-c:a", "aac",
        "-pix_fmt", "yuv420p",
        str(clip_path)
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    return str(clip_path)

def test_integration_full_run(test_clip, tmp_path):
    output_path = tmp_path / "output_720p.mp4"
    stub_bin = os.path.abspath(Path(__file__).parent / "stub_realesrgan.py")
    
    # Run the upscale script directly using python3
    cmd = [
        sys.executable,
        "upscale.py",
        test_clip,
        "-o", str(output_path),
        "--preset", "720p",
        "--realesrgan-bin", stub_bin,
        "--chunk-seconds", "1.0",
        "--encoder", "libx265",
        "--quality", "60"
    ]
    
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, f"Upscaler failed: {res.stderr}\nStdout: {res.stdout}"
    
    # Verify the output exists
    assert output_path.exists()
    
    # Probe the output
    info = probe_video(str(output_path))
    
    # Width should be 320/240 * 720 = 960
    assert info.width == 960
    assert info.height == 720
    
    # Check duration is ~2.0 seconds
    assert abs(info.duration - 2.0) < 0.2
    
    # Verify audio is present
    assert info.has_audio is True

def test_integration_preset_guard(test_clip, tmp_path):
    # Try upscaling a 240p video to a 240p-equivalent preset (Preset guard check)
    # Since our test_clip is 240p (320x240), if we try to upscale to 720p, it works.
    # Let's generate a 1080p source clip, then try to upscale it to 720p.
    big_clip_path = tmp_path / "big_clip.mp4"
    cmd = [
        "ffmpeg",
        "-y",
        "-f", "lavfi", "-i", "testsrc=duration=1:size=1920x1080:rate=24",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        str(big_clip_path)
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    
    output_path = tmp_path / "should_fail.mp4"
    stub_bin = os.path.abspath(Path(__file__).parent / "stub_realesrgan.py")
    
    cmd_run = [
        sys.executable,
        "upscale.py",
        str(big_clip_path),
        "-o", str(output_path),
        "--preset", "720p",  # 720p is < 1080p, should fail preset guard
        "--realesrgan-bin", stub_bin
    ]
    
    res = subprocess.run(cmd_run, capture_output=True, text=True)
    assert res.returncode == 1
    assert "PresetGuardError" in res.stderr or "already greater than or equal to the target preset" in (res.stdout + res.stderr)

def test_integration_batch_resume(test_clip, tmp_path):
    # Setup two files in a directory
    in_dir = tmp_path / "batch_in"
    in_dir.mkdir()
    
    clip1 = in_dir / "clip1.mp4"
    clip2 = in_dir / "clip2.mp4"
    
    shutil_copy = lambda src, dst: Path(dst).write_bytes(Path(src).read_bytes())
    shutil_copy(test_clip, clip1)
    shutil_copy(test_clip, clip2)
    
    out_dir = tmp_path / "batch_out"
    out_dir.mkdir()
    
    stub_bin = os.path.abspath(Path(__file__).parent / "stub_realesrgan.py")
    
    # Run batch upscaling
    cmd = [
        sys.executable,
        "upscale.py",
        str(in_dir),
        "--output-dir", str(out_dir),
        "--preset", "720p",
        "--realesrgan-bin", stub_bin,
        "--chunk-seconds", "1.0",
        "--encoder", "libx265"
    ]
    
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, f"Batch run failed: {res.stderr}"
    
    # Assert output files exist
    out1 = out_dir / "clip1_720p.mp4"
    out2 = out_dir / "clip2_720p.mp4"
    assert out1.exists()
    assert out2.exists()
    
    # Record modification times
    mtime1 = out1.stat().st_mtime
    
    # Re-run same command
    res2 = subprocess.run(cmd, capture_output=True, text=True)
    assert res2.returncode == 0
    
    # Output 1 should have been skipped, so modification time should be identical
    assert out1.stat().st_mtime == mtime1
