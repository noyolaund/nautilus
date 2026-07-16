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
import re
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

# Translate Excel comparison operators (Row 5+, Column B) into the visible
# text used by JDE's Comparison dropdown ("is equal to", "is not equal to", ...).
# Keys are lower-cased; unknown values fall through unchanged.
COMPARISON_MAP: dict[str, str] = {
    "equal": "is equal to",
    "equals": "is equal to",
    "=": "is equal to",
    "==": "is equal to",
    "is equal to": "is equal to",
    "not equal": "is not equal to",
    "is not equal": "is not equal to",
    "is not equal to": "is not equal to",
    "!=": "is not equal to",
    "<>": "is not equal to",
    "greater than": "is greater than",
    ">": "is greater than",
    "is greater than": "is greater than",
    "greater than or equal": "is greater than or equal to",
    "greater than or equal to": "is greater than or equal to",
    ">=": "is greater than or equal to",
    "is greater than or equal to": "is greater than or equal to",
    "less than": "is less than",
    "<": "is less than",
    "is less than": "is less than",
    "less than or equal": "is less than or equal to",
    "less than or equal to": "is less than or equal to",
    "<=": "is less than or equal to",
    "is less than or equal to": "is less than or equal to",
    "in list": "is in list",
    "is in list": "is in list",
    "not in list": "is not in list",
    "is not in list": "is not in list",
    "between": "is between",
    "is between": "is between",
    "not between": "is not between",
    "is not between": "is not between",
}


def resolve_comparison(raw: str) -> str:
    """Return the JDE dropdown text for a raw Excel comparison operator.

    Falls back to the raw value (stripped) if no mapping matches — the
    select step will report a clear failure listing the actual options.
    """
    if not raw:
        return "is equal to"
    key = str(raw).strip().lower()
    return COMPARISON_MAP.get(key, str(raw).strip())


# ---------------------------------------------------------------------------
# Login flow (used when running standalone)
# ---------------------------------------------------------------------------

async def find_right_operand_selector(page: Page, left_operand_text: str) -> str:
    """Find the data-selection row whose Left Operand matches *left_operand_text*
    and return the matching '#RightOperand{N}' selector.

    Works for both row layouts:
      - Unlocked row: matches the SELECTED option text of LeftOperand{N}
      - Locked row:   matches the static text in any <td> cell
                      (locked rows don't render a LeftOperand select at all)

    Matching is whitespace-normalized substring (handles \\xa0 NBSP), with a
    Levenshtein fuzzy fallback for typos. Strategy "any-option" (any option
    of any dropdown) is NOT used here because every LeftOperand dropdown
    holds the same 54 field-name options, so it would always match the first
    dropdown — producing wrong row numbers.

    Raises LookupError if nothing matches.
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
        const rowChecks = [];

        // ── Strategy 1: match the SELECTED option of a LeftOperand dropdown.
        //   Two passes to avoid the "Business Unit" vs "Business Unit - Header"
        //   ambiguity — a shorter needle must NOT hijack a longer row that
        //   merely contains it as a prefix.
        //     Pass A (exact):   text === target
        //     Pass B (substring): text.includes(target), prefer the SHORTEST hit
        const selectedTexts = [];
        for (const sel of selects) {
            const opt = sel.options[sel.selectedIndex];
            const text = norm(opt ? opt.textContent : '');
            const score = fuzzyScore(target, text);
            tried.push({ id: sel.id, normalized: text, includes: text.includes(target), score: score.toFixed(2) });
            if (text) selectedTexts.push({ sel: sel, text: text });
        }
        for (const c of selectedTexts) {
            if (c.text === target) {
                const m = c.sel.id.match(/(\\d+)$/);
                return { id: c.sel.id, n: m ? m[1] : null, strategy: "selected-exact", text: c.text, target: target, tried: tried };
            }
        }
        const selectedSubstr = selectedTexts
            .filter(c => c.text.includes(target))
            .sort((a, b) => a.text.length - b.text.length);
        if (selectedSubstr.length > 0) {
            const c = selectedSubstr[0];
            const m = c.sel.id.match(/(\\d+)$/);
            return { id: c.sel.id, n: m ? m[1] : null, strategy: "selected-substr", text: c.text, target: target, tried: tried };
        }

        // ── Strategy 2: row-cell text match (handles LOCKED rows where the
        //   field name is rendered as static <td> text and no LeftOperand{N}
        //   select exists). We walk every #Select{N} checkbox, look at its
        //   <tr>, and match the EFFECTIVE text of each <td>:
        //     - <td> with a <select>: the selected option's text
        //     - <td> with no <select>: its raw textContent
        //   Two passes as above: exact-equality first, then shortest-substring.
        const cellEffectiveText = (td) => {
            const sels = td.querySelectorAll('select');
            if (sels.length > 0) {
                const parts = [];
                for (const sel of sels) {
                    const opt = sel.options[sel.selectedIndex];
                    if (opt) parts.push(opt.textContent || '');
                }
                return norm(parts.join(' '));
            }
            return norm(td.textContent);
        };
        const rowCheckboxes = document.querySelectorAll(
            "input[type='checkbox'][id^='Select']"
        );
        for (const cb of rowCheckboxes) {
            const idMatch = cb.id.match(/^Select(\\d+)$/);
            if (!idMatch) continue;
            const n = idMatch[1];
            const row = cb.closest('tr');
            if (!row) continue;
            const tds = Array.from(row.querySelectorAll('td'));
            const cellTexts = tds.map(td => cellEffectiveText(td));
            rowChecks.push({ n: n, cellTexts: cellTexts });
        }
        const dumpRowChecks = () => rowChecks.map(r => ({
            n: r.n,
            cellTexts: r.cellTexts.map(t => (t || '').slice(0, 60)),
        }));
        // Pass A: exact cell equality
        for (const r of rowChecks) {
            const matchIdx = r.cellTexts.findIndex(t => t && t === target);
            if (matchIdx !== -1) {
                return {
                    id: '#Select' + r.n, n: r.n,
                    strategy: "row-cell-exact(td[" + matchIdx + "])",
                    text: r.cellTexts[matchIdx], target: target,
                    tried: tried, rowChecks: dumpRowChecks(),
                };
            }
        }
        // Pass B: substring cell match — prefer the shortest containing text
        // so "Business Unit" doesn't swallow the "Business Unit - Header" row.
        let bestSubstr = null;
        for (const r of rowChecks) {
            for (let i = 0; i < r.cellTexts.length; i++) {
                const t = r.cellTexts[i];
                if (t && t.includes(target)) {
                    if (!bestSubstr || t.length < bestSubstr.text.length) {
                        bestSubstr = { n: r.n, col: i, text: t };
                    }
                }
            }
        }
        if (bestSubstr) {
            return {
                id: '#Select' + bestSubstr.n, n: bestSubstr.n,
                strategy: "row-cell-substr(td[" + bestSubstr.col + "])",
                text: bestSubstr.text, target: target,
                tried: tried, rowChecks: dumpRowChecks(),
            };
        }

        // Strategy 3: fuzzy match on row cells (handles typos in the Excel
        // value — e.g. "Bussines Unit" vs "Business Unit" — and works for
        // both locked and unlocked rows since we use the same cell-text
        // extraction as Strategy 2). On score ties, prefer the shortest cell
        // text so "Business Unit" wins over "Business Unit - Header".
        let bestScore = 0;
        let bestRowN = null;
        let bestCol = null;
        let bestText = null;
        for (const r of rowChecks) {
            for (let i = 0; i < r.cellTexts.length; i++) {
                const text = r.cellTexts[i];
                if (!text) continue;
                const score = fuzzyScore(target, text);
                const better = (score > bestScore) ||
                    (score === bestScore && bestText != null && text.length < bestText.length);
                if (better) {
                    bestScore = score;
                    bestRowN = r.n;
                    bestCol = i;
                    bestText = text;
                }
            }
        }
        if (bestRowN != null && bestScore >= FUZZY_THRESHOLD) {
            return {
                id: '#Select' + bestRowN,
                n: bestRowN,
                strategy: "fuzzy(" + bestScore.toFixed(2) + ", td[" + bestCol + "])",
                text: bestText,
                target: target,
                tried: tried,
                rowChecks: rowChecks.map(r => ({
                    n: r.n,
                    cellTexts: r.cellTexts.map(t => (t || '').slice(0, 60))
                })),
            };
        }

        return {
            id: null,
            n: null,
            strategy: "no-match",
            target: target,
            tried: tried,
            bestScore: bestScore.toFixed(2),
            rowChecks: rowChecks.map(r => ({
                n: r.n,
                cellTexts: r.cellTexts.map(t => (t || '').slice(0, 60))
            })),
        };
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

            # Match failed in this frame — dump the normalized comparison
            # plus the per-row cell texts so we can see why no row matched.
            if match and match.get("strategy") == "no-match":
                print(
                    f"  ↳ No match in this frame. Normalized target: {match.get('target')!r} "
                    f"(best fuzzy score: {match.get('bestScore')})"
                )
                for t in match.get("tried", []):
                    print(
                        f"      [LeftOperand] {t['id']}: includes={t['includes']} "
                        f"score={t.get('score')} normalized={t['normalized']!r}"
                    )
                for r in match.get("rowChecks", []):
                    print(
                        f"      [Row Select{r['n']}] cells={r['cellTexts']}"
                    )

    raise LookupError(
        f"No LeftOperand* dropdown matched '{left_operand_text}'. "
        f"Check the console output above to see which pages/frames were inspected."
    )


async def is_data_selection_row_locked(page: Page, row_number) -> bool:
    """Check whether the Data Selection row #N is locked.

    Every row has an <img> next to its checkbox:
        <input type="CHECKBOX" id="Select1" ...>
        <img src="/jde/img/Locked1.gif"  ...>     → LOCKED
        <img src="/jde/img/blank.gif"    ...>     → unlocked

    Heuristic:
      - First <img> sibling whose src contains "blank"  → unlocked
      - Any other <img> src                              → LOCKED
      - No <img> at all within 5 siblings                → unlocked (default)

    Dumps every nearby sibling so we can see what's actually there when the
    detection disagrees with reality.
    """
    n = str(row_number).strip()
    if not n:
        return False

    js = """(n) => {
        const cb = document.querySelector('#Select' + n);
        if (!cb) {
            return { found: false, verdict: 'checkbox #Select' + n + ' not found' };
        }

        // Walk up to 5 element siblings after the checkbox and capture details
        const siblings = [];
        let s = cb.nextElementSibling;
        let i = 0;
        while (s && i < 5) {
            siblings.push({
                idx: i,
                tag: s.tagName,
                src: s.getAttribute('src'),
                alt: s.getAttribute('alt'),
                cls: s.getAttribute('class'),
                text: (s.textContent || '').trim().slice(0, 40),
            });
            s = s.nextElementSibling;
            i++;
        }

        const firstImg = siblings.find(x => x.tag === 'IMG');
        let locked = false;
        let verdict;

        if (!firstImg) {
            verdict = 'no <img> within 5 siblings — defaulting to unlocked';
        } else {
            const src = (firstImg.src || '').toLowerCase();
            if (src.includes('blank')) {
                verdict = 'UNLOCKED (blank.gif): src=' + firstImg.src;
                locked = false;
            } else {
                // Non-blank img (Locked1.gif, Locked2.gif, etc.) → locked
                verdict = 'LOCKED (non-blank img): src=' + firstImg.src;
                locked = true;
            }
        }

        return {
            found: true,
            checkboxId: cb.id,
            siblings: siblings,
            firstImg: firstImg || null,
            locked: locked,
            verdict: verdict,
        };
    }"""

    for frame_idx, frame in enumerate(page.frames):
        frame_label = frame.name or (frame.url[:50] if frame.url else "main")
        try:
            result = await frame.evaluate(js, n)
        except Exception:
            continue
        if not result or not result.get("found"):
            continue

        # Verbose diagnostic: show every nearby sibling so we can see what's
        # actually rendered next to the checkbox.
        locked = bool(result.get("locked"))
        icon = "🔒" if locked else "🔓"
        print(f"      🔎 Row #Select{n} found in frame[{frame_idx}] [{frame_label}]")
        print(f"         verdict: {result.get('verdict')}")
        for sib in result.get("siblings") or []:
            print(
                f"         sibling[{sib['idx']}]: <{(sib.get('tag') or '').lower()}> "
                f"src={sib.get('src')!r} alt={sib.get('alt')!r} "
                f"class={sib.get('cls')!r} text={sib.get('text')!r}"
            )
        print(f"      {icon} → {'LOCKED' if locked else 'unlocked'}")
        return locked

    print(f"      ⚠ Row #Select{n} not found in any frame")
    return False


async def unlock_data_selection_row(runner: StepRunner, row_number) -> None:
    """Unlock a Data Selection row so its fields become editable.

    Sequence:
      1. Mark #Select{N} checkbox (selects the row for the Advanced dialog)
      2. Click the "Advanced" link
      3. Toggle the "Locked" checkbox (currently checked → unchecked)
      4. Click OK (#hc_Select) to apply
    """
    n = str(row_number).strip()
    print(f"      🔓 Unlocking row #Select{n} via Advanced dialog")
    await runner.click(
        f"Select{n} checkbox (pre-unlock)",
        selector=f"#Select{n}", iframe=IFRAME, selector_strategy="css",
    )
    await runner.click(
        "Advanced link",
        selector="a[href*='advanced()']", iframe=IFRAME, selector_strategy="css",
    )
    await runner.click(
        "Locked checkbox (toggle off)",
        selector="input[type='checkbox'][name='Locked']",
        iframe=IFRAME, selector_strategy="css",
    )
    await runner.click(
        "Advanced OK button",
        selector="#hc_Select", iframe=IFRAME, selector_strategy="css",
    )


async def lock_data_selection_row(runner: StepRunner, row_number) -> None:
    """Re-lock a Data Selection row after editing.

    Sequence:
      1. Mark #Select{N} checkbox again (selection may have been cleared
         by the previous Apply)
      2. Click the "Advanced" link
      3. Toggle the "Locked" checkbox (currently unchecked → checked)
      4. Click OK (#hc_Select) to apply
    """
    n = str(row_number).strip()
    print(f"      🔒 Re-locking row #Select{n} via Advanced dialog")
    await runner.click(
        f"Select{n} checkbox (pre-lock)",
        selector=f"#Select{n}", iframe=IFRAME, selector_strategy="css",
    )
    await runner.click(
        "Advanced link",
        selector="a[href*='advanced()']", iframe=IFRAME, selector_strategy="css",
    )
    await runner.click(
        "Locked checkbox (toggle on)",
        selector="input[type='checkbox'][name='Locked']",
        iframe=IFRAME, selector_strategy="css",
    )
    await runner.click(
        "Advanced OK button",
        selector="#hc_Select", iframe=IFRAME, selector_strategy="css",
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


# Frames are searched JDE-app-first: the PO input we want (e.g. P01T0) lives
# in e1menuAppIframe, while the left-panel fast-path field sits in its own
# iframe and would otherwise shadow it.
def _po_frame_priority(f) -> int:
    name = (f.name or "").lower()
    url = (f.url or "").lower()
    if "e1menuappiframe" in name or "e1menuappiframe" in url:
        return 0  # JDE app — search here first with NO skip
    if "fastpath" in name or "fastpath" in url:
        return 99  # left-panel — only useful as last resort
    return 50


async def _type_into_marked_input(
    page: Page,
    selected_frame,
    marker_selector: str,
    value: str,
    iframe: str = IFRAME,
    what: str = "",
) -> None:
    """Click, clear and type *value* into the element tagged with
    *marker_selector*, then Tab to commit."""
    locator = page.frame_locator(iframe).locator(marker_selector)
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
    print(f"      Typed {value!r} into {what or marker_selector}")


def _leading_option_number(label: str) -> Optional[int]:
    """Extract a Processing Option's leading number from its label.

    '1. Sales Order Entry (P4210)' → 1;  '5' → 5;  'Order Type' → None.
    """
    m = re.match(r"\s*(\d+)\s*[.)]?", str(label or ""))
    return int(m.group(1)) if m else None


async def find_processing_input_by_label(
    page: Page, label_text: str,
) -> tuple[Any, Optional[str]]:
    """Tag the text box belonging to *label_text* on the active Processing
    Options tab and return (frame, marker_selector), or (None, None).

    JDE renders each option as its label followed by the input, e.g.
        <td>1. Sales Order Entry (P4210)</td><td><input id="..."></td>
    so we find the text node holding the label and take the first usable
    input after it (next siblings, then the following cell(s) of the row).
    The marker is unique per label so concurrent options can't collide.
    """
    label_text = str(label_text or "").strip()
    if not label_text:
        return None, None

    js = """({ labelText, slug }) => {
        const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
        const want = norm(labelText);
        if (!want) return null;
        const SEL = "input[type='text'], input:not([type]), input[type='number'], textarea";
        const usable = (el) => {
            if (!el) return false;
            if (el.disabled || el.readOnly) return false;
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        };
        const take = (el) => {
            el.setAttribute('data-jde-po-label', slug);
            return {
                selector: "[data-jde-po-label='" + slug + "']",
                targetId: el.id || el.name || '(unnamed)',
            };
        };
        // First usable input at or after `start`, scanning next siblings.
        const scan = (start) => {
            let node = start;
            while (node) {
                const input = (node.matches && node.matches(SEL))
                    ? node
                    : (node.querySelector ? node.querySelector(SEL) : null);
                if (usable(input)) return input;
                node = node.nextElementSibling;
            }
            return null;
        };
        // The input belonging to a label element: first look after the label
        // itself, then in the following cell(s) of its table row.
        const inputFor = (parent) => {
            let input = scan(parent.nextElementSibling);
            if (input) return input;
            const cell = parent.closest('td,th');
            if (!cell) return null;
            input = scan(cell.nextElementSibling);
            if (input) return input;
            const row = cell.closest('tr');
            if (row) {
                const rowInput = row.querySelector(SEL);
                if (usable(rowInput)) return rowInput;
            }
            return null;
        };
        // Prefer an exact label match; only fall back to a containing match.
        // ('1. Order Type' must not hijack '11. Order Type Override'.)
        let partial = null;
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
        while (walker.nextNode()) {
            const node = walker.currentNode;
            const text = norm(node.textContent);
            if (!text) continue;
            const isExact = text === want;
            if (!isExact && !text.includes(want)) continue;
            const parent = node.parentElement;
            if (!parent) continue;
            const input = inputFor(parent);
            if (!input) continue;
            if (isExact) return take(input);
            if (!partial) partial = input;
        }
        return partial ? take(partial) : null;
    }"""

    slug = re.sub(r"[^a-z0-9]+", "-", label_text.lower()).strip("-")[:40] or "po"
    for frame in sorted(page.frames, key=_po_frame_priority):
        try:
            result = await frame.evaluate(js, {"labelText": label_text, "slug": slug})
        except Exception:
            continue
        if result and result.get("selector"):
            frame_label = frame.name or frame.url[:40] or "main"
            print(
                f"      Frame [{frame_label}]: matched label {label_text!r} → "
                f"input (id={result.get('targetId')!r})"
            )
            return frame, result["selector"]
    return None, None


async def fill_processing_option(
    page: Page, option_label: str, value: str, iframe: str = IFRAME,
) -> None:
    """Fill the Processing Option identified by *option_label* (column B).

    Locates the field by searching the label text in JDE and filling the
    closest text box. Falls back to the label's leading option number
    ('1. Sales Order Entry (P4210)' → input #1) when the text isn't found.
    """
    value = (str(value) if value is not None else "").strip()
    if not value:
        return

    frame, marker = await find_processing_input_by_label(page, option_label)
    if frame and marker:
        await _type_into_marked_input(
            page, frame, marker, value, iframe, what=f"option {option_label!r}",
        )
        return

    n = _leading_option_number(option_label)
    if n is None:
        raise RuntimeError(
            f"Could not locate Processing Option {option_label!r} by text, and "
            f"it has no leading option number to fall back on"
        )
    print(f"      Label {option_label!r} not found — falling back to input #{n}")
    await fill_nth_processing_input(page, n, value, iframe)


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

    selected_frame = None
    marker_selector = None

    # Re-order the frames so we try the JDE app iframe first (no skip),
    # then fall back to other frames with skip-first behavior.
    ordered_frames = sorted(page.frames, key=_po_frame_priority)

    for frame in ordered_frames:
        # No skip in the JDE app iframe; skip first elsewhere
        skip_first = _po_frame_priority(frame) != 0
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

    await _type_into_marked_input(
        page, selected_frame, marker_selector, value, iframe, what=f"input #{n}",
    )


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


# Left-operand names that use JDE's multi-value literal editor
# (#litList + #LITtfList + #hc950 Add + #hc952 Delete) instead of a single #LITtf.
MULTI_VALUE_LEFT_OPERANDS: set[str] = {
    "order type",
}


def _split_multi_values(raw: str) -> list[str]:
    """Split a semicolon-separated Excel value into a de-duplicated ordered list."""
    seen: set[str] = set()
    out: list[str] = []
    for chunk in str(raw or "").split(";"):
        v = chunk.strip()
        if not v or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


async def _read_lit_list_values(page: Page) -> list[str]:
    """Return the visible text of every <option> currently in #litList."""
    js = """() => {
        const list = document.querySelector('#litList');
        if (!list) return null;
        return Array.from(list.options).map(o => (o.textContent || '').trim());
    }"""
    for frame in page.frames:
        try:
            result = await frame.evaluate(js)
        except Exception:
            continue
        if result is not None:
            return [t for t in result if t]
    return []


async def _select_lit_list_option(page: Page, needle: str) -> bool:
    """Select the <option> in #litList whose text matches *needle* (case-insensitive)
    and fire a change event so JDE registers the selection. Returns True on success."""
    js = """(target) => {
        const list = document.querySelector('#litList');
        if (!list) return false;
        const norm = (s) => (s || '').trim().toLowerCase();
        const want = norm(target);
        for (const opt of list.options) {
            if (norm(opt.textContent) === want) {
                for (const o of list.options) o.selected = false;
                opt.selected = true;
                list.value = opt.value;
                list.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
            }
        }
        return false;
    }"""
    for frame in page.frames:
        try:
            if await frame.evaluate(js, needle):
                return True
        except Exception:
            continue
    return False


async def detect_active_literal_tab(page: Page) -> Optional[str]:
    """Return the visible text of the currently active tab in JDE's Literal
    editor: one of 'Single Value', 'Range of Values', 'List of Values'.

    JDE marks the active tab with the ``ActiveTabLink`` class:
        <a class="ActiveTabLink" href="javascript:ocLitPrompt(2)">List of Values</a>
    """
    js = """() => {
        const active = document.querySelector('a.ActiveTabLink');
        if (!active) return null;
        return (active.textContent || '').trim();
    }"""
    for frame in page.frames:
        try:
            result = await frame.evaluate(js)
        except Exception:
            continue
        if result:
            return result
    return None


async def write_literal_by_active_tab(
    page: Page, runner: StepRunner, value: str,
) -> None:
    """Detect the currently active tab in the Literal editor and write
    *value* using that tab's controls, then click ``#hc_Select`` to commit.

    Tab mapping:
        Single Value    → #LITtf
        Range of Values → #LITtfFrom / #LITtfTo  (Excel value must be 'A-B')
        List of Values  → #LITtfList + #hc950 (Add) / #litList + #hc952 (Del)
                          — reconciled via sync_literal_list_values.
    """
    active = await detect_active_literal_tab(page)
    tab = (active or "").strip().lower()
    print(f"      🏷 Literal editor active tab: {active!r}")

    if "range" in tab:
        raw = str(value or "").strip()
        parts = raw.split("-", 1)
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            raise StepError(
                "Range of Values",
                f"Excel value {raw!r} is not a 'A-B' range",
                None,
            )
        lo, hi = parts[0].strip(), parts[1].strip()
        await fill_jde_field(page, "#LITtfFrom", lo)
        await fill_jde_field(page, "#LITtfTo", hi)
        await runner.click(
            "Select button", selector="#hc_Select",
            iframe=IFRAME, selector_strategy="css",
        )
        return

    if "list" in tab:
        # sync_literal_list_values itself clicks #hc_Select as its final commit.
        await sync_literal_list_values(page, runner, str(value))
        return

    # Default / Single Value (also the safe fallback when the active tab
    # can't be detected — the classic single-literal flow).
    await fill_jde_field(page, "#LITtf", str(value))
    await runner.click(
        "Select button", selector="#hc_Select",
        iframe=IFRAME, selector_strategy="css",
    )


async def sync_literal_list_values(
    page: Page, runner: StepRunner, desired_raw: str,
) -> None:
    """Reconcile JDE's multi-value literal list so #litList ends up containing
    exactly the semicolon-separated values from Excel.

    Sequence, based on the diff between #litList and *desired_raw*:
      • For each value in Excel but missing from #litList → fill #LITtfList,
        click #hc950 (Add).
      • For each value in #litList but not in Excel → select it in #litList,
        click #hc952 (Delete).
      • Finally click #hc_Select to commit the panel.
    """
    desired = _split_multi_values(desired_raw)
    current = await _read_lit_list_values(page)
    desired_norm = {v.lower() for v in desired}
    current_norm = {c.lower() for c in current}

    missing = [v for v in desired if v.lower() not in current_norm]
    extras = [c for c in current if c.lower() not in desired_norm]

    print(
        f"      🔁 litList sync: desired={desired} current={current} "
        f"missing={missing} extras={extras}"
    )

    # Delete extras first so any single-select interactions don't collide
    # with subsequent Add operations.
    for extra in extras:
        if not await _select_lit_list_option(page, extra):
            print(f"      ⚠ Could not select {extra!r} in #litList — skipping delete")
            continue
        await runner.click(
            f"Delete literal {extra!r} (#hc952)",
            selector="#hc952", iframe=IFRAME, selector_strategy="css",
        )

    for value in missing:
        await fill_jde_field(page, "#LITtfList", value)
        await runner.click(
            f"Add literal {value!r} (#hc950)",
            selector="#hc950", iframe=IFRAME, selector_strategy="css",
        )

    await runner.click(
        "Select button (#hc_Select)",
        selector="#hc_Select", iframe=IFRAME, selector_strategy="css",
    )


# Option values that JDE uses for "not set to a real literal" — matching any
# of these against a non-sentinel Excel value should NOT count as a match.
_RIGHT_OPERAND_SENTINELS: set[str] = {"literal", "blank", "zero", "null"}


async def read_right_operand_selected_text(
    page: Page, row_number: str,
) -> Optional[str]:
    """Return the currently-selected value of #RightOperand{N}.

    A Data Selection row that already has a literal keeps the real value in
    the selected option's ``value`` attribute, and its visible text is empty:

        <option selected value="SA,SF,SM,SO,SW,KK,RF"></option>

    So we compare on the ``value`` attribute, not the (often empty) text.
    For an untouched row the selected option is a sentinel ("Literal",
    "Blank", "Zero", "Null"), whose data lives in the text — hence the text
    fallback when ``value`` is empty.

    Returns None if the select cannot be found in any frame.
    """
    js = """(n) => {
        const sel = document.querySelector('#RightOperand' + n);
        if (!sel) return null;
        const opt = sel.options[sel.selectedIndex];
        if (!opt) return null;
        return {
            text: (opt.textContent || '').trim(),
            value: (opt.value || '').trim(),
        };
    }"""
    for frame in page.frames:
        try:
            result = await frame.evaluate(js, row_number)
        except Exception:
            continue
        if result:
            # Authoritative on the option value; fall back to the visible
            # text (sentinel rows carry their label there, not in value).
            return result.get("value") or result.get("text") or ""
    return None


def _tokenize_multi_value(raw: str) -> set[str]:
    """Split a comma/semicolon list into a normalized set for order-insensitive
    comparison (used for multi-value Left Operands like Order Type)."""
    tokens: set[str] = set()
    for chunk in str(raw or "").replace(";", ",").split(","):
        v = chunk.strip().lower()
        if v:
            tokens.add(v)
    return tokens


def _looks_like_value_list(s: str) -> bool:
    """True if the value is a separated list (JDE uses ',', Excel uses ';')."""
    return "," in s or ";" in s


def _collapse_ws(s: str) -> str:
    """Remove all whitespace so spacing never affects a scalar/range compare
    (e.g. '569  -  620' → '569-620')."""
    return "".join(str(s or "").split())


def right_operand_matches_excel(
    current: Optional[str], excel_value: str, is_multi_value: bool,
) -> bool:
    """True if the current JDE literal matches the Excel value.

    Comparison ignores whitespace and list ordering so equivalent values
    that only differ in spacing or separator style are treated as equal:

        'PA,S,SO'    == 'PA; S; SO'     (list, order/separator-insensitive)
        '569-620'    == '569  -  620'   (range, whitespace-insensitive)

    Sentinel current options ("Literal", "Blank", "Zero", "Null") never match
    a non-sentinel Excel value — they mean "no real literal is set yet".
    """
    if current is None:
        return False
    cur = current.strip()
    exp = str(excel_value or "").strip()
    if not cur or not exp:
        return False
    if cur.lower() in _RIGHT_OPERAND_SENTINELS and exp.lower() not in _RIGHT_OPERAND_SENTINELS:
        return False
    # Treat as a value list when flagged (e.g. Order Type) or when either side
    # is separated by ',' / ';' (e.g. Line Type) — compare as normalized sets.
    if is_multi_value or _looks_like_value_list(cur) or _looks_like_value_list(exp):
        return _tokenize_multi_value(cur) == _tokenize_multi_value(exp)
    # Scalar / range: whitespace-insensitive, case-insensitive comparison.
    return _collapse_ws(cur).lower() == _collapse_ws(exp).lower()


async def find_empty_left_operand_row(page: Page) -> Optional[str]:
    """Return the row number of the last empty #LeftOperand{N} dropdown,
    or None if every visible LeftOperand row already has a field selected.

    JDE Data Selection always shows one blank template row at the bottom.
    We pick the highest N whose selected option is empty (empty text/value),
    so new selections are appended to the tail of the list.
    """
    js = """() => {
        const selects = document.querySelectorAll("select[id^='LeftOperand']");
        let winnerN = null;
        for (const sel of selects) {
            const m = sel.id.match(/(\\d+)$/);
            if (!m) continue;
            const n = parseInt(m[1], 10);
            const opt = sel.options[sel.selectedIndex];
            const text = ((opt ? opt.textContent : '') || '').trim();
            const value = (sel.value || '').trim();
            if (!text && !value) {
                if (winnerN === null || n > winnerN) winnerN = n;
            }
        }
        return winnerN;
    }"""

    for frame in page.frames:
        try:
            result = await frame.evaluate(js)
        except Exception:
            continue
        if result is not None:
            return str(result)
    return None


async def add_data_selection_row(
    page: Page,
    runner: StepRunner,
    left_operand_text: str,
    comparison_text: str,
    value: str,
) -> None:
    """Populate the last empty Data Selection row.

    Sequence:
      1. Pick the field name from the last empty #LeftOperand{N}
      2. Pick the comparison operator from the matching #Comparison{N}
         (translated via COMPARISON_MAP)
      3. Change #RightOperand{N} to "Literal", type the value into #LITtf,
         and click #hc_Select to commit
    """
    row_number = await find_empty_left_operand_row(page)
    if not row_number:
        raise StepError(
            "Add data selection row",
            "No empty LeftOperand dropdown found — cannot add a new row",
            None,
        )

    print(
        f"      ➕ Adding new DS row #{row_number}: "
        f"{left_operand_text!r} {comparison_text!r} {value!r}"
    )

    await runner.select(
        f"LeftOperand{row_number}",
        value=left_operand_text,
        selector=f"#LeftOperand{row_number}",
        iframe=IFRAME,
        selector_strategy="css",
    )

    resolved_comparison = resolve_comparison(comparison_text)
    print(f"        Comparison: {comparison_text!r} → {resolved_comparison!r}")
    await runner.select(
        f"Comparison{row_number}",
        value=resolved_comparison,
        selector=f"#Comparison{row_number}",
        iframe=IFRAME,
        selector_strategy="css",
    )

    await runner.select(
        f"RightOperand{row_number}",
        value="Literal",
        selector=f"#RightOperand{row_number}",
        iframe=IFRAME,
        selector_strategy="css",
    )
    await write_literal_by_active_tab(page, runner, str(value))
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
            {"tab": "Versions", "option_label": "1. Sales Order Entry (P4210)",
             "processing_new": "MOD101"},
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
                comparison_value = sel.get("comparison", "")
                print(
                    f"[{label}]   DS {idx}: {left_operand} {comparison_value or '='} {data_value}"
                )

                # Skip blank Excel cells — they mean "no override for this
                # Left Operand in this report column".
                if not str(data_value).strip():
                    print(f"[{label}]   ↳ empty value, skipping")
                    continue

                # On-hold values (Blank / Zero / Null) use a JDE flow that
                # isn't defined yet — skip so they're never written as a
                # literal. (behavior is attached at parse time.)
                if sel.get("behavior") == "on_hold":
                    print(
                        f"[{label}]   ↳ on-hold value {data_value!r} "
                        f"(behavior TBD) — skipping"
                    )
                    continue

                # Values that failed their field's format rule are skipped so
                # a malformed cell never gets edited into JDE.
                if not sel.get("valid", True):
                    print(
                        f"[{label}]   ↳ {sel.get('validation_message') or 'invalid value'}"
                        f" — skipping"
                    )
                    continue

                # Find the matching RightOperand row by scanning all
                # LeftOperand dropdowns for one whose option text contains
                # the user's left_operand value. If nothing matches, add a
                # brand-new row using the LeftOperand + Comparison from Excel.
                try:
                    right_operand_sel = await find_right_operand_selector(page, left_operand)
                except LookupError as exc:
                    print(f"[{label}]   ↳ {exc} — adding as a new row")
                    try:
                        await add_data_selection_row(
                            page, runner, left_operand, comparison_value, data_value,
                        )
                    except Exception as add_exc:
                        print(f"[{label}] ✖ Could not add new DS row: {add_exc}")
                        await page.screenshot(
                            path=f"logs/jde_add_row_fail_{report.get('app_report', 'unknown')}_{idx}.png",
                            full_page=True,
                        )
                        return {"status": "fail", "error": str(add_exc), "report": report}
                    continue

                # Extract the row number from right_operand_sel ("#RightOperand4" → "4")
                # — same number is used for #Select{N} when removing the row.
                import re as _re
                _row_match = _re.search(r"(\d+)$", right_operand_sel)
                row_number = _row_match.group(1) if _row_match else None

                # Detect whether this row is locked. If so, we open the
                # Advanced dialog and toggle the Locked checkbox off before
                # editing, then toggle it back on after (unless we deleted
                # the row, in which case there's nothing to re-lock).
                row_is_locked = False
                if row_number:
                    row_is_locked = await is_data_selection_row_locked(page, row_number)

                # Pre-edit value check: read the currently-selected option of
                # #RightOperand{N} and compare with the Excel value. If they
                # already match, skip to the next data selection — no unlock
                # / edit / re-lock churn needed. REMOVE always goes through.
                is_remove = str(data_value).strip().upper() == "REMOVE"
                if not is_remove and row_number:
                    current_right = await read_right_operand_selected_text(page, row_number)
                    is_multi = left_operand.strip().lower() in MULTI_VALUE_LEFT_OPERANDS
                    if right_operand_matches_excel(current_right, data_value, is_multi):
                        print(
                            f"[{label}]   ↳ current value {current_right!r} already "
                            f"matches Excel {data_value!r} — skipping"
                        )
                        continue
                    print(
                        f"[{label}]   ↳ current JDE value {current_right!r} differs "
                        f"from Excel {data_value!r} — will edit"
                    )

                # Unlock if needed so the next steps can mutate the row.
                if row_is_locked and row_number:
                    await unlock_data_selection_row(runner, row_number)

                # ── REMOVE branch ────────────────────────────────────────
                # If the Excel "New" value (column H) is "REMOVE" (any case),
                # mark the matching row's checkbox and click Delete instead
                # of filling in a Literal value. The row is gone after this,
                # so no re-lock is needed.
                if str(data_value).strip().upper() == "REMOVE":
                    if not row_number:
                        raise StepError(
                            "REMOVE data selection",
                            f"Could not extract row number from {right_operand_sel!r}",
                            None,
                        )
                    select_checkbox = f"#Select{row_number}"
                    print(
                        f"[{label}]   ↳ REMOVE mode: checking {select_checkbox} "
                        f"then clicking #hc952 (Delete)"
                    )
                    await runner.click(
                        f"Select{row_number} checkbox",
                        selector=select_checkbox, iframe=IFRAME, selector_strategy="css",
                    )
                    await runner.click(
                        "Delete button",
                        selector="#hc952", iframe=IFRAME, selector_strategy="css",
                    )
                    await runner.screenshot()
                    continue

                # ── Default branch — add/update a Literal condition ──────
                # Pick "Literal" from the matching right operand dropdown;
                # the write dispatch below uses the active tab in the Literal
                # editor to pick the right control(s): Single Value → #LITtf,
                # Range of Values → #LITtfFrom/#LITtfTo, List of Values →
                # #LITtfList (multi-value reconciliation).
                await runner.select(
                    "Right Operand dropdown",
                    value="Literal",
                    selector=right_operand_sel, iframe=IFRAME, selector_strategy="css"
                )
                await write_literal_by_active_tab(page, runner, str(data_value))
                await runner.screenshot()

                # Restore the lock state — only for the edit branch.
                # (REMOVE already `continue`d above.)
                if row_is_locked and row_number:
                    await lock_data_selection_row(runner, row_number)

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
                # Column B — the option's label text, searched in JDE to find
                # the text box. Legacy rows may carry a bare option number
                # under "option_number" instead.
                option_label = str(
                    po.get("option_label") or po.get("option_number") or ""
                ).strip()
                processing_value = po.get("processing_new", "")
                print(f"[{label}]   PO {idx}: tab={tab!r}, option={option_label!r}, value={processing_value!r}")

                # A blank Excel cell means "do nothing for this option in
                # this report column".
                if not str(processing_value).strip():
                    print(f"[{label}]   ↳ empty value, skipping")
                    continue
                if not option_label:
                    print(f"[{label}]   ↳ no option label, skipping")
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

                # 2. Fill this option's text box — located by searching the
                # label text, falling back to its leading option number.
                await fill_processing_option(page, option_label, processing_value)

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
        {"tab": "Versions", "option_label": "1. Sales Order Entry (P4210)",
         "processing_new": "MOD101"},
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
