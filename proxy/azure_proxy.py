"""LLM Proxy server for Stagehand calls.

Sits between the QA engines and any OpenAI-compatible LLM endpoint,
translating Stagehand's /act, /observe, /extract endpoints into
chat completions calls.

Supports:
  - Globant GeAI: https://api.clients.geai.globant.com/chat/completions
  - Azure OpenAI: {endpoint}/openai/deployments/{model}/chat/completions
  - Any OpenAI-compatible API

Security:
  - API key read from env var, never hardcoded
  - Request validation via Pydantic
  - Rate limiting
  - No credentials in logs
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from rich.console import Console
from rich.text import Text

_console = Console(stderr=True)

# ---------------------------------------------------------------------------
# Configuration — all from environment variables
# ---------------------------------------------------------------------------

PROXY_LLM_ENDPOINT = os.getenv("PROXY_LLM_ENDPOINT", "https://api.clients.geai.globant.com")
PROXY_LLM_API_KEY = os.getenv("PROXY_LLM_API_KEY", "")
PROXY_LLM_MODEL = os.getenv("PROXY_LLM_MODEL", "gpt-4o")
PROXY_PORT = int(os.getenv("PROXY_PORT", "3456"))

# Legacy Azure env vars as fallback
if not PROXY_LLM_API_KEY:
    PROXY_LLM_API_KEY = os.getenv("AZURE_OPENAI_API_KEY", "")
if not PROXY_LLM_MODEL:
    PROXY_LLM_MODEL = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")


def _get_completions_url() -> str:
    """Build the chat completions URL."""
    base = PROXY_LLM_ENDPOINT.rstrip("/")
    return f"{base}/chat/completions"


# ---------------------------------------------------------------------------
# Console logging helpers
# ---------------------------------------------------------------------------

def _log(icon: str, style: str, message: str, detail: str = "") -> None:
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    text = Text()
    text.append(f"{ts} ", style="dim")
    text.append(f"{icon} PROXY ", style=f"bold {style}")
    text.append(message)
    if detail:
        text.append(f" {detail}", style="dim")
    _console.print(text)


def _log_request(operation: str, instruction: str) -> None:
    _log("→", "cyan", f"{operation}()", f"| {instruction[:120]}{'...' if len(instruction) > 120 else ''}")


def _log_response(operation: str, result: dict, elapsed_ms: float) -> None:
    tokens = result.get("tokens", 0)
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    text = Text()
    text.append(f"{ts} ", style="dim")
    text.append("← PROXY ", style="bold green")
    text.append(f"{operation}() ", style="green")
    text.append(f"{elapsed_ms:.0f}ms ", style="dim")
    text.append(f"[{tokens} tok] ", style="dim cyan")

    if operation == "act":
        sel = result.get("selector", "")
        text.append(f"selector={sel[:80]}" if sel else "no selector", style="cyan" if sel else "yellow")
    elif operation == "observe":
        elements = result.get("elements", [])
        text.append(f"{len(elements)} elements", style="green" if elements else "yellow")
        for el in elements[:3]:
            text.append(f"\n           ↳ ", style="dim")
            text.append(el.get("selector", "?"), style="cyan")
            d = el.get("description", "")
            if d:
                text.append(f" — {d[:50]}", style="dim")
    elif operation == "extract":
        text.append(f"text='{result.get('text', '')[:60]}'", style="green")
    _console.print(text)


def _log_error(operation: str, status: int, body: str) -> None:
    _log("✖", "red", f"{operation}() HTTP {status}", f"| {body[:200]}")


def _log_upstream(method: str, url: str, model: str) -> None:
    _log("↑", "blue", f"{method} {url}", f"| model={model}")


# ---------------------------------------------------------------------------
# System prompts for each Stagehand operation
# ---------------------------------------------------------------------------

_SYSTEM_ACT = """You are a browser automation agent. The user describes an action to perform on a web page.
Respond with a JSON object containing:
- "success": true/false
- "selector": the CSS selector you would use to target the element (best guess)
- "action_performed": brief description of what was done
- "description": what the element is

IMPORTANT: Only use selectors based on attributes you can see in the page content. Never guess.
Respond ONLY with valid JSON, no markdown fences."""

_SYSTEM_OBSERVE = """You are a browser automation agent. The user asks you to identify elements on a web page.
Respond with a JSON object containing:
- "elements": array of objects, each with:
  - "selector": a valid CSS selector based on REAL attributes from the page
  - "description": what the element is

If no matching elements can be inferred, return an empty "elements" array.
Respond ONLY with valid JSON, no markdown fences."""

_SYSTEM_EXTRACT = """You are a browser automation agent. The user asks you to extract data from a web page.
Respond with a JSON object containing:
- "text": the extracted text content
- "value": the extracted value (if applicable)
- "data": any structured data extracted (object or null)

Respond ONLY with valid JSON, no markdown fences."""


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class ActRequest(BaseModel):
    action: str = Field(..., min_length=1, max_length=5000)
    modelName: Optional[str] = None
    modelProvider: Optional[str] = None


class ObserveRequest(BaseModel):
    instruction: str = Field(..., min_length=1, max_length=5000)
    modelName: Optional[str] = None
    modelProvider: Optional[str] = None


class ExtractRequest(BaseModel):
    instruction: str = Field(..., min_length=1, max_length=5000)
    modelName: Optional[str] = None
    modelProvider: Optional[str] = None


# ---------------------------------------------------------------------------
# LLM client
# ---------------------------------------------------------------------------

_http_client = httpx.AsyncClient(timeout=120.0)


async def _call_llm(
    system_prompt: str,
    user_message: str,
    operation: str,
    model_override: Optional[str] = None,
) -> dict[str, Any]:
    """Send a chat completion request and return parsed JSON."""
    url = _get_completions_url()
    api_key = PROXY_LLM_API_KEY
    model = model_override or PROXY_LLM_MODEL

    if not api_key:
        _log("✖", "red", "API key not configured — set PROXY_LLM_API_KEY in .env")
        raise HTTPException(status_code=503, detail="LLM API key not configured")

    # Log the outgoing request
    _log_request(operation, user_message[:200])
    _log_upstream("POST", url, model)

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.1,
        "max_tokens": 2048,
    }

    # Log payload size
    payload_str = json.dumps(payload)
    _log("·", "dim", f"Payload size: {len(payload_str)} chars | model={model}")

    start = time.monotonic()
    try:
        resp = await _http_client.post(url, json=payload, headers=headers)
    except httpx.ConnectError as exc:
        _log("✖", "red", f"CONNECTION FAILED → {url}", f"| {exc}")
        raise HTTPException(status_code=502, detail=f"Cannot connect to LLM endpoint: {url}")
    except httpx.TimeoutException:
        _log("✖", "red", f"TIMEOUT after 120s → {url}")
        raise HTTPException(status_code=502, detail="LLM request timed out")

    elapsed_ms = (time.monotonic() - start) * 1000

    if resp.status_code != 200:
        body = resp.text[:300] if resp.text else "(empty)"
        _log_error(operation, resp.status_code, body)
        raise HTTPException(
            status_code=502,
            detail=f"LLM upstream error: HTTP {resp.status_code}",
        )

    data = resp.json()

    # Log raw response structure
    _log("·", "dim", f"Response keys: {list(data.keys())}")

    # Extract the assistant message
    choices = data.get("choices", [])
    if not choices:
        _log("✖", "yellow", "Empty response — no choices returned")
        raise HTTPException(status_code=502, detail="Empty response from LLM")

    content = choices[0].get("message", {}).get("content", "")

    # Parse JSON from response (strip markdown fences if present)
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        _log("⚠", "yellow", f"LLM returned non-JSON: {content[:100]}")
        result = {"text": content, "tokens": 0}

    # Token tracking from API usage
    usage = data.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    total_tokens = usage.get("total_tokens", 0)
    if total_tokens == 0 and (prompt_tokens or completion_tokens):
        total_tokens = prompt_tokens + completion_tokens
    result["tokens"] = total_tokens

    _log("·", "dim", f"Tokens: prompt={prompt_tokens} completion={completion_tokens} total={total_tokens}")

    # Log the parsed result
    _log_response(operation, result, elapsed_ms)

    return result


# ---------------------------------------------------------------------------
# Simple rate limiter
# ---------------------------------------------------------------------------

_request_counts: dict[str, list[float]] = defaultdict(list)
_RATE_LIMIT = 30


def _check_rate_limit(client_ip: str) -> bool:
    now = time.monotonic()
    window = [t for t in _request_counts[client_ip] if now - t < 60]
    _request_counts[client_ip] = window
    if len(window) >= _RATE_LIMIT:
        return False
    _request_counts[client_ip].append(now)
    return True


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

def create_proxy_app() -> FastAPI:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _log("🚀", "green", "Proxy started")
        _log("·", "dim", f"Endpoint: {PROXY_LLM_ENDPOINT}")
        _log("·", "dim", f"URL: {_get_completions_url()}")
        _log("·", "dim", f"Model: {PROXY_LLM_MODEL}")
        _log("·", "dim", f"API key configured: {'YES' if PROXY_LLM_API_KEY else 'NO'}")
        yield

    app = FastAPI(
        title="Stagehand → LLM Proxy",
        version="2.0.0",
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def security_and_rate_limit(request: Request, call_next):
        client_ip = request.client.host if request.client else "unknown"
        if not _check_rate_limit(client_ip):
            return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health")
    async def health():
        return {
            "status": "healthy",
            "proxy": "llm_proxy",
            "endpoint": PROXY_LLM_ENDPOINT,
            "completions_url": _get_completions_url(),
            "model": PROXY_LLM_MODEL,
            "key_configured": bool(PROXY_LLM_API_KEY),
        }

    @app.post("/act")
    async def act(act_req: ActRequest):
        model = act_req.modelName or None
        result = await _call_llm(_SYSTEM_ACT, act_req.action, "act", model)
        return {
            "success": result.get("success", True),
            "selector": result.get("selector", ""),
            "action_performed": result.get("action_performed", act_req.action),
            "description": result.get("description", ""),
            "tokens": result.get("tokens", 0),
        }

    @app.post("/observe")
    async def observe(observe_req: ObserveRequest):
        model = observe_req.modelName or None
        result = await _call_llm(_SYSTEM_OBSERVE, observe_req.instruction, "observe", model)
        return {
            "elements": result.get("elements", []),
            "tokens": result.get("tokens", 0),
        }

    @app.post("/extract")
    async def extract(extract_req: ExtractRequest):
        model = extract_req.modelName or None
        result = await _call_llm(_SYSTEM_EXTRACT, extract_req.instruction, "extract", model)
        return {
            "text": result.get("text", ""),
            "value": result.get("value", ""),
            "data": result.get("data"),
            "tokens": result.get("tokens", 0),
        }

    return app


proxy_app = create_proxy_app()
