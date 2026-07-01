import pytest

from upscaler import ToolError
from upscaler.encoders import (
    map_quality_crf,
    select_encoder,
    get_encoder_args,
    PRESET_BITRATES
)

def test_map_quality_crf():
    # round(51 - q * 0.51)
    assert map_quality_crf(100) == 0    # lossless/best
    assert map_quality_crf(0) == 51      # worst
    assert map_quality_crf(60) == 20     # default
    assert map_quality_crf(50) == 26
    
    with pytest.raises(ValueError):
        map_quality_crf(-1)
    with pytest.raises(ValueError):
        map_quality_crf(101)

def test_select_encoder():
    # macOS auto-selection
    assert select_encoder("auto", "macos", ["hevc_videotoolbox", "libx265"]) == "videotoolbox"
    assert select_encoder("auto", "macos", ["libx265"]) == "libx265"

    # Linux auto-selection
    assert select_encoder("auto", "linux", ["hevc_nvenc", "libx265"]) == "nvenc"
    assert select_encoder("auto", "linux", ["hevc_vaapi", "libx265"]) == "vaapi"
    assert select_encoder("auto", "linux", ["libx265"]) == "libx265"

    # Explicit requests
    assert select_encoder("videotoolbox", "macos", ["hevc_videotoolbox"]) == "videotoolbox"
    
    # Missing explicit encoder should raise ToolError
    with pytest.raises(ToolError) as exc:
        select_encoder("videotoolbox", "macos", ["libx265"])
    assert "not available" in str(exc.value)

    with pytest.raises(ToolError) as exc:
        select_encoder("invalid_enc", "macos", ["libx265"])
    assert "Invalid encoder option" in str(exc.value)

def test_get_encoder_args_quality():
    # videotoolbox quality mapping
    vt_args = get_encoder_args("videotoolbox", "1080p", 60)
    assert "-c:v" in vt_args
    assert "hevc_videotoolbox" in vt_args
    assert "-q:v" in vt_args
    # vt quality maps directly to q:v
    idx = vt_args.index("-q:v")
    assert vt_args[idx + 1] == "60"
    assert "-tag:v" in vt_args
    assert "hvc1" in vt_args

    # libx265 quality mapping
    x265_args = get_encoder_args("libx265", "1080p", 60)
    assert "libx265" in x265_args
    assert "-crf" in x265_args
    idx = x265_args.index("-crf")
    assert x265_args[idx + 1] == "20"  # map_quality_crf(60) = 20
    assert "-preset" in x265_args
    assert "medium" in x265_args

    # nvenc quality mapping
    nvenc_args = get_encoder_args("nvenc", "1080p", 60)
    assert "hevc_nvenc" in nvenc_args
    assert "-cq" in nvenc_args
    idx = nvenc_args.index("-cq")
    assert nvenc_args[idx + 1] == "20"

    # vaapi quality mapping
    vaapi_args = get_encoder_args("vaapi", "1080p", 60)
    assert "hevc_vaapi" in vaapi_args
    assert "-qp" in vaapi_args
    idx = vaapi_args.index("-qp")
    assert vaapi_args[idx + 1] == "20"
    assert "-vaapi_device" in vaapi_args

def test_get_encoder_args_bitrate():
    # Bitrate overrides quality
    vt_args = get_encoder_args("videotoolbox", "1080p", 60, bitrate="12M")
    assert "-b:v" in vt_args
    idx = vt_args.index("-b:v")
    assert vt_args[idx + 1] == "12M"
    assert "-q:v" not in vt_args

    x265_args = get_encoder_args("libx265", "1080p", 60, bitrate="12M")
    assert "-b:v" in x265_args
    idx = x265_args.index("-b:v")
    assert x265_args[idx + 1] == "12M"
    assert "-crf" not in x265_args
