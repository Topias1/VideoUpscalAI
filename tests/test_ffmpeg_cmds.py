import os
from upscaler.ffmpeg_cmds import (
    build_split_cmd,
    build_extract_cmd,
    build_realesrgan_cmd,
    build_encode_cmd,
    build_concat_cmd,
    build_remux_cmd
)

def test_build_split_cmd():
    cmd = build_split_cmd("/path/to/input.mp4", "/work", 12.0)
    assert "ffmpeg" in cmd
    assert "-i" in cmd
    idx = cmd.index("-i")
    assert cmd[idx + 1] == "/path/to/input.mp4"
    assert "-segment_time" in cmd
    idx_time = cmd.index("-segment_time")
    assert cmd[idx_time + 1] == "12.0"
    assert "-reset_timestamps" in cmd
    assert cmd[-1].endswith("seg_%04d.mkv")

def test_build_extract_cmd_sdr_cfr():
    cmd = build_extract_cmd(
        segment_path="/work/seg_in/seg_0000.mkv",
        work_dir="/work",
        fps="24000/1001",
        is_hdr=False,
        hdr_mode="tonemap",
        is_vfr=False,
        vfr_mode="error"
    )
    assert "-vf" not in cmd
    assert "-fps_mode" in cmd
    assert "passthrough" in cmd
    assert cmd[-1].endswith("f_%08d.png")

def test_build_extract_cmd_hdr_tonemap():
    cmd = build_extract_cmd(
        segment_path="/work/seg_in/seg_0000.mkv",
        work_dir="/work",
        fps="24000/1001",
        is_hdr=True,
        hdr_mode="tonemap",
        is_vfr=False,
        vfr_mode="error"
    )
    assert "-vf" in cmd
    idx = cmd.index("-vf")
    vf_val = cmd[idx + 1]
    assert "zscale=t=linear:npl=100" in vf_val
    assert "tonemap=tonemap=hable" in vf_val
    assert "zscale=p=bt709:t=bt709:m=bt709:r=tv,format=rgb24" in vf_val

def test_build_extract_cmd_vfr_conformed():
    cmd = build_extract_cmd(
        segment_path="/work/seg_in/seg_0000.mkv",
        work_dir="/work",
        fps="30000/1001",
        is_hdr=False,
        hdr_mode="tonemap",
        is_vfr=True,
        vfr_mode="cfr"
    )
    assert "-vf" in cmd
    idx = cmd.index("-vf")
    assert cmd[idx + 1] == "fps=30000/1001"

def test_build_extract_cmd_vfr_hdr_conformed():
    cmd = build_extract_cmd(
        segment_path="/work/seg_in/seg_0000.mkv",
        work_dir="/work",
        fps="24/1",
        is_hdr=True,
        hdr_mode="tonemap",
        is_vfr=True,
        vfr_mode="cfr"
    )
    assert "-vf" in cmd
    idx = cmd.index("-vf")
    vf_val = cmd[idx + 1]
    # Should chain fps filter and tonemap filter with comma
    assert vf_val.startswith("fps=24/1,zscale")

def test_build_realesrgan_cmd():
    cmd = build_realesrgan_cmd("/bin/realesrgan", "/work/frames", "/work/up", "realesrgan-x4plus", "1:2:1")
    assert cmd == [
        "/bin/realesrgan",
        "-i", "/work/frames",
        "-o", "/work/up",
        "-n", "realesrgan-x4plus",
        "-s", "4",
        "-f", "png",
        "-j", "1:2:1"
    ]
    
    # Auto jobs should omit the -j flag
    cmd_auto = build_realesrgan_cmd("/bin/realesrgan", "/work/frames", "/work/up", "realesrgan-x4plus", "auto")
    assert "-j" not in cmd_auto

def test_build_encode_cmd():
    cmd = build_encode_cmd(
        input_pattern="/work/up/f_%08d.png",
        output_path="/work/seg_out/seg_0000.mp4",
        fps="24/1",
        preset="1080p",
        encoder_profile="libx265",
        quality=60
    )
    assert "ffmpeg" in cmd
    assert "-framerate" in cmd
    assert "24/1" in cmd
    assert "-vf" in cmd
    idx = cmd.index("-vf")
    assert cmd[idx + 1] == "scale=-2:1080:flags=lanczos"
    assert "libx265" in cmd
    assert "/work/seg_out/seg_0000.mp4" in cmd

def test_build_encode_cmd_vaapi():
    cmd = build_encode_cmd(
        input_pattern="/work/up/f_%08d.png",
        output_path="/work/seg_out/seg_0000.mp4",
        fps="24/1",
        preset="1080p",
        encoder_profile="vaapi",
        quality=60
    )
    idx = cmd.index("-vf")
    # should contain format=nv12,hwupload
    assert cmd[idx + 1] == "scale=-2:1080:flags=lanczos,format=nv12,hwupload"
    assert "-vaapi_device" in cmd

def test_build_concat_cmd():
    cmd = build_concat_cmd("/work/concat_list.txt", "/work/video_only.mp4")
    assert cmd == [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", "/work/concat_list.txt",
        "-c", "copy",
        "/work/video_only.mp4"
    ]

def test_build_remux_cmd():
    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {"index": 1, "codec_type": "audio", "codec_name": "aac", "channels": 2},
        {"index": 2, "codec_type": "audio", "codec_name": "truehd", "channels": 6},
        {"index": 3, "codec_type": "subtitle", "codec_name": "subrip"},
        {"index": 4, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle"},
        {"index": 5, "codec_type": "data", "codec_name": "bin_data"}
    ]
    cmd, warnings = build_remux_cmd(
        video_only_path="/work/video_only.mp4",
        original_input_path="/path/to/input.mp4",
        output_path="/path/to/output.mp4",
        streams=streams
    )
    
    assert "ffmpeg" in cmd
    # Maps video only from input 0
    assert "-map" in cmd
    assert "0:v:0" in cmd
    
    # Mapped Audio 1: AAC (copy)
    assert "1:1" in cmd
    assert "-c:a:0" in cmd
    assert "copy" in cmd
    
    # Mapped Audio 2: TrueHD (aac transcode, channels=6 -> 576k)
    assert "1:2" in cmd
    assert "-c:a:1" in cmd
    assert "aac" in cmd
    assert "-b:a:1" in cmd
    assert "576k" in cmd
    
    # Mapped Subtitle 1: SubRip -> mov_text
    assert "1:3" in cmd
    assert "-c:s:0" in cmd
    assert "mov_text" in cmd
    
    # Subtitle 2: PGS (dropped) -> not mapped
    assert "1:4" not in cmd
    
    # Data 1: bin_data (dropped) -> not mapped
    assert "1:5" not in cmd
    
    # Metadata and chapters mapped
    assert "-map_metadata" in cmd
    assert "-map_chapters" in cmd
    assert "-movflags" in cmd
    assert "+faststart" in cmd
    
    # Warnings about TrueHD transcode, PGS drop, data drop
    assert any("truehd" in w and "AAC" in w for w in warnings)
    assert any("PGS" in w or "hdmv_pgs_subtitle" in w for w in warnings)
    assert any("Data/Attachment" in w or "bin_data" in w for w in warnings)
