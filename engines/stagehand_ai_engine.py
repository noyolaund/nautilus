"""Implementation A — Stagehand AI-Native engine.

Every element interaction goes through the LLM via Stagehand's
act() / observe() / extract() API. Best for SAP Fiori with dynamic IDs,
Shadow DOM, and deep nesting.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from playwright.async_api import Page

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


class StagehandAIEngine(BaseEngine):
    """AI-Native engine that delegates every step to Stagehand's LLM."""

    engine_type = EngineType.AI_NATIVE

    def __init__(self, request: TestSuiteRequest) -> None:
        super().__init__(request)
        self._stagehand_url = os.getenv(
            "STAGEHAND_SERVER_URL", "http://localhost:3001"
        ).rstrip("/")
        self._llm_provider = request.llm_provider
        self._llm_model = request.llm_model
        self._client = httpx.AsyncClient(timeout=120.0)

    # ------------------------------------------------------------------
    # Stagehand RPC helpers
    # ------------------------------------------------------------------

    async def _stagehand_act(self, page: Page, instruction: str) -> dict[str, Any]:
        """Call Stagehand act() — performs an action described in natural language."""
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

    async def _stagehand_observe(self, page: Page, instruction: str) -> dict[str, Any]:
        """Call Stagehand observe() — identifies elements on the page."""
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

    async def _stagehand_extract(self, page: Page, instruction: str) -> dict[str, Any]:
        """Call Stagehand extract() — extracts structured data from the page."""
        payload = {
            "instruction": instruction,
            "modelName": self._llm_model,
            "modelProvider": self._llm_provider,
        }
        resp = await self._client.post(
            f"{self._stagehand_url}/extract", json=payload
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
                    desc = step.target.description  # type: ignore[union-attr]
                    result = await self._stagehand_act(
                        page, f"Click on {desc}"
                    )
                    tokens_used = result.get("tokens", 0)
                    resolved_selector = result.get("selector", "ai_resolved")

                case ActionType.TYPE:
                    desc = step.target.description  # type: ignore[union-attr]
                    value = step.data.value  # type: ignore[union-attr]
                    display_value = "****" if step.data.sensitive else value  # type: ignore[union-attr]
                    if step.data.clear_before:  # type: ignore[union-attr]
                        await self._stagehand_act(
                            page, f"Clear the text in {desc}"
                        )
                    result = await self._stagehand_act(
                        page, f"Type '{display_value}' into {desc}"
                    )
                    tokens_used = result.get("tokens", 0)
                    resolved_selector = result.get("selector", "ai_resolved")

                case ActionType.SELECT:
                    desc = step.target.description  # type: ignore[union-attr]
                    option = step.data.value  # type: ignore[union-attr]
                    result = await self._stagehand_act(
                        page, f"Select '{option}' from {desc}"
                    )
                    tokens_used = result.get("tokens", 0)

                case ActionType.WAIT:
                    desc = step.target.description  # type: ignore[union-attr]
                    result = await self._stagehand_observe(
                        page, f"Wait until {desc} is visible on the page"
                    )
                    tokens_used = result.get("tokens", 0)

                case ActionType.ASSERT_VISIBLE:
                    desc = step.target.description  # type: ignore[union-attr]
                    result = await self._stagehand_observe(
                        page, f"Find {desc} on the page"
                    )
                    tokens_used = result.get("tokens", 0)
                    if not result.get("elements"):
                        status = StepStatus.FAIL
                        error_message = f"Element not found: {desc}"

                case ActionType.ASSERT_TEXT:
                    desc = step.target.description  # type: ignore[union-attr]
                    expected = step.data.value  # type: ignore[union-attr]
                    result = await self._stagehand_extract(
                        page, f"Extract the text from {desc}"
                    )
                    tokens_used = result.get("tokens", 0)
                    actual = result.get("text", "")
                    if expected not in actual:
                        status = StepStatus.FAIL
                        error_message = f"Expected '{expected}' in '{actual}'"

                case ActionType.ASSERT_VALUE:
                    desc = step.target.description  # type: ignore[union-attr]
                    expected = step.data.value  # type: ignore[union-attr]
                    result = await self._stagehand_extract(
                        page, f"Extract the value of {desc}"
                    )
                    tokens_used = result.get("tokens", 0)
                    actual = result.get("value", "")
                    if str(expected) != str(actual):
                        status = StepStatus.FAIL
                        error_message = f"Expected value '{expected}', got '{actual}'"

                case ActionType.EXTRACT:
                    desc = step.target.description  # type: ignore[union-attr]
                    result = await self._stagehand_extract(
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
                    result = await self._stagehand_act(page, desc)
                    tokens_used = result.get("tokens", 0)

        except httpx.HTTPStatusError as exc:
            status = StepStatus.ERROR
            error_message = f"Stagehand API error: {exc.response.status_code}"
        except httpx.ConnectError:
            status = StepStatus.ERROR
            error_message = "Cannot connect to Stagehand server"
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
