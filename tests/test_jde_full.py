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

JDE_URL = os.getenv("JDE_URL", "")
USERNAME = os.getenv("JDE_USERNAME", "")
PASSWORD = os.getenv("JDE_PASSWORD", "")
IFRAME = "iframe#e1menuAppIframe"


# ---------------------------------------------------------------------------
# Login flow (used when running standalone)
# ---------------------------------------------------------------------------

async def find_right_operand_selector(page: Page, left_operand_text: str) -> str:
    """Scan every page (including popups) and every frame for #LeftOperand*
    dropdowns. Return the matching #RightOperandN selector for the row whose
    Left Operand option text contains *left_operand_text*.

    JDE often opens "Data Selection" in a popup window — so we need to walk
    `context.pages` and not just the original page's frames.

    Raises LookupError if no dropdown matches — execution will stop for the
    current iteration so the user can inspect what's on the page.
    """
    if not left_operand_text:
        raise LookupError("Empty left_operand value — cannot determine RightOperand selector")

    needle = left_operand_text.strip().lower()
    print(f"  ↳ Searching for left operand: {left_operand_text!r} (needle={needle!r})")

    # Wait for the Data Selection dialog to finish rendering on the current page
    print(f"  ↳ Waiting for Data Selection dialog to render...")
    await asyncio.sleep(5)

    # JS that returns a diagnostic dump of every LeftOperand* in a frame
    js_inspect = """() => {
        const selects = document.querySelectorAll("select[id^='LeftOperand']");
        const out = [];
        for (const sel of selects) {
            const selectedOpt = sel.options[sel.selectedIndex];
            const allTexts = Array.from(sel.options).map(o => (o.textContent || '').trim()).filter(Boolean);
            out.push({
                id: sel.id,
                selectedIndex: sel.selectedIndex,
                selectedText: (selectedOpt ? selectedOpt.textContent : '').trim(),
                value: sel.value,
                optionCount: sel.options.length,
                firstFiveOptions: allTexts.slice(0, 5),
            });
        }
        return {
            totalSelects: document.querySelectorAll('select').length,
            leftOperands: out,
        };
    }"""

    # JS that searches across both selected option and any option.
    # JDE uses non-breaking spaces ( ) inside option text, so we normalize
    # them to regular spaces and collapse whitespace before substring-matching.
    js_match = """(needle) => {
        // Normalize: split on any whitespace (including U+00A0 NBSP),
        // rejoin with single spaces, lowercase. Avoids any regex-escape pitfalls.
        const norm = (s) => (s || '')
            .split(/[\\s\\u00A0]+/)
            .filter(Boolean)
            .join(' ')
            .toLowerCase();

        // Levenshtein distance — measures edit distance between two strings
        const levenshtein = (a, b) => {
            if (!a.length) return b.length;
            if (!b.length) return a.length;
            const m = []; for (let i = 0; i <= a.length; i++) m.push([i]);
            for (let j = 1; j <= b.length; j++) m[0][j] = j;
            for (let i = 1; i <= a.length; i++) {
                for (let j = 1; j <= b.length; j++) {
                    const cost = a[i-1] === b[j-1] ? 0 : 1;
                    m[i][j] = Math.min(m[i-1][j]+1, m[i][j-1]+1, m[i-1][j-1]+cost);
                }
            }
            return m[a.length][b.length];
        };

        // Compute similarity between target and the leading tokens of text
        // (we only compare the same number of words as the target).
        const fuzzyScore = (target, text) => {
            const tWords = target.split(' ');
            const xWords = text.split(' ').slice(0, tWords.length);
            const tHead = tWords.join(' ');
            const xHead = xWords.join(' ');
            if (!tHead || !xHead) return 0;
            const dist = levenshtein(tHead, xHead);
            const maxLen = Math.max(tHead.length, xHead.length);
            return 1 - dist / maxLen;
        };

        const FUZZY_THRESHOLD = 0.75;  // 75% similarity → accept

        const target = norm(needle);
        const selects = document.querySelectorAll("select[id^='LeftOperand']");
        const tried = [];

        // Strategy 1: exact substring on selected option
        for (const sel of selects) {
            const opt = sel.options[sel.selectedIndex];
            const text = norm(opt ? opt.textContent : '');
            const score = fuzzyScore(target, text);
            tried.push({ id: sel.id, normalized: text, includes: text.includes(target), score: score.toFixed(2) });
            if (text && text.includes(target)) {
                const m = sel.id.match(/(\\d+)$/);
                return { id: sel.id, n: m ? m[1] : null, strategy: "selected", text: text, target: target, tried: tried };
            }
        }

        // Strategy 2: exact substring on any option
        for (const sel of selects) {
            for (const opt of sel.options) {
                const text = norm(opt.textContent);
                if (text && text.includes(target)) {
                    const m = sel.id.match(/(\\d+)$/);
                    return { id: sel.id, n: m ? m[1] : null, strategy: "any-option", text: text, target: target, tried: tried };
                }
            }
        }

        // Strategy 3: fuzzy match on selected option (handles typos like "bussines" vs "business")
        let bestScore = 0;
        let bestSel = null;
        let bestText = null;
        for (const sel of selects) {
            const opt = sel.options[sel.selectedIndex];
            const text = norm(opt ? opt.textContent : '');
            if (!text) continue;
            const score = fuzzyScore(target, text);
            if (score > bestScore) {
                bestScore = score;
                bestSel = sel;
                bestText = text;
            }
        }
        if (bestSel && bestScore >= FUZZY_THRESHOLD) {
            const m = bestSel.id.match(/(\\d+)$/);
            return {
                id: bestSel.id,
                n: m ? m[1] : null,
                strategy: "fuzzy(" + bestScore.toFixed(2) + ")",
                text: bestText,
                target: target,
                tried: tried,
            };
        }

        return { id: null, n: null, strategy: "no-match", target: target, tried: tried, bestScore: bestScore.toFixed(2) };
    }"""

    # Collect ALL pages in the browser context (main + popups + new tabs)
    pages = list(page.context.pages)
    print(f"  ↳ Browser context has {len(pages)} page(s)")
    for i, p in enumerate(pages):
        try:
            print(f"      page[{i}] url={p.url[:100]!r}  title={(await p.title())[:60]!r}")
        except Exception:
            print(f"      page[{i}] (closed or inaccessible)")

    # Walk every page → every frame
    for page_idx, p in enumerate(pages):
        try:
            frames = p.frames
        except Exception:
            print(f"  ↳ page[{page_idx}] inaccessible, skipping")
            continue

        for frame in frames:
            frame_label = frame.name or frame.url[:60] or "main"
            try:
                inspection = await frame.evaluate(js_inspect)
            except Exception as exc:
                print(f"  ↳ page[{page_idx}] frame [{frame_label}]: cannot evaluate ({exc})")
                continue

            total = inspection.get("totalSelects", 0)
            left_ops = inspection.get("leftOperands", [])

            if total == 0:
                # Empty frame, don't clutter the log
                continue

            print(f"  ↳ page[{page_idx}] frame [{frame_label}]: total <select>={total}, LeftOperand*={len(left_ops)}")
            for d in left_ops:
                print(
                    f"      {d['id']}: selectedIndex={d['selectedIndex']} "
                    f"value={d.get('value')!r} optionCount={d['optionCount']} "
                    f"selectedText={d['selectedText']!r}"
                )
                if d.get("firstFiveOptions"):
                    print(f"        first options: {d['firstFiveOptions']}")

            if not left_ops:
                continue

            # Try to match
            try:
                match = await frame.evaluate(js_match, left_operand_text)
            except Exception as exc:
                print(f"  ↳ js_match failed: {exc}")
                match = None

            if match and match.get("n"):
                n = match["n"]
                selector = f"#RightOperand{n}"
                print(
                    f"  ↳ MATCH: page[{page_idx}] frame [{frame_label}] {match['id']} "
                    f"via {match['strategy']} (text: {match['text']!r}) → {selector}"
                )
                return selector

            # Match failed in this frame — dump the normalized comparison so
            # we can see why the substring check returned false
            if match and match.get("strategy") == "no-match":
                print(
                    f"  ↳ No match in this frame. Normalized target: {match.get('target')!r} "
                    f"(best fuzzy score: {match.get('bestScore')})"
                )
                for t in match.get("tried", []):
                    print(
                        f"      {t['id']}: includes={t['includes']} score={t.get('score')} "
                        f"normalized={t['normalized']!r}"
                    )

    raise LookupError(
        f"No LeftOperand* dropdown matched '{left_operand_text}'. "
        f"Check the console output above to see which pages/frames were inspected."
    )


async def find_processing_option_tab(page: Page, tab_name: str) -> Optional[str]:
    """Find a Processing Options tab anchor by its visible text.

    JDE Processing Options tabs are rendered as:
        <a tabindex="-1" class="ActiveTabLink" href="javascript:onClick=ocPO('X')">Tax Report</a>

    where X is the tab number starting from 0. We match by the anchor's
    text content (whitespace + case-insensitive), tag the element so
    Playwright can target it, and return the selector.
    """
    js = """(needle) => {
        const norm = (s) => (s || '')
            .split(/[\\s\\u00A0]+/)
            .filter(Boolean)
            .join(' ')
            .toLowerCase();
        const target = norm(needle);
        if (!target) return null;

        // Primary: anchors used for JDE Processing Options tabs.
        // Inactive tab class is usually "TabLink"; active is "ActiveTabLink".
        const anchors = document.querySelectorAll(
            "a.ActiveTabLink, a.TabLink, a[class*='TabLink']"
        );

        const candidates = [];
        for (const a of anchors) {
            const text = norm(a.textContent);
            if (!text || !text.includes(target)) continue;
            const rect = a.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) continue;
            // Extract the tab number from href like javascript:onClick=ocPO('3')
            const hrefMatch = (a.getAttribute('href') || '').match(/ocPO\\(['\\"]?(\\d+)['\\"]?\\)/);
            const tabNumber = hrefMatch ? hrefMatch[1] : null;
            candidates.push({ el: a, tabNumber: tabNumber, text: text });
        }

        if (candidates.length === 0) return null;

        // Prefer the one whose text most closely matches the target.
        // If multiple match, pick the shortest (closest match).
        candidates.sort((a, b) => a.text.length - b.text.length);
        const winner = candidates[0];
        const a = winner.el;

        if (!a.id) {
            const slug = 'po-tab-' + target.replace(/\\s+/g, '-');
            a.setAttribute('data-jde-tab-marker', slug);
            return {
                selector: "[data-jde-tab-marker='" + slug + "']",
                tabNumber: winner.tabNumber,
                text: winner.text,
            };
        }
        return { selector: '#' + a.id, tabNumber: winner.tabNumber, text: winner.text };
    }"""

    for frame in page.frames:
        try:
            result = await frame.evaluate(js, tab_name)
        except Exception:
            continue
        if not result or not result.get("selector"):
            continue
        n = result.get("tabNumber")
        if n is not None:
            print(f"      Tab {tab_name!r} → tab #{n} (text: {result.get('text')!r})")
        return result["selector"]
    return None


async def fill_nth_processing_input(
    page: Page, n: int, value: str, iframe: str = IFRAME
) -> None:
    """Fill the Nth visible text input on the currently active Processing
    Options tab (1-indexed)."""
    value = (str(value) if value is not None else "").strip()
    if not value:
        return

    # JS that finds visible text inputs and returns a marker for the Nth one.
    #
    # Per-frame skipping: only skip the first input in frames that look like
    # they host the left-panel fast-path field (not the JDE app frame). The
    # PO input we want (e.g. P01T0) lives in e1menuAppIframe.
    js = """({ n, skipFirst }) => {
        const inputs = document.querySelectorAll(
            "input[type='text'], input:not([type]), input[type='number']"
        );
        const visible = [];
        for (const el of inputs) {
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) continue;
            if (el.disabled || el.readOnly) continue;
            visible.push(el);
        }

        const skipped = (skipFirst && visible.length > 0) ? visible[0] : null;
        const usable = skipFirst ? visible.slice(1) : visible;

        if (n < 1 || n > usable.length) {
            return {
                error: 'No input #' + n + ' (found ' + usable.length + ' usable, ' + visible.length + ' total)',
                total: usable.length,
                visibleTotal: visible.length,
                skipped: skipped ? (skipped.id || skipped.name || '(unnamed)') : null,
                allIds: visible.map(el => el.id || el.name || '(unnamed)'),
            };
        }
        const target = usable[n - 1];
        target.setAttribute('data-jde-po-marker', 'po-input-' + n);
        return {
            selector: "[data-jde-po-marker='po-input-" + n + "']",
            total: usable.length,
            visibleTotal: visible.length,
            skipped: skipped ? (skipped.id || skipped.name || '(unnamed)') : null,
            targetId: target.id || target.name || '(unnamed)',
        };
    }"""

    frame_locator = page.frame_locator(iframe)
    selected_frame = None
    marker_selector = None

    # Re-order the frames so we try the JDE app iframe first (no skip),
    # then fall back to other frames with skip-first behavior. The PO input
    # we want lives in e1menuAppIframe; the fast-path field is in its own
    # iframe (E1MFastpathHiddenIframe / SilentOCLIFrame / etc.).
    def _frame_priority(f):
        name = (f.name or "").lower()
        url = (f.url or "").lower()
        if "e1menuappiframe" in name or "e1menuappiframe" in url:
            return 0  # JDE app — search here first with NO skip
        if "fastpath" in name or "fastpath" in url:
            return 99  # left-panel — only useful as last resort
        return 50

    ordered_frames = sorted(page.frames, key=_frame_priority)

    for frame in ordered_frames:
        # No skip in the JDE app iframe; skip first elsewhere
        skip_first = _frame_priority(frame) != 0
        try:
            result = await frame.evaluate(js, {"n": n, "skipFirst": skip_first})
        except Exception:
            continue
        if not result:
            continue
        frame_label = frame.name or frame.url[:40] or "main"
        if result.get("error"):
            ids = result.get("allIds") or []
            print(
                f"      Frame [{frame_label}] skip_first={skip_first}: {result['error']}"
                + (f" — ids: {ids}" if ids else "")
            )
            continue
        marker_selector = result["selector"]
        selected_frame = frame
        skipped_name = result.get("skipped")
        target_id = result.get("targetId")
        print(
            f"      Frame [{frame_label}] skip_first={skip_first}: "
            f"{result['visibleTotal']} visible, "
            f"skipped={skipped_name!r}, targeting #{n} (id={target_id!r})"
        )
        break

    if not selected_frame or not marker_selector:
        raise RuntimeError(f"Could not find input #{n} in any frame")

    # Use the marker selector from the iframe we tagged
    locator = frame_locator.locator(marker_selector)
    try:
        await locator.first.wait_for(state="visible", timeout=5000)
    except Exception:
        # Fallback: locate directly on the matched frame
        locator = selected_frame.locator(marker_selector)
        await locator.first.wait_for(state="visible", timeout=5000)

    await locator.first.click()
    await page.keyboard.press("Control+a")
    await page.keyboard.press("Delete")
    await locator.first.press_sequentially(value, delay=20)
    await asyncio.sleep(0.2)
    await page.keyboard.press("Tab")
    print(f"      Typed {value!r} into input #{n}")


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
    await runner.navigate(JDE_URL)
    await runner.type("the User ID field", value=USERNAME, sensitive=True)
    await runner.type("the Password field", value=PASSWORD, sensitive=True)
    await runner.click("the Sign In button")
    await runner.assert_visible("Welcome!")
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

        await runner.type(
            "Fast Path input",
            value="bv",
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
            selector="input[name='qbe0_1.1']", iframe=IFRAME, selector_strategy="css"
        )
        await runner.key_press("Enter")

        # ── Select & Copy ───────────────────────────────────────────────
        await runner.click("Select All checkbox", selector="#selectAll0_1", iframe=IFRAME, selector_strategy="css")
        await runner.click("Copy button", selector="#hc_Copy", iframe=IFRAME, selector_strategy="css")

        # ── Fill new version ────────────────────────────────────────────
        await runner.type(
            "New Version field",
            value=report["new_version"],
            selector="#C0_17", iframe=IFRAME, selector_strategy="css"
        )
        await runner.type(
            "New Version Title",
            value=report.get("new_version_title", ""),
            selector="#C0_21", iframe=IFRAME, selector_strategy="css"
        )

        # ── Check for errors (e.g. version already exists) ──────────────
        await runner.check_error("#INYFEContent")

        # ── Click OK ────────────────────────────────────────────────────
        await runner.click("OK button", selector="#hc_OK", iframe=IFRAME, selector_strategy="css")

        # ── Search new version ──────────────────────────────────────────
        await runner.type(
            "version QBE filter",
            value=report["new_version"],
            selector="input[name='qbe0_1.1']", iframe=IFRAME, selector_strategy="css"
        )
        await runner.key_press("Enter")
        await runner.click("Select All checkbox", selector="#selectAll0_1", iframe=IFRAME, selector_strategy="css")

        # ── Data Selection — loop once per entry ────────────────────────
        if data_selections:
            print(f"[{label}] Configuring {len(data_selections)} data selection(s)")
            await runner.click("Row Menu", selector="#C0_58", iframe=IFRAME, selector_strategy="css")
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
            await runner.click("Row Menu", selector="#C0_58", iframe=IFRAME, selector_strategy="css")
            await runner.click("Processing Options", selector="#HE0_118", iframe=IFRAME, selector_strategy="css")
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
        return {"status": "pass", "error": None, "report": report}

    except StepError as e:
        # Step-level failure (element not found, timeout, etc.) — stop this iteration,
        # let the caller (dashboard) move on to the next one.
        print(f"[{label}] ✖ FAILED: {e}")
        try:
            await page.screenshot(path=f"logs/jde_full_error_{report.get('app_report', 'unknown')}.png", full_page=True)
        except Exception:
            pass
        return {"status": "fail", "error": str(e), "report": report}
    except Exception as e:
        # Anything unexpected — still don't crash the outer iteration loop
        import traceback
        print(f"[{label}] ✖ UNEXPECTED ERROR: {type(e).__name__}: {e}")
        traceback.print_exc()
        try:
            await page.screenshot(path=f"logs/jde_full_unexpected_{report.get('app_report', 'unknown')}.png", full_page=True)
        except Exception:
            pass
        return {"status": "fail", "error": f"{type(e).__name__}: {e}", "report": report}


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
            result = await run_jde_full(page, SAMPLE_REPORT)
            if result["status"] == "fail":
                sys.exit(1)
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
