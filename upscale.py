#!/usr/bin/env python3
import argparse
import sys
try:
    sys.stdout.reconfigure(write_through=True, line_buffering=True)
    sys.stderr.reconfigure(write_through=True, line_buffering=True)
except Exception:
    pass
from typing import List

from upscaler import UpscalerError, ToolError
from upscaler.tools import verify_tools
from upscaler.batch import run_batch

def main(argv: List[str] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    # Filter out macOS Cocoa and PyInstaller internal environment options
    ignored_args = {"-B", "-S", "-I", "-c"}
    argv = [arg for arg in argv if arg not in ignored_args and not arg.startswith("-psn_")]

    parser = argparse.ArgumentParser(
        description="Apple Silicon Video Upscaler CLI pipeline using Real-ESRGAN-ncnn-vulkan and ffmpeg."
    )
    
    # Input files and directories
    parser.add_argument(
        "inputs",
        nargs="+",
        metavar="INPUT",
        help="One or more video files and/or directories to upscale."
    )
    
    # Output options
    parser.add_argument(
        "-o", "--output",
        dest="output",
        help="Output filepath (only valid when upscaling a single file)."
    )
    parser.add_argument(
        "--output-dir",
        dest="output_dir",
        help="Directory to save upscaled files. Defaults to alongside each source file."
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Descend into subdirectories when input is a directory."
    )
    
    # Upscaling preset & model
    parser.add_argument(
        "--preset",
        choices=["480p", "720p", "1080p", "4k"],
        default="1080p",
        help="Target output preset. Defaults to '1080p'."
    )
    parser.add_argument(
        "--model",
        default="auto",
        help="Real-ESRGAN model name to use (e.g. 'realesrgan-x4plus', 'realesr-animevideov3'). Use 'auto' to auto-detect content type. Defaults to 'auto'."
    )
    parser.add_argument(
        "--model-path",
        dest="model_path",
        help="Path to the directory containing Real-ESRGAN model files (.bin/.param)."
    )
    
    # Encoder and quality
    parser.add_argument(
        "--encoder",
        choices=["auto", "videotoolbox", "nvenc", "vaapi", "libx265"],
        default="auto",
        help="Hardware or software encoder profile. Defaults to 'auto'."
    )
    parser.add_argument(
        "--quality",
        type=int,
        default=60,
        help="Normalized target quality (0..100). Defaults to 60."
    )
    parser.add_argument(
        "--bitrate",
        help="Target video bitrate (e.g. 12M, 40000k). Overrides --quality."
    )
    parser.add_argument(
        "--interpolate-fps",
        type=int,
        dest="interpolate_fps",
        help="Enable motion-compensated frame interpolation to the target framerate (e.g. 60)."
    )
    parser.add_argument(
        "--temporal-denoise",
        action="store_true",
        dest="temporal_denoise",
        help="Enable 3D temporal denoising filter (hqdn3d) to stabilize frame transitions."
    )
    
    # Real-ESRGAN runner config
    parser.add_argument(
        "--realesrgan-bin",
        dest="realesrgan_bin",
        help="Override path/binary name for Real-ESRGAN. Defaults to realesrgan-ncnn-vulkan."
    )
    parser.add_argument(
        "--jobs",
        default="auto",
        help="Real-ESRGAN thread spec 'load:proc:save' (e.g., '1:2:1'). Defaults to 'auto'."
    )
    
    # Pipeline segment and robustness controls
    parser.add_argument(
        "--chunk-seconds",
        type=float,
        default=12.0,
        dest="chunk_seconds",
        help="Duration of pre-split chunks in seconds. Defaults to 12.0."
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of segment chunks to process in parallel. Defaults to 1."
    )
    parser.add_argument(
        "--hdr-mode",
        choices=["tonemap", "error", "passthrough"],
        default="tonemap",
        dest="hdr_mode",
        help="How to handle HDR video inputs. Defaults to 'tonemap'."
    )
    parser.add_argument(
        "--vfr-mode",
        choices=["error", "cfr", "warn"],
        default="error",
        dest="vfr_mode",
        help="How to handle variable-frame-rate (VFR) video inputs. Defaults to 'error'."
    )
    
    # Work dir and lifecycle
    parser.add_argument(
        "--work-dir",
        dest="work_dir",
        help="Working directory to store intermediate segments and frames. Derived by default."
    )
    parser.add_argument(
        "--keep-work",
        action="store_true",
        dest="keep_work",
        help="Keep intermediate segment files and folders on success."
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        dest="fail_fast",
        help="Abort the entire batch operation on the first file failure."
    )

    args = parser.parse_args(argv)

    # Convert args Namespace to dict for passing
    opts = vars(args)

    # 1. Verify environment and tools
    try:
        tools_info = verify_tools(opts.get("realesrgan_bin"))
    except ToolError as e:
        # In python main, we catch ToolError and print clean message
        print(f"ERROR: Tool verification failed:\n{e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"ERROR: Unexpected tool verification error: {e}", file=sys.stderr)
        return 1

    # If --encoder is "auto", update the encoder in opts with what was auto-selected
    # Wait, run_batch handles it or we can resolve it here. Let's resolve it in encoders.py select_encoder inside pipeline.py
    # But wait, let's pass down tools_info which has platform + encoders list.
    try:
        from upscaler.encoders import select_encoder
        opts["encoder"] = select_encoder(opts["encoder"], tools_info["platform"], tools_info["encoders"])
    except ToolError as e:
        print(f"ERROR: Encoder selection error:\n{e}", file=sys.stderr)
        return 1

    # 2. Run the batch process
    try:
        return run_batch(opts["inputs"], opts, tools_info)
    except UpscalerError as e:
        print(f"ERROR: Upscaler operation failed:\n{e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nOperation cancelled by user.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"ERROR: An unexpected error occurred: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return 1

if __name__ == "__main__":
    sys.exit(main())
