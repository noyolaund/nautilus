"""SAP Solman login via Playwright with SAP Secure Login Client (SLC).

SAP Solution Manager (and most SAP web apps that use SSO) authenticate via
an X.509 client certificate that SAP Secure Login Client publishes into
the Windows certificate store. A normal Chrome session picks the cert up
automatically; Playwright's Chromium runs with a clean profile and does
not see those certs, so navigating to Solman either prompts for a cert
the user can't pick, or fails with ERR_BAD_SSL_CLIENT_AUTH_CERT.

This file supports three connection modes, controlled by SOLMAN_LOGIN_MODE
in .env:

    cert_file    Use Playwright's built-in client_certificates option
                 (Playwright >= 1.46). Requires a PFX/PEM exported from
                 SLC or from the Windows certmgr. RECOMMENDED for CI/headless.

    cdp          Connect to an already-running Chrome with
                 --remote-debugging-port=9222 — that Chrome shares the
                 Windows cert store, so SLC's cert is available. Most
                 reliable on a workstation where SLC is already running.

    none         Just launch a fresh Chromium (current behaviour). Will
                 fail at the cert prompt — only useful when Solman is
                 accessed via a non-SSO route or for non-SLC environments.

Required .env entries:
    SOLMAN_URL=https://your-solman/sap/bc/...
    SOLMAN_USERNAME=...
    SOLMAN_PASSWORD=...
    SOLMAN_LOGIN_MODE=cert_file|cdp|none

    # cert_file mode
    SOLMAN_CERT_PATH=C:\\path\\to\\cert.pfx      # PFX/P12 OR
    SOLMAN_CERT_PEM_PATH=C:\\path\\to\\cert.pem  # PEM cert
    SOLMAN_KEY_PEM_PATH=C:\\path\\to\\key.pem    # PEM key (cert_path + key_path)
    SOLMAN_CERT_PASSPHRASE=...                   # PFX or encrypted-key password

    # cdp mode
    SOLMAN_CDP_URL=http://localhost:9222


HOW TO EXPORT THE SLC CERTIFICATE (one-time, cert_file mode)
    1. Open `certmgr.msc` (Windows)
    2. Personal → Certificates — find the SAP cert (issued by your CompanyName CA)
    3. Right-click → All Tasks → Export...
    4. Yes, export the private key → PFX → set a password → save .pfx
    5. Point SOLMAN_CERT_PATH and SOLMAN_CERT_PASSPHRASE at it

HOW TO RUN CHROME IN DEBUG MODE (cdp mode)
    From a Windows command prompt while SLC is running:
        "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" ^
            --remote-debugging-port=9222 ^
            --user-data-dir="C:\\Temp\\chrome-slc"
    Log into Solman manually once so SLC picks the cert; subsequent
    Playwright runs will reuse that authenticated session.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Optional

# Add project root to Python path so imports work from any directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from playwright.async_api import async_playwright, Playwright
from engines.step_runner import StepRunner

# ---------------------------------------------------------------------------
# Configuration — read from .env
# ---------------------------------------------------------------------------

SOLMAN_URL = os.getenv("SOLMAN_URL", "").strip()
USERNAME = os.getenv("SOLMAN_USERNAME", "").strip()
PASSWORD = os.getenv("SOLMAN_PASSWORD", "").strip()
IFRAME = "iframe#CRMApplicationFrame"

LOGIN_MODE = os.getenv("SOLMAN_LOGIN_MODE", "none").strip().lower()
CERT_PATH = os.getenv("SOLMAN_CERT_PATH", "").strip()            # PFX/P12
CERT_PEM_PATH = os.getenv("SOLMAN_CERT_PEM_PATH", "").strip()    # PEM cert
KEY_PEM_PATH = os.getenv("SOLMAN_KEY_PEM_PATH", "").strip()      # PEM key
CERT_PASSPHRASE = os.getenv("SOLMAN_CERT_PASSPHRASE", "").strip()
CDP_URL = os.getenv("SOLMAN_CDP_URL", "http://localhost:9222").strip()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cert_origin(url: str) -> str:
    """Playwright's client_certificates is matched per ORIGIN — strip path."""
    from urllib.parse import urlparse
    p = urlparse(url)
    if not p.scheme or not p.netloc:
        return url
    return f"{p.scheme}://{p.netloc}"


def _build_client_certificates(url: str) -> list[dict]:
    """Build the client_certificates list for browser.new_context()."""
    if not url:
        raise RuntimeError("SOLMAN_URL is empty — cannot scope client certificate")

    origin = _cert_origin(url)
    entry: dict[str, Any] = {"origin": origin}

    # Playwright's client_certificates uses CAMEL-CASE keys inside each dict
    # (certPath, keyPath, pfxPath, passphrase), even though the top-level
    # kwarg uses snake_case. Don't mix them up.
    if CERT_PATH:                       # PFX / P12 file
        entry["pfxPath"] = CERT_PATH
        if CERT_PASSPHRASE:
            entry["passphrase"] = CERT_PASSPHRASE
    elif CERT_PEM_PATH and KEY_PEM_PATH:
        entry["certPath"] = CERT_PEM_PATH
        entry["keyPath"] = KEY_PEM_PATH
        if CERT_PASSPHRASE:
            entry["passphrase"] = CERT_PASSPHRASE
    else:
        raise RuntimeError(
            "cert_file mode needs SOLMAN_CERT_PATH (PFX) OR "
            "SOLMAN_CERT_PEM_PATH + SOLMAN_KEY_PEM_PATH"
        )

    print(f"  Client certificate scoped to origin: {origin}")
    return [entry]


# ---------------------------------------------------------------------------
# Connection modes — each returns (playwright, browser, context, page)
#
# cert_file: fresh Chromium + client_certificates option (Playwright >= 1.46)
# cdp:       attach to an existing Chrome that already has SLC cert installed
# none:      fresh Chromium with no cert (the existing behaviour)
# ---------------------------------------------------------------------------

async def _connect_cert_file(pw: Playwright):
    print("Connection mode: cert_file (Playwright client_certificates)")
    browser = await pw.chromium.launch(headless=False)
    context = await browser.new_context(
        viewport={"width": 1920, "height": 1080},
        client_certificates=_build_client_certificates(SOLMAN_URL),  # type: ignore[arg-type]
        ignore_https_errors=False,
    )
    page = await context.new_page()
    return browser, context, page


async def _connect_cdp(pw: Playwright):
    print(f"Connection mode: cdp (attaching to {CDP_URL})")
    print(
        "  Make sure Chrome is running with:\n"
        "    chrome.exe --remote-debugging-port=9222 --user-data-dir=\"C:\\Temp\\chrome-slc\"\n"
        "  and that you've already logged into Solman once so SLC supplied the cert."
    )
    browser = await pw.chromium.connect_over_cdp(CDP_URL)
    # Reuse the existing context (it has cookies + cert handshake state)
    if browser.contexts:
        context = browser.contexts[0]
    else:
        context = await browser.new_context()
    if context.pages:
        page = context.pages[0]
    else:
        page = await context.new_page()
    return browser, context, page


async def _connect_none(pw: Playwright):
    print("Connection mode: none (fresh Chromium, no client cert)")
    browser = await pw.chromium.launch(headless=False)
    context = await browser.new_context(viewport={"width": 1920, "height": 1080})
    page = await context.new_page()
    return browser, context, page


async def open_solman_session(pw: Playwright):
    """Pick the connection mode from .env and return (browser, context, page)."""
    mode = LOGIN_MODE
    if mode == "cert_file":
        return await _connect_cert_file(pw)
    if mode == "cdp":
        return await _connect_cdp(pw)
    if mode == "none":
        return await _connect_none(pw)
    raise RuntimeError(
        f"Unknown SOLMAN_LOGIN_MODE={mode!r}. Use cert_file | cdp | none."
    )


# ---------------------------------------------------------------------------
# Login flow — runs AFTER the cert handshake has established the session
# ---------------------------------------------------------------------------

async def login(runner: StepRunner) -> None:
    """Navigate to Solman and complete the form-based login that follows
    the SLC certificate handshake (if any).

    Many SAP Solman setups complete login purely via the client cert and
    do not show a username/password form. In that case the form steps below
    will fail their element-find quickly and the iteration moves on. To skip
    the form-based steps entirely, set SOLMAN_SKIP_FORM_LOGIN=true in .env.
    """
    await runner.navigate(SOLMAN_URL)

    # If the cert handshake fully authenticates the user, Solman lands on
    # the launchpad directly — skip the form-based fallbacks.
    if os.getenv("SOLMAN_SKIP_FORM_LOGIN", "").lower() in ("1", "true", "yes"):
        print("  SOLMAN_SKIP_FORM_LOGIN=true — leaving login to the cert handshake")
        await runner.screenshot()
        return

    # Form-based fallback for environments that still show a login button +
    # username/password form after the cert prompt.
    try:
        await runner.click(
            "Click the Login button",
            selector="#LOGON_BUTTON", iframe=IFRAME, selector_strategy="css",
        )
    except Exception as exc:
        print(f"  No #LOGON_BUTTON ({exc}); skipping that step")

    try:
        await runner.type(
            "Fill the username (MS prompt)",
            value=USERNAME,
            selector="#i0116", selector_strategy="css",
        )
        await runner.click(
            "the Next button",
            selector="#idSIButton9", selector_strategy="css",
        )
    except Exception as exc:
        print(f"  Microsoft username prompt not present ({exc}); skipping")

    try:
        await runner.type(
            "Fill the username (JnJ form)",
            value=USERNAME,
            selector="#username", selector_strategy="css", sensitive=True,
        )
        await runner.type(
            "Fill the password",
            value=PASSWORD,
            selector="#password", selector_strategy="css", sensitive=True,
        )
    except Exception as exc:
        print(f"  JnJ username/password form not present ({exc}); skipping")

    await runner.screenshot()


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

async def main() -> None:
    Path("logs").mkdir(exist_ok=True)

    if not SOLMAN_URL:
        print("ERROR: SOLMAN_URL is not set in .env")
        sys.exit(1)

    async with async_playwright() as pw:
        # `_context` keeps a reference alive so the BrowserContext isn't GC'd
        # while we drive `page`. The variable itself is intentionally unused.
        browser, _context, page = await open_solman_session(pw)
        runner = StepRunner(page)

        try:
            await login(runner)
            print("\n✓ Solman session is open — leaving the browser up for inspection.")
            # Hold the browser open for a moment so the user can verify the
            # cert handshake worked; replace this with the actual flow when ready.
            await asyncio.sleep(60)
        finally:
            try:
                # In CDP mode we attached to a running Chrome — don't close it.
                if LOGIN_MODE != "cdp":
                    await browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())
