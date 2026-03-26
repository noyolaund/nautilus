"""FastAPI REST API for the QA Automation Framework.

Security features:
  - API key authentication via X-API-Key header
  - Rate limiting (slowapi)
  - CORS with configurable origins
  - Secure response headers
  - Input validation via Pydantic models
  - No internal error details leaked to clients
"""

from __future__ import annotations

import asyncio
import os
import secrets
import uuid
from datetime import datetime
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.security import APIKeyHeader
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from models.schemas import (
    EngineType,
    SuiteResult,
    TestCase,
    TestSuiteRequest,
)
from engines.stagehand_ai_engine import StagehandAIEngine
from engines.hybrid_playwright_engine import HybridPlaywrightEngine
from reports.html_report import generate_report

# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

limiter = Limiter(key_func=get_remote_address)

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    app = FastAPI(
        title="QA Automation Framework",
        version="1.0.0",
        docs_url="/docs",
        redoc_url=None,
    )

    app.state.limiter = limiter

    # --- CORS ---------------------------------------------------------------
    origins = os.getenv("CORS_ORIGINS", "").split(",")
    origins = [o.strip() for o in origins if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins or ["http://localhost:3000"],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-API-Key"],
    )

    # --- Security headers middleware ----------------------------------------
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Cache-Control"] = "no-store"
        return response

    # --- Rate limit error handler -------------------------------------------
    @app.exception_handler(RateLimitExceeded)
    async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded. Please try again later."},
        )

    # --- In-memory run store (per-process) ----------------------------------
    runs: dict[str, dict] = {}

    # --- Auth dependency ----------------------------------------------------
    api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
    expected_key = os.getenv("API_SECRET_KEY", "")

    async def verify_api_key(
        api_key: Optional[str] = Security(api_key_header),
    ) -> str:
        if not expected_key:
            return "no-auth"
        if not api_key or not secrets.compare_digest(api_key, expected_key):
            raise HTTPException(status_code=401, detail="Invalid or missing API key")
        return api_key

    # --- Engine factory ------------------------------------------------------
    def _get_engine(request: TestSuiteRequest, engine_type: EngineType):
        if engine_type == EngineType.AI_NATIVE:
            return StagehandAIEngine(request)
        return HybridPlaywrightEngine(request)

    # =======================================================================
    # Endpoints
    # =======================================================================

    @app.get("/health")
    @limiter.limit("60/minute")
    async def health(request: Request):
        return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}

    @app.post("/execute")
    @limiter.limit("10/minute")
    async def execute_suite(
        request: Request,
        suite: TestSuiteRequest,
        engine_type: EngineType = Query(default=EngineType.HYBRID),
        _key: str = Depends(verify_api_key),
    ) -> dict:
        engine = _get_engine(suite, engine_type)
        result: SuiteResult = await engine.run()
        report_path = generate_report(result)
        return {
            "suite_id": result.suite_id,
            "status": "completed",
            "total_tests": result.total_tests,
            "passed": result.passed,
            "failed": result.failed,
            "pass_rate": result.pass_rate,
            "total_tokens": result.total_tokens,
            "duration_ms": result.total_duration_ms,
            "report_path": report_path,
            "result": result.model_dump(mode="json"),
        }

    @app.post("/execute/async")
    @limiter.limit("10/minute")
    async def execute_suite_async(
        request: Request,
        suite: TestSuiteRequest,
        engine_type: EngineType = Query(default=EngineType.HYBRID),
        _key: str = Depends(verify_api_key),
    ) -> dict:
        run_id = str(uuid.uuid4())
        runs[run_id] = {"status": "running", "result": None, "report_path": None}

        async def _background():
            try:
                engine = _get_engine(suite, engine_type)
                result = await engine.run()
                report_path = generate_report(result)
                runs[run_id] = {
                    "status": "completed",
                    "result": result.model_dump(mode="json"),
                    "report_path": report_path,
                }
            except Exception:
                runs[run_id] = {
                    "status": "error",
                    "result": None,
                    "report_path": None,
                }

        asyncio.create_task(_background())
        return {"run_id": run_id, "status": "running"}

    @app.get("/status/{run_id}")
    @limiter.limit("60/minute")
    async def get_status(
        request: Request,
        run_id: str,
        _key: str = Depends(verify_api_key),
    ) -> dict:
        run = runs.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return {"run_id": run_id, "status": run["status"]}

    @app.get("/report/{run_id}")
    @limiter.limit("30/minute")
    async def get_report(
        request: Request,
        run_id: str,
        _key: str = Depends(verify_api_key),
    ):
        run = runs.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        if run["status"] != "completed" or not run.get("report_path"):
            raise HTTPException(status_code=202, detail="Report not ready yet")
        path = run["report_path"]
        if not os.path.isfile(path):
            raise HTTPException(status_code=404, detail="Report file not found")
        return FileResponse(path, media_type="text/html", filename=os.path.basename(path))

    @app.get("/schema/test-suite")
    @limiter.limit("60/minute")
    async def schema_test_suite(request: Request):
        return TestSuiteRequest.model_json_schema()

    @app.get("/schema/test-case")
    @limiter.limit("60/minute")
    async def schema_test_case(request: Request):
        return TestCase.model_json_schema()

    return app


app = create_app()
