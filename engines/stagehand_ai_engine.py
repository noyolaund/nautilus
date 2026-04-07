"""Implementation A — Stagehand AI-Native engine.

Every element interaction goes through the LLM via Stagehand's
act() / observe() / extract() API. Best for SAP Fiori with dynamic IDs,
Shadow DOM, and deep nesting.

Fallback: if the proxy/Stagehand server is unreachable, calls the LLM
directly using the provider configured in .env (DeepSeek, OpenAI, etc.),
passing the real page DOM so the LLM returns actual selectors, then
executes the action via Playwright.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from playwright.async_api import Page, TimeoutError as PwTimeout

from engines.base_engine import BaseEngine
from models.schemas import (
    ActionType,
    EngineType,
    StepResult,
    StepStatus,
    TestCase,
    TestStep,
    TestSuiteRequest,
)
from utils.llm_client import (
    LLMClient,
    SYSTEM_ACT,
    SYSTEM_OBSERVE,
    SYSTEM_EXTRACT,
    log_stagehand_request,
    log_stagehand_response,
    log_stagehand_error,
)


class StagehandAIEngine(BaseEngine):
    """AI-Native engine: proxy first, direct LLM fallback with page context."""

    engine_type = EngineType.AI_NATIVE

    def __init__(self, request: TestSuiteRequest) -> None:
        super().__init__(request)
        self._stagehand_url = os.getenv(
            "STAGEHAND_SERVER_URL", "http://localhost:3001"
        ).rstrip("/")
        self._llm_provider = request.llm_provider
        self._llm_model = request.llm_model
        self._client = httpx.AsyncClient(timeout=120.0)
        self._direct_llm = LLMClient(
            provider=request.llm_provider,
            model=request.llm_model,
        )
        self._proxy_available: Optional[bool] = None

    # ------------------------------------------------------------------
    # Proxy health check (done once at first call)
    # ------------------------------------------------------------------

    async def _check_proxy(self) -> bool:
        if self._proxy_available is not None:
            return self._proxy_available
        try:
            resp = await self._client.get(
                f"{self._stagehand_url}/health", timeout=5.0
            )
            self._proxy_available = resp.status_code == 200
        except (httpx.ConnectError, httpx.TimeoutException):
            self._proxy_available = False

        if self._proxy_available:
            self.logger.info("Proxy available at %s", self._stagehand_url)
        else:
            self.logger.info(
                "Proxy unavailable — falling back to direct LLM (%s/%s)",
                self._llm_provider, self._llm_model,
            )
        return self._proxy_available

    # ------------------------------------------------------------------
    # Unified RPC: proxy → fallback to direct LLM with page context
    # ------------------------------------------------------------------

    async def _call_act(self, page: Page, instruction: str) -> dict[str, Any]:
        import time as _time
        if await self._check_proxy():
            log_stagehand_request("act", instruction, via="proxy")
            payload = {
                "action": instruction,
                "modelName": self._llm_model,
                "modelProvider": self._llm_provider,
            }
            start = _time.monotonic()
            try:
                resp = await self._client.post(
                    f"{self._stagehand_url}/act", json=payload
                )
                resp.raise_for_status()
                result = resp.json()
                log_stagehand_response("act", result, (_time.monotonic() - start) * 1000, via="proxy")
                return result
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                self._proxy_available = False
                log_stagehand_error("act", f"Proxy lost: {exc}", via="proxy")

        # Direct LLM: include real page context
        page_ctx = await self._get_page_context(page)
        prompt = f"PAGE CONTENT:\n{page_ctx}\n\nACTION: {instruction}"
        return await self._direct_llm.chat(SYSTEM_ACT, prompt)

    async def _call_observe(self, page: Page, instruction: str) -> dict[str, Any]:
        import time as _time
        if await self._check_proxy():
            log_stagehand_request("observe", instruction, via="proxy")
            payload = {
                "instruction": instruction,
                "modelName": self._llm_model,
                "modelProvider": self._llm_provider,
            }
            start = _time.monotonic()
            try:
                resp = await self._client.post(
                    f"{self._stagehand_url}/observe", json=payload
                )
                resp.raise_for_status()
                result = resp.json()
                log_stagehand_response("observe", result, (_time.monotonic() - start) * 1000, via="proxy")
                return result
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                self._proxy_available = False
                log_stagehand_error("observe", f"Proxy lost: {exc}", via="proxy")

        page_ctx = await self._get_page_context(page)
        prompt = f"PAGE CONTENT:\n{page_ctx}\n\nFIND: {instruction}"
        return await self._direct_llm.chat(SYSTEM_OBSERVE, prompt)

    async def _call_extract(self, page: Page, instruction: str) -> dict[str, Any]:
        import time as _time
        if await self._check_proxy():
            log_stagehand_request("extract", instruction, via="proxy")
            payload = {
                "instruction": instruction,
                "modelName": self._llm_model,
                "modelProvider": self._llm_provider,
            }
            start = _time.monotonic()
            try:
                resp = await self._client.post(
                    f"{self._stagehand_url}/extract", json=payload
                )
                resp.raise_for_status()
                result = resp.json()
                log_stagehand_response("extract", result, (_time.monotonic() - start) * 1000, via="proxy")
                return result
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                self._proxy_available = False
                log_stagehand_error("extract", f"Proxy lost: {exc}", via="proxy")

        page_ctx = await self._get_page_context(page)
        prompt = f"PAGE CONTENT:\n{page_ctx}\n\nEXTRACT: {instruction}"
        return await self._direct_llm.chat(SYSTEM_EXTRACT, prompt)

    # ------------------------------------------------------------------
    # Resolve selector from LLM result and locate element via Playwright
    # ------------------------------------------------------------------

    async def _resolve_and_locate(
        self, page: Page, result: dict[str, Any], timeout: int = 10000
    ):
        """Try each selector the LLM returned until one matches on the real page.

        Resolution order:
        1. CSS/Playwright selectors from the LLM response
        2. Text-based fallback using element descriptions
        """
        # Collect candidate selectors from the result
        selectors: list[str] = []
        descriptions: list[str] = []

        # From act() response
        sel = result.get("selector", "")
        if sel:
            selectors.append(sel)
        desc = result.get("description", "")
        if desc:
            descriptions.append(desc)

        # From observe() response
        for el in result.get("elements", []):
            s = el.get("selector", "")
            if s:
                selectors.append(s)
            d = el.get("description", "")
            if d:
                descriptions.append(d)

        # 1. Try each CSS/Playwright selector directly
        for selector in selectors:
            try:
                locator = page.locator(selector)
                await locator.first.wait_for(state="visible", timeout=min(timeout, 5000))
                self.logger.info("Selector matched: %s", selector)
                return locator.first, selector
            except (PwTimeout, Exception):
                self.logger.info("Selector did not match: %s", selector)
                continue

        # 2. Text-based fallbacks — extract text from selectors like
        #    button:has-text('X') or descriptions containing quoted text
        text_candidates: list[str] = []
        import re
        for s in selectors:
            m = re.search(r"has-text\(['\"](.+?)['\"]\)", s)
            if m:
                text_candidates.append(m.group(1))
        for d in descriptions:
            # Extract quoted text from descriptions
            for m in re.finditer(r"['\"]([^'\"]{2,})['\"]", d):
                text_candidates.append(m.group(1))

        for text in dict.fromkeys(text_candidates):  # dedupe preserving order
            # Try get_by_role("button") + name
            for role in ["button", "link", "menuitem"]:
                try:
                    locator = page.get_by_role(role, name=text, exact=False)
                    await locator.first.wait_for(state="visible", timeout=min(timeout, 5000))
                    matched = f'role={role}[name="{text}"]'
                    self.logger.info("Text-role fallback matched: %s", matched)
                    return locator.first, matched
                except (PwTimeout, Exception):
                    continue
            # Try input[value="..."] (catches <input type="submit" value="Sign In">)
            try:
                locator = page.locator(f'input[value="{text}" i]')
                await locator.first.wait_for(state="visible", timeout=min(timeout, 3000))
                matched = f'input[value="{text}"]'
                self.logger.info("Input value fallback matched: %s", matched)
                return locator.first, matched
            except (PwTimeout, Exception):
                pass
            # Try get_by_text
            try:
                locator = page.get_by_text(text, exact=False)
                await locator.first.wait_for(state="visible", timeout=min(timeout, 5000))
                matched = f'text="{text}"'
                self.logger.info("Text fallback matched: %s", matched)
                return locator.first, matched
            except (PwTimeout, Exception):
                self.logger.info("Text fallback did not match: %s", text)
                continue

        return None, None

    # ------------------------------------------------------------------
    # Core step executor
    # ------------------------------------------------------------------

    async def execute_step(
        self, page: Page, step: TestStep, test_case: TestCase
    ) -> StepResult:
        step_start = datetime.now(timezone.utc)
        tokens_used = 0
        resolved_selector: Optional[str] = None
        error_message: Optional[str] = None
        status = StepStatus.PASS

        try:
            match step.action:
                case ActionType.NAVIGATE:
                    url = step.data.value  # type: ignore[union-attr]
                    await page.goto(url, wait_until="domcontentloaded")

                case ActionType.CLICK:
                    desc = step.target.description  # type: ignore[union-attr]
                    result = await self._call_act(page, f"Click on {desc}")
                    tokens_used = result.get("tokens", 0)
                    locator, sel = await self._resolve_and_locate(
                        page, result, timeout=step.timeout_ms
                    )
                    if locator:
                        await locator.click()
                        resolved_selector = sel
                    else:
                        status = StepStatus.FAIL
                        error_message = f"Element not found: {desc}"

                case ActionType.TYPE:
                    desc = step.target.description  # type: ignore[union-attr]
                    value = step.data.value  # type: ignore[union-attr]
                    display_value = "****" if step.data.sensitive else value  # type: ignore[union-attr]
                    result = await self._call_act(
                        page, f"Find the input field: {desc}"
                    )
                    tokens_used = result.get("tokens", 0)
                    locator, sel = await self._resolve_and_locate(
                        page, result, timeout=step.timeout_ms
                    )
                    if locator:
                        if step.data.clear_before:  # type: ignore[union-attr]
                            await locator.clear()
                        await locator.fill(value)
                        resolved_selector = sel
                    else:
                        status = StepStatus.FAIL
                        error_message = f"Element not found: {desc}"

                case ActionType.SELECT:
                    desc = step.target.description  # type: ignore[union-attr]
                    option = step.data.value  # type: ignore[union-attr]
                    result = await self._call_act(
                        page, f"Find the dropdown/select: {desc}"
                    )
                    tokens_used = result.get("tokens", 0)
                    locator, sel = await self._resolve_and_locate(
                        page, result, timeout=step.timeout_ms
                    )
                    if locator:
                        await locator.select_option(label=option)
                        resolved_selector = sel
                    else:
                        status = StepStatus.FAIL
                        error_message = f"Element not found: {desc}"

                case ActionType.WAIT:
                    desc = step.target.description  # type: ignore[union-attr]
                    result = await self._call_observe(
                        page, f"Wait until {desc} is visible on the page"
                    )
                    tokens_used = result.get("tokens", 0)
                    locator, sel = await self._resolve_and_locate(
                        page, result, timeout=step.timeout_ms
                    )
                    resolved_selector = sel

                case ActionType.ASSERT_VISIBLE:
                    desc = step.target.description  # type: ignore[union-attr]
                    result = await self._call_observe(
                        page, f"Find {desc} on the page"
                    )
                    tokens_used = result.get("tokens", 0)
                    locator, sel = await self._resolve_and_locate(
                        page, result, timeout=step.timeout_ms
                    )
                    if locator:
                        resolved_selector = sel
                    else:
                        status = StepStatus.FAIL
                        error_message = f"Element not found: {desc}"

                case ActionType.ASSERT_TEXT:
                    desc = step.target.description  # type: ignore[union-attr]
                    expected = step.data.value  # type: ignore[union-attr]
                    result = await self._call_observe(page, f"Find {desc}")
                    tokens_used = result.get("tokens", 0)
                    locator, sel = await self._resolve_and_locate(
                        page, result, timeout=step.timeout_ms
                    )
                    if locator:
                        actual = await locator.text_content() or ""
                        resolved_selector = sel
                        if expected not in actual:
                            status = StepStatus.FAIL
                            error_message = f"Expected '{expected}' in '{actual}'"
                    else:
                        status = StepStatus.FAIL
                        error_message = f"Element not found: {desc}"

                case ActionType.ASSERT_VALUE:
                    desc = step.target.description  # type: ignore[union-attr]
                    expected = step.data.value  # type: ignore[union-attr]
                    result = await self._call_observe(page, f"Find {desc}")
                    tokens_used = result.get("tokens", 0)
                    locator, sel = await self._resolve_and_locate(
                        page, result, timeout=step.timeout_ms
                    )
                    if locator:
                        actual = await locator.input_value()
                        resolved_selector = sel
                        if str(expected) != str(actual):
                            status = StepStatus.FAIL
                            error_message = f"Expected value '{expected}', got '{actual}'"
                    else:
                        status = StepStatus.FAIL
                        error_message = f"Element not found: {desc}"

                case ActionType.EXTRACT:
                    desc = step.target.description  # type: ignore[union-attr]
                    result = await self._call_extract(
                        page, f"Extract data from {desc}"
                    )
                    tokens_used = result.get("tokens", 0)

                case ActionType.SCREENSHOT:
                    path = await self._take_screenshot(
                        page, test_case.test_id, step.step_id,
                        datetime.now().strftime("%Y%m%d")
                    )
                    resolved_selector = path

                case ActionType.CUSTOM:
                    desc = step.target.description  # type: ignore[union-attr]
                    result = await self._call_act(page, desc)
                    tokens_used = result.get("tokens", 0)
                    # Try to resolve and act if a selector was returned
                    locator, sel = await self._resolve_and_locate(
                        page, result, timeout=step.timeout_ms
                    )
                    if locator:
                        resolved_selector = sel

        except httpx.HTTPStatusError as exc:
            status = StepStatus.ERROR
            error_message = f"LLM API error: HTTP {exc.response.status_code}"
        except httpx.ConnectError:
            status = StepStatus.ERROR
            error_message = "Cannot connect to proxy or LLM provider"
        except PwTimeout:
            status = StepStatus.FAIL
            error_message = f"Playwright timeout after {step.timeout_ms}ms"
        except Exception as exc:
            status = StepStatus.FAIL
            error_message = str(exc)

        step_end = datetime.now(timezone.utc)
        duration = (step_end - step_start).total_seconds() * 1000

        self.token_tracker.add(step.step_id, tokens_used)

        return StepResult(
            step_id=step.step_id,
            name=step.name,
            action=step.action,
            status=status,
            started_at=step_start,
            finished_at=step_end,
            duration_ms=duration,
            tokens_used=tokens_used,
            resolved_selector=resolved_selector,
            error_message=error_message,
        )
