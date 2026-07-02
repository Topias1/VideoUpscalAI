# VideoUpscalAI

> [!IMPORTANT]
> 🚀 **[Download VideoUpscalAI v1.0 for macOS (DMG)](https://github.com/Topias1/VideoUpscalAI/releases/download/v1.0/VideoUpscalAI.dmg)**

![VideoUpscalAI Interface](screenshot.png)

A robust, resumable command-line pipeline that upscales videos using **Real-ESRGAN-ncnn-vulkan** (GPU-accelerated inference) for frame upscaling and **ffmpeg** for demuxing, downscaling, and hardware-accelerated re-encoding.

Developed and optimized for **Apple Silicon macOS**, but architected from the ground up to support **Linux** (NVIDIA NVENC, Intel/AMD VAAPI, and CPU libx265).

## Key Features

- **Keyframe-Aligned Segment Chunking**: Splits video into chunks to bound disk usage (avoids materializing all PNG frames at once).
- **Per-Chunk & Per-File Resume**: Automatically checks segment validity and skips completed segments if interrupted.
- **Pluggable Encoders**: Supports Apple VideoToolbox, NVIDIA NVENC, Intel/AMD VAAPI, and CPU libx265.
- **HDR Handling**: Detects HDR content and automatically tonemaps down to SDR (using `zscale` + `tonemap` filters).
- **VFR Conformance**: Detects variable-frame-rate phone/camera videos and conforms them to CFR to avoid audio/video drift.
- **Stream Preservation**: Losslessly remuxes audio, subtitles, chapters, and metadata back into the final upscaled video, with automatic transcoder fallbacks for unsupported formats in MP4.

---

## Installation & Standalone App

### Standalone macOS Application
For macOS users, a pre-compiled, **100% self-contained** standalone bundle is available:
* **To run**: Simply double-click `dist/VideoUpscalAI.app` in Finder, or run:
  ```bash
  open dist/VideoUpscalAI.app
  ```

### Developer Setup (Source Code Run)
If you are running or modifying the raw Python source code directly:
1. Ensure your local copies of the helper binaries (`ffmpeg`, `ffprobe`, and `upscaler/bin/upscayl-bin`) and AI models (`upscaler/models/*.param`/`.bin`) are present in the project structure.
2. The pipeline will automatically locate and run them from the local folders, with no system PATH queries or external installations needed.

---

## Usage

Run the upscaler by passing one or more input video files or directories:

```bash
./upscale.py INPUT_VIDEO.mp4 -o OUTPUT_UPSCALED.mp4 --preset 1080p
```

### CLI Reference

```text
usage: upscale.py [-h] [-o OUTPUT] [--output-dir OUTPUT_DIR] [--recursive]
                  [--preset {480p,720p,1080p,4k}] [--model MODEL]
                  [--model-path MODEL_PATH]
                  [--encoder {auto,videotoolbox,nvenc,vaapi,libx265}]
                  [--quality QUALITY] [--bitrate BITRATE]
                  [--realesrgan-bin REALESRGAN_BIN] [--jobs JOBS]
                  [--chunk-seconds CHUNK_SECONDS]
                  [--hdr-mode {tonemap,error,passthrough}]
                  [--vfr-mode {error,cfr,warn}] [--work-dir WORK_DIR]
                  [--keep-work] [--fail-fast] [--workers WORKERS]
                  [--interpolate-fps INTERPOLATE_FPS] [--temporal-denoise]
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
  --preset {480p,720p,1080p,4k}
                        Target output preset. Defaults to '1080p'.
  --model MODEL         Real-ESRGAN model name to use. Use 'auto' to auto-detect. Defaults to 'auto'.
  --model-path MODEL_PATH
                        Path to the directory containing Real-ESRGAN model files (.bin/.param).
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
  --force               Force overwrite existing output files instead of resuming/skipping.
  --fail-fast           Abort the entire batch operation on the first file failure.
  --workers WORKERS     Number of segment chunks to process in parallel. Defaults to 1.
  --interpolate-fps INTERPOLATE_FPS
                        Enable motion-compensated frame interpolation to the target framerate (e.g. 60).
  --temporal-denoise    Enable 3D temporal denoising filter (hqdn3d) to stabilize frame transitions.
```

---

## Advanced Features

### 1. Parallel Segment Processing (`--workers N`)
Allows processing multiple video segment chunks concurrently. This is highly effective at saturating high-core Apple Silicon GPUs or multi-GPU environments.
```bash
./upscale.py input.mp4 --workers 3
```

### 2. Auto-Content Detection (`--model auto`)
When set to `auto`, the pipeline extracts a sample thumbnail and analyses color counts to classify it as either `"animation"` (flat shading) or `"cinema"` (real-world details/noise).
* **Animation** uses `realesr-animevideov3` (highly-optimized cartoon model).
* **Cinema** uses `realesrgan-x4plus` (live-action model).

### 3. Motion-Compensated Frame Rate Interpolation (`--interpolate-fps N`)
Smooths out stuttery videos up to 60fps (or any other target frame rate) using `ffmpeg`'s motion-estimation interpolation:
```bash
./upscale.py input.mp4 --interpolate-fps 60
```

### 4. 3D Temporal Denoising (`--temporal-denoise`)
Injects `hqdn3d` into the encoding stages, stabilizing frame-to-frame pixel variations and eliminating flickering artifacts typical of neural network frame upscaling.
```bash
./upscale.py input.mp4 --temporal-denoise
```

---

## Local Web GUI Interface

A beautiful, light-weight local Web GUI server is included (`gui.py`) which runs natively on Python's built-in HTTP server.

1. Start the GUI server:
   ```bash
   .venv/bin/python gui.py
   ```
2. Open your web browser at: **`http://127.0.0.1:8080`**
3. Select your video, configure resolution, pick model auto-detection, set parallel workers, toggle filters, and follow real-time progress bars and console logs directly on the web page.

---

## Development & Testing

Run all unit and integration tests with `pytest`:

```bash
PYTHONPATH=. .venv/bin/pytest -v
```
