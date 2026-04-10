"""Persistent browser session manager.

Keeps a single browser + page alive across multiple API calls so the
login happens once and all iterations reuse the same authenticated session.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import Browser, BrowserContext, Page, async_playwright, Playwright

from models.schemas import (
    ActionType,
    EngineType,
    StepResult,
    StepStatus,
    TestCase,
    TestResult,
    TestStatus,
    TestStep,
    TestSuiteRequest,
    get_platform_config,
)
from engines.hybrid_playwright_engine import HybridPlaywrightEngine
from utils.logger import get_logger, step_log


class SessionManager:
    """Manages a persistent browser session for iterative workflows."""

    def __init__(self) -> None:
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._engine: Optional[HybridPlaywrightEngine] = None
        self._is_logged_in: bool = False
        self._run_dir: Optional[Path] = None
        self.logger = get_logger("qa.dashboard", log_dir="logs")

    @property
    def is_active(self) -> bool:
        return self._page is not None and not self._page.is_closed()

    @property
    def is_logged_in(self) -> bool:
        return self._is_logged_in and self.is_active

    @property
    def run_dir(self) -> Optional[Path]:
        return self._run_dir

    async def start_browser(self) -> dict[str, Any]:
        """Launch a browser instance. Returns status."""
        if self.is_active:
            return {"status": "already_running", "message": "Browser is already open"}

        self._pw = await async_playwright().start()
        browser_type = os.getenv("BROWSER_TYPE", "chromium")
        headless = os.getenv("BROWSER_HEADLESS", "false").lower() == "true"
        width = int(os.getenv("BROWSER_WIDTH", "1920"))
        height = int(os.getenv("BROWSER_HEIGHT", "1080"))

        launcher = getattr(self._pw, browser_type)
        self._browser = await launcher.launch(headless=headless)
        self._context = await self._browser.new_context(
            viewport={"width": width, "height": height},
        )
        self._page = await self._context.new_page()
        self._is_logged_in = False

        # Create run directory
        ts = datetime.now().strftime("%m-%d-%Y_%H_%M")
        self._run_dir = Path(os.getenv("LOG_DIR", "logs")) / f"{ts}_JDE_Dashboard"
        self._run_dir.mkdir(parents=True, exist_ok=True)
        (self._run_dir / "screenshots").mkdir(exist_ok=True)

        self.logger.info("Browser started: %s %dx%d headless=%s", browser_type, width, height, headless)
        return {"status": "started", "message": f"Browser launched ({browser_type} {width}x{height})"}

    async def run_login(self, suite_request: TestSuiteRequest) -> dict[str, Any]:
        """Execute the login test case (first test_case in the suite)."""
        if not self.is_active:
            return {"status": "error", "message": "Browser not started. Click 'Start Browser' first."}

        if not suite_request.test_cases:
            return {"status": "error", "message": "No test cases in suite"}

        login_tc = suite_request.test_cases[0]

        # Create engine for this suite (reuses the existing page)
        self._engine = HybridPlaywrightEngine(suite_request)

        page = self._page
        platform_cfg = get_platform_config(login_tc.platform)
        page.set_default_timeout(platform_cfg.get("timeout_ms", 30_000))

        step_results: list[StepResult] = []
        login_status = TestStatus.PASS

        for step in login_tc.steps:
            result = await self._engine._run_step_with_retry(page, step, login_tc)
            step_results.append(result)
            if result.status in (StepStatus.FAIL, StepStatus.ERROR):
                login_status = TestStatus.FAIL
                if not step.continue_on_failure:
                    break

        self._is_logged_in = login_status == TestStatus.PASS

        return {
            "status": "success" if self._is_logged_in else "failed",
            "logged_in": self._is_logged_in,
            "steps": [
                {
                    "step_id": s.step_id,
                    "name": s.name,
                    "status": s.status.value,
                    "duration_ms": round(s.duration_ms),
                    "error": s.error_message,
                }
                for s in step_results
            ],
        }

    async def execute_iteration(
        self,
        test_case: TestCase,
        iteration: int,
        total: int,
    ) -> dict[str, Any]:
        """Execute one iteration of the repeatable task on the existing page."""
        if not self.is_active or not self._engine:
            return {"status": "error", "message": "No active session"}

        page = self._page
        platform_cfg = get_platform_config(test_case.platform)
        page.set_default_timeout(platform_cfg.get("timeout_ms", 30_000))

        step_results: list[StepResult] = []
        test_status = TestStatus.PASS

        self.logger.info("Iteration %d/%d: %s", iteration, total, test_case.name)

        for step in test_case.steps:
            result = await self._engine._run_step_with_retry(page, step, test_case)
            step_results.append(result)
            if result.status in (StepStatus.FAIL, StepStatus.ERROR):
                test_status = TestStatus.FAIL
                if not step.continue_on_failure:
                    break

        total_tokens = sum(s.tokens_used for s in step_results)
        duration = sum(s.duration_ms for s in step_results)

        # Take screenshot after each iteration
        screenshot_path = ""
        try:
            safe_name = re.sub(r"[^\w\-.]", "_", f"{test_case.test_id}_iter{iteration}")
            ss_path = self._run_dir / "screenshots" / f"{safe_name}.png"
            await page.screenshot(path=str(ss_path), full_page=True)
            screenshot_path = str(ss_path)
        except Exception:
            pass

        return {
            "iteration": iteration,
            "total": total,
            "test_id": test_case.test_id,
            "name": test_case.name,
            "status": test_status.value,
            "duration_ms": round(duration),
            "tokens": total_tokens,
            "screenshot": screenshot_path,
            "steps": [
                {
                    "step_id": s.step_id,
                    "name": s.name,
                    "status": s.status.value,
                    "duration_ms": round(s.duration_ms),
                    "error": s.error_message,
                    "selector": s.resolved_selector,
                }
                for s in step_results
            ],
        }

    async def stop(self) -> dict[str, Any]:
        """Close the browser and clean up."""
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass

        self._page = None
        self._context = None
        self._browser = None
        self._pw = None
        self._engine = None
        self._is_logged_in = False

        self.logger.info("Browser session closed")
        return {"status": "stopped", "message": "Browser closed"}
