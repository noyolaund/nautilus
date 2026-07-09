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


# Monotonic counter so each PO-by-label call gets a fresh DOM marker.
_PO_LABEL_MARKER_COUNTER = 0


def reset_processing_option_marker_counter() -> None:
    """Reset the per-call marker counter — call at the start of a test run."""
    global _PO_LABEL_MARKER_COUNTER
    _PO_LABEL_MARKER_COUNTER = 0


async def fill_processing_input_by_label(
    page: Page, label_text: str, value: str, iframe: str = IFRAME,
) -> None:
    """Fill the text input adjacent to a Processing Options label.

    Instead of counting inputs (fragile when a tab has sub-options like
    1.1 / 1.2 that reorder the visible-input index), we locate the DOM
    element whose own text matches *label_text* — the full string from
    the Excel "Option Number" column, e.g. ``1.2. Enter User defined code``
    — and target the nearest editable text input inside its row / parent.

    Matching is whitespace-normalized, case-insensitive, and tolerates:
      • exact equality,
      • the target being a substring of the label's rendered text,
      • the target with its leading numeric prefix (``1.2.``) stripped.
    Shortest matching text wins on ties.
    """
    global _PO_LABEL_MARKER_COUNTER
    _PO_LABEL_MARKER_COUNTER += 1
    marker_value = f"po-input-{_PO_LABEL_MARKER_COUNTER}"

    value = (str(value) if value is not None else "").strip()
    if not value:
        return
    label_text = (str(label_text) if label_text is not None else "").strip()
    if not label_text:
        raise RuntimeError("Empty PO label — cannot locate input")

    print(
        f"      [PO-label] call #{_PO_LABEL_MARKER_COUNTER}: "
        f"marker={marker_value!r}, label={label_text!r}"
    )

    # JS finds the label element, then the closest editable input via a
    # short set of strategies (same-row, parent-walk, forward-DOM). It
    # ALSO clears every prior data-jde-po-marker in this document before
    # tagging, so a stale marker from a previous call can't survive to
    # confuse locator.first (this was the real bug — a fixed marker meant
    # every PO landed in the first-ever tagged input).
    js = r"""({ labelText, marker }) => {
        const norm = (s) => (s || '')
            .split(/[\s ]+/)
            .filter(Boolean)
            .join(' ')
            .toLowerCase();
        // Strip leading "1.", "1.2.", "12.3.4." etc. — same on both sides
        // so the caller can supply either "1.2. Enter …" or "Enter …".
        const stripPrefix = (s) => s.replace(/^(\d+\.)+\s*/, '').trim();
        const rawTarget = norm(labelText);
        const stripTarget = norm(stripPrefix(labelText));
        if (!rawTarget && !stripTarget) return { error: 'empty label' };

        // Wipe every prior data-jde-po-marker in the document so a stale
        // marker from a previous PO call can't win locator.first this call.
        const priorMarked = document.querySelectorAll('[data-jde-po-marker]');
        const priorMarkerValues = [];
        for (const el of priorMarked) {
            priorMarkerValues.push(el.getAttribute('data-jde-po-marker'));
            el.removeAttribute('data-jde-po-marker');
        }

        const describeInput = (el) => {
            const r = el.getBoundingClientRect();
            return {
                tag: el.tagName,
                id: el.id || null,
                name: el.getAttribute('name') || null,
                type: el.getAttribute('type') || null,
                value: (el.value || '').slice(0, 40),
                x: Math.round(r.left), y: Math.round(r.top),
                w: Math.round(r.width), h: Math.round(r.height),
            };
        };

        const isEditable = (el) => {
            if (!el || el.tagName !== 'INPUT') return false;
            const type = (el.getAttribute('type') || 'text').toLowerCase();
            if (!['text', 'number', 'tel', ''].includes(type)) return false;
            if (el.disabled || el.readOnly) return false;
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        };
        const findInSubtree = (root) => {
            if (!root) return null;
            const ins = root.querySelectorAll(
                "input[type='text'], input:not([type]), input[type='number']"
            );
            for (const i of ins) if (isEditable(i)) return i;
            return null;
        };

        // Collect elements whose OWN text (not descendants') looks like the label.
        const candidates = [];
        const skipTags = new Set(['INPUT','TEXTAREA','SELECT','OPTION','SCRIPT','STYLE']);
        for (const el of document.querySelectorAll('body *')) {
            if (skipTags.has(el.tagName)) continue;
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) continue;

            // Own text = direct child text nodes only
            let own = '';
            for (const node of el.childNodes) {
                if (node.nodeType === Node.TEXT_NODE) own += node.textContent + ' ';
            }
            const ownNorm = norm(own);
            if (!ownNorm) continue;
            const ownStripped = norm(stripPrefix(own));

            let rank = -1;
            if (ownNorm === rawTarget || ownStripped === stripTarget) rank = 0;      // exact
            else if (rawTarget && ownNorm.includes(rawTarget)) rank = 1;             // raw substr
            else if (stripTarget && ownStripped.includes(stripTarget)) rank = 2;     // stripped substr
            if (rank === -1) continue;
            candidates.push({ el, rank, len: ownNorm.length, text: ownNorm });
        }
        if (candidates.length === 0) {
            return { error: 'no label matched', target: rawTarget, targetStripped: stripTarget };
        }
        // Best rank first, shortest text on ties.
        candidates.sort((a, b) => a.rank - b.rank || a.len - b.len);

        // For each candidate label, look for the nearest editable input:
        //   row → parent-up-3 → following siblings → forward DOM.
        for (const c of candidates) {
            const winner = c.el;
            const strategies = [];

            const row = winner.closest('tr');
            if (row) {
                const hit = findInSubtree(row);
                if (hit) {
                    hit.setAttribute('data-jde-po-marker', marker);
                    return {
                        selector: "[data-jde-po-marker='" + marker + "']",
                        marker: marker, strategy: 'row',
                        matchedText: c.text, matchedRank: c.rank,
                        candidateCount: candidates.length,
                        priorMarkerCount: priorMarked.length,
                        priorMarkerValues: priorMarkerValues,
                        markerCountAfter: document.querySelectorAll(
                            "[data-jde-po-marker='" + marker + "']").length,
                        target: describeInput(hit),
                        labelTag: winner.tagName,
                        labelId: winner.id || null,
                    };
                }
                strategies.push('row-empty');
            }
            let p = winner.parentElement;
            for (let depth = 0; depth < 4 && p; depth++, p = p.parentElement) {
                const hit = findInSubtree(p);
                if (hit) {
                    hit.setAttribute('data-jde-po-marker', marker);
                    return {
                        selector: "[data-jde-po-marker='" + marker + "']",
                        marker: marker, strategy: 'parent-' + depth,
                        matchedText: c.text, matchedRank: c.rank,
                        candidateCount: candidates.length,
                        priorMarkerCount: priorMarked.length,
                        priorMarkerValues: priorMarkerValues,
                        markerCountAfter: document.querySelectorAll(
                            "[data-jde-po-marker='" + marker + "']").length,
                        target: describeInput(hit),
                        labelTag: winner.tagName,
                        labelId: winner.id || null,
                    };
                }
            }
            let n = winner.nextElementSibling;
            while (n) {
                const hit = findInSubtree(n) || (isEditable(n) ? n : null);
                if (hit) {
                    hit.setAttribute('data-jde-po-marker', marker);
                    return {
                        selector: "[data-jde-po-marker='" + marker + "']",
                        marker: marker, strategy: 'sibling',
                        matchedText: c.text, matchedRank: c.rank,
                        candidateCount: candidates.length,
                        priorMarkerCount: priorMarked.length,
                        priorMarkerValues: priorMarkerValues,
                        markerCountAfter: document.querySelectorAll(
                            "[data-jde-po-marker='" + marker + "']").length,
                        target: describeInput(hit),
                        labelTag: winner.tagName,
                        labelId: winner.id || null,
                    };
                }
                n = n.nextElementSibling;
            }
        }
        return { error: 'label found but no editable input near it',
                 target: rawTarget,
                 candidateCount: candidates.length,
                 tried: candidates.slice(0, 5).map(c => ({ text: c.text, rank: c.rank })),
                 priorMarkerCount: priorMarked.length,
                 priorMarkerValues: priorMarkerValues };
    }"""

    def _frame_priority(f):
        name = (f.name or "").lower()
        url = (f.url or "").lower()
        if "e1menuappiframe" in name or "e1menuappiframe" in url:
            return 0
        if "fastpath" in name or "fastpath" in url:
            return 99
        return 50

    selected_frame = None
    marker_selector: Optional[str] = None
    last_error: Optional[str] = None
    for frame in sorted(page.frames, key=_frame_priority):
        try:
            result = await frame.evaluate(
                js, {"labelText": label_text, "marker": marker_value},
            )
        except Exception as exc:
            print(f"      [PO-label] frame [{frame.name!r}] evaluate error: {exc}")
            continue
        if not result:
            continue
        frame_label = frame.name or (frame.url[:40] if frame.url else "main")
        if result.get("error"):
            last_error = result["error"]
            print(
                f"      [PO-label] frame [{frame_label}]: {result['error']} "
                f"(candidates={result.get('candidateCount')}, "
                f"priorMarkers={result.get('priorMarkerCount')}={result.get('priorMarkerValues')!r})"
            )
            for t in (result.get("tried") or []):
                print(f"        tried: {t}")
            continue
        marker_selector = result["selector"]
        selected_frame = frame
        target_info = result.get("target") or {}
        print(
            f"      [PO-label] frame [{frame_label}] MATCH: "
            f"rank={result.get('matchedRank')} strategy={result.get('strategy')!r} "
            f"candidates={result.get('candidateCount')} "
            f"priorMarkersCleared={result.get('priorMarkerCount')}={result.get('priorMarkerValues')!r} "
            f"markerCountAfter={result.get('markerCountAfter')}"
        )
        print(
            f"        label: <{result.get('labelTag')}> id={result.get('labelId')!r} "
            f"text={result.get('matchedText')!r}"
        )
        print(
            f"        input: <{target_info.get('tag')}> id={target_info.get('id')!r} "
            f"name={target_info.get('name')!r} type={target_info.get('type')!r} "
            f"pos=({target_info.get('x')},{target_info.get('y')}) "
            f"size={target_info.get('w')}x{target_info.get('h')} "
            f"currentValue={target_info.get('value')!r}"
        )
        break

    if not selected_frame or not marker_selector:
        raise RuntimeError(
            f"Could not find input for PO label {label_text!r} in any frame"
            + (f" — last error: {last_error}" if last_error else "")
        )

    frame_locator = page.frame_locator(iframe)
    locator = frame_locator.locator(marker_selector)
    resolve_source = "frame_locator"
    try:
        await locator.first.wait_for(state="visible", timeout=5000)
    except Exception:
        locator = selected_frame.locator(marker_selector)
        resolve_source = "selected_frame"
        await locator.first.wait_for(state="visible", timeout=5000)

    # Post-resolve sanity check: how many DOM nodes match the marker in the
    # frame we're about to click, and where does the resolved one live?
    try:
        resolved_count = await selected_frame.evaluate(
            "(sel) => document.querySelectorAll(sel).length", marker_selector,
        )
        resolved_info = await selected_frame.evaluate(
            """(sel) => {
                const el = document.querySelector(sel);
                if (!el) return null;
                const r = el.getBoundingClientRect();
                return { id: el.id || null, name: el.getAttribute('name') || null,
                         x: Math.round(r.left), y: Math.round(r.top),
                         value: (el.value || '').slice(0, 40) };
            }""",
            marker_selector,
        )
        print(
            f"      [PO-label] resolved via {resolve_source}: marker={marker_value!r} "
            f"matches={resolved_count} → {resolved_info}"
        )
    except Exception as exc:
        print(f"      [PO-label] sanity-check evaluate error: {exc}")

    await locator.first.click()
    await page.keyboard.press("Control+a")
    await page.keyboard.press("Delete")
    await locator.first.press_sequentially(value, delay=20)
    await asyncio.sleep(0.2)
    await page.keyboard.press("Tab")
    print(f"      [PO-label] typed {value!r} via label {label_text!r} (marker={marker_value!r})")


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


async def close_open_jde_dialogs(page: Page, label: str = "") -> None:
    """Best-effort cleanup after a Data Selection / Processing Options failure.

    Repeatedly clicks #hc_Select to dismiss any open JDE dialog stack (Literal
    editor → Data Selection → Processing Options) so the next iteration can
    find the Fast Path / batch-version inputs again. All errors are swallowed
    — this runs from an exception handler and must never itself raise.
    """
    tag = f"[{label}] " if label else ""
    for attempt in range(1, 5):
        try:
            frame = page.frame_locator(IFRAME)
            btn = frame.locator("#hc_Select")
            count = await btn.count()
            if count == 0:
                if attempt == 1:
                    print(f"{tag}emergency close: no #hc_Select present, nothing to dismiss")
                return
            print(f"{tag}emergency close attempt {attempt}: clicking #hc_Select")
            await btn.first.click(timeout=3000)
            await asyncio.sleep(1)
        except Exception as exc:
            print(f"{tag}emergency close attempt {attempt} failed: {type(exc).__name__}: {exc}")
            return


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
                    # Close any open dialogs so the next report can start clean.
                    await close_open_jde_dialogs(page, label)
                    return {
                        "status": "fail", "error": str(exc), "report": report,
                        "steps": list(runner.results),
                    }

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

            # Fresh PO block → fresh marker sequence so log-side markers
            # count 1, 2, 3, ... within this iteration.
            reset_processing_option_marker_counter()

            # Track the currently active tab across the loop. Clicking the
            # same tab twice re-renders it and JDE resets any values we've
            # just typed, so we only click when the tab actually changes.
            current_tab_norm: Optional[str] = None

            for idx, po in enumerate(processing_options, 1):
                tab = po.get("tab", "")
                option_number_raw = po.get("option_number", "")
                processing_value = po.get("processing_new", "")
                print(f"[{label}]   PO {idx}: tab={tab!r}, opt={option_number_raw!r}, value={processing_value!r}")

                # option_number may be either a plain integer (legacy "Nth
                # visible input") or a full option label like
                # "1.2. Enter User defined code" — dispatch is done at the
                # fill step below.
                option_ref = str(option_number_raw).strip()
                if not option_ref:
                    print(f"      ✖ Empty option_number, skipping")
                    continue
                option_number = int(option_ref) if option_ref.isdigit() else None

                # 1. Click the tab by name — but only when it's DIFFERENT from
                # the tab we're already on. Re-clicking the current tab
                # restores its prior values (undoing this loop's edits).
                if tab:
                    tab_norm = " ".join(str(tab).split()).strip().lower()
                    if tab_norm == current_tab_norm:
                        print(f"      Tab {tab!r} already active — skipping re-click")
                    else:
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
                        current_tab_norm = tab_norm

                # 2. Fill the option:
                #    - label-based lookup when option_number is a text label
                #      (robust to sub-options like 1.1 / 1.2 shifting the
                #      visible-input index)
                #    - Nth-input lookup when option_number is a plain integer
                #      (legacy sheets)
                if processing_value:
                    if option_number is not None:
                        await fill_nth_processing_input(page, option_number, processing_value)
                    else:
                        await fill_processing_input_by_label(page, option_ref, processing_value)

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
        # Dismiss any open Data Selection / Processing Options dialogs so
        # the next report iteration doesn't inherit a modal that blocks
        # the Fast Path input.
        await close_open_jde_dialogs(page, label)
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
        await close_open_jde_dialogs(page, label)
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
            result = await run_jde_full(page, SAMPLE_REPORT)
            if result["status"] == "fail":
                sys.exit(1)
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
