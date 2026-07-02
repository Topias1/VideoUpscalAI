import os
import sys
import threading
import time
import webview
from gui import main as start_server, active_process

class Api:
    def __init__(self):
        self.window = None

    def select_file(self):
        # Open native macOS Finder file sheet
        if not self.window:
            return ""
        result = self.window.create_file_dialog(
            webview.OPEN_DIALOG,
            file_types=('Video files (*.mp4;*.mkv;*.mov;*.avi;*.webm)', 'All files (*.*)')
        )
        return result[0] if result else ""

    def select_folder(self):
        # Open native macOS Finder folder sheet
        if not self.window:
            return ""
        result = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        return result[0] if result else ""

def on_closed():
    # Clean up background process when the app closes
    global active_process
    if active_process:
        try:
            active_process.terminate()
        except Exception:
            pass
    sys.exit(0)

def main():
    # Start the server in a separate thread
    server_thread = threading.Thread(target=start_server)
    server_thread.daemon = True
    server_thread.start()
    
    # Wait a bit for server to start
    time.sleep(0.8)
    
    # Instantiate the exposed Javascript API
    api = Api()
    
    # Create webview window
    window = webview.create_window(
        title="Apple Silicon Video Upscaler",
        url="http://127.0.0.1:8080",
        width=900,
        height=750,
        min_size=(800, 650),
        resizable=True,
        js_api=api
    )
    api.window = window
    
    # Register closing callback
    window.events.closed += on_closed
    
    # Start the webview loop
    webview.start()

if __name__ == "__main__":
    import os
    if os.environ.get("VIDEO_UPSCALER_CLI") == "1":
        # Run as the CLI upscaler helper
        import upscale
        upscale.main()
    else:
        # Run as the native GUI app
        main()
