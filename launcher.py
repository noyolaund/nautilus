"""JDE Automation Launcher.

Single entry point that:
  1. Prompts which LLM proxy to use (proxy / proxy-jnj)
  2. Starts that proxy server in a background thread
  3. Starts the dashboard web app
  4. Opens the dashboard in the default browser

Run directly:
    python launcher.py

Or build into a standalone .exe (see build_launcher.bat).
"""

from __future__ import annotations

import os
import sys
import time
import threading
import webbrowser
from pathlib import Path

# When frozen by PyInstaller, the bundle root is sys._MEIPASS; otherwise it's
# the directory containing this file. Make project imports + .env resolvable.
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    RUN_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent
    RUN_DIR = BASE_DIR

sys.path.insert(0, str(BASE_DIR))
os.chdir(RUN_DIR)  # so logs/, config/, tests/ resolve next to the exe

from dotenv import load_dotenv
# Load .env from the run dir first (next to the exe), then the bundle
load_dotenv(RUN_DIR / ".env")
load_dotenv(BASE_DIR / ".env")

import uvicorn


# ---------------------------------------------------------------------------
# Proxy selection prompt
# ---------------------------------------------------------------------------

def prompt_proxy() -> str:
    """Ask the user which proxy to start. Returns 'globant' or 'jnj'."""
    print()
    print("=" * 60)
    print("  JDE Automation Launcher")
    print("=" * 60)
    print()
    print("  Select the LLM proxy to use:")
    print()
    print("    [1] proxy      — Globant GeAI")
    print("    [2] proxy-jnj  — JNJ Azure OpenAI (Cloud PC, needs VPN)")
    print()

    while True:
        choice = input("  Enter 1 or 2 (default 1): ").strip()
        if choice in ("", "1"):
            return "globant"
        if choice == "2":
            return "jnj"
        print("  Invalid choice — type 1 or 2.")


# ---------------------------------------------------------------------------
# Server runners
# ---------------------------------------------------------------------------

def _make_server(app, host: str, port: int) -> uvicorn.Server:
    """Build a uvicorn Server that can run off the main thread.

    uvicorn installs signal handlers in run(); those can only be installed
    on the main thread, so we no-op that for the background proxy.
    """
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    return server


def start_proxy(kind: str) -> tuple[int, threading.Thread]:
    """Start the chosen proxy in a daemon thread. Returns (port, thread)."""
    if kind == "jnj":
        from proxy.jnj_proxy import jnj_proxy_app
        port = int(os.getenv("JNJ_PROXY_PORT", "3457"))
        app = jnj_proxy_app
        label = "JNJ Azure proxy"
    else:
        from proxy.azure_proxy import proxy_app
        port = int(os.getenv("PROXY_PORT", "3456"))
        app = proxy_app
        label = "Globant GeAI proxy"

    server = _make_server(app, "127.0.0.1", port)
    thread = threading.Thread(target=server.run, daemon=True, name="proxy")
    thread.start()
    print(f"  Starting {label} on http://127.0.0.1:{port} ...")
    return port, thread


def wait_for_port(port: int, timeout: float = 20.0) -> bool:
    """Poll until something is listening on the port (or timeout)."""
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.3)
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    kind = prompt_proxy()

    # 1. Start the proxy in the background
    proxy_port, _ = start_proxy(kind)

    # 2. Point the engines at the chosen proxy
    os.environ["STAGEHAND_SERVER_URL"] = f"http://localhost:{proxy_port}"

    # 3. Wait for the proxy to be reachable
    if wait_for_port(proxy_port):
        print(f"  Proxy is up on port {proxy_port}")
    else:
        print(f"  WARNING: proxy didn't respond on {proxy_port} — "
              f"engines will fall back to direct LLM calls.")

    # 4. Open the dashboard in the browser shortly after it starts
    dashboard_port = int(os.getenv("DASHBOARD_PORT", "5000"))
    dashboard_url = f"http://127.0.0.1:{dashboard_port}"

    def _open_browser() -> None:
        if wait_for_port(dashboard_port, timeout=20.0):
            webbrowser.open(dashboard_url)
    threading.Thread(target=_open_browser, daemon=True).start()

    # 5. Run the dashboard in the foreground (main thread → Ctrl+C works)
    print()
    print(f"  Dashboard starting at {dashboard_url}")
    print("  (press Ctrl+C to stop everything)")
    print("=" * 60)
    print()

    from dashboard.app import dashboard_app
    uvicorn.run(dashboard_app, host="127.0.0.1", port=dashboard_port, log_level="info")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Shutting down. Bye.")
    except Exception as exc:  # keep the console open so the user sees the error
        import traceback
        traceback.print_exc()
        input("\n  An error occurred. Press Enter to exit...")
