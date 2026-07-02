import os
from typing import Any, Dict, List, Optional, Tuple

from .encoders import get_encoder_args
from .tools import get_ffmpeg_path

def build_split_cmd(
    input_path: str,
    work_dir: str,
    chunk_seconds: float
) -> List[str]:
    """Builds the ffmpeg command to pre-split the source into keyframe-aligned segments."""
    seg_in_dir = os.path.join(work_dir, "seg_in")
    out_pattern = os.path.join(seg_in_dir, "seg_%04d.mkv")
    
    return [
        get_ffmpeg_path(),
        "-y",
        "-i", input_path,
        "-an", "-sn", "-dn",
        "-c", "copy",
        "-f", "segment",
        "-segment_time", str(chunk_seconds),
        "-reset_timestamps", "1",
        out_pattern
    ]

def build_extract_cmd(
    segment_path: str,
    frames_dir: str,
    fps: str,
    is_hdr: bool,
    hdr_mode: str,
    is_vfr: bool,
    vfr_mode: str
) -> List[str]:
    """Builds the ffmpeg command to extract frames from a segment, injecting VFR/HDR filters."""
    out_pattern = os.path.join(frames_dir, "f_%08d.png")

    filters: List[str] = []
    
    # 1. VFR CFR conformance
    if is_vfr and vfr_mode == "cfr":
        filters.append(f"fps={fps}")
        
    # 2. HDR tonemapping
    if is_hdr and hdr_mode == "tonemap":
        filters.append("zscale=t=linear:npl=100,tonemap=tonemap=hable,zscale=p=bt709:t=bt709:m=bt709:r=tv,format=rgb24")

    cmd = [get_ffmpeg_path(), "-y", "-i", segment_path]
    if filters:
        cmd.extend(["-vf", ",".join(filters)])
        
    cmd.extend(["-fps_mode", "passthrough", out_pattern])
    return cmd

def build_realesrgan_cmd(
    realesrgan_bin: str,
    input_dir: str,
    output_dir: str,
    model: str = "realesrgan-x4plus",
    jobs: str = "auto",
    model_path: Optional[str] = None
) -> List[str]:
    """Builds the realesrgan-ncnn-vulkan command to upscale frames."""
    cmd = [
        realesrgan_bin,
        "-i", input_dir,
        "-o", output_dir,
        "-n", model,
        "-s", "4",
        "-f", "png"
    ]
    
    # Use specified model path, otherwise auto-resolve next to binary or in parent directory
    if model_path:
        models_dir = os.path.abspath(model_path)
    else:
        bin_dir = os.path.dirname(os.path.abspath(realesrgan_bin))
        models_dir = os.path.join(bin_dir, "models")
        if not os.path.isdir(models_dir):
            models_dir = os.path.abspath(os.path.join(bin_dir, "..", "models"))
        
    if os.path.isdir(models_dir):
        cmd.extend(["-m", models_dir])

    if jobs != "auto":
        cmd.extend(["-j", jobs])
    return cmd

def build_encode_cmd(
    input_pattern: str,
    output_path: str,
    fps: str,
    preset: str,
    encoder_profile: str,
    quality: int,
    bitrate: Optional[str] = None,
    interpolate_fps: Optional[int] = None,
    temporal_denoise: bool = False
) -> List[str]:
    """Builds the ffmpeg command to encode upscaled PNGs into a segment MP4."""
    filters = []
    
    # 1. Temporal Denoising
    if temporal_denoise:
        filters.append("hqdn3d=1.5:1.5:3:3")
        
    # 2. Frame Rate Interpolation
    if interpolate_fps:
        filters.append(f"framerate=fps={interpolate_fps}")

    # 3. Scaling
    from .plan import get_target_height
    target_h = get_target_height(preset)
    
    if encoder_profile == "vaapi":
        filters.append(f"scale=-2:{target_h}:flags=lanczos,format=nv12,hwupload")
    else:
        filters.append(f"scale=-2:{target_h}:flags=lanczos")
        
    vf_str = ",".join(filters)

    cmd = [
        get_ffmpeg_path(),
        "-y",
        "-framerate", fps,
        "-i", input_pattern,
        "-vf", vf_str
    ]

    # Encoder specific args
    enc_args = get_encoder_args(encoder_profile, preset, quality, bitrate)
    cmd.extend(enc_args)
    
    cmd.append(output_path)
    return cmd

def build_concat_cmd(
    concat_list_path: str,
    output_path: str
) -> List[str]:
    """Builds the ffmpeg command to concatenate segment MP4s."""
    return [
        get_ffmpeg_path(),
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_list_path,
        "-c", "copy",
        output_path
    ]

def build_remux_cmd(
    video_only_path: str,
    original_input_path: str,
    output_path: str,
    streams: List[Dict[str, Any]]
) -> Tuple[List[str], List[str]]:
    """Builds the ffmpeg command to remux upscaled video with original audio/subtitles/metadata.
    
    Returns (cmd, warnings).
    """
    # Base command maps input 0 (video_only) video track, and maps input 1 (original)
    cmd = [
        get_ffmpeg_path(),
        "-y",
        "-i", video_only_path,
        "-i", original_input_path,
        "-map", "0:v:0"
    ]

    warnings: List[str] = []
    
    # We map audio and subtitles explicitly, keeping track of indices
    audio_idx = 0
    sub_idx = 0
    
    # Standard codecs supported in MP4
    mp4_audio_codecs = {"aac", "ac3", "eac3", "mp3", "mp2", "opus", "flac"}

    for stream in streams:
        c_type = stream.get("codec_type")
        c_name = stream.get("codec_name", "")
        s_index = stream.get("index")

        if c_type == "audio":
            cmd.extend(["-map", f"1:{s_index}"])
            if c_name in mp4_audio_codecs:
                cmd.extend([f"-c:a:{audio_idx}", "copy"])
            else:
                # Transcode fallback
                channels = int(stream.get("channels", 2))
                bitrate_kbps = channels * 96
                cmd.extend([f"-c:a:{audio_idx}", "aac", f"-b:a:{audio_idx}", f"{bitrate_kbps}k"])
                warnings.append(
                    f"Audio stream {s_index} uses codec '{c_name}' which is non-standard for MP4. "
                    f"Transcoding to AAC at {bitrate_kbps}k."
                )
            audio_idx += 1

        elif c_type == "subtitle":
            # Check if text-based or image-based
            # Image-based: hdmv_pgs_subtitle, dvd_subtitle, xsub
            is_image_sub = c_name in ("hdmv_pgs_subtitle", "dvd_subtitle", "xsub") or "pgs" in c_name or "dvd" in c_name
            if is_image_sub:
                warnings.append(
                    f"Subtitle stream {s_index} ({c_name}) is image-based and cannot be remuxed into MP4. Dropping."
                )
            else:
                cmd.extend(["-map", f"1:{s_index}"])
                cmd.extend([f"-c:s:{sub_idx}", "mov_text"])
                warnings.append(
                    f"Subtitle stream {s_index} ({c_name}) transcoded to 'mov_text' for MP4 compatibility."
                )
                sub_idx += 1

        elif c_type == "data" or c_type == "attachment":
            warnings.append(
                f"Data/Attachment stream {s_index} ({c_name}) is not supported in MP4. Dropping."
            )

    cmd.extend([
        "-map_metadata", "1",
        "-map_chapters", "1",
        "-c:v", "copy",
        "-movflags", "+faststart",
        output_path
    ])

    return cmd, warnings
