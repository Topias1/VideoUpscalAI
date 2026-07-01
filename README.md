# Apple Silicon Video Upscaler

A robust, resumable command-line pipeline that upscales videos using **Real-ESRGAN-ncnn-vulkan** (GPU-accelerated inference) for frame upscaling and **ffmpeg** for demuxing, downscaling, and hardware-accelerated re-encoding.

Developed and optimized for **Apple Silicon macOS**, but architected from the ground up to support **Linux** (NVIDIA NVENC, Intel/AMD VAAPI, and CPU libx265) including guests running under **UTM (v5.0+ with Venus Vulkan virtualization)**.

## Key Features

- **Keyframe-Aligned Segment Chunking**: Splits video into chunks to bound disk usage (avoids materializing all PNG frames at once).
- **Per-Chunk & Per-File Resume**: Automatically checks segment validity and skips completed segments if interrupted.
- **Pluggable Encoders**: Supports Apple VideoToolbox, NVIDIA NVENC, Intel/AMD VAAPI, and CPU libx265.
- **HDR Handling**: Detects HDR content and automatically tonemaps down to SDR (using `zscale` + `tonemap` filters).
- **VFR Conformance**: Detects variable-frame-rate phone/camera videos and conforms them to CFR to avoid audio/video drift.
- **Stream Preservation**: Losslessly remuxes audio, subtitles, chapters, and metadata back into the final upscaled video, with automatic transcoder fallbacks for unsupported formats in MP4.

---

## Installation

### macOS (Apple Silicon)

Install the required tools using [Homebrew](https://brew.sh):

```bash
brew install ffmpeg realesrgan-ncnn-vulkan
```

### Linux

1. Install `ffmpeg` (>= 5.1):
   ```bash
   sudo apt-get install ffmpeg
   ```
2. Download the latest binary release of `realesrgan-ncnn-vulkan` from [GitHub Releases](https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan) and add it to your `PATH` or point the CLI to it.

### Ubuntu-on-UTM (Venus Virtualization)

To run GPU-accelerated upscaling inside a Linux VM on Apple Silicon:
1. Ensure you are using **UTM v5.0+** with **Venus (Vulkan virtualization)** enabled.
2. In the guest, install the Mesa Venus Vulkan driver.
3. Install `ffmpeg` inside the guest. The encoding will run on the CPU (`libx265`), while the upscaling executes via GPU acceleration.

---

## Usage

Run the upscaler by passing one or more input video files or directories:

```bash
./upscale.py INPUT_VIDEO.mp4 -o OUTPUT_UPSCALED.mp4 --preset 1080p
```

### CLI Reference

```text
usage: upscale.py [-h] [-o OUTPUT] [--output-dir OUTPUT_DIR] [--recursive]
                  [--preset {720p,1080p,4k}] [--model MODEL]
                  [--encoder {auto,videotoolbox,nvenc,vaapi,libx265}]
                  [--quality QUALITY] [--bitrate BITRATE]
                  [--realesrgan-bin REALESRGAN_BIN] [--jobs JOBS]
                  [--chunk-seconds CHUNK_SECONDS]
                  [--hdr-mode {tonemap,error,passthrough}]
                  [--vfr-mode {error,cfr,warn}] [--work-dir WORK_DIR]
                  [--keep-work] [--fail-fast]
                  INPUT [INPUT ...]

Apple Silicon Video Upscaler CLI pipeline using Real-ESRGAN-ncnn-vulkan and ffmpeg.

positional arguments:
  INPUT                 One or more video files and/or directories to upscale.

options:
  -h, --help            show this help message and exit
  -o OUTPUT, --output OUTPUT
                        Output filepath (only valid when upscaling a single file).
  --output-dir OUTPUT_DIR
                        Directory to save upscaled files. Defaults to alongside each source file.
  --recursive           Descend into subdirectories when input is a directory.
  --preset {720p,1080p,4k}
                        Target output preset. Defaults to '1080p'.
  --model MODEL         Real-ESRGAN model name to use. Defaults to 'realesrgan-x4plus'.
  --encoder {auto,videotoolbox,nvenc,vaapi,libx265}
                        Hardware or software encoder profile. Defaults to 'auto'.
  --quality QUALITY     Normalized target quality (0..100). Defaults to 60.
  --bitrate BITRATE     Target video bitrate (e.g. 12M, 40000k). Overrides --quality.
  --realesrgan-bin REALESRGAN_BIN
                        Override path/binary name for Real-ESRGAN. Defaults to realesrgan-ncnn-vulkan.
  --jobs JOBS           Real-ESRGAN thread spec 'load:proc:save' (e.g., '1:2:1'). Defaults to 'auto'.
  --chunk-seconds CHUNK_SECONDS
                        Duration of pre-split chunks in seconds. Defaults to 12.0.
  --hdr-mode {tonemap,error,passthrough}
                        How to handle HDR video inputs. Defaults to 'tonemap'.
  --vfr-mode {error,cfr,warn}
                        How to handle variable-frame-rate (VFR) video inputs. Defaults to 'error'.
  --work-dir WORK_DIR   Working directory to store intermediate segments and frames. Derived by default.
  --keep-work           Keep intermediate segment files and folders on success.
  --fail-fast           Abort the entire batch operation on the first file failure.
```

---

## Development & Testing

Run all unit and integration tests with `pytest`:

```bash
PYTHONPATH=. .venv/bin/pytest -v
```
