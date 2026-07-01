import os
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from upscaler.batch import discover_inputs, run_batch

def test_discover_inputs_extension_filter_and_recursion(tmp_path):
    # Setup files in tmp_path
    # Root files:
    # - video1.mp4 (valid)
    # - video2.MKV (valid, case insensitive)
    # - image.png (ignored)
    # - notes.txt (ignored)
    # Sub directory:
    # - sub/video3.mkv (valid)
    # - sub/audio.mp3 (ignored)
    
    (tmp_path / "video1.mp4").write_text("dummy")
    (tmp_path / "video2.MKV").write_text("dummy")
    (tmp_path / "image.png").write_text("dummy")
    (tmp_path / "notes.txt").write_text("dummy")
    
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "video3.mkv").write_text("dummy")
    (sub / "audio.mp3").write_text("dummy")

    # Non-recursive discovery
    non_rec_discovered = discover_inputs([str(tmp_path)], recursive=False)
    paths_non_rec = [os.path.basename(p[0]) for p in non_rec_discovered]
    # Should find video1.mp4 and video2.MKV, but NOT sub/video3.mkv
    assert len(non_rec_discovered) == 2
    assert "video1.mp4" in paths_non_rec
    assert "video2.MKV" in paths_non_rec
    
    # Check that they are sorted
    assert paths_non_rec == sorted(paths_non_rec)

    # Recursive discovery
    rec_discovered = discover_inputs([str(tmp_path)], recursive=True)
    paths_rec = [os.path.basename(p[0]) for p in rec_discovered]
    assert len(rec_discovered) == 3
    assert "video1.mp4" in paths_rec
    assert "video2.MKV" in paths_rec
    assert "video3.mkv" in paths_rec

def test_discover_inputs_individual_files(tmp_path):
    f1 = tmp_path / "clip.mp4"
    f1.write_text("dummy")
    f2 = tmp_path / "ignore.txt"
    f2.write_text("dummy")

    # Pass individual file paths
    discovered = discover_inputs([str(f1)], recursive=False)
    assert len(discovered) == 1
    assert discovered[0][0] == os.path.abspath(str(f1))

    # Pass non-existent path raises FileNotFoundError
    with pytest.raises(FileNotFoundError):
        discover_inputs([str(tmp_path / "ghost.mp4")], recursive=False)

@patch("upscaler.batch.run_single_file")
@patch("upscaler.batch.probe_video")
@patch("upscaler.batch.discover_inputs")
def test_run_batch_skip_existing(mock_discover, mock_probe, mock_run_single, tmp_path):
    # Two files discovered
    infile1 = str(tmp_path / "in1.mp4")
    infile2 = str(tmp_path / "in2.mp4")
    
    # Mock output paths: alongside sources
    outfile1 = str(tmp_path / "in1_1080p.mp4")
    outfile2 = str(tmp_path / "in2_1080p.mp4")
    
    # Touch outfile1 to simulate existing output
    Path(outfile1).write_text("dummy")
    
    mock_discover.return_value = [
        (infile1, infile1),
        (infile2, infile2)
    ]
    
    # Mock probe: outfile1 is valid (frame_count > 0)
    mock_out_info = MagicMock()
    mock_out_info.frame_count = 100
    mock_probe.return_value = mock_out_info
    
    opts = {
        "preset": "1080p",
        "output": None,
        "output_dir": None,
        "recursive": False,
        "fail_fast": False
    }
    tools_info = {}
    
    res = run_batch([str(tmp_path)], opts, tools_info)
    assert res == 0
    
    # Should only run run_single_file for input 2, and skip input 1
    mock_run_single.assert_called_once_with(infile2, outfile2, opts, tools_info)
