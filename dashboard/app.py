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

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
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
_suite_raw: Optional[dict] = None
_execution_results: list[dict] = []


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
        """Run login_assert.json — login success is determined by the assert_visible step."""
        global _suite_request, _suite_raw

        # 1. Load the login suite (runs the login flow, ends with assert_visible)
        login_path = "tests/test_cases/login_assert.json"
        if not Path(login_path).exists():
            raise HTTPException(status_code=400, detail=f"Login suite not found: {login_path}")

        login_raw = json.loads(Path(login_path).read_text(encoding="utf-8"))
        login_raw.pop("_data_source", None)
        login_suite = TestSuiteRequest(**login_raw)

        # Force headless=False as required
        login_suite.headless = False

        # 2. Load the main suite for the iteration phase (keeps _data_source for Excel config)
        main_path = "tests/test_cases/jde_copy_report_version.json"
        if Path(main_path).exists():
            main_raw = json.loads(Path(main_path).read_text(encoding="utf-8"))
            _suite_raw = main_raw.copy()
            main_raw.pop("_data_source", None)
            _suite_request = TestSuiteRequest(**main_raw)
            _suite_request.headless = False

        # 3. Execute the login flow — login success = assert_visible step passes
        result = await _session.run_login(login_suite)
        return result

    @app.post("/api/session/stop")
    async def stop_browser():
        """Close the browser."""
        return await _session.stop()

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

    @app.post("/api/data/upload")
    async def upload_excel(file: UploadFile = File(...)):
        """Upload an xlsx file, save to temp, and parse it."""
        global _data_context, _data_source_config

        if not file.filename.lower().endswith(".xlsx"):
            raise HTTPException(status_code=400, detail="Only .xlsx files accepted")

        if not _suite_raw or "_data_source" not in _suite_raw:
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

        # Parse and filter (same logic as load_excel)
        try:
            _data_source_config = DataSourceConfig(**_suite_raw["_data_source"])
            parser = ExcelParser(str(saved_path), _data_source_config)
            _data_context = parser.parse()
        except Exception as exc:
            import traceback
            err = f"{type(exc).__name__}: {exc}"
            _session.logger.error("Excel parse error: %s\n%s", err, traceback.format_exc())
            raise HTTPException(status_code=400, detail=f"Failed to parse Excel: {err}")

        skipped_rows: list[dict] = []
        for sheet_name, rows in list(_data_context.sheets.items()):
            valid_rows = []
            for r in rows:
                app_report = str(r.values.get("app_report", "")).strip().upper()
                if app_report and (app_report.startswith("R") or app_report.startswith("P")):
                    valid_rows.append(r)
                else:
                    skipped_rows.append({
                        "row": r.row_index,
                        "app_report": app_report,
                        "reason": "Column B must start with 'R' or 'P'",
                    })
            _data_context.sheets[sheet_name] = valid_rows
        _data_context.total_rows = sum(len(rs) for rs in _data_context.sheets.values())

        return {
            "status": "success",
            "filename": file.filename,
            "rows": _data_context.total_rows,
            "skipped_rows": skipped_rows,
            "skipped_count": len(skipped_rows),
            "preview": _format_preview(_data_context),
        }

    @app.post("/api/data/load")
    async def load_excel(request: Request):
        """Parse the Excel file and return a preview of the rows."""
        global _data_context, _data_source_config
        body = await request.json()
        excel_path = body.get("excel_path", "")

        if not excel_path or not Path(excel_path).exists():
            raise HTTPException(status_code=400, detail=f"Excel file not found: {excel_path}")

        if not _suite_raw or "_data_source" not in _suite_raw:
            raise HTTPException(status_code=400, detail="Suite has no _data_source config. Run Start Browser & Login first.")

        try:
            _data_source_config = DataSourceConfig(**_suite_raw["_data_source"])
            parser = ExcelParser(excel_path, _data_source_config)
            _data_context = parser.parse()
        except Exception as exc:
            import traceback
            err_detail = f"{type(exc).__name__}: {exc}"
            _session.logger.error("Excel parse error: %s\n%s", err_detail, traceback.format_exc())
            raise HTTPException(status_code=400, detail=f"Failed to parse Excel: {err_detail}")

        # Filter: only keep rows where column B (app_report) starts with R or P
        skipped_rows: list[dict] = []
        try:
            for sheet_name, rows in list(_data_context.sheets.items()):
                valid_rows = []
                for r in rows:
                    app_report = str(r.values.get("app_report", "")).strip().upper()
                    if app_report and (app_report.startswith("R") or app_report.startswith("P")):
                        valid_rows.append(r)
                    else:
                        skipped_rows.append({
                            "row": r.row_index,
                            "app_report": app_report,
                            "reason": "Column B must start with 'R' or 'P'",
                        })
                _data_context.sheets[sheet_name] = valid_rows
            _data_context.total_rows = sum(len(rs) for rs in _data_context.sheets.values())
        except Exception as exc:
            import traceback
            err_detail = f"{type(exc).__name__}: {exc}"
            _session.logger.error("Filter error: %s\n%s", err_detail, traceback.format_exc())
            raise HTTPException(status_code=400, detail=f"Failed to filter rows: {err_detail}")

        response = {
            "rows": _data_context.total_rows,
            "skipped_rows": skipped_rows,
            "skipped_count": len(skipped_rows),
            "preview": _format_preview(_data_context),
        }

        if _data_context.validation_errors:
            response["status"] = "warning"
            response["errors"] = _data_context.validation_errors[:10]
        else:
            response["status"] = "success"
        return response

    @app.get("/api/data/preview")
    async def data_preview():
        """Get the loaded data preview."""
        if not _data_context:
            raise HTTPException(status_code=400, detail="No data loaded")
        return {"rows": _data_context.total_rows, "preview": _format_preview(_data_context)}

    # --- Execution endpoints ------------------------------------------------

    @app.post("/api/execute")
    async def execute_all():
        """Run all iterations using the loaded data."""
        global _execution_results
        if not _session.is_logged_in:
            raise HTTPException(status_code=400, detail="Not logged in. Run login first.")
        if not _data_context:
            raise HTTPException(status_code=400, detail="No data loaded. Load Excel first.")
        if not _suite_request or len(_suite_request.test_cases) < 2:
            raise HTTPException(status_code=400, detail="Suite needs at least 2 test cases (login + task)")

        _execution_results = []

        # The repeatable task is the second test case
        task_tc = _suite_request.test_cases[1]

        # Resolve templates for each data row
        default_sheet = list(_data_context.sheets.keys())[0]
        all_rows = _data_context.sheets[default_sheet]
        total = len(all_rows)

        resolver = TemplateResolver(_data_context, default_sheet=default_sheet)

        for i, row in enumerate(all_rows, 1):
            # Resolve templates for this row
            resolved_suite = resolver._resolve_suite_for_row(_suite_request, row, i)
            resolved_tc = resolved_suite.test_cases[1] if len(resolved_suite.test_cases) > 1 else resolved_suite.test_cases[0]

            result = await _session.execute_iteration(resolved_tc, i, total)
            _execution_results.append(result)

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
