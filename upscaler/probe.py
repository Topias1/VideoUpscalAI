import json
import subprocess
from fractions import Fraction
from typing import Any, Dict, NamedTuple, Optional

from . import ProbeError
from .tools import get_ffprobe_path

class VideoInfo(NamedTuple):
    width: int
    height: int
    fps: str  # Rational string, e.g., "24000/1001"
    frame_count: int
    duration: float
    has_audio: bool
    has_subtitles: bool
    has_chapters: bool
    is_hdr: bool
    is_vfr: bool
    color_transfer: Optional[str]
    color_primaries: Optional[str]

def parse_rational(r_str: str) -> Optional[Fraction]:
    try:
        if not r_str or r_str == "0/0":
            return None
        return Fraction(r_str)
    except (ValueError, ZeroDivisionError):
        return None

def parse_ffprobe_scalar_int(stdout: str) -> Optional[int]:
    """Parse a single integer from ffprobe's `-of csv=p=0` output.

    ffprobe 8.x appends a trailing field separator ("59,\\n"), so a plain
    int() on the stripped output raises. Take the first non-empty field.
    """
    for field in stdout.strip().split(","):
        field = field.strip()
        if not field:
            continue
        try:
            return int(field)
        except ValueError:
            return None
    return None

def probe_video(video_path: str) -> VideoInfo:
    ffprobe_path = get_ffprobe_path()
    cmd = [
        ffprobe_path,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        video_path
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(res.stdout)
    except subprocess.CalledProcessError as e:
        raise ProbeError(f"ffprobe failed to read metadata for {video_path}: {e.stderr}")
    except json.JSONDecodeError as e:
        raise ProbeError(f"ffprobe returned invalid JSON for {video_path}: {e}")

    streams = data.get("streams", [])
    format_info = data.get("format", {})
    chapters = data.get("chapters", [])

    video_stream: Optional[Dict[str, Any]] = None
    has_audio = False
    has_subtitles = False

    for stream in streams:
        c_type = stream.get("codec_type")
        if c_type == "video" and not video_stream:
            video_stream = stream
        elif c_type == "audio":
            has_audio = True
        elif c_type == "subtitle":
            has_subtitles = True

    if not video_stream:
        raise ProbeError(f"No video stream found in {video_path}")

    # Parse basic dimensions
    try:
        width = int(video_stream["width"])
        height = int(video_stream["height"])
    except (KeyError, ValueError):
        raise ProbeError(f"Invalid video dimensions in {video_path}")

    # Parse framerate
    r_frame_rate_str = video_stream.get("r_frame_rate", "")
    avg_frame_rate_str = video_stream.get("avg_frame_rate", "")
    
    r_fps = parse_rational(r_frame_rate_str)
    avg_fps = parse_rational(avg_frame_rate_str)

    if not r_fps or r_fps == 0:
        if avg_fps and avg_fps != 0:
            r_fps = avg_fps
            r_frame_rate_str = avg_frame_rate_str
        else:
            raise ProbeError(f"Could not determine video frame rate in {video_path}")

    # VFR Detection
    is_vfr = False
    if r_fps and avg_fps:
        # If they differ by more than a tiny tolerance, mark VFR
        # We can also check if they are exactly not equal, as requested by spec: "compare r_frame_rate vs avg_frame_rate"
        is_vfr = r_fps != avg_fps

    # Duration
    try:
        duration = float(format_info.get("duration", 0.0))
    except ValueError:
        duration = 0.0

    if duration <= 0.0:
        try:
            duration = float(video_stream.get("duration", 0.0))
        except ValueError:
            duration = 0.0

    # Parse frame count
    frame_count = 0
    # Try nb_frames in stream first
    nb_frames_str = video_stream.get("nb_frames")
    if nb_frames_str:
        try:
            frame_count = int(nb_frames_str)
        except ValueError:
            pass

    if frame_count <= 0:
        # Try count packets exactly as a fallback
        try:
            count_cmd = [
                ffprobe_path,
                "-v", "error",
                "-select_streams", "v:0",
                "-count_packets",
                "-show_entries", "stream=nb_read_packets",
                "-of", "csv=p=0",
                video_path
            ]
            count_res = subprocess.run(count_cmd, capture_output=True, text=True, check=True)
            val = parse_ffprobe_scalar_int(count_res.stdout)
            if val is not None:
                frame_count = val
        except Exception:
            pass

    if frame_count <= 0:
        # Approximate frame count if counting failed
        if duration > 0.0 and r_fps:
            frame_count = int(round(duration * float(r_fps)))

    if frame_count <= 0:
        raise ProbeError(f"Could not determine frame count for {video_path}")

    # HDR Detection
    color_transfer = video_stream.get("color_transfer")
    color_primaries = video_stream.get("color_primaries")
    
    is_hdr = False
    if color_transfer in ("smpte2084", "arib-std-b67") or color_primaries == "bt2020":
        is_hdr = True

    has_chapters = len(chapters) > 0

    return VideoInfo(
        width=width,
        height=height,
        fps=r_frame_rate_str,
        frame_count=frame_count,
        duration=duration,
        has_audio=has_audio,
        has_subtitles=has_subtitles,
        has_chapters=has_chapters,
        is_hdr=is_hdr,
        is_vfr=is_vfr,
        color_transfer=color_transfer,
        color_primaries=color_primaries,
    )

def detect_video_type(video_path: str) -> str:
    """Detects if a video is 'animation' or 'cinema' using color variance heuristics."""
    import tempfile
    import os
    import uuid
    from .tools import get_ffmpeg_path
    
    # Extract 1 frame from the video
    temp_dir = tempfile.gettempdir()
    temp_png = os.path.join(temp_dir, f"probe_{uuid.uuid4().hex}.png")
    
    cmd = [
        get_ffmpeg_path(), "-y",
        "-ss", "00:00:01",  # Seek to 1s to avoid black intro frames
        "-i", video_path,
        "-vframes", "1",
        "-f", "image2",
        "-vcodec", "png",
        temp_png
    ]
    
    # If seek to 1s fails, try without seeking
    try:
        subprocess.run(cmd, capture_output=True, check=True)
    except subprocess.CalledProcessError:
        cmd_noseek = [
            get_ffmpeg_path(), "-y",
            "-i", video_path,
            "-vframes", "1",
            "-f", "image2",
            "-vcodec", "png",
            temp_png
        ]
        try:
            subprocess.run(cmd_noseek, capture_output=True, check=True)
        except Exception:
            return "cinema"  # Fallback

    if not os.path.exists(temp_png):
        return "cinema"

    try:
        from PIL import Image
        img = Image.open(temp_png).convert("RGB")
        img_small = img.resize((64, 64), Image.Resampling.LANCZOS)
        colors = img_small.getcolors(maxcolors=4096)
        num_colors = len(colors) if colors else 4096
        img.close()
        
        # Heuristic: animation has fewer unique colors
        is_anime = num_colors <= 800
    except Exception:
        is_anime = False
    finally:
        try:
            os.remove(temp_png)
        except Exception:
            pass

    return "animation" if is_anime else "cinema"
