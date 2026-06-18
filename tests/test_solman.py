"""JDE Full Path — Copy Report Version (Python test case).

Uses the framework's hybrid engine: CSS selectors first, LLM fallback.

Supports multiple data selection rows and multiple processing option rows
per report — the data selection block loops once per entry.

Run standalone:
    python tests/test_jde_full.py

Or import and call programmatically (used by the dashboard):
    from tests.test_jde_full import run_jde_full
    await run_jde_full(page, report_group)
"""

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Optional

# Add project root to Python path so imports work from any directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from playwright.async_api import async_playwright, Page
from engines.step_runner import StepRunner, StepError

# ---------------------------------------------------------------------------
# Configuration — read from .env
# ---------------------------------------------------------------------------

SOLMAN_URL = os.getenv("SOLMAN_URL", "")
USERNAME = os.getenv("SOLMAN_USERNAME", "")
PASSWORD = os.getenv("SOLMAN_PASSWORD", "")
IFRAME = "iframe#CRMApplicationFrame"


# ---------------------------------------------------------------------------
# Login flow (used when running standalone)
# ---------------------------------------------------------------------------


async def fill_jde_field(page: Page, selector: str, value: str, iframe: str = IFRAME) -> None:
    """Robustly fill a JDE input field.

    JDE's onkeyup/onblur handlers can swallow characters when Playwright's
    .fill() fires synthetic events. Real keystrokes via press_sequentially
    fire a proper keydown/keyup per character, then we Tab to blur and
    commit the value.

    Strips whitespace from the value to avoid stray leading/trailing spaces.
    """
    value = (str(value) if value is not None else "").strip()

    frame = page.frame_locator(iframe) if iframe else page
    locator = frame.locator(selector)

    await locator.first.wait_for(state="visible", timeout=5000)
    # 1. Click to focus
    await locator.first.click()
    # 2. Select all + delete to clear
    await page.keyboard.press("Control+a")
    await page.keyboard.press("Delete")
    # 3. Type each character — fires real keyboard events
    await locator.first.press_sequentially(value, delay=20)
    # 4. Brief settle, then Tab to blur and commit
    await asyncio.sleep(0.2)
    await page.keyboard.press("Tab")
    print(f"      Typed {value!r} into {selector}")


async def login(runner: StepRunner) -> None:
    """Run the JDE login flow."""
    await runner.navigate(SOLMAN_URL)
    await runner.click("Click the Login button", selector="#LOGON_BUTTON", iframe=IFRAME, selector_strategy="css")
    await runner.type(
            "Fill the username",
            value=USERNAME,
            selector="#i0116", iframe=IFRAME, selector_strategy="css"
        )
    await runner.click("the Next button", selector="#idSIButton9", iframe=IFRAME, selector_strategy="css")
    await runner.type(
            "Fill the username in JnJ",
            value=USERNAME,
            selector="#username", iframe=IFRAME, selector_strategy="css"
        )
    await runner.type(
            "Fill the password",
            value=PASSWORD,
            selector="#password", iframe=IFRAME, selector_strategy="css"
        )
    """
    await runner.type("the User ID field", value=USERNAME, sensitive=True)
    await runner.type("the Password field", value=PASSWORD, sensitive=True)
    await runner.assert_visible("Welcome!")
    """
    await runner.screenshot()


# ---------------------------------------------------------------------------
# Main JDE Full Path flow — callable per Excel report group
# ---------------------------------------------------------------------------

async def run_jde_full(page: Page, report_group: dict[str, Any]) -> dict[str, Any]:
    """Execute the JDE Full Path flow for one report.

    report_group structure:
    {
        "report": {
            "app_report": "R4311Z1I",
            "current_version": "EDOES011",
            "new_version": "DPSES0116",
            "new_version_title": "DPS6 - PO Inbound - ...",
            ...
        },
        "data_selections": [
            {"left_operand": "Transaction Originator", "data_new": "088"},
            {"left_operand": "Account",                "data_new": "ESD501"},
            ...   # multiple rows = multiple data selection entries
        ],
        "processing_options": [
            {"tab": "10a", "option_number": "5", "processing_new": "INV"},
            ...
        ]
    }

    Returns: {"status": "pass"|"fail", "error": str|None, "report": ...}
    """
    report = report_group["report"]
    data_selections = report_group.get("data_selections", []) or []
    processing_options = report_group.get("processing_options", []) or []

    # Reset the step counter so each iteration's logs start at S001
    StepRunner.reset_step_counter()
    runner = StepRunner(page)
    label = f"{report.get('app_report', '?')}/{report.get('new_version', '?')}"

    try:
        # ── Submit Job ──────────────────────────────────────────────────
        print(f"\n[{label}] === Starting JDE Full Path ===")
        print(f"[{label}] Data selections: {len(data_selections)}, Processing options: {len(processing_options)}")

        # Branch on the App/Report prefix: R-* reports vs P-* applications
        # use different Fast Path entry points and QBE filter columns.
        app_report_value = str(report.get("app_report", "")).strip().upper()
        is_p_app = app_report_value.startswith("P")

        if is_p_app:
            fast_path_value = "iv"
            version_qbe_selector = "input[name='qbe0_1.0']"
            version_name_new = "#C0_20"
            version_name_title = "#C0_18"
            row_menu_selector = "#C0_69"
            processing_options_selector = "#HE0_19"
        else:
            fast_path_value = "bv"
            version_qbe_selector = "input[name='qbe0_1.1']"
            version_name_new = "#C0_17"
            version_name_title = "#C0_21"
            row_menu_selector = "#C0_58"
            processing_options_selector = "#HE0_118"
        print(f"[{label}] App type: {'P' if is_p_app else 'R'}  "
              f"fast_path={fast_path_value!r}  "
              f"qbe_selector={version_qbe_selector!r}")

        await runner.type(
            "Fast Path input",
            value=fast_path_value,
            selector="#TE_FAST_PATH_BOX",
            selector_strategy="css",
        )
        await runner.key_press("Enter")

        # ── Batch Application ───────────────────────────────────────────
        await runner.type(
            "Batch Application field",
            value=report["app_report"],
            selector="#C0_11", iframe=IFRAME, selector_strategy="css"
        )
        await runner.key_press("Ctrl+Alt+I")

        # ── Search current version ──────────────────────────────────────
        await runner.type(
            "version QBE filter",
            value=report["current_version"],
            selector=version_qbe_selector, iframe=IFRAME, selector_strategy="css"
        )
        await runner.key_press("Enter")

        # ── Select & Copy ───────────────────────────────────────────────
        await runner.click("Select All checkbox", selector="#selectAll0_1", iframe=IFRAME, selector_strategy="css")
        await runner.click("Copy button", selector="#hc_Copy", iframe=IFRAME, selector_strategy="css")

        # ── Fill new version ────────────────────────────────────────────
        await runner.type(
            "New Version field",
            value=report["new_version"],
            selector=version_name_new, iframe=IFRAME, selector_strategy="css"
        )
        await runner.type(
            "New Version Title",
            value=report.get("new_version_title", ""),
            selector=version_name_title, iframe=IFRAME, selector_strategy="css"
        )

        # ── Check for errors (e.g. version already exists) ──────────────
        await runner.check_error("#INYFEContent")

        # ── Click OK ────────────────────────────────────────────────────
        await runner.click("OK button", selector="#hc_OK", iframe=IFRAME, selector_strategy="css")

        # ── Search new version ──────────────────────────────────────────
        await runner.type(
            "version QBE filter",
            value=report["new_version"],
            selector=version_qbe_selector, iframe=IFRAME, selector_strategy="css"
        )
        await runner.key_press("Enter")
        await runner.click("Select All checkbox", selector="#selectAll0_1", iframe=IFRAME, selector_strategy="css")

        # ── Data Selection — loop once per entry ────────────────────────
        if data_selections:
            print(f"[{label}] Configuring {len(data_selections)} data selection(s)")
            await runner.click("Row Menu", selector=row_menu_selector, iframe=IFRAME, selector_strategy="css")
            await runner.click("Data Selection option", selector="#HE0_127", iframe=IFRAME, selector_strategy="css")

            for idx, sel in enumerate(data_selections, 1):
                left_operand = sel.get("left_operand", "")
                data_value = sel.get("data_new", "")
                print(f"[{label}]   DS {idx}: {left_operand} = {data_value}")

                # Find the matching RightOperand row by scanning all
                # LeftOperand dropdowns for one whose option text contains
                # the user's left_operand value. Stops the iteration if
                # nothing matches so the user can debug from the console output.
                try:
                    right_operand_sel = await find_right_operand_selector(page, left_operand)
                except LookupError as exc:
                    print(f"[{label}] ✖ {exc}")
                    await page.screenshot(
                        path=f"logs/jde_no_match_{report.get('app_report', 'unknown')}_{idx}.png",
                        full_page=True,
                    )
                    return {"status": "fail", "error": str(exc), "report": report}

                # Pick "Literal" from the matching right operand dropdown
                await runner.select(
                    "Right Operand dropdown",
                    value="Literal",
                    selector=right_operand_sel, iframe=IFRAME, selector_strategy="css"
                )
                # Enter the literal value char-by-char with explicit Tab commit
                # to avoid JDE's onkey handlers swallowing characters between
                # iterations.
                await fill_jde_field(page, "#LITtf", str(data_value))
                # Apply
                await runner.click("Select button", selector="#hc_Select", iframe=IFRAME, selector_strategy="css")
                await runner.screenshot()

            # Close the Data Selections dialog
            await runner.click("Close Data Selection dialog", selector="#hc_Select", iframe=IFRAME, selector_strategy="css")

        # --- Processing Options -- loop once per entry
        if processing_options:
            print(f"[{label}] Configuring {len(processing_options)} processing option(s)")
            await runner.click("Row Menu", selector=row_menu_selector, iframe=IFRAME, selector_strategy="css")
            await runner.click("Processing Options", selector=processing_options_selector, iframe=IFRAME, selector_strategy="css")
            # Wait for the Processing Options dialog to fully render its tabs
            await asyncio.sleep(2)

            for idx, po in enumerate(processing_options, 1):
                tab = po.get("tab", "")
                option_number_raw = po.get("option_number", "")
                processing_value = po.get("processing_new", "")
                print(f"[{label}]   PO {idx}: tab={tab!r}, opt={option_number_raw!r}, value={processing_value!r}")

                # Parse option_number as int
                try:
                    option_number = int(str(option_number_raw).strip())
                except (ValueError, TypeError):
                    print(f"      ✖ Invalid option_number: {option_number_raw!r}, skipping")
                    continue

                # 1. Click the tab by name
                if tab:
                    tab_selector = await find_processing_option_tab(page, tab)
                    if not tab_selector:
                        raise StepError(
                            "Find Processing Options tab",
                            f"Could not find tab named {tab!r}",
                            None,
                        )
                    print(f"      Tab matched: {tab_selector}")
                    await runner.click(
                        f"Tab {tab!r}",
                        selector=tab_selector, iframe=IFRAME, selector_strategy="css",
                    )
                    # Give the tab content time to render
                    await asyncio.sleep(1)

                # 2. Find the Nth text input on this tab and fill it
                if processing_value:
                    await fill_nth_processing_input(page, option_number, processing_value)

            # Apply (OK button closes the Processing Options dialog)
            await runner.click("OK button", selector="#hc_Select", iframe=IFRAME, selector_strategy="css")

        # ── Done ────────────────────────────────────────────────────────
        await runner.screenshot()
        print(f"[{label}] ✓ Completed successfully")
        return {
            "status": "pass",
            "error": None,
            "report": report,
            "steps": list(runner.results),
        }

    except StepError as e:
        # Step-level failure (element not found, timeout, etc.) — stop this iteration,
        # let the caller (dashboard) move on to the next one.
        print(f"[{label}] ✖ FAILED: {e}")
        try:
            await page.screenshot(path=f"logs/jde_full_error_{report.get('app_report', 'unknown')}.png", full_page=True)
        except Exception:
            pass
        return {
            "status": "fail",
            "error": str(e),
            "report": report,
            "steps": list(runner.results),
        }
    except Exception as e:
        # Anything unexpected — still don't crash the outer iteration loop
        import traceback
        print(f"[{label}] ✖ UNEXPECTED ERROR: {type(e).__name__}: {e}")
        traceback.print_exc()
        try:
            await page.screenshot(path=f"logs/jde_full_unexpected_{report.get('app_report', 'unknown')}.png", full_page=True)
        except Exception:
            pass
        return {
            "status": "fail",
            "error": f"{type(e).__name__}: {e}",
            "report": report,
            "steps": list(runner.results),
        }


# ---------------------------------------------------------------------------
# Standalone runner — used when calling this file directly
# ---------------------------------------------------------------------------

# Sample data when running standalone (replace with Excel data when integrating)
SAMPLE_REPORT = {
    "report": {
        "app_report": "R4311Z1I",
        "current_version": "EDOES011",
        "new_version": "DPSES0116",
        "new_version_title": "DPS6 - PO Inbound - Mitek - JDEPOASN",
    },
    "data_selections": [
        {"left_operand": "Transaction Originator", "data_new": "ESD501"},
        # Add more entries here to test the loop
    ],
    "processing_options": [
        {"tab": "10a", "option_number": "5", "processing_new": "EDOBE017"},
    ],
}


async def main():
    """Standalone runner: opens a browser, logs in, and runs one JDE Full Path."""
    Path("logs").mkdir(exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1920, "height": 1080})
        page = await context.new_page()

        runner = StepRunner(page)

        try:
            await login(runner)
            """
            result = await run_jde_full(page, SAMPLE_REPORT)
            if result["status"] == "fail":
                sys.exit(1)
            """
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
