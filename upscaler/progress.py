"""Structured progress events for GUI consumers.

The CLI's human-readable output is not parseable when segments run in
parallel: worker lines interleave and per-segment percentages cannot be
attributed to a segment. When the GUI launches the CLI it sets
VIDEO_UPSCALER_CLI=1, which turns on a one-line-per-event JSON protocol
alongside the normal output:

    @@RAVIVE {"t": "seg", "seg": "seg_0000.mkv", "stage": "upscale", "pct": 42.0}

Consumers filter lines starting with EVENT_PREFIX out of the log view.
Stage weights let a segment's overall percentage account for extraction
and encoding, not just the upscaling pass.
"""
import json
import os
import sys
import threading
from typing import Any

EVENT_PREFIX = "@@RAVIVE "

# Fractions of a segment's wall-clock work, by stage. Upscaling dominates.
STAGE_WEIGHTS = {
    "extract": 0.05,
    "upscale": 0.85,
    "encode": 0.10,
}

_emit_lock = threading.Lock()


def events_enabled() -> bool:
    return os.environ.get("VIDEO_UPSCALER_CLI") == "1"


def emit(**event: Any) -> None:
    """Write a single JSON progress event, if a GUI consumer is listening."""
    if not events_enabled():
        return
    line = EVENT_PREFIX + json.dumps(event, separators=(",", ":"))
    # Workers emit concurrently; keep each event on its own unbroken line.
    with _emit_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def segment_pct(stage: str, stage_pct: float) -> float:
    """Map a within-stage percentage onto the segment's overall percentage."""
    completed = 0.0
    for name, weight in STAGE_WEIGHTS.items():
        if name == stage:
            break
        completed += weight
    stage_pct = max(0.0, min(100.0, stage_pct))
    return (completed + STAGE_WEIGHTS.get(stage, 0.0) * stage_pct / 100.0) * 100.0
