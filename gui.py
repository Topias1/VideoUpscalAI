import json
import os
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

# Global state to track upscaling task
task_state = {
    "status": "idle",       # idle, running, completed, failed
    "progress": 0.0,
    "current_segment": "",
    "logs": [],
    "output_file": ""
}

task_lock = threading.Lock()
active_process = None

def run_upscale_thread(cmd_args):
    global active_process
    with task_lock:
        task_state["status"] = "running"
        task_state["progress"] = 0.0
        task_state["current_segment"] = "Starting split..."
        task_state["logs"] = ["Starting upscaler CLI pipeline..."]
        task_state["output_file"] = ""
        
    import sys
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
    
    try:
        import os
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
        
        buffer = []
        while True:
            char = active_process.stdout.read(1)
            if not char:
                break
            if char in ("\r", "\n"):
                line = "".join(buffer).strip()
                buffer.clear()
                if line:
                    with task_lock:
                        task_state["logs"].append(line)
                        if len(task_state["logs"]) > 100:
                            task_state["logs"].pop(0)
                        
                        # Parse progress bars
                        if "%" in line:
                            try:
                                val_str = line.split("%")[0].strip().split()[-1]
                                if "[" in val_str:
                                    val_str = val_str.split("]")[-1].strip()
                                val_str = val_str.replace(",", ".")
                                pct = float(val_str)
                                task_state["progress"] = max(0.0, min(100.0, pct))
                            except ValueError:
                                pass
                        
                        # Parse file progress line, e.g.: "File 1/3: video.mp4"
                        if "File " in line and "/" in line and ":" in line:
                            try:
                                parts = line.split(":", 1)
                                file_part = parts[0].replace("File", "").strip() # "1/3"
                                filename_part = parts[1].strip() # "video.mp4"
                                task_state["current_file_info"] = {
                                    "progress": file_part,
                                    "name": filename_part
                                }
                                task_state["current_segment"] = f"Processing: {filename_part} ({file_part})"
                            except Exception:
                                pass
                                
                        # Parse segment progress line, e.g.: "Segment 1/2: seg_0000.mkv starting..."
                        elif "Segment " in line and "/" in line and ":" in line:
                            try:
                                parts = line.split(":", 1)
                                seg_part = parts[0].replace("Segment", "").strip() # "1/2"
                                # Check if total segments is 1, in which case we don't need to append "Part 1/1"
                                if seg_part == "1/1":
                                    if "current_file_info" in task_state:
                                        info = task_state["current_file_info"]
                                        task_state["current_segment"] = f"Processing: {info['name']} ({info['progress']})"
                                else:
                                    if "current_file_info" in task_state:
                                        info = task_state["current_file_info"]
                                        task_state["current_segment"] = f"Processing: {info['name']} ({info['progress']}) - Part {seg_part}"
                                    else:
                                        task_state["current_segment"] = f"Processing - Part {seg_part}"
                            except Exception:
                                pass
                        elif "Successfully upscaled" in line:
                            parts = line.split("->")
                            if len(parts) > 1:
                                task_state["output_file"] = parts[1].strip()
            else:
                buffer.append(char)
                
        active_process.wait()
        
        with task_lock:
            if active_process.returncode == 0:
                task_state["status"] = "completed"
                task_state["progress"] = 100.0
                task_state["current_segment"] = "Finished successfully!"
                task_state["logs"].append("Upscaling completed successfully.")
            else:
                task_state["status"] = "failed"
                task_state["logs"].append(f"Process exited with non-zero code: {active_process.returncode}")
    except Exception as e:
        with task_lock:
            task_state["status"] = "failed"
            task_state["logs"].append(f"Unexpected error: {e}")
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
            global active_process
            if active_process:
                try:
                    active_process.terminate()
                except Exception:
                    pass
            with task_lock:
                task_state["status"] = "failed"
                task_state["logs"].append("Process cancelled by user.")
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
        if self.path == "/upscale":
            with task_lock:
                if task_state["status"] == "running":
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
            workers = params.get("workers", 1)
            denoise = params.get("denoise", False)
            interpolate = params.get("interpolate", False)
            recursive = params.get("recursive", False)
            
            cmd_args = [input_file]
            if output_file:
                cmd_args.extend(["-o", output_file])
                
            cmd_args.extend(["--preset", preset])
            cmd_args.extend(["--model", model])
            cmd_args.extend(["--workers", str(workers)])
            cmd_args.append("--force")
            
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
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #0b0f19;
            --card-bg: rgba(22, 28, 45, 0.6);
            --accent: linear-gradient(135deg, #6366f1, #06b6d4);
            --text-color: #f3f4f6;
            --text-muted: #9ca3af;
            --border-color: rgba(255, 255, 255, 0.08);
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        body {
            font-family: 'Outfit', sans-serif;
            background-color: var(--bg-color);
            color: var(--text-color);
            width: 100vw;
            height: 100vh;
            margin: 0;
            padding: 0;
            display: flex;
            justify-content: stretch;
            align-items: stretch;
            background-image: radial-gradient(circle at top right, rgba(99, 102, 241, 0.15), transparent 400px),
                              radial-gradient(circle at bottom left, rgba(6, 182, 212, 0.15), transparent 400px);
            overflow: hidden;
        }

        .container {
            width: 100%;
            height: 100%;
            max-width: none;
            background: var(--card-bg);
            border: none;
            border-radius: 0;
            backdrop-filter: blur(16px);
            padding: 20px 24px;
            display: flex;
            flex-direction: column;
            box-shadow: none;
        }

        .main-layout {
            display: grid;
            grid-template-columns: 1fr 1.2fr;
            gap: 24px;
            align-items: stretch;
            flex: 1;
            min-height: 0;
        }

        .form-side {
            display: flex;
            flex-direction: column;
            height: 100%;
            overflow-y: auto;
            padding-right: 8px;
            min-height: 0;
        }

        .form-side::-webkit-scrollbar {
            width: 6px;
        }

        .form-side::-webkit-scrollbar-thumb {
            background: rgba(255, 255, 255, 0.1);
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
            font-size: 1.8rem;
            font-weight: 800;
            background: var(--accent);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin: 0;
            text-align: center;
        }

        .subtitle {
            font-size: 0.95rem;
            color: var(--text-muted);
            text-align: center;
            margin-bottom: 16px;
        }

        .form-group {
            margin-bottom: 14px;
        }

        label {
            display: block;
            font-size: 0.95rem;
            font-weight: 600;
            margin-bottom: 8px;
            color: var(--text-color);
        }

        .input-with-btn {
            display: flex;
            gap: 12px;
        }

        input[type="text"], select, input[type="number"] {
            width: 100%;
            padding: 10px 12px;
            background: rgba(17, 24, 39, 0.8);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            color: var(--text-color);
            font-family: inherit;
            font-size: 0.95rem;
            transition: all 0.3s ease;
        }

        input[type="text"]:focus, select:focus, input[type="number"]:focus {
            border-color: #6366f1;
            outline: none;
            box-shadow: 0 0 10px rgba(99, 102, 241, 0.2);
        }

        .btn-browse {
            padding: 10px 14px;
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            color: var(--text-color);
            cursor: pointer;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 8px;
            transition: all 0.2s ease;
        }

        .btn-browse:hover {
            background: rgba(99, 102, 241, 0.15);
            border-color: #6366f1;
        }

        .grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
        }

        .checkbox-group {
            display: flex;
            align-items: center;
            gap: 12px;
            margin-top: 10px;
        }

        input[type="checkbox"] {
            width: 20px;
            height: 20px;
            accent-color: #6366f1;
            cursor: pointer;
        }

        .btn-container {
            display: flex;
            gap: 16px;
            margin-top: 20px;
        }

        button {
            flex: 1;
            padding: 16px;
            font-family: inherit;
            font-size: 1.05rem;
            font-weight: 600;
            border: none;
            border-radius: 14px;
            cursor: pointer;
            transition: all 0.3s ease;
        }

        .btn-primary {
            background: var(--accent);
            color: white;
            box-shadow: 0 4px 14px rgba(99, 102, 241, 0.4);
        }

        .btn-primary:hover {
            opacity: 0.95;
            transform: translateY(-2px);
        }

        .btn-cancel {
            background: rgba(239, 68, 68, 0.2);
            color: #ef4444;
            border: 1px solid rgba(239, 68, 68, 0.4);
        }

        .btn-cancel:hover {
            background: rgba(239, 68, 68, 0.3);
        }

        /* Progress Card */
        .progress-card {
            padding: 16px;
            background: rgba(17, 24, 39, 0.6);
            border: 1px solid var(--border-color);
            border-radius: 16px;
            flex: 1;
            display: flex;
            flex-direction: column;
            min-height: 0;
            overflow: hidden;
        }

        .progress-title {
            display: flex;
            justify-content: space-between;
            font-weight: 600;
            margin-bottom: 12px;
        }

        .progress-bar-container {
            width: 100%;
            height: 12px;
            background: rgba(255, 255, 255, 0.05);
            border-radius: 6px;
            overflow: hidden;
            margin-bottom: 16px;
        }

        .progress-bar-fill {
            width: 0%;
            height: 100%;
            background: var(--accent);
            transition: width 0.4s ease;
        }

        .log-terminal {
            width: 100%;
            flex: 1;
            background: #05070c;
            border: 1px solid var(--border-color);
            border-radius: 10px;
            padding: 12px;
            font-family: monospace;
            font-size: 0.85rem;
            color: #34d399;
            overflow-y: auto;
            white-space: pre-wrap;
            margin-top: 12px;
            min-height: 0;
        }

        .status-badge {
            display: inline-block;
            padding: 6px 12px;
            border-radius: 20px;
            font-size: 0.85rem;
            font-weight: 600;
        }

        .status-idle { background: rgba(156, 163, 175, 0.2); color: #9ca3af; }
        .status-running { background: rgba(59, 130, 246, 0.2); color: #60a5fa; }
        .status-completed { background: rgba(16, 185, 129, 0.2); color: #34d399; }
        .status-failed { background: rgba(239, 68, 68, 0.2); color: #f87171; }

        /* File Explorer Modal */
        .modal {
            display: none;
            position: fixed;
            z-index: 100;
            left: 0;
            top: 0;
            width: 100%;
            height: 100%;
            background-color: rgba(0, 0, 0, 0.7);
            backdrop-filter: blur(8px);
            align-items: center;
            justify-content: center;
        }

        .modal-content {
            background: rgba(17, 24, 39, 0.95);
            border: 1px solid var(--border-color);
            border-radius: 20px;
            width: 90%;
            max-width: 600px;
            max-height: 80%;
            display: flex;
            flex-direction: column;
            padding: 24px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
        }

        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 16px;
        }

        .close-btn {
            background: none;
            border: none;
            color: var(--text-muted);
            font-size: 2rem;
            cursor: pointer;
            line-height: 1;
        }

        .close-btn:hover {
            color: var(--text-color);
        }

        .breadcrumbs {
            font-size: 0.9rem;
            color: #6366f1;
            background: rgba(255, 255, 255, 0.03);
            padding: 8px 12px;
            border-radius: 8px;
            margin-bottom: 16px;
            word-break: break-all;
            cursor: pointer;
        }

        .file-list {
            flex: 1;
            overflow-y: auto;
            min-height: 300px;
            max-height: 400px;
            border: 1px solid var(--border-color);
            border-radius: 10px;
            background: rgba(0, 0, 0, 0.2);
        }

        .file-item {
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 10px 14px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.03);
            cursor: pointer;
            transition: background 0.2s ease;
        }

        .file-item:hover {
            background: rgba(99, 102, 241, 0.1);
        }

        .file-icon {
            font-size: 1.2rem;
        }

        .file-name {
            font-size: 0.95rem;
            color: var(--text-color);
            word-break: break-all;
        }

        .modal-footer {
            margin-top: 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
    </style>
</head>
<body>
    <div class="container">
        <div style="text-align: center; margin-bottom: 12px; display: flex; align-items: center; justify-content: center; gap: 16px;">
            <img src="/logo.jpg" alt="Logo" style="width: 50px; height: 50px; border-radius: 12px; box-shadow: 0 5px 15px rgba(99, 102, 241, 0.4); border: 2px solid rgba(255, 255, 255, 0.1); display: inline-block;">
            <h1>Ravive</h1>
        </div>
        <div class="subtitle" style="margin-bottom: 20px;">Local GUI for AI Processing</div>

        <div class="main-layout">
            <div class="form-side">
                <form id="upscaleForm" onsubmit="startUpscale(event)">
                    <div class="form-group">
                        <label for="input_file">Source Video or Folder</label>
                        <div class="input-with-btn">
                            <input type="text" id="input_file" required placeholder="Select a file or folder...">
                            <button type="button" class="btn-browse" onclick="openExplorer('input_file', false)">🎥 File</button>
                            <button type="button" class="btn-browse" onclick="openExplorer('input_file', true)">📁 Folder</button>
                        </div>
                    </div>

            <div class="form-group">
                <label for="output_file">Output Folder (Optional)</label>
                <div class="input-with-btn">
                    <input type="text" id="output_file" placeholder="e.g. /Users/username/Downloads/">
                    <button type="button" class="btn-browse" onclick="openExplorer('output_file', true)">📁 Folder</button>
                </div>
            </div>

            <div class="grid">
                <div class="form-group">
                    <label for="preset">Resolution Preset</label>
                    <select id="preset">
                        <option value="480p">480p (Target: 480px height)</option>
                        <option value="720p">720p (Target: 720px height)</option>
                        <option value="1080p" selected>1080p (Target: 1080px height)</option>
                        <option value="4k">4K (Target: 2160px height)</option>
                    </select>
                </div>

                <div class="form-group">
                    <label for="model">AI Upscaling Model</label>
                    <select id="model">
                        <option value="auto" selected>Auto Detection (Recommended)</option>
                        <option value="realesrgan-x4plus">Real-ESRGAN x4plus (Real Video)</option>
                        <option value="realesr-animevideov3">Real-ESR Anime Video v3 (2D/3D Animation)</option>
                        <option value="digital-art-4x">Digital Art 4x (CGI / Upscayl)</option>
                        <option value="ultrasharp-4x">UltraSharp 4x (Photos/Textures)</option>
                    </select>
                </div>
            </div>

            <div class="grid">
                <div class="form-group">
                    <label for="workers">Parallel Chunks</label>
                    <input type="number" id="workers" min="1" max="8" value="4">
                </div>

                <div class="form-group">
                    <label style="margin-bottom: 15px;">Filters & Enhancements</label>
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
                </div>
            </div>
        </form>
    </div>
            
            <div class="status-side">
                <div class="progress-card" id="progressCard">
                    <div class="progress-title">
                        <span id="progressSegment">Ready to start</span>
                        <span id="progressText">0%</span>
                    </div>
                    <div class="progress-bar-container" style="margin-bottom: 6px;">
                        <div class="progress-bar-fill" id="progressBar"></div>
                    </div>
                    <div id="timeEstimate" style="font-size: 0.85rem; color: var(--text-muted); margin-bottom: 14px; text-align: right;">Elapsed: 00:00 | ETA: --:--</div>
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <span>Status: <span class="status-badge status-idle" id="statusBadge">IDLE</span></span>
                        <span id="outputSuccess" style="color: #34d399; font-weight: 600;"></span>
                    </div>
                    <div class="log-terminal" id="logTerminal">Waiting to start upscaling...</div>
                </div>

                <div class="btn-container">
                    <button type="submit" form="upscaleForm" class="btn-primary" id="submitBtn">Start AI Upscaling</button>
                    <button type="button" class="btn-cancel" id="cancelBtn" onclick="cancelUpscale()" style="display:none;">Cancel</button>
                </div>
            </div>
        </div>
    </div>

    <!-- File Explorer Modal -->
    <div id="explorerModal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h2>File Explorer</h2>
                <button type="button" class="close-btn" onclick="closeExplorer()">&times;</button>
            </div>
            <div class="breadcrumbs" id="breadcrumbs"></div>
            <div class="file-list" id="fileList"></div>
            <div class="modal-footer">
                <button type="button" class="btn-primary" id="selectCurrentFolderBtn" style="flex:none; width:auto; padding: 10px 16px;">Select Current Folder</button>
                <button type="button" class="btn-cancel" onclick="closeExplorer()" style="flex:none; width:auto; padding: 10px 16px;">Close</button>
            </div>
        </div>
    </div>

    <script>
        let pollInterval = null;
        let activeInputId = "";
        let upscaleStartTime = null;

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
                activeInputId = inputId;
                document.getElementById("explorerModal").style.display = "flex";
                loadDir("");
            }
        }

        function closeExplorer() {
            document.getElementById("explorerModal").style.display = "none";
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
                    const item = document.createElement("div");
                    item.className = "file-item";
                    item.onclick = () => loadDir(data.parent_path);
                    item.innerHTML = `<span class="file-icon">📁</span><span class="file-name">.. (Parent Folder)</span>`;
                    list.appendChild(item);
                }
                
                // Entries
                data.entries.forEach(entry => {
                    const item = document.createElement("div");
                    item.className = "file-item";
                    if (entry.is_dir) {
                        item.onclick = () => loadDir(entry.path);
                        item.innerHTML = `<span class="file-icon">📁</span><span class="file-name">${entry.name}</span>`;
                    } else {
                        item.onclick = () => selectFile(entry.path);
                        item.innerHTML = `<span class="file-icon">🎥</span><span class="file-name">${entry.name}</span>`;
                    }
                    list.appendChild(item);
                });
                
                // Breadcrumbs
                const crumbs = document.getElementById("breadcrumbs");
                crumbs.innerText = data.current_path;
                
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
            
            // Reset UI states immediately for a responsive experience
            document.getElementById("logTerminal").innerText = "Launching AI Upscaling...";
            document.getElementById("progressText").innerText = "0%";
            document.getElementById("progressBar").style.width = "0%";
            document.getElementById("progressSegment").innerText = "Initializing...";
            const badge = document.getElementById("statusBadge");
            badge.innerText = "RUNNING";
            badge.className = "status-badge status-running";
            document.getElementById("progressCard").style.display = "block";
            document.getElementById("outputSuccess").innerText = "";
            
            upscaleStartTime = Date.now();
            document.getElementById("timeEstimate").innerText = "Elapsed: 00:00 | ETA: --:--";
            
            const params = {
                input_file: document.getElementById("input_file").value,
                output_file: document.getElementById("output_file").value,
                preset: document.getElementById("preset").value,
                model: document.getElementById("model").value,
                workers: parseInt(document.getElementById("workers").value),
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
                    document.getElementById("cancelBtn").style.display = "block";
                    
                    if (pollInterval) clearInterval(pollInterval);
                    pollInterval = setInterval(pollStatus, 500);
                }
            });
        }

        function pollStatus() {
            fetch("/status")
            .then(res => res.json())
            .then(state => {
                document.getElementById("progressText").innerText = Math.round(state.progress) + "%";
                document.getElementById("progressBar").style.width = state.progress + "%";
                document.getElementById("progressSegment").innerText = state.current_segment;
                
                const badge = document.getElementById("statusBadge");
                badge.innerText = state.status.toUpperCase();
                badge.className = "status-badge status-" + state.status;
                
                const term = document.getElementById("logTerminal");
                term.innerText = state.logs.join("\\n");
                term.scrollTop = term.scrollHeight;
                
                if (upscaleStartTime) {
                    const elapsedSec = Math.floor((Date.now() - upscaleStartTime) / 1000);
                    if (state.progress > 0) {
                        const totalSec = elapsedSec / (state.progress / 100.0);
                        const remainingSec = Math.max(0, Math.floor(totalSec - elapsedSec));
                        document.getElementById("timeEstimate").innerText = `Elapsed: ${formatTime(elapsedSec)} | ETA: ${formatTime(remainingSec)}`;
                    } else {
                        document.getElementById("timeEstimate").innerText = `Elapsed: ${formatTime(elapsedSec)} | ETA: --:--`;
                    }
                    if (state.status === "completed") {
                        document.getElementById("timeEstimate").innerText = `Finished in: ${formatTime(elapsedSec)}`;
                    }
                }
                
                if (state.status === "completed") {
                    clearInterval(pollInterval);
                    document.getElementById("submitBtn").style.display = "block";
                    document.getElementById("cancelBtn").style.display = "none";
                    if (state.output_file) {
                        document.getElementById("outputSuccess").innerText = "Output: " + state.output_file.split("/").pop();
                    }
                } else if (state.status === "failed") {
                    clearInterval(pollInterval);
                    document.getElementById("submitBtn").style.display = "block";
                    document.getElementById("cancelBtn").style.display = "none";
                }
            });
        }

        function cancelUpscale() {
            fetch("/cancel")
            .then(() => {
                clearInterval(pollInterval);
                pollStatus();
            });
        }
    </script>
</body>
</html>
"""

def main():
    server = HTTPServer(("127.0.0.1", 8080), GUIHandler)
    print("================================================================================")
    print("   Ravive GUI Server Started Successfully")
    print("   Open your browser and navigate to: http://127.0.0.1:8080")
    print("================================================================================")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    print("\nStopping server...")

if __name__ == "__main__":
    main()
