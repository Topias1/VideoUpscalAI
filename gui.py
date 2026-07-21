import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from upscaler.progress import EVENT_PREFIX

# Global state to track upscaling task
task_state = {
    "status": "idle",       # idle, running, completed, failed, cancelling, cancelled
    "progress": 0.0,
    "current_segment": "",
    "logs": [],
    "output_file": "",
    "error_hint": "",       # short actionable line, populated only when status == "failed"
    "workers": []           # [{"seg": "seg_0000.mkv", "pct": 42.0, "stage": "upscale"}]
}

task_lock = threading.Lock()
active_process = None


def _append_log(line):
    """Append a new line to the rolling log, capped at 100 lines. Sole
    choke point for appends so the cap can't be forgotten at a call site."""
    task_state["logs"].append(line)
    if len(task_state["logs"]) > 100:
        task_state["logs"].pop(0)


def _replace_last_log(line):
    """Overwrite the last line in place (realesrgan progress ticking up) —
    same logical line, new text."""
    if task_state["logs"]:
        task_state["logs"][-1] = line
    else:
        _append_log(line)


def classify_failure(logs):
    """Maps a known pipeline error to a short, actionable line.

    Matches the exact exception text the CLI prints (see upscaler/plan.py,
    upscaler/probe.py). Falls back to a generic line for anything else —
    the full log stays visible either way.
    """
    text = "\n".join(logs)
    if "already greater than or equal to the target preset" in text:
        return "The source is already at or above that resolution. Pick a larger preset and try again."
    if "Low disk space on workspace drive" in text:
        return "There isn't enough free disk space to process this file. Free up space and try again."
    if ("ffprobe failed to read metadata" in text
            or "No video stream found" in text
            or "Invalid video dimensions" in text):
        return "This file couldn't be read. It may be corrupted or in a format Ravive doesn't support."
    return "Something went wrong during processing."


def _finalize_run(returncode):
    """Reconcile the terminal task_state after the CLI subprocess exits.
    Must be called with task_lock held.

    /cancel flips status to "cancelling" and only then calls terminate() --
    that ordering guarantees this function always observes "cancelling"
    before it can observe any returncode caused by that terminate() (a
    process cannot exit before it has been signalled). So a cancelled run
    can never be reported as "failed" just because SIGTERM produced a
    non-zero exit code.
    """
    if task_state["status"] == "cancelling":
        task_state["status"] = "cancelled"
        _append_log("Process cancelled by user.")
        return
    if returncode == 0:
        task_state["status"] = "completed"
        task_state["progress"] = 100.0
        task_state["current_segment"] = "Done"
        _append_log("Upscaling finished.")
    else:
        task_state["status"] = "failed"
        task_state["current_segment"] = "Couldn't finish"
        task_state["error_hint"] = classify_failure(task_state["logs"])
        _append_log(f"Process exited with code {returncode}.")


def _handle_cancel_request():
    """Body of GET /cancel. Acquires task_lock itself -- do not call while
    already holding it."""
    proc = None
    with task_lock:
        proc = active_process
        if task_state["status"] == "running":
            # Set the transitional status *before* terminate() is ever
            # called -- see _finalize_run's docstring for why this
            # ordering is what makes the race safe.
            task_state["status"] = "cancelling"
            task_state["current_segment"] = "Cancelling..."
            _append_log("Cancelling...")
    if proc:
        try:
            proc.terminate()
        except Exception:
            pass


def _resolve_reveal_target():
    """Return the job's output file if it's safe to reveal, else None.
    Never trusts client input -- only ever reveals the path the pipeline
    itself just produced, and only once the run is actually done."""
    with task_lock:
        output_path = task_state.get("output_file", "")
        status = task_state.get("status")
    if status == "completed" and output_path and os.path.isfile(output_path):
        return output_path
    return None

# Segments processed concurrently. Not exposed in the UI: the upscaling pass
# is serialised on the single GPU, so raising this cannot speed up inference —
# it only lets one segment's extraction/encoding overlap another's GPU work,
# at the cost of holding that many frame sets on disk at once.
GUI_WORKERS = 2


class ProgressTracker:
    """Turns the CLI's @@RAVIVE events into an overall percentage.

    Progress is weighted by real work: every segment of every file counts
    the same, and a segment's own percentage spans its extract/upscale/
    encode stages. Segments processed in parallel are tracked individually
    so the GUI can show one bar per worker.
    """

    def __init__(self):
        self.total_files = 1
        self.file_idx = 1
        self.total_segs = 1
        self.active = {}        # seg name -> {"pct": float, "stage": str}
        self.done = set()

    def handle(self, event):
        t = event.get("t")
        if t == "file":
            self.total_files = max(1, int(event.get("total", 1)))
            self.file_idx = max(1, int(event.get("idx", 1)))
            # Segment tracking is per-file.
            self.total_segs = 1
            self.active = {}
            self.done = set()
        elif t == "segs":
            self.total_segs = max(1, int(event.get("total", 1)))
            self.active = {}
            self.done = set()
        elif t == "seg":
            seg = event.get("seg")
            if seg and seg not in self.done:
                self.active[seg] = {
                    "pct": max(0.0, min(100.0, float(event.get("pct", 0.0)))),
                    "stage": event.get("stage", ""),
                }
        elif t == "seg_done":
            seg = event.get("seg")
            if seg:
                self.done.add(seg)
                self.active.pop(seg, None)

    def file_progress(self):
        """Percentage of the current file, 0-100."""
        completed = min(len(self.done), self.total_segs)
        in_flight = sum(w["pct"] for w in self.active.values()) / 100.0
        segs_done = min(float(self.total_segs), completed + in_flight)
        return segs_done / self.total_segs * 100.0

    def overall_progress(self):
        overall = ((self.file_idx - 1) * 100.0 + self.file_progress()) / self.total_files
        return max(0.0, min(100.0, overall))

    def worker_bars(self):
        """One entry per in-flight segment, ordered for a stable display."""
        return [
            {"seg": seg, "pct": w["pct"], "stage": w["stage"]}
            for seg, w in sorted(self.active.items())
        ]

def run_upscale_thread(cmd_args):
    global active_process
    with task_lock:
        task_state["status"] = "running"
        task_state["progress"] = 0.0
        task_state["current_segment"] = "Starting split..."
        task_state["logs"] = []
        task_state["output_file"] = ""
        task_state["error_hint"] = ""
        task_state["workers"] = []
        _append_log("Starting upscaler CLI pipeline...")

    # Everything below runs inside the try: a failure here used to kill the
    # thread outright, leaving the UI stuck on "running" forever.
    try:
        if getattr(sys, 'frozen', False):
            bundle_dir = sys._MEIPASS
            python_bin = sys.executable
            cmd = [python_bin] + cmd_args
        else:
            bundle_dir = os.path.dirname(os.path.abspath(__file__))
            python_bin = os.path.join(bundle_dir, ".venv", "bin", "python")
            if not os.path.exists(python_bin):
                python_bin = sys.executable
            upscale_script = os.path.join(bundle_dir, "upscale.py")
            cmd = [python_bin, upscale_script] + cmd_args

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["VIDEO_UPSCALER_CLI"] = "1"
        
        active_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env
        )
        
        tracker = ProgressTracker()

        buffer = []
        while True:
            char = active_process.stdout.read(1)
            if not char:
                break
            if char in ("\r", "\n"):
                line = "".join(buffer).strip()
                buffer.clear()
                if line:
                    # Structured progress events drive the bars; they never
                    # reach the log view.
                    if line.startswith(EVENT_PREFIX):
                        try:
                            event = json.loads(line[len(EVENT_PREFIX):])
                        except json.JSONDecodeError:
                            continue
                        with task_lock:
                            tracker.handle(event)
                            task_state["progress"] = tracker.overall_progress()
                            task_state["workers"] = tracker.worker_bars()
                            if event.get("t") == "file":
                                task_state["current_segment"] = (
                                    f"Processing: {event.get('name', '')} "
                                    f"({event.get('idx')}/{event.get('total')})"
                                )
                        continue

                    with task_lock:
                        # Prevent realesrgan progress lines from flooding the log view
                        is_realesrgan_progress = "[realesrgan]" in line and "%" in line
                        if is_realesrgan_progress and task_state["logs"] and "[realesrgan]" in task_state["logs"][-1]:
                            _replace_last_log(line)
                        else:
                            _append_log(line)

                        if "Successfully upscaled" in line:
                            parts = line.split("->")
                            if len(parts) > 1:
                                task_state["output_file"] = parts[1].strip()
            else:
                buffer.append(char)
                
        active_process.wait()

        with task_lock:
            _finalize_run(active_process.returncode)
    except Exception as e:
        with task_lock:
            # An exception here (e.g. Popen itself failing) is a genuine
            # crash, never a cancellation -- /cancel only runs once
            # active_process already exists, so this path and "cancelling"
            # cannot overlap in practice, but guard anyway for safety.
            if task_state["status"] == "cancelling":
                task_state["status"] = "cancelled"
                _append_log("Process cancelled by user.")
            else:
                task_state["status"] = "failed"
                task_state["current_segment"] = "Couldn't finish"
                task_state["error_hint"] = "Ravive hit an unexpected error."
                _append_log(f"Unexpected error: {e}")
    finally:
        active_process = None

class GUIHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        
        if parsed_url.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_CONTENT.encode("utf-8"))
            
        elif parsed_url.path == "/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            with task_lock:
                self.wfile.write(json.dumps(task_state).encode("utf-8"))
                
        elif parsed_url.path == "/cancel":
            # A user-initiated cancel is not a failure (PRODUCT.md:
            # "cancelled is not failed"). _handle_cancel_request sets the
            # transitional "cancelling" status *before* terminate() runs,
            # so run_upscale_thread's finalize step can never mistake the
            # resulting SIGTERM exit code for a real failure.
            _handle_cancel_request()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"cancelled": True}).encode("utf-8"))
            
        elif parsed_url.path == "/logo.jpg":
            import sys
            if getattr(sys, 'frozen', False):
                logo_path = os.path.join(sys._MEIPASS, "logo.jpg")
            else:
                logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.jpg")
                
            if os.path.exists(logo_path):
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.end_headers()
                with open(logo_path, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_response(404)
                self.end_headers()
            
        elif parsed_url.path == "/explore":
            query = urllib.parse.parse_qs(parsed_url.query)
            path_param = query.get("path", [None])[0]
            
            if not path_param:
                current_path = os.path.expanduser("~")
            else:
                current_path = os.path.abspath(path_param)
                
            if not os.path.exists(current_path) or not os.path.isdir(current_path):
                current_path = os.path.expanduser("~")
                
            try:
                entries = []
                for entry in sorted(os.listdir(current_path)):
                    if entry.startswith(".") and not entry == ".work":
                        continue
                    full_path = os.path.join(current_path, entry)
                    is_dir = os.path.isdir(full_path)
                    
                    if not is_dir:
                        ext = os.path.splitext(entry)[1].lower()
                        if ext not in (".mp4", ".mkv", ".mov", ".avi", ".webm"):
                            continue
                            
                    entries.append({
                        "name": entry,
                        "path": full_path,
                        "is_dir": is_dir
                    })
                
                response_data = {
                    "current_path": current_path,
                    "parent_path": os.path.dirname(current_path) if current_path != "/" else "/",
                    "entries": entries
                }
                
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(response_data).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/reveal":
            # Never trusts client input: only ever reveals the path the
            # pipeline itself just produced, and only once the run is done.
            target = _resolve_reveal_target()
            revealed = False
            if target:
                try:
                    subprocess.run(["open", "-R", target], check=False)
                    revealed = True
                except Exception:
                    revealed = False
            self.send_response(200 if revealed else 404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"revealed": revealed}).encode("utf-8"))
            return

        if self.path == "/upscale":
            with task_lock:
                if task_state["status"] in ("running", "cancelling"):
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "Already running"}).encode("utf-8"))
                    return

            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            params = json.loads(post_data.decode("utf-8"))
            
            input_file = params.get("input_file")
            output_file = params.get("output_file")
            preset = params.get("preset", "720p")
            model = params.get("model", "auto")
            denoise = params.get("denoise", False)
            interpolate = params.get("interpolate", False)
            recursive = params.get("recursive", False)
            
            cmd_args = [input_file]
            if output_file:
                # The field is a destination *folder*. Passing it as -o would
                # make ffmpeg mux to an extension-less file and fail at the
                # very end of the run, so route it to --output-dir unless the
                # user typed a real filename.
                if os.path.isdir(output_file) or not os.path.splitext(output_file)[1]:
                    cmd_args.extend(["--output-dir", output_file])
                else:
                    cmd_args.extend(["-o", output_file])


            cmd_args.extend(["--preset", preset])
            cmd_args.extend(["--model", model])
            # Not user-facing: the upscaling pass is serialised on the single
            # GPU, so extra workers only overlap extraction/encoding while
            # multiplying transient disk use. Two is enough to keep the GPU fed.
            cmd_args.extend(["--workers", str(GUI_WORKERS)])
            cmd_args.append("--force")
            # Conform variable-frame-rate sources to CFR so they process
            # instead of erroring on the CLI's default --vfr-mode error.
            cmd_args.extend(["--vfr-mode", "cfr"])
            
            if denoise:
                cmd_args.append("--temporal-denoise")
            if interpolate:
                cmd_args.extend(["--interpolate-fps", "60"])
            if recursive:
                cmd_args.append("--recursive")
                
            thread = threading.Thread(target=run_upscale_thread, args=(cmd_args,))
            thread.daemon = True
            thread.start()
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"started": True}).encode("utf-8"))

HTML_CONTENT = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Ravive</title>
    <link rel="icon" href="/logo.jpg">
    <style>
        :root {
            color-scheme: dark;

            /* Neutral scale — native macOS dark grey, tinted 0.004-0.008 chroma. */
            --bg:            oklch(0.155 0.006 250);
            --surface-1:     oklch(0.205 0.006 250);   /* form-side: inputs, base */
            --surface-2:     oklch(0.235 0.008 250);   /* status panel, modal */
            --surface-3:     oklch(0.125 0.004 250);   /* log terminal, deepest */
            --border:        oklch(0.520 0.008 250);   /* component boundary, 3:1+ */
            --border-subtle: oklch(0.260 0.006 250);   /* decorative divider only */

            --text-primary:   oklch(0.930 0.006 250);  /* 15.9:1 on --bg */
            --text-secondary: oklch(0.780 0.008 250);  /* 9.8:1 on --bg */
            --text-muted:     oklch(0.685 0.010 250);  /* 6.3-6.9:1, incl. placeholder */
            --text-disabled:  oklch(0.400 0.006 250);  /* WCAG-exempt, dimmed only */

            /* Accent — single solid warm amber/gold. Used ONLY for: primary
               action, focus ring, current-progress fill, links. */
            --accent:        oklch(0.780 0.150 72);
            --accent-hover:  oklch(0.830 0.140 72);
            --accent-active: oklch(0.720 0.160 72);
            --on-accent:     oklch(0.160 0.010 72);    /* dark text for accent fills */

            /* Destructive — the Cancel action button only, never the
               Cancelled status (that's neutral; see below). */
            --danger:        oklch(0.720 0.160 25);
            --danger-hover:  oklch(0.770 0.150 25);

            /* Status vocabulary — text + surface verified together. */
            --idle-surface:       oklch(0.235 0.006 250);
            --idle-text:          oklch(0.700 0.008 250);
            --running-surface:    oklch(0.270 0.055 72);
            --running-text:       oklch(0.820 0.150 72);
            --completed-surface:  oklch(0.270 0.050 155);
            --completed-text:     oklch(0.800 0.150 155);
            --failed-surface:     oklch(0.270 0.065 25);
            --failed-text:        oklch(0.780 0.170 25);
            --cancelled-surface:  oklch(0.235 0.008 250);  /* neutral, deliberately == --surface-2 */
            --cancelled-text:     oklch(0.780 0.012 250);

            /* Terminal — same neutral/state system, genre-conventional hue. */
            --terminal-bg:   var(--surface-3);
            --terminal-text: oklch(0.800 0.130 150);

            /* Type scale — 400/500/600 only. --text-lg/--text-xl are
               intentional display breaks for the wordmark and the hero %
               readout, scanned from across the room. */
            --text-2xs: 0.75rem;
            --text-xs: 0.8125rem;
            --text-sm: 0.875rem;
            --text-base: 1rem;
            --text-md: 1.125rem;
            --text-lg: 1.5rem;
            --text-xl: 2rem;

            /* Spacing scale — 4/8-based. */
            --space-1: 0.25rem;
            --space-2: 0.5rem;
            --space-3: 0.75rem;
            --space-4: 1rem;
            --space-5: 1.25rem;
            --space-6: 1.5rem;
            --space-7: 2rem;
            --space-8: 2.5rem;

            /* Shared control height — inputs and their adjacent icon buttons. */
            --control-height: 2.5rem;

            /* Radius scale. */
            --radius-sm: 0.625rem;
            --radius-md: 0.75rem;
            --radius-lg: 1rem;
            --radius-xl: 1.25rem;
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        /* Visually hidden but readable by assistive tech (live region, legends). */
        .sr-only {
            position: absolute;
            width: 1px;
            height: 1px;
            padding: 0;
            margin: -1px;
            overflow: hidden;
            clip: rect(0, 0, 0, 0);
            white-space: nowrap;
            border: 0;
        }

        /* One consistent, high-contrast ring for every interactive element.
           Never removed without this replacing it. */
        :is(a, button, input, select, [tabindex]):focus-visible {
            outline: 3px solid var(--accent);
            outline-offset: 2px;
            border-radius: 4px;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', system-ui, sans-serif;
            -webkit-font-smoothing: antialiased;
            background-color: var(--bg);
            color: var(--text-primary);
            width: 100vw;
            height: 100vh;
            margin: 0;
            padding: 0;
            display: flex;
            justify-content: stretch;
            align-items: stretch;
            overflow: hidden;
        }

        .container {
            width: 100%;
            height: 100%;
            max-width: none;
            background: var(--bg);
            border: none;
            border-radius: 0;
            padding: var(--space-6) var(--space-6);
            display: flex;
            flex-direction: column;
            box-shadow: none;
        }

        .main-layout {
            display: grid;
            grid-template-columns: 1fr 1.2fr;
            gap: var(--space-6);
            align-items: stretch;
            flex: 1;
            min-height: 0;
        }

        /* While a job runs, the form collapses and the status panel takes
           the full width — progress and logs are all that matter then. */
        .main-layout.processing {
            grid-template-columns: 1fr;
        }

        .main-layout.processing .form-side {
            display: none;
        }

        /* Stack only below the app's own minimum window width (800px). The
           two-column split IS the layout at every size the shipped window can
           take (800x650 min, 900x750 default): the form column scrolls inside
           itself while the status panel and Start button stay pinned. Stacking
           at 62.5rem instead — as an earlier pass did — meant the shipped app
           never used the two-column path at all, and the stack is 1078px tall
           against a 650px window, which put Start below the bottom edge.
           The form's inner .grid is auto-fit, so preset/model stack on their
           own once the column narrows; nothing clips. */
        @media (max-width: 47.5rem) {
            /* The window must never scroll: this is a fixed desktop window
               (900x750 default, 800x650 minimum), and the stack is taller
               than the viewport at both. Sizing the rows `auto auto` — or
               letting .container scroll — squeezes the form and pushes Start
               past the bottom edge, which is where the primary action of the
               whole app then lives. Instead the form row absorbs the deficit
               and scrolls inside itself (see .form-side), while the status
               panel and the button row keep their natural height and stay
               pinned in view. */
            .main-layout:not(.processing) {
                grid-template-columns: 1fr;
                grid-template-rows: minmax(0, 1fr) auto;
            }
            .main-layout:not(.processing) .status-side {
                height: auto;
                min-height: 0;
            }
            /* Idle has nothing to report yet, so the empty log must not claim
               vertical space at the expense of the controls above it. */
            .main-layout:not(.processing) .log-terminal-wrap {
                max-height: 6rem;
            }
        }

        .form-side {
            display: flex;
            flex-direction: column;
            height: 100%;
            overflow-y: auto;
            padding-right: var(--space-2);
            min-height: 0;
        }

        .form-side::-webkit-scrollbar {
            width: 6px;
        }

        .form-side::-webkit-scrollbar-thumb {
            background: var(--border-subtle);
            border-radius: 3px;
        }

        .status-side {
            display: flex;
            flex-direction: column;
            height: 100%;
            min-height: 0;
            overflow: hidden;
        }

        h1 {
            font-size: var(--text-lg);
            font-weight: 600;
            color: var(--text-primary);
            margin: 0;
            text-align: center;
        }

        .subtitle {
            font-size: var(--text-sm);
            font-weight: 400;
            color: var(--text-muted);
            text-align: center;
            margin-bottom: var(--space-4);
        }

        .form-group {
            margin-bottom: var(--space-4);
        }

        label {
            display: block;
            font-size: var(--text-sm);
            font-weight: 600;
            margin-bottom: var(--space-2);
            color: var(--text-primary);
        }

        .input-with-btn {
            display: flex;
            align-items: center;
            gap: var(--space-2);
        }

        input[type="text"], select, input[type="number"] {
            width: 100%;
            height: var(--control-height);
            padding: 0 var(--space-3);
            background: var(--surface-1);
            border: 1px solid var(--border);
            border-radius: var(--radius-sm);
            color: var(--text-primary);
            font-family: inherit;
            font-size: var(--text-sm);
            transition: border-color 180ms ease-out, box-shadow 180ms ease-out;
        }

        input::placeholder {
            color: var(--text-muted);
            opacity: 1;
        }

        input[type="text"]:focus-visible, select:focus-visible, input[type="number"]:focus-visible {
            border-color: var(--accent);
            box-shadow: 0 0 0 3px color-mix(in oklch, var(--accent) 25%, transparent);
            outline: none;
        }

        input[type="text"]:disabled, select:disabled, input[type="number"]:disabled {
            background: var(--surface-1);
            color: var(--text-disabled);
            opacity: 1;
            cursor: not-allowed;
        }

        .btn-browse {
            width: var(--control-height);
            height: var(--control-height);
            padding: 0;
            flex: none;
            background: var(--surface-1);
            border: 1px solid var(--border);
            border-radius: var(--radius-sm);
            color: var(--text-primary);
            cursor: pointer;
            font-weight: 600;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            transition: background-color 180ms ease-out, border-color 180ms ease-out;
        }

        .btn-browse svg {
            width: 1rem;
            height: 1rem;
            flex: none;
        }

        .btn-browse:hover {
            background: color-mix(in oklch, var(--accent) 12%, var(--surface-1));
            border-color: var(--accent);
        }

        .btn-browse:active {
            background: color-mix(in oklch, var(--accent) 20%, var(--surface-1));
        }

        .btn-browse:disabled {
            opacity: 0.5;
            cursor: not-allowed;
            background: var(--surface-1);
            border-color: var(--border-subtle);
        }

        .grid {
            display: grid;
            /* 17rem = the longest option label's rendered width (231px) plus
               padding/border — stacks full-width instead of clipping. */
            grid-template-columns: repeat(auto-fit, minmax(17rem, 1fr));
            gap: var(--space-5);
        }

        .field-hint {
            font-size: var(--text-2xs);
            color: var(--text-muted);
            margin-top: var(--space-2);
        }

        .checkbox-group {
            display: flex;
            align-items: center;
            gap: var(--space-3);
            margin-top: var(--space-3);
        }

        input[type="checkbox"] {
            /* WCAG 2.2 §2.5.8: 24x24 CSS px minimum target size. */
            width: 24px;
            height: 24px;
            accent-color: var(--accent);
            cursor: pointer;
        }

        input[type="checkbox"]:hover {
            accent-color: var(--accent-hover);
        }

        input[type="checkbox"]:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }

        fieldset {
            border: none;
            padding: 0;
            margin: 0;
        }

        fieldset legend {
            display: block;
            font-size: var(--text-sm);
            font-weight: 600;
            margin-bottom: var(--space-2);
            color: var(--text-primary);
            padding: 0;
        }

        .form-hint {
            margin-top: auto;
            padding: var(--space-4);
            background: var(--surface-1);
            border: 1px solid var(--border-subtle);
            border-radius: var(--radius-md);
            font-size: var(--text-2xs);
            color: var(--text-muted);
            line-height: 1.5;
        }

        .form-hint strong {
            color: var(--text-primary);
            font-weight: 500;
        }

        .btn-container {
            display: flex;
            gap: var(--space-4);
            margin-top: var(--space-5);
        }

        button {
            flex: 1;
            padding: var(--space-4);
            font-family: inherit;
            font-size: var(--text-base);
            font-weight: 600;
            border: none;
            border-radius: var(--radius-sm);
            cursor: pointer;
        }

        .btn-primary {
            background: var(--accent);
            color: var(--on-accent);
            transition: background-color 180ms ease-out;
        }

        .btn-primary:hover {
            background: var(--accent-hover);
        }

        .btn-primary:active {
            background: var(--accent-active);
        }

        button:disabled {
            opacity: 0.5;
            cursor: not-allowed;
            box-shadow: none;
        }

        .btn-cancel {
            /* Not flex:1 like .btn-primary — once submitBtn is hidden for a
               running job this is the only child left in .btn-container,
               and a full-width danger-red button would outweigh the
               progress readout as the most saturated thing on screen for
               the whole job. Fixed intrinsic width, pinned right instead. */
            flex: none;
            width: auto;
            padding-left: var(--space-6);
            padding-right: var(--space-6);
            margin-left: auto;
            background: transparent;
            color: var(--danger);
            border: 1px solid var(--danger);
            transition: background-color 180ms ease-out;
        }

        .btn-cancel:hover {
            background: color-mix(in oklch, var(--danger) 15%, transparent);
        }

        .btn-cancel:active {
            background: color-mix(in oklch, var(--danger) 30%, transparent);
        }

        /* Progress Card */
        .progress-card {
            padding: var(--space-5);
            background: var(--surface-2);
            border: 1px solid var(--border-subtle);
            border-radius: var(--radius-lg);
            flex: 1;
            display: flex;
            flex-direction: column;
            min-height: 0;
            overflow: hidden;
        }

        .progress-title {
            display: flex;
            align-items: baseline;
            justify-content: space-between;
            gap: var(--space-4);
            font-weight: 600;
            margin-bottom: var(--space-2);
        }

        #progressSegment {
            flex: 1 1 auto;
            min-width: 0;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            font-size: var(--text-sm);
            font-weight: 500;
            color: var(--text-muted);
        }

        #progressText {
            flex: none;
            font-size: var(--text-xl);
            font-weight: 600;
            font-variant-numeric: tabular-nums;
            line-height: 1;
            color: var(--text-primary);
        }

        .progress-bar-container {
            width: 100%;
            height: 12px;
            background: var(--surface-1);
            border-radius: 6px;
            overflow: hidden;
            margin-bottom: var(--space-4);
        }

        .progress-bar-fill {
            width: 0%;
            height: 100%;
            background: var(--accent);
            /* 450ms, just under the 500ms poll cadence, so a fill finishes
               before the next update lands instead of visibly stepping. */
            transition: width 450ms linear;
        }

        /* Per-worker bars */
        .worker-list {
            display: flex;
            flex-direction: column;
            gap: var(--space-2);
            margin-bottom: var(--space-5);
        }

        .worker-row {
            display: flex;
            align-items: center;
            gap: var(--space-3);
            font-size: var(--text-xs);
            color: var(--text-muted);
        }

        .worker-label {
            width: 150px;
            flex: none;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        .worker-bar-container {
            flex: 1;
            height: 6px;
            background: var(--surface-1);
            border-radius: 3px;
            overflow: hidden;
        }

        .worker-bar-fill {
            display: block;   /* a span would stay inline and ignore height */
            width: 0%;
            height: 100%;
            background: var(--accent);
            transition: width 450ms linear;
        }

        .worker-pct {
            width: 48px;
            flex: none;
            text-align: right;
            font-variant-numeric: tabular-nums;
        }

        .log-terminal-wrap {
            position: relative;
            flex: 1;
            min-height: 0;
            margin-top: var(--space-3);
            display: flex;
        }

        .log-terminal {
            width: 100%;
            flex: 1;
            background: var(--terminal-bg);
            border: 1px solid var(--border-subtle);
            border-radius: var(--radius-md);
            padding: var(--space-3);
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            font-size: var(--text-xs);
            color: var(--terminal-text);
            overflow-y: auto;
            white-space: pre-wrap;
            min-height: 0;
        }

        .log-empty {
            margin: auto;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: var(--space-2);
            color: var(--text-muted);
            text-align: center;
        }

        .log-empty svg {
            width: 1.5rem;
            height: 1.5rem;
            opacity: 0.6;
        }

        .log-empty p {
            font-size: var(--text-xs);
            max-width: 20rem;
            line-height: 1.5;
        }

        .jump-to-latest {
            position: absolute;
            bottom: 12px;
            right: 12px;
            flex: none;
            padding: 6px 14px;
            font-size: var(--text-xs);
            font-weight: 600;
            border-radius: var(--radius-xl);
            border: 1px solid var(--border-subtle);
            background: var(--surface-2);
            color: var(--text-primary);
            cursor: pointer;
            transition: background-color 180ms ease-out, border-color 180ms ease-out;
        }

        .jump-to-latest:hover {
            background: color-mix(in oklch, var(--accent) 18%, var(--surface-2));
            border-color: var(--accent);
        }

        .jump-to-latest:active {
            background: color-mix(in oklch, var(--accent) 30%, var(--surface-2));
        }

        .jump-to-latest[hidden] {
            display: none;
        }

        .status-badge {
            display: inline-block;
            padding: 6px 12px;
            border-radius: var(--radius-xl);
            font-size: var(--text-xs);
            font-weight: 600;
            transition: background-color 200ms ease-out, color 200ms ease-out;
        }

        .status-message {
            font-size: var(--text-xs);
            color: var(--text-muted);
            margin-top: var(--space-2);
        }

        .result-panel {
            display: none;
            align-items: center;
            justify-content: space-between;
            gap: var(--space-3);
            margin-top: var(--space-2);
            padding: var(--space-3);
            background: var(--surface-1);
            border: 1px solid var(--border-subtle);
            border-radius: var(--radius-md);
            font-size: var(--text-xs);
        }

        .result-path {
            color: var(--text-muted);
            word-break: break-all;
        }

        .btn-secondary {
            flex: none;
            padding: 6px 14px;
            border-radius: var(--radius-sm);
            border: 1px solid var(--border);
            background: var(--surface-2);
            color: var(--text-primary);
            cursor: pointer;
            font-size: var(--text-xs);
            font-weight: 600;
            transition: background-color 180ms ease-out;
        }

        .btn-secondary:hover {
            background: color-mix(in oklch, var(--accent) 12%, var(--surface-2));
        }

        .status-idle       { background: var(--idle-surface);      color: var(--idle-text); }
        .status-running    { background: var(--running-surface);   color: var(--running-text); }
        .status-cancelling { background: var(--idle-surface);      color: var(--idle-text); }
        .status-completed  { background: var(--completed-surface); color: var(--completed-text); }
        .status-failed     { background: var(--failed-surface);    color: var(--failed-text); }
        .status-cancelled  { background: var(--cancelled-surface); color: var(--cancelled-text); }

        /* File Explorer Modal — native <dialog>. showModal() supplies the
           top layer, Escape-to-close, focus trap and inertness of the rest
           of the page; only cosmetics live here. The backdrop blur is kept
           deliberately (unlike .container's, which was removed) — it only
           renders while the dialog is open, not for the life of a 40-minute
           job, and it separates a foreground dialog from dissimilar content. */
        dialog#explorerModal {
            width: 90%;
            max-width: 600px;
            max-height: 80vh;
            padding: 0;
            border: 1px solid var(--border-subtle);
            border-radius: var(--radius-xl);
            background: var(--surface-2);
            color: inherit;
            box-shadow: 0 10px 30px oklch(0 0 0 / 0.5);
        }

        dialog#explorerModal::backdrop {
            /* Solid scrim, no backdrop-filter — the ban applies without
               exceptions, so this trades the blur for a slightly darker
               flat scrim rather than arguing the letter of the rule. */
            background-color: color-mix(in oklch, var(--bg) 85%, transparent);
        }

        .modal-content {
            display: flex;
            flex-direction: column;
            height: 100%;
            max-height: 80vh;
            padding: var(--space-6);
            overflow: hidden;
        }

        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: var(--space-4);
        }

        .close-btn {
            background: none;
            border: none;
            color: var(--text-muted);
            font-size: var(--text-lg);
            cursor: pointer;
            line-height: 1;
            padding: 0 4px;
        }

        .close-btn:hover {
            color: var(--text-primary);
        }

        .close-btn:active {
            color: var(--text-muted);
        }

        .breadcrumbs {
            font-size: var(--text-sm);
            color: var(--text-muted);
            background: var(--surface-1);
            padding: var(--space-2) var(--space-3);
            border-radius: var(--radius-md);
            margin-bottom: var(--space-4);
            word-break: break-all;
            display: flex;
            flex-wrap: wrap;
        }

        .breadcrumb-segment {
            color: var(--accent);
            cursor: pointer;
            border-radius: 4px;
        }

        .breadcrumb-segment:hover,
        .breadcrumb-segment:focus-visible {
            text-decoration: underline;
        }

        .breadcrumb-sep {
            color: var(--text-muted);
            padding: 0 2px;
        }

        .file-list {
            flex: 1;
            overflow-y: auto;
            min-height: 300px;
            max-height: 400px;
            border: 1px solid var(--border);
            border-radius: var(--radius-md);
            background: var(--surface-1);
        }

        /* Rows are real <button>s so they're keyboard-reachable and get the
           global focus-visible ring; reset the UA button chrome first. */
        .file-item {
            display: flex;
            align-items: center;
            gap: var(--space-3);
            width: 100%;
            padding: 10px 14px;
            border: none;
            border-bottom: 1px solid var(--border-subtle);
            background: none;
            color: inherit;
            font: inherit;
            text-align: left;
            cursor: pointer;
            transition: background-color 180ms ease-out;
        }

        .file-item:hover {
            background: color-mix(in oklch, var(--accent) 10%, transparent);
        }

        .file-item:active {
            background: color-mix(in oklch, var(--accent) 18%, transparent);
        }

        .file-icon {
            display: flex;
            flex: none;
            color: var(--text-muted);
        }

        .file-icon svg {
            width: 1.1rem;
            height: 1.1rem;
        }

        .file-name {
            font-size: var(--text-sm);
            color: var(--text-primary);
            word-break: break-all;
        }

        .modal-footer {
            margin-top: 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        @media (prefers-reduced-motion: reduce) {
            *, *::before, *::after {
                transition-duration: 0.01ms !important;
                animation-duration: 0.01ms !important;
                animation-iteration-count: 1 !important;
                scroll-behavior: auto !important;
            }

            dialog#explorerModal,
            dialog#explorerModal::backdrop {
                animation: none !important;
            }
        }
    </style>
</head>
<body>
    <div class="container" role="main">
        <div style="text-align: center; margin-bottom: 12px; display: flex; align-items: center; justify-content: center; gap: 16px;">
            <img src="/logo.jpg" alt="" style="width: 50px; height: 50px; border-radius: 12px; box-shadow: 0 5px 15px oklch(0 0 0 / 0.35); border: 2px solid var(--border-subtle); display: inline-block;">
            <h1>Ravive</h1>
        </div>
        <div class="subtitle" style="margin-bottom: 20px;">Upscales video locally. Nothing leaves this Mac.</div>

        <div id="liveAnnouncer" class="sr-only" aria-live="polite" aria-atomic="true"></div>

        <div class="main-layout">
            <div class="form-side">
                <form id="upscaleForm" onsubmit="startUpscale(event)">
                    <div class="form-group">
                        <label for="input_file">Source Video or Folder</label>
                        <div class="input-with-btn">
                            <input type="text" id="input_file" required placeholder="Select a file or folder...">
                            <button type="button" class="btn-browse" title="Choose file" aria-label="Choose file" onclick="openExplorer('input_file', false)">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 4h11l5 5v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1z"/><path d="M15 4v5h5"/></svg>
                            </button>
                            <button type="button" class="btn-browse" title="Choose folder" aria-label="Choose folder" onclick="openExplorer('input_file', true)">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6a1 1 0 0 1 1-1h5l2 2h9a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6z"/></svg>
                            </button>
                        </div>
                    </div>

            <div class="form-group">
                <label for="output_file">Output Folder (Optional)</label>
                <div class="input-with-btn">
                    <input type="text" id="output_file" placeholder="e.g. /Users/username/Downloads/">
                    <button type="button" class="btn-browse" title="Choose folder" aria-label="Choose folder" onclick="openExplorer('output_file', true)">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6a1 1 0 0 1 1-1h5l2 2h9a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6z"/></svg>
                    </button>
                </div>
            </div>

            <div class="grid">
                <div class="form-group">
                    <label for="preset">Resolution Preset</label>
                    <select id="preset">
                        <option value="480p">480p</option>
                        <option value="720p">720p</option>
                        <option value="1080p" selected>1080p</option>
                        <option value="4k">4K</option>
                    </select>
                </div>

                <div class="form-group">
                    <label for="model">AI Upscaling Model</label>
                    <select id="model">
                        <option value="auto" selected data-hint="Picks a model from your source: strong restoration below 720p, gentler above.">Auto Detection</option>
                        <option value="realesrgan-x4plus" data-hint="Strong restoration. Best on old or heavily compressed video (VHS captures, social-network exports).">Restore — old / damaged video</option>
                        <option value="high-fidelity-4x" data-hint="Gentler, stays closest to the source and flickers least. Best when the video is already sharp.">Refine — already sharp video</option>
                        <option value="realesr-animevideov3" data-hint="For 2D/3D animation.">Animation</option>
                        <option value="digital-art-4x" data-hint="For CGI renders and digital art.">CGI / digital art</option>
                        <option value="ultrasharp-4x" data-hint="Sharpest, but the least steady between frames — can shimmer on video.">UltraSharp (photos)</option>
                    </select>
                    <div class="field-hint" id="modelHint">Auto Detection picks the right model for your source.</div>
                </div>
            </div>

            <div>
                <div class="form-group">
                    <fieldset>
                        <legend>Options</legend>
                        <div class="checkbox-group">
                            <input type="checkbox" id="denoise">
                            <label for="denoise" style="margin-bottom: 0; font-weight: normal;">Reduce Flickering (Temporal Denoise)</label>
                        </div>
                        <div class="checkbox-group">
                            <input type="checkbox" id="interpolate">
                            <label for="interpolate" style="margin-bottom: 0; font-weight: normal;">Smooth to 60 FPS (Interpolation)</label>
                        </div>
                        <div class="checkbox-group">
                            <input type="checkbox" id="recursive">
                            <label for="recursive" style="margin-bottom: 0; font-weight: normal;">Process subfolders recursively</label>
                        </div>
                    </fieldset>
                </div>
            </div>

            <p class="form-hint"><strong>Defaults work.</strong> 1080p and Auto model handle most videos — pick a file and press Start.</p>
        </form>
    </div>

            <div class="status-side">
                <div class="progress-card" id="progressCard" role="region" aria-labelledby="progressHeading">
                    <h2 id="progressHeading" class="sr-only">Upscaling status</h2>
                    <div class="progress-title">
                        <span id="progressSegment">Ready to start</span>
                        <span id="progressText">0%</span>
                    </div>
                    <div class="progress-bar-container" style="margin-bottom: 6px;" role="progressbar" id="progressBarTrack" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" aria-valuetext="Ready to start" aria-labelledby="progressHeading">
                        <div class="progress-bar-fill" id="progressBar"></div>
                    </div>
                    <div id="timeEstimate" style="font-size: var(--text-xs); font-variant-numeric: tabular-nums; color: var(--text-muted); margin-bottom: 14px; text-align: right;">Elapsed: 00:00 | ETA: --:--</div>
                    <div class="worker-list" id="workerList"></div>
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <span>Status: <span class="status-badge status-idle" id="statusBadge">Idle</span></span>
                        <span id="outputSuccess" style="color: var(--completed-text); font-weight: 600;"></span>
                    </div>
                    <div class="status-message" id="statusMessage"></div>
                    <div class="result-panel" id="resultPanel">
                        <span class="result-path" id="resultPath"></span>
                        <button type="button" class="btn-secondary" id="revealBtn">Reveal in Finder</button>
                    </div>
                    <div class="log-terminal-wrap">
                        <div class="log-terminal" id="logTerminal" role="log" aria-live="off" aria-label="Job log" tabindex="0">
                            <div class="log-empty" id="logEmpty">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 17l6-6-6-6"/><path d="M12 19h8"/></svg>
                                <p>Processing output appears here once you press Start — extraction, upscaling, and encoding, segment by segment.</p>
                            </div>
                        </div>
                        <button type="button" id="jumpToLatestBtn" class="jump-to-latest" hidden>Jump to latest <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" style="width:0.9rem; height:0.9rem; vertical-align:-1px;"><path d="M6 9l6 6 6-6"/></svg></button>
                    </div>
                </div>

                <div class="btn-container">
                    <button type="submit" form="upscaleForm" class="btn-primary" id="submitBtn">Start AI Upscaling</button>
                    <button type="button" class="btn-cancel" id="cancelBtn" onclick="cancelUpscale()" style="display:none;">Cancel</button>
                </div>
            </div>
        </div>
    </div>

    <!-- File Explorer Modal: browser-fallback file picker. Only used when
         window.pywebview.api is absent — the shipped app uses native
         pywebview dialogs (see openExplorer()) and never opens this. -->
    <dialog id="explorerModal" aria-labelledby="explorerModalTitle">
        <div class="modal-content">
            <div class="modal-header">
                <h2 id="explorerModalTitle">File Explorer</h2>
                <button type="button" class="close-btn" onclick="closeExplorer()" aria-label="Close file browser"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" style="width:1.1rem; height:1.1rem;"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
            </div>
            <div class="breadcrumbs" id="breadcrumbs"></div>
            <div class="file-list" id="fileList"></div>
            <div class="modal-footer">
                <button type="button" class="btn-primary" id="selectCurrentFolderBtn" style="flex:none; width:auto; padding: 10px 16px;">Select Current Folder</button>
                <button type="button" class="btn-cancel" onclick="closeExplorer()" style="flex:none; width:auto; padding: 10px 16px;">Close</button>
            </div>
        </div>
    </dialog>

    <script>
        let pollInterval = null;
        let activeInputId = "";
        let upscaleStartTime = null;
        let explorerOpenerEl = null;
        let lastAnnouncedSegment = "";
        let lastAnnouncedMilestone = -1;
        let jobRunning = false;

        const STATUS_LABELS = {
            idle: "Idle",
            running: "Running",
            cancelling: "Cancelling…",
            completed: "Completed",
            failed: "Failed",
            cancelled: "Cancelled"
        };

        function announce(message) {
            document.getElementById("liveAnnouncer").textContent = message;
        }

        // Whether the log keeps following new output. Only a real user scroll
        // clears it. Deriving it per-poll from the scroll offset instead looks
        // equivalent but isn't: each poll appends output, so by the time the
        // next poll measures, the bottom has moved by more than any sane slack
        // and the log reads as "user scrolled away" after the first burst —
        // which is exactly when a 40-minute job needs to keep following.
        let logFollow = true;

        function isLogAtBottom(term) {
            // A line and a half of slack: sub-pixel layout, the terminal's own
            // bottom padding, and one line arriving mid-measurement.
            return term.scrollHeight - term.scrollTop - term.clientHeight < 24;
        }

        function scrollLogToBottom() {
            const term = document.getElementById("logTerminal");
            term.scrollTop = term.scrollHeight;
            logFollow = true;
            document.getElementById("jumpToLatestBtn").hidden = true;
        }

        function setFormDisabled(disabled) {
            const form = document.getElementById("upscaleForm");
            form.querySelectorAll("input, select, button").forEach(el => {
                el.disabled = disabled;
            });
            // Belt-and-suspenders: even if the "processing" layout class
            // fails to hide .form-side, inert keeps it out of the tab
            // order and off the accessibility tree.
            document.querySelector(".form-side").toggleAttribute("inert", disabled);
        }

        function formatTime(seconds) {
            const h = Math.floor(seconds / 3600);
            const m = Math.floor((seconds % 3600) / 60);
            const s = seconds % 60;
            const pad = (n) => String(n).padStart(2, "0");
            if (h > 0) {
                return `${pad(h)}:${pad(m)}:${pad(s)}`;
            }
            return `${pad(m)}:${pad(s)}`;
        }

        function openExplorer(inputId, isFolder = false) {
            if (window.pywebview && window.pywebview.api) {
                if (isFolder) {
                    window.pywebview.api.select_folder().then(path => {
                        if (path) document.getElementById(inputId).value = path;
                    });
                } else {
                    window.pywebview.api.select_file().then(path => {
                        if (path) document.getElementById(inputId).value = path;
                    });
                }
            } else {
                // Browser-fallback path only (see comment above the dialog
                // markup). showModal() gives Escape, a focus trap and
                // inertness of the rest of the page for free.
                activeInputId = inputId;
                explorerOpenerEl = document.activeElement;
                const dialog = document.getElementById("explorerModal");
                dialog.showModal();
                loadDir("");
            }
        }

        function closeExplorer() {
            const dialog = document.getElementById("explorerModal");
            if (dialog.open) dialog.close();
            // Return focus to whatever opened the dialog rather than
            // letting it fall back to <body>.
            if (explorerOpenerEl) {
                explorerOpenerEl.focus();
                explorerOpenerEl = null;
            }
        }

        // Static SVG markup (never mixed with filesystem-provided text) —
        // no emoji glyphs anywhere in the file browser.
        const FOLDER_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6a1 1 0 0 1 1-1h5l2 2h9a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6z"/></svg>';
        const FILE_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h11l5 5v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1z"/><path d="M15 4v5h5"/></svg>';

        // Row factory shared by the parent-nav entry and every file/folder
        // entry. Real <button>s so the list is keyboard-operable, and every
        // piece of filesystem-provided text goes through textContent —
        // never innerHTML — so a filename can't inject markup.
        function makeFileRow(iconSvg, name, onActivate) {
            const item = document.createElement("button");
            item.type = "button";
            item.className = "file-item";
            item.addEventListener("click", onActivate);

            const iconSpan = document.createElement("span");
            iconSpan.className = "file-icon";
            iconSpan.setAttribute("aria-hidden", "true");
            iconSpan.innerHTML = iconSvg; // static constant above, never user data

            const nameSpan = document.createElement("span");
            nameSpan.className = "file-name";
            nameSpan.textContent = name;

            item.append(iconSpan, nameSpan);
            return item;
        }

        function loadDir(path) {
            const url = "/explore?path=" + encodeURIComponent(path);
            fetch(url)
            .then(res => res.json())
            .then(data => {
                const list = document.getElementById("fileList");
                list.innerHTML = "";

                // Parent folder navigation
                if (data.parent_path && data.parent_path !== data.current_path) {
                    list.appendChild(makeFileRow(FOLDER_ICON, ".. (Parent Folder)", () => loadDir(data.parent_path)));
                }

                // Entries
                data.entries.forEach(entry => {
                    if (entry.is_dir) {
                        list.appendChild(makeFileRow(FOLDER_ICON, entry.name, () => loadDir(entry.path)));
                    } else {
                        list.appendChild(makeFileRow(FILE_ICON, entry.name, () => selectFile(entry.path)));
                    }
                });

                // Breadcrumbs: one clickable segment per path component,
                // Finder-path-bar style — jumps straight to that ancestor
                // folder instead of only ever showing the current path.
                const crumbs = document.getElementById("breadcrumbs");
                crumbs.innerHTML = "";
                const parts = data.current_path.split("/").filter(p => p.length > 0);
                let acc = "";
                parts.forEach((part, i) => {
                    acc += "/" + part;
                    const target = acc;
                    const seg = document.createElement("span");
                    seg.className = "breadcrumb-segment";
                    seg.textContent = part;
                    seg.setAttribute("role", "button");
                    seg.setAttribute("tabindex", "0");
                    seg.addEventListener("click", () => loadDir(target));
                    seg.addEventListener("keydown", (e) => {
                        if (e.key === "Enter" || e.key === " ") {
                            e.preventDefault();
                            loadDir(target);
                        }
                    });
                    crumbs.appendChild(seg);
                    if (i < parts.length - 1) {
                        const sep = document.createElement("span");
                        sep.className = "breadcrumb-sep";
                        sep.textContent = "/";
                        crumbs.appendChild(sep);
                    }
                });

                // Folder selection config
                const selectFolderBtn = document.getElementById("selectCurrentFolderBtn");
                selectFolderBtn.onclick = () => {
                    document.getElementById(activeInputId).value = data.current_path;
                    closeExplorer();
                };
            });
        }

        function selectFile(filePath) {
            document.getElementById(activeInputId).value = filePath;
            closeExplorer();
        }

        function startUpscale(event) {
            event.preventDefault();

            // Disable immediately — not just hidden — so a screen reader
            // can't find a live "start" control mid-submit, and the user
            // can't double-fire the request during the fetch round trip.
            const submitBtn = document.getElementById("submitBtn");
            submitBtn.disabled = true;

            // Reset UI states immediately for a responsive experience
            document.getElementById("logTerminal").innerText = "Launching AI Upscaling...";
            logFollow = true;
            document.getElementById("jumpToLatestBtn").hidden = true;
            document.getElementById("progressText").innerText = "0%";
            document.getElementById("progressBar").style.width = "0%";
            document.getElementById("progressSegment").innerText = "Initializing...";
            const progressTrack = document.getElementById("progressBarTrack");
            progressTrack.setAttribute("aria-valuenow", "0");
            progressTrack.setAttribute("aria-valuetext", "Initializing");
            const badge = document.getElementById("statusBadge");
            badge.innerText = STATUS_LABELS.running;
            badge.className = "status-badge status-running";
            document.getElementById("outputSuccess").innerText = "";
            document.getElementById("statusMessage").innerText = "";
            document.getElementById("resultPanel").style.display = "none";

            lastAnnouncedSegment = "";
            lastAnnouncedMilestone = -1;
            announce("Upscaling started.");

            upscaleStartTime = Date.now();
            document.getElementById("timeEstimate").innerText = "Elapsed: 00:00 | ETA: --:--";

            const params = {
                input_file: document.getElementById("input_file").value,
                output_file: document.getElementById("output_file").value,
                preset: document.getElementById("preset").value,
                model: document.getElementById("model").value,
                denoise: document.getElementById("denoise").checked,
                interpolate: document.getElementById("interpolate").checked,
                recursive: document.getElementById("recursive").checked
            };

            fetch("/upscale", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(params)
            })
            .then(res => res.json())
            .then(data => {
                if (data.started) {
                    document.getElementById("submitBtn").style.display = "none";
                    const cancelBtn = document.getElementById("cancelBtn");
                    cancelBtn.disabled = false;
                    cancelBtn.style.display = "block";
                    // Give the status panel the full window while running,
                    // and take the rest of the form out of the tab order —
                    // display:none already hides it from the a11y tree, but
                    // inert makes that explicit and survives layout bugs.
                    document.querySelector(".main-layout").classList.add("processing");
                    setFormDisabled(true);
                    jobRunning = true;

                    if (pollInterval) clearInterval(pollInterval);
                    pollInterval = setInterval(pollStatus, 500);
                } else {
                    // Didn't actually start (e.g. validation failure) — give
                    // the button back.
                    submitBtn.disabled = false;
                }
            });
        }

        const STAGE_LABELS = {
            extract: "extracting",
            upscale: "upscaling",
            encode: "encoding"
        };

        // One bar per in-flight chunk. Rows are keyed by chunk name so an
        // existing bar animates instead of being torn down every poll.
        function renderWorkers(workers) {
            const list = document.getElementById("workerList");
            const seen = new Set();

            workers.forEach((w, idx) => {
                const id = "worker-" + w.seg.replace(/[^a-zA-Z0-9]/g, "_");
                seen.add(id);
                let row = document.getElementById(id);
                if (!row) {
                    row = document.createElement("div");
                    row.id = id;
                    row.className = "worker-row";
                    row.innerHTML =
                        '<span class="worker-label"></span>' +
                        '<span class="worker-bar-container" role="progressbar" aria-valuemin="0" aria-valuemax="100"><span class="worker-bar-fill"></span></span>' +
                        '<span class="worker-pct"></span>';
                    list.appendChild(row);
                }
                const chunk = w.seg.replace(/\\.mkv$/, "");
                const stage = STAGE_LABELS[w.stage] || w.stage;
                const pct = Math.round(w.pct);
                const label = chunk + (stage ? " — " + stage : "");
                row.querySelector(".worker-label").innerText = label;
                row.querySelector(".worker-bar-fill").style.width = w.pct + "%";
                row.querySelector(".worker-pct").innerText = pct + "%";
                const track = row.querySelector(".worker-bar-container");
                track.setAttribute("aria-valuenow", String(pct));
                track.setAttribute("aria-valuetext",
                    `segment ${idx + 1} of ${workers.length}, ${stage || "processing"}, ${pct}%`);
            });

            Array.from(list.children).forEach(row => {
                if (!seen.has(row.id)) row.remove();
            });
        }

        function pollStatus() {
            fetch("/status")
            .then(res => res.json())
            .then(state => {
                const pct = Math.round(state.progress);
                document.getElementById("progressText").innerText = pct + "%";
                document.getElementById("progressBar").style.width = state.progress + "%";
                document.getElementById("progressSegment").innerText = state.current_segment;

                const progressTrack = document.getElementById("progressBarTrack");
                progressTrack.setAttribute("aria-valuenow", String(pct));
                progressTrack.setAttribute("aria-valuetext",
                    state.current_segment ? `${state.current_segment}, ${pct}%` : `${pct}%`);

                const badge = document.getElementById("statusBadge");
                badge.innerText = STATUS_LABELS[state.status] || state.status;
                badge.className = "status-badge status-" + state.status;

                // Announce job/stage progress without spamming: only when
                // the segment text changes (new file or stage) or a new
                // ~10% milestone is crossed — never on every 500ms tick.
                const milestone = Math.floor(pct / 10) * 10;
                if (state.status === "running") {
                    if (state.current_segment && state.current_segment !== lastAnnouncedSegment) {
                        lastAnnouncedSegment = state.current_segment;
                        lastAnnouncedMilestone = milestone;
                        announce(`${state.current_segment} — ${pct}%`);
                    } else if (milestone > 0 && milestone !== lastAnnouncedMilestone) {
                        lastAnnouncedMilestone = milestone;
                        announce(`${state.current_segment} — ${pct}%`);
                    }
                }

                const term = document.getElementById("logTerminal");
                term.innerText = state.logs.join("\\n");
                if (logFollow) {
                    scrollLogToBottom();
                } else {
                    document.getElementById("jumpToLatestBtn").hidden = false;
                }

                renderWorkers(state.workers || []);

                if (upscaleStartTime) {
                    const elapsedSec = Math.floor((Date.now() - upscaleStartTime) / 1000);
                    if (state.status === "completed") {
                        document.getElementById("timeEstimate").innerText = `Finished in: ${formatTime(elapsedSec)}`;
                    } else if (state.status === "cancelled" || state.status === "failed") {
                        document.getElementById("timeEstimate").innerText = `Stopped after: ${formatTime(elapsedSec)}`;
                    } else if (state.progress > 0) {
                        const totalSec = elapsedSec / (state.progress / 100.0);
                        const remainingSec = Math.max(0, Math.floor(totalSec - elapsedSec));
                        document.getElementById("timeEstimate").innerText = `Elapsed: ${formatTime(elapsedSec)} | ETA: ${formatTime(remainingSec)}`;
                    } else {
                        document.getElementById("timeEstimate").innerText = `Elapsed: ${formatTime(elapsedSec)} | ETA: --:--`;
                    }
                }

                // "cancelling" keeps polling — the run is still winding
                // down and hasn't settled into "cancelled" yet.
                if (state.status === "completed" || state.status === "failed" || state.status === "cancelled") {
                    clearInterval(pollInterval);
                    pollInterval = null;
                    jobRunning = false;
                    const submitBtn = document.getElementById("submitBtn");
                    submitBtn.disabled = false;
                    submitBtn.style.display = "block";
                    document.getElementById("cancelBtn").style.display = "none";
                    // Bring the form back so another run can be configured.
                    document.querySelector(".main-layout").classList.remove("processing");
                    setFormDisabled(false);
                    renderWorkers([]);

                    const statusMessage = document.getElementById("statusMessage");
                    const resultPanel = document.getElementById("resultPanel");

                    if (state.status === "completed") {
                        announce(state.output_file
                            ? `Upscaling complete. Output saved to ${state.output_file.split("/").pop()}.`
                            : "Upscaling complete.");
                        statusMessage.innerText = "";
                        if (state.output_file) {
                            document.getElementById("outputSuccess").innerText = "Output: " + state.output_file.split("/").pop();
                            document.getElementById("resultPath").innerText = "Saved to " + state.output_file;
                            resultPanel.style.display = "flex";
                            document.getElementById("revealBtn").onclick = () => {
                                fetch("/reveal", { method: "POST" }).catch(() => {});
                            };
                        }
                    } else if (state.status === "cancelled") {
                        announce("Upscaling cancelled.");
                        resultPanel.style.display = "none";
                        statusMessage.innerText = "Stopped before finishing. No file was produced. Press Start when you're ready to try again.";
                    } else {
                        announce("Upscaling failed. Check the log for details.");
                        resultPanel.style.display = "none";
                        statusMessage.innerText = (state.error_hint || "Something went wrong during processing.") + " The full log is below.";
                    }
                }
            });
        }

        function cancelUpscale() {
            document.getElementById("cancelBtn").disabled = true;
            fetch("/cancel")
            .then(() => {
                pollStatus();
            });
        }

        // A background window burning a fetch + full DOM update every
        // 500ms while minimized/hidden is pure waste for a job that keeps
        // running server-side regardless. Pause while hidden, catch up
        // immediately on return instead of waiting up to 500ms.
        document.addEventListener("visibilitychange", () => {
            if (document.hidden) {
                if (pollInterval) {
                    clearInterval(pollInterval);
                    pollInterval = null;
                }
            } else if (jobRunning && !pollInterval) {
                pollStatus();
                pollInterval = setInterval(pollStatus, 500);
            }
        });

        const modelSelect = document.getElementById("model");
        const modelHint = document.getElementById("modelHint");
        function updateModelHint() {
            const opt = modelSelect.options[modelSelect.selectedIndex];
            modelHint.innerText = opt.dataset.hint || "";
        }
        modelSelect.addEventListener("change", updateModelHint);
        updateModelHint();

        document.getElementById("jumpToLatestBtn").addEventListener("click", scrollLogToBottom);

        // The only thing that stops the log following: the user scrolling away
        // from the bottom themselves. Programmatic pins land exactly at the
        // bottom, so they re-arm it rather than clearing it.
        document.getElementById("logTerminal").addEventListener("scroll", function () {
            logFollow = isLogAtBottom(this);
            if (logFollow) {
                document.getElementById("jumpToLatestBtn").hidden = true;
            }
        });

        // Native Escape closes the <dialog> without running our JS, so
        // focus-restoration (closeExplorer) never fired for that path.
        // The "close" event fires for every dismissal (button, Escape,
        // backdrop-triggered .close()) so wiring it here covers all of
        // them uniformly; closeExplorer() is a no-op re-entry guard when
        // it was the one that called .close() in the first place.
        const explorerDialogEl = document.getElementById("explorerModal");
        explorerDialogEl.addEventListener("close", closeExplorer);
        explorerDialogEl.addEventListener("click", (e) => {
            if (e.target === explorerDialogEl) closeExplorer();
        });
    </script>
</body>
</html>
"""

def main():
    # Overridable so a second instance can be run alongside the installed app.
    port = int(os.environ.get("RAVIVE_GUI_PORT", "8080"))
    server = HTTPServer(("127.0.0.1", port), GUIHandler)
    print("================================================================================")
    print("   Ravive GUI Server Started Successfully")
    print(f"   Open your browser and navigate to: http://127.0.0.1:{port}")
    print("================================================================================")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    print("\nStopping server...")

if __name__ == "__main__":
    main()
