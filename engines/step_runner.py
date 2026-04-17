"""Lightweight step runner for Python test scripts.

Wraps the hybrid engine so you can execute JSON-defined steps
on an existing Playwright page — CSS selectors first, LLM fallback.

Usage:
    from engines.step_runner import StepRunner

    runner = StepRunner(page)
    await runner.click("the Submit button", selector="#hc_Submit", iframe="iframe#e1menuAppIframe")
    await runner.type("the Batch Application field", value="R4311Z1I", selector="#C0_11", iframe="iframe#e1menuAppIframe")
    await runner.key_press("Enter")
    await runner.check_error("#INYFEContent")
"""

from __future__ import annotations

import os
from typing import Optional

from playwright.async_api import Page

from engines.hybrid_playwright_engine import HybridPlaywrightEngine
from engines.base_engine import normalize_key_combo
from models.schemas import (
    ActionType,
    StepData,
    StepTarget,
    StepResult,
    StepStatus,
    TestCase,
    TestStep,
    TestSuiteRequest,
)
from utils.logger import get_logger, step_log


class StepRunner:
    """Execute individual steps on an existing page using the hybrid engine's
    full resolution chain (CSS → cache → AI fallback)."""

    _step_counter: int = 0

    def __init__(
        self,
        page: Page,
        llm_provider: str = "",
        llm_model: str = "",
    ) -> None:
        self._page = page
        self._provider = llm_provider or os.getenv("LLM_PROVIDER", "deepseek")
        self._model = llm_model or os.getenv("LLM_MODEL", "deepseek-chat")

        # Create a minimal suite request so the hybrid engine can initialize
        self._suite = TestSuiteRequest(
            suite_id="SUITE-SCRIPT-001",
            suite_name="Python Script",
            llm_provider=self._provider,
            llm_model=self._model,
            test_cases=[TestCase(
                test_id="TC-SCRIPT",
                name="Script",
                base_url="http://localhost",
                steps=[TestStep(step_id="S001", name="placeholder", action=ActionType.NAVIGATE,
                                data=StepData(value="http://localhost"))],
            )],
        )
        self._engine = HybridPlaywrightEngine(self._suite)
        self._tc = self._suite.test_cases[0]
        self.logger = self._engine.logger

    def _next_step_id(self) -> str:
        StepRunner._step_counter += 1
        return f"S{StepRunner._step_counter:03d}"

    def _make_step(
        self,
        action: ActionType,
        name: str,
        description: str = "",
        selector: str = "",
        selector_strategy: str = "css",
        iframe: str = "",
        value: str = "",
        sensitive: bool = False,
        clear_before: bool = False,
        timeout_ms: int = 10000,
        continue_on_failure: bool = False,
    ) -> TestStep:
        target = None
        if description or selector:
            target = StepTarget(
                description=description or name,
                selector=selector or None,
                selector_strategy=selector_strategy,
                iframe=iframe or None,
            )
        data = None
        if value is not None and value != "":
            data = StepData(value=value, sensitive=sensitive, clear_before=clear_before)

        return TestStep(
            step_id=self._next_step_id(),
            name=name,
            action=action,
            target=target,
            data=data,
            timeout_ms=timeout_ms,
            retry_count=1,
            continue_on_failure=continue_on_failure,
        )

    async def _run(self, step: TestStep) -> StepResult:
        """Execute a step using the engine's retry wrapper."""
        result = await self._engine._run_step_with_retry(self._page, step, self._tc)
        if result.status in (StepStatus.FAIL, StepStatus.ERROR):
            raise StepError(step.name, result.error_message or "Unknown error", result)
        return result

    # ------------------------------------------------------------------
    # Public API — mirrors JSON actions
    # ------------------------------------------------------------------

    async def navigate(self, url: str, **kw) -> StepResult:
        step = self._make_step(ActionType.NAVIGATE, f"Navigate to {url}", value=url, **kw)
        return await self._run(step)

    async def click(
        self, description: str, *, selector: str = "", iframe: str = "",
        selector_strategy: str = "ai", **kw
    ) -> StepResult:
        step = self._make_step(
            ActionType.CLICK, f"Click {description}",
            description=description, selector=selector,
            selector_strategy=selector_strategy, iframe=iframe, **kw,
        )
        return await self._run(step)

    async def right_click(
        self, description: str, *, selector: str = "", iframe: str = "",
        selector_strategy: str = "ai", **kw
    ) -> StepResult:
        step = self._make_step(
            ActionType.RIGHT_CLICK, f"Right-click {description}",
            description=description, selector=selector,
            selector_strategy=selector_strategy, iframe=iframe, **kw,
        )
        return await self._run(step)

    async def type(
        self, description: str, *, value: str, selector: str = "", iframe: str = "",
        selector_strategy: str = "ai", clear_before: bool = True, sensitive: bool = False, **kw
    ) -> StepResult:
        step = self._make_step(
            ActionType.TYPE, f"Type into {description}",
            description=description, selector=selector,
            selector_strategy=selector_strategy, iframe=iframe,
            value=value, clear_before=clear_before, sensitive=sensitive, **kw,
        )
        return await self._run(step)

    async def select(
        self, description: str, *, value: str, selector: str = "", iframe: str = "",
        selector_strategy: str = "ai", **kw
    ) -> StepResult:
        step = self._make_step(
            ActionType.SELECT, f"Select {value} from {description}",
            description=description, selector=selector,
            selector_strategy=selector_strategy, iframe=iframe, value=value, **kw,
        )
        return await self._run(step)

    async def key_press(self, key_combo: str, *, description: str = "", selector: str = "",
                        iframe: str = "", **kw) -> StepResult:
        step = self._make_step(
            ActionType.KEY_PRESS, f"Press {key_combo}",
            description=description, selector=selector, iframe=iframe,
            value=key_combo, **kw,
        )
        return await self._run(step)

    async def assert_visible(self, description: str, *, selector: str = "",
                             iframe: str = "", selector_strategy: str = "ai", **kw) -> StepResult:
        step = self._make_step(
            ActionType.ASSERT_VISIBLE, f"Assert visible: {description}",
            description=description, selector=selector,
            selector_strategy=selector_strategy, iframe=iframe, **kw,
        )
        return await self._run(step)

    async def check_error(self, selector: str = "#INYFEContent", *, iframe: str = "",
                          **kw) -> StepResult:
        step = self._make_step(
            ActionType.CHECK_ERROR, "Check for JDE errors",
            description="JDE Error Banner", selector=selector,
            selector_strategy="css", iframe=iframe,
            continue_on_failure=True, **kw,
        )
        # check_error doesn't raise — we inspect the result
        result = await self._engine._run_step_with_retry(self._page, step, self._tc)
        if result.status == StepStatus.FAIL:
            raise StepError("Check Error", result.error_message or "JDE error detected", result)
        return result

    async def screenshot(self, path: str = "", **kw) -> StepResult:
        step = self._make_step(ActionType.SCREENSHOT, "Screenshot", **kw)
        return await self._engine._run_step_with_retry(self._page, step, self._tc)


class StepError(Exception):
    """Raised when a step fails."""
    def __init__(self, step_name: str, message: str, result: StepResult):
        self.step_name = step_name
        self.result = result
        super().__init__(f"[{step_name}] {message}")
