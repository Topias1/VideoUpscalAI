"""Apple Silicon Video Upscaler.

A robust, resumable CLI pipeline that upscales live-action video using
Real-ESRGAN-ncnn-vulkan for frame upscaling and ffmpeg for demux, downscale,
and re-encode. See docs/superpowers/specs for the design.
"""

__version__ = "1.0.0"


class UpscalerError(Exception):
    """Base class for all expected, user-facing errors in this package.

    Raising an ``UpscalerError`` (or subclass) signals a condition we can
    describe cleanly to the user (bad input, missing tool, param mismatch,
    frame-count reconciliation failure) rather than an unexpected crash.
    """


class ToolError(UpscalerError):
    """A required external tool is missing, too old, or lacks a feature."""


class ProbeError(UpscalerError):
    """ffprobe failed or returned data we cannot interpret."""


class PresetGuardError(UpscalerError):
    """Source is already >= the target preset height (nothing to upscale)."""


class VFRError(UpscalerError):
    """Source is variable-frame-rate and ``--vfr-mode error`` is set."""


class HDRError(UpscalerError):
    """Source is HDR and ``--hdr-mode error`` is set."""


class ReconciliationError(UpscalerError):
    """A per-chunk frame-count assertion failed (e.g. open-GOP frame drop)."""


class SubprocessError(UpscalerError):
    """An external command exited non-zero; carries file/chunk/stage context."""


class ManifestMismatchError(UpscalerError):
    """Resuming with parameters that differ from the recorded manifest."""


class DiskEstimateError(UpscalerError):
    """A segment or the run would exceed the estimated disk budget."""


__all__ = [
    "__version__",
    "UpscalerError",
    "ToolError",
    "ProbeError",
    "PresetGuardError",
    "VFRError",
    "HDRError",
    "ReconciliationError",
    "SubprocessError",
    "ManifestMismatchError",
    "DiskEstimateError",
]
