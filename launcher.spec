# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the JDE Automation Launcher.

Build with:
    pyinstaller launcher.spec

Produces dist/JDE-Automation-Launcher.exe

NOTE: Playwright's Chromium is NOT bundled (it's ~150 MB and lives in a
separate cache). On the target machine run once:
    playwright install chromium
or set PLAYWRIGHT_BROWSERS_PATH to a shared location.
"""

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

# Data files that must ship next to the code
datas = [
    ("dashboard/index.html", "dashboard"),
    ("tests/test_cases", "tests/test_cases"),
    ("config", "config"),
]
# Bundle .env so the exe has defaults (the launcher also reads .env next to the exe)
import os
if os.path.exists(".env"):
    datas.append((".env", "."))

# uvicorn / fastapi / playwright load some modules dynamically
hiddenimports = []
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("fastapi")
hiddenimports += collect_submodules("playwright")
hiddenimports += collect_submodules("openpyxl")
hiddenimports += [
    "engines.hybrid_playwright_engine",
    "engines.stagehand_ai_engine",
    "engines.step_runner",
    "engines.base_engine",
    "proxy.azure_proxy",
    "proxy.jnj_proxy",
    "dashboard.app",
    "dashboard.session_manager",
    "tests.test_jde_full",
]

a = Analysis(
    ["launcher.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="JDE-Automation-Launcher",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,          # keep the console — the launcher prompts for proxy
    disable_windowed_traceback=False,
    icon=None,
)
