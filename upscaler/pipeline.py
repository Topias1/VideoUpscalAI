import json
import os
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import (
    ManifestMismatchError,
    ProbeError,
    ReconciliationError,
    SubprocessError,
    UpscalerError
)
from .probe import probe_video
from .plan import check_preset_guard, check_vfr_mode, check_hdr_mode, estimate_disk_usage, verify_disk_space
from .ffmpeg_cmds import (
    build_split_cmd,
    build_extract_cmd,
    build_realesrgan_cmd,
    build_encode_cmd,
    build_concat_cmd,
    build_remux_cmd
)
from .tools import get_ffprobe_path

def get_exact_frame_count(video_path: str) -> int:
    ffprobe_path = get_ffprobe_path()
    # Try count_packets first (highly accurate for segment chunks)
    try:
        cmd = [
            ffprobe_path,
            "-v", "error",
            "-select_streams", "v:0",
            "-count_packets",
            "-show_entries", "stream=nb_read_packets",
            "-of", "csv=p=0",
            video_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        val = res.stdout.strip()
        if val:
            return int(val)
    except Exception:
        pass
        
    # Fallback to probe
    try:
        info = probe_video(video_path)
        return info.frame_count
    except Exception as e:
        raise ProbeError(f"Could not count frames in {video_path}: {e}")

def run_cmd_checked(cmd: List[str], file_path: str, stage: str, chunk: Optional[str] = None) -> subprocess.CompletedProcess:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            chunk_info = f" chunk {chunk}" if chunk else ""
            msg = (
                f"Subprocess failed in {stage}{chunk_info} for file {file_path}.\n"
                f"Command: {' '.join(cmd)}\n"
                f"Exit code: {res.returncode}\n"
                f"Stderr: {res.stderr}\n"
                f"Stdout: {res.stdout}"
            )
            raise SubprocessError(msg)
        return res
    except Exception as e:
        if isinstance(e, SubprocessError):
            raise
        chunk_info = f" chunk {chunk}" if chunk else ""
        raise SubprocessError(
            f"Failed to execute command for {stage}{chunk_info} for file {file_path}: {e}"
        )

def run_realesrgan_stream(cmd: List[str], file_path: str, chunk: str, show_progress: bool = True) -> None:
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )
        
        # Buffer to collect stderr characters for progress parsing
        buffer = []
        while True:
            char = proc.stderr.read(1)
            if not char:
                break
            if char in ("\r", "\n"):
                line = "".join(buffer).strip()
                buffer.clear()
                if show_progress and line and "%" in line:
                    try:
                        val_str = line.split("%")[0].strip().split()[-1]
                        pct = float(val_str)
                        pct = max(0.0, min(100.0, pct))
                        bar_len = 25
                        filled_len = int(round(bar_len * pct / 100))
                        bar = "█" * filled_len + "░" * (bar_len - filled_len)
                        print(f"\r  [realesrgan] {chunk}: [{bar}] {pct:.2f}%", end="", flush=True)
                    except Exception:
                        print(f"\r  [realesrgan] {chunk} progress: {line}", end="", flush=True)
            else:
                buffer.append(char)
                
        proc.wait()
        # Print a final newline to clear the progress line
        if show_progress:
            print()
        
        if proc.returncode != 0:
            stderr_left = proc.stderr.read()
            stdout_left = proc.stdout.read()
            raise SubprocessError(
                f"realesrgan failed with exit code {proc.returncode} for chunk {chunk}.\n"
                f"Command: {' '.join(cmd)}\n"
                f"Stderr: {stderr_left}\n"
                f"Stdout: {stdout_left}"
            )
    except Exception as e:
        if isinstance(e, SubprocessError):
            raise
        raise SubprocessError(f"Failed to execute realesrgan for chunk {chunk}: {e}")

def load_or_create_manifest(
    manifest_path: str,
    source_path: str,
    resolved_params: Dict[str, Any],
    info: VideoInfo
) -> Dict[str, Any]:
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r") as f:
                manifest = json.load(f)
            
            # Check for mismatch in resolved parameters
            old_params = manifest.get("resolved_params", {})
            for k, v in resolved_params.items():
                if old_params.get(k) != v:
                    raise ManifestMismatchError(
                        f"Parameter mismatch for parameter '{k}' on resume.\n"
                        f"Stored in manifest: {old_params.get(k)}\n"
                        f"Current parameters: {v}\n"
                        f"Please use a fresh work directory or delete the existing manifest.json."
                    )
            return manifest
        except (json.JSONDecodeError, KeyError) as e:
            if isinstance(e, ManifestMismatchError):
                raise
            # Stale/corrupt manifest, overwrite it
            pass

    manifest = {
        "source_path": os.path.abspath(source_path),
        "resolved_params": resolved_params,
        "probe": {
            "width": info.width,
            "height": info.height,
            "fps": info.fps,
            "frame_count": info.frame_count,
            "is_hdr": info.is_hdr,
            "is_vfr": info.is_vfr,
            "color_transfer": info.color_transfer,
            "color_primaries": info.color_primaries,
        },
        "status": "in_progress",
        "chunks": {}
    }
    save_manifest(manifest_path, manifest)
    return manifest

def save_manifest(manifest_path: str, manifest: Dict[str, Any]) -> None:
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

def run_single_file(
    input_path: str,
    output_path: str,
    opts: Dict[str, Any],
    tools_info: Dict[str, Any]
) -> None:
    """Runs the upscale pipeline on a single file."""
    input_abs = os.path.abspath(input_path)
    output_abs = os.path.abspath(output_path)
    
    # 1. Probe input
    info = probe_video(input_abs)
    
    # Auto-detect model if 'auto' is selected
    if opts.get("model") == "auto":
        from .probe import detect_video_type
        v_type = detect_video_type(input_abs)
        is_upscayl = "upscayl" in os.path.basename(tools_info["realesrgan_path"]).lower()
        if v_type == "animation":
            opts["model"] = "digital-art-4x" if is_upscayl else "realesr-animevideov3"
        else:
            opts["model"] = "upscayl-standard-4x" if is_upscayl else "realesrgan-x4plus"
        print(f"Auto-detected content type: {v_type}. Using model: {opts['model']}")

    # 2. Preset Guard & VFR/HDR validation
    check_preset_guard(info.height, opts["preset"])
    check_vfr_mode(info.is_vfr, opts["vfr_mode"])
    check_hdr_mode(info.is_hdr, opts["hdr_mode"])
    
    # If HDR, verify ffmpeg supports zscale and tonemap
    if info.is_hdr and opts["hdr_mode"] == "tonemap":
        if not tools_info["has_zscale"] or not tools_info["has_tonemap"]:
            raise ProbeError(
                f"Source is HDR and --hdr-mode tonemap is selected, but your ffmpeg "
                f"is missing the required 'zscale' (libzimg) or 'tonemap' filter. "
                f"Please install a build of ffmpeg that includes libzimg (e.g. via Homebrew on macOS)."
            )

    # 3. Setup work directory
    preset = opts["preset"]
    if opts.get("work_dir"):
        work_dir = os.path.abspath(opts["work_dir"])
    else:
        # Default work dir: .work_<stem>_<preset> inside current working directory
        # If running from a double-clicked macOS bundle, CWD is / (read-only), so we fallback to user home directory.
        in_path = Path(input_abs)
        cwd = os.getcwd()
        if cwd == "/" or not os.access(cwd, os.W_OK):
            work_dir_parent = os.path.expanduser("~")
        else:
            work_dir_parent = cwd
        work_dir = os.path.abspath(os.path.join(work_dir_parent, f".work_{in_path.stem}_{preset}"))
        
    os.makedirs(work_dir, exist_ok=True)
    manifest_path = os.path.join(work_dir, "manifest.json")
    
    # Verify disk space
    disk_est = estimate_disk_usage(info, preset, opts["chunk_seconds"])
    file_size = os.path.getsize(input_abs)
    verify_disk_space(work_dir, disk_est["peak_transient_bytes"], file_size)

    # Load/Create manifest
    resolved_params = {
        "preset": preset,
        "model": opts["model"],
        "quality": opts["quality"],
        "encoder": opts["encoder"],
        "bitrate": opts.get("bitrate"),
        "hdr_mode": opts["hdr_mode"],
        "vfr_mode": opts["vfr_mode"],
        "chunk_seconds": opts["chunk_seconds"]
    }
    manifest = load_or_create_manifest(manifest_path, input_abs, resolved_params, info)

    # 4. Pre-split source if seg_in is empty or not present
    seg_in_dir = os.path.join(work_dir, "seg_in")
    os.makedirs(seg_in_dir, exist_ok=True)
    
    # Find segment files in seg_in
    segments = sorted([
        os.path.join(seg_in_dir, f) for f in os.listdir(seg_in_dir)
        if f.startswith("seg_") and f.endswith(".mkv")
    ])
    
    if not segments:
        print(f"Pre-splitting source into keyframe-aligned segments...")
        split_cmd = build_split_cmd(input_abs, work_dir, opts["chunk_seconds"])
        run_cmd_checked(split_cmd, input_abs, "pre-split")
        
        segments = sorted([
            os.path.join(seg_in_dir, f) for f in os.listdir(seg_in_dir)
            if f.startswith("seg_") and f.endswith(".mkv")
        ])
        if not segments:
            raise ProbeError("Pre-splitting completed but no segments were created.")

    # Create seg_out directory
    seg_out_dir = os.path.join(work_dir, "seg_out")
    os.makedirs(seg_out_dir, exist_ok=True)

    # Setup transient folders
    frames_dir = os.path.join(work_dir, "frames")
    up_dir = os.path.join(work_dir, "up")

    # TQDM progress setup
    has_tqdm = False
    try:
        from tqdm import tqdm
        has_tqdm = True
    except ImportError:
        pass

    # 5. Process each segment
    total_segments = len(segments)
    concat_list_path = os.path.join(work_dir, "concat_list.txt")
    
    manifest_lock = threading.Lock()
    print_lock = threading.Lock()
    workers = opts.get("workers", 1)
    
    def process_segment(i: int) -> None:
        seg_path = segments[i]
        seg_name = os.path.basename(seg_path)
        seg_stem = Path(seg_path).stem
        out_seg_path = os.path.join(seg_out_dir, f"{seg_stem}.mp4")
        
        frames_dir = os.path.join(work_dir, f"frames_{seg_stem}")
        up_dir = os.path.join(work_dir, f"up_{seg_stem}")
        
        # Try to validate if already completed
        skip_chunk = False
        with manifest_lock:
            already_completed = manifest["chunks"].get(seg_name) == "completed"
            
        if already_completed and os.path.exists(out_seg_path):
            try:
                in_count = get_exact_frame_count(seg_path)
                out_count = get_exact_frame_count(out_seg_path)
                if in_count == out_count and out_count > 0:
                    skip_chunk = True
                    if workers == 1:
                        print(f"Segment {i+1}/{total_segments}: {seg_name} (Skipped - already completed)")
            except Exception:
                pass

        if not skip_chunk:
            if workers > 1:
                with print_lock:
                    print(f"Segment {i+1}/{total_segments}: {seg_name} starting...")
            elif not has_tqdm:
                print(f"Segment {i+1}/{total_segments}: {seg_name}")

            # Wipe frames and up dirs
            if os.path.exists(frames_dir):
                shutil.rmtree(frames_dir)
            if os.path.exists(up_dir):
                shutil.rmtree(up_dir)
            os.makedirs(frames_dir, exist_ok=True)
            os.makedirs(up_dir, exist_ok=True)

            # Get segment frame count for reconciliation
            seg_frame_count = get_exact_frame_count(seg_path)

            # Stage 1: Extract frames
            extract_cmd = build_extract_cmd(
                seg_path,
                frames_dir,
                info.fps,
                info.is_hdr,
                opts["hdr_mode"],
                info.is_vfr,
                opts["vfr_mode"]
            )
            run_cmd_checked(extract_cmd, input_abs, "frame extraction", seg_name)

            # Reconcile extracted PNGs
            png_files = [f for f in os.listdir(frames_dir) if f.endswith(".png")]
            png_count = len(png_files)
            if png_count != seg_frame_count:
                raise ReconciliationError(
                    f"Frame extraction reconciliation mismatch for {seg_name}.\n"
                    f"Segment frame count: {seg_frame_count}\n"
                    f"Extracted PNG count: {png_count}\n"
                    f"This typically indicates frame drops due to open-GOP seeking."
                )

            # Stage 2: Upscale 4x
            real_cmd = build_realesrgan_cmd(
                tools_info["realesrgan_path"],
                frames_dir,
                up_dir,
                opts["model"],
                opts["jobs"],
                model_path=opts.get("model_path")
            )
            run_realesrgan_stream(real_cmd, input_abs, seg_name, show_progress=(workers == 1))

            # Verify upscale PNGs match input count
            up_png_files = [f for f in os.listdir(up_dir) if f.endswith(".png")]
            up_png_count = len(up_png_files)
            if up_png_count != png_count:
                raise ReconciliationError(
                    f"Upscale frame reconciliation mismatch for {seg_name}.\n"
                    f"Expected upscaled PNG count: {png_count}\n"
                    f"Actual upscaled PNG count: {up_png_count}"
                )

            # Stage 3: Scale down to preset and encode
            input_pattern = os.path.join(up_dir, "f_%08d.png")
            encode_cmd = build_encode_cmd(
                input_pattern,
                out_seg_path,
                info.fps,
                preset,
                opts["encoder"],
                opts["quality"],
                opts.get("bitrate"),
                interpolate_fps=opts.get("interpolate_fps"),
                temporal_denoise=opts.get("temporal_denoise", False)
            )
            run_cmd_checked(encode_cmd, input_abs, "re-encode", seg_name)

            # Verify encoded frame count
            encoded_frame_count = get_exact_frame_count(out_seg_path)
            if encoded_frame_count != png_count:
                raise ReconciliationError(
                    f"Encoder frame reconciliation mismatch for {seg_name}.\n"
                    f"Expected frame count: {png_count}\n"
                    f"Encoded frame count: {encoded_frame_count}"
                )

            # Update manifest under lock
            with manifest_lock:
                manifest["chunks"][seg_name] = "completed"
                save_manifest(manifest_path, manifest)

            # Stage 4: Clean up frames & up dirs
            if os.path.exists(frames_dir):
                shutil.rmtree(frames_dir)
            if os.path.exists(up_dir):
                shutil.rmtree(up_dir)
                
            if workers > 1:
                with print_lock:
                    print(f"Segment {i+1}/{total_segments}: {seg_name} completed.")

    # Execute sequentially or in parallel
    if workers > 1:
        print(f"Processing {total_segments} segments in parallel using {workers} workers...")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(process_segment, i) for i in range(total_segments)]
            # Re-raise any thread exceptions
            for fut in futures:
                fut.result()
    else:
        iterator = range(total_segments)
        if has_tqdm:
            iterator = tqdm(iterator, desc="Processing segments")
        for i in iterator:
            process_segment(i)

    # 6. Finalization
    # Write concat list file
    with open(concat_list_path, "w") as f:
        for seg_path in segments:
            seg_stem = Path(seg_path).stem
            out_seg_file = os.path.join(seg_out_dir, f"{seg_stem}.mp4")
            abs_path = os.path.abspath(out_seg_file)
            escaped_path = abs_path.replace("'", "'\\''")
            f.write(f"file '{escaped_path}'\n")

    video_only_path = os.path.join(work_dir, "video_only.mp4")
    print("Concatenating segments...")
    concat_cmd = build_concat_cmd(concat_list_path, video_only_path)
    run_cmd_checked(concat_cmd, input_abs, "concat segments")

    # Re-mux audio, subtitles, metadata, chapters
    # We must probe original video streams to build the remux command
    # ffprobe JSON parsing (since we need full stream structures)
    ffprobe_path = get_ffprobe_path()
    probe_cmd = [
        ffprobe_path,
        "-v", "error",
        "-print_format", "json",
        "-show_streams",
        input_abs
    ]
    try:
        res = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
        raw_info = json.loads(res.stdout)
        streams = raw_info.get("streams", [])
    except Exception as e:
        raise ProbeError(f"Failed to probe streams for remux: {e}")

    print("Remuxing final video with audio/subtitles/metadata...")
    remux_cmd, warnings = build_remux_cmd(
        video_only_path,
        input_abs,
        output_abs,
        streams
    )
    for warning in warnings:
        print(f"WARNING: {warning}")
        
    # Ensure parent output directory exists
    os.makedirs(os.path.dirname(output_abs), exist_ok=True)
    run_cmd_checked(remux_cmd, input_abs, "remux final output")

    # 7. Clean up work dir if requested
    if not opts.get("keep_work", False):
        print(f"Cleaning up work directory: {work_dir}")
        shutil.rmtree(work_dir)
    else:
        # Mark manifest as completed
        manifest["status"] = "completed"
        save_manifest(manifest_path, manifest)
        
    print(f"Successfully upscaled: {input_abs} -> {output_abs}")
