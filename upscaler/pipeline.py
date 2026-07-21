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
from .probe import VideoInfo, parse_ffprobe_scalar_int, probe_video
from . import progress as progress_events
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
        val = parse_ffprobe_scalar_int(res.stdout)
        if val is not None and val > 0:
            return val
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

def is_source_degraded(info: VideoInfo) -> bool:
    """Is this footage damaged enough to warrant aggressive restoration?

    Resolution is the discriminator, not bitrate: modern codecs are efficient
    enough that pristine 4K can sit *below* a VHS capture in bits per pixel
    (measured: 0.107 vs 0.170), so bitrate ranks the two backwards. The short
    side is what counts, so portrait clips are judged on their real detail
    level rather than their tall dimension.
    """
    return min(info.width, info.height) <= 720


def select_auto_model(info: VideoInfo, video_type: str, is_upscayl: bool) -> str:
    """Pick a model in --model auto, from content type and source condition.

    Measured across real material: on genuinely degraded footage (a 1999 PAL
    camcorder capture, 480p social-network exports) the aggressive
    upscayl-standard-4x restores clearly more detail, because there is real
    degradation to invert — which is what these networks are trained on. On a
    clean sharp source there is nothing to undo and that same aggressiveness
    turns into invented texture; there high-fidelity-4x scored higher and
    stayed noticeably steadier from frame to frame.
    """
    if video_type == "animation":
        return "digital-art-4x" if is_upscayl else "realesr-animevideov3"
    if not is_upscayl:
        return "realesrgan-x4plus"
    return "upscayl-standard-4x" if is_source_degraded(info) else "high-fidelity-4x"


def run_realesrgan_stream(
    cmd: List[str],
    file_path: str,
    chunk: str,
    show_progress: bool = True,
    up_dir: Optional[str] = None,
    total_frames: Optional[int] = None
) -> None:
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
        last_pct = -1.0
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
                        val_str = val_str.replace(",", ".")
                        pct = float(val_str)
                        pct = max(0.0, min(100.0, pct))
                        
                        if up_dir and total_frames and total_frames > 0:
                            try:
                                files_upscaled = len([f for f in os.listdir(up_dir) if f.endswith(".png")])
                            except Exception:
                                files_upscaled = 0
                            overall_pct = ((files_upscaled + (pct / 100.0)) / total_frames) * 100.0
                            overall_pct = max(0.0, min(100.0, overall_pct))
                            pct = overall_pct
                            
                        if pct - last_pct >= 0.5 or pct == 100.0 or last_pct < 0:
                            last_pct = pct
                            progress_events.emit(
                                t="seg",
                                seg=chunk,
                                stage="upscale",
                                pct=progress_events.segment_pct("upscale", pct),
                            )
                            # The \r-redrawn bar would be interleaved into the
                            # event stream (and is redundant once a GUI renders
                            # its own per-chunk bars), so emit one or the other.
                            if not progress_events.events_enabled():
                                bar_len = 25
                                filled_len = int(round(bar_len * pct / 100))
                                bar = "█" * filled_len + "░" * (bar_len - filled_len)
                                print(f"\r  [realesrgan] {chunk}: [{bar}] {pct:.2f}%", end="", flush=True)
                    except Exception:
                        if not progress_events.events_enabled():
                            print(f"\r  [realesrgan] {chunk} progress: {line}", end="", flush=True)
            else:
                buffer.append(char)
                
        proc.wait()
        # Print a final newline to clear the progress line
        if show_progress and not progress_events.events_enabled():
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
            mismatch = False
            for k, v in resolved_params.items():
                if old_params.get(k) != v:
                    print(f"WARNING: Parameter mismatch for parameter '{k}' on resume (Stored: {old_params.get(k)}, Current: {v}).")
                    mismatch = True
                    break
            
            if mismatch:
                print("Clearing stale work directory to start a fresh run.")
                import shutil
                work_dir_parent = os.path.dirname(manifest_path)
                for sub in ("seg_in", "seg_out", "frames", "up"):
                    p = os.path.join(work_dir_parent, sub)
                    if os.path.exists(p):
                        try:
                            shutil.rmtree(p)
                        except Exception:
                            pass
                try:
                    os.remove(manifest_path)
                except Exception:
                    pass
            else:
                return manifest
        except (json.JSONDecodeError, KeyError) as e:
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
    
    # Auto-detect or translate model name to match binary capabilities
    is_upscayl = "upscayl" in os.path.basename(tools_info["realesrgan_path"]).lower()
    if opts.get("model") == "auto":
        from .probe import detect_video_type
        v_type = detect_video_type(input_abs)
        opts["model"] = select_auto_model(info, v_type, is_upscayl)
        condition = "degraded" if is_source_degraded(info) else "clean"
        print(f"Auto-detected content type: {v_type}, source "
              f"{info.width}x{info.height} ({condition}). "
              f"Using model: {opts['model']}")
    else:
        # Translate manual model name to match binary capabilities (Upscayl vs Standard)
        model = opts.get("model")
        if is_upscayl:
            if model in ("realesrgan-x4plus", "realesrgan-x4plus-anime"):
                opts["model"] = "upscayl-standard-4x"
            elif model == "realesr-animevideov3":
                opts["model"] = "upscayl-lite-4x"
        else:
            if model in ("upscayl-standard-4x", "high-fidelity-4x"):
                opts["model"] = "realesrgan-x4plus"
            elif model in ("upscayl-lite-4x", "digital-art-4x"):
                opts["model"] = "realesr-animevideov3"
            elif model == "ultrasharp-4x":
                opts["model"] = "realesrgan-x4plus"
        print(f"Using mapped model: {opts['model']}")

    # 2. Preset Guard & VFR/HDR validation
    check_preset_guard(info.height, opts["preset"])
    check_vfr_mode(info.is_vfr, opts["vfr_mode"])
    check_hdr_mode(info.is_hdr, opts["hdr_mode"])
    
    # If HDR, verify ffmpeg supports zscale and tonemap
    if info.is_hdr and opts["hdr_mode"] == "tonemap":
        if not tools_info["has_zscale"] or not tools_info["has_tonemap"]:
            # The bundled ffmpeg is the only one ever used (see tools.py), so
            # telling the user to install another build was a dead end.
            raise ProbeError(
                "This video is HDR (HLG/PQ, as recorded by recent iPhones) and "
                "the bundled ffmpeg lacks the 'zscale' filter needed to convert "
                "it to SDR correctly.\n"
                "Workarounds, in order of quality:\n"
                "  1. Convert the source to SDR first, with an app that supports "
                "HDR (e.g. export from Photos or Final Cut as Rec.709).\n"
                "  2. Re-run with --hdr-mode passthrough to upscale without "
                "converting: colours will look washed out on an SDR screen.\n"
                "Note: installing ffmpeg separately does not help — this app "
                "always uses its own bundled copy."
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
    
    # Verify disk space. The estimate covers one segment, but every worker
    # holds its own frame set (raw + upscaled PNGs) at the same time, so the
    # real peak scales with the worker count.
    disk_est = estimate_disk_usage(info, preset, opts["chunk_seconds"])
    concurrent_segments = max(1, int(opts.get("workers", 1)))
    peak_transient = disk_est["peak_transient_bytes"] * concurrent_segments
    file_size = os.path.getsize(input_abs)
    verify_disk_space(work_dir, peak_transient, file_size)

    # Load/Create manifest
    resolved_params = {
        "preset": preset,
        "model": opts["model"],
        "quality": opts["quality"],
        "encoder": opts["encoder"],
        "bitrate": opts.get("bitrate"),
        "hdr_mode": opts["hdr_mode"],
        "vfr_mode": opts["vfr_mode"],
        "chunk_seconds": opts["chunk_seconds"],
        "interpolate_fps": opts.get("interpolate_fps"),
        "temporal_denoise": opts.get("temporal_denoise")
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
    # Displayed width the output must reach. Computed here, from the probed
    # source, because the upscaled frames have lost the pixel aspect ratio by
    # the time they reach the encoder.
    from .plan import get_target_height
    _target_h = get_target_height(preset)
    target_width = None
    if info.display_aspect:
        target_width = int(round(_target_h * info.display_aspect / 2)) * 2

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
    gpu_lock = threading.Lock()
    workers = opts.get("workers", 1)

    progress_events.emit(t="segs", total=total_segments, workers=min(workers, total_segments))
    
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
                    progress_events.emit(t="seg_done", seg=seg_name, idx=i + 1)
                    if workers == 1:
                        print(f"Segment {i+1}/{total_segments}: {seg_name} (Skipped - already completed)")
            except Exception:
                pass

        if not skip_chunk:
            progress_events.emit(t="seg", seg=seg_name, idx=i + 1, stage="extract", pct=0.0)
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
            # CFR conformance (--vfr-mode cfr) resamples via the fps= filter, so
            # the extracted PNG count legitimately differs from the source segment
            # frame count. The resampled count is authoritative; downstream stages
            # reconcile against png_count. Only require a non-empty extraction here.
            cfr_conformed = info.is_vfr and opts["vfr_mode"] == "cfr"
            if cfr_conformed:
                if png_count == 0:
                    raise ReconciliationError(
                        f"Frame extraction produced no PNGs for {seg_name} "
                        f"(segment frame count: {seg_frame_count})."
                    )
            elif png_count != seg_frame_count:
                raise ReconciliationError(
                    f"Frame extraction reconciliation mismatch for {seg_name}.\n"
                    f"Segment frame count: {seg_frame_count}\n"
                    f"Extracted PNG count: {png_count}\n"
                    f"This typically indicates frame drops due to open-GOP seeking."
                )

            progress_events.emit(
                t="seg",
                seg=seg_name,
                idx=i + 1,
                stage="upscale",
                pct=progress_events.segment_pct("upscale", 0.0),
            )

            # Stage 2: Upscale 4x
            with gpu_lock:
                real_cmd = build_realesrgan_cmd(
                    tools_info["realesrgan_path"],
                    frames_dir,
                    up_dir,
                    opts["model"],
                    opts["jobs"],
                    model_path=opts.get("model_path")
                )
                run_realesrgan_stream(
                    real_cmd,
                    input_abs,
                    seg_name,
                    show_progress=True,
                    up_dir=up_dir,
                    total_frames=png_count
                )

            # Verify upscale PNGs match input count
            up_png_files = [f for f in os.listdir(up_dir) if f.endswith(".png")]
            up_png_count = len(up_png_files)
            if up_png_count != png_count:
                raise ReconciliationError(
                    f"Upscale frame reconciliation mismatch for {seg_name}.\n"
                    f"Expected upscaled PNG count: {png_count}\n"
                    f"Actual upscaled PNG count: {up_png_count}"
                )

            progress_events.emit(
                t="seg",
                seg=seg_name,
                idx=i + 1,
                stage="encode",
                pct=progress_events.segment_pct("encode", 0.0),
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
                temporal_denoise=opts.get("temporal_denoise", False),
                target_w=target_width
            )
            run_cmd_checked(encode_cmd, input_abs, "re-encode", seg_name)

            # Verify encoded frame count
            encoded_frame_count = get_exact_frame_count(out_seg_path)
            # Frame interpolation (--interpolate-fps) intentionally resamples the
            # output to a new framerate, so the encoded count legitimately differs
            # from png_count. Only require a non-empty encode in that case.
            if opts.get("interpolate_fps"):
                if encoded_frame_count <= 0:
                    raise ReconciliationError(
                        f"Encoder produced no frames for {seg_name} "
                        f"(input PNG count: {png_count})."
                    )
            elif encoded_frame_count != png_count:
                raise ReconciliationError(
                    f"Encoder frame reconciliation mismatch for {seg_name}.\n"
                    f"Expected frame count: {png_count}\n"
                    f"Encoded frame count: {encoded_frame_count}"
                )

            # Update manifest under lock
            with manifest_lock:
                manifest["chunks"][seg_name] = "completed"
                save_manifest(manifest_path, manifest)

            progress_events.emit(t="seg_done", seg=seg_name, idx=i + 1)

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
