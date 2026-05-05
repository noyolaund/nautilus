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
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from dashboard.session_manager import SessionManager
from data_provider.excel_parser import ExcelParser
from data_provider.data_models import DataSourceConfig, DataContext, DataRow
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
_data_context: Optional[DataContext] = None
_data_source_config: Optional[DataSourceConfig] = None
_execution_results: list[dict] = []
_row_paths: dict[int, str] = {}  # row_index → path name (full|a|b)
_report_groups: list[dict] = []  # grouped Excel rows ready for run_jde_full()
_login_completed: bool = False


# ---------------------------------------------------------------------------
# Path detection — choose which JSON to run for each row
# ---------------------------------------------------------------------------

PATH_TO_JSON: dict[str, str] = {
    "full": "tests/test_cases/jde_full.json",
    "a":    "tests/test_cases/jde_a_path.json",
    "b":    "tests/test_cases/jde_b_path.json",
}

# Excel column mapping — fixed business contract for this workflow
#   A=user_story, B=app_report, C=current_version, D=new_version,
#   E=current_version_title, F=new_version_title,
#   G=left_operand (for path detection),
#   H=data_new, I=tab (for path detection),
#   J=option_number, K=processing_new
EXCEL_COLUMN_MAPPINGS: list[dict] = [
    {"column": "A", "variable_name": "user_story",           "data_type": "string", "required": False},
    {"column": "B", "variable_name": "app_report",           "data_type": "string", "required": True},
    {"column": "C", "variable_name": "current_version",      "data_type": "string", "required": True},
    {"column": "D", "variable_name": "new_version",          "data_type": "string", "required": True},
    {"column": "E", "variable_name": "current_version_title","data_type": "string", "required": False},
    {"column": "F", "variable_name": "new_version_title",    "data_type": "string", "required": True},
    {"column": "G", "variable_name": "left_operand",         "data_type": "string", "required": False},
    {"column": "H", "variable_name": "data_new",             "data_type": "string", "required": False},
    {"column": "I", "variable_name": "tab",                  "data_type": "string", "required": False},
    {"column": "J", "variable_name": "option_number",        "data_type": "string", "required": False},
    {"column": "K", "variable_name": "processing_new",       "data_type": "string", "required": False},
]


def _build_data_source_config(sheet_name: str) -> DataSourceConfig:
    """Build the Excel DataSourceConfig from the fixed dashboard contract."""
    return DataSourceConfig(**{
        "source_id": "jde_report_versions",
        "source_type": "excel_local",
        "excel": {
            "sheets": [{
                "sheet_name": sheet_name,
                "header_row": 1,
                "data_start_row": 2,
                "column_mappings": EXCEL_COLUMN_MAPPINGS,
            }]
        },
        "iteration": {
            "mode": "all_rows",
            "sheet_name": sheet_name,
            "max_rows": 500,
        },
    })


def _cell_has_value(val) -> bool:
    """True if a cell actually contains data (not None, not empty, not literal 'None')."""
    if val is None:
        return False
    s = str(val).strip()
    return bool(s) and s.lower() != "none"


def detect_path(row_values: dict) -> Optional[str]:
    """Return 'full', 'a', 'b', or None based on G (left_operand) and I (tab) columns.

    - Full path: G has data AND I has data
    - A path:    G has data AND I is empty
    - B path:    G is empty AND I has data
    - None:      both empty (row will be skipped)
    """
    g = _cell_has_value(row_values.get("left_operand"))
    i = _cell_has_value(row_values.get("tab"))
    if g and i:
        return "full"
    if g and not i:
        return "a"
    if not g and i:
        return "b"
    return None


def group_excel_rows(rows: list) -> tuple[list[dict], list[dict]]:
    """Group consecutive Excel rows into report groups.

    A row whose column B (`app_report`) starts with R or P STARTS a new group
    (the report row). Subsequent rows whose `app_report` is empty but have G/H
    or I/J/K data are CONTINUATION rows that add more data selections /
    processing options to the previous group.

    Returns: (groups, skipped)
    """
    groups: list[dict] = []
    skipped: list[dict] = []
    current: Optional[dict] = None

    def _has_ds(values: dict) -> bool:
        return _cell_has_value(values.get("left_operand"))

    def _has_po(values: dict) -> bool:
        return _cell_has_value(values.get("tab")) or _cell_has_value(values.get("processing_new"))

    for r in rows:
        values = r.values
        app_report = str(values.get("app_report", "") or "").strip().upper()

        if app_report and (app_report.startswith("R") or app_report.startswith("P")):
            # New report — finalize previous group, start a fresh one
            current = {
                "row_index": r.row_index,
                "report": dict(values),
                "data_selections": [],
                "processing_options": [],
            }
            if _has_ds(values):
                current["data_selections"].append({
                    "left_operand": values.get("left_operand", ""),
                    "data_new": values.get("data_new", ""),
                })
            if _has_po(values):
                current["processing_options"].append({
                    "tab": values.get("tab", ""),
                    "option_number": values.get("option_number", ""),
                    "processing_new": values.get("processing_new", ""),
                })
            groups.append(current)
        elif app_report:
            # Has app_report but doesn't start with R or P
            skipped.append({
                "row": r.row_index,
                "app_report": app_report,
                "reason": "Column B must start with 'R' or 'P'",
            })
        elif current is not None and (_has_ds(values) or _has_po(values)):
            # Continuation row — append data selection / processing option to current group
            if _has_ds(values):
                current["data_selections"].append({
                    "left_operand": values.get("left_operand", ""),
                    "data_new": values.get("data_new", ""),
                })
            if _has_po(values):
                current["processing_options"].append({
                    "tab": values.get("tab", ""),
                    "option_number": values.get("option_number", ""),
                    "processing_new": values.get("processing_new", ""),
                })
        else:
            # Empty row with no app_report and no DS/PO data
            skipped.append({
                "row": r.row_index,
                "app_report": app_report,
                "reason": "Empty row (no app_report, no data selection, no processing option)",
            })

    return groups, skipped


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
        """Launch the browser."""
        return await _session.start_browser()

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

        login_suite = TestSuiteRequest(**login_raw)
        login_suite.headless = False
        _suite_request = login_suite

        result = await _session.run_login(login_suite)
        _login_completed = bool(result.get("logged_in"))
        return result

    @app.post("/api/session/stop")
    async def stop_browser():
        """Close the browser and reset all dashboard state."""
        global _suite_request, _data_context, _data_source_config
        global _execution_results, _row_paths, _report_groups, _login_completed
        result = await _session.stop()
        # Wipe server-side state so the dashboard is ready for a fresh run
        _suite_request = None
        _data_context = None
        _data_source_config = None
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
            "data_loaded": _data_context is not None,
            "data_rows": _data_context.total_rows if _data_context else 0,
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
        global _data_context, _data_source_config

        if not file.filename.lower().endswith(".xlsx"):
            raise HTTPException(status_code=400, detail="Only .xlsx files accepted")

        if not _login_completed:
            raise HTTPException(status_code=400, detail="Run Start Browser & Login first.")

        # Save uploaded file to the run dir (or a temp location)
        run_dir = _session.run_dir or Path("logs")
        run_dir.mkdir(parents=True, exist_ok=True)
        saved_path = run_dir / f"uploaded_{file.filename}"

        try:
            content = await file.read()
            if len(content) > 50 * 1024 * 1024:  # 50 MB safety limit
                raise HTTPException(status_code=413, detail="File too large (max 50 MB)")
            saved_path.write_bytes(content)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to save upload: {exc}")

        # Parse and filter using the dashboard's fixed column mapping
        try:
            _data_source_config = _build_data_source_config(sheet_name.strip() or "Sheet1")
            parser = ExcelParser(str(saved_path), _data_source_config)
            _data_context = parser.parse()
        except Exception as exc:
            import traceback
            err = f"{type(exc).__name__}: {exc}"
            _session.logger.error("Excel parse error: %s\n%s", err, traceback.format_exc())
            raise HTTPException(status_code=400, detail=f"Failed to parse Excel: {err}")

        global _report_groups
        _report_groups = []
        skipped_rows: list[dict] = []

        # Log raw rows for debugging
        for sheet_name, rows in _data_context.sheets.items():
            for r in rows:
                _session.logger.info(
                    "Row %d: app_report=%r G(left_operand)=%r H(data_new)=%r I(tab)=%r J(option)=%r K(processing)=%r",
                    r.row_index,
                    r.values.get("app_report"),
                    r.values.get("left_operand"),
                    r.values.get("data_new"),
                    r.values.get("tab"),
                    r.values.get("option_number"),
                    r.values.get("processing_new"),
                )

            # Group consecutive rows into report groups (continuation rows merge into previous)
            sheet_groups, sheet_skipped = group_excel_rows(rows)
            _report_groups.extend(sheet_groups)
            skipped_rows.extend(sheet_skipped)

        # Build a flat preview — one row per report group
        preview = []
        for group in _report_groups:
            report = group["report"]
            preview.append({
                "_row": group["row_index"],
                "app_report": report.get("app_report", ""),
                "current_version": report.get("current_version", ""),
                "new_version": report.get("new_version", ""),
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
        if not _data_context:
            raise HTTPException(status_code=400, detail="No data loaded")
        return {"rows": _data_context.total_rows, "preview": _format_preview(_data_context)}

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
            try:
                result = await run_jde_full(page, group)
                status = result.get("status", "fail")
                error = result.get("error")
            except Exception as exc:
                import traceback
                _session.logger.error("Iteration %d crashed: %s\n%s", i, exc, traceback.format_exc())
                status = "fail"
                error = f"Unhandled exception: {exc}"

            duration_ms = (time.monotonic() - start) * 1000
            _execution_results.append({
                "iteration": i,
                "total": total,
                "test_id": report.get("app_report", "?"),
                "name": label,
                "status": status,
                "duration_ms": round(duration_ms),
                "tokens": 0,
                "screenshot": "",
                "data_selections_count": len(group["data_selections"]),
                "processing_options_count": len(group["processing_options"]),
                "steps": [{
                    "step_id": "S001",
                    "name": label,
                    "status": status,
                    "duration_ms": round(duration_ms),
                    "error": error,
                    "selector": None,
                }] if status == "fail" else [{
                    "step_id": "S001",
                    "name": label,
                    "status": "pass",
                    "duration_ms": round(duration_ms),
                    "error": None,
                    "selector": None,
                }],
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

def _format_preview(ctx: DataContext) -> list[dict]:
    """Format data rows for the frontend preview table."""
    rows = []
    for sheet_name, data_rows in ctx.sheets.items():
        for dr in data_rows:
            row = {"_row": dr.row_index, "_sheet": sheet_name}
            row.update(dr.values)
            rows.append(row)
    return rows


def _generate_execution_report() -> str:
    """Build a SuiteResult from execution results and generate HTML report."""
    if not _suite_request or not _execution_results:
        return ""

    test_results = []
    for r in _execution_results:
        steps = [
            StepResult(
                step_id=s["step_id"],
                name=s["name"],
                action="click",  # simplified for report
                status=s["status"],
                duration_ms=s.get("duration_ms", 0),
                error_message=s.get("error"),
                resolved_selector=s.get("selector"),
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
    return generate_report(suite_result, output_dir=str(run_dir))


dashboard_app = create_dashboard_app()
