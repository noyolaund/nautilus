"""Direct LLM client for OpenAI-compatible APIs.

Supports any provider that exposes an OpenAI-compatible /chat/completions
endpoint: OpenAI, DeepSeek, Azure OpenAI, Anthropic (via proxy), local
models, etc.

Used as fallback when the Stagehand proxy is unavailable.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Optional

import httpx
from rich.console import Console
from rich.text import Text

logger = logging.getLogger("qa.llm_client")

_console = Console(stderr=True)


# ---------------------------------------------------------------------------
# Shared logging for Stagehand/proxy calls
# ---------------------------------------------------------------------------

def log_stagehand_request(operation: str, instruction: str, via: str = "proxy") -> None:
    """Log an outgoing Stagehand request to the console."""
    ts = datetime.now().strftime("%H:%M:%S")
    text = Text()
    text.append(f"{ts} ", style="dim")
    text.append("🔗 STG ", style="bold blue")
    text.append(f"[{via}] ", style="dim blue")
    text.append(f"{operation}() → ", style="blue")
    text.append(instruction[:120], style="white")
    if len(instruction) > 120:
        text.append("...", style="dim")
    _console.print(text)


def log_stagehand_response(
    operation: str, result: dict, elapsed_ms: float, via: str = "proxy"
) -> None:
    """Log a Stagehand response to the console."""
    ts = datetime.now().strftime("%H:%M:%S")
    tokens = result.get("tokens", 0)
    resp_text = Text()
    resp_text.append(f"{ts} ", style="dim")
    resp_text.append("🔗 STG ", style="bold green")
    resp_text.append(f"[{via}] ", style="dim green")
    resp_text.append(f"← {elapsed_ms:.0f}ms ", style="dim green")
    if tokens:
        resp_text.append(f"[{tokens} tok] ", style="dim cyan")

    if operation == "act":
        selector = result.get("selector", "")
        success = result.get("success", "?")
        resp_text.append(f"success={success}", style="green" if success else "red")
        if selector:
            resp_text.append(f"  selector=", style="dim")
            resp_text.append(selector[:80], style="cyan")
    elif operation == "observe":
        elements = result.get("elements", [])
        resp_text.append(f"{len(elements)} elements found", style="green" if elements else "yellow")
        for el in elements[:3]:
            resp_text.append(f"\n         ↳ ", style="dim")
            resp_text.append(el.get("selector", "?"), style="cyan")
            desc = el.get("description", "")
            if desc:
                resp_text.append(f" — {desc[:60]}", style="dim")
    elif operation == "extract":
        text_val = result.get("text", "")[:80]
        resp_text.append(f"text='{text_val}'", style="green")

    _console.print(resp_text)


def log_stagehand_error(operation: str, error: str, via: str = "proxy") -> None:
    """Log a Stagehand error to the console."""
    ts = datetime.now().strftime("%H:%M:%S")
    text = Text()
    text.append(f"{ts} ", style="dim")
    text.append("🔗 STG ", style="bold red")
    text.append(f"[{via}] ", style="dim red")
    text.append(f"{operation}() ERROR: ", style="red bold")
    text.append(error[:150], style="red")
    _console.print(text)

# Provider → base URL mapping for well-known providers
_PROVIDER_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
}


class LLMClient:
    """OpenAI-compatible chat completions client with provider auto-detection."""

    def __init__(
        self,
        provider: str = "",
        model: str = "",
        api_key: str = "",
        base_url: str = "",
    ) -> None:
        self._provider = provider or os.getenv("LLM_PROVIDER", "openai")
        self._model = model or os.getenv("LLM_MODEL", "gpt-4o")
        self._api_key = api_key or os.getenv("LLM_API_KEY", "")
        self._base_url = (
            base_url
            or os.getenv("LLM_BASE_URL", "")
            or _PROVIDER_URLS.get(self._provider, "")
        ).rstrip("/")
        self._client = httpx.AsyncClient(timeout=120.0)

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key and self._base_url)

    async def chat(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.1,
        max_tokens: int = 2048,
    ) -> dict[str, Any]:
        """Send a chat completion and return the parsed JSON response."""
        url = f"{self._base_url}/chat/completions"
        ts = datetime.now().strftime("%H:%M:%S")

        headers: dict[str, str] = {
            "Content-Type": "application/json",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        # Determine the prompt type from the system prompt
        prompt_type = "act"
        if "identify elements" in system_prompt:
            prompt_type = "observe"
        elif "extract data" in system_prompt:
            prompt_type = "extract"

        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        # --- Log: request ---
        req_text = Text()
        req_text.append(f"{ts} ", style="dim")
        req_text.append("🤖 LLM ", style="bold magenta")
        req_text.append(f"[{self._provider}/{self._model}] ", style="dim magenta")
        req_text.append(f"{prompt_type}() → ", style="magenta")
        req_text.append(user_message[:120], style="white")
        if len(user_message) > 120:
            req_text.append("...", style="dim")
        _console.print(req_text)

        start = time.monotonic()
        try:
            resp = await self._client.post(url, json=payload, headers=headers)
        except httpx.ConnectError as exc:
            err_text = Text()
            err_text.append(f"{ts} ", style="dim")
            err_text.append("🤖 LLM ", style="bold red")
            err_text.append(f"CONNECTION FAILED → {self._base_url}", style="red")
            _console.print(err_text)
            raise
        except httpx.TimeoutException:
            err_text = Text()
            err_text.append(f"{ts} ", style="dim")
            err_text.append("🤖 LLM ", style="bold red")
            err_text.append("TIMEOUT — no response within 120s", style="red")
            _console.print(err_text)
            raise

        elapsed_ms = (time.monotonic() - start) * 1000

        if resp.status_code != 200:
            err_text = Text()
            err_text.append(f"{ts} ", style="dim")
            err_text.append("🤖 LLM ", style="bold red")
            err_text.append(f"HTTP {resp.status_code} ", style="red bold")
            # Show truncated error body for debugging (mask any leaked keys)
            from utils.logger import mask_sensitive
            err_body = resp.text[:300] if resp.text else "(empty)"
            err_text.append(mask_sensitive(err_body), style="red")
            _console.print(err_text)
            resp.raise_for_status()

        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            warn_text = Text()
            warn_text.append(f"{ts} ", style="dim")
            warn_text.append("🤖 LLM ", style="bold yellow")
            warn_text.append("Empty response — no choices returned", style="yellow")
            _console.print(warn_text)
            return {"text": "", "tokens": 0}

        content = choices[0].get("message", {}).get("content", "")
        raw_content = content.strip()

        # Strip markdown fences if present
        content = raw_content
        if content.startswith("```"):
            content = content.split("\n", 1)[-1]
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()

        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            result = {"text": content, "tokens": 0}

        # Token tracking — always prefer the API usage stats over LLM output
        usage = data.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        total_tokens = usage.get("total_tokens", 0)
        # Some providers omit total_tokens but include the parts
        if total_tokens == 0 and (prompt_tokens or completion_tokens):
            total_tokens = prompt_tokens + completion_tokens
        # If API returned no usage at all, check completion_tokens/prompt_tokens variants
        if total_tokens == 0:
            # DeepSeek and some providers use different key names
            total_tokens = (
                usage.get("total_tokens", 0)
                or data.get("total_tokens", 0)
            )
        result["tokens"] = total_tokens
        result["prompt_tokens"] = prompt_tokens
        result["completion_tokens"] = completion_tokens

        # --- Log: response ---
        resp_text = Text()
        resp_text.append(f"{ts} ", style="dim")
        resp_text.append("🤖 LLM ", style="bold green")
        resp_text.append(f"← {elapsed_ms:.0f}ms ", style="dim green")
        resp_text.append(f"[{prompt_tokens}+{completion_tokens}={total_tokens} tok] ", style="dim cyan")
        # Show key fields from the parsed result
        if prompt_type == "act":
            selector = result.get("selector", "")
            success = result.get("success", "?")
            resp_text.append(f"success={success}", style="green" if success else "red")
            if selector:
                resp_text.append(f"  selector=", style="dim")
                resp_text.append(selector[:80], style="cyan")
        elif prompt_type == "observe":
            elements = result.get("elements", [])
            resp_text.append(f"{len(elements)} elements found", style="green" if elements else "yellow")
            for el in elements[:3]:
                resp_text.append(f"\n         ↳ ", style="dim")
                resp_text.append(el.get("selector", "?"), style="cyan")
                resp_text.append(f" — {el.get('description', '')[:60]}", style="dim")
        elif prompt_type == "extract":
            text_val = result.get("text", "")[:80]
            resp_text.append(f"text='{text_val}'", style="green")
        else:
            resp_text.append(raw_content[:100], style="dim")
        _console.print(resp_text)

        return result

    async def close(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# Stagehand-compatible system prompts (shared with proxy)
# ---------------------------------------------------------------------------

SYSTEM_ACT = """You are a browser automation agent. You will receive the current page structure (accessibility tree or interactive elements) followed by an action instruction.

Analyze the ACTUAL page content provided and find the REAL element that matches the instruction.

Respond with a JSON object containing:
- "selectors": an array of 5+ different CSS selectors for the SAME target element, using every available attribute. Include ALL of these that apply:
  - by id: "#myId"
  - by name: "input[name='x']"
  - by placeholder: "input[placeholder='x']"
  - by value: "input[value='x']"
  - by type+class: "input.className[type='text']"
  - by data-testid: "[data-testid='x']"
  - by aria-label: "[aria-label='x']"
  - by href: "a[href*='x']"
  - by role: "[role='button']"
- "selector": the single BEST selector from the list above
- "description": what the element is
- "success": true if you found a matching element, false if not

IMPORTANT: Only use selectors based on attributes you can actually see in the page content. Never guess. Return as many valid selectors as possible for the same element.
Respond ONLY with valid JSON, no markdown fences."""

SYSTEM_OBSERVE = """You are a browser automation agent. You will receive the current page structure (accessibility tree or interactive elements) followed by an instruction to find elements.

Analyze the ACTUAL page content provided and identify elements that match the instruction.

Respond with a JSON object containing:
- "elements": array of objects, each with:
  - "selectors": array of 5+ different CSS selectors for this element using every available attribute (id, name, placeholder, value, class, data-testid, aria-label, href, role, type)
  - "selector": the single BEST selector
  - "description": what the element is

IMPORTANT: Only return selectors based on attributes visible in the page content. Never invent selectors. Return as many valid selectors as possible per element. If no elements match, return an empty array.
Respond ONLY with valid JSON, no markdown fences."""

SYSTEM_EXTRACT = """You are a browser automation agent. You will receive the current page structure (accessibility tree or interactive elements) followed by an extraction instruction.

Analyze the ACTUAL page content provided and extract the requested data.

Respond with a JSON object containing:
- "text": the extracted text content
- "value": the extracted value (if applicable)
- "data": any structured data extracted (object or null)

Respond ONLY with valid JSON, no markdown fences."""
