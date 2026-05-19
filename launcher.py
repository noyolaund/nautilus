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

Everything printed is ALSO written to launcher.log next to the exe, so a
blank/crashed console can still be diagnosed afterwards.
"""

from __future__ import annotations

import os
import sys
import time
import socket
import argparse
import subprocess
import threading
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Path resolution — works as a plain script AND as a PyInstaller .exe
# ---------------------------------------------------------------------------

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys._MEIPASS)            # type: ignore[attr-defined]
    RUN_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent
    RUN_DIR = BASE_DIR

sys.path.insert(0, str(BASE_DIR))
try:
    os.chdir(RUN_DIR)
except Exception:
    pass

LOG_FILE = RUN_DIR / "launcher.log"


# ---------------------------------------------------------------------------
# Logging — print to console (flushed) AND append to launcher.log
# ---------------------------------------------------------------------------

def _make_unbuffered() -> None:
    """Force stdout/stderr to be line-buffered so prints show immediately.

    Frozen exes often block-buffer stdout, which makes the console look
    blank even though the program is running (or waiting on input()).
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
        except Exception:
            pass


def log(msg: str = "") -> None:
    """Print to console (flushed) and append to launcher.log."""
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"{ts}  {msg}" if msg else ""
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Load .env
# ---------------------------------------------------------------------------

def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        loaded_run = load_dotenv(RUN_DIR / ".env")
        loaded_base = load_dotenv(BASE_DIR / ".env")
        log(f".env loaded: run_dir={loaded_run}  bundle={loaded_base}")
    except Exception as exc:
        log(f"WARNING: could not load .env — {exc}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _self_command(mode: str) -> list[str]:
    """Build the command that re-invokes this launcher in a given --run mode."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--run", mode]
    return [sys.executable, str(Path(__file__).resolve()), "--run", mode]


def spawn_in_new_console(cmd: list[str], title: str) -> "subprocess.Popen | None":
    """Start *cmd* in a brand-new console window."""
    log(f"Spawning [{title}]: {' '.join(cmd)}")
    try:
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_CONSOLE  # type: ignore[attr-defined]
        proc = subprocess.Popen(
            cmd,
            creationflags=creationflags,
            cwd=str(RUN_DIR),
            env=dict(os.environ),
        )
        log(f"  → [{title}] started, PID {proc.pid}")
        return proc
    except Exception as exc:
        log(f"  ✖ FAILED to spawn [{title}]: {type(exc).__name__}: {exc}")
        log(traceback.format_exc())
        return None


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
    _make_unbuffered()
    log("Mode: Globant GeAI proxy")
    import uvicorn
    from proxy.azure_proxy import proxy_app
    port = int(os.getenv("PROXY_PORT", "3456"))
    log(f"Globant GeAI proxy → http://127.0.0.1:{port}")
    uvicorn.run(proxy_app, host="127.0.0.1", port=port, log_level="info")


def run_proxy_jnj() -> None:
    _make_unbuffered()
    log("Mode: JNJ Azure proxy")
    import uvicorn
    from proxy.jnj_proxy import jnj_proxy_app
    port = int(os.getenv("JNJ_PROXY_PORT", "3457"))
    log(f"JNJ Azure proxy → http://127.0.0.1:{port}")
    uvicorn.run(jnj_proxy_app, host="127.0.0.1", port=port, log_level="info")


def run_dashboard() -> None:
    _make_unbuffered()
    log("Mode: Dashboard")
    import uvicorn
    from dashboard.app import dashboard_app
    port = int(os.getenv("DASHBOARD_PORT", "5000"))
    log(f"Dashboard → http://127.0.0.1:{port}")
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
    log()
    log("=" * 60)
    log("  JDE Automation Launcher")
    log("=" * 60)
    log()
    log("  Select the LLM proxy to use:")
    log()
    log("    [1] proxy      — Globant GeAI")
    log("    [2] proxy-jnj  — JNJ Azure OpenAI (Cloud PC, needs VPN)")
    log()
    while True:
        try:
            choice = input("  Enter 1 or 2 (default 1): ").strip()
        except EOFError:
            # No interactive stdin (rare) — default to Globant
            log("  (no input available, defaulting to 1)")
            return "globant"
        if choice in ("", "1"):
            return "globant"
        if choice == "2":
            return "jnj"
        print("  Invalid choice — type 1 or 2.", flush=True)


# ---------------------------------------------------------------------------
# Orchestrator — prompts, then spawns proxy + dashboard in separate consoles
# ---------------------------------------------------------------------------

def orchestrate() -> None:
    log(f"Launcher start — frozen={getattr(sys, 'frozen', False)}")
    log(f"  sys.executable = {sys.executable}")
    log(f"  BASE_DIR       = {BASE_DIR}")
    log(f"  RUN_DIR        = {RUN_DIR}")
    _load_env()

    kind = prompt_proxy()
    log(f"Selected proxy: {kind}")

    if kind == "jnj":
        proxy_mode = "proxy-jnj"
        proxy_port = int(os.getenv("JNJ_PROXY_PORT", "3457"))
        proxy_title = "JNJ Azure Proxy"
    else:
        proxy_mode = "proxy-globant"
        proxy_port = int(os.getenv("PROXY_PORT", "3456"))
        proxy_title = "Globant GeAI Proxy"

    dashboard_port = int(os.getenv("DASHBOARD_PORT", "5000"))

    log()
    log(f"  Launching {proxy_title} and Dashboard in separate windows...")
    log()

    # 1. Spawn the proxy in its own console
    proxy_proc = spawn_in_new_console(_self_command(proxy_mode), proxy_title)
    if proxy_proc is None:
        log("  ✖ Could not start the proxy — aborting.")
        input("\n  Press Enter to exit...")
        return

    # 2. Wait for the proxy to come up
    log(f"  Waiting for proxy on port {proxy_port}...")
    if wait_for_port(proxy_port):
        log(f"  ✓ Proxy is up on port {proxy_port}")
    else:
        log(f"  ⚠ Proxy didn't respond on {proxy_port} within 30s — "
            f"engines will fall back to direct LLM calls.")

    # 3. Spawn the dashboard in its own console
    dash_proc = spawn_in_new_console(_self_command("dashboard"), "Dashboard")
    if dash_proc is None:
        log("  ✖ Could not start the dashboard — aborting.")
        input("\n  Press Enter to exit...")
        return

    # 4. Wait for the dashboard, then open the browser
    dashboard_url = f"http://127.0.0.1:{dashboard_port}"
    log(f"  Waiting for dashboard on port {dashboard_port}...")
    if wait_for_port(dashboard_port):
        log(f"  ✓ Dashboard is up — opening {dashboard_url}")
        try:
            webbrowser.open(dashboard_url)
        except Exception as exc:
            log(f"  ⚠ Could not open browser automatically: {exc}")
    else:
        log(f"  ⚠ Dashboard didn't respond on {dashboard_port} within 30s")

    log()
    log("=" * 60)
    log("  Both servers are running in their own windows.")
    log(f"  Dashboard: {dashboard_url}")
    log(f"  Proxy:     http://127.0.0.1:{proxy_port}")
    log("=" * 60)
    log()
    input("  Press Enter to close this launcher window "
          "(servers keep running)...")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    _make_unbuffered()

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--run", choices=list(_RUN_MODES.keys()), default=None)
    args, _ = parser.parse_known_args()

    if args.run:
        _load_env()
        _RUN_MODES[args.run]()
    else:
        orchestrate()


if __name__ == "__main__":
    # Truncate the log at the start of an orchestrate run (not for --run children)
    if "--run" not in sys.argv:
        try:
            LOG_FILE.write_text("", encoding="utf-8")
        except Exception:
            pass
    try:
        main()
    except KeyboardInterrupt:
        log("\n  Stopped (Ctrl+C).")
    except Exception:
        tb = traceback.format_exc()
        log("\n  ✖ UNHANDLED ERROR:")
        log(tb)
        try:
            input("\n  An error occurred (see launcher.log). Press Enter to exit...")
        except Exception:
            time.sleep(15)  # keep the window up long enough to read
