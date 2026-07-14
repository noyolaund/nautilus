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
from datetime import datetime
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

# Excel layout — new format exported directly from JDE.
#
#   Row 1:   (blank or free-form header)
#   Row 2:   Object Name (A/B) + New Version Title per report column
#   Row 3:   "Copy from" label (A) + Current Version per report column
#   Row 4:   "DS Field" (A), "DATA SELECTION" (B) + New Version per report column
#   Row 5+:  Left Operand (A), Comparison (B) + New DS value per report column
#
# Each column from C onward is one report iteration. Data Selections are
# rows 5..N in that column; row A tells us the field, B tells us the
# comparison operator (used when we add a brand-new DS row in JDE).
JDE_META_ROW_TITLE = 2
JDE_META_ROW_CURRENT = 3
JDE_META_ROW_NEW = 4
JDE_DATA_START_ROW = 5
JDE_FIRST_REPORT_COL = 3  # column C


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


# ---------------------------------------------------------------------------
# Data Selection value rules — behavior + per-field format validation
# ---------------------------------------------------------------------------
#
# Every extracted Data Selection value is classified into an edit *behavior*:
#
#   "remove"  → delete the matching row (handled by the REMOVE branch)
#   "on_hold" → Blank / Zero / Null; a JDE flow that is defined later
#   "literal" → a concrete value written via the Literal editor (default)
#
# Only "literal" values are format-checked against the field's rule below.
# A malformed literal is flagged (valid=False) so the executor can skip that
# single Data Selection instead of writing a bad value into JDE.

# Excel values that map to the (still-to-be-defined) on-hold flow.
# Compared case-insensitively.
_ON_HOLD_VALUES: set[str] = {"blank", "zero", "null"}


def classify_ds_behavior(value: str) -> str:
    """Classify an extracted Data Selection value into an edit behavior."""
    v = str(value or "").strip()
    if v.upper() == "REMOVE":
        return "remove"
    if v.lower() in _ON_HOLD_VALUES:
        return "on_hold"
    return "literal"


def _ds_tokens(value: str) -> list[str]:
    """Split a ';'-separated value into trimmed, non-empty tokens."""
    return [t.strip() for t in str(value).split(";") if t.strip()]


def _rule_code_list(min_len: int, max_len: int):
    """Build a validator: one or more alphanumeric codes of the given length,
    separated by ';' (e.g. Order Type 'SA; SF', Line Type 'S; W2')."""
    def check(value: str) -> bool:
        toks = _ds_tokens(value)
        return bool(toks) and all(
            t.isalnum() and min_len <= len(t) <= max_len for t in toks
        )
    return check


def _rule_int_range(value: str) -> bool:
    """Validator: an integer range 'N - M' (e.g. Status Code '520 - 600')."""
    return bool(re.fullmatch(r"\s*\d+\s*-\s*\d+\s*", str(value)))


def _rule_single_int(value: str) -> bool:
    """Validator: a single non-negative integer."""
    return bool(re.fullmatch(r"\s*\d+\s*", str(value)))


def _rule_string(value: str) -> bool:
    """Validator: any non-empty string."""
    return bool(str(value).strip())


# Per-field rules keyed by the cleaned Left Operand name (see
# _clean_left_operand). Each entry is (validator, human-readable expectation).
# Left Operands not listed here are not format-checked.
_FIELD_RULES: dict[str, tuple] = {
    "order type":           (_rule_code_list(2, 2), "one or more 2-character codes separated by ';' (e.g. 'SA; SF')"),
    "line type":            (_rule_code_list(1, 2), "one or more 1-2 character codes separated by ';' (e.g. 'S; W2')"),
    "status code":          (_rule_int_range,       "an integer range 'N - M' (e.g. '520 - 600')"),
    "inter branch sales":   (_rule_single_int,      "a single integer"),
    "order company":        (_rule_single_int,      "a single integer"),
    "original document no": (_rule_single_int,      "a single integer"),
    "tax rate/area":        (_rule_string,          "a non-empty string"),
}


def validate_ds_value(left_operand: str, value: str) -> tuple[bool, str]:
    """Validate a Literal Data Selection value against its field rule.

    Returns (ok, message). Left Operands without a rule are accepted (ok=True,
    empty message).
    """
    key = str(left_operand or "").strip().lower()
    rule = _FIELD_RULES.get(key)
    if rule is None:
        return True, ""
    check, expectation = rule
    if check(value):
        return True, ""
    return False, f"{left_operand!r} expects {expectation}; got {value!r}"


def parse_jde_excel_export(file_path: str, sheet_name: str) -> tuple[list[dict], list[dict]]:
    """Parse the JDE-exported Excel file into report groups.

    Returns (report_groups, skipped) where each report_group has the same
    shape run_jde_full expects: {report, data_selections, processing_options}.
    Processing Options are always empty in this format (feature on hold).
    """
    from openpyxl import load_workbook

    wb = load_workbook(file_path, read_only=True, data_only=True)
    try:
        if sheet_name not in wb.sheetnames:
            raise ValueError(
                f"Sheet '{sheet_name}' not found. Available sheets: {wb.sheetnames}"
            )
        ws = wb[sheet_name]

        # Read the first 4 rows in full (for Object Name detection + metadata)
        header_cells: list = []
        rows_by_index: dict[int, list] = {}
        for row_index, row in enumerate(
            ws.iter_rows(min_row=1, max_row=JDE_META_ROW_NEW, values_only=True), start=1
        ):
            rows_by_index[row_index] = list(row)
            for cell in row:
                header_cells.append(cell)

        app_report = _extract_object_name(header_cells)

        # Metadata rows
        row_title = rows_by_index.get(JDE_META_ROW_TITLE, [])
        row_current = rows_by_index.get(JDE_META_ROW_CURRENT, [])
        row_new = rows_by_index.get(JDE_META_ROW_NEW, [])

        # Data-selection rows: read as many as we can, keep only those with
        # a non-empty Left Operand in column A.
        ds_rows: list[dict] = []
        for row_index, row in enumerate(
            ws.iter_rows(min_row=JDE_DATA_START_ROW, values_only=True),
            start=JDE_DATA_START_ROW,
        ):
            row = list(row)
            left = row[0] if len(row) > 0 else None
            if left is None or not str(left).strip():
                continue
            ds_rows.append({
                "row_index": row_index,
                "row": row,
                "left_operand": _clean_left_operand(str(left)),
                "comparison": str(row[1]).strip() if len(row) > 1 and row[1] is not None else "",
            })

        # Determine how many report columns we have (columns C..N).
        # A column is considered "present" if any of Row 2/3/4 has data.
        # Grow the search up to the widest row.
        max_len = max(
            len(row_title), len(row_current), len(row_new),
            *[len(r["row"]) for r in ds_rows], JDE_FIRST_REPORT_COL,
        )
        report_groups: list[dict] = []
        skipped: list[dict] = []

        # Excel columns are 1-indexed; column C = index 2 in a zero-based list
        for col_idx0 in range(JDE_FIRST_REPORT_COL - 1, max_len):
            col_letter = _col_letter(col_idx0 + 1)
            title = _cell(row_title, col_idx0)
            current = _cell(row_current, col_idx0)
            new_ver = _cell(row_new, col_idx0)

            # Skip completely empty columns
            if not any([title, current, new_ver]):
                # Also check if this column has ANY DS values — if it does,
                # something is off. Otherwise just skip.
                if not any(_cell(r["row"], col_idx0) for r in ds_rows):
                    continue

            if not new_ver or not current:
                skipped.append({
                    "row": col_letter,
                    "app_report": app_report,
                    "reason": (
                        f"Column {col_letter} missing "
                        f"{'current version' if not current else 'new version'}"
                    ),
                })
                continue

            if app_report and not app_report.upper().startswith(("R", "P")):
                skipped.append({
                    "row": col_letter,
                    "app_report": app_report,
                    "reason": "App/Report must start with 'R' or 'P'",
                })
                continue

            # Collect data selections for this report column
            data_selections: list[dict] = []
            for r in ds_rows:
                val = _cell(r["row"], col_idx0)
                if val is None or not str(val).strip():
                    continue
                data_new = str(val).strip()

                # Classify behavior and, for Literal values, validate the
                # value against the field's format rule. on_hold / remove
                # values are not format-checked.
                behavior = classify_ds_behavior(data_new)
                if behavior == "literal":
                    valid, validation_message = validate_ds_value(
                        r["left_operand"], data_new
                    )
                else:
                    valid, validation_message = True, ""

                data_selections.append({
                    "left_operand": r["left_operand"],
                    "comparison": r["comparison"],
                    "data_new": data_new,
                    "behavior": behavior,
                    "valid": valid,
                    "validation_message": validation_message,
                    "_source_row": r["row_index"],
                })

            report_groups.append({
                "row_index": col_letter,  # keep letter for the UI preview
                "report": {
                    "app_report": app_report,
                    "current_version": str(current).strip(),
                    "new_version": str(new_ver).strip(),
                    "new_version_title": str(title).strip() if title else "",
                },
                "data_selections": data_selections,
                "processing_options": [],  # deprecated / on hold
            })

        return report_groups, skipped
    finally:
        wb.close()


def _cell(row: list, idx0: int):
    """Safely read row[idx0], return None if out of range or empty-ish."""
    if idx0 >= len(row):
        return None
    v = row[idx0]
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


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
                "Report col %s: app=%s current=%s new=%s DS=%d",
                g["row_index"],
                g["report"].get("app_report"),
                g["report"].get("current_version"),
                g["report"].get("new_version"),
                len(g["data_selections"]),
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
                "data_selections_count": len(group["data_selections"]),
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
                "data_selections_count": len(g["data_selections"]),
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
