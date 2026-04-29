"""JDE Full Path — Copy Report Version (Python test case).

Uses the framework's hybrid engine: CSS selectors first, LLM fallback.
Same resolution chain as the JSON tests (cache, label, placeholder,
role, text, adjacent-input, AI).

Run:
    python tests/test_jde_full.py

With pytest:
    pytest tests/test_jde_full.py -v -s
"""

import asyncio
import sys
from pathlib import Path

# Add project root to Python path so imports work from any directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from playwright.async_api import async_playwright
from engines.step_runner import StepRunner, StepError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

JDE_URL = "http://e1w0000036.jnj.com:9222/jde/E1Menu.maf"
USERNAME = "jnoyolam"
PASSWORD = "AmdsamI8!"

# Data (would come from Excel in the dashboard)
APP_REPORT = "R4311Z1I"
CURRENT_VERSION = "EDOES011"
NEW_VERSION = "DPSES0116"
NEW_VERSION_TITLE = "DPS6 - PO Inbound - Mitek - JDEPOASN"
LEFT_OPERAND_VALUE = "ESD501"
PROCESSING_OPTION = "EDOBE017"

IFRAME = "iframe#e1menuAppIframe"


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

async def run():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1920, "height": 1080})
        page = await context.new_page()

        # Create the step runner — CSS first, LLM fallback
        r = StepRunner(page)

        try:
            # ── Login ───────────────────────────────────────────────────
            await r.navigate(JDE_URL)

            # CSS selector → if not found → LLM finds "User" field
            await r.type("the User ID field", value=USERNAME, sensitive=True)
            await r.type("the Password field", value=PASSWORD, sensitive=True)

            # AI finds the Sign In button (could be <input value="Sign In">)
            await r.click("the 'Sign In' button")
            await r.assert_visible("Welcome!")
            await r.screenshot()

            # ── Submit Job ──────────────────────────────────────────────
            # AI finds "Submit Job" text link
            await r.click("the 'Submit Job' text")
            await r.screenshot()

            # ── Batch Application (CSS + iframe) ────────────────────────
            await r.type(
                "Batch Application field",
                value="R4311Z1I",
                selector="#C0_11", iframe=IFRAME,
                selector_strategy="css"
            )

            # Find button via Ctrl+Alt+I
            await r.key_press("Ctrl+Alt+I")

            # ── Search current version ──────────────────────────────────
            await r.type(
                "version QBE filter",
                value=CURRENT_VERSION,
                selector="input[name='qbe0_1.1']", iframe=IFRAME,
                selector_strategy="css"
            )
            await r.key_press("Enter")

            # ── Select & Copy ───────────────────────────────────────────
            await r.click("Select All checkbox", selector="#selectAll0_1", iframe=IFRAME, selector_strategy="css")
            await r.click("Copy button", selector="#hc_Copy", iframe=IFRAME, selector_strategy="css")

            # ── Fill new version ────────────────────────────────────────
            await r.type("New Version field", value=NEW_VERSION, selector="#C0_17", iframe=IFRAME, selector_strategy="css")
            await r.type("New Version Title", value=NEW_VERSION_TITLE, selector="#C0_21", iframe=IFRAME, selector_strategy="css")

            # ── Check for errors ────────────────────────────────────────
            await r.check_error("#INYFEContent")

            # ── Click OK ────────────────────────────────────────────────
            await r.click("OK button", selector="#hc_OK", iframe=IFRAME, selector_strategy="css")

            # ── Search new version ──────────────────────────────────────
            await r.type(
                "version QBE filter",
                value=NEW_VERSION,
                selector="input[name='qbe0_1.1']", iframe=IFRAME,
                selector_strategy="css"
            )
            await r.key_press("Enter")
            await r.click("Select All checkbox", selector="#selectAll0_1", iframe=IFRAME, selector_strategy="css")

            # ── Data Selection ──────────────────────────────────────────
            await r.click("Row Menu", selector="#C0_58", iframe=IFRAME, selector_strategy="css")
            await r.click("Data Selection option", selector="#HEC0_127", iframe=IFRAME, selector_strategy="css")
            await r.select(
                "Right Operand dropdown",
                value="Literal",
                selector="#RightOperand3", iframe=IFRAME, selector_strategy="css"
            )
            await r.type("Literal text field", value=LEFT_OPERAND_VALUE, selector="#LITtf", iframe=IFRAME, selector_strategy="css")
            await r.click("Select button", selector="#hc_Select", iframe=IFRAME, selector_strategy="css")
            await r.screenshot()
            await r.click("Select button (confirm)", selector="#hc_Select", iframe=IFRAME, selector_strategy="css")

            # ── Processing Options ──────────────────────────────────────
            await r.click("Row Menu", selector="#C0_58", iframe=IFRAME, selector_strategy="css")
            await r.click("Processing Options", selector="#HE0_118", iframe=IFRAME, selector_strategy="css")
            await r.type("P.O. Entry field", value=PROCESSING_OPTION, selector="#PO1T0", iframe=IFRAME, selector_strategy="css")
            await r.click("OK button", selector="#hc_Select", iframe=IFRAME, selector_strategy="css")

            # ── Done ────────────────────────────────────────────────────
            await r.screenshot()
            print("\n✓ JDE Full Path completed successfully")

        except StepError as e:
            print(f"\n✖ FAILED at: {e}")
            await page.screenshot(path="logs/jde_full_error.png", full_page=True)
            sys.exit(1)

        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    Path("logs").mkdir(exist_ok=True)
    asyncio.run(run())
