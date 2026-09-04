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
import difflib
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

# Settle time after each dropdown select while adding a new Data Selection row.
# Selecting LeftOperand/Comparison fires onchange="FilterRightOp(N)", which
# re-renders the downstream cells; without a pause the next select can time out
# locating an element that JDE is still repainting.
_ADD_ROW_SETTLE_S = float(os.getenv("JDE_ADD_ROW_SETTLE_S", "1.5"))

# Settle after clicking a Literal editor tab so its controls render before we
# type into them.
_LITERAL_TAB_SETTLE_S = float(os.getenv("JDE_LITERAL_TAB_SETTLE_S", "0.8"))


def _run_screenshot_path(name: str) -> str:
    """Resolve an error-screenshot path inside the current run's screenshots
    folder (JDE_RUN_DIR, set by the dashboard's SessionManager), falling back
    to the logs root when running standalone."""
    safe = re.sub(r"[^\w\-.]", "_", name)
    run_dir = os.getenv("JDE_RUN_DIR", "").strip()
    base = Path(run_dir) / "screenshots" if run_dir else Path(os.getenv("LOG_DIR", "logs"))
    base.mkdir(parents=True, exist_ok=True)
    return str(base / f"{safe}.png")


async def _diagnostic_screenshot(page, name: str) -> None:
    """Capture a full-page screenshot into the run's screenshots folder.

    Best-effort and taken ONLY when an error or warning occurs — the success
    path takes no screenshots, which noticeably cuts execution time."""
    try:
        await page.screenshot(path=_run_screenshot_path(name), full_page=True)
    except Exception:
        pass


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

def _norm_ws(s: str) -> str:
    """Collapse all whitespace (including NBSP) to single spaces and trim."""
    return " ".join(str(s or "").split())


# Enumerate the Left Operand column of JDE's Data Selection grid, row by row.
#
# Grid layout (#jdeGrid): column 1 is "Operator" (And/Or), column 2 is the
# Left Operand. Rows come in two forms:
#   - Unlocked: the Left Operand cell holds a <select id="LeftOperandN">; the
#     display name is the selected option's `displayvalue` attribute.
#   - Locked:   the Left Operand cell holds a <span class="StaticText">. The
#     Operator column also uses StaticText, so we take the span in the Left
#     Operand column (detected from an unlocked row's select column, falling
#     back to the 2nd StaticText in the row).
# Row number N comes from the row's #SelectN checkbox (present in every row).
_JS_LIST_LEFT_OPERANDS = """() => {
    const scope = document.querySelector('#jdeGrid') || document;
    const boxes = scope.querySelectorAll("input[type='checkbox'][id^='Select']");
    if (!boxes.length) return null;
    const norm = (s) => (s || '').split(/[\\s\\u00A0]+/).filter(Boolean).join(' ');

    // Detect the Left Operand column index from any unlocked row's select.
    let leftCol = -1;
    for (const cb of boxes) {
        const tr = cb.closest('tr'); if (!tr) continue;
        const sel = tr.querySelector("select[id^='LeftOperand']");
        if (sel) {
            const td = sel.closest('td');
            if (td) { leftCol = Array.prototype.indexOf.call(tr.children, td); break; }
        }
    }

    const rows = [];
    for (const cb of boxes) {
        const m = cb.id.match(/^Select(\\d+)$/); if (!m) continue;
        const tr = cb.closest('tr'); if (!tr) continue;
        let value = '', locked = false, source = '';
        const sel = tr.querySelector("select[id^='LeftOperand']");
        if (sel) {
            const opt = sel.options[sel.selectedIndex];
            value = opt ? (opt.getAttribute('displayvalue') || opt.textContent || '') : '';
            locked = false; source = 'select';
        } else {
            locked = true;
            const cell = (leftCol >= 0 && tr.children[leftCol]) ? tr.children[leftCol] : null;
            if (cell) {
                const span = cell.querySelector('span.StaticText, .StaticText');
                value = (span ? span.textContent : cell.textContent) || '';
                source = 'grid-col';
            } else {
                const st = tr.querySelectorAll('span.StaticText, .StaticText');
                if (st.length >= 2) { value = st[1].textContent || ''; source = 'static-2nd'; }
                else if (st.length === 1) { value = st[0].textContent || ''; source = 'static-1st'; }
            }
        }
        rows.push({ n: m[1], value: norm(value), locked: locked, source: source });
    }
    return { leftCol: leftCol, rows: rows, usedGrid: !!document.querySelector('#jdeGrid') };
}"""


async def verify_data_selection_dialog_open(
    page: Page, attempts: int = 2, delay_s: float = 4.0,
) -> bool:
    """Confirm the Data Selection dialog actually opened.

    JDE can be slow to render the dialog after the option is clicked, so on
    each of *attempts* tries we wait *delay_s* seconds and then look, across
    every frame, for ``#jdeFormTitle`` whose title (or text) is
    "Data Selection". Returns True as soon as it's found, False if all attempts
    are exhausted.
    """
    js = """() => {
        const el = document.querySelector('#jdeFormTitle');
        if (!el) return null;
        return (el.getAttribute('title') || el.textContent || '').trim();
    }"""
    for attempt in range(1, attempts + 1):
        await asyncio.sleep(delay_s)
        for frame in page.frames:
            try:
                title = await frame.evaluate(js)
            except Exception:
                continue
            if title and title.strip().lower() == "data selection":
                print(f"      ✓ Data Selection dialog confirmed (attempt {attempt})")
                return True
        print(
            f"      ⏳ Data Selection dialog not ready "
            f"(attempt {attempt}/{attempts}, #jdeFormTitle title!='Data Selection')"
        )
    return False


async def list_left_operands(
    page: Page, settle_ms: int = 0,
) -> tuple[list[dict], Optional[str]]:
    """Enumerate the Left Operand column of #jdeGrid (locked + unlocked) row by
    row and log the complete list. Returns (rows, frame_label) where rows is
    ``[{n, value, locked, source}]`` in document (top-to-bottom) order.

    *settle_ms* waits before enumerating — use a few seconds the first time the
    Data Selection dialog opens (it renders asynchronously), a short settle
    after a row is added/removed, and 0 when the DOM is known to be ready.
    """
    if settle_ms > 0:
        await asyncio.sleep(settle_ms / 1000)
    for p in list(page.context.pages):
        try:
            frames = p.frames
        except Exception:
            continue
        for frame in frames:
            try:
                res = await frame.evaluate(_JS_LIST_LEFT_OPERANDS)
            except Exception:
                continue
            if not res or not res.get("rows"):
                continue
            rows = res["rows"]
            frame_label = frame.name or frame.url[:60] or "main"
            print(
                f"  ↳ Left Operand column "
                f"({'#jdeGrid' if res.get('usedGrid') else 'document'}, "
                f"leftCol={res.get('leftCol')}) in frame [{frame_label}]: {len(rows)} row(s)"
            )
            for r in rows:
                print(
                    f"      row {r['n']}: {r['value']!r} "
                    f"(locked={r['locked']}, via {r['source']})"
                )
            return rows, frame_label
    return [], None


# Enumerate every Data Selection row's Left Operand, Comparison and Right
# Operand. Columns in #jdeGrid: Operator (1), Left Operand (2), Comparison (3),
# Right Operand (4). Unlocked cells are <select>s (Left/Comparison show the
# selected option's displayvalue/text; Right Operand keeps the real literal in
# the option's `value`, sentinels in the text); locked cells are StaticText.
_JS_LIST_DS_ROWS = """() => {
    const scope = document.querySelector('#jdeGrid') || document;
    const boxes = scope.querySelectorAll("input[type='checkbox'][id^='Select']");
    if (!boxes.length) return null;
    const norm = (s) => (s || '').split(/[\\s\\u00A0]+/).filter(Boolean).join(' ');

    let leftCol = -1;
    for (const cb of boxes) {
        const tr = cb.closest('tr'); if (!tr) continue;
        const sel = tr.querySelector("select[id^='LeftOperand']");
        if (sel) { const td = sel.closest('td');
            if (td) { leftCol = Array.prototype.indexOf.call(tr.children, td); break; } }
    }
    const compCol = leftCol >= 0 ? leftCol + 1 : -1;
    const rightCol = leftCol >= 0 ? leftCol + 2 : -1;

    const optDisplay = (sel) => {
        const o = sel && sel.options[sel.selectedIndex];
        return o ? (o.getAttribute('displayvalue') || o.textContent || '') : '';
    };
    const optRight = (sel) => {  // real literal lives in value; sentinel in text
        const o = sel && sel.options[sel.selectedIndex];
        return o ? ((o.value || '').trim() || o.textContent || '') : '';
    };
    const cellStatic = (tr, col) => {
        if (col < 0 || !tr.children[col]) return '';
        const cell = tr.children[col];
        const span = cell.querySelector('span.StaticText, .StaticText');
        return (span ? span.textContent : cell.textContent) || '';
    };

    const rows = [];
    for (const cb of boxes) {
        const m = cb.id.match(/^Select(\\d+)$/); if (!m) continue;
        const tr = cb.closest('tr'); if (!tr) continue;
        const lo = tr.querySelector("select[id^='LeftOperand']");
        const co = tr.querySelector("select[id^='Comparison']");
        const ro = tr.querySelector("select[id^='RightOperand']");
        rows.push({
            n: m[1],
            left: norm(lo ? optDisplay(lo) : cellStatic(tr, leftCol)),
            comparison: norm(co ? optDisplay(co) : cellStatic(tr, compCol)),
            right: norm(ro ? optRight(ro) : cellStatic(tr, rightCol)),
            locked: !lo,
        });
    }
    return { rows: rows };
}"""


async def enumerate_data_selection_rows(page: Page) -> list[dict]:
    """Return every Data Selection grid row as
    ``[{n, left, comparison, right, locked}]`` (document order), reading Left
    Operand, Comparison and Right Operand for both locked and unlocked rows."""
    for frame in page.frames:
        try:
            res = await frame.evaluate(_JS_LIST_DS_ROWS)
        except Exception:
            continue
        if res and res.get("rows"):
            return res["rows"]
    return []


def _clean_operand_for_match(value: str) -> str:
    """Normalize a Left Operand name for matching by dropping the parenthesized
    and bracketed qualifiers JDE appends — data item description, table, alias,
    and section tag — then lower-casing:

        'Order Company (Order Number) (F4211) (KCOO) [BC]' → 'order company'

    This makes the enumerated grid value comparable to the cleaned Excel value,
    and groups duplicates that differ only by table (F4201 vs F4211).
    """
    s = re.sub(r"\([^)]*\)", " ", str(value or ""))
    s = re.sub(r"\[[^\]]*\]", " ", s)
    return _norm_ws(s).lower()


def _match_left_operand_row(
    rows: list[dict], needle: str, occurrence: int,
) -> Optional[dict]:
    """Return the *occurrence*-th row (1-based, document order) whose Left
    Operand matches *needle*.

    Both the grid value and the needle are normalized with
    _clean_operand_for_match (parenthesized/bracketed qualifiers stripped), so
    the same field from different tables (F4201 vs F4211) groups together.
    Exact matches are preferred; otherwise the shortest-containing group is
    used so a shorter needle can't hijack a longer row ("Business Unit" vs
    "Business Unit - Header"). Returns None if fewer than *occurrence* rows
    match.
    """
    key = _clean_operand_for_match(needle)

    def norm(r: dict) -> str:
        return _clean_operand_for_match(r["value"])

    exact = [r for r in rows if norm(r) == key]
    group = exact
    if not group and key:
        subs = [r for r in rows if key in norm(r)]
        if subs:
            shortest_len = min(len(norm(r)) for r in subs)
            keep = next(norm(r) for r in subs if len(norm(r)) == shortest_len)
            group = [r for r in subs if norm(r) == keep]
    if len(group) >= occurrence:
        return group[occurrence - 1]
    return None


async def find_right_operand_selector(
    page: Page, left_operand_text: str, occurrence: int = 1,
    rows: Optional[list[dict]] = None,
) -> str:
    """Find the Data Selection row whose Left Operand matches *left_operand_text*
    and return its '#RightOperand{N}' selector.

    *rows* is a pre-fetched Left Operand enumeration (from list_left_operands),
    reused across the whole Data Selection loop so the grid is scanned once
    rather than per field. When omitted, the grid is enumerated on demand
    (with the initial render wait) — used by standalone callers.

    *occurrence* (1-based) selects among duplicated Left Operands — the Nth
    matching row in document order.

    Raises LookupError if nothing matches (which routes to the add-new-row
    path in the caller).
    """
    if not left_operand_text:
        raise LookupError("Empty left_operand value — cannot determine RightOperand selector")

    needle = _norm_ws(left_operand_text).lower()
    print(
        f"  ↳ Searching for left operand: {left_operand_text!r} "
        f"(needle={needle!r}, occurrence={occurrence})"
    )

    if rows is None:
        # No cached enumeration — scan now (wait for the dialog to render).
        rows, _frame = await list_left_operands(page, settle_ms=5000)
    if not rows:
        raise LookupError(
            f"No Data Selection rows found (#jdeGrid) — cannot locate "
            f"'{left_operand_text}'"
        )

    match = _match_left_operand_row(rows, needle, occurrence)
    if match is None:
        raise LookupError(
            f"No Left Operand matched '{left_operand_text}' (occurrence "
            f"{occurrence}). Available: {[r['value'] for r in rows]}"
        )

    selector = f"#RightOperand{match['n']}"
    print(
        f"  ↳ MATCH occurrence #{occurrence}: row {match['n']} "
        f"(value={match['value']!r}, locked={match['locked']}) → {selector}"
    )
    return selector


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
    # The Advanced OK reloads the grid frame; give it time to re-render before
    # the next field touches #Select{N} (otherwise the selector isn't found yet).
    await asyncio.sleep(0.5)


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


# Some JDE versions render the Processing Options tabs as a combo box instead
# of clickable anchor tabs. This is its id.
PO_TAB_COMBO_SELECTOR = "#jdeWebTabBodynull"


async def find_po_tab_combo_option(
    page: Page, tab_name: str,
) -> tuple[str, Optional[str], Optional[list]]:
    """Detect whether the Processing Options tabs are a combo box
    (#jdeWebTabBodynull) and, if so, resolve the option matching *tab_name*.

    Returns (status, label, options):
        "not_present" → combo absent in every frame → use the anchor-tab click
        "matched"     → *label* is the exact option text to select
        "no_match"    → combo present but no option matches; *options* lists
                        the available tab labels
    """
    js = """(needle) => {
        const norm = (s) => (s || '')
            .split(/[\\s\\u00A0]+/).filter(Boolean).join(' ').toLowerCase();
        const sel = document.querySelector('#jdeWebTabBodynull');
        if (!sel) return { status: 'not_present' };
        const opts = Array.from(sel.options || []);
        const labels = opts.map(o => (o.textContent || '').trim()).filter(Boolean);
        const target = norm(needle);
        if (!target) return { status: 'no_match', options: labels };
        // Prefer an exact normalized match, then a containing match.
        let hit = opts.find(o => norm(o.textContent) === target)
               || opts.find(o => norm(o.textContent).includes(target));
        if (!hit) return { status: 'no_match', options: labels };
        return { status: 'matched', label: (hit.textContent || '').trim() };
    }"""
    for frame in page.frames:
        try:
            result = await frame.evaluate(js, tab_name)
        except Exception:
            continue
        if not result:
            continue
        status = result.get("status")
        if status == "not_present":
            continue  # combo not in this frame — keep looking
        return status, result.get("label"), result.get("options")
    return "not_present", None, None


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


async def activate_po_tab(page: Page, runner: StepRunner, tab: str) -> None:
    """Activate the Processing Options tab named *tab*, whether JDE renders the
    tabs as a combo box (#jdeWebTabBodynull) or clickable anchor tabs. Raises
    StepError if the tab can't be found."""
    combo_status, combo_label, combo_opts = await find_po_tab_combo_option(page, tab)
    if combo_status == "matched":
        print(f"      Tab {tab!r} → combo box {PO_TAB_COMBO_SELECTOR} option {combo_label!r}")
        await runner.select(
            f"Processing Options tab {tab!r}",
            value=combo_label,
            selector=PO_TAB_COMBO_SELECTOR, iframe=IFRAME, selector_strategy="css",
        )
    elif combo_status == "no_match":
        raise StepError(
            "Select Processing Options tab",
            f"Tab {tab!r} not found in combo box {PO_TAB_COMBO_SELECTOR}; "
            f"available: {combo_opts}",
            None,
        )
    else:  # not_present → classic clickable anchor tabs
        tab_selector = await find_processing_option_tab(page, tab)
        if not tab_selector:
            raise StepError(
                "Find Processing Options tab", f"Could not find tab named {tab!r}", None,
            )
        print(f"      Tab matched: {tab_selector}")
        await runner.click(
            f"Tab {tab!r}", selector=tab_selector, iframe=IFRAME, selector_strategy="css",
        )


async def read_processing_inputs(page: Page) -> list[str]:
    """Return the values of the visible/enabled text inputs on the active
    Processing Options tab, in order (0-based positions) — the same set
    fill_nth_processing_input targets, so index i here is text box i+1 there."""
    js = """({ skipFirst }) => {
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
        const usable = skipFirst ? visible.slice(1) : visible;
        return usable.map(el => el.value || '');
    }"""
    for frame in sorted(page.frames, key=_po_frame_priority):
        skip_first = _po_frame_priority(frame) != 0
        try:
            res = await frame.evaluate(js, {"skipFirst": skip_first})
        except Exception:
            continue
        if res:
            return res
    return []


async def verify_and_fix_po_tab(
    page: Page, runner: StepRunner, tab: str, entries: list[dict],
) -> list[str]:
    """Verify the active tab's text boxes match the Excel values (by position)
    and re-fill any mismatch, retrying until equal or passes run out.

    Returns the remaining discrepancy messages (empty if fully reconciled)."""
    expected = {
        int(po.get("position", 0)): str(po.get("processing_new", "")).strip()
        for po in entries if str(po.get("processing_new", "")).strip()
    }
    MAX_PASSES = 4
    for _pass in range(1, MAX_PASSES + 1):
        inputs = await read_processing_inputs(page)
        mismatches = [
            (pos, exp, (inputs[pos] if pos < len(inputs) else None))
            for pos, exp in sorted(expected.items())
            if pos >= len(inputs) or _norm_ws(inputs[pos]) != _norm_ws(exp)
        ]
        if not mismatches:
            print(f"      ✓ PO tab {tab!r} verified ({len(expected)} field(s))")
            return []
        print(
            f"      🔁 PO tab {tab!r} pass {_pass}/{MAX_PASSES}: "
            f"{len(mismatches)} mismatch(es), re-filling"
        )
        for pos, exp, got in mismatches:
            if pos >= len(inputs):
                continue  # no text box at this position — can't fill
            try:
                await fill_nth_processing_input(page, pos + 1, exp)
            except Exception as exc:
                print(f"      ⚠ could not re-fill text box #{pos + 1}: {exc}")

    inputs = await read_processing_inputs(page)
    diffs = []
    for pos, exp in sorted(expected.items()):
        got = inputs[pos] if pos < len(inputs) else None
        if got is None or _norm_ws(got) != _norm_ws(exp):
            diffs.append(f"tab {tab!r} text box #{pos + 1}: expected {exp!r} but JDE has {got!r}")
    return diffs


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
    # 1. Click to focus, then let focus settle so the first keystrokes aren't
    #    dropped by JDE's handlers (a cause of truncated values, "BEK501"→"501").
    await locator.first.click()
    await asyncio.sleep(0.15)
    # 2. Select all + delete to clear
    await page.keyboard.press("Control+a")
    await page.keyboard.press("Delete")
    # 3. Type each character — fires real keyboard events
    await locator.first.press_sequentially(value, delay=30)
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


async def is_session_active(page: Page) -> bool:
    """True if the JDE session is still logged in.

    JDE shows a "Sign Out" control in the header while a session is active; once
    it times out / logs the user out, that control is gone (the login page is
    shown instead). We look, across all frames, for a VISIBLE element whose text
    is exactly "Sign Out". Returns False if none is visible.
    """
    js = """() => {
        const norm = s => (s||'').split(/[\\s\\u00A0]+/).filter(Boolean).join(' ').toLowerCase();
        const els = Array.from(document.querySelectorAll('a, span, button, div, td'));
        for (const el of els) {
            if (norm(el.textContent) !== 'sign out') continue;
            const r = el.getBoundingClientRect();
            if (r.width > 0 && r.height > 0) return true;
        }
        return false;
    }"""
    for frame in page.frames:
        try:
            if await frame.evaluate(js):
                return True
        except Exception:
            continue
    return False


# Left-operand names that use JDE's multi-value literal editor
# (#litList + #LITtfList + #hc950 Add + #hc952 Delete) instead of a single #LITtf.
MULTI_VALUE_LEFT_OPERANDS: set[str] = {
    "order type",
}


def _split_multi_values(raw: str) -> list[str]:
    """Split a comma/semicolon-separated Excel value into a de-duplicated,
    ordered list. Excel may use ',' or ';' (e.g. '10101, 10450, 10502' or
    'SA; SF; SM'); surrounding quotes on each value are stripped."""
    seen: set[str] = set()
    out: list[str] = []
    for chunk in re.split(r"[;,]", str(raw or "")):
        v = chunk.strip().strip("'\"").strip()
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
    """Select the <option> in #litList whose text matches *needle*
    (case-insensitive) so JDE's Delete button (#hc952) acts on it.

    Prefers Playwright's native select_option — a real selection gesture that
    updates selectedIndex and fires input/change the way JDE expects — because
    a purely programmatic `.selected` is not always honored by the Delete
    handler. Falls back to a JS selection + change event. Returns True on
    success.
    """
    want = needle.strip().lower()
    for frame in page.frames:
        try:
            loc = frame.locator("#litList")
            if await loc.count() == 0:
                continue
            opts = await loc.evaluate(
                "(el) => Array.from(el.options).map(o => (o.textContent || '').trim())"
            )
        except Exception:
            continue
        idx = next((i for i, o in enumerate(opts) if o.lower() == want), None)
        if idx is None:
            continue
        try:
            await loc.select_option(index=idx)
            return True
        except Exception:
            pass
        # Fallback: set the selection in JS and fire change on the same frame.
        js = """(i) => {
            const list = document.querySelector('#litList');
            if (!list || i < 0 || i >= list.options.length) return false;
            for (const o of list.options) o.selected = false;
            list.options[i].selected = true;
            list.selectedIndex = i;
            list.value = list.options[i].value;
            list.dispatchEvent(new Event('change', { bubbles: true }));
            return true;
        }"""
        try:
            if await frame.evaluate(js, idx):
                return True
        except Exception:
            continue
    return False


async def detect_active_literal_tab(
    page: Page, timeout_ms: int = 6000, poll_ms: int = 250,
) -> Optional[str]:
    """Return the visible text of the currently active tab in JDE's Literal
    editor: one of 'Single Value', 'Range of Values', 'List of Values'.

    The editor renders asynchronously after "Literal" is selected, so we poll
    until the active tab appears or *timeout_ms* elapses (a single immediate
    check often runs before the tabs exist and returns None).

    JDE marks the active tab with the ``ActiveTabLink`` class:
        <a class="ActiveTabLink" href="javascript:ocLitPrompt(2)">List of Values</a>

    Returns None if no active tab is detected within the timeout.
    """
    js = """() => {
        const active = document.querySelector('a.ActiveTabLink');
        if (!active) return null;
        return (active.textContent || '').trim();
    }"""
    attempts = max(1, timeout_ms // poll_ms)
    for attempt in range(attempts):
        for frame in page.frames:
            try:
                result = await frame.evaluate(js)
            except Exception:
                continue
            if result:
                return result
        if attempt < attempts - 1:
            await asyncio.sleep(poll_ms / 1000)
    return None


async def click_literal_tab(
    page: Page, runner: StepRunner, tab_name: str,
    timeout_ms: int = 6000, poll_ms: int = 250,
) -> bool:
    """Click the Literal editor tab whose visible text is *tab_name*
    ('Single Value' / 'Range of Values' / 'List of Values').

    The editor renders asynchronously after "Literal" is picked, so we poll for
    the tab anchor (an ``<a href="javascript:ocLitPrompt(N)">`` link), tag it,
    and click it via Playwright. Returns True once clicked, False if the tab
    never appeared within the timeout.
    """
    want = _norm_ws(tab_name).lower()
    js = """(want) => {
        const norm = s => (s||'').split(/[\\s\\u00A0]+/).filter(Boolean).join(' ').toLowerCase();
        const anchors = Array.from(document.querySelectorAll('a'));
        let hit = anchors.find(a => norm(a.textContent) === want
            && /litprompt/i.test(a.getAttribute('href') || ''));
        if (!hit) hit = anchors.find(a => norm(a.textContent) === want);
        if (!hit) return false;
        hit.setAttribute('data-lit-tab-target', '1');
        return true;
    }"""
    attempts = max(1, timeout_ms // poll_ms)
    for attempt in range(attempts):
        for frame in page.frames:
            try:
                found = await frame.evaluate(js, want)
            except Exception:
                continue
            if found:
                await runner.click(
                    f"Literal tab {tab_name!r}",
                    selector="a[data-lit-tab-target='1']",
                    iframe=IFRAME, selector_strategy="css",
                )
                try:
                    await frame.evaluate(
                        """() => { const el = document.querySelector("a[data-lit-tab-target='1']");
                                   if (el) el.removeAttribute('data-lit-tab-target'); }"""
                    )
                except Exception:
                    pass
                return True
        if attempt < attempts - 1:
            await asyncio.sleep(poll_ms / 1000)
    return False


class LiteralTypeMismatch(Exception):
    """The Excel value's shape doesn't match the active Literal editor tab.

    Raised (instead of a report-aborting StepError) so the dispatch loop can
    record the error, leave that Data Selection unchanged, and continue with
    the remaining fields.
    """


class LiteralTabNotDetected(Exception):
    """The active Literal editor tab could not be detected within the timeout.

    Without the active tab we can't tell which data type the field expects, so
    the dispatch loop records a warning, leaves the field unchanged, and
    continues with the remaining fields / versions.
    """


class LeftOperandAddFailed(Exception):
    """A new Left Operand row could not be added / verified in JDE.

    Raised (instead of a report-aborting StepError) so the dispatch loop can
    record the error, leave that Data Selection unchanged, and continue with
    the remaining fields.
    """


class DataSelectionDialogError(Exception):
    """The Data Selection dialog did not open after clicking the option.

    This is CRITICAL: without the dialog there is no way to apply any Data
    Selection or Processing Option for this version, so — unlike per-field
    errors — it must abort the iteration (never be swallowed and continued).
    """


class VersionNotFoundError(Exception):
    """JDE raised an error after the Data Selection option was clicked because
    the version being modified does not exist.

    Nothing downstream (Data Selection or Processing Options) can run for a
    missing version, so this aborts the current iteration; the caller records it
    to the report/dashboard and the batch continues with the next iteration.
    """


def _classify_value_shape(value: str) -> str:
    """Classify an Excel literal value by shape:
        'list'   → separated by ',' or ';' (one or more values)
        'range'  → an integer range 'A - B'
        'single' → a single string/int scalar
    """
    s = str(value or "").strip()
    if _looks_like_value_list(s):
        return "list"
    if re.fullmatch(r"\d+\s*-\s*\d+", s):
        return "range"
    return "single"


def _classify_literal_value(value: str) -> tuple[str, list[str]]:
    """Decide which Literal tab a value belongs to, by its separator.

    Returns (shape, tokens):
        Range of Values → a dash between values ('220-260', '234 - 234').
        List of Values  → ';'- or ','-separated values, regardless of spacing
                          ('SDF330, DFS340', 'SDF330; DFS340', '50101;10101').
        Single Value    → a plain scalar ('4', 'MOD', '10501').
    """
    s = _strip_edge_quotes(str(value or "").strip())

    # A dash → Range of Values; split into the two bounds around the first dash
    # (guard against a leading/trailing dash leaving an empty side).
    if "-" in s:
        lo, hi = (p.strip() for p in s.split("-", 1))
        if lo and hi:
            return "range", [lo, hi]

    # A ';' or ',' separator → List of Values (two or more values).
    parts = [t.strip() for t in re.split(r"[;,]", s) if t.strip()]
    if len(parts) >= 2:
        return "list", parts

    return "single", [s]


def _literal_tab_type_error(active_tab: Optional[str], value: str) -> Optional[str]:
    """Return a human-readable error if *value*'s shape doesn't match the
    active Literal editor tab, else None.

        Single Value    → a single string/int
        Range of Values → an integer range 'A - B'
        List of Values  → a list of one or more values

    A single value is accepted on the List tab (a one-element list); only a
    range shape is rejected there. When the tab can't be detected we don't
    block the write.
    """
    tab = (active_tab or "").strip().lower()
    if not tab:
        return None
    shape = _classify_value_shape(value)
    raw = str(value or "").strip()
    named = {"single": "a single value", "range": "a range", "list": "a list"}[shape]

    if "range" in tab:
        if shape != "range":
            return (f"Active tab 'Range of Values' expects an integer range "
                    f"'A - B', but Excel value {raw!r} is {named}")
    elif "list" in tab:
        if shape == "range":
            return (f"Active tab 'List of Values' expects a list of values, "
                    f"but Excel value {raw!r} is {named}")
    elif "single" in tab:
        if shape != "single":
            return (f"Active tab 'Single Value' expects a single string/int, "
                    f"but Excel value {raw!r} is {named}")
    return None


async def write_literal_by_active_tab(
    page: Page, runner: StepRunner, value: str,
) -> None:
    """Write *value* into the Literal editor, choosing the tab from the value's
    shape — NOT from whichever tab happens to be active — then commit.

    Tab is chosen by how many values the Excel cell holds:
        1 value           → 'Single Value'    → #LITtf
        exactly 2 values  → 'Range of Values' → #LITtfFrom / #LITtfTo
                            ('A - B' range, or two ';'/','-separated values)
        3+ values         → 'List of Values'  → sync_literal_list_values
                            (#LITtfList + #hc950 / #litList + #hc952)

    The chosen tab is clicked first so the right controls are shown, then
    ``#hc_Select`` commits (the List flow commits inside sync). If the tab can't
    be found/clicked, LiteralTabNotDetected is raised so the caller records it
    and moves on.
    """
    shape, tokens = _classify_literal_value(value)
    tab_name = {
        "single": "Single Value",
        "range": "Range of Values",
        "list": "List of Values",
    }[shape]
    print(f"      🏷 Literal value {value!r} → {shape} → tab {tab_name!r}")

    if not await click_literal_tab(page, runner, tab_name):
        raise LiteralTabNotDetected(
            f"could not find/click the {tab_name!r} Literal tab for value "
            f"{value!r} — field left unchanged"
        )
    # Let the tab's controls render before typing into them.
    await asyncio.sleep(_LITERAL_TAB_SETTLE_S)

    if shape == "range":
        lo, hi = tokens[0], tokens[1]
        await fill_jde_field(page, "#LITtfFrom", lo)
        await fill_jde_field(page, "#LITtfTo", hi)
        await runner.click(
            "Select button", selector="#hc_Select",
            iframe=IFRAME, selector_strategy="css",
        )
        return

    if shape == "list":
        # sync_literal_list_values itself clicks #hc_Select as its final commit.
        await sync_literal_list_values(page, runner, str(value))
        return

    # Single Value.
    await fill_jde_field(page, "#LITtf", tokens[0])
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
    desired_norm = {v.lower() for v in desired}
    print(f"      🔁 litList sync: desired={desired}")

    async def _delete_extras() -> None:
        """Delete every #litList value that isn't desired, verifying each one
        actually left (JDE re-renders after each delete and can drop the
        selection). Values that refuse to select are parked so one stuck entry
        can't spin forever."""
        stuck: set[str] = set()
        guard = 60
        while guard > 0:
            guard -= 1
            current = await _read_lit_list_values(page)
            extras = [
                c for c in current
                if c.lower() not in desired_norm and c.lower() not in stuck
            ]
            if not extras:
                return
            extra = extras[0]
            if not await _select_lit_list_option(page, extra):
                print(f"      ⚠ Could not select {extra!r} in #litList — skipping delete")
                stuck.add(extra.lower())
                continue
            await asyncio.sleep(0.3)  # let the selection register
            await runner.click(
                f"Delete literal {extra!r} (#hc952)",
                selector="#hc952", iframe=IFRAME, selector_strategy="css",
            )
            await asyncio.sleep(0.4)  # let #litList re-render

    async def _add_one(value: str) -> bool:
        """Fill + Add one value, then verify it actually landed. JDE can
        truncate a typed value ("BEK501"→"501"); if the exact value didn't
        appear, delete whatever bogus entry did and report failure so the outer
        loop retries."""
        before = {c.lower() for c in await _read_lit_list_values(page)}
        await fill_jde_field(page, "#LITtfList", value)
        await runner.click(
            f"Add literal {value!r} (#hc950)",
            selector="#hc950", iframe=IFRAME, selector_strategy="css",
        )
        await asyncio.sleep(0.3)  # let the row append
        after = await _read_lit_list_values(page)
        if any(c.lower() == value.lower() for c in after):
            return True
        # The value didn't land correctly — remove any bogus entry it produced
        # (present now, not before, and not the value we wanted).
        bogus = [c for c in after if c.lower() not in before and c.lower() != value.lower()]
        for b in bogus:
            print(f"      ⚠ {value!r} inserted wrong as {b!r} — deleting it")
            if await _select_lit_list_option(page, b):
                await asyncio.sleep(0.2)
                await runner.click(
                    f"Delete truncated {b!r} (#hc952)",
                    selector="#hc952", iframe=IFRAME, selector_strategy="css",
                )
                await asyncio.sleep(0.3)
        return False

    # Reconcile: delete wrong values, add the missing ones (verified), and
    # repeat until #litList equals the Excel list or we run out of passes.
    MAX_PASSES = 5
    for _pass in range(1, MAX_PASSES + 1):
        await _delete_extras()
        current_norm = {c.lower() for c in await _read_lit_list_values(page)}
        missing = [v for v in desired if v.lower() not in current_norm]
        if not missing:
            break
        print(f"      🔁 litList pass {_pass}/{MAX_PASSES}: adding missing={missing}")
        for value in missing:
            if not await _add_one(value):
                print(f"      ⚠ {value!r} did not insert correctly — retry next pass")

    final = await _read_lit_list_values(page)
    final_norm = {c.lower() for c in final}
    still_missing = [v for v in desired if v.lower() not in final_norm]
    stray = [c for c in final if c.lower() not in desired_norm]
    if still_missing or stray:
        print(f"      ⚠ litList not fully reconciled — missing={still_missing} stray={stray}")
    else:
        print(f"      ✓ litList reconciled to {desired}")

    # Commit the panel.
    await runner.click(
        "Select button (#hc_Select)",
        selector="#hc_Select", iframe=IFRAME, selector_strategy="css",
    )


# Option values that JDE uses for "not set to a real literal" — matching any
# of these against a non-sentinel Excel value should NOT count as a match.
_RIGHT_OPERAND_SENTINELS: set[str] = {
    "literal", "blank", "zero", "null", "datetoday [sl]",
}

# Right Operand combo options for behaviors that pick a plain dropdown option
# (no Literal editor) — maps the parsed behavior string to the exact option
# text JDE renders in the dropdown.
_RIGHT_OPERAND_OPTION: dict[str, str] = {
    "zero": "Zero",
    "null": "Null",
    "datetoday": "DateToday [SL]",
}


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


async def read_locked_right_operand_text(
    page: Page, row_number: str,
) -> Optional[str]:
    """Return the current Right Operand value of a LOCKED Data Selection row.

    Locked rows render no #RightOperand{N} select — the value is static text.
    In #jdeGrid the columns are Operator (1), Left Operand (2), Comparison (3),
    Right Operand (4), so we read the Right Operand column's StaticText span.
    The column index is detected from an unlocked row's #RightOperand select
    (or #LeftOperand + 2), falling back to the 4th StaticText span in the row.

    Returns None if the row can't be found in any frame.
    """
    js = """(n) => {
        const norm = (s) => (s || '').split(/[\\s\\u00A0]+/).filter(Boolean).join(' ');
        const cb = document.querySelector('#Select' + n);
        if (!cb) return null;
        const tr = cb.closest('tr');
        if (!tr) return null;

        // Detect the Right Operand column index across the grid.
        const scope = document.querySelector('#jdeGrid') || document;
        let rightCol = -1;
        const ro = scope.querySelector("select[id^='RightOperand']");
        if (ro) {
            const td = ro.closest('td');
            if (td) rightCol = Array.prototype.indexOf.call(td.parentElement.children, td);
        }
        if (rightCol < 0) {
            const lo = scope.querySelector("select[id^='LeftOperand']");
            if (lo) {
                const td = lo.closest('td');
                if (td) rightCol = Array.prototype.indexOf.call(td.parentElement.children, td) + 2;
            }
        }

        let value = null, via = '';
        if (rightCol >= 0 && tr.children[rightCol]) {
            const cell = tr.children[rightCol];
            const span = cell.querySelector('span.StaticText, .StaticText');
            value = (span ? span.textContent : cell.textContent) || '';
            via = 'grid-col';
        } else {
            const st = tr.querySelectorAll('span.StaticText, .StaticText');
            if (st.length >= 4) { value = st[3].textContent || ''; via = 'static-4th'; }
        }
        return value === null ? null : { value: norm(value), via: via };
    }"""
    for frame in page.frames:
        try:
            result = await frame.evaluate(js, str(row_number))
        except Exception:
            continue
        if result:
            print(
                f"      🔒 locked row {row_number} Right Operand "
                f"(via {result.get('via')}): {result.get('value')!r}"
            )
            return result.get("value") or ""
    return None


def _tokenize_multi_value(raw: str) -> set[str]:
    """Split a comma/semicolon list into a normalized set for order-insensitive
    comparison (used for multi-value Left Operands like Order Type). Surrounding
    quotes on the value or individual tokens are stripped so a JDE value like
    '"10101,10450,10502"' matches the Excel '10101, 10450, 10502'."""
    tokens: set[str] = set()
    for chunk in str(raw or "").replace(";", ",").split(","):
        v = chunk.strip().strip("'\"").strip().lower()
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


def _strip_edge_quotes(s: str) -> str:
    """Strip the single/double quotes JDE sometimes wraps a literal in
    (e.g. '"10101,10450,10502"' → '10101,10450,10502')."""
    s = str(s or "").strip()
    while len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    return s


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
    cur = _strip_edge_quotes(current)
    exp = _strip_edge_quotes(excel_value)
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


def _expected_right_operand(sel: dict) -> str:
    """The Right Operand value Excel expects for a Data Selection: the sentinel
    option text for zero/null/datetoday, else the literal value."""
    beh = sel.get("behavior", "literal")
    if beh in _RIGHT_OPERAND_OPTION:
        return _RIGHT_OPERAND_OPTION[beh]
    return str(sel.get("data_new", ""))


async def verify_data_selection_result(
    page: Page, runner: StepRunner, data_selections: list[dict], label: str,
) -> list[str]:
    """After the Data Selection stage, re-read JDE's grid and compare it with
    the Excel fields (ignoring REMOVE). The final JDE list must equal the Excel
    list on Left Operand, Comparison and Right Operand.

    Each difference is recorded (record_failure) so it lands in the report and
    dashboard, and returned as a list of messages. Non-fatal — it only reports.
    """
    # Expected list from Excel — REMOVE rows are gone from JDE, so drop them.
    expected = [s for s in data_selections if s.get("behavior") != "remove"]

    actual = await enumerate_data_selection_rows(page)
    # Only rows with a real Left Operand (skip the blank template row).
    remaining = [
        {
            "left_n": _clean_operand_for_match(a.get("left", "")),
            "comp_n": _norm_ws(a.get("comparison", "")).lower(),
            "right": a.get("right", ""),
            "raw": a,
        }
        for a in actual if _clean_operand_for_match(a.get("left", ""))
    ]

    diffs: list[str] = []
    for sel in expected:
        want_left = _clean_operand_for_match(sel.get("left_operand", ""))
        want_comp = resolve_comparison(sel.get("comparison", "")).strip().lower()
        want_right = _expected_right_operand(sel)
        is_multi = sel.get("left_operand", "").strip().lower() in MULTI_VALUE_LEFT_OPERANDS
        beh = sel.get("behavior", "literal")

        def _right_ok(row_right: str) -> bool:
            if beh in _RIGHT_OPERAND_OPTION:
                return _norm_ws(row_right).lower() == _norm_ws(want_right).lower()
            return right_operand_matches_excel(row_right, want_right, is_multi)

        # Prefer a full match (left + comparison + right); consume it.
        full = next(
            (i for i, r in enumerate(remaining)
             if r["left_n"] == want_left
             and (not r["comp_n"] or r["comp_n"] == want_comp)
             and _right_ok(r["right"])),
            None,
        )
        if full is not None:
            remaining.pop(full)
            continue

        # Left present but comparison/right differs — report the specifics.
        partial = next((i for i, r in enumerate(remaining) if r["left_n"] == want_left), None)
        if partial is not None:
            r = remaining.pop(partial)
            problems = []
            if r["comp_n"] and r["comp_n"] != want_comp:
                problems.append(f"comparison expected {want_comp!r} but JDE has {r['comp_n']!r}")
            if not _right_ok(r["right"]):
                problems.append(f"value expected {want_right!r} but JDE has {r['right']!r}")
            diffs.append(
                f"{sel.get('left_operand', '?')}: "
                + (", ".join(problems) if problems else "mismatch")
            )
        else:
            diffs.append(
                f"{sel.get('left_operand', '?')}: expected in JDE "
                f"({want_comp} / {want_right!r}) but not found"
            )

    # Anything still in JDE that Excel didn't list → unexpected extra rows.
    for r in remaining:
        diffs.append(
            f"Unexpected JDE row not in Excel: {r['raw'].get('left', '')!r} "
            f"{r['raw'].get('comparison', '')!r} {r['raw'].get('right', '')!r}"
        )

    if diffs:
        print(f"[{label}] ⚠ DS verification found {len(diffs)} difference(s):")
        for d in diffs:
            print(f"[{label}]     • {d}")
            runner.record_failure("Data Selection verification", d)
    else:
        print(f"[{label}] ✓ DS verification: JDE matches Excel ({len(expected)} field(s))")
    return diffs


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


async def click_add_row_button(
    page: Page, runner: StepRunner, timeout_ms: int = 4000, poll_ms: int = 250,
) -> bool:
    """Click JDE's 'Add Row' button to append a fresh blank Data Selection row.

    The button is a ``<span class="StaticText"> Add Row </span>``. We poll for it
    (text is whitespace-normalized), tag it, and click it via Playwright.
    Returns True once clicked, False if it never appeared within the timeout.
    """
    js = """() => {
        const norm = s => (s||'').split(/[\\s\\u00A0]+/).filter(Boolean).join(' ').toLowerCase();
        const spans = Array.from(document.querySelectorAll('span'));
        const hit = spans.find(s => norm(s.textContent) === 'add row');
        if (!hit) return false;
        hit.setAttribute('data-add-row-target', '1');
        return true;
    }"""
    attempts = max(1, timeout_ms // poll_ms)
    for attempt in range(attempts):
        for frame in page.frames:
            try:
                found = await frame.evaluate(js)
            except Exception:
                continue
            if found:
                await runner.click(
                    "Add Row button",
                    selector="span[data-add-row-target='1']",
                    iframe=IFRAME, selector_strategy="css",
                )
                try:
                    await frame.evaluate(
                        """() => { const el = document.querySelector("span[data-add-row-target='1']");
                                   if (el) el.removeAttribute('data-add-row-target'); }"""
                    )
                except Exception:
                    pass
                return True
        if attempt < attempts - 1:
            await asyncio.sleep(poll_ms / 1000)
    return False


async def select_left_operand_by_similarity(
    page: Page, runner: StepRunner, row_number: str, desired: str,
) -> Optional[str]:
    """Select, in #LeftOperand{row_number}, the option most similar to *desired*.

    JDE renders each option with qualifiers appended
    ("Order Type (F4211) (DCTO) [BC]"), so an exact value select of the cleaned
    Excel name ("Order Type") never matches. We enumerate the options, normalize
    each with _clean_operand_for_match, and pick the best match: an exact
    normalized equality wins outright, then substring containment, then the
    highest difflib ratio. Returns the chosen option text, or None if no option
    is a reasonable match.
    """
    js = """(rid) => {
        const sel = document.getElementById(rid);
        if (!sel) return null;
        return Array.from(sel.options).map((o, i) => ({
            index: i, text: (o.textContent || '').trim(),
        }));
    }"""
    owner = None
    options: list[dict] = []
    for frame in page.frames:
        try:
            res = await frame.evaluate(js, f"LeftOperand{row_number}")
        except Exception:
            continue
        if res is not None:
            owner, options = frame, res
            break
    if not options:
        return None

    key = _clean_operand_for_match(desired)
    best = None
    best_score = -1.0
    for o in options:
        text = o["text"]
        if not text:
            continue
        cand = _clean_operand_for_match(text)
        if cand == key and key:
            best, best_score = o, 2.0            # exact normalized match wins
            break
        score = difflib.SequenceMatcher(None, key, cand).ratio()
        if key and key in cand:
            score = max(score, 0.9)              # strong boost for containment
        if score > best_score:
            best, best_score = o, score

    # Guard against a nonsense pick when nothing really resembles the operand
    # (exact matches score 2.0 and substring containment 0.9, so both pass).
    if best is None or (best_score < 0.5 and not (key and key in _clean_operand_for_match(best["text"]))):
        return None

    print(
        f"        LeftOperand similarity: {desired!r} → {best['text']!r} "
        f"(score {best_score:.2f})"
    )
    try:
        await owner.locator(f"#LeftOperand{row_number}").select_option(index=best["index"])
    except Exception:
        # Fallback to the runner's select by the exact option text.
        await runner.select(
            f"LeftOperand{row_number}",
            value=best["text"],
            selector=f"#LeftOperand{row_number}",
            iframe=IFRAME, selector_strategy="css",
        )
    return best["text"]


async def select_comparison_by_text(
    page: Page, row_number: str, desired_text: str,
) -> bool:
    """Select, in #Comparison{row_number}, the option matching *desired_text*.

    JDE's Comparison options can carry a coded ``value`` and label whitespace
    that differ from the plain phrase ("is equal to"), so a select by label or
    value can fail to find them. We enumerate the options (logging them for
    diagnosis), match on normalized text OR value — exact, then containment,
    then best difflib ratio — and select by index. Returns True on success,
    False if no option is a reasonable match.
    """
    js = """(rid) => {
        const sel = document.getElementById(rid);
        if (!sel) return null;
        return Array.from(sel.options).map((o, i) => ({
            index: i, text: (o.textContent || '').trim(), value: (o.value || ''),
        }));
    }"""
    owner = None
    options: list[dict] = []
    for frame in page.frames:
        try:
            res = await frame.evaluate(js, f"Comparison{row_number}")
        except Exception:
            continue
        if res is not None:
            owner, options = frame, res
            break
    if not options:
        print(f"        ✖ #Comparison{row_number} has no options to select")
        return False

    want = _norm_ws(desired_text).lower()
    best = None
    best_score = -1.0
    for o in options:
        t = _norm_ws(o["text"]).lower()
        v = _norm_ws(o["value"]).lower()
        if want and (t == want or v == want):
            best, best_score = o, 2.0
            break
        score = difflib.SequenceMatcher(None, want, t).ratio()
        if want and want in t:
            score = max(score, 0.9)
        if score > best_score:
            best, best_score = o, score

    print(
        f"        Comparison options for #Comparison{row_number}: "
        f"{[o['text'] for o in options]}"
    )
    if best is None or best_score < 0.5:
        return False

    print(
        f"        Comparison match: {desired_text!r} → {best['text']!r} "
        f"(score {best_score:.2f})"
    )
    try:
        await owner.locator(f"#Comparison{row_number}").select_option(index=best["index"])
        return True
    except Exception as exc:
        print(f"        ✖ select_option failed for #Comparison{row_number}: {exc}")
        return False


async def add_data_selection_row(
    page: Page,
    runner: StepRunner,
    left_operand_text: str,
    comparison_text: str,
    value: str,
    right_operand: str = "Literal",
) -> list[dict]:
    """Add a new Data Selection row for a Left Operand JDE doesn't have yet.

    Sequence (operates on the last empty grid row, #...{N}):
      1. #LeftOperand{N}: select the option with the highest similarity to the
         Excel operand (options carry qualifiers, so exact match won't do).
      2. #Comparison{N}: select the operator equal to the Excel comparison
         (column B, translated via COMPARISON_MAP).
      3. #RightOperand{N}: set *right_operand* and, for "Literal", write the
         Excel value via the existing active-tab writer.
      4. Verify the operand now appears by re-reading JDE's full Left Operand
         list and confirming the added name is present.

    Returns the refreshed Left Operand enumeration (so the caller can reuse it
    as its grid cache). Raises LeftOperandAddFailed if no similar option exists
    or the operand is absent after the add.
    """
    row_number = await find_empty_left_operand_row(page)
    if not row_number:
        # JDE ran out of blank template rows — click "Add Row" to append one so
        # the DS process can still place every Excel field.
        print(
            f"      ↳ No empty LeftOperand row — clicking 'Add Row' to make one "
            f"for {left_operand_text!r}"
        )
        if not await click_add_row_button(page, runner):
            raise LeftOperandAddFailed(
                f"No empty LeftOperand row and could not click 'Add Row' to add "
                f"{left_operand_text!r}"
            )
        await asyncio.sleep(_ADD_ROW_SETTLE_S)  # let the new row render
        row_number = await find_empty_left_operand_row(page)
        if not row_number:
            raise LeftOperandAddFailed(
                f"'Add Row' did not produce an empty LeftOperand row for "
                f"{left_operand_text!r}"
            )

    print(
        f"      ➕ Adding new DS row #{row_number}: "
        f"{left_operand_text!r} {comparison_text!r} {value!r}"
    )

    # 1. Left Operand — pick the most similar option in the expandable menu.
    chosen = await select_left_operand_by_similarity(
        page, runner, row_number, left_operand_text
    )
    if chosen is None:
        raise LeftOperandAddFailed(
            f"No LeftOperand option resembled {left_operand_text!r}"
        )
    # Selecting the operand fires onchange="FilterRightOp(N)", which re-renders
    # and repopulates this row's Comparison/RightOperand cells. Give JDE time to
    # finish before touching #Comparison{N}, else the select times out locating
    # it mid-re-render.
    await asyncio.sleep(_ADD_ROW_SETTLE_S)

    # 2. Comparison — the operator equal to the Excel value (column B).
    resolved_comparison = resolve_comparison(comparison_text)
    print(f"        Comparison: {comparison_text!r} → {resolved_comparison!r}")
    if not await select_comparison_by_text(page, row_number, resolved_comparison):
        raise LeftOperandAddFailed(
            f"Could not select comparison {resolved_comparison!r} in "
            f"#Comparison{row_number}"
        )
    # Comparison also fires FilterRightOp(N) → let RightOperand re-render.
    await asyncio.sleep(_ADD_ROW_SETTLE_S)

    # 3. Right Operand + value.
    await runner.select(
        f"RightOperand{row_number}",
        value=right_operand,
        selector=f"#RightOperand{row_number}",
        iframe=IFRAME,
        selector_strategy="css",
    )
    await asyncio.sleep(_ADD_ROW_SETTLE_S)
    # "Zero" / "Null" / "DateToday [SL]" need no value — selecting is the edit.
    if right_operand == "Literal":
        await write_literal_by_active_tab(page, runner, str(value))

    # 4. Verify: the operand must now be in JDE's full Left Operand list.
    rows, _ = await list_left_operands(page, settle_ms=1000)
    key = _clean_operand_for_match(left_operand_text)
    jde_names = [_clean_operand_for_match(r["value"]) for r in rows if r.get("value")]
    present = bool(key) and (key in jde_names or any(key in n for n in jde_names))
    if not present:
        raise LeftOperandAddFailed(
            f"{left_operand_text!r} not found in JDE Left Operand list after add "
            f"(JDE list: {[r['value'] for r in rows]})"
        )
    print(f"        ✓ Verified {left_operand_text!r} added ({len(rows)} rows now)")
    return rows


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
    # copy_version True (default): copy current_version into a new version.
    # False: edit current_version in place — skip the copy and don't use
    # new_version. Old JSON test cases without the flag default to copy.
    copy_version = bool(report.get("copy_version", True))
    _version_label = report.get("new_version") if copy_version else report.get("current_version")
    label = f"{report.get('app_report', '?')}/{_version_label or '?'}"

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

        # ── Search the version to work on ───────────────────────────────
        # Copy mode: current_version is the source we copy from.
        # Edit mode: current_version is the existing version we edit in place.
        await runner.type(
            "version QBE filter",
            value=report["current_version"],
            selector=version_qbe_selector, iframe=IFRAME, selector_strategy="css"
        )
        await runner.key_press("Enter")

        if copy_version:
            # ── Select & Copy → create the new version ──────────────────
            await runner.click("Select All checkbox", selector="#selectAll0_1", iframe=IFRAME, selector_strategy="css")
            await runner.click("Copy button", selector="#hc_Copy", iframe=IFRAME, selector_strategy="css")

            # ── Fill new version ────────────────────────────────────────
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

            # ── Check for errors (e.g. version already exists) ──────────
            await runner.check_error("#INYFEContent")

            # ── Click OK ────────────────────────────────────────────────
            await runner.click("OK button", selector="#hc_OK", iframe=IFRAME, selector_strategy="css")

            # ── Search the newly-created version so edits target it ─────
            await runner.type(
                "version QBE filter",
                value=report["new_version"],
                selector=version_qbe_selector, iframe=IFRAME, selector_strategy="css"
            )
            await runner.key_press("Enter")
        else:
            print(f"[{label}] Edit mode: editing existing version "
                  f"{report['current_version']!r} in place (no copy)")

        # ── Select the version's rows before opening the Row Menu ───────
        await runner.click("Select All checkbox", selector="#selectAll0_1", iframe=IFRAME, selector_strategy="css")

        # Per-field validation errors (wrong data type for the field / tab).
        # These don't abort the run — the field is left unchanged, the error
        # is recorded for the report, and processing continues. If any occur,
        # the iteration is reported as failed at the end.
        field_errors: list[str] = []
        # Non-fatal warnings (e.g. the active Literal tab could not be
        # detected). Recorded to the report but don't fail the iteration.
        field_warnings: list[str] = []

        # ── Data Selection — loop once per entry ────────────────────────
        if data_selections:
            try:
                print(f"[{label}] Configuring {len(data_selections)} data selection(s)")
                await runner.click("Row Menu", selector=row_menu_selector, iframe=IFRAME, selector_strategy="css")
                await runner.click("Data Selection option", selector="#HE0_127", iframe=IFRAME, selector_strategy="css")

                # If the version being modified doesn't exist, JDE shows an error
                # banner here (same #INYFEContent banner the copy flow checks when
                # naming a new version). Detect it and abort this iteration.
                await asyncio.sleep(0.5)  # let the banner render
                try:
                    await runner.check_error("#INYFEContent")
                except StepError as verr:
                    raise VersionNotFoundError(
                        f"Version {report.get('current_version', '?')!r} could not be "
                        f"opened for Data Selection — JDE error: {verr}"
                    )

                # JDE can be slow to render the dialog. Wait and confirm it
                # actually opened (#jdeFormTitle title="Data Selection"); if not,
                # this is a CRITICAL failure — nothing downstream can run.
                if not await verify_data_selection_dialog_open(page):
                    raise DataSelectionDialogError(
                        "Data Selection dialog did not open — #jdeFormTitle with "
                        "title='Data Selection' not found after 2 attempts"
                    )

                # Enumerate the Left Operand column ONCE for the whole dialog (the
                # grid doesn't change on a value edit — only when a row is added or
                # removed, after which lo_rows is refreshed). Reused for every field
                # so duplicated Left Operands map their Nth entry to the Nth row.
                print(f"[{label}] Enumerating Left Operand column (one-time)...")
                lo_rows, _ = await list_left_operands(page, settle_ms=5000)

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

                    # Constant values map to distinct behaviors (attached at parse
                    # time): "remove" (REMOVE / Blank) deletes the row; "zero" /
                    # "null" select that option in the Right Operand combo box;
                    # everything else is a "literal". The literal's data type is
                    # verified at write time against JDE's active tab (see
                    # write_literal_by_active_tab), not from the Left Operand name.
                    behavior = sel.get("behavior", "literal")

                    # Nth Excel entry of a Left Operand → Nth JDE row with that
                    # name (handles duplicates like two "Order Company" rows). The
                    # ordinal is computed at parse time so blank cells in other
                    # report columns don't shift it.
                    occurrence = int(sel.get("occurrence", 1) or 1)

                    # Find the matching RightOperand row by scanning all
                    # LeftOperand dropdowns for one whose option text contains
                    # the user's left_operand value. If nothing matches, the
                    # operand isn't in JDE yet: "remove" has nothing to delete;
                    # otherwise add a brand-new row with the right combo option.
                    try:
                        right_operand_sel = await find_right_operand_selector(
                            page, left_operand, occurrence=occurrence, rows=lo_rows,
                        )
                    except LookupError as exc:
                        if behavior == "remove":
                            print(
                                f"[{label}]   ↳ {left_operand!r} not present — "
                                f"nothing to remove, skipping"
                            )
                            continue
                        print(f"[{label}]   ↳ {exc} — adding as a new row")
                        try:
                            # add_data_selection_row returns the refreshed grid
                            # (it re-reads to verify the operand was added).
                            lo_rows = await add_data_selection_row(
                                page, runner, left_operand, comparison_value, data_value,
                                right_operand=_RIGHT_OPERAND_OPTION.get(behavior, "Literal"),
                            )
                        except LeftOperandAddFailed as adderr:
                            # Couldn't add / verify the operand — record it and
                            # keep going with the remaining fields.
                            msg = f"{left_operand}: {adderr}"
                            print(f"[{label}]   ↳ ✖ {msg} — leaving field unchanged")
                            runner.record_failure(f"Data Selection: {left_operand}", str(adderr))
                            field_errors.append(msg)
                            await _diagnostic_screenshot(
                                page, f"jde_ds_addfail_{report.get('app_report', 'unknown')}_{idx}"
                            )
                        except LiteralTypeMismatch as tmerr:
                            # Wrong data type for the field — leave it unset,
                            # record the error, and keep going.
                            msg = f"{left_operand}: {tmerr}"
                            print(f"[{label}]   ↳ ✖ {msg} — leaving field unchanged")
                            runner.record_failure(f"Data Selection: {left_operand}", str(tmerr))
                            field_errors.append(msg)
                            await _diagnostic_screenshot(
                                page, f"jde_ds_typemismatch_{report.get('app_report', 'unknown')}_{idx}"
                            )
                        except LiteralTabNotDetected as tnderr:
                            # Active tab undetected — warn and keep going.
                            msg = f"{left_operand}: {tnderr}"
                            print(f"[{label}]   ↳ ⚠ {msg} — leaving field unchanged")
                            runner.record_warning(f"Data Selection: {left_operand}", str(tnderr))
                            field_warnings.append(msg)
                            await _diagnostic_screenshot(
                                page, f"jde_ds_tabundetected_{report.get('app_report', 'unknown')}_{idx}"
                            )
                        except Exception as add_exc:
                            # Couldn't add the row (e.g. the operand isn't in JDE
                            # and the add flow failed). Bubble to the Data Selection
                            # handler, which records it and still runs Processing
                            # Options.
                            raise StepError("Add Data Selection row", str(add_exc), None)
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

                    # Pre-edit check: read the current Right Operand value and skip
                    # when the row is already in the desired state — no unlock /
                    # edit / re-lock churn needed. "remove" always goes through.
                    # Locked rows have no #RightOperand{N} select, so their value
                    # is read from the column-4 StaticText instead.
                    if behavior != "remove" and row_number:
                        if row_is_locked:
                            current_right = await read_locked_right_operand_text(page, row_number)
                        else:
                            current_right = await read_right_operand_selected_text(page, row_number)
                        if behavior in _RIGHT_OPERAND_OPTION:
                            option_text = _RIGHT_OPERAND_OPTION[behavior]
                            if (current_right or "").strip().lower() == option_text.lower():
                                print(
                                    f"[{label}]   ↳ Right Operand already "
                                    f"{option_text!r} — skipping"
                                )
                                continue
                            print(
                                f"[{label}]   ↳ setting Right Operand to "
                                f"{option_text!r}"
                            )
                        else:  # literal
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

                    # ── REMOVE branch (REMOVE / Blank) ───────────────────────
                    # Mark the matching row's checkbox and click Delete instead of
                    # setting a value. The row is gone after this, so no re-lock.
                    if behavior == "remove":
                        if not row_number:
                            raise StepError(
                                "REMOVE data selection",
                                f"Could not extract row number from {right_operand_sel!r}",
                                None,
                            )
                        select_checkbox = f"#Select{row_number}"
                        print(
                            f"[{label}]   ↳ remove mode: checking {select_checkbox} "
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
                        # A deleted row changed the grid — refresh the cache.
                        lo_rows, _ = await list_left_operands(page, settle_ms=1000)
                        continue

                    # ── Combo-option branch (Zero / Null / DateToday [SL]) ────
                    # These are plain Right Operand combo options — no Literal
                    # editor, no value to type.
                    if behavior in _RIGHT_OPERAND_OPTION:
                        await runner.select(
                            "Right Operand dropdown",
                            value=_RIGHT_OPERAND_OPTION[behavior],
                            selector=right_operand_sel, iframe=IFRAME, selector_strategy="css",
                        )
                        if row_is_locked and row_number:
                            await lock_data_selection_row(runner, row_number)
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
                    try:
                        await write_literal_by_active_tab(page, runner, str(data_value))
                    except LiteralTypeMismatch as tmerr:
                        # The value's data type doesn't fit the active tab. Leave
                        # the field unchanged (the literal was never committed),
                        # record the error, and continue with the next field.
                        msg = f"{left_operand}: {tmerr}"
                        print(f"[{label}]   ↳ ✖ {msg} — leaving field unchanged")
                        runner.record_failure(f"Data Selection: {left_operand}", str(tmerr))
                        field_errors.append(msg)
                        await _diagnostic_screenshot(
                            page, f"jde_ds_typemismatch_{report.get('app_report', 'unknown')}_{idx}"
                        )
                        if row_is_locked and row_number:
                            await lock_data_selection_row(runner, row_number)
                        continue
                    except LiteralTabNotDetected as tnderr:
                        # Active tab couldn't be detected — we can't tell the
                        # required data type. Record a warning, leave the field
                        # unchanged, and continue with the remaining fields.
                        msg = f"{left_operand}: {tnderr}"
                        print(f"[{label}]   ↳ ⚠ {msg} — leaving field unchanged (warning)")
                        runner.record_warning(f"Data Selection: {left_operand}", str(tnderr))
                        field_warnings.append(msg)
                        await _diagnostic_screenshot(
                            page, f"jde_ds_tabundetected_{report.get('app_report', 'unknown')}_{idx}"
                        )
                        if row_is_locked and row_number:
                            await lock_data_selection_row(runner, row_number)
                        continue

                    # Restore the lock state — only for the edit branch.
                    # (REMOVE already `continue`d above.)
                    if row_is_locked and row_number:
                        await lock_data_selection_row(runner, row_number)

                # ── Verify: JDE's final DS grid must match the Excel list ────
                # Re-read Left Operand / Comparison / Right Operand for every
                # row and compare with the Excel fields (REMOVE ignored). Any
                # difference is recorded to the report — non-fatal.
                try:
                    ds_diffs = await verify_data_selection_result(
                        page, runner, data_selections, label
                    )
                    if ds_diffs:
                        field_errors.extend(ds_diffs)
                        await _diagnostic_screenshot(
                            page, f"jde_ds_verify_{report.get('app_report', 'unknown')}"
                        )
                except Exception as verr:
                    print(f"[{label}]   ↳ ⚠ DS verification could not run: {verr}")

                # Close the Data Selections dialog
                await runner.click("Close Data Selection dialog", selector="#hc_Select", iframe=IFRAME, selector_strategy="css")
            except DataSelectionDialogError as crit:
                # CRITICAL: the dialog never opened — record it for the
                # dashboard and abort the iteration (do NOT fall through to
                # Processing Options; nothing downstream can run).
                print(f"[{label}] ✖ CRITICAL: {crit} — stopping this version")
                runner.record_failure("Open Data Selection dialog", str(crit))
                await _diagnostic_screenshot(
                    page, f"jde_ds_dialog_not_open_{report.get('app_report', 'unknown')}"
                )
                raise StepError("Open Data Selection dialog", str(crit), None)
            except VersionNotFoundError as verr:
                # The version to modify doesn't exist — record it for the
                # report/dashboard and abort THIS iteration so the batch moves
                # on to the next one (nothing downstream can run).
                print(f"[{label}] ✖ {verr} — skipping to next iteration")
                runner.record_failure("Open version for Data Selection", str(verr))
                await _diagnostic_screenshot(
                    page, f"jde_ds_version_missing_{report.get('app_report', 'unknown')}"
                )
                raise StepError("Open version for Data Selection", str(verr), None)
            except Exception as ds_err:
                # A Data Selection failure (e.g. a Left Operand from the
                # Excel file that isn't in JDE and can't be added) must NOT
                # skip Processing Options. Record it, snapshot for
                # debugging, close the dialog best-effort, and fall through
                # to the Processing Options block below.
                msg = f"Data Selection failed: {ds_err}"
                print(f"[{label}] ✖ {msg} — continuing with Processing Options")
                runner.record_failure("Data Selection", str(ds_err))
                field_errors.append(str(ds_err))
                await _diagnostic_screenshot(
                    page, f"jde_ds_fail_{report.get('app_report', 'unknown')}"
                )
                try:
                    await runner.click(
                        "Close Data Selection dialog",
                        selector="#hc_Select", iframe=IFRAME, selector_strategy="css",
                    )
                except Exception:
                    pass

        # --- Processing Options — filled POSITIONALLY, one tab at a time ────
        # The number of PO rows for a tab equals the number of text boxes in
        # that JDE tab, so each row maps to a text box by position (blank rows
        # skip a box). We fill a whole tab, then re-read and reconcile it
        # against the Excel values before moving to the next tab.
        if processing_options:
            print(f"[{label}] Configuring {len(processing_options)} processing option(s)")
            await runner.click("Row Menu", selector=row_menu_selector, iframe=IFRAME, selector_strategy="css")
            await runner.click("Processing Options", selector=processing_options_selector, iframe=IFRAME, selector_strategy="css")
            # Wait for the Processing Options dialog to fully render its tabs
            await asyncio.sleep(2)

            # Group PO entries by tab, preserving first-seen tab order.
            tabs_in_order: list[str] = []
            by_tab: dict[str, list[dict]] = {}
            for po in processing_options:
                tab = po.get("tab", "")
                if tab not in by_tab:
                    by_tab[tab] = []
                    tabs_in_order.append(tab)
                by_tab[tab].append(po)

            for tab in tabs_in_order:
                entries = by_tab[tab]
                print(f"[{label}]   PO tab {tab!r}: {len(entries)} value(s)")
                try:
                    # 1. Activate the tab and let its controls render.
                    if tab:
                        await activate_po_tab(page, runner, tab)
                        await asyncio.sleep(1)

                    # 2. Fill each value into its text box by position (position
                    #    N -> the (N+1)-th text box; blank rows were never
                    #    emitted, so their boxes are left untouched).
                    for po in entries:
                        pos = int(po.get("position", 0))
                        value = str(po.get("processing_new", "")).strip()
                        if not value:
                            continue
                        print(f"[{label}]     - text box #{pos + 1} <- {value!r}")
                        await fill_nth_processing_input(page, pos + 1, value)

                    # 3. Verify the tab and re-fill any mismatch until equal.
                    diffs = await verify_and_fix_po_tab(page, runner, tab, entries)
                    if diffs:
                        for d in diffs:
                            print(f"[{label}]   -> FAIL {d}")
                            runner.record_failure(f"Processing Option tab {tab}", d)
                            field_errors.append(d)
                        await _diagnostic_screenshot(
                            page,
                            f"jde_po_verify_{report.get('app_report', 'unknown')}"
                            f"_{_norm_ws(tab).replace(' ', '_')}",
                        )
                except Exception as po_err:
                    # The tab couldn't be activated / filled. Record it and
                    # continue with the remaining tabs.
                    msg = f"PO tab {tab}: {po_err}"
                    print(f"[{label}]   -> FAIL {msg} - continuing with the next tab")
                    runner.record_failure(f"Processing Option tab {tab}", str(po_err))
                    field_errors.append(msg)
                    await _diagnostic_screenshot(
                        page,
                        f"jde_po_fail_{report.get('app_report', 'unknown')}"
                        f"_{_norm_ws(tab).replace(' ', '_')}",
                    )
                    continue

            # Apply (OK button closes the Processing Options dialog)
            await runner.click("OK button", selector="#hc_Select", iframe=IFRAME, selector_strategy="css")

        # ── Done ────────────────────────────────────────────────────────
        warn_summary = "; ".join(field_warnings)
        if field_warnings:
            print(f"[{label}] ⚠ {len(field_warnings)} warning(s): {warn_summary}")

        # If any field had the wrong data type it was left unchanged and its
        # error recorded — report the iteration as failed (all other fields
        # were still processed) so the dashboard and report surface it.
        if field_errors:
            summary = "; ".join(field_errors)
            print(f"[{label}] ⚠ Completed with {len(field_errors)} field error(s): {summary}")
            return {
                "status": "fail",
                "error": f"{len(field_errors)} field(s) skipped: {summary}",
                "warnings": warn_summary,
                "report": report,
                "steps": list(runner.results),
            }

        # Warnings alone don't fail the iteration — they're recorded as
        # warning steps and surfaced in the report, and the run continues.
        print(f"[{label}] ✓ Completed successfully")
        return {
            "status": "pass",
            "error": None,
            "warnings": warn_summary,
            "report": report,
            "steps": list(runner.results),
        }

    except StepError as e:
        # Step-level failure (element not found, timeout, etc.) — stop this iteration,
        # let the caller (dashboard) move on to the next one.
        print(f"[{label}] ✖ FAILED: {e}")
        await _diagnostic_screenshot(page, f"jde_full_error_{report.get('app_report', 'unknown')}")
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
        await _diagnostic_screenshot(page, f"jde_full_unexpected_{report.get('app_report', 'unknown')}")
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
