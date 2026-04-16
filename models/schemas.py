"""Pydantic models for the QA Automation Framework.

Defines the JSON schema for test suites, test cases, steps, targets,
and all result/report structures.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    WAIT = "wait"
    ASSERT_VISIBLE = "assert_visible"
    ASSERT_TEXT = "assert_text"
    ASSERT_VALUE = "assert_value"
    EXTRACT = "extract"
    KEY_PRESS = "key_press"
    CHECK_ERROR = "check_error"
    RIGHT_CLICK = "right_click"
    SCREENSHOT = "screenshot"
    CUSTOM = "custom"


class SelectorStrategy(str, Enum):
    AI = "ai"
    CSS = "css"
    XPATH = "xpath"
    TEXT = "text"
    ROLE = "role"
    DATA_ATTR = "data_attr"
    UI5_STABLE = "ui5_stable"


class Platform(str, Enum):
    SAP_FIORI = "sap_fiori"
    SAP_WEBGUI = "sap_webgui"
    JDE_E1 = "jde_e1"
    SALESFORCE = "salesforce"
    DYNAMICS_365 = "dynamics_365"
    GENERIC_WEB = "generic_web"
    CUSTOM = "custom"


class StepStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    SKIP = "skip"


class TestStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"


class EngineType(str, Enum):
    AI_NATIVE = "ai_native"
    HYBRID = "hybrid"


class Priority(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# ---------------------------------------------------------------------------
# Platform-specific timeouts (ms)
# ---------------------------------------------------------------------------

PLATFORM_DEFAULTS: dict[str, dict[str, Any]] = {
    Platform.SAP_FIORI: {"timeout_ms": 60_000, "settle_ms": 3000},
    Platform.SAP_WEBGUI: {"timeout_ms": 45_000, "settle_ms": 2000},
    Platform.JDE_E1: {"timeout_ms": 45_000, "settle_ms": 2000},
    Platform.SALESFORCE: {"timeout_ms": 45_000, "settle_ms": 2000},
    Platform.DYNAMICS_365: {"timeout_ms": 40_000, "settle_ms": 1500},
    Platform.GENERIC_WEB: {"timeout_ms": 30_000, "settle_ms": 1000},
    Platform.CUSTOM: {"timeout_ms": 30_000, "settle_ms": 1000},
}
# Fallback for any unrecognized platform name
_DEFAULT_PLATFORM_CFG: dict[str, Any] = {"timeout_ms": 30_000, "settle_ms": 1000}


def get_platform_config(platform: str | Platform) -> dict[str, Any]:
    """Get platform config, falling back to defaults for custom platform names."""
    key = platform.value if isinstance(platform, Platform) else platform
    for k, v in PLATFORM_DEFAULTS.items():
        check = k.value if isinstance(k, Platform) else k
        if check == key:
            return v
    return _DEFAULT_PLATFORM_CFG


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class StepTarget(BaseModel):
    description: str = Field(..., min_length=1, max_length=500)
    selector: Optional[str] = Field(default=None, max_length=1000)
    selector_strategy: SelectorStrategy = SelectorStrategy.AI
    iframe: Optional[str] = Field(default=None, max_length=500)
    shadow_host: Optional[str] = Field(default=None, max_length=500)

    @field_validator("selector")
    @classmethod
    def sanitize_selector(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        # Block javascript: protocol in selectors to prevent injection
        if re.search(r"javascript\s*:", v, re.IGNORECASE):
            raise ValueError("Selector must not contain javascript: protocol")
        return v.strip()


class StepData(BaseModel):
    value: Optional[str] = Field(default=None, max_length=10_000)
    clear_before: bool = False
    sensitive: bool = False


class TestStep(BaseModel):
    step_id: str = Field(..., pattern=r"^S\d{3,6}$")
    name: str = Field(..., min_length=1, max_length=200)
    action: ActionType
    target: Optional[StepTarget] = None
    data: Optional[StepData] = None
    timeout_ms: int = Field(default=15_000, ge=1_000, le=300_000)
    retry_count: int = Field(default=2, ge=0, le=10)
    continue_on_failure: bool = False
    pre_wait_ms: int = Field(default=0, ge=0, le=30_000)
    screenshot_on_failure: bool = True

    @model_validator(mode="after")
    def validate_action_requirements(self) -> "TestStep":
        actions_need_target = {
            ActionType.CLICK, ActionType.TYPE, ActionType.SELECT,
            ActionType.WAIT, ActionType.ASSERT_VISIBLE, ActionType.ASSERT_TEXT,
            ActionType.ASSERT_VALUE, ActionType.EXTRACT, ActionType.CUSTOM,
            ActionType.CHECK_ERROR, ActionType.RIGHT_CLICK,
        }
        actions_need_data = {
            ActionType.NAVIGATE, ActionType.TYPE, ActionType.SELECT,
            ActionType.ASSERT_TEXT, ActionType.ASSERT_VALUE,
            ActionType.KEY_PRESS,
        }
        if self.action in actions_need_target and self.target is None:
            raise ValueError(f"Action '{self.action.value}' requires a target")
        if self.action in actions_need_data and (self.data is None or self.data.value is None):
            raise ValueError(f"Action '{self.action.value}' requires data.value")
        return self


class TestCase(BaseModel):
    test_id: str = Field(..., pattern=r"^TC-[\w-]{1,50}$")
    name: str = Field(..., min_length=1, max_length=300)
    description: str = Field(default="", max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    platform: str = Field(default=Platform.GENERIC_WEB, max_length=50)
    base_url: str = Field(..., min_length=1, max_length=2000)
    preconditions: str = Field(default="", max_length=2000)
    steps: list[TestStep] = Field(..., min_length=1, max_length=200)
    expected_result: str = Field(default="", max_length=2000)
    priority: Priority = Priority.MEDIUM

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        if not re.match(r"^https?://", v, re.IGNORECASE):
            raise ValueError("base_url must start with http:// or https://")
        return v


class TestSuiteRequest(BaseModel):
    suite_id: str = Field(..., pattern=r"^SUITE-[\w-]{1,50}$")
    suite_name: str = Field(..., min_length=1, max_length=300)
    environment: str = Field(default="staging", max_length=100)
    browser: str = Field(default="chromium", pattern=r"^(chromium|firefox|webkit)$")
    headless: bool = True
    llm_provider: str = Field(default="anthropic", max_length=50)
    llm_model: str = Field(default="claude-sonnet-4-20250514", max_length=100)
    parallel: bool = False
    test_cases: list[TestCase] = Field(..., min_length=1, max_length=100)


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------

class StepResult(BaseModel):
    step_id: str
    name: str
    action: ActionType
    status: StepStatus
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    duration_ms: float = 0.0
    tokens_used: int = 0
    resolved_selector: Optional[str] = None
    error_message: Optional[str] = None
    screenshot_path: Optional[str] = None


class TestResult(BaseModel):
    test_id: str
    name: str
    status: TestStatus
    platform: str
    steps: list[StepResult] = Field(default_factory=list)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    duration_ms: float = 0.0
    total_tokens: int = 0


class SuiteResult(BaseModel):
    suite_id: str
    suite_name: str
    environment: str
    browser: str
    engine_type: EngineType
    llm_provider: str
    llm_model: str
    test_results: list[TestResult] = Field(default_factory=list)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    total_duration_ms: float = 0.0
    total_tokens: int = 0

    @property
    def total_tests(self) -> int:
        return len(self.test_results)

    @property
    def passed(self) -> int:
        return sum(1 for t in self.test_results if t.status == TestStatus.PASS)

    @property
    def failed(self) -> int:
        return sum(1 for t in self.test_results if t.status == TestStatus.FAIL)

    @property
    def errors(self) -> int:
        return sum(1 for t in self.test_results if t.status == TestStatus.ERROR)

    @property
    def pass_rate(self) -> float:
        if self.total_tests == 0:
            return 0.0
        return (self.passed / self.total_tests) * 100
