import os
import sys
import threading
import time
import webview
from gui import main as start_server, active_process

def on_closed():
    # Clean up background processes when window is closed
    global active_process
    if active_process:
        try:
            active_process.terminate()
        except Exception:
            pass
    sys.exit(0)

def main():
    # Start the local upscaler backend server in a background thread
    server_thread = threading.Thread(target=start_server)
    server_thread.daemon = True
    server_thread.start()
    
    # Give the HTTP server a moment to bind to port 8080
    time.sleep(0.8)
    
    # Create the native macOS window hosting WKWebView
    window = webview.create_window(
        title="Apple Silicon Video Upscaler",
        url="http://127.0.0.1:8080",
        width=900,
        height=750,
        min_size=(800, 650),
        resizable=True
    )
    
    # Bind closed event for clean process termination
    window.events.closed += on_closed
    
    # Start webview loop
    webview.start()

if __name__ == "__main__":
    main()
