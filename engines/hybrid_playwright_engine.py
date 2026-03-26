"""Implementation B — Hybrid Playwright + Stagehand fallback engine.

Resolution order: CSS/XPath → selector cache → SAP attributes → AI fallback.
Resolved selectors are cached for future runs.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from playwright.async_api import Page, Locator, TimeoutError as PwTimeout

from engines.base_engine import BaseEngine
from models.schemas import (
    ActionType,
    EngineType,
    Platform,
    SelectorStrategy,
    StepResult,
    StepStatus,
    TestCase,
    TestStep,
    TestSuiteRequest,
)


class HybridPlaywrightEngine(BaseEngine):
    """Deterministic Playwright first, AI fallback when selectors fail."""

    engine_type = EngineType.HYBRID

    def __init__(self, request: TestSuiteRequest) -> None:
        super().__init__(request)
        self._cache_path = Path(
            os.getenv("SELECTOR_CACHE_PATH", "config/selector_cache.json")
        )
        self._cache: dict[str, str] = self._load_cache()
        self._stagehand_url = os.getenv(
            "STAGEHAND_SERVER_URL", "http://localhost:3001"
        ).rstrip("/")
        self._llm_model = request.llm_model
        self._llm_provider = request.llm_provider
        self._client = httpx.AsyncClient(timeout=120.0)

    # ------------------------------------------------------------------
    # Selector cache
    # ------------------------------------------------------------------

    def _load_cache(self) -> dict[str, str]:
        if self._cache_path.exists():
            try:
                data = json.loads(self._cache_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save_cache(self) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(
            json.dumps(self._cache, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _cache_key(self, test_id: str, step_id: str) -> str:
        return f"{test_id}::{step_id}"

    # ------------------------------------------------------------------
    # Element resolution chain
    # ------------------------------------------------------------------

    async def _resolve_element(
        self, page: Page, step: TestStep, test_case: TestCase
    ) -> tuple[Optional[Locator], Optional[str], int]:
        """Try resolution strategies in order. Returns (locator, selector_str, tokens)."""
        target = step.target
        if target is None:
            return None, None, 0

        tokens = 0

        # 1. Explicit selector (CSS / XPath / data-attr / ui5-stable)
        if target.selector and target.selector_strategy != SelectorStrategy.AI:
            locator = self._get_locator(page, target.selector, target.selector_strategy)
            try:
                await locator.first.wait_for(state="visible", timeout=5000)
                return locator.first, target.selector, 0
            except PwTimeout:
                pass

        # 2. Cached selector
        cache_key = self._cache_key(test_case.test_id, step.step_id)
        cached = self._cache.get(cache_key)
        if cached:
            try:
                locator = page.locator(cached)
                await locator.first.wait_for(state="visible", timeout=5000)
                return locator.first, f"cached: {cached}", 0
            except PwTimeout:
                pass

        # 3. SAP-specific data-ui5-stable attribute
        if test_case.platform in (Platform.SAP_FIORI, Platform.SAP_WEBGUI) and target.selector:
            ui5_selector = f"[data-ui5-stable='{target.selector}']"
            try:
                locator = page.locator(ui5_selector)
                await locator.first.wait_for(state="visible", timeout=5000)
                self._cache[cache_key] = ui5_selector
                self._save_cache()
                return locator.first, ui5_selector, 0
            except PwTimeout:
                pass

        # 4. Text-based matching
        if target.selector_strategy == SelectorStrategy.TEXT and target.selector:
            try:
                locator = page.get_by_text(target.selector)
                await locator.first.wait_for(state="visible", timeout=5000)
                return locator.first, f"text={target.selector}", 0
            except PwTimeout:
                pass

        # 5. Role-based matching
        if target.selector_strategy == SelectorStrategy.ROLE and target.selector:
            try:
                locator = page.get_by_role(target.selector)  # type: ignore[arg-type]
                await locator.first.wait_for(state="visible", timeout=5000)
                return locator.first, f"role={target.selector}", 0
            except PwTimeout:
                pass

        # 6. AI fallback via Stagehand
        try:
            result = await self._stagehand_observe(target.description)
            tokens = result.get("tokens", 0)
            elements = result.get("elements", [])
            if elements:
                ai_selector = elements[0].get("selector", "")
                if ai_selector:
                    try:
                        locator = page.locator(ai_selector)
                        await locator.first.wait_for(state="visible", timeout=10000)
                        self._cache[cache_key] = ai_selector
                        self._save_cache()
                        return locator.first, f"ai_resolved: {ai_selector}", tokens
                    except PwTimeout:
                        pass
        except Exception:
            pass

        return None, None, tokens

    def _get_locator(self, page: Page, selector: str, strategy: SelectorStrategy) -> Locator:
        if strategy == SelectorStrategy.XPATH:
            return page.locator(f"xpath={selector}")
        if strategy == SelectorStrategy.DATA_ATTR:
            return page.locator(f"[data-testid='{selector}']")
        if strategy == SelectorStrategy.UI5_STABLE:
            return page.locator(f"[data-ui5-stable='{selector}']")
        return page.locator(selector)

    # ------------------------------------------------------------------
    # Stagehand fallback
    # ------------------------------------------------------------------

    async def _stagehand_observe(self, instruction: str) -> dict[str, Any]:
        payload = {
            "instruction": instruction,
            "modelName": self._llm_model,
            "modelProvider": self._llm_provider,
        }
        resp = await self._client.post(
            f"{self._stagehand_url}/observe", json=payload
        )
        resp.raise_for_status()
        return resp.json()

    async def _stagehand_act(self, instruction: str) -> dict[str, Any]:
        payload = {
            "action": instruction,
            "modelName": self._llm_model,
            "modelProvider": self._llm_provider,
        }
        resp = await self._client.post(
            f"{self._stagehand_url}/act", json=payload
        )
        resp.raise_for_status()
        return resp.json()

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
                    locator, sel, tok = await self._resolve_element(page, step, test_case)
                    tokens_used += tok
                    resolved_selector = sel
                    if locator is None:
                        raise RuntimeError(
                            f"Element not found: {step.target.description}"  # type: ignore[union-attr]
                        )
                    await locator.click()

                case ActionType.TYPE:
                    locator, sel, tok = await self._resolve_element(page, step, test_case)
                    tokens_used += tok
                    resolved_selector = sel
                    if locator is None:
                        raise RuntimeError(
                            f"Element not found: {step.target.description}"  # type: ignore[union-attr]
                        )
                    value = step.data.value  # type: ignore[union-attr]
                    if step.data.clear_before:  # type: ignore[union-attr]
                        await locator.clear()
                    await locator.fill(value)

                case ActionType.SELECT:
                    locator, sel, tok = await self._resolve_element(page, step, test_case)
                    tokens_used += tok
                    resolved_selector = sel
                    if locator is None:
                        raise RuntimeError(
                            f"Element not found: {step.target.description}"  # type: ignore[union-attr]
                        )
                    await locator.select_option(label=step.data.value)  # type: ignore[union-attr]

                case ActionType.WAIT:
                    locator, sel, tok = await self._resolve_element(page, step, test_case)
                    tokens_used += tok
                    resolved_selector = sel
                    if locator is None:
                        raise RuntimeError(
                            f"Element not found: {step.target.description}"  # type: ignore[union-attr]
                        )

                case ActionType.ASSERT_VISIBLE:
                    locator, sel, tok = await self._resolve_element(page, step, test_case)
                    tokens_used += tok
                    resolved_selector = sel
                    if locator is None:
                        status = StepStatus.FAIL
                        error_message = (
                            f"Element not found after {step.timeout_ms}ms: "
                            f"{step.target.description}"  # type: ignore[union-attr]
                        )

                case ActionType.ASSERT_TEXT:
                    locator, sel, tok = await self._resolve_element(page, step, test_case)
                    tokens_used += tok
                    resolved_selector = sel
                    if locator is None:
                        status = StepStatus.FAIL
                        error_message = f"Element not found: {step.target.description}"  # type: ignore[union-attr]
                    else:
                        actual = await locator.text_content() or ""
                        expected = step.data.value  # type: ignore[union-attr]
                        if expected not in actual:
                            status = StepStatus.FAIL
                            error_message = f"Expected '{expected}' in '{actual}'"

                case ActionType.ASSERT_VALUE:
                    locator, sel, tok = await self._resolve_element(page, step, test_case)
                    tokens_used += tok
                    resolved_selector = sel
                    if locator is None:
                        status = StepStatus.FAIL
                        error_message = f"Element not found: {step.target.description}"  # type: ignore[union-attr]
                    else:
                        actual = await locator.input_value()
                        expected = step.data.value  # type: ignore[union-attr]
                        if str(expected) != str(actual):
                            status = StepStatus.FAIL
                            error_message = f"Expected value '{expected}', got '{actual}'"

                case ActionType.EXTRACT:
                    locator, sel, tok = await self._resolve_element(page, step, test_case)
                    tokens_used += tok
                    resolved_selector = sel
                    if locator is None:
                        status = StepStatus.FAIL
                        error_message = f"Element not found: {step.target.description}"  # type: ignore[union-attr]

                case ActionType.SCREENSHOT:
                    path = await self._take_screenshot(
                        page, test_case.test_id, step.step_id,
                        datetime.now().strftime("%Y%m%d")
                    )
                    resolved_selector = path

                case ActionType.CUSTOM:
                    desc = step.target.description  # type: ignore[union-attr]
                    result = await self._stagehand_act(desc)
                    tokens_used = result.get("tokens", 0)

        except PwTimeout as exc:
            status = StepStatus.FAIL
            error_message = f"TimeoutError: {exc}"
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
