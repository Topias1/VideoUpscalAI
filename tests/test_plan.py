import pytest
from pathlib import Path
import os

from upscaler import PresetGuardError, VFRError, HDRError
from upscaler.plan import (
    get_target_height,
    check_preset_guard,
    check_vfr_mode,
    check_hdr_mode,
    resolve_output_paths
)

def test_get_target_height():
    assert get_target_height("480p") == 480
    assert get_target_height("720p") == 720
    assert get_target_height("1080p") == 1080
    assert get_target_height("4k") == 2160
    with pytest.raises(ValueError):
        get_target_height("invalid")

def test_check_preset_guard():
    # Target height for 1080p is 1080
    # Source height < 1080 should pass
    check_preset_guard(720, "1080p")
    check_preset_guard(1079, "1080p")
    
    # Source height >= 1080 should raise PresetGuardError
    with pytest.raises(PresetGuardError):
        check_preset_guard(1080, "1080p")
    with pytest.raises(PresetGuardError):
        check_preset_guard(2160, "1080p")

def test_check_vfr_mode():
    # If not VFR, it should always pass
    check_vfr_mode(False, "error")
    check_vfr_mode(False, "cfr")
    check_vfr_mode(False, "warn")
    
    # If VFR, error mode raises VFRError, other modes pass
    with pytest.raises(VFRError):
        check_vfr_mode(True, "error")
    check_vfr_mode(True, "cfr")
    check_vfr_mode(True, "warn")

def test_check_hdr_mode():
    # If not HDR, it should always pass
    check_hdr_mode(False, "error")
    check_hdr_mode(False, "tonemap")
    check_hdr_mode(False, "passthrough")
    
    # If HDR, error mode raises HDRError, other modes pass
    with pytest.raises(HDRError):
        check_hdr_mode(True, "error")
    check_hdr_mode(True, "tonemap")
    check_hdr_mode(True, "passthrough")

def test_resolve_output_paths_single_file_with_output():
    # Single file with -o option
    inputs = [("/path/to/input.mp4", "/path/to/input.mp4")]
    res = resolve_output_paths(inputs, "/custom/output.mp4", None, "1080p")
    assert res == [("/path/to/input.mp4", "/custom/output.mp4")]

def test_resolve_output_paths_multiple_files_with_output_raises():
    # Multiple files with -o option should raise ValueError
    inputs = [
        ("/path/to/input1.mp4", "/path/to/input1.mp4"),
        ("/path/to/input2.mp4", "/path/to/input2.mp4"),
    ]
    with pytest.raises(ValueError) as exc:
        resolve_output_paths(inputs, "/custom/output.mp4", None, "1080p")
    assert "can only be used when upscaling a single file" in str(exc.value)

def test_resolve_output_paths_alongside_source():
    inputs = [
        ("/path/to/input1.mp4", "/path/to/input1.mp4"),
        ("/path/to/dir/input2.mkv", "/path/to/dir/input2.mkv"),
    ]
    res = resolve_output_paths(inputs, None, None, "1080p")
    assert res == [
        ("/path/to/input1.mp4", os.path.abspath("/path/to/input1_1080p.mp4")),
        ("/path/to/dir/input2.mkv", os.path.abspath("/path/to/dir/input2_1080p.mp4")),
    ]

def test_resolve_output_paths_with_output_dir():
    inputs = [
        ("/path/to/input1.mp4", "/path/to/input1.mp4"),
        ("/path/to/dir/input2.mkv", "/path/to/dir/input2.mkv"),
    ]
    res = resolve_output_paths(inputs, None, "/out", "1080p")
    assert res == [
        ("/path/to/input1.mp4", os.path.abspath("/out/input1_1080p.mp4")),
        ("/path/to/dir/input2.mkv", os.path.abspath("/out/input2_1080p.mp4")),
    ]

def test_resolve_output_paths_recursive_mirroring():
    # Directory walk structure: root dir / subdir / file.mp4
    inputs = [
        ("/path/to/root/subdir1/fileA.mp4", "/path/to/root"),
        ("/path/to/root/subdir2/fileB.mkv", "/path/to/root"),
    ]
    res = resolve_output_paths(inputs, None, "/out", "4k")
    assert res == [
        ("/path/to/root/subdir1/fileA.mp4", os.path.abspath("/out/subdir1/fileA_4k.mp4")),
        ("/path/to/root/subdir2/fileB.mkv", os.path.abspath("/out/subdir2/fileB_4k.mp4")),
    ]

def test_resolve_output_paths_collision_raises():
    # Collision if different input files map to the same output file
    inputs = [
        ("/path/to/root1/file.mp4", "/path/to/root1"),
        ("/path/to/root2/file.mp4", "/path/to/root2"),
    ]
    # Here root is not a dir, but we map to /out directory
    # Both inputs are mapped under /out/file_1080p.mp4 if roots are files
    inputs_with_roots = [
        ("/path/to/root1/file.mp4", "/path/to/root1/file.mp4"),
        ("/path/to/root2/file.mp4", "/path/to/root2/file.mp4"),
    ]
    with pytest.raises(ValueError) as exc:
        resolve_output_paths(inputs_with_roots, None, "/out", "1080p")
    assert "Output path collision detected" in str(exc.value)
