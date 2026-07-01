from typing import Dict, List, Optional
from . import ToolError

PRESET_BITRATES: Dict[str, str] = {
    "720p": "6M",
    "1080p": "12M",
    "4k": "40M",
}

def map_quality_crf(quality: int) -> int:
    """Maps normalized 0..100 quality to CRF-like native values.
    
    Formula: round(51 - q * 0.51)
    Quality 100 maps to 0 (lossless/best).
    Quality 0 maps to 51 (worst).
    Quality 60 maps to 20.
    """
    if quality < 0 or quality > 100:
        raise ValueError("Quality must be between 0 and 100.")
    return int(round(51.0 - float(quality) * 0.51))

def select_encoder(
    encoder_opt: str,
    platform: str,
    available_encoders: List[str]
) -> str:
    """Selects the best available encoder based on preference, platform, and availability."""
    valid_profiles = ["videotoolbox", "nvenc", "vaapi", "libx265"]
    
    if encoder_opt != "auto":
        if encoder_opt not in valid_profiles:
            raise ToolError(f"Invalid encoder option '{encoder_opt}'. Must be one of {valid_profiles} or 'auto'.")
        
        # Check if requested encoder is actually available
        codec_map = {
            "videotoolbox": "hevc_videotoolbox",
            "nvenc": "hevc_nvenc",
            "vaapi": "hevc_vaapi",
            "libx265": "libx265",
        }
        requested_codec = codec_map[encoder_opt]
        if requested_codec not in available_encoders:
            raise ToolError(
                f"Requested encoder '{encoder_opt}' ({requested_codec}) is not available in ffmpeg."
            )
        return encoder_opt

    # Auto-selection logic
    if platform == "macos" and "hevc_videotoolbox" in available_encoders:
        return "videotoolbox"
    elif platform == "linux":
        if "hevc_nvenc" in available_encoders:
            return "nvenc"
        elif "hevc_vaapi" in available_encoders:
            return "vaapi"
    
    # Default fallback
    if "libx265" in available_encoders:
        return "libx265"
    
    # If not listed but we must fallback
    return "libx265"

def get_encoder_args(
    profile: str,
    preset: str,
    quality: int,
    bitrate: Optional[str] = None,
    vaapi_device: str = "/dev/dri/renderD128"
) -> List[str]:
    """Generates the command arguments for the specified encoder profile."""
    # Resolve the bitrate if explicitly provided, else keep it None
    # (If bitrate is not provided, we will map quality. Note that quality defaults to 60)
    
    args: List[str] = []
    
    if profile == "videotoolbox":
        args.extend(["-c:v", "hevc_videotoolbox"])
        if bitrate:
            args.extend(["-b:v", bitrate])
        else:
            # videotoolbox supports -q:v <0..100>
            args.extend(["-q:v", str(quality)])
        args.extend(["-tag:v", "hvc1", "-pix_fmt", "yuv420p"])

    elif profile == "nvenc":
        args.extend(["-c:v", "hevc_nvenc"])
        if bitrate:
            args.extend(["-b:v", bitrate])
        else:
            crf = map_quality_crf(quality)
            args.extend(["-cq", str(crf)])
        args.extend(["-tag:v", "hvc1", "-pix_fmt", "yuv420p"])

    elif profile == "vaapi":
        args.extend(["-vaapi_device", vaapi_device])
        args.extend(["-c:v", "hevc_vaapi"])
        if bitrate:
            args.extend(["-b:v", bitrate])
        else:
            qp = map_quality_crf(quality)
            args.extend(["-qp", str(qp)])

    elif profile == "libx265":
        args.extend(["-c:v", "libx265"])
        if bitrate:
            args.extend(["-b:v", bitrate])
        else:
            crf = map_quality_crf(quality)
            args.extend(["-crf", str(crf)])
        args.extend(["-preset", "medium", "-tag:v", "hvc1", "-pix_fmt", "yuv420p"])
        
    else:
        raise ValueError(f"Unknown encoder profile: {profile}")

    return args
