# Apple Silicon Video Upscaler — Design Spec

**Date:** 2026-07-01
**Repo:** git@github.com:Topias1/apple-silicon-video-upscaler.git
**Status:** Approved design — pending implementation plan

## 1. Purpose

A robust, resumable CLI pipeline that upscales live-action video using
**Real-ESRGAN-ncnn-vulkan** (GPU inference via ncnn → Vulkan) for frame upscaling and
**ffmpeg** for demux, downscale, and re-encode.

Primary, tested target: **macOS on Apple Silicon** (developed/tested on M5), Homebrew
tools, HEVC via the hardware **VideoToolbox** encoder. The pipeline is **architected
for Linux** from the start (encoder abstraction + cross-platform tool discovery) so it
also runs on a real Linux GPU box and on **Ubuntu-in-UTM (v5.0+/Venus)** on the same
M5 — those paths are wired but not tested here. See §7.

English docs (public repo).

Non-goals (YAGNI): denoise/deblock filters, GUI, anime-specific models,
cloud/distributed processing, GPU/CPU pipelining, full HDR preservation. HDR is handled
by **detection + tonemap-to-SDR** only (§9).

## 2. Success Criteria

- Given a file **or a directory** of SDR (or auto-tonemapped HDR) CFR live-action
  sources, produce upscaled `.mp4` outputs at a chosen preset (720p / 1080p / 4k),
  preserving all audio/subtitle/chapter streams and matching duration.
- Disk footprint stays bounded regardless of source length (never materializes all
  frames at once).
- Killed/interrupted runs resume without redoing completed work — at both the **file**
  and **chunk** granularity.
- Runs unattended with clear per-file + per-chunk progress and actionable errors; a
  batch continues past a failing file (unless `--fail-fast`) and prints a summary.
- Refuses, with a clear message, any source already ≥ the target preset height.
- Selects a working encoder automatically per platform; falls back cleanly.

## 3. Key Design Decision — Chunking Strategy

The central constraint is **disk**: a 4K PNG frame is ~15–25 MB. A 90-minute 1080p
source at 24 fps upscaled 4× would be ~3 TB of PNGs if materialized at once.

**Chosen approach: pre-split each source into keyframe-aligned video segments** using
the ffmpeg segment muxer, then process one segment at a time.

```
ffmpeg -i INPUT -an -sn -dn -c copy -f segment -segment_time <chunk_seconds> \
       -reset_timestamps 1 <work>/seg_in/seg_%04d.mkv
```

Rationale:
- Segment split is lossless (`-c copy`), fast (no re-encode), self-contained.
- Because every frame of each segment is re-extracted, re-encoded, and concatenated in
  order, the final result is **frame-exact** — no dropped or duplicated frames. Avoids
  frame-index seek pitfalls. Caveat: **open-GOP** sources (x265 default) can drop
  leading RASL frames when segments are decoded independently — so frame-exactness is
  **enforced, not assumed**, via the per-chunk reconciliation asserts in §4.
- Chunk unit = a segment; `--chunk-seconds` bounds peak disk usage
  (≈ chunk_seconds × fps × frame_size). Caveat: `-segment_time` is a **floor** — cuts
  land on the next keyframe, so a sparse-GOP source (60 s GOPs, or a single keyframe)
  produces giant segments. Probe the max keyframe interval up front
  (`ffprobe -skip_frame nokey`) and validate segment sizes post-split; warn, and abort
  the file when a segment would blow the per-chunk disk estimate.
- Audio/subtitles/chapters are stripped here (`-an -sn`) and re-muxed from the original
  source at the end (§5).

Rejected: frame-index `-ss` seeking (not frame-exact / O(n²)); global no-chunk
(explodes disk); pipelined GPU/CPU overlap (marginal gain, harder resume — realesrgan
is GPU-bound). All deferred.

## 4. Per-Chunk Loop

For each `seg_in/seg_XXXX.mkv`, **skip if `seg_out/seg_XXXX.mp4` exists and is valid**
(ffprobe check):

0. **Wipe** `<work>/frames/` and `<work>/up/` — stale PNGs from a run killed mid-chunk
   would otherwise leak into this chunk with colliding frame numbers.
1. **Extract frames** (tonemap filter injected when source is HDR — §9; `fps=<fps>`
   prepended when `--vfr-mode cfr` — §11):
   `ffmpeg -i seg_in/seg_XXXX.mkv [-vf <filters>] -fps_mode passthrough <work>/frames/f_%08d.png`
   **Reconcile:** PNG count must equal the segment's frame count (ffprobe); abort the
   file on mismatch (catches open-GOP frame drops — §3).
2. **Upscale 4× (whole directory in one call — keeps GPU saturated):**
   `<realesrgan-bin> -i <work>/frames -o <work>/up -n realesrgan-x4plus -s 4 -f png -j <jobs>`
3. **Downscale to preset height + encode** via the selected **encoder profile** (§7):
   `ffmpeg -framerate <fps> -i <work>/up/f_%08d.png -vf "scale=-2:<H>:flags=lanczos" \
    <encoder-args> seg_out/seg_XXXX.mp4`
   **Reconcile:** encoded frame count must equal the PNG count; abort the file on
   mismatch.
4. **Delete** `<work>/frames/` and `<work>/up/` for this chunk, then continue.

The AI always upscales 4× first; ffmpeg then resizes to the exact preset height
(lanczos). Output is **8-bit yuv420p**. `<fps>` = source fps from probe, passed as the
**exact rational string** (e.g. `24000/1001`) — never a float: 23.976 rounded to 24 is
~5 s of A/V drift over a 90-minute movie.

**Preset → target height** (width = `-2` to preserve AR and stay even, required by
yuv420p/HEVC):

| Preset | Height |
|--------|--------|
| 720p   | 720    |
| 1080p  | 1080 (default) |
| 4k     | 2160   |

**Preset guard:** if source height ≥ target height, abort before processing that file
with a message suggesting a higher preset (nothing to upscale). In batch, this skips
the file (logged) rather than aborting the whole run.

## 5. Finalization (per file)

```
# Concat output segments (lossless; params identical across segments)
ffmpeg -f concat -safe 0 -i <work>/concat_list.txt -c copy <work>/video_only.mp4

# Re-mux: upscaled video (input 0) + everything except video from original (input 1)
ffmpeg -i <work>/video_only.mp4 -i INPUT \
       -map 0:v:0 -map 1 -map -1:v \
       -map_metadata 1 -map_chapters 1 \
       -c copy -movflags +faststart OUTPUT
```

Preserves **all** original audio tracks, subtitles, chapters, metadata. Global metadata
defaults to input 0 (the rebuilt video, which has none), so `-map_metadata 1` is
required or source tags are silently lost; chapters happen to default to the first
input that has any, but are mapped explicitly anyway. Fallbacks when `-c copy` rejects
a stream in `.mp4`: subtitles → `-c:s mov_text` (text) or drop image-based subs with a
warning; audio → `-c:a aac` at 96 kb/s per channel (192k stereo, 576k 5.1); data
streams (GoPro gpmd, timecode, …) → drop with a warning.

## 6. CLI

```
upscale.py INPUT [INPUT ...]              # one or more files and/or directories
  [-o, --output PATH]                     # output path; single-file input only
  [--output-dir DIR]                      # batch destination; default: alongside each source
  [--recursive]                           # descend into subdirectories when INPUT is a dir
  [--preset {720p,1080p,4k}]              # default: 1080p
  [--model NAME]                          # default: realesrgan-x4plus
  [--encoder {auto,videotoolbox,nvenc,vaapi,libx265}]  # default: auto
  [--realesrgan-bin PATH]                 # override realesrgan binary (else env/PATH)
  [--chunk-seconds N]                     # default: 12
  [--quality Q]                           # normalized 0..100, default: 60
  [--bitrate RATE]                        # overrides --quality (per-preset fallback path)
  [--hdr-mode {tonemap,error,passthrough}]# default: tonemap
  [--vfr-mode {error,cfr,warn}]           # default: error
  [--jobs SPEC]                           # realesrgan -j load:proc:save, default: auto
  [--work-dir DIR]                        # work root; default derived; reuse to resume
  [--keep-work]                           # keep work dirs on success
  [--fail-fast]                           # stop the batch on the first file error
```

Defaults chosen: preset 1080p, model realesrgan-x4plus, encoder auto, chunk-seconds 12,
quality 60, hdr-mode tonemap, vfr-mode error. `-o/--output` is rejected (with a clear
message) when the input resolves to more than one file — use `--output-dir` for batches.

## 7. Platform & Encoder Abstraction

VideoToolbox is macOS-only. To keep Linux/UTM viable, the encode step is driven by a
pluggable **encoder profile**, selected via `--encoder` (default `auto`).

**Auto-selection** (from platform + `ffmpeg -encoders` availability):
1. macOS → `videotoolbox`
2. Linux with NVIDIA + `hevc_nvenc` → `nvenc`
3. Linux with a VAAPI render node + `hevc_vaapi` → `vaapi`
4. otherwise → `libx265` (CPU; also the Ubuntu-on-UTM path, since the Apple GPU isn't
   exposed as an NVENC/VAAPI device inside the guest)

**Profiles** (each maps the normalized `--quality` 0–100 and pixfmt/tag):

| Profile | Codec | Quality arg | Extra |
|---------|-------|-------------|-------|
| videotoolbox | `hevc_videotoolbox` | `-q:v <0..100>` (fallback `-b:v`) | `-tag:v hvc1 -pix_fmt yuv420p` |
| nvenc | `hevc_nvenc` | `-cq <mapped>` (fallback `-b:v`) | `-tag:v hvc1 -pix_fmt yuv420p` |
| vaapi | `hevc_vaapi` | `-qp <mapped>` (fallback `-b:v`) | `-vaapi_device …`; `format=nv12,hwupload` **appended** after the software scale (`scale=-2:<H>:flags=lanczos,format=nv12,hwupload`) — software filters can't run on hardware frames |
| libx265 | `libx265` | `-crf <mapped>` | `-preset medium -tag:v hvc1 -pix_fmt yuv420p` |

Normalized quality (default **60**) maps to each native scale (e.g. libx265/nvenc CRF-like
≈ `round(51 − q·0.51)` → ~20; videotoolbox uses `-q:v` directly). `--bitrate` overrides
to the per-preset bitrate fallback (4K≈40, 1080p≈12, 720p≈6 Mb/s). Concat uses `-c copy`
regardless of encoder (segments share identical params).

**realesrgan discovery:** `--realesrgan-bin` > `REALESRGAN_BIN` env > PATH lookup of
`realesrgan-ncnn-vulkan`. macOS install = Homebrew; Linux install = GitHub release
binary (documented in README). The model directory (`-m`) is auto-resolved next to the
binary when needed.

**Ubuntu-on-UTM note (secondary, untested here):** requires UTM v5.0+ with **Venus**
enabled and Mesa Venus Vulkan driver in the guest → realesrgan runs GPU-accelerated
(≈75% of native Metal on comparable compute workloads); encode falls to `libx265`
(CPU). Documented, not validated on this machine.

## 8. Batch / Multi-file (v1)

The CLI accepts one or more **files and/or directories**.

- **Discovery:** directories are scanned for video files by extension
  (`.mp4 .mov .mkv .avi .m4v .webm .mpg .mpeg .wmv .flv .ts`); `--recursive` descends
  into subdirectories. Files are processed in sorted order.
- **Core:** a single-file `run(input, opts) -> result` function; the batch layer is a
  thin loop over discovered inputs. (This is the “single-file core” that was requested;
  batch is the wrapper, shipped in v1.)
- **Output:** `--output-dir` sets the destination (default: alongside each source);
  each output is `<stem>_<preset>.mp4`. With `--recursive` + `--output-dir`, each
  source's path relative to its input root is mirrored under the output dir (two
  `episode.mkv` in different subdirs must not overwrite each other); any residual
  collision is a hard error before processing starts. `-o/--output` applies only when
  the input is a single file.
- **Per-file resume:** skip files whose final output exists and is valid; within a file,
  per-chunk resume (§10). Each file gets its own work subdir under the batch work root.
  Limitation (documented): the final mp4 carries no manifest, so file-level skip cannot
  detect changed params beyond the preset embedded in the name — re-running with a
  different quality/model requires deleting outputs or a fresh `--output-dir`.
- **Error policy:** a failing file is logged and the batch continues; `--fail-fast`
  stops on first error. A **summary** (succeeded / failed / skipped) prints at the end.
- **Progress:** `File i/N: <name> — chunk j/M`.

## 9. HDR Handling

realesrgan operates in SDR 8-bit RGB, so HDR must be handled explicitly to avoid silent
color breakage.

- **Detection** (probe): HDR if `color_transfer ∈ {smpte2084, arib-std-b67}` or
  `color_primaries = bt2020`.
- **Default (`--hdr-mode tonemap`):** inject an HDR→SDR tonemap into frame extraction
  (§4.1) and warn:
  `zscale=t=linear:npl=100,tonemap=tonemap=hable,zscale=p=bt709:t=bt709:m=bt709:r=tv,format=rgb24`
  (tonemap keeps its default `desat=2`; `desat=0` invites hue shifts in clipped
  highlights — a bad trade for an unattended pipeline.)
  Requires ffmpeg with libzimg (Homebrew ffmpeg has it; verified at tool-check when a
  source is HDR).
- **`--hdr-mode error`:** abort/skip the file with a clear message.
- **`--hdr-mode passthrough`:** treat as SDR without conversion (likely washed-out) +
  warning. Escape hatch only.

## 10. State & Resume

Batch work root layout:
```
<work>/
  <file-slug>/
    manifest.json        # run params + probe (incl. HDR flag) + chunk list + status
    seg_in/seg_XXXX.mkv  # pre-split source segments (video only)
    seg_out/seg_XXXX.mp4 # completed upscaled+encoded segments
    frames/  up/         # transient (current chunk only)
    concat_list.txt
```

- `manifest.json` records probe + resolved params (preset/model/quality/encoder/hdr).
  Resuming with mismatched params aborts that file with a clear message.
- On start, per file: reload manifest, skip chunks whose `seg_out/seg_XXXX.mp4` exists
  and passes an ffprobe validity check. `frames/` and `up/` are wiped at chunk start
  (§4.0), so a kill mid-chunk leaves no stale state that can corrupt the rerun.
- Chunk = unit of resume within a file; file = unit of resume within a batch.

## 11. Robustness / Error Handling

- **Tool discovery** at startup: verify `ffmpeg` (≥ 5.1, required for `-fps_mode`),
  `ffprobe`, and a realesrgan binary;
  detect platform + available encoders; on missing tools print exact install hints
  (`brew install ffmpeg realesrgan-ncnn-vulkan` on macOS; GitHub-release + apt ffmpeg on
  Linux) and exit non-zero. If any source is HDR, additionally verify ffmpeg has
  zimg/tonemap.
- **Input probe** (ffprobe JSON): width, height, fps (`r_frame_rate`), frame count,
  audio/subtitle streams, color transfer/primaries (HDR).
- **Preset guard** (§4). **VFR detection:** compare `r_frame_rate` vs `avg_frame_rate`.
  Frame-exact reassembly and the duration criterion (§2) assume CFR, and phone footage
  — a likely input — is commonly VFR, so `--vfr-mode` decides: `error` (default)
  aborts/skips the file with a clear message; `cfr` conforms at extraction by
  prepending `fps=<fps>` (exact rational) to the filter chain (§4.1); `warn` proceeds
  anyway (documented drift risk).
- **Subprocess handling:** every call checks return code, captures stderr; failures are
  reported with the offending file + chunk + stage.
- **Disk check** before start: estimate the transient per-chunk footprint **plus** the
  steady state — `seg_in/` holds a full copy of the video stream and `seg_out/`
  accumulates the full output (≈ 2× movie size total); warn if low.
- **Progress:** `tqdm` if importable, else plain printed lines. realesrgan's own
  progress is surfaced.

## 12. Project Structure

```
upscale.py                 # CLI entrypoint (arg parsing, batch discovery/loop, summary)
upscaler/
  __init__.py
  tools.py                 # platform + tool discovery (ffmpeg/ffprobe/realesrgan/zimg), encoder detection
  probe.py                 # ffprobe wrapper -> VideoInfo (incl. HDR detection)
  plan.py                  # preset->height, preset guard, chunk planning, output naming
  encoders.py              # encoder profiles + normalized quality mapping + auto-select
  ffmpeg_cmds.py           # pure command builders (split/extract/tonemap/encode/concat/mux)
  pipeline.py              # single-file run(): chunk loop, resume, manifest I/O
  batch.py                 # discovery + per-file loop + summary
tests/
  test_plan.py             # preset mapping, preset guard, chunk/output-name logic
  test_encoders.py         # profile selection per platform + quality mapping (mocked)
  test_ffmpeg_cmds.py      # command construction incl. tonemap + mux + per-encoder args
  test_probe.py            # ffprobe JSON parsing incl. HDR detection (fixtures)
  test_batch.py            # directory discovery, extension filter, recursion, skip-existing
  test_integration.py      # end-to-end on a generated testsrc clip (slow-marked)
README.md                  # English (install: macOS brew + Linux release; UTM/Venus note)
requirements.txt           # tqdm (optional)
.gitignore                 # *.work/, __pycache__/, output media
```

Language: Python 3.11+, standard library + `subprocess`; `tqdm` optional (graceful
fallback).

## 13. Testing Strategy

- **Unit (fast, no GPU):** preset→height, preset guard, chunk planning, output naming
  (incl. recursive relative-path mirroring + collision error), encoder selection +
  quality mapping (platform/`ffmpeg -encoders` mocked), ffmpeg command builders (assert
  arg lists incl. tonemap injection, mux mapping with `-map_metadata`/`-map_chapters`,
  per-encoder args, rational `-framerate`), frame-count reconciliation logic, vfr-mode
  handling (error/cfr/warn), ffprobe JSON parsing incl. HDR + VFR detection (fixtures),
  batch discovery (extensions, recursion, skip-existing).
- **Integration (slow, needs GPU + tools):** generate a 2 s `testsrc` clip, run the full
  pipeline at 720p, assert output exists, correct dimensions, duration ≈ input, audio
  preserved. A second fixture exercises the preset-guard skip path; a small directory
  fixture exercises batch discovery + per-file resume.
- **CI-without-GPU option:** a stub realesrgan (copies/lanczos-scales frames) exercises
  orchestration/resume/batch logic without the GPU model.

## 14. Deferred

- GPU/CPU pipelining (approach B) — revisit only if profiling shows CPU extract/encode
  is a meaningful fraction of wall time.
- Full HDR preservation (BT.2020/PQ 10-bit through the upscaler).
- Timestamp-preserving VFR handling (v1 offers `--vfr-mode cfr` conformance or error).
- Tested first-class Linux/UTM validation; additional models (e.g. anime) via `--model`.
- 10-bit output profiles.
