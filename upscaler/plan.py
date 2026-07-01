import os
import shutil
from fractions import Fraction
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import PresetGuardError, VFRError, HDRError, DiskEstimateError
from .probe import VideoInfo

PRESETS: Dict[str, int] = {
    "720p": 720,
    "1080p": 1080,
    "4k": 2160,
}

def get_target_height(preset: str) -> int:
    if preset not in PRESETS:
        raise ValueError(f"Unknown preset: {preset}")
    return PRESETS[preset]

def check_preset_guard(source_height: int, preset: str) -> None:
    target_height = get_target_height(preset)
    if source_height >= target_height:
        raise PresetGuardError(
            f"Source height ({source_height}) is already greater than or equal to the target preset '{preset}' height ({target_height}). "
            f"Please select a larger preset (e.g., 4k if upscaling 1080p)."
        )

def check_vfr_mode(is_vfr: bool, vfr_mode: str) -> None:
    if is_vfr and vfr_mode == "error":
        raise VFRError(
            "Source video has variable-frame-rate (VFR) and --vfr-mode is set to 'error'."
        )

def check_hdr_mode(is_hdr: bool, hdr_mode: str) -> None:
    if is_hdr and hdr_mode == "error":
        raise HDRError(
            "Source video is HDR and --hdr-mode is set to 'error'."
        )

def resolve_output_paths(
    resolved_inputs: List[Tuple[str, str]],  # List of (resolved_filepath, root_dir_or_file)
    output: Optional[str],
    output_dir: Optional[str],
    preset: str
) -> List[Tuple[str, str]]:
    """Resolves output paths for all input files, validating constraints and detecting collisions.
    
    Returns a list of (input_path, output_path).
    Raises ValueError on invalid options or collisions.
    """
    if len(resolved_inputs) == 0:
        return []

    # If output is specified, it's only valid if there's exactly 1 input resolved
    if output:
        if len(resolved_inputs) > 1:
            raise ValueError(
                f"-o/--output can only be used when upscaling a single file. "
                f"Resolved {len(resolved_inputs)} files. Use --output-dir instead."
            )
        return [(resolved_inputs[0][0], os.path.abspath(output))]

    output_mappings: List[Tuple[str, str]] = []
    seen_outputs = {}

    for infile, root in resolved_inputs:
        infile_path = Path(infile)
        stem = infile_path.stem
        ext = ".mp4"  # Always produce .mp4 output as per spec

        if output_dir:
            out_dir_path = Path(output_dir)
            if root and root != infile:
                try:
                    # We can mirror the relative path
                    rel_path = infile_path.relative_to(root)
                    # Reconstruct output path under output_dir
                    out_path = out_dir_path / rel_path.parent / f"{stem}_{preset}{ext}"
                except ValueError:
                    out_path = out_dir_path / f"{stem}_{preset}{ext}"
            else:
                out_path = out_dir_path / f"{stem}_{preset}{ext}"
        else:
            out_path = infile_path.parent / f"{stem}_{preset}{ext}"

        out_abs = os.path.abspath(str(out_path))
        if out_abs in seen_outputs:
            raise ValueError(
                f"Output path collision detected: both '{infile}' and '{seen_outputs[out_abs]}' "
                f"map to output '{out_abs}'."
            )
        seen_outputs[out_abs] = infile
        output_mappings.append((infile, out_abs))

    return output_mappings

def estimate_disk_usage(
    info: VideoInfo,
    preset: str,
    chunk_seconds: float,
    scale_factor: float = 4.0
) -> Dict[str, int]:
    """Estimates disk usage requirements (in bytes) for processing a chunk of chunk_seconds.
    
    Returns a dict with:
    - 'chunk_raw_frames_bytes': memory/disk for original frames
    - 'chunk_up_frames_bytes': memory/disk for upscaled frames
    - 'peak_transient_bytes': maximum space needed at any one time (chunk_raw + chunk_up)
    - 'steady_state_bytes': space for seg_in, seg_out, and final output (roughly 2x movie size)
    """
    fps = float(Fraction(info.fps))
    frames_per_chunk = int(round(fps * chunk_seconds))

    # Pixel sizes (3 bytes per pixel as uncompressed RGB/PNG safety limit)
    raw_frame_size = info.width * info.height * 3
    
    # Target scale dimensions
    target_height = get_target_height(preset)
    aspect_ratio = info.width / info.height
    target_width = int(round(target_height * aspect_ratio))
    # Make width even
    if target_width % 2 != 0:
        target_width += 1

    # realesrgan always upscales 4x from raw frames
    up_width = int(info.width * scale_factor)
    up_height = int(info.height * scale_factor)
    up_frame_size = up_width * up_height * 3

    chunk_raw_frames_bytes = frames_per_chunk * raw_frame_size
    chunk_up_frames_bytes = frames_per_chunk * up_frame_size
    peak_transient_bytes = chunk_raw_frames_bytes + chunk_up_frames_bytes

    # Steady state is:
    # 1. seg_in/ directory which holds the copy of the input video (no audio/subtitles)
    # 2. seg_out/ directory which accumulates the upscaled segment mp4 files (about the same size as final video)
    # 3. video_only.mp4 and final output.mp4
    # We estimate based on a safe bitrate fallback or raw source file size.
    # Let's say steady state is roughly 3x the original video file size.
    # We don't have the original file size here directly, but we can approximate it.
    # Since we can check the file size in the caller, we'll return the transient size here.
    return {
        "chunk_raw_frames_bytes": chunk_raw_frames_bytes,
        "chunk_up_frames_bytes": chunk_up_frames_bytes,
        "peak_transient_bytes": peak_transient_bytes,
    }

def verify_disk_space(
    work_dir: str,
    estimated_bytes: int,
    file_size_bytes: int
) -> None:
    """Verifies that the workspace partition has enough free space for the run."""
    # We need: peak_transient_bytes + steady_state (approx 2.5 * file_size_bytes)
    required_bytes = estimated_bytes + int(2.5 * file_size_bytes)
    
    total, used, free = shutil.disk_usage(work_dir)
    if free < required_bytes:
        raise DiskEstimateError(
            f"Low disk space on workspace drive. Estimated requirement: {required_bytes / (1024**3):.2f} GB. "
            f"Available space: {free / (1024**3):.2f} GB."
        )
