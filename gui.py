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
                                pct = float(val_str)
                                task_state["progress"] = max(0.0, min(100.0, pct))
                            except ValueError:
                                pass
                        
                        if "Segment" in line:
                            task_state["current_segment"] = line
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
            
            upscayl_bin = "/Applications/Upscayl.app/Contents/Resources/bin/upscayl-bin"
            if os.path.exists(upscayl_bin):
                cmd_args.extend(["--realesrgan-bin", upscayl_bin])
                
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
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Apple Silicon Video Upscaler</title>
    <style>
        :root {
            /* Perceptually uniform OKLCH colors (restrained system-cohesive blue/indigo palette) */
            --bg-color: oklch(0.12 0.008 250);
            --card-bg: oklch(0.15 0.005 250);
            --accent-color: oklch(0.6 0.18 250);
            --accent-hover: oklch(0.55 0.18 250);
            --accent-active: oklch(0.5 0.18 250);
            --text-color: oklch(0.95 0.005 250);
            --text-muted: oklch(0.65 0.01 250);
            --border-color: oklch(0.22 0.01 250);
            
            --success-color: oklch(0.68 0.16 140);
            --success-bg: oklch(0.68 0.16 140 / 0.12);
            --error-color: oklch(0.6 0.18 20);
            --error-bg: oklch(0.6 0.18 20 / 0.12);
            --warning-color: oklch(0.75 0.15 65);
            --warning-bg: oklch(0.75 0.15 65 / 0.12);
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-color);
            min-height: 100vh;
            margin: 0;
            padding: 0;
            background-image: radial-gradient(circle at top right, oklch(0.6 0.18 250 / 0.08), transparent 400px),
                              radial-gradient(circle at bottom left, oklch(0.68 0.16 140 / 0.05), transparent 400px);
        }

        .container {
            width: 100%;
            min-height: 100vh;
            box-sizing: border-box;
            padding: 24px;
            display: flex;
            flex-direction: column;
        }

        header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding-bottom: 16px;
            margin-bottom: 20px;
            border-bottom: 1px solid var(--border-color);
        }

        .header-brand {
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .header-logo {
            width: 42px;
            height: 42px;
            border-radius: 10px;
            border: 1px solid var(--border-color);
            background-color: var(--card-bg);
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
        }

        h1 {
            font-size: 1.35rem;
            font-weight: 700;
            color: var(--text-color);
            letter-spacing: -0.02em;
        }

        .subtitle {
            font-size: 0.85rem;
            color: var(--text-muted);
            margin-top: 2px;
        }

        .main-layout {
            display: grid;
            grid-template-columns: 420px 1fr;
            gap: 24px;
            align-items: stretch;
            flex-grow: 1;
        }

        .form-side {
            display: flex;
            flex-direction: column;
            gap: 16px;
        }

        .form-group {
            display: flex;
            flex-direction: column;
            gap: 6px;
        }

        label {
            font-size: 0.85rem;
            font-weight: 600;
            color: oklch(0.75 0.01 250);
        }

        .input-with-btn {
            display: flex;
            gap: 8px;
        }

        input[type="text"], select, input[type="number"] {
            width: 100%;
            padding: 8px 12px;
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            color: var(--text-color);
            font-family: inherit;
            font-size: 0.9rem;
            transition: border-color 0.15s ease, box-shadow 0.15s ease;
        }

        input[type="text"]:focus, select:focus, input[type="number"]:focus {
            border-color: var(--accent-color);
            outline: none;
            box-shadow: 0 0 0 2px oklch(0.6 0.18 250 / 0.15);
        }

        .btn-browse {
            padding: 8px 12px;
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            color: var(--text-color);
            cursor: pointer;
            font-weight: 600;
            font-size: 0.85rem;
            display: flex;
            align-items: center;
            gap: 6px;
            transition: background-color 0.15s ease, border-color 0.15s ease;
        }

        .btn-browse:hover {
            background: oklch(0.2 0.008 250);
            border-color: oklch(0.3 0.01 250);
        }

        .grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
        }

        .checkbox-group {
            display: flex;
            align-items: center;
            gap: 10px;
            margin-top: 4px;
        }

        input[type="checkbox"] {
            width: 18px;
            height: 18px;
            accent-color: var(--accent-color);
            cursor: pointer;
        }

        .btn-container {
            display: flex;
            gap: 12px;
            margin-top: 16px;
        }

        button {
            flex: 1;
            padding: 12px;
            font-family: inherit;
            font-size: 0.95rem;
            font-weight: 600;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            transition: background-color 0.15s ease, opacity 0.15s ease;
        }

        .btn-primary {
            background: var(--accent-color);
            color: white;
            box-shadow: 0 2px 8px rgba(99, 102, 241, 0.2);
        }

        .btn-primary:hover {
            background: var(--accent-hover);
        }

        .btn-primary:active {
            background: var(--accent-active);
        }

        .btn-cancel {
            background: var(--error-bg);
            color: var(--error-color);
            border: 1px solid oklch(0.6 0.18 20 / 0.3);
        }

        .btn-cancel:hover {
            background: oklch(0.6 0.18 20 / 0.2);
        }

        .status-side {
            display: flex;
            flex-direction: column;
            height: 100%;
        }

        /* Progress Card */
        .progress-card {
            padding: 20px;
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            flex-grow: 1;
            display: flex;
            flex-direction: column;
            box-shadow: 0 4px 16px rgba(0, 0, 0, 0.15);
        }

        .progress-title {
            display: flex;
            justify-content: space-between;
            font-weight: 600;
            font-size: 0.9rem;
            margin-bottom: 12px;
        }

        .progress-bar-container {
            width: 100%;
            height: 8px;
            background: oklch(0.2 0.005 250);
            border-radius: 4px;
            overflow: hidden;
            margin-bottom: 16px;
        }

        .progress-bar-fill {
            width: 0%;
            height: 100%;
            background: var(--accent-color);
            transition: width 0.3s ease;
        }

        .status-meta {
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 0.85rem;
            color: var(--text-muted);
        }

        .log-terminal {
            width: 100%;
            flex-grow: 1;
            min-height: 250px;
            height: 0;
            background: oklch(0.08 0.005 250);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 14px;
            font-family: SF Mono, Monaco, Consolas, "Liberation Mono", monospace;
            font-size: 0.82rem;
            color: oklch(0.85 0.02 140);
            overflow-y: scroll;
            white-space: pre-wrap;
            margin-top: 14px;
            line-height: 1.4;
        }

        .status-badge {
            display: inline-block;
            padding: 4px 8px;
            border-radius: 6px;
            font-size: 0.75rem;
            font-weight: 700;
            letter-spacing: 0.05em;
            text-transform: uppercase;
        }

        .status-idle { background: oklch(0.2 0.005 250); color: var(--text-muted); }
        .status-running { background: oklch(0.6 0.18 250 / 0.15); color: var(--accent-color); }
        .status-completed { background: var(--success-bg); color: var(--success-color); }
        .status-failed { background: var(--error-bg); color: var(--error-color); }

        /* File Explorer Modal */
        .modal {
            display: none;
            position: fixed;
            z-index: 100;
            left: 0;
            top: 0;
            width: 100%;
            height: 100%;
            background-color: rgba(0, 0, 0, 0.75);
            backdrop-filter: blur(4px);
            align-items: center;
            justify-content: center;
        }

        .modal-content {
            background: oklch(0.14 0.005 250);
            border: 1px solid var(--border-color);
            border-radius: 16px;
            width: 90%;
            max-width: 600px;
            max-height: 80%;
            display: flex;
            flex-direction: column;
            padding: 20px;
            box-shadow: 0 20px 40px rgba(0, 0, 0, 0.5);
        }

        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
        }

        .modal-header h2 {
            font-size: 1.1rem;
            font-weight: 700;
        }

        .close-btn {
            background: none;
            border: none;
            color: var(--text-muted);
            font-size: 1.5rem;
            cursor: pointer;
            line-height: 1;
        }

        .close-btn:hover {
            color: var(--text-color);
        }

        .breadcrumbs {
            font-size: 0.85rem;
            color: var(--accent-color);
            background: oklch(0.08 0.005 250);
            padding: 6px 10px;
            border-radius: 6px;
            margin-bottom: 12px;
            word-break: break-all;
            cursor: pointer;
        }

        .file-list {
            flex: 1;
            overflow-y: auto;
            min-height: 250px;
            max-height: 350px;
            border: 1px solid var(--border-color);
            border-radius: 8px;
            background: oklch(0.08 0.005 250);
        }

        .file-item {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 8px 12px;
            border-bottom: 1px solid oklch(0.2 0.005 250 / 0.5);
            cursor: pointer;
            transition: background-color 0.15s ease;
            font-size: 0.85rem;
        }

        .file-item:hover {
            background: oklch(0.6 0.18 250 / 0.08);
        }

        .file-icon {
            font-size: 1.1rem;
        }

        .file-name {
            color: var(--text-color);
            word-break: break-all;
        }

        .modal-footer {
            margin-top: 12px;
            display: flex;
            justify-content: flex-end;
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="header-brand">
                <img src="/logo.jpg" alt="Logo" class="header-logo">
                <div>
                    <h1>Apple Silicon Video Upscaler</h1>
                    <div class="subtitle">Interface de traitement local haute performance</div>
                </div>
            </div>
        </header>

        <div class="main-layout">
            <div class="form-side">
                <form id="upscaleForm" onsubmit="startUpscale(event)">
                    <div class="form-group">
                        <label for="input_file">Vidéo ou Dossier source</label>
                        <div class="input-with-btn">
                            <input type="text" id="input_file" required placeholder="Sélectionnez un fichier ou un dossier...">
                            <button type="button" class="btn-browse" onclick="openExplorer('input_file', false)">🎥 Fichier</button>
                            <button type="button" class="btn-browse" onclick="openExplorer('input_file', true)">📁 Dossier</button>
                        </div>
                    </div>

                    <div class="form-group">
                        <label for="output_file">Dossier de sortie (Optionnel)</label>
                        <div class="input-with-btn">
                            <input type="text" id="output_file" placeholder="Ex: /Users/nom_utilisateur/Downloads/">
                            <button type="button" class="btn-browse" onclick="openExplorer('output_file', true)">📁 Dossier</button>
                        </div>
                    </div>

                    <div class="grid">
                        <div class="form-group">
                            <label for="preset">Preset de Résolution</label>
                            <select id="preset">
                                <option value="480p">480p (Target: 480px height)</option>
                                <option value="720p">720p (Target: 720px height)</option>
                                <option value="1080p" selected>1080p (Target: 1080px height)</option>
                                <option value="4k">4K (Target: 2160px height)</option>
                            </select>
                        </div>

                        <div class="form-group">
                            <label for="model">Modèle d'Upscaling IA</label>
                            <select id="model">
                                <option value="auto" selected>Détection Automatique (Recommandé)</option>
                                <option value="realesrgan-x4plus">Real-ESRGAN x4plus (Films Réels)</option>
                                <option value="realesr-animevideov3">Real-ESR Anime Video v3 (Animation 2D/3D)</option>
                                <option value="digital-art-4x">Digital Art 4x (CGI / Upscayl)</option>
                                <option value="ultrasharp-4x">UltraSharp 4x (Photos/Textures)</option>
                            </select>
                        </div>
                    </div>

                    <div class="grid">
                        <div class="form-group">
                            <label for="workers">Nombre de Chunks en Parallèle</label>
                            <input type="number" id="workers" min="1" max="8" value="2">
                        </div>

                        <div class="form-group">
                            <label style="margin-bottom: 6px;">Filtres & Améliorations</label>
                            <div class="checkbox-group">
                                <input type="checkbox" id="denoise">
                                <label for="denoise" style="font-weight: normal; color: var(--text-color); cursor: pointer;">Réduire le scintillement (Denoise temporel)</label>
                            </div>
                            <div class="checkbox-group">
                                <input type="checkbox" id="interpolate">
                                <label for="interpolate" style="font-weight: normal; color: var(--text-color); cursor: pointer;">Fluidifier à 60 FPS (Interpolation)</label>
                            </div>
                            <div class="checkbox-group">
                                <input type="checkbox" id="recursive">
                                <label for="recursive" style="font-weight: normal; color: var(--text-color); cursor: pointer;">Traiter les sous-dossiers récursivement</label>
                            </div>
                        </div>
                    </div>
                </form>
            </div>
            
            <div class="status-side">
                <div class="progress-card" id="progressCard">
                    <div class="progress-title">
                        <span id="progressSegment">Prêt à démarrer</span>
                        <span id="progressText">0%</span>
                    </div>
                    <div class="progress-bar-container">
                        <div class="progress-bar-fill" id="progressBar"></div>
                    </div>
                    <div class="status-meta">
                        <span>Statut : <span class="status-badge status-idle" id="statusBadge">IDLE</span></span>
                        <span id="outputSuccess" style="color: var(--success-color); font-weight: 600;"></span>
                    </div>
                    <div class="log-terminal" id="logTerminal">En attente de lancement d'upscaling...</div>
                </div>

                <div class="btn-container">
                    <button type="submit" form="upscaleForm" class="btn-primary" id="submitBtn">Lancer l'Upscaling par IA</button>
                    <button type="button" class="btn-cancel" id="cancelBtn" onclick="cancelUpscale()" style="display:none;">Annuler</button>
                </div>
            </div>
        </div>
    </div>

    <!-- File Explorer Modal -->
    <div id="explorerModal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h2>Explorateur de Fichiers</h2>
                <button type="button" class="close-btn" onclick="closeExplorer()">&times;</button>
            </div>
            <div class="breadcrumbs" id="breadcrumbs"></div>
            <div class="file-list" id="fileList"></div>
            <div class="modal-footer">
                <button type="button" class="btn-primary" id="selectCurrentFolderBtn" style="flex:none; width:auto; padding: 8px 14px; font-size: 0.85rem;">Sélectionner ce dossier</button>
            </div>
        </div>
    </div>

    <script>
        let pollInterval = null;
        let activeInputId = "";

        window.addEventListener("DOMContentLoaded", () => {
            fetch("/status")
            .then(res => res.json())
            .then(state => {
                if (state.status === "running") {
                    document.getElementById("submitBtn").style.display = "none";
                    document.getElementById("cancelBtn").style.display = "block";
                    pollStatus();
                    pollInterval = setInterval(pollStatus, 500);
                } else if (state.status === "completed" || state.status === "failed") {
                    pollStatus();
                }
            });
        });

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
                    item.innerHTML = `<span class="file-icon">📁</span><span class="file-name">.. (Dossier Parent)</span>`;
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
                    document.getElementById("outputSuccess").innerText = "";
                    
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
                document.getElementById("progressSegment").innerText = state.current_segment || "Traitement...";
                
                const badge = document.getElementById("statusBadge");
                badge.innerText = state.status.toUpperCase();
                badge.className = "status-badge status-" + state.status;
                
                const term = document.getElementById("logTerminal");
                term.innerText = state.logs.join("\\n");
                term.scrollTop = term.scrollHeight;
                
                if (state.status === "completed") {
                    clearInterval(pollInterval);
                    document.getElementById("submitBtn").style.display = "block";
                    document.getElementById("cancelBtn").style.display = "none";
                    if (state.output_file) {
                        document.getElementById("outputSuccess").innerText = "Sortie : " + state.output_file.split("/").pop();
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
    print("   Apple Silicon Video Upscaler GUI Server Started Successfully")
    print("   Open your browser and navigate to: http://127.0.0.1:8080")
    print("================================================================================")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    print("\nStopping server...")

if __name__ == "__main__":
    main()
