<!-- GSD:project-start source:PROJECT.md -->
## Project

**Ravive**

A robust, resumable desktop app and CLI pipeline for macOS that upscales videos using Real-ESRGAN-ncnn-vulkan (GPU-accelerated inference) for frame upscaling and ffmpeg for demuxing, downscaling, and hardware-accelerated re-encoding.

**Core Value:** Providing a 100% self-contained, fully signed and notarized application bundle that delivers fast, high-quality, and robust video upscaling on macOS.

### Constraints

- **Tech Stack**: PyInstaller for packaging, pywebview for GUI, subprocess-based CLI invocation.
- **Hardware**: Vulkan-capable GPU required (Apple Silicon / Metal virtualization).
<!-- GSD:project-end -->

<!-- GSD:stack-start source:codebase/STACK.md -->
## Technology Stack

## Languages
- Python 3.14 - All application logic (CLI pipeline in `upscale.py` + `upscaler/`, GUI server in `gui.py`, native shell in `app.py`)
- HTML/CSS/JavaScript - Embedded single-page GUI served as a string literal from `gui.py` (see the `INDEX_HTML` payload and inline `<script>` around `gui.py:848`); talks to the Python backend over HTTP and the pywebview `js_api` bridge.
## Runtime
- CPython 3.14.5 (Homebrew `python@3.14`, Apple Silicon / arm64)
- Virtualenv at `.venv/` (`.venv/pyvenv.cfg`), not committed
- pip (`pip`, `pip3.14` in `.venv/bin/`)
- Lockfile: missing (only unpinned `requirements.txt`; no `poetry.lock`/`Pipfile.lock`)
## Frameworks
- pywebview - Native macOS window wrapping the local web GUI (`app.py`, imports `webview`; `webview.create_window`, `webview.start`)
- `http.server` (stdlib `BaseHTTPRequestHandler`/`HTTPServer`) - Local GUI backend on `127.0.0.1:8080` (`gui.py:7`)
- pytest - Unit + integration tests under `tests/` (`tests/test_*.py`), config cache in `.pytest_cache/`
- PyInstaller - Packages the app into `AppleSiliconVideoUpscaler.app` (`AppleSiliconVideoUpscaler.spec`, `.venv/bin/pyinstaller`); UPX compression enabled, icon `logo.icns`
- UPX - Binary compression during PyInstaller `EXE`/`COLLECT`
## Key Dependencies
- `tqdm` - Progress bar rendering for CLI batch/pipeline stages
- `pytest` - Test runner (dev/test dependency listed alongside runtime deps)
- `Pillow` - Image handling for upscaled frame processing
- `pywebview` - Native GUI (imported in `app.py`; NOT listed in `requirements.txt` — undeclared runtime dependency)
- `ffmpeg` / `ffprobe` - Demux, split, downscale, filter (HDR tonemap, VFR CFR conform, hqdn3d), and hardware/software re-encode. Resolved via `shutil.which` in `upscaler/tools.py` (`get_ffmpeg_path`, `get_ffprobe_path`). Requires ffmpeg >= 5.1.
- `realesrgan-ncnn-vulkan` - GPU (Vulkan) Real-ESRGAN frame upscaling. Located via `upscaler/tools.py:find_realesrgan`. A bundled universal (x86_64 + arm64) Mach-O binary plus models ships under `realesrgan/` (gitignored).
- Vulkan (via ncnn) - GPU inference backend for Real-ESRGAN, including Venus Vulkan virtualization under UTM on Apple Silicon
- Apple VideoToolbox / NVIDIA NVENC / Intel-AMD VAAPI / libx265 - Encoder backends selected at runtime (`upscaler/encoders.py`)
## Configuration
- `VIDEO_UPSCALER_CLI=1` - Switches `app.py` from GUI mode to CLI helper mode (`app.py` `__main__`, set by `gui.py` when spawning the worker)
- `PYTHONUNBUFFERED=1` - Set by `gui.py` on the subprocess env to stream progress without buffering
- No `.env` file present; no secret-bearing configuration detected
- `AppleSiliconVideoUpscaler.spec` - PyInstaller build config; bundles `upscale.py`, `upscaler/`, `gui.py`, `logo.jpg` as data; entry point `app.py`
- No `pyproject.toml` / `setup.py` — project is script-based, not an installable package
## Platform Requirements
- macOS on Apple Silicon (arm64), Python 3.14 via Homebrew
- `ffmpeg` and `realesrgan-ncnn-vulkan` on PATH (`brew install ffmpeg realesrgan-ncnn-vulkan`)
- Packaged macOS `.app` bundle (PyInstaller) for Apple Silicon
- Also architected for Linux (NVENC / VAAPI / libx265) and Ubuntu-on-UTM with Venus Vulkan (see `README.md`)
<!-- GSD:stack-end -->

<!-- GSD:conventions-start source:CONVENTIONS.md -->
## Conventions

## Naming Patterns
- Lowercase `snake_case` module names: `ffmpeg_cmds.py`, `probe.py`, `plan.py`, `batch.py`, `encoders.py`, `pipeline.py`, `tools.py`
- Package lives in `upscaler/`; CLI entry point is top-level `upscale.py`; GUI is top-level `gui.py`; PyInstaller launcher is `app.py`
- Test modules mirror source with `test_` prefix: `tests/test_plan.py` ↔ `upscaler/plan.py`
- `snake_case`, verb-first, descriptive: `check_preset_guard`, `resolve_output_paths`, `build_encode_cmd`, `get_exact_frame_count`, `run_cmd_checked`
- Command builders consistently prefixed `build_*` (`upscaler/ffmpeg_cmds.py`): `build_split_cmd`, `build_extract_cmd`, `build_realesrgan_cmd`, `build_encode_cmd`, `build_concat_cmd`, `build_remux_cmd`
- Tool discovery prefixed `get_*`: `get_ffmpeg_path`, `get_ffprobe_path`, `get_ffmpeg_version` (`upscaler/tools.py`)
- Validation guards prefixed `check_*` and raise on failure: `check_preset_guard`, `check_vfr_mode`, `check_hdr_mode` (`upscaler/plan.py`)
- `snake_case` throughout: `target_height`, `seg_in_dir`, `frames_per_chunk`, `resolved_params`
- Path variables suffixed `_path`, `_dir`, `_abs`: `input_abs`, `work_dir`, `manifest_path`, `seg_out_dir`
- Module-level constants `UPPER_SNAKE`: `PRESETS`, `PRESET_BITRATES`, `SUPPORTED_EXTENSIONS`
- `NamedTuple` for structured returns: `VideoInfo` (`upscaler/probe.py:9`)
- Exception classes `CamelCase` ending in `Error`, all subclass `UpscalerError` (`upscaler/__init__.py`)
## Code Style
- No formatter config present (no `.prettierrc`, no `black`/`ruff` config, no `pyproject.toml`)
- De facto style: 4-space indentation, PEP 8 aligned, roughly 100-char lines
- Some trailing whitespace on blank lines exists (e.g. `upscaler/pipeline.py`) — not enforced
- No linter configured. Conventions are enforced by convention, not tooling.
## Import Organization
- None. Package uses explicit relative imports (`from .`) within `upscaler/`; entry points use absolute package imports (`from upscaler.tools import verify_tools`).
- Optional/heavy deps imported inside functions to keep them optional: `from tqdm import tqdm` (`upscaler/pipeline.py:289`), `from PIL import Image` (`upscaler/probe.py:213`), `from .probe import detect_video_type` (`upscaler/pipeline.py:197`)
## Error Handling
- `UpscalerError` is the base for all *expected, user-facing* errors.
- Subclasses signal specific conditions: `ToolError`, `ProbeError`, `PresetGuardError`, `VFRError`, `HDRError`, `ReconciliationError`, `SubprocessError`, `ManifestMismatchError`, `DiskEstimateError`.
- All exported via `__all__`.
- Library code raises typed `UpscalerError` subclasses with rich, multi-line messages including context (file, chunk, stage, expected vs actual). Example: `SubprocessError` in `run_cmd_checked` (`upscaler/pipeline.py:57`).
- The CLI boundary (`upscale.py:main`) is the *only* place that catches broadly: catches `ToolError`/`UpscalerError` → prints `ERROR: ...` to stderr and returns exit code `1`; `KeyboardInterrupt` → returns `130`; unexpected `Exception` → prints traceback and returns `1`.
- Subprocess wrappers re-raise their own typed error but avoid double-wrapping: `if isinstance(e, SubprocessError): raise` (`upscaler/pipeline.py:72`).
- Best-effort fallbacks swallow exceptions with bare `except Exception: pass` when a degraded result is acceptable (frame counting `upscaler/pipeline.py:47`, content-type detection `upscaler/probe.py`).
## Logging
- Progress rendered as a manual ASCII bar in `run_realesrgan_stream` (`upscaler/pipeline.py:106`) using `█`/`░` and `\r` carriage returns.
- Optional `tqdm` progress bar for sequential segment processing.
- Warnings surfaced as `print(f"WARNING: {warning}")` (e.g. remux stream drops, `upscaler/pipeline.py:486`).
- GUI (`gui.py`) captures subprocess output into a shared `task_state` dict guarded by `task_lock`.
## Comments
- Explain *why*, especially non-obvious pipeline/ffmpeg decisions and platform quirks (e.g. macOS bundle CWD fallback `upscaler/pipeline.py:225`, Cocoa/PyInstaller arg filtering `upscale.py:14`).
- Numbered stage comments narrate the pipeline (`# 1. Probe input`, `# 2. Preset Guard`, ... in `run_single_file`).
- Triple-quoted docstrings on non-trivial public functions describe purpose, return shape, and raise conditions (e.g. `resolve_output_paths`, `estimate_disk_usage`). Google-ish prose, not reStructuredText/Napoleon.
- Exception classes carry one-line docstrings describing the condition.
- Simple guard/getter functions often have no docstring.
## Function Design
- Type hints on nearly all signatures (params and returns), using `typing` (`Dict`, `List`, `Optional`, `Tuple`, `Any`).
- Options threaded through the pipeline as an `opts: Dict[str, Any]` dict (derived from `vars(argparse.Namespace)`), accessed with `opts["key"]` for required and `opts.get("key", default)` for optional. New CLI flags flow automatically from `upscale.py` into `opts`.
- `tools_info: Dict[str, Any]` similarly carries resolved tool paths/capabilities.
- Pure/testable functions return values (command lists, path mappings, `VideoInfo`); side-effecting orchestration returns `None` or an exit-code `int`.
- Command builders return `List[str]` argv lists (never shell strings) — always invoked without `shell=True`.
## Module Design
- `plan.py` — pure validation + path/disk planning (no subprocess execution)
- `ffmpeg_cmds.py` — pure argv builders (no execution), making commands unit-testable
- `pipeline.py` — orchestration + subprocess execution
- `encoders.py` — encoder selection + arg mapping
- `probe.py` / `tools.py` — external tool interaction
- Only `upscaler/__init__.py` defines `__all__` (the error hierarchy + `__version__`). Other modules rely on direct name imports.
<!-- GSD:conventions-end -->

<!-- GSD:architecture-start source:ARCHITECTURE.md -->
## Architecture

## System Overview
```text
```
## Component Responsibilities
| Component | Responsibility | File |
|-----------|----------------|------|
| Desktop shell | Native window, native file/folder dialogs via JS API, process cleanup on close | `app.py` |
| UI server | Serves embedded HTML UI, exposes `/upscale` `/status` `/cancel` `/explore` endpoints, manages `task_state`, spawns and streams the CLI child process | `gui.py` |
| CLI arg layer | Parse args, verify tools, resolve encoder, dispatch batch | `upscale.py` |
| Batch orchestration | Discover inputs, resolve output paths, iterate files, fail-fast handling | `upscaler/batch.py` |
| Single-file pipeline | Split → extract → upscale → encode → concat → remux, manifest resume, frame reconciliation | `upscaler/pipeline.py` |
| Media probing | ffprobe wrapper → `VideoInfo` (dims, fps, HDR, VFR, audio) | `upscaler/probe.py` |
| Planning/guards | Preset guard, VFR/HDR mode checks, disk estimation, output path resolution | `upscaler/plan.py` |
| ffmpeg command builders | Pure functions building ffmpeg/realesrgan argv lists | `upscaler/ffmpeg_cmds.py` |
| Encoder selection | Map quality→CRF, select HW/SW encoder, build encoder args | `upscaler/encoders.py` |
| Tool detection | Locate ffmpeg/ffprobe, parse versions, enumerate encoders | `upscaler/tools.py` |
| Error taxonomy | Shared `UpscalerError` hierarchy | `upscaler/__init__.py` |
## Pattern Overview
- Single-binary reuse: `app.py` runs as GUI, or as CLI when `VIDEO_UPSCALER_CLI=1` (same executable, branch in `app.py:70`).
- Pure command builders (`ffmpeg_cmds.py`, `encoders.py`) separated from subprocess execution (`pipeline.py`), making them unit-testable.
- Resumable pipeline via per-file JSON manifest and frame-count reconciliation.
- No web framework: `gui.py` uses stdlib `http.server` with an embedded HTML string.
## Layers
- Purpose: Wrap the local server in a native macOS webview window; provide Finder dialogs.
- Depends on: `pywebview`, `gui.main`.
- Used by: PyInstaller bundle entry (`AppleSiliconVideoUpscaler.spec`).
- Purpose: Serve UI, translate form input into CLI args, stream child-process output into `task_state`.
- Depends on: stdlib `http.server`, `subprocess`, `upscale.py` (as child).
- Used by: `app.py` (thread) or run standalone.
- Purpose: Argument surface + orchestration entry.
- Depends on: `upscaler.tools`, `upscaler.encoders`, `upscaler.batch`.
- Purpose: All media logic. `batch` → `pipeline` → (`probe`, `plan`, `ffmpeg_cmds`, `encoders`, `tools`).
## Data Flow
### Primary Upscale Path (GUI)
### CLI-only Path
- Global `active_process` and `task_state` dicts in `gui.py` (single in-flight job).
- Resumability via per-file manifest JSON in the work dir (`load_or_create_manifest`, `pipeline.py:131`).
## Key Abstractions
- Purpose: Immutable probe result carrying dims/fps/HDR/VFR/audio flags.
- File: `upscaler/probe.py`.
- Purpose: Distinguish clean user-facing failures from unexpected crashes.
- Subclasses: `ToolError`, `ProbeError`, `PresetGuardError`, `VFRError`, `HDRError`, `ReconciliationError`, `SubprocessError`, `ManifestMismatchError`, `DiskEstimateError` (`upscaler/__init__.py`).
- Pure functions returning argv lists: `build_split_cmd`, `build_extract_cmd`, `build_realesrgan_cmd`, `build_encode_cmd`, `build_concat_cmd`, `build_remux_cmd` (`upscaler/ffmpeg_cmds.py`).
## Entry Points
## Architectural Constraints
- **Threading:** UI server runs the upscale job on a background thread; Real-ESRGAN Vulkan on Apple Silicon requires single-threaded invocation to avoid a driver deadlock (see commit `77fa237`), so `--jobs`/worker concurrency is constrained on that path.
- **Global state:** `active_process` and `task_state` are module-level singletons in `gui.py` — only one upscale job at a time.
- **Single-executable dual mode:** behavior branches on `VIDEO_UPSCALER_CLI` env var (`app.py:71`); breaking that contract breaks GUI→CLI dispatch.
- **Argument sanitization:** macOS Cocoa (`-psn_`) and PyInstaller flags (`-B -S -I -c`) must be filtered in `upscale.py:15-16` or argparse fails inside the bundle.
- **Bundle path resolution:** asset lookups must handle `sys._MEIPASS` (frozen) vs source dir (`gui.py:35-37`).
## Anti-Patterns
### Embedded HTML/JS in a Python string
### Char-by-char stdout parsing for progress
## Error Handling
- Subprocess failures wrapped in `SubprocessError` with file/chunk/stage context (`pipeline.py:57-79`).
- Frame-count mismatches raise `ReconciliationError` (`pipeline.py:362-409`).
## Cross-Cutting Concerns
<!-- GSD:architecture-end -->

<!-- GSD:skills-start source:skills/ -->
## Project Skills

No project skills found. Add skills to any of: `.claude/skills/`, `.agents/skills/`, `.cursor/skills/`, `.github/skills/`, or `.codex/skills/` with a `SKILL.md` index file.
<!-- GSD:skills-end -->

<!-- GSD:workflow-start source:GSD defaults -->
## GSD Workflow Enforcement

Before using Edit, Write, or other file-changing tools, start work through a GSD command so planning artifacts and execution context stay in sync.

Use these entry points:
- `/gsd-quick` for small fixes, doc updates, and ad-hoc tasks
- `/gsd-debug` for investigation and bug fixing
- `/gsd-execute-phase` for planned phase work

Do not make direct repo edits outside a GSD workflow unless the user explicitly asks to bypass it.
<!-- GSD:workflow-end -->



<!-- GSD:profile-start -->
## Developer Profile

> Profile not yet configured. Run `/gsd-profile-user` to generate your developer profile.
> This section is managed by `generate-claude-profile` -- do not edit manually.
<!-- GSD:profile-end -->
