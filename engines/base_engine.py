"""Abstract base engine with retry logic, logging, and screenshot capture.

Both concrete engines (AI-Native and Hybrid) inherit from this class.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from models.schemas import (
    ActionType,
    EngineType,
    get_platform_config,
    StepResult,
    StepStatus,
    SuiteResult,
    TestCase,
    TestResult,
    TestStatus,
    TestStep,
    TestSuiteRequest,
)
from utils.logger import TokenTracker, get_logger, step_log


class BaseEngine(ABC):
    """Base class for test execution engines."""

    engine_type: EngineType

    def __init__(self, request: TestSuiteRequest) -> None:
        self.request = request
        self.token_tracker = TokenTracker()

        # Build run folder: logs/MM-DD-YYYY_HH_MM_test_name/
        base_log_dir = Path(os.getenv("LOG_DIR", "logs"))
        ts = datetime.now().strftime("%m-%d-%Y_%H_%M")
        safe_name = re.sub(r"[^\w\-]", "_", request.suite_name)[:60]
        self._run_dir = base_log_dir / f"{ts}_{safe_name}"
        self._run_dir.mkdir(parents=True, exist_ok=True)

        self._run_id = f"{request.suite_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.logger = get_logger(
            f"qa.{self.engine_type.value}",
            log_dir=str(self._run_dir),
            run_id=self._run_id,
        )
        self._screenshot_dir = self._run_dir / "screenshots"
        self._screenshot_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Page context extraction for direct LLM calls
    # ------------------------------------------------------------------

    async def _get_page_context(self, page: Page, max_length: int = 6000) -> str:
        """Extract a compact, LLM-friendly snapshot of interactive elements.

        Optimized to keep token count low (~1000-1500 tokens) by:
        - Only extracting visible elements in the viewport
        - Dropping noisy CSS-in-JS class names (hashes, long utility classes)
        - Limiting to 80 elements and short attribute values
        - Skipping the accessibility tree (HTML is enough)
        """
        try:
            html = await page.evaluate("""() => {
                const sels = 'a, button, input, select, textarea, ' +
                    '[role="button"], [role="link"], [role="tab"], [role="menuitem"], ' +
                    '[data-testid], [type="submit"]';
                const els = document.querySelectorAll(sels);
                const result = [];
                const seen = new Set();
                for (const el of els) {
                    if (result.length >= 80) break;
                    // Skip hidden/off-screen elements
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    const tag = el.tagName.toLowerCase();
                    const text = (el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 50);
                    const attrs = [];
                    // High-value selector attrs only (skip class — too noisy)
                    for (const a of ['id', 'href', 'type', 'name', 'placeholder',
                                     'aria-label', 'data-testid', 'role']) {
                        const v = el.getAttribute(a);
                        if (v) attrs.push(a + '="' + v.slice(0, 50) + '"');
                    }
                    const line = '<' + tag + (attrs.length ? ' ' + attrs.join(' ') : '') + '>' + text + '</' + tag + '>';
                    if (!seen.has(line)) {
                        seen.add(line);
                        result.push(line);
                    }
                }
                return result.join('\\n');
            }""")
            if html:
                if len(html) > max_length:
                    html = html[:max_length] + "\n... (truncated)"
                return html
        except Exception:
            pass

        # Fallback: accessibility tree only
        try:
            snapshot = await page.accessibility.snapshot()  # type: ignore[union-attr]
            if snapshot:
                lines = self._flatten_a11y(snapshot, depth=0)
                text = "\n".join(lines)
                if len(text) > max_length:
                    text = text[:max_length] + "\n... (truncated)"
                return text
        except Exception:
            pass

        return "(page context unavailable)"

    def _flatten_a11y(self, node: dict, depth: int) -> list[str]:
        """Recursively flatten an accessibility tree into indented lines."""
        lines: list[str] = []
        role = node.get("role", "")
        name = node.get("name", "")
        if role and role not in ("none", "generic", "GenericContainer"):
            indent = "  " * depth
            line = f"{indent}[{role}]"
            if name:
                line += f' "{name}"'
            lines.append(line)
        for child in node.get("children", []):
            lines.extend(self._flatten_a11y(child, depth + 1))
        return lines

    # ------------------------------------------------------------------
    # Abstract — each engine implements these
    # ------------------------------------------------------------------

    @abstractmethod
    async def execute_step(
        self, page: Page, step: TestStep, test_case: TestCase
    ) -> StepResult:
        """Execute a single test step and return the result."""

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> SuiteResult:
        """Run the entire test suite and return structured results."""
        suite_start = datetime.now(timezone.utc)

        suite_result = SuiteResult(
            suite_id=self.request.suite_id,
            suite_name=self.request.suite_name,
            environment=self.request.environment,
            browser=self.request.browser,
            engine_type=self.engine_type,
            llm_provider=self.request.llm_provider,
            llm_model=self.request.llm_model,
            started_at=suite_start,
        )

        async with async_playwright() as pw:
            browser_launcher = getattr(pw, self.request.browser)
            browser: Browser = await browser_launcher.launch(
                headless=self.request.headless
            )
            try:
                for tc in self.request.test_cases:
                    test_result = await self._run_test_case(browser, tc)
                    suite_result.test_results.append(test_result)
            finally:
                await browser.close()

        suite_end = datetime.now(timezone.utc)
        suite_result.finished_at = suite_end
        suite_result.total_duration_ms = (suite_end - suite_start).total_seconds() * 1000
        # Compute total tokens from actual test results (source of truth)
        suite_result.total_tokens = sum(
            tr.total_tokens for tr in suite_result.test_results
        )
        return suite_result

    # ------------------------------------------------------------------
    # Per-test-case runner
    # ------------------------------------------------------------------

    async def _run_test_case(self, browser: Browser, tc: TestCase) -> TestResult:
        test_start = datetime.now(timezone.utc)
        platform_cfg = get_platform_config(tc.platform)
        width = int(os.getenv("BROWSER_WIDTH", "1920"))
        height = int(os.getenv("BROWSER_HEIGHT", "1080"))
        context: BrowserContext = await browser.new_context(
            viewport={"width": width, "height": height},
        )
        page: Page = await context.new_page()
        page.set_default_timeout(platform_cfg.get("timeout_ms", 30_000))

        step_results: list[StepResult] = []
        test_status = TestStatus.PASS
        test_tokens = 0
        abort = False

        try:
            for step in tc.steps:
                if abort:
                    step_results.append(StepResult(
                        step_id=step.step_id,
                        name=step.name,
                        action=step.action,
                        status=StepStatus.SKIP,
                    ))
                    continue

                result = await self._run_step_with_retry(page, step, tc)
                step_results.append(result)
                test_tokens += result.tokens_used

                if result.status in (StepStatus.FAIL, StepStatus.ERROR):
                    test_status = TestStatus.FAIL if result.status == StepStatus.FAIL else TestStatus.ERROR
                    if not step.continue_on_failure:
                        abort = True
        except Exception as exc:
            test_status = TestStatus.ERROR
            self.logger.error("Unexpected error in test %s: %s", tc.test_id, str(exc))
        finally:
            await context.close()

        test_end = datetime.now(timezone.utc)
        # Compute total tokens from step results (source of truth)
        actual_tokens = sum(s.tokens_used for s in step_results)
        return TestResult(
            test_id=tc.test_id,
            name=tc.name,
            status=test_status,
            platform=tc.platform,
            steps=step_results,
            started_at=test_start,
            finished_at=test_end,
            duration_ms=(test_end - test_start).total_seconds() * 1000,
            total_tokens=actual_tokens,
        )

    # ------------------------------------------------------------------
    # Retry wrapper
    # ------------------------------------------------------------------

    async def _run_step_with_retry(
        self, page: Page, step: TestStep, tc: TestCase
    ) -> StepResult:
        last_result: Optional[StepResult] = None
        attempts = step.retry_count + 1

        for attempt in range(1, attempts + 1):
            step_log(
                self.logger,
                step.name,
                test_id=tc.test_id,
                step_id=step.step_id,
                status="RUNNING",
            )

            if step.pre_wait_ms > 0:
                await asyncio.sleep(step.pre_wait_ms / 1000)

            # Wait for all network activity to finish before acting
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass  # proceed even if timeout — page may use long-polling

            step_start = datetime.now(timezone.utc)
            try:
                last_result = await self.execute_step(page, step, tc)
            except Exception as exc:
                step_end = datetime.now(timezone.utc)
                last_result = StepResult(
                    step_id=step.step_id,
                    name=step.name,
                    action=step.action,
                    status=StepStatus.ERROR,
                    started_at=step_start,
                    finished_at=step_end,
                    duration_ms=(step_end - step_start).total_seconds() * 1000,
                    error_message=str(exc),
                )

            # Log result
            step_log(
                self.logger,
                step.name,
                test_id=tc.test_id,
                step_id=step.step_id,
                status=last_result.status.value.upper(),
                duration_ms=last_result.duration_ms,
                tokens=last_result.tokens_used,
                selector=last_result.resolved_selector,
                error_message=last_result.error_message,
            )

            if last_result.status == StepStatus.PASS:
                return last_result

            # Capture screenshot on failure if configured
            if step.screenshot_on_failure and last_result.status in (StepStatus.FAIL, StepStatus.ERROR):
                last_result.screenshot_path = await self._take_screenshot(
                    page, tc.test_id, step.step_id, "FAIL"
                )

            if attempt < attempts:
                self.logger.info(
                    "Retrying step %s/%s (attempt %d/%d)",
                    tc.test_id, step.step_id, attempt + 1, attempts,
                )

        return last_result  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Screenshot helper
    # ------------------------------------------------------------------

    async def _take_screenshot(
        self, page: Page, test_id: str, step_id: str, tag: str
    ) -> str:
        safe_name = re.sub(r"[^\w\-.]", "_", f"{test_id}_{step_id}_{tag}")
        path = self._screenshot_dir / f"{safe_name}.png"
        try:
            await page.screenshot(path=str(path), full_page=True)
        except Exception as exc:
            self.logger.warning("Screenshot failed: %s", str(exc))
            return ""
        return str(path)
