import os
import re
import shutil
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

from . import ToolError

def get_ffmpeg_path() -> str:
    if getattr(sys, "frozen", False):
        base_dir = sys._MEIPASS
    else:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bundled = os.path.join(base_dir, "ffmpeg")
    if os.path.exists(bundled) and os.path.isfile(bundled):
        return os.path.abspath(bundled)
    raise ToolError("ffmpeg binary not found in the application bundle.")

def get_ffprobe_path() -> str:
    if getattr(sys, "frozen", False):
        base_dir = sys._MEIPASS
    else:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bundled = os.path.join(base_dir, "ffprobe")
    if os.path.exists(bundled) and os.path.isfile(bundled):
        return os.path.abspath(bundled)
    raise ToolError("ffprobe binary not found in the application bundle.")

def get_ffmpeg_version() -> Tuple[int, int]:
    ffmpeg_path = get_ffmpeg_path()
    try:
        res = subprocess.run([ffmpeg_path, "-version"], capture_output=True, text=True, check=True)
        # Typically: "ffmpeg version 8.1.2 ..." or "ffmpeg version N-N ..." or "ffmpeg version n5.1.2 ..."
        # Let's extract digits.
        match = re.search(r"ffmpeg version (?:n)?(\d+)\.(\d+)", res.stdout)
        if match:
            return int(match.group(1)), int(match.group(2))
        
        # Try match just major version, e.g. "ffmpeg version 5"
        match_major = re.search(r"ffmpeg version (?:n)?(\d+)", res.stdout)
        if match_major:
            return int(match_major.group(1)), 0
            
        raise ToolError(f"Could not parse ffmpeg version from output: {res.stdout.splitlines()[0] if res.stdout else ''}")
    except subprocess.SubprocessError as e:
        raise ToolError(f"Failed to run ffmpeg -version: {e}")

def check_ffmpeg_filters() -> Tuple[bool, bool]:
    ffmpeg_path = get_ffmpeg_path()
    try:
        res = subprocess.run([ffmpeg_path, "-filters"], capture_output=True, text=True, check=True)
        has_zscale = "zscale" in res.stdout
        has_tonemap = "tonemap" in res.stdout
        return has_zscale, has_tonemap
    except subprocess.SubprocessError as e:
        raise ToolError(f"Failed to run ffmpeg -filters: {e}")

def get_available_encoders() -> List[str]:
    ffmpeg_path = get_ffmpeg_path()
    try:
        res = subprocess.run([ffmpeg_path, "-encoders"], capture_output=True, text=True, check=True)
        encoders = []
        for line in res.stdout.splitlines():
            # Lines look like: " V..... hevc_videotoolbox    Apple VideoToolbox HEVC encoder (codec hevc)"
            match = re.search(r"\b(hevc_videotoolbox|hevc_nvenc|hevc_vaapi|libx265)\b", line)
            if match:
                encoders.append(match.group(1))
        # Deduplicate while preserving order
        seen = set()
        dedup_encoders = []
        for enc in encoders:
            if enc not in seen:
                seen.add(enc)
                dedup_encoders.append(enc)
        return dedup_encoders
    except subprocess.SubprocessError as e:
        raise ToolError(f"Failed to run ffmpeg -encoders: {e}")

def detect_platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    elif sys.platform.startswith("linux"):
        return "linux"
    else:
        return "other"

def find_realesrgan(custom_path: Optional[str] = None) -> str:
    # An explicit --realesrgan-bin wins over the bundled binary. Ignoring it
    # silently made tests believe they were driving a stub while they were in
    # fact running the real model on the GPU.
    if custom_path:
        expanded = os.path.abspath(os.path.expanduser(custom_path))
        if not os.path.isfile(expanded):
            raise ToolError(f"realesrgan binary not found at {expanded}.")
        if not os.access(expanded, os.X_OK):
            raise ToolError(f"realesrgan binary at {expanded} is not executable.")
        return expanded

    # Check local project directory or PyInstaller bundle directory (frozen)
    if getattr(sys, "frozen", False):
        base_dir = sys._MEIPASS
    else:
        # __file__ is in upscaler/tools.py, so go up one level to get project root
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        
    local_bin = os.path.join(base_dir, "upscaler", "bin", "upscayl-bin")
    if os.path.exists(local_bin) and os.path.isfile(local_bin):
        return os.path.abspath(local_bin)
        
    raise ToolError("upscayl-bin binary not found in the application bundle.")

def verify_tools(realesrgan_path: Optional[str] = None) -> Dict[str, any]:
    # Check ffmpeg & ffprobe paths
    try:
        ffmpeg_path = get_ffmpeg_path()
        ffprobe_path = get_ffprobe_path()
    except ToolError as e:
        plat = detect_platform()
        if plat == "macos":
            hint = "\nInstall with: brew install ffmpeg"
        elif plat == "linux":
            hint = "\nInstall with: apt-get install ffmpeg"
        else:
            hint = ""
        raise ToolError(str(e) + hint)

    # Check version
    major, minor = get_ffmpeg_version()
    if (major, minor) < (5, 1):
        raise ToolError(
            f"ffmpeg version {major}.{minor} found, but version >= 5.1 is required (needed for -fps_mode)."
        )

    # Find realesrgan
    real_bin = find_realesrgan(realesrgan_path)

    # Detect features
    has_zscale, has_tonemap = check_ffmpeg_filters()
    encoders = get_available_encoders()
    plat = detect_platform()

    return {
        "ffmpeg_path": ffmpeg_path,
        "ffprobe_path": ffprobe_path,
        "ffmpeg_version": (major, minor),
        "realesrgan_path": real_bin,
        "has_zscale": has_zscale,
        "has_tonemap": has_tonemap,
        "encoders": encoders,
        "platform": plat,
    }
