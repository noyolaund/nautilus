"""Dashboard web app for JDE repetitive task automation.

Workflow:
1. Start Browser → login to JDE
2. Load Excel data → preview rows
3. Execute iterations → process each row
4. View report → see results
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, date
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from dashboard.session_manager import SessionManager
from data_provider.template_resolver import TemplateResolver
from models.schemas import TestSuiteRequest
from reports.html_report import generate_report
from models.schemas import (
    EngineType,
    SuiteResult,
    TestResult,
    TestStatus,
    StepResult,
)

# ---------------------------------------------------------------------------
# App state
# ---------------------------------------------------------------------------

_session = SessionManager()
_suite_request: Optional[TestSuiteRequest] = None
_execution_results: list[dict] = []
_row_paths: dict[int, str] = {}  # row_index → path name (full|a|b)
_report_groups: list[dict] = []  # per-column report groups ready for run_jde_full()
_login_completed: bool = False


# ---------------------------------------------------------------------------
# Path detection — choose which JSON to run for each row
# ---------------------------------------------------------------------------

PATH_TO_JSON: dict[str, str] = {
    "full": "tests/test_cases/jde_full.json",
    "a":    "tests/test_cases/jde_a_path.json",
    "b":    "tests/test_cases/jde_b_path.json",
}

# Excel layout — template exported directly from JDE.
#
#   Row 1:   Object Name (App/Report) in Column A — must start with R or P.
#   Row 3:   "Processing Options" label (A) + Current Version per report column
#   Row 4:   New Version per report column
#   Row 5:   New Version Title per report column
#   Row 6+:  Processing Options — Tab (A), Option label (B) + New value per
#            report column ...
#            ... until a separator row whose column A == "Data Selection" ...
#   then:    Data Selection rows — each report column (C+) holds the WHOLE
#            instruction as one string, e.g.
#              'BC Order Type (F4211)(DCTO) is equal to "DF; E2; KZ, SN"'
#            → left operand "Order Type", comparison "is equal to", value
#              "DF; E2; KZ, SN". (Columns A/B are unused for DS rows.)
#
# Each column from C onward is one report iteration. Processing Options come
# first (rows 6 → the "Data Selection" separator); Data Selections follow it.
JDE_OBJECT_NAME_ROW = 1       # Column A
JDE_META_ROW_CURRENT = 3      # Current Version, per report column (C+)
JDE_META_ROW_NEW = 4          # New Version, per report column (C+)
JDE_META_ROW_TITLE = 5        # New Version Title, per report column (C+)
JDE_DATA_START_ROW = 6        # Processing Options begin here
JDE_FIRST_REPORT_COL = 3      # column C
JDE_DS_SEPARATOR = "data selection"  # column-A marker → Data Selection section


def _extract_object_name(cell_values: list) -> str:
    """Pull the Object Name (App/Report) out of the first rows of the sheet.

    We accept any variation like:
        "Object Name: R4210IC"
        "Object Name : R4210IC"
        "R4210IC"                     (fallback — bare token)
    """
    for raw in cell_values:
        if raw is None:
            continue
        s = str(raw).strip()
        if not s:
            continue
        # Look for "Object Name: <TOKEN>"
        m = re.search(
            r"object\s*name\s*[:\-]?\s*([A-Za-z0-9_]+)",
            s,
            re.IGNORECASE,
        )
        if m:
            token = m.group(1).strip()
            if token and token.upper()[0] in ("R", "P"):
                return token
        # Otherwise: whole cell IS the token
        if re.match(r"^[RP][A-Za-z0-9_]+$", s):
            return s
    return ""


def _clean_left_operand(raw: str) -> str:
    """Normalize a JDE data-selection field name into a plain option label.

    The new Excel export names data selections like:

        "And BC Line Type (F411)(LNTY)"  ->  "Line Type"
        "BC Line Type (F411)(LNTY)"      ->  "Line Type"

    We strip, in order:
      - a leading boolean operator ("And"/"Or"), which is optional,
      - the leading two-letter section code that follows it ("BC"),
      - any parenthesized data-dictionary codes like "(F411)(LNTY)".

    The cleaned label is what we use to look up a Left Operand option.
    """
    if not raw:
        return ""
    s = str(raw)
    # Drop parenthesized codes anywhere, e.g. "(F411)(LNTY)"
    s = re.sub(r"\([^)]*\)", " ", s)
    # Drop a leading boolean operator ("And"/"Or"), if present
    s = re.sub(r"^\s*(?:and|or)\b\s*", "", s, flags=re.IGNORECASE)
    # Drop the leading two-letter section code (e.g. "BC")
    s = re.sub(r"^\s*[A-Za-z]{2}\b\s*", "", s)
    # Collapse leftover whitespace
    return re.sub(r"\s+", " ", s).strip()


def _po_label_first_segment(text: str) -> str:
    """Keep only the field-name portion of a Processing Option label (column B).

    JDE exports pack the option's help text after its name, separated by a run
    of 3+ spaces, e.g.:

        "5.  Prevent Next Status Update    Blank = Update next status  1 = ..."

    Only the leading segment ("5.  Prevent Next Status Update") names the field
    used to locate its text box, so we split on the first run of 3 or more
    spaces and keep the first part. Shorter gaps (like the "5.  " after the
    number) are preserved.
    """
    return re.split(r" {3,}", str(text or "").strip(), maxsplit=1)[0].strip()


# JDE comparison phrases as they appear inside a combined Data Selection cell.
# Ordered longest-first so "is greater than or equal to" is matched before
# "is greater than".
_JDE_COMPARISON_PHRASES: list[str] = [
    "is greater than or equal to",
    "is less than or equal to",
    "is not equal to",
    "is greater than",
    "is less than",
    "is equal to",
]


def _parse_ds_instruction(text: str) -> tuple[Optional[str], str, str]:
    """Split a combined Data Selection cell into (left_operand, comparison, value).

    In the current template each report column holds the whole instruction, e.g.

        'BC Order Type (F4211)(DCTO) is equal to "DF; E2; KZ, SN"'

    which splits into left operand "Order Type" (cleaned), comparison
    "is equal to", and value "DF; E2; KZ, SN" (surrounding quotes stripped).

    Returns (None, "", "") when the cell is blank. When no comparison phrase is
    present the whole prefix is treated as the operand and any trailing quoted
    chunk as the value.
    """
    s = str(text or "").strip()
    if not s:
        return None, "", ""

    low = s.lower()
    comparison = ""
    split_at = -1
    phrase_len = 0
    for phrase in _JDE_COMPARISON_PHRASES:
        i = low.find(phrase)
        if i != -1:
            comparison, split_at, phrase_len = phrase, i, len(phrase)
            break

    if split_at >= 0:
        left_raw = s[:split_at]
        value_raw = s[split_at + phrase_len:].strip()
    else:
        # No comparison phrase — trailing quoted chunk (if any) is the value.
        m = re.search(r'"([^"]*)"\s*$', s)
        if m:
            left_raw, value_raw = s[:m.start()], m.group(0)
        else:
            left_raw, value_raw = s, ""

    # Strip a single surrounding pair of double quotes from the value.
    value = value_raw.strip()
    vm = re.match(r'^"(.*)"$', value)
    value = (vm.group(1) if vm else value.strip('"')).strip()
    value = _normalize_date_string(value)

    return _clean_left_operand(left_raw), comparison, value


# ---------------------------------------------------------------------------
# Data Selection value behavior
# ---------------------------------------------------------------------------
#
# Every extracted Data Selection value is classified into an edit *behavior*:
#
#   "remove"    → delete the matching row (REMOVE and "Blank" both map here)
#   "zero"      → select the "Zero" option in the Right Operand combo box
#   "null"      → select the "Null" option in the Right Operand combo box
#   "datetoday" → select the "DateToday [SL]" option in the Right Operand combo
#                 box (Excel value "SL DateToday")
#   "literal"   → a concrete value written via the Literal editor (default)
#
# The required data *type* for a literal is NOT inferred from the Left Operand
# name — it is determined at execution time from JDE's active Literal tab
# (Single Value / Range of Values / List of Values). See the executor's
# detect_active_literal_tab / _literal_tab_type_error.


def classify_ds_behavior(value: str) -> str:
    """Classify an extracted Data Selection value into an edit behavior.

    The team wraps the sentinel keywords in angle brackets in the new Excel
    template (``<Zero>``, ``<Null>``); a single surrounding ``<...>`` pair is
    stripped before matching, so both the new bracketed form and the legacy
    bare form (``Zero``/``Null``) classify identically.
    """
    v = str(value or "").strip()
    # Strip one surrounding pair of angle brackets: "<Zero>" -> "Zero".
    token = re.sub(r"^<\s*(.+?)\s*>$", r"\1", v).strip()
    low = token.lower()
    if token.upper() == "REMOVE" or low == "blank":
        return "remove"
    if low == "zero":
        return "zero"
    if low == "null":
        return "null"
    # "SL DateToday" → select the "DateToday [SL]" Right Operand option.
    if re.sub(r"\s+", " ", low) == "sl datetoday":
        return "datetoday"
    return "literal"


def parse_jde_excel_export(file_path: str, sheet_name: str) -> tuple[list[dict], list[dict]]:
    """Parse the JDE-exported Excel file into report groups.

    Returns (report_groups, skipped) where each report_group has the same
    shape run_jde_full expects: {report, data_selections, processing_options}.

    Layout (see the module comment near JDE_OBJECT_NAME_ROW): Object Name in
    Row 1 / Column A; Current / New / Title in Rows 3 / 4 / 5 per report column
    (C+); Processing Options from Row 6 down to the "Data Selection" separator;
    Data Selection rows after it, each report column holding the whole
    instruction as one string.

    Raises ValueError when the file is not a valid template (Object Name in
    Row 1 / Column A missing or not starting with R/P).
    """
    from openpyxl import load_workbook

    wb = load_workbook(file_path, read_only=True, data_only=True)
    try:
        if sheet_name not in wb.sheetnames:
            raise ValueError(
                f"Sheet '{sheet_name}' not found. Available sheets: {wb.sheetnames}"
            )
        ws = wb[sheet_name]

        # Read the metadata rows (1..title) up-front.
        rows_by_index: dict[int, list] = {}
        for row_index, row in enumerate(
            ws.iter_rows(min_row=1, max_row=JDE_META_ROW_TITLE, values_only=True),
            start=1,
        ):
            rows_by_index[row_index] = list(row)

        # Object Name lives in Row 1 / Column A and MUST start with R or P;
        # otherwise this isn't a valid JDE template.
        row_object = rows_by_index.get(JDE_OBJECT_NAME_ROW, [])
        obj_cell = row_object[0] if len(row_object) > 0 else None
        app_report = _extract_object_name([obj_cell])
        if not app_report or not app_report.upper().startswith(("R", "P")):
            raise ValueError(
                "Not a valid JDE template: Object Name (Row 1, Column A) must be "
                "present and start with 'R' or 'P' "
                f"(found {str(obj_cell).strip()!r})"
            )

        # Per-column metadata rows.
        row_current = rows_by_index.get(JDE_META_ROW_CURRENT, [])
        row_new = rows_by_index.get(JDE_META_ROW_NEW, [])
        row_title = rows_by_index.get(JDE_META_ROW_TITLE, [])

        # From Row 6 down: Processing Options first, then — after a row whose
        # Column A == "Data Selection" — the Data Selection rows. Unlike PO
        # rows (Tab in Column A), a DS row keeps its whole instruction in the
        # per-report columns (C+), so Column A/B are ignored for DS.
        po_rows: list[dict] = []
        ds_rows: list[dict] = []
        in_ds_section = False
        for row_index, row in enumerate(
            ws.iter_rows(min_row=JDE_DATA_START_ROW, values_only=True),
            start=JDE_DATA_START_ROW,
        ):
            row = list(row)
            col_a = row[0] if len(row) > 0 else None
            a_str = str(col_a).strip() if col_a is not None else ""

            if not in_ds_section:
                # Separator → everything below is Data Selection.
                if a_str.lower() == JDE_DS_SEPARATOR:
                    in_ds_section = True
                    continue
                if not a_str:
                    continue  # PO rows need a Tab in Column A
                col_b = str(row[1]).strip() if len(row) > 1 and row[1] is not None else ""
                po_rows.append({
                    "row_index": row_index,
                    "row": row,
                    "tab": a_str,
                    # Keep only the field name; drop trailing help text that
                    # JDE separates with 3+ spaces.
                    "option_label": _po_label_first_segment(col_b),
                })
            else:
                # DS row — the instruction lives in the report columns (C+),
                # so keep the row if any of those cells has content.
                if any(
                    c is not None and str(c).strip()
                    for c in row[JDE_FIRST_REPORT_COL - 1:]
                ):
                    ds_rows.append({"row_index": row_index, "row": row})

        # Determine how many report columns we have (columns C..N).
        max_len = max(
            len(row_current), len(row_new), len(row_title),
            *[len(r["row"]) for r in ds_rows],
            *[len(r["row"]) for r in po_rows],
            JDE_FIRST_REPORT_COL,
        )
        report_groups: list[dict] = []
        skipped: list[dict] = []

        # Excel columns are 1-indexed; column C = index 2 in a zero-based list
        for col_idx0 in range(JDE_FIRST_REPORT_COL - 1, max_len):
            col_letter = _col_letter(col_idx0 + 1)
            current = _cell(row_current, col_idx0)
            new_ver = _cell(row_new, col_idx0)
            title = _cell(row_title, col_idx0)

            # Row 4 (New Version) decides the mode PER report column:
            #   text  → copy current_version into this new version.
            #   empty → edit current_version in place.
            is_copy_mode = bool(new_ver)

            # Collect the Data Selection instructions for this report column.
            # Each cell holds the full "<operand> <comparison> "<value>"" string.
            # Occurrence is counted PER COLUMN among the entries actually filled
            # here (not globally): each column is a distinct JDE version, so its
            # Nth filled duplicate operand maps to the Nth matching grid row.
            data_selections: list[dict] = []
            col_occurrence: dict[str, int] = {}
            for r in ds_rows:
                raw = r["row"][col_idx0] if col_idx0 < len(r["row"]) else None
                left_operand, comparison, value = _parse_ds_instruction(raw)
                if not left_operand or not value:
                    continue
                lo_key = left_operand.lower()
                col_occurrence[lo_key] = col_occurrence.get(lo_key, 0) + 1
                data_selections.append({
                    "left_operand": left_operand,
                    "comparison": comparison,
                    "data_new": value,
                    "behavior": classify_ds_behavior(value),
                    "occurrence": col_occurrence[lo_key],
                    "_source_row": r["row_index"],
                })

            # Collect the Processing Options for this report column. Tab = col A,
            # option label = col B, New Value = this report's column. A blank
            # cell means "do nothing for this option in this report".
            processing_options: list[dict] = []
            for r in po_rows:
                val = _cell(r["row"], col_idx0)
                if val is None or not str(val).strip():
                    continue
                processing_options.append({
                    "tab": r["tab"],
                    "option_label": r["option_label"],
                    "processing_new": str(val).strip(),
                    "_source_row": r["row_index"],
                })

            # Skip completely empty columns (no metadata and no DS/PO values).
            if not any([current, new_ver, title]) and \
                    not data_selections and not processing_options:
                continue

            # Both modes need the version to open (Row 3).
            if not current:
                skipped.append({
                    "row": col_letter,
                    "app_report": app_report,
                    "reason": (
                        f"Column {col_letter} missing "
                        f"{'current version' if is_copy_mode else 'version to edit'} (Row 3)"
                    ),
                })
                continue

            report_groups.append({
                "row_index": col_letter,  # keep letter for the UI preview
                "report": {
                    "app_report": app_report,
                    "current_version": str(current).strip(),
                    "new_version": str(new_ver).strip() if new_ver else "",
                    "new_version_title": str(title).strip() if title else "",
                    # True → copy current_version into new_version (default
                    # workflow). False → edit current_version in place.
                    "copy_version": is_copy_mode,
                },
                "data_selections": data_selections,
                "processing_options": processing_options,
            })

        return report_groups, skipped
    finally:
        wb.close()


_DATE_MDY = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$")
_DATE_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T]00:00:00)?$")


def _normalize_date_string(s: str) -> str:
    """Reformat a date-looking string to JDE's DD/MM/YY.

    The Excel export uses US order MM/DD/YYYY (07/26/2026); JDE expects
    DD/MM/YY (26/07/26). Also accepts ISO (2026-07-26, optionally with a
    midnight time). Returns *s* unchanged if it isn't a valid date.
    """
    m = _DATE_MDY.match(s)
    if m:
        mm, dd, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = _DATE_ISO.match(s)
        if not m:
            return s
        yyyy, mm, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return datetime(yyyy, mm, dd).strftime("%d/%m/%y")
    except ValueError:
        return s


def _cell(row: list, idx0: int):
    """Safely read row[idx0], return None if out of range or empty-ish.

    Date cells are normalized to JDE's DD/MM/YY format. openpyxl returns real
    Excel dates as datetime objects whose str() is an ISO
    "2026-07-26 00:00:00" — JDE mis-parses that into e.g. "7-26-2026". The Excel
    export writes dates as MM/DD/YYYY, so we format date objects and reshape
    date-looking text to DD/MM/YY so JDE stores the date correctly.
    """
    if idx0 >= len(row):
        return None
    v = row[idx0]
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.strftime("%d/%m/%y")
    s = str(v).strip()
    if not s:
        return None
    return _normalize_date_string(s)


def _col_letter(col_num: int) -> str:
    """1 → 'A', 27 → 'AA', ..."""
    letters = ""
    while col_num > 0:
        col_num, rem = divmod(col_num - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters



def create_dashboard_app() -> FastAPI:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await _session.stop()

    app = FastAPI(
        title="JDE Automation Dashboard",
        version="1.0.0",
        lifespan=lifespan,
    )

    # --- Serve the frontend -------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index():
        html_path = Path(__file__).parent / "index.html"
        return HTMLResponse(html_path.read_text(encoding="utf-8"))

    # --- Session endpoints --------------------------------------------------

    @app.post("/api/session/start")
    async def start_browser():
        """Launch the browser. Returns a clear error if launch fails."""
        try:
            return await _session.start_browser()
        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            _session.logger.error("Browser start failed: %s\n%s", exc, tb)
            # Make sure any partially-initialized state is cleaned up
            try:
                await _session.stop()
            except Exception:
                pass
            raise HTTPException(
                status_code=500,
                detail=f"Failed to launch browser: {type(exc).__name__}: {exc}",
            )

    @app.post("/api/session/login")
    async def login(request: Request):
        """Run login_assert.json — login only, no Excel data involved."""
        global _suite_request, _login_completed

        login_path = "tests/test_cases/login_assert.json"
        if not Path(login_path).exists():
            raise HTTPException(status_code=400, detail=f"Login suite not found: {login_path}")

        login_raw = json.loads(Path(login_path).read_text(encoding="utf-8"))
        # Login suite has no _data_source by design; strip it if present anyway
        login_raw.pop("_data_source", None)

        # Override URL + credentials with values from .env so changing them
        # there takes effect without editing login_assert.json.
        jde_url = os.getenv("JDE_URL", "").strip()
        jde_user = os.getenv("JDE_USERNAME", "").strip()
        jde_pass = os.getenv("JDE_PASSWORD", "").strip()

        for tc in login_raw.get("test_cases", []):
            if jde_url:
                tc["base_url"] = jde_url
            for step in tc.get("steps", []):
                action = step.get("action")
                name = (step.get("name") or "").lower()

                # navigate → JDE URL
                if action == "navigate" and jde_url:
                    step.setdefault("data", {})
                    step["data"]["value"] = jde_url

                # type step whose name mentions "user" → username
                elif action == "type" and "user" in name and jde_user:
                    step.setdefault("data", {})
                    step["data"]["value"] = jde_user

                # type step whose name mentions "password" → password
                elif action == "type" and "password" in name and jde_pass:
                    step.setdefault("data", {})
                    step["data"]["value"] = jde_pass

        _session.logger.info(
            "Login overrides from .env — url=%s user=%s pass=%s",
            jde_url or "(not set)",
            jde_user or "(not set)",
            "****" if jde_pass else "(not set)",
        )
        if not jde_url or not jde_user or not jde_pass:
            _session.logger.warning(
                "Some JDE_* values missing in .env — login_assert.json values "
                "will be used for the missing ones."
            )

        login_suite = TestSuiteRequest(**login_raw)
        login_suite.headless = False
        _suite_request = login_suite

        result = await _session.run_login(login_suite)
        _login_completed = bool(result.get("logged_in"))
        return result

    @app.post("/api/session/stop")
    async def stop_browser():
        """Close the browser and reset all dashboard state."""
        global _suite_request
        global _execution_results, _row_paths, _report_groups, _login_completed
        result = await _session.stop()
        # Wipe server-side state so the dashboard is ready for a fresh run
        _suite_request = None
        _execution_results = []
        _row_paths = {}
        _report_groups = []
        _login_completed = False
        return result

    @app.get("/api/session/status")
    async def session_status():
        """Get current session state."""
        return {
            "browser_active": _session.is_active,
            "logged_in": _session.is_logged_in,
            "suite_loaded": _suite_request is not None,
            "data_loaded": bool(_report_groups),
            "data_rows": len(_report_groups),
            "executions_completed": len(_execution_results),
        }

    # --- Data endpoints -----------------------------------------------------

    @app.post("/api/data/sheets")
    async def list_sheets(file: UploadFile = File(...)):
        """Return the list of sheet names in the uploaded xlsx file.

        Used to populate the sheet-name combo box in the dashboard before
        the user picks which sheet to parse.
        """
        if not file.filename.lower().endswith(".xlsx"):
            raise HTTPException(status_code=400, detail="Only .xlsx files accepted")

        try:
            content = await file.read()
            if len(content) > 50 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="File too large (max 50 MB)")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to read upload: {exc}")

        # Open the workbook from the in-memory bytes and list sheet names
        try:
            from openpyxl import load_workbook
            from io import BytesIO
            wb = load_workbook(BytesIO(content), read_only=True, data_only=True)
            sheets = list(wb.sheetnames)
            wb.close()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Could not read xlsx: {exc}")

        return {"filename": file.filename, "sheets": sheets}

    @app.post("/api/data/upload")
    async def upload_excel(
        file: UploadFile = File(...),
        sheet_name: str = Form("Sheet1"),
    ):
        """Upload an xlsx file, save to temp, and parse it."""
        if not file.filename.lower().endswith(".xlsx"):
            raise HTTPException(status_code=400, detail="Only .xlsx files accepted")

        if not _login_completed:
            raise HTTPException(status_code=400, detail="Run Start Browser & Login first.")

        # Write the upload to a TEMP file (deleted after parsing) so the
        # run-folder doesn't fill up with xlsx copies the user doesn't want.
        import tempfile
        try:
            content = await file.read()
            if len(content) > 50 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="File too large (max 50 MB)")
            tf = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
            try:
                tf.write(content)
                tf.flush()
                saved_path = Path(tf.name)
            finally:
                tf.close()
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to save upload: {exc}")

        # Parse the JDE-exported workbook using the format-specific parser
        # (columns C..N are one report iteration each; metadata is in rows 2-4).
        try:
            groups, skipped_rows = parse_jde_excel_export(
                str(saved_path), sheet_name.strip() or "Sheet1"
            )
        except Exception as exc:
            import traceback
            err = f"{type(exc).__name__}: {exc}"
            _session.logger.error("Excel parse error: %s\n%s", err, traceback.format_exc())
            raise HTTPException(status_code=400, detail=f"Failed to parse Excel: {err}")
        finally:
            # Always remove the temp xlsx — the data is already in memory
            try:
                saved_path.unlink(missing_ok=True)
            except Exception:
                pass

        global _report_groups
        _report_groups = groups

        # Log a per-column summary for debugging
        for g in _report_groups:
            _session.logger.info(
                "Report col %s: app=%s current=%s new=%s mode=%s DS=%d PO=%d",
                g["row_index"],
                g["report"].get("app_report"),
                g["report"].get("current_version"),
                g["report"].get("new_version"),
                "copy" if g["report"].get("copy_version", True) else "edit",
                len(g["data_selections"]),
                len(g["processing_options"]),
            )

        # Build a flat preview — one row per report group (column)
        preview = []
        for group in _report_groups:
            report = group["report"]
            preview.append({
                "_row": group["row_index"],
                "app_report": report.get("app_report", ""),
                "current_version": report.get("current_version", ""),
                "new_version": report.get("new_version", ""),
                "new_version_title": report.get("new_version_title", ""),
                "copy_version": report.get("copy_version", True),
                "data_selections_count": len(group["data_selections"]),
                "processing_options_count": len(group["processing_options"]),
            })

        return {
            "status": "success",
            "filename": file.filename,
            "rows": len(_report_groups),
            "skipped_rows": skipped_rows,
            "skipped_count": len(skipped_rows),
            "preview": preview,
        }

    @app.get("/api/data/preview")
    async def data_preview():
        """Get the loaded data preview."""
        if not _report_groups:
            raise HTTPException(status_code=400, detail="No data loaded")
        preview = []
        for g in _report_groups:
            r = g["report"]
            preview.append({
                "_row": g["row_index"],
                "app_report": r.get("app_report", ""),
                "current_version": r.get("current_version", ""),
                "new_version": r.get("new_version", ""),
                "new_version_title": r.get("new_version_title", ""),
                "copy_version": r.get("copy_version", True),
                "data_selections_count": len(g["data_selections"]),
                "processing_options_count": len(g["processing_options"]),
            })
        return {"rows": len(_report_groups), "preview": preview}

    # --- Execution endpoints ------------------------------------------------

    @app.post("/api/execute")
    async def execute_all():
        """Run iterations: each report group runs the JDE Full Path Python flow."""
        global _execution_results
        import time
        from tests.test_jde_full import run_jde_full

        if not _session.is_logged_in:
            raise HTTPException(status_code=400, detail="Not logged in. Run login first.")
        if not _report_groups:
            raise HTTPException(status_code=400, detail="No data loaded. Load Excel first.")

        _execution_results = []
        total = len(_report_groups)
        page = _session._page  # the persistent logged-in page

        if page is None:
            raise HTTPException(status_code=500, detail="Browser page is not available")

        for i, group in enumerate(_report_groups, 1):
            report = group["report"]
            label = f"{report.get('app_report', '?')} → {report.get('new_version', '?')}"
            print(f"\n=== Iteration {i}/{total}: {label} ===")
            print(f"   Data selections: {len(group['data_selections'])}, Processing options: {len(group['processing_options'])}")

            start = time.monotonic()
            steps_raw = []  # list of StepResult objects from the runner
            try:
                result = await run_jde_full(page, group)
                status = result.get("status", "fail")
                error = result.get("error")
                steps_raw = result.get("steps") or []
            except Exception as exc:
                import traceback
                _session.logger.error("Iteration %d crashed: %s\n%s", i, exc, traceback.format_exc())
                status = "fail"
                error = f"Unhandled exception: {exc}"

            duration_ms = (time.monotonic() - start) * 1000

            # Convert every StepResult into a JSON-friendly dict for the report
            steps_dicts = []
            total_tokens = 0
            for s in steps_raw:
                total_tokens += getattr(s, "tokens_used", 0) or 0
                steps_dicts.append({
                    "step_id": s.step_id,
                    "name": s.name,
                    "action": s.action.value if hasattr(s.action, "value") else str(s.action),
                    "status": s.status.value if hasattr(s.status, "value") else str(s.status),
                    "duration_ms": round(s.duration_ms or 0),
                    "tokens_used": s.tokens_used or 0,
                    "error": s.error_message,
                    "selector": s.resolved_selector,
                    "started_at": s.started_at.isoformat() if s.started_at else None,
                    "finished_at": s.finished_at.isoformat() if s.finished_at else None,
                })

            # If the iteration failed before any step ran (e.g. crash), surface
            # the error as a synthetic step so the report still shows something.
            if not steps_dicts and status == "fail":
                steps_dicts.append({
                    "step_id": "S000",
                    "name": "Iteration crashed",
                    "action": "custom",
                    "status": "fail",
                    "duration_ms": round(duration_ms),
                    "tokens_used": 0,
                    "error": error,
                    "selector": None,
                })

            _execution_results.append({
                "iteration": i,
                "total": total,
                "test_id": report.get("app_report", "?"),
                "name": label,
                "status": status,
                "duration_ms": round(duration_ms),
                "tokens": total_tokens,
                "screenshot": "",
                "data_selections_count": len(group["data_selections"]),
                "processing_options_count": len(group["processing_options"]),
                "steps": steps_dicts,
            })

        # Generate report
        report_path = _generate_execution_report()

        passed = sum(1 for r in _execution_results if r["status"] == "pass")
        failed = total - passed

        return {
            "status": "completed",
            "total": total,
            "passed": passed,
            "failed": failed,
            "report_path": report_path,
            "results": _execution_results,
        }

    @app.get("/api/execute/results")
    async def get_results():
        """Get execution results."""
        if not _execution_results:
            return {"status": "no_results", "results": []}

        passed = sum(1 for r in _execution_results if r["status"] == "pass")
        return {
            "status": "completed",
            "total": len(_execution_results),
            "passed": passed,
            "failed": len(_execution_results) - passed,
            "results": _execution_results,
        }

    @app.get("/api/report")
    async def get_report():
        """Get the HTML report."""
        if not _execution_results:
            raise HTTPException(status_code=400, detail="No execution results")

        report_path = _generate_execution_report()
        html = Path(report_path).read_text(encoding="utf-8")
        return HTMLResponse(html)

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _generate_execution_report() -> str:
    """Build a SuiteResult from execution results and generate HTML report."""
    if not _suite_request or not _execution_results:
        return ""

    from models.schemas import ActionType
    from datetime import datetime as _dt

    def _parse_action(value: str) -> ActionType:
        try:
            return ActionType(value)
        except Exception:
            return ActionType.CUSTOM

    def _parse_ts(value):
        if not value:
            return None
        try:
            return _dt.fromisoformat(value)
        except Exception:
            return None

    test_results = []
    for r in _execution_results:
        steps = [
            StepResult(
                step_id=s["step_id"],
                name=s["name"],
                action=_parse_action(s.get("action", "custom")),
                status=s["status"],
                duration_ms=s.get("duration_ms", 0),
                tokens_used=s.get("tokens_used", 0),
                error_message=s.get("error"),
                resolved_selector=s.get("selector"),
                started_at=_parse_ts(s.get("started_at")),
                finished_at=_parse_ts(s.get("finished_at")),
            )
            for s in r.get("steps", [])
        ]
        test_results.append(TestResult(
            test_id=f"{r['test_id']}_iter{r['iteration']}",
            name=r["name"],
            status=r["status"],
            platform=_suite_request.test_cases[0].platform if _suite_request.test_cases else "generic_web",
            steps=steps,
            duration_ms=r.get("duration_ms", 0),
            total_tokens=r.get("tokens", 0),
        ))

    suite_result = SuiteResult(
        suite_id=_suite_request.suite_id,
        suite_name=_suite_request.suite_name,
        environment=_suite_request.environment,
        browser=_suite_request.browser,
        engine_type=EngineType.HYBRID,
        llm_provider=_suite_request.llm_provider,
        llm_model=_suite_request.llm_model,
        test_results=test_results,
        started_at=datetime.now(),
        finished_at=datetime.now(),
        total_duration_ms=sum(r.get("duration_ms", 0) for r in _execution_results),
        total_tokens=sum(r.get("tokens", 0) for r in _execution_results),
    )

    run_dir = _session.run_dir or Path("logs")
    return generate_report(suite_result, output_dir=str(run_dir), filename="report.html")


dashboard_app = create_dashboard_app()
