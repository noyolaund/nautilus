"""JDE Automation Launcher.

Single entry point that:
  1. Prompts which LLM proxy to use (proxy / proxy-jnj)
  2. Spawns that proxy server in its OWN console window
  3. Spawns the dashboard web app in its OWN console window
  4. Opens the dashboard in the default browser

Run directly:
    python launcher.py

Or build into a standalone .exe (see build_launcher.bat).

Internal modes (used when the launcher re-spawns itself):
    python launcher.py --run proxy-globant
    python launcher.py --run proxy-jnj
    python launcher.py --run dashboard
"""

from __future__ import annotations

import os
import sys
import time
import socket
import argparse
import subprocess
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
load_dotenv(RUN_DIR / ".env")
load_dotenv(BASE_DIR / ".env")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _self_command(mode: str) -> list[str]:
    """Build the command that re-invokes this launcher in a given --run mode.

    Works whether running as a frozen .exe or a plain .py script.
    """
    if getattr(sys, "frozen", False):
        # The exe IS the launcher — just pass the flag
        return [sys.executable, "--run", mode]
    # Running as a script — invoke python with this file
    return [sys.executable, str(Path(__file__).resolve()), "--run", mode]


def spawn_in_new_console(cmd: list[str], title: str) -> subprocess.Popen:
    """Start *cmd* in a brand-new console window."""
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_CONSOLE  # type: ignore[attr-defined]
    # On Windows, `start` would also work, but CREATE_NEW_CONSOLE keeps a
    # direct handle to the child process.
    env = dict(os.environ)
    proc = subprocess.Popen(cmd, creationflags=creationflags, cwd=str(RUN_DIR), env=env)
    print(f"  → {title} started (PID {proc.pid})")
    return proc


def wait_for_port(port: int, timeout: float = 30.0) -> bool:
    """Poll until something is listening on *port* (or timeout)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.3)
    return False


# ---------------------------------------------------------------------------
# --run modes — each runs a single server in the current console
# ---------------------------------------------------------------------------

def run_proxy_globant() -> None:
    import uvicorn
    from proxy.azure_proxy import proxy_app
    port = int(os.getenv("PROXY_PORT", "3456"))
    print(f"Globant GeAI proxy → http://127.0.0.1:{port}")
    uvicorn.run(proxy_app, host="127.0.0.1", port=port, log_level="info")


def run_proxy_jnj() -> None:
    import uvicorn
    from proxy.jnj_proxy import jnj_proxy_app
    port = int(os.getenv("JNJ_PROXY_PORT", "3457"))
    print(f"JNJ Azure proxy → http://127.0.0.1:{port}")
    uvicorn.run(jnj_proxy_app, host="127.0.0.1", port=port, log_level="info")


def run_dashboard() -> None:
    import uvicorn
    from dashboard.app import dashboard_app
    port = int(os.getenv("DASHBOARD_PORT", "5000"))
    print(f"Dashboard → http://127.0.0.1:{port}")
    uvicorn.run(dashboard_app, host="127.0.0.1", port=port, log_level="info")


_RUN_MODES = {
    "proxy-globant": run_proxy_globant,
    "proxy-jnj": run_proxy_jnj,
    "dashboard": run_dashboard,
}


# ---------------------------------------------------------------------------
# Proxy selection prompt
# ---------------------------------------------------------------------------

def prompt_proxy() -> str:
    """Ask which proxy to start. Returns 'globant' or 'jnj'."""
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
# Orchestrator — prompts, then spawns proxy + dashboard in separate consoles
# ---------------------------------------------------------------------------

def orchestrate() -> None:
    kind = prompt_proxy()

    if kind == "jnj":
        proxy_mode = "proxy-jnj"
        proxy_port = int(os.getenv("JNJ_PROXY_PORT", "3457"))
        proxy_title = "JNJ Azure Proxy"
    else:
        proxy_mode = "proxy-globant"
        proxy_port = int(os.getenv("PROXY_PORT", "3456"))
        proxy_title = "Globant GeAI Proxy"

    dashboard_port = int(os.getenv("DASHBOARD_PORT", "5000"))

    print()
    print(f"  Launching {proxy_title} and Dashboard in separate windows...")
    print()

    # 1. Spawn the proxy in its own console
    spawn_in_new_console(_self_command(proxy_mode), proxy_title)

    # 2. Wait for the proxy to come up
    if wait_for_port(proxy_port):
        print(f"  Proxy is up on port {proxy_port}")
    else:
        print(f"  WARNING: proxy didn't respond on {proxy_port} — "
              f"engines will fall back to direct LLM calls.")

    # 3. Spawn the dashboard in its own console
    spawn_in_new_console(_self_command("dashboard"), "Dashboard")

    # 4. Wait for the dashboard, then open the browser
    dashboard_url = f"http://127.0.0.1:{dashboard_port}"
    if wait_for_port(dashboard_port):
        print(f"  Dashboard is up — opening {dashboard_url}")
        webbrowser.open(dashboard_url)
    else:
        print(f"  WARNING: dashboard didn't respond on {dashboard_port}")

    print()
    print("=" * 60)
    print("  Both servers are running in their own windows.")
    print(f"  Dashboard: {dashboard_url}")
    print(f"  Proxy:     http://127.0.0.1:{proxy_port}")
    print()
    print("  Close those windows (or this one) to stop the servers.")
    print("=" * 60)
    input("\n  Press Enter to close this launcher window...")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--run", choices=list(_RUN_MODES.keys()), default=None)
    args, _ = parser.parse_known_args()

    if args.run:
        # Internal mode: run a single server in this console
        _RUN_MODES[args.run]()
    else:
        # Default mode: prompt + spawn both in separate consoles
        orchestrate()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  Stopped.")
    except Exception:
        import traceback
        traceback.print_exc()
        input("\n  An error occurred. Press Enter to exit...")
